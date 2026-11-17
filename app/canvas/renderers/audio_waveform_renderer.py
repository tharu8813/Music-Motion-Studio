"""Audio waveform Canvas renderer with the same styles as export and preset tiles."""

from __future__ import annotations

from math import sin
from typing import TYPE_CHECKING

from PySide6.QtGui import QColor, QPainter

from app.canvas.renderers.base import paint_background, paint_selection_guide
from app.utils.waveform_paint import paint_waveform

if TYPE_CHECKING:
    from app.canvas.source_item import SourceItem


def render(
    item: "SourceItem", painter: QPainter, option: object, widget: object | None = None,
) -> None:
    rect, _fill, _pen = paint_background(item, painter)
    points = max(24, min(128, item.source.visualizer_bars))
    # An editor-only example. Playback/export supply real waveform amplitudes.
    levels = [0.22 + 0.64 * abs(sin(index * 0.38 + 0.8)) for index in range(points)]
    paint_waveform(
        painter, rect, levels,
        style=item.source.waveform_style,
        color=QColor(item.source.fill_color),
        line_width=item.source.visualizer_line_width,
    )
    paint_selection_guide(item, painter, rect)
