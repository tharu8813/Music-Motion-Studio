"""Short-lived background worker for per-layer visualizer preview frames.

The preview dialog owns worker lifetime and generation invalidation; this
module only prepares images and emits results. It does not manage playback.
"""

from __future__ import annotations

import threading

import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget

from app.preview.overlay_frame_cache import OverlaySignature
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.renderer.python_visualizer import PythonVisualizerRenderer


class OverlayFrameWorker(QThread):
    """Pre-render a small run of pure-QImage overlay frames off the UI thread."""

    ready = Signal(str, int, int, object, object)
    failed = Signal(str)

    def __init__(self, track_id: str, fps: int, generation: int, start_frame: int, frame_count: int,
                 overlays: tuple[VisualizerOverlay, ...],
                 analysis_indices: OverlaySignature,
                 analysis: dict[str, np.ndarray],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self.prepare(track_id, fps, generation, start_frame, frame_count,
                     overlays, analysis_indices, analysis)

    def prepare(self, track_id: str, fps: int, generation: int, start_frame: int, frame_count: int,
                overlays: tuple[VisualizerOverlay, ...], analysis_indices: OverlaySignature,
                analysis: dict[str, np.ndarray]) -> None:
        """Reuse the worker only after its previous finished signal was joined."""
        self.track_id = track_id
        self.fps = fps
        self.generation = generation
        self.start_frame = start_frame
        self.frame_count = frame_count
        self.overlays = overlays
        if len(analysis_indices) != len(overlays):
            raise ValueError("Every preview overlay requires its original analysis index.")
        self.analysis_indices = analysis_indices
        self.overlay_signature = analysis_indices
        self.analysis = analysis
        self.cancel_event.clear()
        self.busy = True

    def cancel(self) -> None:
        """Discard this short prefetch batch at the next frame boundary."""
        self.cancel_event.set()

    def run(self) -> None:
        try:
            rendered: list[tuple[int, tuple[QImage, ...]]] = []
            spectrum = self.analysis["levels"]
            waveform = self.analysis["waveform"]
            for frame_index in range(self.start_frame, self.start_frame + self.frame_count):
                if self.cancel_event.is_set():
                    return
                layers: list[QImage] = []
                processed = self.analysis.get("processed", ())
                meters = self.analysis.get("meters", ())
                for overlay_index, overlay in enumerate(self.overlays):
                    analysis_index = self.analysis_indices[overlay_index]
                    values = waveform if overlay.kind == "waveform" else spectrum
                    is_processed = analysis_index < len(processed)
                    if is_processed:
                        values = processed[analysis_index]
                    values_for_frame = values[min(frame_index, len(values) - 1)]
                    meter_values = None
                    peak_values = None
                    if analysis_index < len(meters) and meters[analysis_index] is not None:
                        meter_levels, meter_peaks = meters[analysis_index]
                        meter_index = min(frame_index, len(meter_levels) - 1)
                        meter_values = tuple(float(value) for value in meter_levels[meter_index])
                        peak_values = tuple(float(value) for value in meter_peaks[meter_index])
                    layers.append(PythonVisualizerRenderer.preview_image(
                        overlay.width, overlay.height, overlay, values_for_frame, frame_index,
                        processed=is_processed,
                        frame_rate=self.fps,
                        channel_values=meter_values,
                        peak_values=peak_values,
                    ))
                rendered.append((frame_index, tuple(layers)))
            if not self.cancel_event.is_set():
                self.ready.emit(
                    self.track_id, self.fps, self.generation,
                    self.overlay_signature, rendered,
                )
        except Exception as error:
            if not self.cancel_event.is_set():
                self.failed.emit(str(error))
