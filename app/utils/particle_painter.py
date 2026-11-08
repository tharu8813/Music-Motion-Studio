"""Shared deterministic particle drawing for Canvas, preview, and export."""

from __future__ import annotations

from functools import lru_cache
from math import cos, floor, pi, radians, sin
import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from app.utils.performance import timed


# Opt-out allows the project's export A/B tools to compare uncached and cached
# rendering without changing project data or the animation timeline.
_DISABLE_PARTICLE_STATIC_CACHE = (
    os.environ.get("PLAYLIST_CANVAS_DISABLE_PARTICLE_STATIC_CACHE", "")
    .strip().lower() in {"1", "true", "yes"}
)


def _random_unit(index: int, channel: int, seed: int) -> float:
    """Return a stable pseudo-random value without mutable renderer state."""
    value = sin((index + 1) * (12.9898 + channel * 7.233) + seed * 0.731) * 43758.5453
    return value - floor(value)


@lru_cache(maxsize=64)
def _particle_static_values(
    density: int, seed: int,
) -> tuple[tuple[float, float, float, float], ...]:
    """Cache the four time-invariant random parameters of each particle.

    One immutable table is shared by all frames using the same seed/density.
    The 64-entry bound limits memory in projects with many independent layers;
    no QPainter, QColor, source model or mutable Qt state is retained.
    """
    return tuple(
        (
            _random_unit(index, 0, seed) * 2.0 * pi,
            _random_unit(index, 1, seed),
            _random_unit(index, 2, seed),
            _random_unit(index, 3, seed),
        )
        for index in range(density)
    )


@timed("canvas.particle_paint_seconds")
def paint_particles(
    painter: QPainter,
    rect: QRectF,
    *,
    style: str,
    color: str,
    secondary_color: str,
    density: int,
    speed: float,
    minimum_size: float,
    maximum_size: float,
    particle_opacity: float,
    direction: float,
    drift: float,
    twinkle: float,
    glow: float,
    seed: int,
    time_seconds: float = 0.0,
) -> None:
    """Paint a repeatable animated particle field inside *rect*."""
    density = max(4, min(500, int(density)))
    speed = max(0.0, min(5.0, float(speed)))
    minimum_size = max(0.5, min(40.0, float(minimum_size)))
    maximum_size = max(minimum_size, min(80.0, float(maximum_size)))
    particle_opacity = max(0.0, min(1.0, float(particle_opacity)))
    drift = max(0.0, min(2.0, float(drift)))
    twinkle = max(0.0, min(1.0, float(twinkle)))
    glow = max(0.0, min(1.0, float(glow)))
    angle = radians(float(direction))
    travel = time_seconds * speed * max(rect.width(), rect.height()) * 0.075

    primary = QColor(color)
    secondary = QColor(secondary_color)
    painter.setPen(Qt.PenStyle.NoPen)
    static_values = (
        None if _DISABLE_PARTICLE_STATIC_CACHE
        else _particle_static_values(density, seed)
    )
    for index in range(density):
        if static_values is None:
            phase = _random_unit(index, 0, seed) * 2.0 * pi
            base_x = _random_unit(index, 1, seed) * rect.width()
            base_y = _random_unit(index, 2, seed) * rect.height()
            size_ratio = _random_unit(index, 3, seed)
        else:
            phase, x_ratio, y_ratio, size_ratio = static_values[index]
            base_x = x_ratio * rect.width()
            base_y = y_ratio * rect.height()
        sway = sin(time_seconds * speed * 1.35 + phase) * drift
        x = (base_x + cos(angle) * travel + sway * rect.width() * 0.035) % max(1.0, rect.width())
        y = (base_y + sin(angle) * travel + cos(phase) * sway * rect.height() * 0.018) % max(1.0, rect.height())
        size = minimum_size + (maximum_size - minimum_size) * size_ratio
        pulse = 1.0 - twinkle * 0.45 + twinkle * 0.45 * (
            sin(time_seconds * (1.8 + size_ratio * 2.4) + phase) * 0.5 + 0.5
        )
        alpha = max(0, min(255, round(255 * particle_opacity * pulse)))
        dot = QColor(secondary if index % 3 == 0 else primary)
        dot.setAlpha(alpha)
        particle_rect = QRectF(rect.left() + x - size / 2, rect.top() + y - size / 2, size, size)

        if glow > 0.0 and style in {"neon", "bokeh", "stars"}:
            halo = QColor(dot)
            halo.setAlpha(max(1, round(alpha * glow * 0.28)))
            halo_size = size * (1.8 + glow * 2.2)
            painter.setBrush(halo)
            painter.drawEllipse(QRectF(
                particle_rect.center().x() - halo_size / 2,
                particle_rect.center().y() - halo_size / 2,
                halo_size,
                halo_size,
            ))

        if style == "noise":
            painter.setBrush(dot)
            painter.drawRect(particle_rect)
        elif style == "snow":
            painter.setBrush(dot)
            painter.drawEllipse(particle_rect)
            if size >= 3.0:
                flake = QColor(dot)
                flake.setAlpha(max(1, round(alpha * 0.72)))
                painter.setPen(QPen(flake, max(0.7, size * 0.1)))
                center = particle_rect.center()
                painter.drawLine(
                    QPointF(center.x(), particle_rect.top()),
                    QPointF(center.x(), particle_rect.bottom()),
                )
                painter.drawLine(
                    QPointF(particle_rect.left(), center.y()),
                    QPointF(particle_rect.right(), center.y()),
                )
                painter.setPen(Qt.PenStyle.NoPen)
        elif style == "stars":
            painter.setBrush(dot)
            center = particle_rect.center()
            path = QPainterPath(QPointF(center.x(), particle_rect.top()))
            path.lineTo(QPointF(center.x() + size * 0.18, center.y() - size * 0.18))
            path.lineTo(QPointF(particle_rect.right(), center.y()))
            path.lineTo(QPointF(center.x() + size * 0.18, center.y() + size * 0.18))
            path.lineTo(QPointF(center.x(), particle_rect.bottom()))
            path.lineTo(QPointF(center.x() - size * 0.18, center.y() + size * 0.18))
            path.lineTo(QPointF(particle_rect.left(), center.y()))
            path.lineTo(QPointF(center.x() - size * 0.18, center.y() - size * 0.18))
            path.closeSubpath()
            painter.drawPath(path)
        elif style == "bokeh":
            outline = QColor(dot)
            outline.setAlpha(max(1, round(alpha * 0.8)))
            fill = QColor(dot)
            fill.setAlpha(max(1, round(alpha * 0.18)))
            painter.setPen(QPen(outline, max(1.0, size * 0.12)))
            painter.setBrush(fill)
            painter.drawEllipse(particle_rect)
            painter.setPen(Qt.PenStyle.NoPen)
        elif style == "confetti":
            painter.save()
            painter.translate(particle_rect.center())
            painter.rotate((time_seconds * speed * 90.0 + phase * 57.2958) % 360.0)
            painter.setBrush(dot)
            painter.drawRoundedRect(QRectF(-size / 2, -size * 0.22, size, size * 0.44), 1.0, 1.0)
            painter.restore()
        else:
            painter.setBrush(dot)
            painter.drawEllipse(particle_rect)
