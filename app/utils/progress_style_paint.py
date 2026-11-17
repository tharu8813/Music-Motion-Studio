"""Shared progress-bar fill policy for Canvas/export and visual preset cards."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QLinearGradient


def progress_fill_brush(style: str, rect: QRectF, fill: QBrush, color: QColor) -> QBrush:
    """Apply a visible gradient preset without overriding user-defined gradients.

    Previously `gradient` and `rounded` rendered identically unless the
    user independently enabled a gradient, making the choice misleading.
    """
    if style != "gradient" or fill.style() != Qt.BrushStyle.SolidPattern:
        return fill
    gradient = QLinearGradient(rect.topLeft(), rect.topRight())
    gradient.setColorAt(0.0, color.lighter(155))
    gradient.setColorAt(1.0, color.darker(145))
    return QBrush(gradient)
