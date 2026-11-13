"""Circular (radial) visualizer bars shared by the Canvas and reactive export layers."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from math import cos, pi, radians, sin

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen


@lru_cache(maxsize=32)
def radial_bar_directions(count: int) -> tuple[tuple[float, float], ...]:
    step = 180.0 / count
    angles = (radians(-90.0 + side * (index + 0.5) * step)
              for index in range(count) for side in (1.0, -1.0))
    return tuple((cos(angle), sin(angle)) for angle in angles)


def radial_bar_geometry(width: float, height: float, count: int, line_width: float,
                        inner_ratio: float) -> tuple[float, float, float]:
    outer = min(width, height) / 2.0
    inner = outer * max(0.1, min(0.95, inner_ratio))
    reach = max(1.0, outer - inner - line_width / 2.0)
    circumference_share = 2.0 * pi * inner / (count * 2)
    return inner, reach, max(1.0, min(line_width, circumference_share * 0.7))


def paint_radial_bars(
    painter: QPainter, rect: QRectF, levels: Sequence[float], color: QColor,
    line_width: float, inner_ratio: float,
) -> None:
    """Draw bars radiating out of a ring, mirrored left/right from the top.

    ``inner_ratio`` is the ring radius as a share of the largest circle that
    fits ``rect``; the space inside it is left free for a centred cover.
    """
    count = len(levels)
    if count == 0 or rect.isEmpty():
        return
    center = rect.center()
    inner, reach, stroke = radial_bar_geometry(rect.width(), rect.height(), count, line_width, inner_ratio)
    pen = QPen(color, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.save()
    painter.setPen(pen)
    directions = iter(radial_bar_directions(count))
    for level in levels:
        length = reach * max(0.03, min(1.0, float(level)))
        for x, y in (next(directions), next(directions)):
            direction = QPointF(x, y)
            painter.drawLine(center + direction * inner, center + direction * (inner + length))
    painter.restore()
