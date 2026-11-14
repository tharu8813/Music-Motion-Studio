"""Background rendering of one blended preview audio track.

Roadmap Phase 6 section 9: "The full existing Preview must use the same
CompiledRenderPlan that would be used by export... pre-render a full
preview audio master." This controller does exactly that -- it calls
FFmpegRenderer.prepare_playlist_audio() (the same method render() calls
for export) off the GUI thread, so ExportPreviewDialog never re-derives
AutoMix/crossfade timing itself.

If rendering fails, is unavailable, or has not finished yet, the caller
falls back to legacy per-track playback -- this must never prevent
Preview from working, only add to it once ready.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QThread, Signal

from app.models.audio_effects import AudioEffectSettings, has_audio_effects
from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderCancelledError, RenderError
from app.timeline.compiler import compile_playlist
from app.utils.qt_worker_lifecycle import stop_qthread_now
from app.utils.subprocess_utils import background_work

LOGGER = logging.getLogger(__name__)


def prepare_audio_for_ui(renderer, tracks, directory, mode, crossfade_seconds,
                         settings, cancel_event, progress=None, automix_settings=None, *, audio_effects=None):
    """Resolve audio and timing together while the caller's modal UI stays responsive."""
    worker = _PreviewAudioWorker(renderer, tracks, directory, mode, crossfade_seconds,
                                 settings=settings, cancel_event=cancel_event,
                                 automix_settings=automix_settings, audio_effects=audio_effects)
    results, errors = [], []
    worker.ready.connect(lambda path, plan: results.append((Path(path), plan)))
    worker.failed.connect(errors.append)
    if progress is not None:
        worker.progress.connect(progress)
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    worker.start()
    loop.exec()
    # The loop quits on finished, so this is normally the already-finished case
    # of the shared teardown; it deletes the QThread now rather than in whichever
    # nested loop happens to run next.
    stop_qthread_now(worker)
    if cancel_event.is_set():
        raise RenderCancelledError("Audio preparation was cancelled.")
    if not results:
        raise RenderError(errors[0] if errors else "Could not prepare playlist audio.")
    return results[0]


class _PreviewAudioWorker(QThread):
    ready = Signal(str, object)
    """Emits the rendered audio file's path on success."""
    failed = Signal(str)
    progress = Signal(str, float, str)

    def __init__(
        self, renderer: FFmpegRenderer, tracks: list[PlaylistTrack], output_directory: Path,
        transition_mode: str, crossfade_seconds: float, parent: QObject | None = None,
        *, settings=None, cancel_event: threading.Event | None = None, automix_settings=None,
        audio_codec: str = "aac", background: bool = False,
        audio_effects: AudioEffectSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self._background = background
        """Preview's own mix: yield the CPU to playback (never set for Export)."""
        self._automix_settings = automix_settings
        self._audio_effects = audio_effects
        self._audio_codec = audio_codec
        self._renderer = renderer
        self._tracks = tracks
        self._output_directory = output_directory
        self._transition_mode = transition_mode
        self._crossfade_seconds = crossfade_seconds
        self._cancel_event = cancel_event or threading.Event()
        self._settings = settings
        self.plan = compile_playlist(tracks)

    def _accept_plan(self, plan) -> None:
        self.plan = plan

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        if self._background:
            with background_work():
                self._prepare()
        else:
            self._prepare()

    def _prepare(self) -> None:
        try:
            from app.renderer.ffmpeg_renderer import RenderSettings

            self._output_directory.mkdir(parents=True, exist_ok=True)
            path = self._renderer.prepare_playlist_audio(
                self._tracks, self._output_directory, self._settings or RenderSettings(),
                transition_mode=self._transition_mode, crossfade_seconds=self._crossfade_seconds,
                cancel_event=self._cancel_event,
                plan_callback=self._accept_plan,
                progress_callback=self.progress.emit,
                automix_settings=self._automix_settings,
                audio_codec=self._audio_codec,
                audio_effects=self._audio_effects,
            )
        except RenderCancelledError:
            return
        except Exception as error:
            LOGGER.warning("Preview blended-audio render failed: %s", error)
            self.failed.emit(str(error))
            return
        if not self._cancel_event.is_set():
            self.ready.emit(str(path), self.plan)


class PreviewAudioController(QObject):
    """Owns at most one blended-preview-audio render at a time."""

    audio_ready = Signal(str, object)
    """Emits the rendered audio file's path."""
    audio_failed = Signal(str)
    progress = Signal(str, float, str)
    """Forwards the active worker's (stage, fraction, message) progress, if any."""

    def __init__(self, renderer: FFmpegRenderer, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._renderer = renderer
        self._worker: _PreviewAudioWorker | None = None
        self._pending = None
        self._shutting_down = False
        self.last_progress: tuple[str, float, str] | None = None
        """Last ``progress`` payload, so an adopting Preview continues from it."""
        self.progress.connect(lambda *args: setattr(self, "last_progress", args))

    def start(
        self, tracks: list[PlaylistTrack], output_directory: Path,
        transition_mode: str, crossfade_seconds: float, automix_settings=None,
        audio_effects: AudioEffectSettings | None = None,
    ) -> None:
        """Render blended preview audio in the background, replacing any run underway."""
        if self._shutting_down:
            return
        self.cancel()
        if not tracks or (transition_mode == "none" and not (
            has_audio_effects(audio_effects) or any(track.audio_filter for track in tracks)
        )):
            return
        if self._worker is not None:
            self._pending = (tracks, output_directory, transition_mode, crossfade_seconds, automix_settings, audio_effects)
            return
        # Preview plays the export's audio before its AAC encode: same mix, same
        # loudness, lossless, and without the encode (27 s per 40 min of audio).
        worker = _PreviewAudioWorker(
            self._renderer, tracks, output_directory, transition_mode, crossfade_seconds, self,
            automix_settings=automix_settings, audio_codec="flac", background=True,
            audio_effects=audio_effects,
        )
        worker.ready.connect(
            lambda path, plan: self.audio_ready.emit(path, plan)
            if not worker._cancel_event.is_set() else None
        )
        worker.failed.connect(
            lambda message: self.audio_failed.emit(message)
            if not worker._cancel_event.is_set() else None
        )
        worker.finished.connect(lambda: self._forget(worker))
        worker.progress.connect(self.progress.emit)
        self._worker = worker
        worker.start()

    def _forget(self, worker: "_PreviewAudioWorker") -> None:
        if self._worker is worker:
            self._worker = None
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
            self.start(*pending)

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

    def shutdown(self) -> None:
        """Finish cancellation before the owner or its temporary files are deleted.

        stop_qthread_now() takes exclusive ownership of the worker's
        wait()/deleteLater() sequence, so ``_forget()`` can't delete it too.
        """
        self._shutting_down = True
        self.cancel()
        worker, self._worker = self._worker, None
        stop_qthread_now(worker)
