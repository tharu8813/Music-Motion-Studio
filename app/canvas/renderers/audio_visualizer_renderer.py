"""Canvas renderer for SourceType.AUDIO_VISUALIZER, split out of
SourceItem._paint_legacy."""

from __future__ import annotations

from math import sin
from typing import TYPE_CHECKING

from PySide6.QtGui import QColor, QPainter

from app.canvas.renderers.base import paint_background, paint_selection_guide
from app.utils.visualizer_paint import draw_visualizer_content

if TYPE_CHECKING:
    from app.canvas.source_item import SourceItem


def render(
    item: "SourceItem", painter: QPainter, option: object, widget: object | None = None,
) -> None:
    rect, _fill, _pen = paint_background(item, painter)
    bar_count = max(4, min(96, item.source.visualizer_bars))
    gap = max(1.0, rect.width() * 0.012 / bar_count)
    bar_width = max(1.0, (rect.width() - gap * (bar_count - 1)) / bar_count)
    minimum = max(0.0, min(0.5, item.source.visualizer_min_level))
    maximum = max(minimum, min(1.0, item.source.visualizer_max_level))
    curve = max(0.25, min(3.0, item.source.visualizer_curve))
    # The editor has no playback signal, so show a representative design
    # sample.  Real preview/export frames use the audio analysis and are
    # completely flat at silence when Minimum level is zero.
    levels = [
        minimum + (maximum - minimum)
        * (0.16 + 0.76 * abs(sin(index * 0.61 + 0.8))) ** curve
        for index in range(bar_count)
    ]
    # Share exact bar geometry with preview and export. The caller's
    # paint_background already applies horizontal/vertical mirroring once.
    draw_visualizer_content(
        painter, rect, levels,
        style=item.source.visualizer_style,
        color=QColor(item.source.fill_color),
        line_width=item.source.visualizer_line_width,
        inner_radius=item.source.visualizer_inner_radius,
    )
    paint_selection_guide(item, painter, rect)
