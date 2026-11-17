"""Shared waveform geometry for the Inspector, Canvas and Python overlay renderer.

The source's waveform_style must never paint as three indistinguishable
thumbnails. Live export supplies analyzed amplitudes; editor tiles supply
representative amplitudes, but both take this exact style rendering path.
"""

from __future__ import annotations

from collections.abc import Iterable
from math import isfinite

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen


def paint_waveform(
    painter: QPainter, rect: QRectF, levels: Iterable[float], *,
    style: str = "line", color: QColor | str = "#7C3AED",
    line_width: float = 2.0,
) -> None:
    """Draw a line, filled envelope or symmetrical waveform inside `rect`."""
    amplitudes = [
        max(0.0, min(1.0, abs(float(level)))) if isfinite(float(level)) else 0.0
        for level in levels
    ]
    if not amplitudes or rect.isEmpty():
        return

    ink = QColor(color)
    if not ink.isValid():
        return
    center_y = rect.center().y()
    top = QPainterPath()
    bottom = QPainterPath()
    last = max(1, len(amplitudes) - 1)
    for index, level in enumerate(amplitudes):
        x = rect.left() + rect.width() * index / last
        displacement = level * rect.height() * 0.44
        point = QPointF(x, center_y - displacement)
        mirrored = QPointF(x, center_y + displacement)
        if index == 0:
            top.moveTo(point)
            bottom.moveTo(mirrored)
        else:
            top.lineTo(point)
            bottom.lineTo(mirrored)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
    painter.setPen(QPen(ink, max(1.0, float(line_width))))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if style == "filled":
        area = QPainterPath(top)
        area.lineTo(rect.right(), center_y)
        area.lineTo(rect.left(), center_y)
        area.closeSubpath()
        fill = QColor(ink)
        fill.setAlphaF(ink.alphaF() * 0.40)
        painter.fillPath(area, fill)
    painter.drawPath(top)
    if style == "mirror":
        painter.drawPath(bottom)
    painter.restore()
