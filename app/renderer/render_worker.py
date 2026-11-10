"""Background QThread wrapper around the synchronous FFmpeg renderer."""

from __future__ import annotations

import threading
import logging
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
from app.utils.logging_setup import report_unexpected_error


LOGGER = logging.getLogger(__name__)


class RenderWorker(QThread):
    """Runs an FFmpeg export without blocking the Qt event loop."""

    progress = Signal(str, float, str)
    succeeded = Signal(object)
    failed = Signal(str)
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
                 automix_settings=None) -> None:
        super().__init__()
        self.renderer = renderer
        self.transition_mode = transition_mode
        self.crossfade_seconds = crossfade_seconds
        self.automix_settings = automix_settings
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
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Request a cooperative FFmpeg stop at the next process polling interval."""
        self._cancel_event.set()

    def run(self) -> None:
        """Execute the render and forward outcome signals to the GUI thread."""
        try:
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
            )
        except RenderCancelledError:
            self.cancelled.emit()
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
