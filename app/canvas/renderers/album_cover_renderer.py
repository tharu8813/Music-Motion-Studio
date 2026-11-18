"""Canvas renderer for SourceType.ALBUM_COVER, split out of SourceItem._paint_legacy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QColor, QPainter, QPen

from app.canvas.renderers.base import paint_background, paint_image_content, paint_selection_guide
from app.utils.album_frame_paint import paint_album_frame_back, paint_album_frame_front

if TYPE_CHECKING:
    from app.canvas.source_item import SourceItem


def render(
    item: "SourceItem", painter: QPainter, option: object, widget: object | None = None,
) -> None:
    rect, _fill, _pen = paint_background(item, painter)
    if not item._pixmap.isNull():
        paint_image_content(item, painter, rect, item.source.album_frame_style)
    else:
        frame = item.source.album_frame_style
        if frame in {"vinyl", "filmstrip"}:
            paint_album_frame_back(painter, rect, frame)
        else:
            painter.drawRoundedRect(rect, item.source.border_radius, item.source.border_radius)
        painter.setPen(QPen(QColor("#FFFFFF"), 2))
        painter.drawEllipse(rect.center(), rect.width() * 0.16, rect.width() * 0.16)
        if frame in {"vinyl", "filmstrip"}:
            paint_album_frame_front(painter, rect, frame)
    paint_selection_guide(item, painter, rect)
