"""Canvas renderer for SourceType.VIDEO, split out of SourceItem._paint_legacy."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath

from app.canvas.renderers.base import paint_background, paint_image_content, paint_selection_guide

if TYPE_CHECKING:
    from app.canvas.source_item import SourceItem


def render(
    item: "SourceItem", painter: QPainter, option: object, widget: object | None = None,
) -> None:
    rect, _fill, _pen = paint_background(item, painter)
    frame = item._video_presented_frame
    if item._video_timeline_preview_active and not frame.isNull():
        paint_image_content(item, painter, rect, "rounded", image=frame)
    elif not item._pixmap.isNull():
        paint_image_content(item, painter, rect, "rounded")
    else:
        painter.save()
        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, item.source.border_radius, item.source.border_radius)
        painter.setClipPath(clip_path, Qt.ClipOperation.IntersectClip)
        painter.fillRect(rect, QColor("#10151D"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 210))
        size = min(rect.width(), rect.height()) * 0.22
        center = rect.center()
        play = QPainterPath()
        play.moveTo(center.x() - size * 0.35, center.y() - size * 0.55)
        play.lineTo(center.x() + size * 0.55, center.y())
        play.lineTo(center.x() - size * 0.35, center.y() + size * 0.55)
        play.closeSubpath()
        painter.drawPath(play)
        painter.restore()
        paths = item.source.video_paths
        filename = Path(paths[0]).name if paths else "No video selected"
        suffix = f"  +{len(paths) - 1}" if len(paths) > 1 else ""
        painter.setPen(QColor("#D8E1EE"))
        painter.drawText(
            rect.adjusted(10, 10, -10, -10),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
            filename + suffix,
        )
    paint_selection_guide(item, painter, rect)
