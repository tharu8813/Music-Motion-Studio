"""The AutoMix mix and its loudness pass on every core.

One FFmpeg used to fold the whole mix, a second to measure it and a third to
normalize it: three single-threaded passes over the entire playlist, 98.6 s
for 96 min of music on a 20-thread CPU. Here every stage runs on parts at once:

1. clips -- each clip's own filters (the render graph's first half, see
   ``graph_parts``), one FFmpeg per clip;
2. fold -- the playlist cut at seams inside a clip's plain body, where the mix
   *is* that one pre-rendered clip; each part folds its own clips, so across a
   seam the parts reproduce the one-graph fold sample for sample;
3. loudness -- each part measured in EBU R128's 400 ms blocks, and the blocks
   of all parts gated together into the playlist's integrated loudness;
4. normalize -- each part through the loudness policy's filter, with a
   one-second run-up from its own clip so the stateful filters (resampler,
   true-peak limiter) are settled when the part's first sample comes;
5. encode -- one FFmpeg joins the normalized parts into the final file.

Intermediate files are raw 48 kHz stereo float32: a file's length in samples
is its size / 8, which the seams need exactly.
"""

from __future__ import annotations

from app.utils.performance import timed

import logging
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from app.automix.renderer import (
    INLINE_FILTER_GRAPH_LIMIT,
    SAMPLE_RATE,
    AutoMixAudioPipeline,
    AutoMixRenderCancelled,
    AutoMixRenderError,
    ProgressCallback,
    ffmpeg_filter_names,
    ffmpeg_supports_transition_dsp,
    graph_parts,
    transition_dsp_style,
)
from app.ffmpeg.audio_effects import audio_effect_graph
from app.models.audio_effects import AudioEffectSettings
from app.renderer import loudness
from app.timeline.render_plan import AudioRenderPlan, TransitionDsp
from app.utils.subprocess_utils import lower_thread_if_background

LOGGER = logging.getLogger(__name__)

RAW = ["-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2"]
"""Input options for an intermediate file (raw float32 stereo, no header)."""
FRAME_BYTES = 8
SEAM_MARGIN_SECONDS = 1.0
"""A seam keeps this far from either transition of its clip."""
RUN_UP_SECONDS = 1.0
MIN_PART_SECONDS = 60.0
BLOCK_ABSOLUTE_GATE = -70.0
BLOCK_RELATIVE_GATE = 10.0


@dataclass(frozen=True, slots=True)
class MixPart:
    """Part of the mix: samples ``first`` .. ``first + count`` of ``path``.

    ``run_up`` samples before ``first`` are the mix too (the same clip), for
    filters that need to settle before the part starts.
    """

    path: Path
    first: int
    count: int
    run_up: int = 0


def _samples(path: Path) -> int:
    return path.stat().st_size // FRAME_BYTES


def _command(
    executable: Path, inputs: Sequence[Path], filter_complex: str, label: str, output: Path | str,
    output_args: Sequence[str], script: Path, loglevel: str = "error",
) -> list[str]:
    graph = ["-filter_complex", filter_complex]
    if len(filter_complex) > INLINE_FILTER_GRAPH_LIMIT:
        script.write_text(filter_complex, encoding="utf-8")
        graph = ["-/filter_complex", str(script)]
    arguments = [str(executable), "-hide_banner", "-loglevel", loglevel, "-nostdin"]
    for path in inputs:
        arguments.extend([*(RAW if Path(path).suffix == ".f32" else []), "-i", str(path)])
    return [*arguments, *graph, "-map", f"[{label}]", *output_args,
            "-progress", "pipe:1", "-nostats", "-y", str(output)]


def _parallel(workers: int, jobs: Sequence[Callable[[], object]]) -> list[object]:
    """Run ``jobs`` on ``workers`` threads; the first real failure (not a cancel it caused) is raised."""
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="automix-part",
                            initializer=lower_thread_if_background) as pool:
        futures = [pool.submit(job) for job in jobs]
    errors = [future.exception() for future in futures if future.exception() is not None]
    if errors:
        raise next((error for error in errors if not isinstance(error, AutoMixRenderCancelled)), errors[0])
    return [future.result() for future in futures]


def plan_seams(plan: AudioRenderPlan, workers: int) -> list[tuple[int, float]]:
    """(clip index, clip-local seconds) to cut the mix at, about ``workers`` parts' worth.

    Only inside a clip's plain body: SEAM_MARGIN_SECONDS + RUN_UP_SECONDS after
    its incoming transition ends and SEAM_MARGIN_SECONDS before its outgoing
    one starts, so a part's run-up is that clip alone too.
    """
    clips = plan.clips
    total = max(clip.timeline_end for clip in clips)
    target = max(MIN_PART_SECONDS, total / max(1, workers))
    into = {t.clip_b: t for t in plan.transitions if t.duration > 0.0}
    out_of = {t.clip_a: t for t in plan.transitions if t.duration > 0.0}
    seams: list[tuple[int, float]] = []
    last = 0.0
    for index, clip in enumerate(clips):
        incoming, outgoing = into.get(clip.clip_id), out_of.get(clip.clip_id)
        low = (incoming.timeline_start + incoming.duration - clip.timeline_start if incoming else 0.0)
        low += SEAM_MARGIN_SECONDS + RUN_UP_SECONDS
        high = (outgoing.timeline_start - clip.timeline_start if outgoing else clip.duration) - SEAM_MARGIN_SECONDS
        if high <= low:
            continue
        local = min(max(last + target - clip.timeline_start, low), high)
        position = clip.timeline_start + local
        if position - last < 0.6 * target or total - position < 0.4 * target:
            continue
        seams.append((index, local))
        last = position
    return seams


@timed("automix.parallel_mix_seconds")
def render_parts(
    pipeline: AutoMixAudioPipeline,
    plan: AudioRenderPlan,
    track_paths: Mapping[str, str],
    directory: Path,
    *,
    workers: int,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
    track_filters: Mapping[str, str] | None = None,
) -> list[MixPart]:
    """``AutoMixAudioPipeline.render``'s mix, as parts rendered on ``workers`` FFmpegs at once."""
    cancel_event = cancel_event or threading.Event()
    clips = plan.clips
    if not clips:
        raise AutoMixRenderError("Cannot render an AudioRenderPlan with no clips.")
    missing = [clip for clip in clips if not Path(track_paths.get(clip.track_id, "")).is_file()]
    if missing:
        raise AutoMixRenderError(f"Audio file is missing for track: {missing[0].track_id}")
    executable = pipeline.ffmpeg_executable
    use_dsp = any(transition_dsp_style(t) is not None for t in plan.transitions)
    if use_dsp and not ffmpeg_supports_transition_dsp(str(executable)):
        LOGGER.warning("FFmpeg lacks the transition DSP filters; styled transitions use their legacy crossfade.")
        use_dsp = False
    needs_stretcher = any(clip.tempo_ramp for clip in clips) or any(
        transition_dsp_style(t) is TransitionDsp.TAPE_STOP for t in plan.transitions)
    ramp_filter = "rubberband" if needs_stretcher and "rubberband" in ffmpeg_filter_names(str(executable)) else None
    work = directory / "automix_parts"
    work.mkdir(parents=True, exist_ok=True)
    sources = [Path(track_paths[clip.track_id]) for clip in clips]
    total = max(clip.timeline_end for clip in clips)
    raw_out = ["-c:a", "pcm_f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-f", "f32le"]

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress("Combining mix", fraction, message)

    def attempt(dsp: bool) -> list[MixPart]:
        per_clip, _fold, _label = graph_parts(clips, plan.transitions, transition_dsp=dsp,
                                              ramp_filter=ramp_filter, track_filters=track_filters)
        clip_paths = [work / f"clip_{index:03d}.f32" for index in range(len(clips))]
        done = [0]
        lock = threading.Lock()

        def clip_job(index: int) -> Callable[[], None]:
            def run() -> None:
                graph = ";".join(line.replace(f"[{index}:a]", "[0:a]") for line in per_clip[index])
                pipeline._run(_command(executable, [sources[index]], graph, f"c{index}", clip_paths[index],
                                       raw_out, work / f"clip_{index:03d}.txt"), cancel_event, None)
                with lock:
                    done[0] += 1
                    count = done[0]
                report(0.1 + 0.45 * count / len(clips), f"Rendering clips {count}/{len(clips)}")
            return run

        _parallel(workers, [clip_job(index) for index in range(len(clips))])
        lengths = [_samples(path) for path in clip_paths]

        seams = plan_seams(plan, workers)
        bounds = [0, *(index for index, _local in seams), len(clips) - 1]
        cuts = {index: round(local * SAMPLE_RATE) for index, local in seams}
        part_paths = [work / f"part_{k:03d}.f32" for k in range(len(bounds) - 1)]
        folded = [0]

        def fold_job(k: int) -> Callable[[], None]:
            def run() -> None:
                first, last = bounds[k], bounds[k + 1]
                origin = clips[first].timeline_start if k else 0.0
                ids = {clip.clip_id for clip in clips[first:last + 1]}
                sub_clips = [replace(clip, timeline_start=clip.timeline_start - origin)
                             for clip in clips[first:last + 1]]
                sub_transitions = [replace(t, timeline_start=t.timeline_start - origin)
                                   for t in plan.transitions if t.clip_a in ids and t.clip_b in ids]
                _per_clip, fold, label = graph_parts(sub_clips, sub_transitions, transition_dsp=dsp,
                                                     ramp_filter=ramp_filter)
                inputs = [f"[{i}:a]anull[c{i}]" for i in range(len(sub_clips))]
                pipeline._run(_command(executable, clip_paths[first:last + 1], ";".join([*inputs, *fold]), label,
                                       part_paths[k], raw_out, work / f"part_{k:03d}.txt"), cancel_event, None)
                with lock:
                    folded[0] += 1
                    count = folded[0]
                report(0.55 + 0.3 * count / len(part_paths), f"Combining mix {count}/{len(part_paths)}")
            return run

        _parallel(workers, [fold_job(k) for k in range(len(part_paths))])
        parts = []
        for k, path in enumerate(part_paths):
            size = _samples(path)
            start = cuts[bounds[k]] if k else 0
            end = size - (lengths[bounds[k + 1]] - cuts[bounds[k + 1]]) if k < len(part_paths) - 1 else size
            run_up = min(start, round(RUN_UP_SECONDS * SAMPLE_RATE)) if k else 0
            if end <= start:
                raise AutoMixRenderError(f"AutoMix part {k} came out empty.")
            parts.append(MixPart(path, start, end - start, run_up))
        rendered = sum(part.count for part in parts) / SAMPLE_RATE
        if abs(rendered - total) > 0.15:
            raise AutoMixRenderError(
                f"Rendered AutoMix audio duration ({rendered:.3f}s) does not match the compiled plan ({total:.3f}s).")
        return parts

    try:
        try:
            parts = attempt(use_dsp)
        except AutoMixRenderCancelled:
            raise
        except AutoMixRenderError as error:
            if not use_dsp:
                raise
            LOGGER.warning("Transition DSP render failed (%s); retrying with legacy crossfades.", error)
            parts = attempt(False)
    except AutoMixRenderError:
        cleanup(directory)
        raise
    for path in work.glob("clip_*"):
        path.unlink(missing_ok=True)
    report(0.9, "AutoMix audio ready")
    return parts


def cleanup(directory: Path) -> None:
    work = directory / "automix_parts"
    for path in work.glob("*"):
        path.unlink(missing_ok=True)


_M_LINE = re.compile(r"\bt:\s*([\d.]+)\s+TARGET:.*?\bM:\s*(-?(?:[\d.]+|inf))")
_TRUE_PEAK = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?(?:[\d.]+|inf))")


def integrated_loudness(blocks: Sequence[float]) -> float:
    """BS.1770 integrated loudness of momentary (400 ms, 75 % overlap) block loudnesses, gated."""

    def mean_loudness(values: Sequence[float]) -> float:
        return -0.691 + 10 * math.log10(sum(10 ** ((value + 0.691) / 10) for value in values) / len(values))

    audible = [block for block in blocks if block > BLOCK_ABSOLUTE_GATE]
    if not audible:
        return -math.inf
    relative = mean_loudness(audible) - BLOCK_RELATIVE_GATE
    gated = [block for block in audible if block > relative]
    return mean_loudness(gated) if gated else -math.inf


def finish_parts(
    pipeline: AutoMixAudioPipeline, parts: Sequence[MixPart], output: Path, output_args: Sequence[str],
    *, workers: int, cancel_event: threading.Event | None = None,
    progress: Callable[[float, str], None] | None = None,
    audio_effects: AudioEffectSettings | None = None,
) -> str | None:
    """Measure, normalize and encode ``parts`` into ``output``; returns the loudness filter used (None: none)."""
    cancel_event = cancel_event or threading.Event()
    executable = pipeline.ffmpeg_executable
    work = parts[0].path.parent

    def say(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    measured = [0]
    lock = threading.Lock()

    def measure_job(k: int, part: MixPart) -> Callable[[], tuple[list[float], float]]:
        def run() -> tuple[list[float], float]:
            graph = (f"[0:a]atrim=start_sample={part.first}:end_sample={part.first + part.count},"
                     f"ebur128=peak=true:framelog=info[m]")
            text = pipeline._run(_command(executable, [part.path], graph, "m", "-", ["-f", "null"],
                                          work / f"measure_{k:03d}.txt", loglevel="info"), cancel_event, None)
            # A part's first blocks would reach back before it: the part before measured them.
            blocks = [float(m) for t, m in _M_LINE.findall(text) if float(t) >= (0.4 if k else 0.0)]
            peaks = _TRUE_PEAK.findall(text)
            with lock:
                measured[0] += 1
                say(0.3 * measured[0] / len(parts), f"Measuring loudness {measured[0]}/{len(parts)}")
            return blocks, (float(peaks[-1]) if peaks else -math.inf)
        return run

    results = _parallel(workers, [measure_job(k, part) for k, part in enumerate(parts)])
    blocks = [block for part_blocks, _peak in results for block in part_blocks]
    integrated = integrated_loudness(blocks)
    true_peak = max(peak for _blocks, peak in results)
    chain = None
    if math.isfinite(integrated) and math.isfinite(true_peak):
        chain = loudness.normalization_filter(integrated, true_peak)
        LOGGER.info("Loudness: %.1f LUFS, %.1f dBTP (%d parts) -> %s", integrated, true_peak, len(parts), chain)
    else:
        LOGGER.info("Mix measured %.1f LUFS (near-silent or unmeasurable); skipping loudness normalization",
                    integrated)

    normalized = [work / f"normalized_{k:03d}.f32" for k in range(len(parts))]
    finished = [0]

    def normalize_job(k: int, part: MixPart) -> Callable[[], None]:
        def run() -> None:
            graph = (f"[0:a]atrim=start_sample={part.first - part.run_up}:end_sample={part.first + part.count},"
                     f"asetpts=PTS-STARTPTS,{chain + ',' if chain else ''}"
                     f"atrim=start_sample={part.run_up},asetpts=PTS-STARTPTS,"
                     f"apad=whole_len={part.count},atrim=end_sample={part.count}[n]")
            pipeline._run(_command(executable, [part.path], graph, "n", normalized[k],
                                   ["-c:a", "pcm_f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-f", "f32le"],
                                   work / f"normalize_{k:03d}.txt"), cancel_event, None)
            with lock:
                finished[0] += 1
                say(0.3 + 0.4 * finished[0] / len(parts), f"Normalizing loudness {finished[0]}/{len(parts)}")
        return run

    try:
        _parallel(workers, [normalize_job(k, part) for k, part in enumerate(parts)])
        for part in parts:
            part.path.unlink(missing_ok=True)
        total = sum(part.count for part in parts) / SAMPLE_RATE
        joined = "".join(f"[{k}:a]" for k in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]"
        effects = audio_effect_graph(audio_effects, "out", "master", total)
        if effects:
            joined += ";" + effects
        pipeline._run(
            _command(executable, normalized, joined, "master" if effects else "out", output, output_args,
                     work / "encode.txt"),
            cancel_event,
            lambda seconds: say(0.7 + 0.3 * min(1.0, seconds / max(0.01, total)),
                                f"Saving audio {seconds:.1f}s / {total:.1f}s"),
        )
    finally:
        for path in (*normalized, *(part.path for part in parts), *work.glob("*.txt")):
            path.unlink(missing_ok=True)
    return chain
