"""Shared content mirroring and inside frames for Canvas and reactive export."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen


def mirror_content(painter: QPainter, rect: QRectF, source: object) -> None:
    horizontal = bool(getattr(source, "flip_horizontal", False))
    vertical = bool(getattr(source, "flip_vertical", False))
    if horizontal or vertical:
        center = rect.center()
        painter.translate(center)
        painter.scale(-1.0 if horizontal else 1.0, -1.0 if vertical else 1.0)
        painter.translate(-center)


def frame_geometry(rect: QRectF, width: float, inset: float) -> tuple[QRectF, float]:
    """Clamp an inside stroke to small sources without inverted rectangles."""
    limit = min(rect.width(), rect.height()) / 2.0
    inset = max(0.0, min(inset, limit))
    width = max(0.0, min(width, limit - inset))
    return rect.adjusted(inset + width / 2, inset + width / 2,
                         -inset - width / 2, -inset - width / 2), width


def paint_frame(painter: QPainter, rect: QRectF, source: object) -> None:
    box, width = frame_geometry(rect, float(getattr(source, "frame_width", 0.0)),
                                float(getattr(source, "frame_inset", 0.0)))
    if width <= 0:
        return
    painter.save()
    painter.setPen(QPen(QColor(getattr(source, "frame_color", "#FFFFFF")), width,
                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap, Qt.PenJoinStyle.MiterJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(box)
    painter.restore()
