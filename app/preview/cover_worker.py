"""Background thumbnail reader used by the full-playlist preview.

All artwork loading happens on QThread; only the receiving GUI creates
QIcons. Requesting interruption prevents subsequent images being emitted.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from app.preview.album_art import prewarm_cover_thumbnail


LOGGER = logging.getLogger(__name__)


class PreviewCoverWorker(QThread):
    """Read artwork off the GUI thread; only the receiver creates icons."""

    ready = Signal(int, QImage)

    def __init__(self, tracks, parent=None):
        super().__init__(parent)
        self.paths = tuple((track.file_path, track.cover_path) for track in tracks)

    def run(self) -> None:
        for index, (audio_path, cover_path) in enumerate(self.paths):
            if self.isInterruptionRequested():
                return
            try:
                image = prewarm_cover_thumbnail(audio_path, cover_path)
            except Exception as error:
                LOGGER.warning("Preview cover could not load for %s: %s", audio_path, error)
                continue
            if self.isInterruptionRequested():
                return
            if not image.isNull():
                self.ready.emit(index, image)
