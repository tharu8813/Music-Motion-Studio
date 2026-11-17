"""Canvas renderer for SourceType.PROGRESS_BAR, split out of SourceItem._paint_legacy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter

from app.canvas.renderers.base import paint_background, paint_selection_guide
from app.utils.progress_style_paint import progress_fill_brush

if TYPE_CHECKING:
    from app.canvas.source_item import SourceItem


def render(
    item: "SourceItem", painter: QPainter, option: object, widget: object | None = None,
) -> None:
    rect, fill, _pen = paint_background(item, painter)
    full_rect = rect
    style = item.source.progress_style
    knob = item.source.progress_knob
    if knob == "auto":
        knob = "spotify" if style == "spotify" else "none"
    elif knob != "none":
        # A slider handle: a slimmer track so the handle stands out of it.
        thickness = rect.height() * 0.4
        rect = QRectF(rect.left(), rect.center().y() - thickness / 2, rect.width(), thickness)
    radius = 0.0 if style == "youtube" else rect.height() / 2
    track_color = QColor(item.source.progress_track_color)
    if style == "apple":
        track_color = QColor(item.source.fill_color)
        track_color.setAlpha(80)
    elif style == "spotify":
        track_color = QColor("#1B2530")
    painter.setBrush(track_color)
    painter.drawRoundedRect(rect, radius, radius)
    painter.setBrush(progress_fill_brush(style, rect, fill, QColor(item.source.fill_color)))
    progress_width = rect.width() * max(0.0, min(1.0, item.source.progress_value))
    if style == "apple":
        progress_width = rect.width() * max(0.0, min(1.0, item.source.progress_value))
    painter.drawRoundedRect(QRectF(rect.left(), rect.top(), progress_width, rect.height()), radius, radius)
    center = QPointF(rect.left() + progress_width, rect.center().y())
    painter.setPen(Qt.PenStyle.NoPen)
    if knob == "spotify":
        painter.drawEllipse(center, rect.height() * 0.34, rect.height() * 0.34)
    elif knob == "circle":
        # Kept inside the element so the handle is never clipped at either end.
        knob_radius = full_rect.height() / 2
        center.setX(max(knob_radius, min(full_rect.width() - knob_radius, center.x())))
        painter.drawEllipse(center, knob_radius, knob_radius)
    elif knob == "bar":
        knob_width = max(2.0, full_rect.height() * 0.3)
        left = max(0.0, min(full_rect.width() - knob_width, center.x() - knob_width / 2))
        painter.drawRoundedRect(
            QRectF(left, full_rect.top(), knob_width, full_rect.height()),
            knob_width / 2, knob_width / 2,
        )
    paint_selection_guide(item, painter, full_rect)
