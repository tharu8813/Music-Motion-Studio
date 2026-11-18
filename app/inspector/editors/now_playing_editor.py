"""Inspector section for SourceType.NOW_PLAYING.

``edit`` decides which fields a now-playing card shows (split out of
SourceInspector._update_legacy_source_specific_fields). ``NowPlayingSection``
owns the card's own fields -- the ``Source.now_playing_*`` properties --
end to end (see FieldSection).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QLineEdit, QWidget

from app.inspector.editors.base import FieldSection, editing, show_fields
from app.models.source import Source
from app.widgets.visual_preset_combo import VisualPresetComboBox

if TYPE_CHECKING:
    from app.inspector.source_inspector import SourceInspector


class NowPlayingSection(FieldSection):
    """The now-playing card's Inspector fields (``Source.now_playing_*``)."""

    FAMILY = ("현재 재생 카드", "now-playing card")

    ROWS = (
        ("now_playing_style", None),
        ("now_playing_label", None),
        ("now_playing_align", None),
        ("now_playing_duration", None),
        ("now_playing_exit", "np_exit"),
        ("now_playing_exit_duration", "np_exit"),
    )

    LABELS = {
        "now_playing_style": ("카드 스타일", "Card style"),
        "now_playing_label": ("상단 문구", "Heading text"),
        "now_playing_align": ("카드 정렬", "Card alignment"),
        "now_playing_duration": ("표시 시간", "Display seconds"),
        "now_playing_exit": ("사라짐 효과", "Exit effect"),
        "now_playing_exit_duration": ("사라짐 시간", "Exit duration"),
    }

    SECTION_TITLES = {"np_exit": ("종료 효과", "Exit")}

    # "style" is shared with other families and stays in SourceInspector's table.
    HELP = {
        "label": ("제목 위에 작게 표시되는 문구입니다. 비우면 표시하지 않고 제목을 위로 올립니다.", "Small heading above the title. Leave it empty to hide it and move the title up."),
        "align": ("카드 안의 문구를 왼쪽, 가운데 또는 오른쪽으로 정렬합니다.", "Aligns the card's text left, centre or right."),
        "duration": ("카드 또는 전환이 화면에 유지되는 시간입니다.", "How long the card or transition remains on screen."),
        "exit": ("카드가 사라질 때 사용할 전환 효과입니다.", "Transition used when the card disappears."),
        "exit_duration": ("사라짐 효과가 완료되는 데 걸리는 시간입니다.", "Time required for the exit effect to complete."),
    }

    # The field key predates the model attribute's name; language packs use the key.
    ATTRIBUTES = {"now_playing_exit": "now_playing_exit_animation"}

    def __init__(self, spin: Callable[[float, float, float], QDoubleSpinBox]) -> None:
        style = VisualPresetComboBox("now_playing")
        for label, value in (
            ("Card", "card"), ("Minimal", "minimal"), ("Glass", "glass"),
            ("Outline", "outline"), ("Accent", "accent"), ("Gradient", "gradient"),
        ):
            style.addItem(label, value)
        exit_combo = VisualPresetComboBox("exit")
        for label, value in (("Fade", "fade"), ("Slide up", "slide_up"), ("Slide down", "slide_down"), ("Zoom", "zoom")):
            exit_combo.addItem(label, value)
        align = QComboBox()
        for label, value in (("Left", "left"), ("Center", "center"), ("Right", "right")):
            align.addItem(label, value)
        self.widgets: dict[str, QWidget] = {
            "now_playing_style": style,
            "now_playing_label": QLineEdit(),
            "now_playing_align": align,
            "now_playing_duration": spin(0.5, 15, 0.25),
            "now_playing_exit": exit_combo,
            "now_playing_exit_duration": spin(0.05, 3.0, 0.05),
        }

    def retranslate(self, korean: bool) -> None:
        for key, labels in (
            ("now_playing_style",
             ("카드", "미니멀", "글래스", "윤곽선", "강조선", "그라데이션") if korean
             else ("Card", "Minimal", "Glass", "Outline", "Accent", "Gradient")),
            ("now_playing_exit", ("페이드", "위쪽 슬라이드", "아래쪽 슬라이드", "줌")
             if korean else ("Fade", "Slide up", "Slide down", "Zoom")),
            ("now_playing_align", ("왼쪽", "가운데", "오른쪽") if korean else ("Left", "Center", "Right")),
        ):
            for index, label in enumerate(labels):
                self.widgets[key].setItemText(index, label)


_OWN_FIELD_KEYS = (
    "text", "font_size", "font_weight", "font_family",
    "text_stroke_color", "text_stroke_width", "text_alignment",
    *(key for key, _section in NowPlayingSection.ROWS),
)


def edit(inspector: "SourceInspector", source: Source) -> None:
    with editing(inspector, source):
        show_fields(inspector, _OWN_FIELD_KEYS)
