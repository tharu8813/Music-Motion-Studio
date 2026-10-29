"""Inspector section for SourceType.LYRICS.

``edit`` decides which fields a lyrics source shows (split out of
SourceInspector._update_legacy_source_specific_fields). ``LyricsSection``
owns the lyrics-only fields themselves -- the ``Source.subtitle_*``
properties -- end to end (see FieldSection).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QPushButton, QSpinBox, QWidget,
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
        ("subtitle_auto_shrink", "sub_layout"),
        ("subtitle_auto_wrap", "sub_layout"),
        ("subtitle_wrap_to_source", "sub_layout"),
        ("subtitle_wrap_width", "sub_layout"),
        ("subtitle_animation", "sub_transition"),
        ("subtitle_animation_duration", "sub_transition"),
        ("subtitle_context_lines", "sub_layout"),
        ("subtitle_next_lines", "sub_layout"),
        ("subtitle_line_spacing", "sub_layout"),
        ("subtitle_previous_opacity", "sub_prev"),
        ("subtitle_previous_blur", "sub_prev"),
        ("subtitle_current_scale", "sub_current"),
        ("subtitle_karaoke_enabled", "sub_karaoke"),
        ("subtitle_karaoke_lines", "sub_karaoke"),
        ("subtitle_accent_enabled", "sub_karaoke"),
        ("subtitle_accent_color", "sub_karaoke"),
        ("subtitle_karaoke_pending_mode", "sub_karaoke"),
        ("subtitle_karaoke_pending_color", "sub_karaoke"),
        ("subtitle_karaoke_pending_brightness", "sub_karaoke"),
        ("subtitle_karaoke_offset", "sub_karaoke"),
        ("subtitle_timing_offset", None),
    )

    LABELS = {
        "subtitle_auto_shrink": ("글꼴 크기 자동 감소", "Automatically reduce font size"),
        "subtitle_auto_wrap": ("자동 줄바꿈", "Automatic word wrap"),
        "subtitle_wrap_to_source": ("줄바꿈 기준", "Wrap width basis"),
        "subtitle_wrap_width": ("줄바꿈 너비", "Wrap width"),
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
        "subtitle_karaoke_enabled": ("실시간 가사", "Real-time lyrics"),
        "subtitle_karaoke_lines": ("강조할 가사 줄", "Highlighted lyric lines"),
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
        "sub_karaoke": ("실시간 가사 · 색상과 타이밍", "Real-time lyrics · Colors and timing"),
    }

    HELP = {
        "auto_shrink": ("요소 너비보다 긴 줄의 글꼴 크기를 자동으로 줄입니다. 줄바꿈과 함께 사용할 수 있습니다.", "Shrink lines that exceed the element width. Can be combined with word wrapping."),
        "auto_wrap": ("단어 사이에서 줄을 나눕니다. 한 단어를 글자별로 나누지 않으며, 원래 줄바꿈과 가사 시간은 유지합니다.", "Wrap at spaces without splitting words. Original line breaks and lyric timings are preserved."),
        "wrap_to_source": ("요소 너비를 사용하거나 별도의 줄바꿈 너비를 지정합니다. 가로 이동에서는 각 가사의 표시 영역 너비를 사용합니다.", "Use the element width or a custom wrap width. Horizontal flow uses each cue's available width."),
        "wrap_width": ("배율 적용 전 픽셀 단위 너비입니다. 요소 크기와 별도로 줄바꿈 위치를 정합니다.", "Width in pixels before element scaling, independent of the element size."),
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
        "karaoke_enabled": ("이 요소에 표시되는 모든 곡에 공통으로 적용합니다. 일반 가사는 처음부터 줄 전체를 표시하고 다음 가사 시간에 전환합니다. TTML만 파일에 기록된 단어·음절 시간으로 강조합니다. 음악 분석이나 시간 추정은 하지 않습니다.", "Applies to every track displayed by this element. Ordinary lyrics show complete lines from the start until the next cue. Only TTML uses the file's word and syllable timings. No audio analysis or timing estimation is used."),
        "karaoke_lines": ("비워 두면 모든 줄을 강조합니다. 원문·번역처럼 한 가사에 여러 줄이 있으면 강조할 줄 번호(1~12)를 쉼표로 입력하세요. 예: 1, 3. 이 요소의 모든 곡에 같은 줄 기준을 적용합니다.", "Leave empty to highlight every line. For multi-line cues such as original text and translation, enter line numbers (1–12) separated by commas, for example 1, 3. The same line positions apply to every track in this element."),
        "karaoke_offset": ("단어 강조만 보정합니다. 양수는 더 일찍, 음수는 더 늦게 채웁니다. 예: 0.20은 0.2초 앞당깁니다. 가사 줄 전환과 간주 시간은 유지합니다.", "Adjusts word highlighting only. Positive fills earlier; negative fills later. For example, 0.20 advances it by 0.2 seconds. Cue transitions and interlude timing stay unchanged."),
        "accent_color": ("실시간 가사에서 현재 가사에 사용할 색입니다. TTML에서는 도달한 단어에 사용합니다.", "Color for current lyrics, or reached words in TTML real-time lyrics."),
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
        wrap_basis = QComboBox()
        wrap_basis.addItem("Element width", True)
        wrap_basis.addItem("Custom width", False)
        lines = QLineEdit()
        lines.setMaxLength(64)
        lines.setValidator(QRegularExpressionValidator(QRegularExpression(
            r"\s*(?:(?:[1-9]|1[0-2])(?:\s*,\s*(?:[1-9]|1[0-2]))*)?\s*"
        ), lines))
        self._lines_edited = False
        lines.textEdited.connect(self._mark_lines_edited)
        self.widgets: dict[str, QWidget] = {
            "subtitle_auto_shrink": QCheckBox(),
            "subtitle_auto_wrap": QCheckBox(),
            "subtitle_wrap_to_source": wrap_basis,
            "subtitle_wrap_width": spin(16, 100_000, 10),
            "subtitle_animation": animation,
            "subtitle_animation_duration": animation_duration,
            "subtitle_context_lines": context_lines,
            "subtitle_next_lines": next_lines,
            "subtitle_line_spacing": spin(0, 120, 1),
            "subtitle_previous_opacity": spin(0.05, 0.9, 0.05),
            "subtitle_previous_blur": spin(0, 8, 0.5),
            "subtitle_current_scale": spin(1.0, 2.0, 0.02),
            "subtitle_karaoke_enabled": QCheckBox(),
            "subtitle_karaoke_lines": lines,
            "subtitle_accent_enabled": QCheckBox(),
            "subtitle_accent_color": color_button(),
            "subtitle_karaoke_pending_mode": pending_mode,
            "subtitle_karaoke_pending_color": color_button(),
            "subtitle_karaoke_pending_brightness": spin(0, 1, 0.05),
            "subtitle_karaoke_offset": spin(-5, 5, 0.01),
            "subtitle_timing_offset": spin(-5.0, 5.0, 0.01),
        }

    def _mark_lines_edited(self, _text: str) -> None:
        self._lines_edited = True

    def connect(self, update, **kwargs) -> None:
        def update_field(key, value):
            if key == "subtitle_karaoke_lines":
                # Mixed selections are blank too; merely leaving the field must
                # not replace their individual masks with "all lines".
                editor = self.widgets[key]
                if not self._lines_edited or not editor.hasAcceptableInput():
                    return
                self._lines_edited = False
                value = sorted({int(part.strip()) - 1 for part in value.split(",")}) if value.strip() else []
            update(key, value)
        super().connect(update_field, **kwargs)

    def displayed_value(self, source: Source, key: str) -> object:
        if key == "subtitle_karaoke_lines":
            return ", ".join(str(index + 1) for index in source.subtitle_karaoke_lines)
        return super().displayed_value(source, key)

    def fill(self, source: Source, *, set_color=None) -> None:
        self._lines_edited = False
        super().fill(source, set_color=set_color)
        self.widgets["subtitle_animation_duration"].setEnabled(source.subtitle_animation != "none")

    def hidden_when_off(self, source: Source) -> dict[str, bool]:
        """Previous-line styling only matters while previous lines are shown."""
        shows_previous = source.subtitle_context_lines != 0
        return {
            "subtitle_wrap_to_source": source.subtitle_auto_wrap,
            "subtitle_wrap_width": source.subtitle_auto_wrap and not source.subtitle_wrap_to_source,
            "subtitle_previous_opacity": shows_previous,
            "subtitle_previous_blur": shows_previous,
            "subtitle_accent_color": source.subtitle_accent_enabled or source.subtitle_karaoke_enabled,
            "subtitle_accent_enabled": not source.subtitle_karaoke_enabled,
            "subtitle_karaoke_lines": source.subtitle_karaoke_enabled,
            "subtitle_karaoke_offset": source.subtitle_karaoke_enabled,
            "subtitle_karaoke_pending_mode": source.subtitle_karaoke_enabled,
            "subtitle_karaoke_pending_color": source.subtitle_karaoke_enabled and source.subtitle_karaoke_pending_mode == "custom",
            "subtitle_karaoke_pending_brightness": source.subtitle_karaoke_enabled and source.subtitle_karaoke_pending_mode == "dim",
        }

    def retranslate(self, korean: bool) -> None:
        for key in ("subtitle_auto_shrink", "subtitle_auto_wrap"):
            self.widgets[key].setText("사용" if korean else "Enabled")
        basis = self.widgets["subtitle_wrap_to_source"]
        basis.setItemText(0, "요소 너비" if korean else "Element width")
        basis.setItemText(1, "직접 지정" if korean else "Custom width")
        self.widgets["subtitle_wrap_width"].setSuffix(" px")
        self.widgets["subtitle_animation_duration"].setSuffix(" 초" if korean else " s")
        self.widgets["subtitle_accent_enabled"].setText("사용" if korean else "Enabled")
        self.widgets["subtitle_karaoke_enabled"].setText("사용" if korean else "Enabled")
        self.widgets["subtitle_karaoke_lines"].setPlaceholderText("모든 줄 (예: 1, 3)" if korean else "All lines (e.g. 1, 3)")
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
