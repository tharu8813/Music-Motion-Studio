"""Shared visualizer drawing rules for the editor, preview and video export.

This function paints only the source-local content. Callers apply the same
source mirror via mirror_content exactly once, before drawing. They own
masking, opacity, frames and coordinate transforms.
"""
from __future__ import annotations

from math import sin
from typing import Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

from app.utils.radial_visualizer import paint_radial_bars


def draw_visualizer_content(
    painter: QPainter,
    rect: QRectF,
    levels: Sequence[float],
    *,
    style: str,
    color: QColor,
    line_width: float,
    inner_radius: float = 0.55,
) -> None:
    """Draw source-local coordinates without implicitly flipping the content.

    Canvas and the FFmpeg layer previously maintained two similar drawing
    routines. Their pen defaults differed and could produce different edges.
    Keep orientation in mirror_content only.
    """
    count = len(levels)
    if count == 0:
        return
    gap = max(1.0, rect.width() * 0.012 / count)
    bar_width = max(1.0, (rect.width() - gap * (count - 1)) / count)
    painter.save()
    try:
        if style in {"line", "wave"}:
            painter.setPen(QPen(color, max(float(line_width), rect.height() * 0.025)))
            path = QPainterPath(QPointF(rect.left(), rect.center().y()))
            for index, level in enumerate(levels):
                x = rect.left() + index * rect.width() / max(1, count - 1)
                y = rect.center().y() - (float(level) - 0.5) * rect.height() * 0.82
                if style == "wave":
                    y = rect.center().y() - sin(index * 0.42) * float(level) * rect.height() * 0.36
                path.lineTo(x, y)
            painter.drawPath(path)
        elif style == "radial":
            paint_radial_bars(painter, rect, [float(level) for level in levels],
                              color, line_width, inner_radius)
        elif style == "arc":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for index, level in enumerate(levels):
                inset = index * min(rect.width(), rect.height()) / max(1, count * 3.2)
                arc_rect = rect.adjusted(inset, inset, -inset, -inset)
                arc_color = QColor(color)
                arc_color.setAlpha(max(45, int(255 * float(level))))
                painter.setPen(QPen(arc_color, max(1.0, line_width * 0.75)))
                painter.drawArc(arc_rect, 210 * 16, int(120 * 16 * float(level)))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            for index, level in enumerate(levels):
                value = float(level)
                x = rect.left() + index * (bar_width + gap)
                if style == "spectrum":
                    painter.setBrush(QColor.fromHsv(int(300 * index / max(1, count - 1)), 210, 245))
                else:
                    painter.setBrush(color)
                if style == "led":
                    segments = 8
                    segment_gap = max(1.0, rect.height() * 0.025)
                    segment_height = (rect.height() - segment_gap * (segments - 1)) / segments
                    active = max(1, round(value * segments))
                    for segment in range(active):
                        y = rect.bottom() - (segment + 1) * segment_height - segment * segment_gap
                        led_color = QColor(color)
                        led_color.setAlpha(max(80, color.alpha() - segment * 9))
                        painter.setBrush(led_color)
                        painter.drawRoundedRect(QRectF(x, y, bar_width, segment_height), 2, 2)
                elif style == "center":
                    bar_height = rect.height() * value
                    painter.drawRoundedRect(
                        QRectF(x, rect.bottom() - bar_height, bar_width, bar_height),
                        bar_width / 2, bar_width / 2,
                    )
                elif style == "mirror":
                    bar_height = rect.height() * value * 0.48
                    painter.drawRoundedRect(
                        QRectF(x, rect.center().y() - bar_height, bar_width, bar_height),
                        bar_width / 2, bar_width / 2,
                    )
                    painter.drawRoundedRect(
                        QRectF(x, rect.center().y(), bar_width, bar_height),
                        bar_width / 2, bar_width / 2,
                    )
                elif style == "dots":
                    dot_size = max(3.0, min(bar_width * 1.35, rect.height() * 0.16))
                    for dot_index in range(max(1, int(value * 7))):
                        y = rect.bottom() - dot_size - dot_index * (dot_size + 3)
                        painter.drawEllipse(QRectF(x, y, dot_size, dot_size))
                else:
                    bar_height = rect.height() * value
                    draw_width = bar_width * 0.62 if style == "capsule" else bar_width
                    radius = draw_width / 2
                    painter.drawRoundedRect(
                        QRectF(x + (bar_width - draw_width) / 2,
                               rect.center().y() - bar_height / 2,
                               draw_width, bar_height),
                        radius, radius,
                    )
    finally:
        painter.restore()
