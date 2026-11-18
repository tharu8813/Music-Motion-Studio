"""Shared progress-bar design painting for Canvas, export and picker cards."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen


def progress_fill_brush(style: str, rect: QRectF, fill: QBrush, color: QColor) -> QBrush:
    """Apply a visible gradient preset without overriding user-defined gradients."""
    if style != "gradient" or fill.style() != Qt.BrushStyle.SolidPattern:
        return fill
    gradient = QLinearGradient(rect.topLeft(), rect.topRight())
    gradient.setColorAt(0.0, color.lighter(155))
    gradient.setColorAt(1.0, color.darker(145))
    return QBrush(gradient)


def paint_progress_design(
    painter: QPainter, rect: QRectF, style: str, value: float,
    track_color: QColor, fill: QBrush, fill_color: QColor,
) -> bool:
    """Paint additional designs while respecting user color and progress state.

    Return False for legacy styles, whose rendering remains untouched.
    This is intentionally shared by the compact design gallery and Canvas.
    """
    if style not in {"segments", "rail", "neon"}:
        return False
    if rect.isEmpty():
        return True
    value = max(0.0, min(1.0, float(value)))
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if style == "segments":
        # Keep narrow bars usable instead of producing negative-width slots.
        count = min(12, max(1, int(rect.width() / 6.0)))
        gap = min(4.0, max(0.8, rect.width() * 0.012))
        block = (rect.width() - gap * (count - 1)) / count
        if block <= 0.0:
            painter.restore()
            return True
        for index in range(count):
            slot = QRectF(
                rect.left() + index * (block + gap), rect.top(),
                block, rect.height(),
            )
            radius = min(slot.width(), slot.height()) * 0.23
            painter.setBrush(track_color)
            painter.drawRoundedRect(slot, radius, radius)
            fraction = max(0.0, min(1.0, value * count - index))
            if fraction:
                painter.setBrush(fill)
                painter.drawRoundedRect(
                    QRectF(slot.left(), slot.top(), slot.width() * fraction, slot.height()),
                    radius, radius,
                )
    elif style == "rail":
        radius = rect.height() / 2.0
        thin = max(1.0, rect.height() * 0.23)
        mid = rect.center().y()
        track = QRectF(rect.left(), mid - thin / 2.0, rect.width(), thin)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, thin / 2.0, thin / 2.0)
        filled = QRectF(rect.left(), mid - thin * 0.8, rect.width() * value, thin * 1.6)
        painter.setBrush(fill)
        painter.drawRoundedRect(filled, thin * 0.8, thin * 0.8)
        if value > 0.0:
            tip = rect.left() + rect.width() * value
            painter.setBrush(fill_color)
            painter.drawEllipse(
                QRectF(tip - radius * 0.32, mid - radius * 0.32,
                       radius * 0.64, radius * 0.64)
            )
    else:
        radius = rect.height() / 2.0
        painter.setBrush(track_color)
        painter.drawRoundedRect(rect, radius, radius)
        filled = QRectF(rect.left(), rect.top(), rect.width() * value, rect.height())
        if filled.width() > 0.0:
            glow = QColor(fill_color)
            glow.setAlphaF(glow.alphaF() * 0.18)
            painter.setBrush(glow)
            painter.drawRoundedRect(
                filled.adjusted(0, rect.height() * 0.07, 0, -rect.height() * 0.07),
                radius, radius,
            )
            inner = filled.adjusted(0, rect.height() * 0.22, 0, -rect.height() * 0.22)
            painter.setBrush(fill)
            painter.drawRoundedRect(inner, inner.height() / 2.0, inner.height() / 2.0)
            edge = QColor(fill_color).lighter(155)
            edge.setAlphaF(fill_color.alphaF() * 0.80)
            painter.setPen(QPen(edge, max(0.8, rect.height() * 0.045)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(inner, inner.height() / 2.0, inner.height() / 2.0)
    painter.restore()
    return True
