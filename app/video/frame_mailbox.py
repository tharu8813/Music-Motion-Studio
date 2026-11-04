"""Keep one decoded preview frame before crossing the GUI event queue.

Only QVideoFrame handles cross this bridge. Conversion, QPixmap creation and
scene updates remain on the GUI thread. Final export never uses this bridge.
"""
from threading import Lock

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtMultimedia import QVideoFrame


class VideoFrameMailbox(QObject):
    available = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = Lock()
        self._frame: QVideoFrame | None = None
        self._dropped = 0
        self._notification_pending = False
        self._closed = False

    @Slot(QVideoFrame)
    def offer(self, frame: QVideoFrame) -> None:
        # An end-of-stream notification must not discard the final valid still.
        if not frame.isValid():
            return
        with self._lock:
            if self._closed:
                return
            notify = not self._notification_pending
            if self._frame is not None:
                self._dropped += 1
            self._frame = QVideoFrame(frame)
            self._notification_pending = True
        if notify:
            try:
                self.available.emit()
            except RuntimeError:
                # The sink may be destroyed after a decoder callback begins.
                self.close()

    def take(self) -> tuple[QVideoFrame | None, int]:
        with self._lock:
            frame, dropped = self._frame, self._dropped
            self._frame = None
            self._dropped = 0
            self._notification_pending = False
        return frame, dropped

    def discard(self) -> None:
        """Invalidate a pre-seek frame without adding a second GUI wakeup."""
        with self._lock:
            self._frame = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._frame = None
            self._dropped = 0
            self._notification_pending = False
