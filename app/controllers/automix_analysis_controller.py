"""Background AutoMix analysis for the currently loaded playlist.

Runs off the GUI thread (roadmap Phase 1 section 7 / Phase 6 section 4:
"Do not freeze the UI"). Results here are session-only, in-memory display
data for MainWindow's playlist badges and track-details panel -- the
durable, cross-session cache is AnalysisCache (app/automix/cache.py);
this controller only orchestrates one background pass over the current
playlist and reports what came back.

``app.automix.analysis.basic`` (and, through it, ``librosa`` and its
dependency chain) is imported lazily, inside the worker thread's own
``run()``, not at module scope. AutoMix is optional -- the roadmap's
"keep the basic engine usable" rule extends to "installing the app at
all": MainWindow imports this controller unconditionally at startup, so
a top-level ``import librosa`` here would make a missing/broken librosa
installation prevent the entire application from launching, not just
AutoMix.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from app.automix.models import TrackAnalysis
from app.models.playlist import PlaylistTrack
from app.utils.qt_worker_lifecycle import stop_qthread_now

LOGGER = logging.getLogger(__name__)

BACKGROUND_ANALYSIS_WORKERS = 2
"""Files analyzed at once while the user edits: half of Preview/Export's
foreground pool, so warming the cache never takes over the machine."""

RHYTHM_STAGE = "rhythm"
"""Beats, downbeats and vocals (one provider pass per track)."""
STRUCTURE_STAGE = "structure"
"""Sonara song structure, after the rhythm pass (only when enabled and installed)."""


class _AutoMixAnalysisWorker(QThread):
    analyzed = Signal(dict)
    """Emits track_id -> TrackAnalysis for every track that analyzed successfully."""
    structures_analyzed = Signal(dict)
    """Emits track_id -> TrackStructureAnalysis, only when enable_structure_analysis
    was requested and the optional Sonara dependency is actually available."""
    progress = Signal(str, int, int)
    """(stage, completed tracks, total tracks); stage is RHYTHM_STAGE or STRUCTURE_STAGE.
    Both stages report 0 up front, so combined progress never moves backwards."""
    track_step = Signal(str, str, float)
    """(track_id, step, fraction of that track's rhythm pass): a provider step
    (provider.ANALYSIS_STEPS), STEP_CACHED, or STRUCTURE_STAGE when Sonara starts."""
    failed = Signal(dict)
    """track_id -> error message for the tracks the analyzer could not read."""

    def __init__(
        self, tracks: list[PlaylistTrack], ffmpeg_executable: Path | None, parent: QObject | None = None,
        *, provider_id: str = "basic", enable_structure_analysis: bool = False,
    ) -> None:
        super().__init__(parent)
        self._tracks = tracks
        self._ffmpeg_executable = ffmpeg_executable
        self._provider_id = provider_id
        self._enable_structure_analysis = enable_structure_analysis
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            from app.automix.analysis.registry import create_analysis_provider
            from app.automix.settings import AutoMixAnalysisSettings
            from app.automix.workflow import AutoMixWorkflow
            provider = create_analysis_provider(self._provider_id, self._ffmpeg_executable)
        except ImportError as error:
            LOGGER.warning("AutoMix analysis is unavailable: %s", error)
            return
        except ValueError as error:
            # An unrecognized provider_id is a wiring bug (see registry.py),
            # not a missing-dependency situation -- log it loudly but still
            # never crash the background worker over it.
            LOGGER.error("AutoMix analysis misconfigured: %s", error)
            return
        workflow = AutoMixWorkflow(
            provider, analysis_settings=AutoMixAnalysisSettings(max_workers=BACKGROUND_ANALYSIS_WORKERS),
        )
        total = len(self._tracks)
        self.progress.emit(RHYTHM_STAGE, 0, total)
        if self._structure_available():
            self.progress.emit(STRUCTURE_STAGE, 0, total)
        result = workflow.analyze(
            self._tracks, cancel_event=self._cancel_event,
            progress=lambda completed, count, _message: self.progress.emit(RHYTHM_STAGE, completed, count),
            step_progress=self.track_step.emit,
        )
        if self._cancel_event.is_set():
            return
        self.analyzed.emit(result.analyses)
        if result.failures:
            self.failed.emit(result.failures)
        self._run_structure_analysis()
        self._warm_rhythm_model(provider)

    def _warm_rhythm_model(self, provider) -> None:
        """Load the shared beat model now, while the user edits.

        Loading holds the GIL for ~0.65 s; done here it never lands in the
        middle of Preview playback (the session is shared app-wide, see
        beat_this_onnx.shared_beat_model). A no-op for other providers.
        """
        load = getattr(provider, "_load_model", None)
        if load is None or self._cancel_event.is_set():
            return
        try:
            load()
        except Exception as error:  # noqa: BLE001 - only a warm-up; analysis loads it again if needed
            LOGGER.info("AutoMix beat model warm-up skipped: %s", error)

    def _structure_available(self) -> bool:
        if not self._enable_structure_analysis:
            return False
        from app.automix.structure.sonara import sonara_available

        return sonara_available()

    def _run_structure_analysis(self) -> None:
        """Structure analysis runs sequentially, after rhythm analysis, still
        entirely off the GUI thread -- this QThread does not return control
        to Qt until both are done, so there is no separate UI-freeze risk to
        manage versus running them concurrently, only a longer total
        background run. Optional and independent: a missing Sonara install
        (checked cheaply up front, never imported to find out) or a failure
        here never invalidates the rhythm analysis already emitted above.
        """
        if not self._enable_structure_analysis or self._cancel_event.is_set():
            return
        try:
            from app.automix.structure.sonara import SonaraStructureProvider, sonara_available
            from app.automix.structure.service import StructureAnalysisService
        except ImportError as error:
            LOGGER.info("AutoMix structure analysis is unavailable: %s", error)
            return
        if not sonara_available():
            LOGGER.info("AutoMix structure analysis skipped: Sonara is not installed.")
            return
        service = StructureAnalysisService(
            SonaraStructureProvider(self._ffmpeg_executable), max_workers=BACKGROUND_ANALYSIS_WORKERS,
        )
        for track in self._tracks:
            self.track_step.emit(track.id, STRUCTURE_STAGE, 1.0)
        result = service.analyze_tracks(
            self._tracks, cancel_event=self._cancel_event,
            progress=lambda completed, count, _message: self.progress.emit(STRUCTURE_STAGE, completed, count),
        )
        if not self._cancel_event.is_set():
            self.structures_analyzed.emit(result.analyses)


class AutoMixAnalysisController(QObject):
    """Owns at most one AutoMix analysis pass at a time."""

    analyses_updated = Signal(dict)
    """track_id -> TrackAnalysis for the tracks that just finished analyzing."""
    structures_updated = Signal(dict)
    """track_id -> TrackStructureAnalysis for the tracks whose structure just
    finished analyzing. Only emitted when enable_structure_analysis was
    requested and the optional Sonara dependency is actually available."""
    progress_changed = Signal(dict)
    """stage -> (completed, total) for the running pass (see RHYTHM_STAGE/STRUCTURE_STAGE)."""
    running_changed = Signal(bool)
    """True when a pass starts, False when it ends (finished or cancelled)."""
    track_step_changed = Signal(str, str, float)
    """(track_id, step, fraction) as each analysis step starts (see _AutoMixAnalysisWorker.track_step)."""
    analyses_failed = Signal(dict)
    """track_id -> error message for tracks that could not be analyzed."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker: _AutoMixAnalysisWorker | None = None
        self._pending = None
        self._shutting_down = False
        self._stages: dict[str, tuple[int, int]] = {}

    @property
    def is_running(self) -> bool:
        return self._worker is not None

    @property
    def stages(self) -> dict[str, tuple[int, int]]:
        """The running pass's stage -> (completed, total), empty before its first report."""
        return dict(self._stages)

    def start(
        self, tracks: list[PlaylistTrack], ffmpeg_executable: Path | None, *, provider_id: str = "basic",
        enable_structure_analysis: bool = False,
    ) -> None:
        """Analyze ``tracks`` in the background, replacing any run already underway.

        ``provider_id`` selects the AnalysisProvider (see
        app/automix/analysis/registry.py) -- defaults to "basic" so existing
        callers are unaffected; pass "beat_this" to use the optional Beat
        This! engine when available, which falls back to "basic" per-track
        on its own if the dependency/model is missing or inference fails.

        ``enable_structure_analysis`` additionally runs the optional Sonara
        structure analyzer (intro/outro/sections/energy curve) after rhythm
        analysis completes, emitting ``structures_updated`` -- defaults to
        False so existing callers are unaffected. A missing Sonara install
        or a per-track structure-analysis failure never affects rhythm
        analysis, which has already been emitted by the time structure
        analysis even starts.
        """
        if self._shutting_down:
            return
        self.cancel()
        if not tracks:
            return
        if self._worker is not None:
            self._pending = (tracks, ffmpeg_executable, provider_id, enable_structure_analysis)
            return
        worker = _AutoMixAnalysisWorker(
            tracks, ffmpeg_executable, self,
            provider_id=provider_id, enable_structure_analysis=enable_structure_analysis,
        )
        worker.analyzed.connect(
            lambda result: self.analyses_updated.emit(result)
            if not worker._cancel_event.is_set() else None
        )
        worker.structures_analyzed.connect(
            lambda result: self.structures_updated.emit(result)
            if not worker._cancel_event.is_set() else None
        )
        worker.failed.connect(
            lambda failures: self.analyses_failed.emit(failures)
            if not worker._cancel_event.is_set() else None
        )
        worker.track_step.connect(
            lambda track_id, step, fraction: self.track_step_changed.emit(track_id, step, fraction)
            if worker is self._worker and not worker._cancel_event.is_set() else None
        )
        worker.progress.connect(lambda stage, completed, total: self._report(worker, stage, completed, total))
        # Clear our reference *before* scheduling deletion: a worker that
        # finishes on its own (not via cancel()) would otherwise leave
        # self._worker pointing at a QThread whose C++ object deleteLater()
        # has already destroyed by the time the next start()/cancel() call
        # touches it ("libshiboken: ... already deleted").
        worker.finished.connect(lambda: self._forget(worker))
        self._worker = worker
        self._stages = {}
        worker.start()
        self.running_changed.emit(True)

    def _report(self, worker: "_AutoMixAnalysisWorker", stage: str, completed: int, total: int) -> None:
        if worker is not self._worker or worker._cancel_event.is_set():
            return  # a superseded or cancelled pass
        self._stages[stage] = (completed, total)
        self.progress_changed.emit(dict(self._stages))

    def _forget(self, worker: "_AutoMixAnalysisWorker") -> None:
        if self._worker is worker:
            self._worker = None
            self._stages = {}
            self.running_changed.emit(False)
        if self._shutting_down:
            # shutdown() takes exclusive ownership of this worker's
            # wait()/deleteLater() sequence once shutdown has started (see
            # shutdown() below). Scheduling deletion here too raced
            # shutdown()'s own wait()/deleteLater() call on Windows: the
            # deferred-delete event could destroy the C++ QThread object
            # while shutdown()'s nested event loop was still processing
            # events for it, producing a 0xC0000409 fail-fast crash the
            # very first time this was exercised under real threading.
            return
        worker.deleteLater()
        pending, self._pending = self._pending, None
        if pending is not None:
            tracks, ffmpeg_executable, provider_id, enable_structure_analysis = pending
            self.start(
                tracks, ffmpeg_executable, provider_id=provider_id,
                enable_structure_analysis=enable_structure_analysis,
            )

    def cancel(self) -> None:
        """Request cancellation; retain ownership until finished is delivered."""
        self._pending = None
        worker = self._worker
        if worker is None:
            return
        try:
            worker.isRunning()  # Detect an already-deleted Qt wrapper.
            worker.cancel()  # Suppress even results already queued for delivery.
        except RuntimeError:
            self._worker = None
        # The worker may still drain an uninterruptible step; the UI stops showing it now.
        self._stages = {}
        self.running_changed.emit(False)

    def shutdown(self) -> None:
        """Finish cancellation before the owner or its temporary files are deleted.

        stop_qthread_now() takes exclusive ownership of the worker's
        wait()/deleteLater() sequence, so ``_forget()`` can't delete it too.
        """
        self._shutting_down = True
        self.cancel()
        worker, self._worker = self._worker, None
        stop_qthread_now(worker)
