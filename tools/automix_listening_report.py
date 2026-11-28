"""AutoMix listening/tuning report for real music (developer tool, not shipped).

    python tools/automix_listening_report.py OUT_DIR SONG1 SONG2 [SONG3 ...] [--ffmpeg PATH] [--ab]
    python tools/automix_listening_report.py OUT_DIR PLAYLIST.m3u8 --blind

Analyzes the songs in the given order exactly like Export does (rhythm
provider "auto", Sonara structure when installed,
the persistent caches), compiles the AutoMix plan, renders the whole mix to
``OUT_DIR/automix_mix.flac`` and writes ``report.csv``/``report.json``: one row
per junction with every planner number (``app.automix.diagnostics``) plus
objective window metrics (level dip/peak, low-end build-up) measured on the
rendered mix.

``--ab`` also renders each transition as a short excerpt once per DSP style
(``OUT_DIR/ab/<n>_<style>.flac``) with the same geometry, and measures each,
for side-by-side listening. ``ratings.csv`` is the blank sheet a human listener
fills in per transition (protocol: docs/automix-history/followup/02_real_music_listening_and_tuning.md).
``--blind`` packages anonymously ordered A/B clips for EQ transitions. The
control uses a regular equal-power fade at the exact same cues and duration.
Never commit the audio files or reports.
"""

from __future__ import annotations

import argparse
import csv
import random
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.automix.analysis.registry import create_analysis_provider  # noqa: E402
from app.automix.analysis.service import AnalysisService  # noqa: E402
from app.automix.diagnostics import rows_to_csv, rows_to_json, transition_rows, window_metrics  # noqa: E402
from app.automix.planner import compile_automix  # noqa: E402
from app.automix.progressive import render_window  # noqa: E402
from app.automix.renderer import BAND_ENVELOPES, AutoMixAudioPipeline, settings_for_ffmpeg, transition_dsp_style  # noqa: E402
from app.automix.settings import AUTOMIX_SETTINGS  # noqa: E402
from app.models.playlist import PlaylistTrack  # noqa: E402
from app.renderer.ffmpeg_renderer import FFmpegRenderer  # noqa: E402
from app.services.m3u_playlist import M3uPlaylistError, PLAYLIST_FILE_EXTENSIONS, read_m3u  # noqa: E402
from app.timeline.crossfade import compile_crossfade  # noqa: E402
from app.timeline.models import TransitionType  # noqa: E402
from app.timeline.render_plan import AudioRenderPlan, TransitionDsp  # noqa: E402

METRIC_RATE = 22050
EXCERPT_CONTEXT_SECONDS = 8.0
AB_STYLES = ("legacy", *(style.value for style in TransitionDsp))
# 1-5 scores: 5 = best (natural timing / no clash / no energy hole).
RATING_COLUMNS = (
    "timing_1to5", "vocal_clash_1to5", "low_end_clash_1to5", "energy_drop_1to5",
    "length_short_ok_long", "preferred_ab_style", "preferred_mix", "comment",
)


def write_rating_sheet(path: Path, rows: list[dict]) -> None:
    """One blank row per real transition; utf-8-sig so Excel shows Korean titles."""
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(("index", "from", "to", "start", "style", "duration_s", *RATING_COLUMNS))
        for row in rows:
            if row["type"] == "gap":
                continue
            start = int(row["timeline_start"])
            writer.writerow((
                row["index"], row["from"], row["to"], f"{start // 60}:{start % 60:02d}",
                row["dsp"] or row["type"], f"{row['duration']:.1f}", *("" for _ in RATING_COLUMNS),
            ))


def probe_duration(ffmpeg: Path, path: Path) -> float:
    probe = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    result = subprocess.run(
        [str(probe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def decode_mono(ffmpeg: Path, path: Path) -> np.ndarray:
    result = subprocess.run(
        [str(ffmpeg), "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(METRIC_RATE), "-f", "f32le", "pipe:1"],
        capture_output=True, check=True,
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def write_excerpt(ffmpeg: Path, source: Path, output: Path, start: float, duration: float) -> None:
    subprocess.run(
        [str(ffmpeg), "-v", "error", "-nostdin", "-y", "-ss", f"{start:.6f}",
         "-i", str(source), "-t", f"{duration:.6f}", str(output)], check=True,
    )


def compare_mixes(ffmpeg, out_dir, variants, titles):
    """Measure and excerpt the rendered junctions, including cuts with no overlap."""
    comparisons = []
    for name, (compiled, rendered, samples) in variants.items():
        effective = replace(compiled, audio=rendered.audio_plan) if rendered.audio_plan is not None else compiled
        for row in transition_rows(effective, titles):
            index = row["index"]
            start, duration = row["timeline_start"], row["duration"]
            excerpt_start = max(0.0, start - EXCERPT_CONTEXT_SECONDS)
            excerpt_duration = start + duration + EXCERPT_CONTEXT_SECONDS - excerpt_start
            output = out_dir / f"{index:02d}_{name}.flac"
            write_excerpt(ffmpeg, rendered.path, output, excerpt_start, excerpt_duration)
            comparisons.append({**row, "version": name, "style": row["dsp"] or row["type"], "audio": str(output),
                                **window_metrics(samples, METRIC_RATE, start, duration)})
    return comparisons


def write_blind_comparison(ffmpeg, pipeline, plan, tracks, out_dir) -> int:
    """Render each matched pair from the same audition window and keep the key separate."""
    tracks_by_id = {track.id: track for track in tracks}
    transitions = {(t.clip_a, t.clip_b): t for t in plan.audio.transitions}
    ratings, key = [], []
    rng = random.SystemRandom()
    with TemporaryDirectory(dir=out_dir) as temporary:
        directory = Path(temporary)
        for index, (outgoing, incoming) in enumerate(zip(plan.audio.clips, plan.audio.clips[1:])):
            transition = transitions.get((outgoing.clip_id, incoming.clip_id))
            if transition is None or transition.duration <= 0:
                continue
            style = transition_dsp_style(transition)
            if style not in BAND_ENVELOPES:
                continue
            window = render_window(plan, index, 1.0, preroll=EXCERPT_CONTEXT_SECONDS, postroll=EXCERPT_CONTEXT_SECONDS)
            control = replace(window.plan.transitions[0], dsp=TransitionDsp.SHORT_FADE, band_windows=None, vocal_handoff=None)
            variants = [("automix", window.plan), ("matched_fade", replace(window.plan, transitions=(control,)))]
            rng.shuffle(variants)
            # Keep the crossover's phase response in both variants, including the solo sections.
            resting = {clip.clip_id: (True, window.resting_dsp.get(clip.clip_id, (False, False))[1])
                       for clip in window.plan.clips}
            pair = [tracks_by_id[clip.track_id] for clip in window.plan.clips]
            paths = {track.id: track.file_path for track in pair}
            filters = {track.id: track.audio_filter for track in pair}
            lead = max(window.start, transition.timeline_start - EXCERPT_CONTEXT_SECONDS) - window.origin
            duration = window.end - window.origin - lead
            label = f"{index + 1:02d}"
            ratings.append({"pair": label, "from": pair[0].title, "to": pair[1].title,
                            "transition_at_s": transition.timeline_start - window.origin - lead,
                            "preferred_A_B_tie": "", "reason": ""})
            for letter, (version, audio) in zip("AB", variants, strict=True):
                filename = f"{label}_{letter}.flac"
                with TemporaryDirectory(dir=directory) as scratch:
                    rendered = pipeline.render(audio, paths, Path(scratch), container="flac",
                                               resting_dsp=resting, track_filters=filters)
                    write_excerpt(ffmpeg, rendered.path, directory / filename, lead, duration)
                key.append({"pair": label, "file": filename, "version": version,
                            "style": style.value if version == "automix" else control.dsp.value})
        (directory / "ratings.csv").write_text(rows_to_csv(ratings), encoding="utf-8-sig")
        (directory / "README.txt").write_text(
            f"전환 비교 {len(ratings)}건\n"
            "같은 번호의 A와 B를 같은 볼륨으로 듣고 더 자연스러운 쪽을 ratings.csv에 기록하세요.\n"
            "두 파일의 전환 시작은 transition_at_s에 표시됩니다. 차이가 없으면 tie를 기록하세요.\n"
            "박자, 보컬, 저음, 순간 음량 가운데 선택에 영향을 준 부분을 reason에 적어 주세요.\n",
            encoding="utf-8-sig",
        )
        archive = directory / "blind.zip"
        with ZipFile(archive, "w") as bundle:
            for path in sorted(directory.iterdir()):
                if path != archive:
                    bundle.write(path, path.name)
        (directory / "blind-key.json").write_text(rows_to_json(key), encoding="utf-8")
        archive.replace(out_dir / archive.name)
        (directory / "blind-key.json").replace(out_dir / "blind-key.json")
    return len(ratings)


def excerpt_plan(plan, transition, style: str) -> tuple[AudioRenderPlan, float]:
    """Just this transition plus EXCERPT_CONTEXT_SECONDS of solo audio each side, same geometry."""
    clips = {clip.clip_id: clip for clip in plan.audio.clips}
    outgoing, incoming = clips[transition.clip_a], clips[transition.clip_b]
    cue = outgoing.source_at(transition.timeline_start)
    source_in = outgoing.source_at(max(outgoing.timeline_start, transition.timeline_start - EXCERPT_CONTEXT_SECONDS))
    lead = transition.timeline_start - outgoing.timeline_at(source_in)
    outgoing = replace(outgoing, timeline_start=0.0, source_in=source_in, gain=1.0)
    incoming = replace(
        incoming, timeline_start=lead, gain=1.0,
        source_out=min(incoming.source_out,
                       incoming.source_in + (transition.duration + EXCERPT_CONTEXT_SECONDS) * incoming.playback_rate),
    )
    if style == "legacy":
        styled = replace(transition, timeline_start=lead, dsp=None,
                         type=TransitionType.EQUAL_POWER if transition.type is TransitionType.BEAT_MATCH else transition.type)
    else:
        selected = TransitionDsp(style)
        styled = replace(transition, timeline_start=lead, dsp=selected,
                         band_windows=transition.band_windows if selected is transition.dsp else None,
                         vocal_handoff=transition.vocal_handoff if selected is transition.dsp else None)
    return AudioRenderPlan(clips=(outgoing, incoming), transitions=(styled,)), lead


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("songs", nargs="+", type=Path)
    parser.add_argument("--ffmpeg", type=Path, default=None)
    parser.add_argument("--ab", action="store_true", help="render every transition once per DSP style")
    parser.add_argument("--compare", action="store_true", help="compare AutoMix, rules without acoustic search, and basic crossfade")
    parser.add_argument("--blind", action="store_true", help="package blind A/B clips for EQ transitions versus same-cue fades")
    args = parser.parse_args()
    songs = args.songs
    if len(songs) == 1 and songs[0].suffix.lower() in PLAYLIST_FILE_EXTENSIONS:
        try:
            entries = read_m3u(songs[0])
        except M3uPlaylistError as error:
            parser.error(str(error))
        if entries.skipped:
            print(f"Skipped {len(entries.skipped)} missing or unsupported playlist entries.", file=sys.stderr)
        songs = entries.audio_paths
        if not songs:
            parser.error("The playlist contains no playable local tracks.")
    if args.blind and len(songs) < 2:
        parser.error("Blind comparison needs at least two tracks.")
    ffmpeg = FFmpegRenderer.find_executable(args.ffmpeg)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    tracks = [
        PlaylistTrack(str(path.resolve()), path.stem, duration_seconds=probe_duration(ffmpeg, path))
        for path in songs
    ]
    titles = {track.id: track.title for track in tracks}
    provider = create_analysis_provider("auto", ffmpeg)
    print(f"rhythm provider: {provider.provider_id}", flush=True)
    analyses = AnalysisService(provider).analyze_tracks(tracks).analyses
    structures = {}
    from app.automix.structure.sonara import SonaraStructureProvider, sonara_available

    if sonara_available():
        from app.automix.structure.service import StructureAnalysisService

        structures = StructureAnalysisService(SonaraStructureProvider(ffmpeg)).analyze_tracks(tracks).analyses
    print(f"structure: {'sonara' if structures else 'unavailable'}", flush=True)

    settings = settings_for_ffmpeg(AUTOMIX_SETTINGS, ffmpeg)
    plan = compile_automix(tracks, analyses, settings, structures=structures)
    pipeline = AutoMixAudioPipeline(ffmpeg)
    paths = {track.id: track.file_path for track in tracks}
    mix = pipeline.render(plan.audio, paths, out_dir, container="flac")
    samples = decode_mono(ffmpeg, mix.path)
    effective = replace(plan, audio=mix.audio_plan) if mix.audio_plan is not None else plan
    rows = transition_rows(effective, titles)
    if args.blind:
        count = write_blind_comparison(ffmpeg, pipeline, effective, tracks, out_dir)
        if count:
            print(f"Blind comparison: {count} pairs in {out_dir / 'blind.zip'}")
        else:
            print("No rendered EQ transitions to compare.")
    if args.compare:
        before = compile_automix(tracks, {key: replace(value, rms_curve=(), bass_curve=(), brightness_curve=(),
                                 percussive_curve=()) for key, value in analyses.items()}, settings,
                                 structures=structures, log_diagnostics=False)
        comparison_dir = out_dir / "compare"
        variants = {"after": (plan, mix, samples)}
        for name, compiled in (("before", before), ("crossfade", compile_crossfade(tracks, settings.fallback_crossfade_seconds))):
            rendered = pipeline.render(compiled.audio, paths, comparison_dir / name, container="flac")
            variants[name] = (compiled, rendered, decode_mono(ffmpeg, rendered.path))
        comparisons = compare_mixes(ffmpeg, comparison_dir, variants, titles)
        (out_dir / "comparison.json").write_text(rows_to_json(comparisons), encoding="utf-8")
    transitions = {t.timeline_start: t for t in plan.audio.transitions}
    for row in rows:
        transition = transitions.get(row["timeline_start"]) if row["duration"] else None
        row.update(window_metrics(samples, METRIC_RATE, row["timeline_start"], row["duration"]))
        if args.ab and transition is not None:
            for style in AB_STYLES:
                excerpt, lead = excerpt_plan(plan, transition, style)
                directory = out_dir / "ab" / f"{row['index']:02d}_{style}"
                rendered = pipeline.render(excerpt, paths, directory, container="flac")
                metrics = window_metrics(decode_mono(ffmpeg, rendered.path), METRIC_RATE, lead, transition.duration)
                row.update({f"ab_{style}_{name}": value for name, value in metrics.items()})

    (out_dir / "report.csv").write_text(rows_to_csv(rows), encoding="utf-8")
    (out_dir / "report.json").write_text(rows_to_json(rows), encoding="utf-8")
    write_rating_sheet(out_dir / "ratings.csv", rows)
    for row in rows:
        print(f"{row['index']:>2} {row['from']} -> {row['to']}: {row['type']}/{row['dsp']} "
              f"{row['duration']:.1f}s dip={row.get('level_dip_db', float('nan')):.1f}dB "
              f"peak={row.get('level_peak_db', float('nan')):.1f}dB "
              f"low={row.get('low_excess_db', float('nan')):.1f}dB")
    print(f"mix: {mix.path}\nreport: {out_dir / 'report.csv'}\nrating sheet: {out_dir / 'ratings.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
