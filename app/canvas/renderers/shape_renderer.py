"""Canvas renderer for SourceType.SHAPE, split out of SourceItem._paint_legacy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QColor, QPainter, QPen

from app.canvas.renderers.base import paint_background, paint_selection_guide
from app.utils.mask_shapes import mask_path

if TYPE_CHECKING:
    from app.canvas.source_item import SourceItem


def render(
    item: "SourceItem", painter: QPainter, option: object, widget: object | None = None,
) -> None:
    rect, _fill, _pen = paint_background(item, painter)
    if item.source.shape_kind == "circle":
        painter.drawEllipse(rect)
    elif item.source.shape_kind == "line":
        painter.setPen(QPen(QColor(item.source.fill_color), max(1.0, item.source.height)))
        painter.drawLine(rect.left(), rect.center().y(), rect.right(), rect.center().y())
    elif item.source.shape_kind != "rectangle":
        painter.drawPath(mask_path(rect, item.source.shape_kind))
    else:
        painter.drawRoundedRect(rect, item.source.border_radius, item.source.border_radius)
    paint_selection_guide(item, painter, rect)
