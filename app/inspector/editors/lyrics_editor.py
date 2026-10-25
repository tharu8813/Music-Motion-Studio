"""Inspector section for SourceType.LYRICS.

``edit`` decides which fields a lyrics source shows (split out of
SourceInspector._update_legacy_source_specific_fields). ``LyricsSection``
owns the lyrics-only fields themselves -- the ``Source.subtitle_*``
properties -- end to end (see FieldSection).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QPushButton, QSpinBox, QWidget,
)

from app.inspector.editors.base import FieldSection, MotionSection, editing, show_fields
from app.models.source import Source
from app.animation.lyrics import LYRIC_EFFECT_LABELS

if TYPE_CHECKING:
    from app.inspector.source_inspector import SourceInspector


class LyricsSection(FieldSection):
    """The lyrics-only Inspector fields (``Source.subtitle_*``)."""

    FAMILY = ("가사", "lyrics")
    TRANSITION_KEYS = ("subtitle_animation", "subtitle_animation_duration")
    ADVANCED_BASIC_FIELDS = {
        "motion": TRANSITION_KEYS,
        "layout": ("subtitle_context_lines", "subtitle_next_lines", "subtitle_line_spacing", "text_alignment"),
        "styles": ("subtitle_previous_opacity", "subtitle_previous_blur", "subtitle_current_scale"),
        "intro": (),
    }

    ROWS = (
        ("subtitle_animation", "sub_transition"),
        ("subtitle_animation_duration", "sub_transition"),
        ("subtitle_context_lines", "sub_layout"),
        ("subtitle_next_lines", "sub_layout"),
        ("subtitle_line_spacing", "sub_layout"),
        ("subtitle_previous_opacity", "sub_prev"),
        ("subtitle_previous_blur", "sub_prev"),
        ("subtitle_current_scale", "sub_current"),
        ("subtitle_karaoke_enabled", "sub_karaoke"),
        ("subtitle_accent_enabled", "sub_karaoke"),
        ("subtitle_accent_color", "sub_karaoke"),
        ("subtitle_karaoke_pending_mode", "sub_karaoke"),
        ("subtitle_karaoke_pending_color", "sub_karaoke"),
        ("subtitle_karaoke_pending_brightness", "sub_karaoke"),
        ("subtitle_karaoke_offset", "sub_karaoke"),
        ("subtitle_timing_offset", None),
    )

    LABELS = {
        "subtitle_animation": ("전환 효과", "Transition effect"),
        "subtitle_animation_duration": ("전환 시간", "Transition duration"),
        "subtitle_context_lines": ("이전 가사 줄", "Previous lyric lines"),
        "subtitle_next_lines": ("다음 가사 줄", "Next lyric lines"),
        "subtitle_line_spacing": ("가사 줄 간격", "Lyric line spacing"),
        "subtitle_previous_opacity": ("이전 가사 불투명도", "Previous lyric opacity"),
        "subtitle_previous_blur": ("이전 가사 블러", "Previous lyric blur"),
        "subtitle_timing_offset": ("전체 가사 시간 보정 (초)", "All lyric timing offset (s)"),
        "subtitle_current_scale": ("현재 줄 크기", "Current line size"),
        "subtitle_accent_enabled": ("현재 줄 강조 색", "Current line accent"),
        "subtitle_karaoke_enabled": ("단어별 노래방 표시 (베타)", "Word karaoke highlight (Beta)"),
        "subtitle_karaoke_offset": ("단어 강조 보정 (초)", "Word highlight offset (s)"),
        "subtitle_accent_color": ("도달한 가사 색 / 강조 색", "Reached lyric / accent color"),
        "subtitle_karaoke_pending_mode": ("부르기 전 표시", "Pending lyric appearance"),
        "subtitle_karaoke_pending_color": ("부르기 전 색", "Pending lyric color"),
        "subtitle_karaoke_pending_brightness": ("부르기 전 밝기", "Pending brightness"),
        "subtitle_line_styles": ("줄별 스타일", "Per-line styles"),
    }

    SECTION_TITLES = {
        "sub_transition": ("자막 전환 · 기본", "Subtitle transition · Basic"),
        "sub_layout": ("줄 배치 · 기본", "Line layout · Basic"),
        "sub_prev": ("이전 줄 · 기본", "Previous lines · Basic"),
        "sub_current": ("현재 줄 · 기본", "Current line · Basic"),
        "sub_karaoke": ("노래방 · 색상과 타이밍", "Karaoke · Colors and timing"),
    }

    HELP = {
        "animation": ("재생 중 가사·자막 큐가 바뀔 때 적용할 효과입니다. 미리보기와 내보내기에 동일하게 적용됩니다.", "Effect used when a lyric or subtitle cue changes during playback. Applies to both preview and export."),
        "animation_duration": ("자막 전환 효과가 재생되는 시간입니다. 효과를 없음으로 선택하면 즉시 전환됩니다.", "Duration of each subtitle transition. None switches cues immediately."),
        "context_lines": ("현재 가사와 함께 표시할 이전 가사 수입니다. 위치는 이동 방향을 따릅니다.", "Previous cues shown alongside the current cue. Their position follows the flow direction."),
        "next_lines": ("미리 표시할 다음 가사 수입니다. 위치는 이동 방향을 따릅니다.", "Upcoming cues shown in advance. Their position follows the flow direction."),
        "line_spacing": ("가사 줄 사이 간격입니다. 가로 이동에서는 가사 영역 사이의 여백에도 적용됩니다.", "Spacing between lyric lines; also controls the gap between horizontal cue slots."),
        "previous_opacity": ("지나간 가사 줄을 얼마나 흐리게 표시할지 정합니다.", "Controls how faint previous lyric lines appear."),
        "previous_blur": ("지나간 가사 줄에 적용할 흐림 정도입니다.", "Blur applied to previous lyric lines."),
        "timing_offset": ("모든 곡의 가사를 초 단위로 앞당기거나 늦추는 공통 보정입니다. 곡별 보정값과 합산됩니다.", "Global timing adjustment for lyrics on every track. It is added to each track's individual offset."),
        "current_scale": ("앞뒤 가사 줄에 비해 현재 줄을 얼마나 크게 표시할지 정합니다. 1이면 모든 줄이 같은 크기입니다.", "How much larger the current line is than the surrounding lines. 1 keeps every line the same size."),
        "accent_enabled": ("현재 줄만 별도의 강조 색으로 표시합니다. 줄이 바뀔 때 색이 부드럽게 넘어갑니다.", "Shows the current line in its own accent color, blending smoothly as lines change."),
        "karaoke_enabled": ("베타 버전 기능입니다. 재생 시간에 맞춰 단어별로 강조 색을 채웁니다. 기존 단어 시간은 유지하고 빠진 단어는 주변 단어와 줄 시간으로 추정합니다. 오디오 분석 없이도 사용할 수 있습니다.", "Beta feature. Fills words during playback. Preserves existing word times and estimates missing words from surrounding anchors and cue duration. Works without audio analysis."),
        "karaoke_offset": ("단어 강조만 보정합니다. 양수는 더 일찍, 음수는 더 늦게 채웁니다. 예: 0.20은 0.2초 앞당깁니다. 가사 줄 전환과 간주 시간은 유지합니다.", "Adjusts word highlighting only. Positive fills earlier; negative fills later. For example, 0.20 advances it by 0.2 seconds. Cue transitions and interlude timing stay unchanged."),
        "accent_color": ("노래방 표시에서 도달한 단어에 사용할 색입니다. 노래방 표시를 끄면 현재 줄 강조 색으로 사용합니다.", "Color for reached words in karaoke mode, or the current-line accent when karaoke is off."),
        "karaoke_pending_mode": ("아직 부르지 않은 가사를 별도 색상으로 표시하거나, 도달한 가사 색의 밝기만 낮춥니다.", "Use a separate color for pending lyrics, or dim the reached lyric color."),
        "karaoke_pending_color": ("별도 색상 방식에서 아직 도달하지 않은 단어와 다음 가사에 사용할 색입니다.", "Separate color for unreached words and upcoming lyrics."),
        "karaoke_pending_brightness": ("도달한 가사 색 대비 밝기입니다. 0은 검정, 1은 도달한 가사와 같은 밝기입니다. 불투명도는 유지합니다.", "Brightness relative to the reached lyric color: 0 is black, 1 is unchanged. Opacity is preserved."),
        "line_styles": ("다중행 가사 큐의 2번째 줄부터 줄 위치별 색상과 글꼴을 설정합니다.", "Sets color and typography from the second physical line inside each multi-line lyric cue."),
    }

    def __init__(
        self, spin: Callable[[float, float, float], QDoubleSpinBox],
        color_button: Callable[[], QPushButton],
    ) -> None:
        animation = QComboBox()
        for value, labels in LYRIC_EFFECT_LABELS.items():
            animation.addItem(labels[1], value)
        animation_duration = spin(0.05, 3.0, 0.05)
        animation.currentIndexChanged.connect(
            lambda _index: animation_duration.setEnabled(animation.currentData() != "none")
        )
        context_lines = QSpinBox()
        context_lines.setRange(-1, 6)  # -1 = automatic
        next_lines = QSpinBox()
        next_lines.setRange(-1, 6)
        pending_mode = QComboBox()
        pending_mode.addItem("Separate color", "custom")
        pending_mode.addItem("Dim reached color", "dim")
        self.widgets: dict[str, QWidget] = {
            "subtitle_animation": animation,
            "subtitle_animation_duration": animation_duration,
            "subtitle_context_lines": context_lines,
            "subtitle_next_lines": next_lines,
            "subtitle_line_spacing": spin(0, 120, 1),
            "subtitle_previous_opacity": spin(0.05, 0.9, 0.05),
            "subtitle_previous_blur": spin(0, 8, 0.5),
            "subtitle_current_scale": spin(1.0, 2.0, 0.02),
            "subtitle_karaoke_enabled": QCheckBox(),
            "subtitle_accent_enabled": QCheckBox(),
            "subtitle_accent_color": color_button(),
            "subtitle_karaoke_pending_mode": pending_mode,
            "subtitle_karaoke_pending_color": color_button(),
            "subtitle_karaoke_pending_brightness": spin(0, 1, 0.05),
            "subtitle_karaoke_offset": spin(-5, 5, 0.01),
            "subtitle_timing_offset": spin(-5.0, 5.0, 0.01),
        }

    def fill(self, source: Source, *, set_color=None) -> None:
        super().fill(source, set_color=set_color)
        self.widgets["subtitle_animation_duration"].setEnabled(source.subtitle_animation != "none")

    def hidden_when_off(self, source: Source) -> dict[str, bool]:
        """Previous-line styling only matters while previous lines are shown."""
        shows_previous = source.subtitle_context_lines != 0
        return {
            "subtitle_previous_opacity": shows_previous,
            "subtitle_previous_blur": shows_previous,
            "subtitle_accent_color": source.subtitle_accent_enabled or source.subtitle_karaoke_enabled,
            "subtitle_accent_enabled": not source.subtitle_karaoke_enabled,
            "subtitle_karaoke_offset": source.subtitle_karaoke_enabled,
            "subtitle_karaoke_pending_mode": source.subtitle_karaoke_enabled,
            "subtitle_karaoke_pending_color": source.subtitle_karaoke_enabled and source.subtitle_karaoke_pending_mode == "custom",
            "subtitle_karaoke_pending_brightness": source.subtitle_karaoke_enabled and source.subtitle_karaoke_pending_mode == "dim",
        }

    def retranslate(self, korean: bool) -> None:
        self.widgets["subtitle_animation_duration"].setSuffix(" 초" if korean else " s")
        self.widgets["subtitle_accent_enabled"].setText("사용" if korean else "Enabled")
        self.widgets["subtitle_karaoke_enabled"].setText("사용" if korean else "Enabled")
        mode = self.widgets["subtitle_karaoke_pending_mode"]
        mode.setItemText(0, "별도 색상" if korean else "Separate color")
        mode.setItemText(1, "도달한 색의 밝기 낮추기" if korean else "Dim reached color")
        automatic = "자동" if korean else "Auto"
        self.widgets["subtitle_context_lines"].setSpecialValueText(automatic)
        self.widgets["subtitle_next_lines"].setSpecialValueText(automatic)
        animation = self.widgets["subtitle_animation"]
        for index in range(animation.count()):
            animation.setItemText(index, LYRIC_EFFECT_LABELS[animation.itemData(index)][0 if korean else 1])

    def notes(self, korean: bool) -> dict[str, str]:
        automatic = (
            "자동은 세로 이동에서 요소 높이와 글자 크기·줄 간격에 맞춰 표시합니다. 가로 이동에서는 앞뒤 한 가사씩 표시합니다."
            if korean else
            "Auto fits vertical context to the source height, font size and line spacing. Horizontal flow shows one cue on each side."
        )
        return {
            "subtitle_animation": (
                "글로우는 빛과 함께 부드럽게, 라이즈는 빠르게 미끄러져 이동합니다. "
                "페이드·슬라이드·줌·바운스도 선택할 수 있습니다. 순차 이동은 다음 가사에만 시차를 둡니다. "
                "설정창에서 방향·속도·줄 스타일을 조절하세요. 없음은 즉시 전환합니다."
                if korean else
                "Glow moves smoothly with a halo; Rise slides quickly. Fade, Slide, Zoom and Bounce add different motions. "
                "Staggered flow delays only upcoming cues. Customize direction, timing and role styles in the settings dialog. "
                "None switches immediately."
            ),
            "subtitle_context_lines": automatic,
            "subtitle_next_lines": automatic,
        }


_OWN_FIELD_KEYS = (
    "text", "font_size", "font_weight", "font_family",
    "text_stroke_color", "text_stroke_width", "text_alignment",
    "subtitle_line_styles",
    *MotionSection.REACTIVE_KEYS,
    *(key for key, _section in LyricsSection.ROWS),
)


def edit(inspector: "SourceInspector", source: Source) -> None:
    with editing(inspector, source):
        show_fields(inspector, _OWN_FIELD_KEYS)
