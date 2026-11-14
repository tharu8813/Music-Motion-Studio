"""Inspector section for SourceType.TRACK_LIST.

``edit`` decides which fields a track list shows (split out of
SourceInspector._update_legacy_source_specific_fields). ``TrackListSection``
owns the list's own fields -- the ``Source.track_list_*`` properties --
end to end (see FieldSection).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QPushButton, QSpinBox, QWidget

from app.inspector.editors.base import FieldSection, editing, show_fields
from app.models.source import Source
from app.widgets.visual_preset_combo import VisualPresetComboBox

if TYPE_CHECKING:
    from app.inspector.source_inspector import SourceInspector


class TrackListSection(FieldSection):
    """The track list's Inspector fields (``Source.track_list_*``)."""

    FAMILY = ("트랙 목록", "track list")

    ROWS = (
        ("track_list_count", "tl_layout"),
        ("track_list_style", "tl_layout"),
        ("track_list_window", "tl_layout"),
        ("track_list_marker", "tl_layout"),
        ("track_list_show_number", "tl_content"),
        ("track_list_show_artist", "tl_content"),
        ("track_list_show_album", "tl_content"),
        ("track_list_show_dividers", "tl_content"),
        ("track_list_row_spacing", "tl_spacing"),
        ("track_list_item_padding", "tl_spacing"),
        ("track_list_current_scale", "tl_spacing"),
        ("track_list_inactive_opacity", "tl_spacing"),
        ("track_list_current_color", "tl_colors"),
        ("track_list_inactive_color", "tl_colors"),
        ("track_list_current_background", "tl_colors"),
    )

    LABELS = {
        "track_list_count": ("표시 곡 개수", "Visible tracks"),
        "track_list_style": ("목록 스타일", "List style"),
        "track_list_window": ("표시 범위", "Track range"),
        "track_list_show_number": ("트랙 번호", "Show track numbers"),
        "track_list_show_artist": ("아티스트", "Show artist"),
        "track_list_show_album": ("앨범", "Show album"),
        "track_list_marker": ("현재 곡 표시", "Current-track marker"),
        "track_list_row_spacing": ("행 간격", "Row spacing"),
        "track_list_item_padding": ("목록 안쪽 여백", "List padding"),
        "track_list_current_color": ("현재 곡 글자색", "Current text color"),
        "track_list_inactive_color": ("다른 곡 글자색", "Other-track color"),
        "track_list_current_background": ("현재 곡 강조색", "Current highlight"),
        "track_list_inactive_opacity": ("다른 곡 투명도", "Other-track opacity"),
        "track_list_current_scale": ("현재 곡 크기", "Current-track scale"),
        "track_list_show_dividers": ("행 구분선", "Row dividers"),
    }

    SECTION_TITLES = {
        "tl_layout": ("레이아웃", "Layout"),
        "tl_content": ("표시 항목", "Shown details"),
        "tl_spacing": ("간격 · 크기", "Spacing & size"),
        "tl_colors": ("색상", "Colors"),
    }

    # "style" is shared with other families and stays in SourceInspector's table.
    HELP = {
        "count": ("화면에 동시에 표시할 항목 수입니다.", "Number of entries displayed at the same time."),
        "window": ("현재 항목을 기준으로 이전 항목과 다음 항목을 어떤 비율로 보여줄지 정합니다.", "Chooses how previous and upcoming entries are arranged around the current item."),
        "show_number": ("각 곡 앞에 플레이리스트 순번을 표시합니다.", "Shows the playlist position before each track."),
        "show_artist": ("트랙 목록에 아티스트 이름을 함께 표시합니다.", "Shows artist names in the track list."),
        "show_album": ("트랙 목록에 앨범 이름을 함께 표시합니다.", "Shows album names in the track list."),
        "marker": ("재생 중인 곡을 알아보기 위한 아이콘 또는 강조선을 선택합니다.", "Chooses an icon or accent that identifies the current track."),
        "row_spacing": ("목록의 각 행 사이 간격입니다. 값이 크면 목록이 더 넓게 펼쳐집니다.", "Space between rows. Larger values spread the list farther apart."),
        "item_padding": ("각 목록 항목의 글자와 배경 사이 안쪽 여백입니다.", "Inner space between each row's text and background."),
        "current_color": ("현재 재생 중인 곡의 글자색입니다.", "Text color for the currently playing track."),
        "inactive_color": ("현재 곡을 제외한 다른 곡의 글자색입니다.", "Text color for tracks other than the current one."),
        "current_background": ("현재 곡 뒤에 표시할 강조 배경색입니다.", "Highlight background behind the current track."),
        "inactive_opacity": ("현재 곡이 아닌 항목을 흐리게 표시하는 정도입니다.", "Controls how faint non-current entries appear."),
        "current_scale": ("현재 곡만 확대하거나 축소하는 배율입니다. 1은 원래 크기입니다.", "Scale applied only to the current track. A value of 1 is original size."),
        "show_dividers": ("목록의 각 행 사이에 구분선을 표시합니다.", "Shows divider lines between list rows."),
    }

    def __init__(
        self, spin: Callable[[float, float, float], QDoubleSpinBox],
        color_button: Callable[[], QPushButton],
    ) -> None:
        count = QSpinBox()
        count.setRange(0, 15)  # 0 = automatic
        style = VisualPresetComboBox("track_list")
        for label, value in (
            ("Compact", "compact"), ("Cards", "cards"), ("Queue", "queue"),
            ("Minimal", "minimal"), ("Scroll / fade", "scroll"),
            ("Glass", "glass"), ("Pills", "pills"),
        ):
            style.addItem(label, value)
        window = QComboBox()
        for label, value in (
            ("Previous + current + next", "centered"),
            ("Current + upcoming", "upcoming"),
            ("History + current", "history"),
        ):
            window.addItem(label, value)
        show_number = QCheckBox()
        show_artist = QCheckBox()
        show_album = QCheckBox()
        marker = QComboBox()
        for label, value in (
            ("Play ▶", "play"), ("Dot ●", "dot"), ("Accent ▌", "line"), ("None", "none"),
        ):
            marker.addItem(label, value)
        row_spacing = spin(0, 40, 1)
        item_padding = spin(0, 40, 1)
        current_color = color_button()
        inactive_color = color_button()
        current_background = color_button()
        inactive_opacity = spin(0.05, 1.0, 0.05)
        current_scale = spin(0.8, 1.5, 0.05)
        show_dividers = QCheckBox()
        self.widgets: dict[str, QWidget] = {
            "track_list_count": count,
            "track_list_style": style,
            "track_list_window": window,
            "track_list_show_number": show_number,
            "track_list_show_artist": show_artist,
            "track_list_show_album": show_album,
            "track_list_marker": marker,
            "track_list_row_spacing": row_spacing,
            "track_list_item_padding": item_padding,
            "track_list_current_color": current_color,
            "track_list_inactive_color": inactive_color,
            "track_list_current_background": current_background,
            "track_list_inactive_opacity": inactive_opacity,
            "track_list_current_scale": current_scale,
            "track_list_show_dividers": show_dividers,
        }

    def retranslate(self, korean: bool) -> None:
        for key, labels in (
            ("track_list_style", ("컴팩트", "카드", "재생 대기열", "미니멀", "스크롤 / 페이드", "글래스", "필")
             if korean else ("Compact", "Cards", "Queue", "Minimal", "Scroll / fade", "Glass", "Pills")),
            ("track_list_window", ("이전 + 현재 + 다음", "현재 + 다음 곡", "이전 곡 + 현재")
             if korean else ("Previous + current + next", "Current + upcoming", "History + current")),
            ("track_list_marker", ("재생 ▶", "점 ●", "강조선 ▌", "표시 없음")
             if korean else ("Play ▶", "Dot ●", "Accent ▌", "None")),
        ):
            for index, label in enumerate(labels):
                self.widgets[key].setItemText(index, label)
        self.widgets["track_list_count"].setSpecialValueText("자동" if korean else "Auto")

    def notes(self, korean: bool) -> dict[str, str]:
        return {
            "track_list_count": (
                "자동을 선택하면 요소 높이와 글자 크기에 맞춰 표시 곡 수가 바뀝니다."
                if korean else
                "Auto changes the visible track count to fit the source height and font size."
            ),
        }


_OWN_FIELD_KEYS = (
    "text", "font_size", "font_weight", "font_family",
    "text_stroke_color", "text_stroke_width", "text_alignment", "text_overflow",
    *(key for key, _section in TrackListSection.ROWS),
)


def edit(inspector: "SourceInspector", source: Source) -> None:
    with editing(inspector, source):
        show_fields(inspector, _OWN_FIELD_KEYS)
