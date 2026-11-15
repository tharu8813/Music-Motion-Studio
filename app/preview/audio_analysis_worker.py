"""Background audio analysis used by the export-preview playback UI.

No QWidget, player or scene is accessed by the worker thread. Qt signals
deliver the resulting immutable analysis payload back to the GUI owner.
"""

from __future__ import annotations

import threading
from concurrent.futures import CancelledError
from pathlib import Path
import numpy as np
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QWidget

from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.services.audio_decode import AudioDecodeError, read_pcm
from app.services.track_tempo_service import analyze_track_tempos

class AudioAnalysisWorker(QThread):
    """Build audio-reactive preview frames without blocking the playback UI."""

    ready = Signal(str, int, object)
    failed = Signal(str)

    def __init__(self, renderer: PythonVisualizerRenderer, track: PlaylistTrack,
                 bands: int, fps: int = 30, needs_waveform: bool = False,
                 overlays: tuple[VisualizerOverlay, ...] = (),
                 parent: QWidget | None = None, *, music_profiles: tuple = (),
                 cached_analysis: dict | None = None, needs_tempo: bool = False,
                 needs_levels: bool = True) -> None:
        super().__init__(parent)
        self.renderer = renderer
        self.track = track
        self.bands = bands
        self.fps = fps
        self.needs_waveform = needs_waveform
        self.overlays = overlays
        self.cancel_event = threading.Event()
        self.music_profiles = music_profiles
        self.cached_analysis = cached_analysis
        self.needs_tempo = needs_tempo
        self.needs_levels = needs_levels

    def cancel(self) -> None:
        """Request cancellation before the next analysis block."""
        self.cancel_event.set()

    def run(self) -> None:
        try:
            payload = dict(self.cached_analysis or {})
            if self.needs_levels and "levels" not in payload:
                payload.update(self._analyze_audio())
            if self.needs_tempo and "tempo" not in payload:
                tempos = analyze_track_tempos(
                    [self.track], self.renderer.ffmpeg_executable, self.cancel_event,
                )
                payload["tempo"] = tempos.get(self.track.id)
            payload["music_envelopes"] = {
                profile: self.renderer.music_envelope(payload["levels"], self.fps, profile, self.cancel_event)
                for profile in self.music_profiles
            }
        except Exception as error:
            if not self.cancel_event.is_set():
                self.failed.emit(str(error))
            return
        if not self.cancel_event.is_set():
            self.ready.emit(self.track.id, self.fps, payload)

    def _analyze_audio(self) -> dict:
        needs_meter = any(overlay.kind == "level_meter" for overlay in self.overlays)
        path = Path(self.track.file_path)
        channels = 2 if needs_meter else 1
        try:
            pcm = read_pcm(path, self.cancel_event, sample_rate=self.renderer.sample_rate, channels=channels)
        except AudioDecodeError:
            if self.cancel_event.is_set():
                raise CancelledError()
            if self.renderer.ffmpeg_executable is None:
                raise
            pcm = (self.renderer._decode_stereo_audio(path, self.cancel_event) if needs_meter else
                   self.renderer._decode_mono_audio(path, self.cancel_event)[:, None])
        else:
            if not len(pcm):
                pcm = np.zeros((1, channels), dtype=np.float32)
            # Preserve the sample range and resolution of the FFmpeg s16le preview path.
            pcm *= 32768.0
            np.rint(pcm, out=pcm)
            np.clip(pcm, -32768.0, 32767.0, out=pcm)
            pcm /= 32768.0
        stereo = pcm if needs_meter else None
        samples = pcm.mean(axis=1, dtype=np.float32) if needs_meter else pcm[:, 0]
        levels = self.renderer._analyze_levels(samples, self.fps, self.bands, self.cancel_event)
        waveform = (
            self.renderer._analyze_waveform(samples, self.fps, max(32, self.bands), self.cancel_event)
            if self.needs_waveform else np.zeros((1, max(32, self.bands)), dtype=np.float32)
        )
        processed = tuple(
            (
                np.zeros((1, 1), dtype=np.float32)
                if overlay.kind in {"particles", "level_meter"} else
                self.renderer.process_level_sequence(
                    waveform if overlay.kind == "waveform" else levels,
                    overlay,
                    max(4, min(96, overlay.bar_count)),
                )
            )
            for overlay in self.overlays
        )
        raw_meter = (
            np.column_stack((
                self.renderer._analyze_rms(stereo[:, 0], self.fps, self.cancel_event),
                self.renderer._analyze_rms(stereo[:, 1], self.fps, self.cancel_event),
            ))
            if stereo is not None else None
        )
        meters = tuple(
            self.renderer.process_meter_sequence(raw_meter, overlay, self.fps)
            if overlay.kind == "level_meter" and raw_meter is not None else None
            for overlay in self.overlays
        )
        return {"levels": levels, "waveform": waveform,
                "bass": self.renderer.bass_envelope(levels, self.fps),
                "processed": processed, "meters": meters}
