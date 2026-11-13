"""Async video duration lookup without blocking the preview GUI."""

from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QWidget

from app.services.playlist_service import PlaylistService
from app.video.duration_probe import mp4_container_duration

class VideoDurationProbeWorker(QThread):
    """Resolve one media duration without blocking preview interaction."""

    ready = Signal(str, float)

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path

    def run(self) -> None:
        path = Path(self.path)
        # Local MP4/MOV boxes expose movie duration without initializing the
        # managed FFmpeg environment or spawning a 15-second FFprobe process.
        # Other formats and incomplete containers retain the old fallback.
        duration = mp4_container_duration(path)
        if duration is None and not self.isInterruptionRequested():
            duration = PlaylistService._probe_duration(path)
        if not self.isInterruptionRequested():
            self.ready.emit(self.path, duration or 0.0)
