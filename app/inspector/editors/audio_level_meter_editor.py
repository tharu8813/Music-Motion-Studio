"""Inspector section for SourceType.AUDIO_LEVEL_METER.

``edit`` decides which fields a level meter shows (split out of
SourceInspector._update_legacy_source_specific_fields). ``LevelMeterSection``
owns the meter's own fields -- the ``Source.level_meter_*`` properties --
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


class LevelMeterSection(FieldSection):
    """The audio level meter's Inspector fields (``Source.level_meter_*``)."""

    FAMILY = ("오디오 레벨 미터", "audio level meter")

    ROWS = (
        ("level_meter_mode", "lm_display"),
        ("level_meter_style", "lm_display"),
        ("level_meter_orientation", "lm_display"),
        ("level_meter_segments", "lm_display"),
        ("level_meter_gap", "lm_display"),
        ("level_meter_sensitivity", "lm_response"),
        ("level_meter_attack", "lm_response"),
        ("level_meter_release", "lm_response"),
        ("level_meter_min_level", "lm_range"),
        ("level_meter_max_level", "lm_range"),
        ("level_meter_show_peak", "lm_peak"),
        ("level_meter_peak_hold", "lm_peak"),
        ("level_meter_peak_decay", "lm_peak"),
        ("level_meter_track_color", "lm_colors"),
        ("level_meter_low_color", "lm_colors"),
        ("level_meter_mid_color", "lm_colors"),
        ("level_meter_high_color", "lm_colors"),
    )

    LABELS = {
        "level_meter_mode": ("레벨 미터", "Level meter"),
        "level_meter_style": ("미터 스타일", "Meter style"),
        "level_meter_orientation": ("방향", "Orientation"),
        "level_meter_sensitivity": ("입력 감도", "Input sensitivity"),
        "level_meter_attack": ("상승 속도", "Attack speed"),
        "level_meter_release": ("하강 속도", "Release speed"),
        "level_meter_min_level": ("최소 레벨", "Minimum level"),
        "level_meter_max_level": ("최대 레벨", "Maximum level"),
        "level_meter_segments": ("구간 수", "Segments"),
        "level_meter_gap": ("채널 간격", "Channel gap"),
        "level_meter_show_peak": ("피크 표시", "Show peak"),
        "level_meter_peak_hold": ("피크 유지 시간", "Peak hold"),
        "level_meter_peak_decay": ("피크 하강 속도", "Peak decay"),
        "level_meter_track_color": ("배경 트랙 색", "Track color"),
        "level_meter_low_color": ("낮은 레벨 색", "Low-level color"),
        "level_meter_mid_color": ("중간 레벨 색", "Mid-level color"),
        "level_meter_high_color": ("피크 색", "Peak color"),
    }

    SECTION_TITLES = {
        "lm_display": ("표시", "Display"),
        "lm_response": ("반응", "Response"),
        "lm_range": ("레벨 범위", "Level range"),
        "lm_peak": ("피크 표시", "Peak"),
        "lm_colors": ("색상", "Colors"),
    }

    # Meter-only help. style/sensitivity/attack/release/min_level/max_level are
    # shared with the visualizer and stay in SourceInspector's common table.
    HELP = {
        "mode": ("스테레오 채널을 나눠 표시하거나 하나의 모노 신호로 합칠지 선택합니다.", "Chooses separate stereo channels or one combined mono signal."),
        "orientation": ("미터가 세로로 상승할지 가로로 진행할지 선택합니다.", "Chooses whether the meter rises vertically or progresses horizontally."),
        "segments": ("분할형 미터에 표시할 칸의 개수입니다. 많을수록 변화가 세밀합니다.", "Number of blocks in a segmented meter. More blocks show finer changes."),
        "gap": ("스테레오 두 채널 사이의 간격입니다.", "Space between the two stereo channels."),
        "show_peak": ("최근 가장 큰 레벨 위치를 피크 표시선으로 유지합니다.", "Keeps a marker at the most recent maximum level."),
        "peak_hold": ("피크 표시선이 내려가기 전에 현재 위치를 유지하는 시간입니다.", "Time the peak marker stays in place before falling."),
        "peak_decay": ("유지 시간이 끝난 뒤 피크 표시선이 내려오는 속도입니다.", "Speed at which the peak marker falls after its hold time."),
        "track_color": ("신호가 없는 미터 배경 영역의 색상입니다.", "Color of the inactive meter track."),
        "low_color": ("낮은 음량 구간에 사용할 색상입니다.", "Color used for low audio levels."),
        "mid_color": ("중간 음량 구간에 사용할 색상입니다.", "Color used for medium audio levels."),
        "high_color": ("높은 음량과 피크 구간에 사용할 색상입니다.", "Color used for high levels and peaks."),
    }

    def __init__(
        self, spin: Callable[[float, float, float], QDoubleSpinBox],
        color_button: Callable[[], QPushButton],
    ) -> None:
        mode = QComboBox()
        for label, value in (("Stereo", "stereo"), ("Mono", "mono")):
            mode.addItem(label, value)
        style = VisualPresetComboBox("meter")
        for label, value in (
            ("Gradient", "gradient"), ("Solid", "solid"), ("LED", "led"), ("Segments", "segments"),
        ):
            style.addItem(label, value)
        orientation = QComboBox()
        for label, value in (("Vertical", "vertical"), ("Horizontal", "horizontal")):
            orientation.addItem(label, value)
        sensitivity = spin(0.25, 4.0, 0.05)
        attack = spin(0.01, 1.0, 0.05)
        release = spin(0.01, 1.0, 0.05)
        min_level = spin(0.0, 0.5, 0.01)
        max_level = spin(0.1, 1.0, 0.01)
        segments = QSpinBox()
        segments.setRange(3, 64)
        gap = spin(0.0, 30.0, 0.5)
        show_peak = QCheckBox()
        peak_hold = spin(0.0, 3.0, 0.05)
        peak_decay = spin(0.05, 3.0, 0.05)
        self.widgets: dict[str, QWidget] = {
            "level_meter_mode": mode,
            "level_meter_style": style,
            "level_meter_orientation": orientation,
            "level_meter_sensitivity": sensitivity,
            "level_meter_attack": attack,
            "level_meter_release": release,
            "level_meter_min_level": min_level,
            "level_meter_max_level": max_level,
            "level_meter_segments": segments,
            "level_meter_gap": gap,
            "level_meter_show_peak": show_peak,
            "level_meter_peak_hold": peak_hold,
            "level_meter_peak_decay": peak_decay,
            "level_meter_track_color": color_button(),
            "level_meter_low_color": color_button(),
            "level_meter_mid_color": color_button(),
            "level_meter_high_color": color_button(),
        }

    def displayed_value(self, source: Source, key: str) -> object:
        # Old projects stored the LED look as a *mode*; show it as stereo + LED style.
        if source.level_meter_mode == "led":
            if key == "level_meter_mode":
                return "stereo"
            if key == "level_meter_style":
                return "led"
        return super().displayed_value(source, key)

    def hidden_when_off(self, source: Source) -> dict[str, bool]:
        return {
            "level_meter_peak_hold": source.level_meter_show_peak,
            "level_meter_peak_decay": source.level_meter_show_peak,
        }

    def retranslate(self, korean: bool) -> None:
        self.widgets["level_meter_show_peak"].setText("사용" if korean else "Enabled")
        style = self.widgets["level_meter_style"]
        for index, label in enumerate(
            ("그라데이션", "단색", "LED", "분할 막대") if korean else ("Gradient", "Solid", "LED", "Segments")
        ):
            style.setItemText(index, label)
        mode = self.widgets["level_meter_mode"]
        mode.setItemText(0, "스테레오" if korean else "Stereo")
        mode.setItemText(1, "모노" if korean else "Mono")
        orientation = self.widgets["level_meter_orientation"]
        orientation.setItemText(0, "세로" if korean else "Vertical")
        orientation.setItemText(1, "가로" if korean else "Horizontal")


_OWN_FIELD_KEYS = tuple(key for key, _section in LevelMeterSection.ROWS)


def edit(inspector: "SourceInspector", source: Source) -> None:
    with editing(inspector, source):
        show_fields(inspector, _OWN_FIELD_KEYS)
