"""Background QThread wrapper around the synchronous FFmpeg renderer."""

from __future__ import annotations

import threading
import logging
from time import monotonic
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import (
    ExportMetadata,
    FFmpegRenderer,
    RenderCancelledError,
    RenderError,
    RenderResult,
    RenderFrame,
    PipedStaticOverlayLayer,
    PipedVideoInput,
    PreparedVideoInput,
    RenderSettings,
    PreparedStaticOverlayLayer,
    StaticOverlayLayer,
    VisualizerOverlay,
    VideoClipOverlay,
)
from app.renderer.native_gpu_stream import NativeGpuPreflightError
from app.utils.logging_setup import report_unexpected_error


LOGGER = logging.getLogger(__name__)


class RenderWorker(QThread):
    """Runs an FFmpeg export without blocking the Qt event loop."""

    progress = Signal(str, float, str)
    succeeded = Signal(object)
    failed = Signal(str)
    native_preflight_failed = Signal(str)
    cancelled = Signal()
    storage_path_changed = Signal(str, object)

    def __init__(self, renderer: FFmpegRenderer,
                 image: QImage | list[QImage] | list[RenderFrame] | PreparedVideoInput
                 | PipedVideoInput,
                 tracks: list[PlaylistTrack], output_path: str | Path,
                 settings: RenderSettings | None = None,
                 visualizers: list[VisualizerOverlay] | None = None,
                 static_layers: list[
                     StaticOverlayLayer | PreparedStaticOverlayLayer | PipedStaticOverlayLayer
                 ] | None = None,
                 video_clips: list[VideoClipOverlay] | None = None,
                 metadata: ExportMetadata | None = None,
                 transition_mode: str = "none",
                 crossfade_seconds: float = 3.0,
                 compiled_plan=None, prepared_audio_path: Path | None = None,
                 automix_settings=None, native_project=None, native_frame_source=None,
                 native_scale=1.0, cancel_event=None, native_optimizations=None, audio_effects=None) -> None:
        super().__init__()
        self.renderer = renderer
        self.transition_mode = transition_mode
        self.crossfade_seconds = crossfade_seconds
        self.automix_settings = automix_settings
        self.audio_effects = audio_effects
        self.compiled_plan = compiled_plan
        self.prepared_audio_path = prepared_audio_path
        if isinstance(image, (PreparedVideoInput, PipedVideoInput)):
            self.image = image
        elif isinstance(image, list):
            self.image = [
                RenderFrame(
                    frame.image if isinstance(frame.image, Path) else QImage(frame.image),
                    frame.duration_seconds,
                )
                if isinstance(frame, RenderFrame) else QImage(frame)
                for frame in image
            ]
        else:
            self.image = QImage(image)
        self.tracks = list(tracks)
        self.output_path = Path(output_path)
        self.settings = settings
        self.visualizers = list(visualizers or [])
        self.video_clips = list(video_clips or [])
        self.static_layers = list(static_layers or [])
        self.metadata = metadata
        self._cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.native_project = native_project
        self.native_frame_source = native_frame_source
        self._native_last_progress_at = 0.0
        self.native_scale = native_scale
        self.native_optimizations = native_optimizations

    def cancel(self) -> None:
        """Request a cooperative FFmpeg stop at the next process polling interval."""
        self._cancel_event.set()

    def _report_native_frame(self, done: int, total: int) -> None:
        """Bound expensive GUI progress signals to 4 Hz, always report last.

        The original code queued one full Qt dialog update for every frame:
        131,417 signals for a 36-minute 60 FPS export. Progress callbacks
        are presentational; every native frame is still sent and ACKed.
        """
        now = monotonic()
        if (done != 1 and done != total
                and now - self._native_last_progress_at < 0.25):
            return
        self._native_last_progress_at = now
        self.progress.emit(
            "Encoding video", 0.1 + 0.75 * done / max(1, total),
            f"Rust GPU: {done}/{total} frames",
        )

    def run(self) -> None:
        """Execute the render and forward outcome signals to the GUI thread."""
        try:
            if self.native_project is not None:
                from app.renderer.native_project_export import export_project_document
                result = export_project_document(
                    self.native_project, self.output_path, fps=self.settings.fps,
                    scale=self.native_scale, ffmpeg=str(self.renderer.executable),
                    cancel_event=self._cancel_event, frame_source=self.native_frame_source,
                    audio_bitrate=self.settings.audio_bitrate,
                    native_nvenc_preset=self.settings.native_nvenc_preset,
                    native_nvenc_cq=self.settings.native_nvenc_cq,
                    optimizations=self.native_optimizations,
                    storage_path_callback=self.storage_path_changed.emit,
                    preparation_progress_callback=lambda stage, fraction, message: self.progress.emit(
                        stage, 0.07 * fraction, message,
                    ),
                    progress_callback=self._report_native_frame,
                    mux_progress_callback=lambda stage, fraction, message: self.progress.emit(
                        stage, 0.85 + 0.15 * fraction, message,
                    ),
                )
            else:
                result = self.renderer.render(
                    self.image, self.tracks, self.output_path, self.settings,
                    progress_callback=self.progress.emit, cancel_event=self._cancel_event,
                    visualizers=self.visualizers, static_layers=self.static_layers,
                    video_clips=self.video_clips, metadata=self.metadata,
                    storage_path_callback=self.storage_path_changed.emit,
                    transition_mode=self.transition_mode,
                    crossfade_seconds=self.crossfade_seconds,
                    compiled_plan=self.compiled_plan,
                    prepared_audio_path=self.prepared_audio_path,
                    automix_settings=self.automix_settings,
                    audio_effects=self.audio_effects,
                )
        except RenderCancelledError:
            self.cancelled.emit()
        except NativeGpuPreflightError as error:
            self.native_preflight_failed.emit(str(error))
        except RenderError as error:
            self.failed.emit(str(error))
        except Exception as error:
            # Never let an unexpected worker exception escape QThread.  Besides
            # avoiding a process-level Qt abort, this preserves the traceback in
            # the persistent log and returns control to the export error dialog.
            LOGGER.exception("Unexpected export worker failure")
            report_unexpected_error("Export worker", error)
            self.failed.emit(f"Unexpected export failure: {error}")
        else:
            self.succeeded.emit(result)
