"""Progressive AutoMix preview: analysis per track, partial mixes, then the export mix.

Drop-in for PreviewAudioController in AutoMix mode (same ``audio_ready`` /
``audio_failed`` / ``progress`` signals and ``start``/``shutdown``), plus
``progressive_ready`` for partial mixes, ``level_ready`` for the level
per-track audio plays at before the mix lands, and ``report_playhead`` so it
can render what the listener needs next. Policy lives in
``app.automix.progressive``; this module only moves work between threads.

``audio_ready`` is still the unmodified export pipeline
(``FFmpegRenderer.prepare_playlist_audio``), run once analysis is complete,
so the final Preview plan is exactly the Export plan.

Every worker result and progress event carries the generation it was
started for; ``cancel()``/``shutdown()`` bump the generation, so nothing from
a cancelled run can reach a newer one.
"""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import threading
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from app.automix.analysis.provider import (
    STEP_BARS, STEP_BEAT_MODEL, STEP_DECODE, STEP_KEY_ENERGY, STEP_RHYTHM, STEP_VOCALS,
)
from app.automix.cache import MEMO_CAPACITY, BoundedMemo, canonical_media_path
from app.automix.progressive import (
    Action,
    ProgressiveAnalysis,
    RenderScheduler,
    partial_plan,
    preview_gain,
    render_prefix,
)
from app.automix.settings import AutoMixTransitionSettings
from app.controllers.preview_audio_controller import PreviewAudioController
from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import FFmpegRenderer
from app.renderer.progress_text import audio_progress_text, clock
from app.utils.qt_worker_lifecycle import stop_qthread_now
from app.utils.subprocess_utils import (
    background_work, hidden_process_kwargs, lower_thread_if_background,
)

LOGGER = logging.getLogger(__name__)

_LUFS = re.compile(r"I:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*LUFS")
_PEAK = re.compile(r"Peak:\s*(-?(?:\d+(?:\.\d+)?|inf))\s*dBFS")

PREVIEW_ANALYSIS_WORKERS = 2
"""Tracks analysed side by side while Preview plays: each worker's Python steps
contend with playback for the GIL. Measured on 11 tracks: 2 workers finish the
mix as fast as 4 (the final render dominates) with fewer late frames."""
LEVEL_WORKERS = 4
"""Loudness scans at once while Preview opens (each is one FFmpeg decode)."""
PUBLISH_INTERVAL_SECONDS = 0.25
"""At most this often a burst of analysis steps redraws the status line."""
ANALYSIS_WEIGHT = 0.45
"""Share of the overall bar for analysis; the export-pipeline final mix gets the rest."""
_FINAL_PIPELINE_END = 0.64
"""prepare_playlist_audio's own fraction when its audio is written (0.64 -> video)."""

_STEP_NAMES = {
    STEP_DECODE: ("디코딩", "decoding"),
    STEP_RHYTHM: ("비트", "beats"),
    STEP_BARS: ("마디", "bars"),
    STEP_KEY_ENERGY: ("키·에너지·장르·분위기", "key/energy/genre/mood"),
    STEP_BEAT_MODEL: ("비트 모델", "beat model"),
    STEP_VOCALS: ("보컬 구간", "vocals"),
}


def measure_loudness(executable: Path, path: str, cancel_event: threading.Event) -> tuple[float, float]:
    """(integrated LUFS, sample peak dBFS) of one file via FFmpeg ``ebur128``; -inf when unknown."""
    if cancel_event.is_set():
        return -math.inf, -math.inf
    try:
        with subprocess.Popen(
            [str(executable), "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0",
             "-af", "ebur128=peak=sample", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            **hidden_process_kwargs(),
        ) as process:
            deadline = monotonic() + 300
            try:
                while True:
                    if cancel_event.is_set():
                        return -math.inf, -math.inf
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(process.args, 300)
                    try:
                        _stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
            if process.returncode != 0:
                return -math.inf, -math.inf
    except (OSError, subprocess.SubprocessError) as error:
        LOGGER.warning("Preview loudness measurement failed for %s: %s", path, error)
        return -math.inf, -math.inf
    lufs, peaks = _LUFS.findall(stderr), _PEAK.findall(stderr)
    return (float(lufs[-1]) if lufs else -math.inf), (float(peaks[-1]) if peaks else -math.inf)


# (canonical path, size, mtime_ns) -> (LUFS, peak): each file is measured once per
# app session, so reopening Preview knows its level at once.
_LEVELS = BoundedMemo(MEMO_CAPACITY)


def track_level(executable: Path, path: str, cancel_event: threading.Event) -> tuple[float, float]:
    """``measure_loudness``, remembered per file version; failed or cancelled results are not."""
    try:
        stat = os.stat(path)
        key = (canonical_media_path(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return -math.inf, -math.inf
    known = _LEVELS.get(key)
    if known is not None:
        return known
    level = measure_loudness(executable, path, cancel_event)
    if math.isfinite(level[0]) and not cancel_event.is_set():
        _LEVELS.put(key, level)
    return level


def playlist_gain(executable: Path, tracks: list[PlaylistTrack], cancel_event: threading.Event) -> float | None:
    """``preview_gain`` of the whole (enabled) playlist -- what the final mix is normalized by; None if cancelled.

    Estimating from the analyzed prefix only put partial mixes at another
    level than the final mix whenever the first tracks were louder or quieter
    than the rest.
    """
    from concurrent.futures import ThreadPoolExecutor

    enabled = [track for track in tracks if track.enabled]
    # Deduplicate before the pool starts. Its cache alone cannot prevent two
    # simultaneous cold scans of a file used by several playlist entries.
    paths = list(dict.fromkeys(track.file_path for track in enabled))
    with ThreadPoolExecutor(max_workers=LEVEL_WORKERS, thread_name_prefix="preview-levels",
                            initializer=lower_thread_if_background) as pool:
        measured = dict(zip(paths, pool.map(lambda path: track_level(executable, path, cancel_event), paths)))
    if cancel_event.is_set():
        return None
    # ponytail: the track's own volume shifts its level; its EQ is not measured.
    levels = {}
    for track in enabled:
        loudness, peak = measured[track.file_path]
        levels[track.id] = loudness + track.volume_db, peak + track.volume_db
    return preview_gain(levels, {track.id: track.duration_seconds for track in enabled})


class _AnalysisWorker(QThread):
    """Rhythm, structure and loudness side by side, reporting each track as it lands."""

    rhythm_done = Signal(int, str, object)
    rhythm_step = Signal(int, str, str)
    """generation, track id, the provider step it just started (provider.ANALYSIS_STEPS or "cached")."""
    structure_done = Signal(int, str, object)
    level_done = Signal(int, float)
    """generation, the playlist's preview gain (linear)."""

    def __init__(self, executable: Path, tracks: list[PlaylistTrack], generation: int,
                 cancel_event: threading.Event, structure_enabled: bool, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executable = executable
        self._tracks = tracks
        self._generation = generation
        self._cancel_event = cancel_event
        self._structure_enabled = structure_enabled

    def run(self) -> None:
        with background_work():
            self._analyze()

    def _analyze(self) -> None:
        # Loudness is one cheap decode per file (~0.1 s); running it first
        # thing puts per-track playback at the mix's level within moments.
        threads = [threading.Thread(target=self._run_levels, name="progressive-levels")]
        if self._structure_enabled:
            threads.append(threading.Thread(target=self._run_structure, name="progressive-structure"))
        for thread in threads:
            thread.start()
        try:
            # Same provider policy and service as prepare_playlist_audio, so the
            # cache entries written here are the ones the final render reads.
            from app.automix.analysis.registry import create_analysis_provider
            from app.automix.analysis.service import AnalysisService
            from app.automix.settings import AutoMixAnalysisSettings

            AnalysisService(
                create_analysis_provider("auto", self._executable),
                settings=AutoMixAnalysisSettings(max_workers=PREVIEW_ANALYSIS_WORKERS),
            ).analyze_tracks(
                self._tracks, cancel_event=self._cancel_event,
                on_result=lambda track_id, analysis: self.rhythm_done.emit(self._generation, track_id, analysis),
                step_progress=lambda track_id, step, _fraction: self.rhythm_step.emit(
                    self._generation, track_id, step),
            )
        except Exception as error:  # noqa: BLE001 - degrade to the final render, never crash Preview
            LOGGER.warning("Progressive AutoMix rhythm analysis failed: %s", error)
        finally:
            # Deliberately unbounded: the children emit on this QThread, so one
            # abandoned past a timeout would emit on a deleted object after
            # shutdown. Both honor cancel_event between tracks; the longest wait
            # is one in-flight ffmpeg loudness scan (cancel-polled) or Sonara
            # analyze_file() call, the same bound as the rhythm step above.
            for thread in threads:
                thread.join()

    def _run_levels(self) -> None:
        lower_thread_if_background()
        try:
            gain = playlist_gain(self._executable, self._tracks, self._cancel_event)
        except Exception as error:  # noqa: BLE001 - the partial render measures again if needed
            LOGGER.warning("Progressive AutoMix loudness measurement failed: %s", error)
            return
        if gain is not None:
            self.level_done.emit(self._generation, gain)

    def _run_structure(self) -> None:
        lower_thread_if_background()
        try:
            from app.automix.structure.service import StructureAnalysisService
            from app.automix.structure.sonara import SonaraStructureProvider

            StructureAnalysisService(SonaraStructureProvider(self._executable)).analyze_tracks(
                self._tracks, cancel_event=self._cancel_event,
                on_result=lambda track_id, structure: self.structure_done.emit(self._generation, track_id, structure),
            )
        except Exception as error:  # noqa: BLE001
            LOGGER.warning("Progressive AutoMix structure analysis failed: %s", error)
            for track in self._tracks:
                self.structure_done.emit(self._generation, track.id, None)


class _PartialRenderWorker(QThread):
    """Renders one partial mix (first ``track_count`` clips) to a playable FLAC."""

    ready = Signal(int, str, object, float, float)
    """generation, path, plan, covered_until seconds, linear gain."""
    failed = Signal(int, str)
    progress = Signal(int, str)
    """generation, the mix pipeline's progress line (see progress_text)."""

    def __init__(self, executable: Path, plan, track_count: int, tracks: list[PlaylistTrack],
                 directory: Path, generation: int, cancel_event: threading.Event,
                 gain: float | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executable = executable
        self._plan = plan
        self._track_count = track_count
        self._tracks = tracks
        self._directory = directory
        self._generation = generation
        self._cancel_event = cancel_event
        self._gain = gain

    def run(self) -> None:
        with background_work():
            self._render()

    def _render(self) -> None:
        from app.automix.renderer import AutoMixAudioPipeline, AutoMixRenderError, render_workers

        try:
            gain = self._gain
            if gain is None:
                gain = playlist_gain(self._executable, self._tracks, self._cancel_event)
                if gain is None:
                    return  # cancelled
            render, covered_until = render_prefix(self._plan, self._track_count, gain)
            prepared = AutoMixAudioPipeline(self._executable).render(
                render, {track.id: track.file_path for track in self._tracks}, self._directory,
                cancel_event=self._cancel_event, container="flac",
                progress=lambda _stage, _fraction, message: self.progress.emit(self._generation, message),
                track_filters={track.id: track.audio_filter for track in self._tracks},
                workers=render_workers(),
            )
        except AutoMixRenderError as error:
            if not self._cancel_event.is_set():
                self.failed.emit(self._generation, str(error))
            return
        except Exception as error:  # noqa: BLE001
            self.failed.emit(self._generation, str(error))
            return
        if not self._cancel_event.is_set():
            self.ready.emit(self._generation, str(prepared.path), self._plan, covered_until, gain)


class ProgressiveAutoMixController(QObject):
    """One progressive AutoMix preview run at a time (see module docstring)."""

    audio_ready = Signal(str, object)
    """Final export-pipeline mix: (path, CompiledRenderPlan)."""
    audio_failed = Signal(str)
    progress = Signal(str, float, str)
    progressive_ready = Signal(str, object, float, float)
    """Partial mix: (path, provisional plan, covered-until seconds, linear gain)."""
    level_ready = Signal(float)
    """Linear gain per-track audio should play at so it matches the mix's loudness."""
    initial_load_done = Signal()
    """Once per run: every cached analysis has landed (what is left is real analysis, or nothing)."""

    def __init__(self, renderer: FFmpegRenderer, parent: QObject | None = None, *, korean: bool = True) -> None:
        super().__init__(parent)
        self._renderer = renderer
        self._korean = korean
        self._generation = 0
        self._cancel_event = threading.Event()
        self._analysis_worker: _AnalysisWorker | None = None
        self._render_worker: _PartialRenderWorker | None = None
        self._final: PreviewAudioController | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._evaluate)
        # Status line: redrawn at most every PUBLISH_INTERVAL_SECONDS, and each
        # second while work runs so the elapsed time stays live.
        self._publish_timer = QTimer(self)
        self._publish_timer.setSingleShot(True)
        self._publish_timer.timeout.connect(lambda: self._publish(force=True))
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(lambda: self._publish(force=True))
        self._last_publish = -math.inf
        self._gain: float | None = None
        self._progressive_enabled = True
        self._shutting_down = False
        self.render_count = 0
        """Partial renders started (diagnostics/tests)."""
        self.latest_partial: tuple[str, object, float, float] | None = None
        """Last ``progressive_ready`` payload, for a Preview that adopts this run mid-way."""
        self.playback_gain: float | None = None
        """Last ``level_ready`` value, for a Preview that adopts this run mid-way."""
        self.last_progress: tuple[str, float, str] | None = None
        """Last ``progress`` payload, so an adopting Preview continues from it."""
        self.progress_steps: list[tuple[str, float | None, str]] = []
        """(label, progress or None when not measurable, detail) per stage, for the status-bar popup."""
        self.fallback_message: str | None = None
        """Set when the final mix is not AutoMix after all (it fell back to back-to-back audio)."""
        self.initial_loading = False
        """True from start() until initial_load_done."""
        self.progress.connect(lambda *args: setattr(self, "last_progress", args))

    # -- public API (PreviewAudioController-compatible) ----------------------

    def start(self, tracks: list[PlaylistTrack], output_directory: Path,
              transition_mode: str, crossfade_seconds: float,
              automix_settings: AutoMixTransitionSettings | None = None) -> None:
        """A new generation: any running one (e.g. another preset's) is cancelled first."""
        if self._shutting_down or not tracks:
            return
        self.cancel()
        from app.automix.structure.sonara import sonara_available

        self._generation += 1
        self._cancel_event = threading.Event()
        self._tracks = list(tracks)
        self._titles = {track.id: (track.title or track.filename).strip() for track in self._tracks}
        self._directory = Path(output_directory)
        self._crossfade_seconds = crossfade_seconds
        # The same resolved preset plans the partial mixes and the final
        # (export-pipeline) mix, so the final Preview plan is the Export plan.
        self._settings = automix_settings or AutoMixTransitionSettings(enabled=True)
        self._state = ProgressiveAnalysis(self._tracks, structure_enabled=sonara_available())
        self._scheduler = RenderScheduler()
        self._frontier = -1
        self._gain = None  # frozen by the playlist's loudness (or the first partial mix)
        self._progressive_enabled = True
        self.latest_partial = None
        self.playback_gain = None
        self.fallback_message = None
        self._started_at = monotonic()
        self._running: dict[str, str] = {}  # track id -> current step, in start order
        self._cached: set[str] = set()
        self._partial_line: str | None = None
        self._final_line: str | None = None
        self._final_fraction = 0.0
        self._overall = 0.0
        self._done = False
        self.initial_loading = True
        self._ticker.start()
        self._publish(force=True)
        worker = _AnalysisWorker(
            self._renderer.executable, self._tracks, self._generation, self._cancel_event,
            self._state.structure_enabled, self,
        )
        worker.rhythm_done.connect(self._on_rhythm)
        worker.rhythm_step.connect(self._on_rhythm_step)
        worker.structure_done.connect(self._on_structure)
        worker.level_done.connect(self._on_level)
        worker.finished.connect(lambda worker=worker: self._on_analysis_finished(worker))
        self._analysis_worker = worker
        worker.start()

    def analysis_snapshot(self) -> tuple[dict, dict]:
        """(rhythm analyses, structures) known so far, for Transition details' editor."""
        state = getattr(self, "_state", None)
        if state is None:
            return {}, {}
        return dict(state.rhythm), dict(state.structures)

    def report_playhead(self, seconds: float, playing: bool) -> None:
        """Preview's global playhead; the first report marks Preview as attached."""
        if self._shutting_down or not hasattr(self, "_scheduler"):
            return
        self._scheduler.playhead_changed(seconds, playing)
        self._evaluate()

    def cancel(self) -> None:
        self._generation += 1
        self._cancel_event.set()
        self._timer.stop()
        self._publish_timer.stop()
        self._ticker.stop()
        if self._final is not None:
            self._final.cancel()

    def shutdown(self) -> None:
        self._shutting_down = True
        self.cancel()
        workers = (self._analysis_worker, self._render_worker)
        self._analysis_worker = self._render_worker = None
        for worker in workers:
            stop_qthread_now(worker)
        if self._final is not None:
            self._final.shutdown()

    # -- analysis ------------------------------------------------------------

    def _on_rhythm_step(self, generation: int, track_id: str, step: str) -> None:
        if generation != self._generation:
            return
        if step == "cached":
            self._cached.add(track_id)
        else:
            self._running[track_id] = step
            self._settle_initial_load()  # a real analysis: minutes, not a load
            self._publish()

    def _on_rhythm(self, generation: int, track_id: str, analysis) -> None:
        if generation == self._generation:
            self._running.pop(track_id, None)
            self._state.record_rhythm(track_id, analysis)
            self._on_analysis_progress()
            if self._state.rhythm_count() >= len(self._tracks):
                self._settle_initial_load()

    def _settle_initial_load(self) -> None:
        if self.initial_loading:
            self.initial_loading = False
            self.initial_load_done.emit()

    def _on_structure(self, generation: int, track_id: str, structure) -> None:
        if generation == self._generation:
            self._state.record_structure(track_id, structure)
            self._on_analysis_progress()

    def _on_level(self, generation: int, gain: float) -> None:
        if generation != self._generation:
            return
        if self._gain is None:
            self._gain = gain
        self.playback_gain = self._gain
        self.level_ready.emit(self._gain)

    def _on_analysis_progress(self) -> None:
        frontier = self._state.frontier()
        if frontier != self._frontier:
            # Planning is immediate on every frontier move; rendering is not.
            self._frontier = frontier
            self._plan = partial_plan(self._tracks, self._state, self._settings)
            self._scheduler.plan_updated(self._plan, frontier, monotonic())
        if self._state.complete():
            self._scheduler.analysis_complete = True
        self._publish()
        self._evaluate()

    def _on_analysis_finished(self, worker: _AnalysisWorker) -> None:
        if not self._release(worker, "_analysis_worker") or worker._generation != self._generation:
            return
        # Missing results (failed/cancelled tracks) must not stall the final mix.
        self._running.clear()
        self._settle_initial_load()
        self._scheduler.analysis_complete = True
        self._evaluate()

    # -- rendering -----------------------------------------------------------

    def _evaluate(self) -> None:
        if self._shutting_down or not hasattr(self, "_scheduler"):
            return
        now = monotonic()
        action = self._scheduler.decide(now)
        if action is Action.RENDER:
            # Partials disabled after a failure: the next analysis event or the
            # final mix re-evaluates. Re-arming the timer here spun every 10 ms.
            if self._progressive_enabled:
                self._start_partial_render()
        elif action is Action.FINAL:
            self._start_final()
        else:
            wait = self._scheduler.wake_after(now)
            if wait is not None:
                self._timer.start(max(10, round(wait * 1000)))

    def _start_partial_render(self) -> None:
        self._scheduler.render_started()
        self.render_count += 1
        self._partial_line = self._message("믹싱 준비 중", "preparing the mix")
        self._publish(force=True)
        worker = _PartialRenderWorker(
            self._renderer.executable, self._plan, self._frontier, self._tracks,
            self._directory / f"partial-{self.render_count}", self._generation, self._cancel_event,
            self._gain, self,
        )
        worker.ready.connect(self._on_partial_ready)
        worker.failed.connect(self._on_partial_failed)
        worker.progress.connect(self._on_partial_progress)
        worker.finished.connect(lambda worker=worker: self._release(worker, "_render_worker"))
        self._render_worker = worker
        worker.start()

    def _release(self, worker: QThread, attribute: str) -> bool:
        """Forget a finished worker; False while shutdown() owns its deletion."""
        if self._shutting_down:
            return False
        if getattr(self, attribute) is worker:
            setattr(self, attribute, None)
        worker.deleteLater()
        return True

    def _on_partial_progress(self, generation: int, message: str) -> None:
        if generation == self._generation and self._partial_line is not None:
            self._partial_line = audio_progress_text(message, self._korean) or self._partial_line
            self._publish()

    def _on_partial_ready(self, generation: int, path: str, plan, covered_until: float, gain: float) -> None:
        if generation != self._generation:
            return
        self._scheduler.render_finished()
        self._partial_line = None
        self._gain = gain  # frozen: every partial mix shares one level
        self.latest_partial = (path, plan, covered_until, gain)
        self.progressive_ready.emit(path, plan, covered_until, gain)
        self._publish(force=True)
        self._evaluate()

    def _on_partial_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        LOGGER.warning("Progressive AutoMix partial render failed; waiting for the final mix: %s", message)
        self._scheduler.render_finished()
        self._partial_line = None
        self._progressive_enabled = False
        self._publish(force=True)
        self._evaluate()

    def _start_final(self) -> None:
        self._scheduler.render_started(final=True)
        self._final_line = self._message("준비 중", "starting")
        self._publish(force=True)
        generation = self._generation
        final = PreviewAudioController(self._renderer, self)
        final.audio_ready.connect(lambda path, plan: self._on_final_ready(generation, path, plan))
        final.audio_failed.connect(lambda message: self._on_final_failed(generation, message))
        final.progress.connect(lambda _stage, fraction, message: self._on_final_progress(generation, fraction, message))
        self._final = final
        final.start(self._tracks, self._directory / "final", "automix", self._crossfade_seconds,
                    automix_settings=self._settings)

    def _on_final_progress(self, generation: int, fraction: float, message: str) -> None:
        if generation != self._generation:
            return  # a cancelled run's pipeline still draining
        self._final_fraction = max(self._final_fraction, min(1.0, fraction / _FINAL_PIPELINE_END))
        self._final_line = audio_progress_text(message, self._korean) or self._final_line
        self._publish()

    def _on_final_ready(self, generation: int, path: str, plan) -> None:
        if generation != self._generation:
            return
        self._scheduler.render_finished()
        self._done = True
        self._ticker.stop()
        self._publish_timer.stop()  # the forced publish below is the last word
        audio = getattr(plan, "audio", None)
        if audio is not None and len(audio.clips) > 1 and not audio.transitions:
            # The export pipeline fell back to back-to-back audio (a failed
            # AutoMix render, or no pair could be blended): never call that AutoMix.
            self.fallback_message = self._message(
                "AutoMix를 적용하지 못해 곡을 순서대로 이어서 재생합니다.",
                "AutoMix could not be applied; the tracks play back to back.",
            )
            LOGGER.warning("Progressive AutoMix final mix has no transitions; playing back to back.")
        self._publish(force=True)
        self.audio_ready.emit(path, plan)

    def _on_final_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self._ticker.stop()
        self._publish_timer.stop()
        self.audio_failed.emit(message)

    # -- progress (always from real state) -----------------------------------

    def _publish(self, *, force: bool = False) -> None:
        """Emit the status line; bursts are coalesced to one per PUBLISH_INTERVAL_SECONDS."""
        if self._shutting_down or not hasattr(self, "_state"):
            return
        now = monotonic()
        if not force and now - self._last_publish < PUBLISH_INTERVAL_SECONDS:
            if not self._publish_timer.isActive():
                self._publish_timer.start(round(PUBLISH_INTERVAL_SECONDS * 1000))
            return
        self._last_publish = now
        total = max(1, len(self._tracks))
        passes = 2 if self._state.structure_enabled else 1
        analysis = (self._state.rhythm_count()
                    + (self._state.structure_count() if self._state.structure_enabled else 0)) / (total * passes)
        overall = ANALYSIS_WEIGHT * min(1.0, analysis) + (1.0 - ANALYSIS_WEIGHT) * self._final_fraction
        # Monotonic, and never 100 % before the final mix is actually playable.
        self._overall = 1.0 if self._done else max(self._overall, min(0.99, overall))
        self.progress_steps = self._steps(total)
        self.progress.emit("AutoMix", self._overall, self._status_line(total))

    def _status_line(self, total: int) -> str:
        rendered = self._scheduler.rendered_frontier
        parts = []
        if self._done:
            parts.append(self.fallback_message or self._message("AutoMix 준비 완료", "AutoMix ready"))
        elif self._final_line is not None:
            parts.append(self._message("최종 믹스 · ", "Final mix · ") + self._final_line)
        elif self._partial_line is not None:
            parts.append(self._message("미리보기 ", "Preview ") + self._partial_line)
        else:
            parts.append(self._analysis_line(total))
        if rendered and not self._done:
            parts.append(self._message(f"AutoMix 재생 가능 · {rendered}번째 곡까지",
                                       f"AutoMix playable through track {rendered}"))
        failed = self._state.failed_count()
        if failed:
            parts.append(self._message(f"분석 실패 {failed}곡(기본 전환으로 연결)",
                                       f"{failed} track(s) failed analysis (plain transitions)"))
        if not self._done:
            parts.append(self._message("경과 ", "elapsed ") + clock(monotonic() - self._started_at))
        return " · ".join(parts)

    def _analysis_line(self, total: int) -> str:
        rhythm = self._state.rhythm_count()
        planned = len(self._plan.audio.transitions) if getattr(self, "_plan", None) is not None else 0
        pairs = max(0, total - 1)
        if rhythm < total:
            line = self._message(f"리듬·보컬 분석 중 · {rhythm}/{total}곡 완료",
                                 f"Analyzing beats & vocals · {rhythm}/{total} done")
            if self._running:
                line += " · " + self._running_text()
            if self._cached:
                line += self._message(f" · 캐시 사용 {len(self._cached)}곡", f" · {len(self._cached)} from cache")
        elif self._state.structure_enabled and self._state.structure_count() < total:
            line = self._message(f"곡 구조 분석 중 · {self._state.structure_count()}/{total}곡 완료",
                                 f"Analyzing song structure · {self._state.structure_count()}/{total} done")
        else:
            line = self._message("분석 완료", "Analysis done")
        return line + self._message(f" · 전환 계획 {planned}/{pairs}", f" · transitions planned {planned}/{pairs}")

    def _running_text(self) -> str:
        """Every track analyzing right now (several run at once), with its current step."""
        names = [f"{self._short(self._titles.get(track_id, track_id))}"
                 f"({_STEP_NAMES.get(step, (step, step))[0 if self._korean else 1]})"
                 for track_id, step in self._running.items()]
        shown = ", ".join(names[:2])
        extra = len(names) - 2
        if extra > 0:
            shown += self._message(f" 외 {extra}곡", f" +{extra} more")
        return self._message("분석 중: ", "now: ") + shown

    @staticmethod
    def _short(title: str) -> str:
        return title if len(title) <= 18 else title[:17] + "…"

    def _steps(self, total: int) -> list[tuple[str, float | None, str]]:
        rhythm, structure = self._state.rhythm_count(), self._state.structure_count()
        rendered = total if self._done else self._scheduler.rendered_frontier
        steps = [(self._message("리듬·보컬 분석", "Beats & vocals"), rhythm / total,
                  self._message(f"{rhythm}/{total}곡", f"{rhythm}/{total}")
                  + (self._message(f" · 캐시 {len(self._cached)}곡", f" · {len(self._cached)} cached")
                     if self._cached else ""))]
        if self._state.structure_enabled:
            steps.append((self._message("곡 구조 분석", "Song structure"), structure / total,
                          self._message(f"{structure}/{total}곡", f"{structure}/{total}")))
        steps.append((
            self._message("미리보기 믹스", "Preview mix"),
            None if self._partial_line is not None else rendered / total,
            self._partial_line or (self._message(f"{rendered}번째 곡까지 재생 가능", f"playable through track {rendered}")
                                   if rendered else ""),
        ))
        steps.append((self._message("최종 믹스·음량 보정", "Final mix & loudness"),
                      1.0 if self._done else self._final_fraction, self._final_line or ""))
        return steps

    def _message(self, korean: str, english: str) -> str:
        return korean if self._korean else english
