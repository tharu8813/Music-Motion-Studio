"""Decorative album-cover frames used by the Canvas and design picker."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen


def album_art_rect(rect: QRectF, style: str) -> QRectF:
    """The portion of the source occupied by artwork inside the new frames."""
    if style == "vinyl":
        size = min(rect.width(), rect.height()) * 0.78
        return QRectF(rect.center().x() - size / 2, rect.center().y() - size / 2, size, size)
    if style == "filmstrip":
        return rect.adjusted(rect.width() * 0.16, rect.height() * 0.045,
                             -rect.width() * 0.16, -rect.height() * 0.045)
    return QRectF(rect)


def paint_album_frame_back(painter: QPainter, rect: QRectF, style: str) -> None:
    """Paint the backing, keeping all design decoration inside the source."""
    if style not in {"vinyl", "filmstrip"}:
        return
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    if style == "vinyl":
        painter.setBrush(QColor("#11151B"))
        painter.drawEllipse(rect)
    else:
        painter.setBrush(QColor("#12161B"))
        painter.drawRoundedRect(rect, min(7.0, rect.width() * 0.045),
                                min(7.0, rect.width() * 0.045))
    painter.restore()


def paint_album_frame_front(painter: QPainter, rect: QRectF, style: str) -> None:
    """Draw vinyl grooves / spindle or the perforations of a film strip."""
    if style not in {"vinyl", "filmstrip"}:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if style == "vinyl":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QColor("#CFD7DB")
        pen.setAlpha(110)
        painter.setPen(QPen(pen, max(0.8, rect.width() * 0.009)))
        for factor in (0.93, 0.86):
            radius = min(rect.width(), rect.height()) * factor / 2.0
            painter.drawEllipse(rect.center(), radius, radius)
        hole_radius = max(1.1, min(rect.width(), rect.height()) * 0.045)
        painter.setBrush(QColor("#14171C"))
        painter.setPen(QPen(QColor("#D7E3E5"), max(0.7, hole_radius * 0.16)))
        painter.drawEllipse(rect.center(), hole_radius, hole_radius)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#D6DEE0"))
        holes = 6
        hole_width = max(1.4, rect.width() * 0.055)
        hole_height = max(1.4, rect.height() * 0.065)
        for index in range(holes):
            y = rect.top() + rect.height() * (index + 0.55) / holes
            for x in (
                rect.left() + rect.width() * 0.052,
                rect.right() - rect.width() * 0.052 - hole_width,
            ):
                painter.drawRoundedRect(
                    QRectF(x, y - hole_height / 2, hole_width, hole_height),
                    min(1.0, hole_width * 0.18), min(1.0, hole_width * 0.18),
                )
    painter.restore()
