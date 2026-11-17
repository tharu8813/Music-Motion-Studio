"""Inspector editor for SourceType.TIME, split out of
SourceInspector._update_legacy_source_specific_fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.inspector.editors.base import editing, show_fields
from app.models.source import Source

if TYPE_CHECKING:
    from app.inspector.source_inspector import SourceInspector

_OWN_FIELD_KEYS = (
    "text", "font_size", "font_weight", "font_family",
    "text_stroke_color", "text_stroke_width", "text_alignment", "time_format",
)


def edit(inspector: "SourceInspector", source: Source) -> None:
    with editing(inspector, source):
        show_fields(inspector, _OWN_FIELD_KEYS)
