"""CPU preview canvas widget with letterboxed, cached QImage painting.

Presentation only: the ExportPreviewDialog retains ownership of the image
producer, timers, preview signals, GPU fallback and playback worker lifetime.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.ui.design_system import COLORS


class CpuPreviewSurface(QWidget):
    """CPU preview surface that scales during paint instead of copying a pixmap."""

    frame_presented = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.image = QImage()
        self._target_key: tuple[int, int, int, int] | None = None
        self._target_rect = QRect()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def set_image(self, image: QImage) -> None:
        """Present the latest frame without constructing a scaled QPixmap copy."""
        self.image = image
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["stage"]))
        if not self.image.isNull():
            target_key = (self.image.width(), self.image.height(), self.width(), self.height())
            if self._target_key != target_key:
                size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
                x = (self.width() - size.width()) // 2
                y = (self.height() - size.height()) // 2
                self._target_rect = QRect(x, y, size.width(), size.height())
                self._target_key = target_key
            needs_scaling = self._target_rect.size() != self.image.size()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, needs_scaling)
            painter.drawImage(self._target_rect, self.image)
        painter.end()
        self.frame_presented.emit()
