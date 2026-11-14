"""Inspector section for SourceType.AUDIO_VISUALIZER.

``edit`` decides which fields a visualizer shows (split out of
SourceInspector._update_legacy_source_specific_fields). ``VisualizerSection``
owns the visualizer's own fields -- the ``Source.visualizer_*`` properties --
end to end (see FieldSection).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox, QWidget

from app.inspector.editors.base import FieldSection, editing, show_fields
from app.models.source import Source
from app.widgets.visual_preset_combo import VisualPresetComboBox

if TYPE_CHECKING:
    from app.inspector.source_inspector import SourceInspector


class VisualizerSection(FieldSection):
    """The audio visualizer's Inspector fields (``Source.visualizer_*``)."""

    FAMILY = ("오디오 비주얼라이저", "audio visualizer")

    # The form adds these two rows right after the progress-bar style and the
    # rest after the progress fields; keeping both spots keeps the row order.
    EARLY_KEYS = ("visualizer_style", "visualizer_bars")
    LATE_KEYS = (
        "visualizer_line_width", "visualizer_inner_radius", "visualizer_sensitivity", "visualizer_reactivity",
        "visualizer_attack", "visualizer_release", "visualizer_smoothing", "visualizer_curve",
        "visualizer_noise_gate", "visualizer_min_level", "visualizer_max_level",
    )

    ROWS = (
        ("visualizer_style", "vz_display"),
        ("visualizer_bars", "vz_display"),
        ("visualizer_line_width", "vz_display"),
        ("visualizer_inner_radius", "vz_display"),
        ("visualizer_sensitivity", "vz_response"),
        ("visualizer_reactivity", "vz_response"),
        ("visualizer_attack", "vz_response"),
        ("visualizer_release", "vz_response"),
        ("visualizer_smoothing", "vz_response"),
        ("visualizer_curve", "vz_response"),
        ("visualizer_noise_gate", "vz_range"),
        ("visualizer_min_level", "vz_range"),
        ("visualizer_max_level", "vz_range"),
    )

    LABELS = {
        "visualizer_style": ("비주얼라이저 스타일", "Visualizer style"),
        "visualizer_bars": ("막대 / 점 개수", "Bars / dots"),
        "visualizer_line_width": ("선 두께", "Line width"),
        "visualizer_inner_radius": ("원 안쪽 크기", "Inner circle size"),
        "visualizer_sensitivity": ("비주얼라이저 감도", "Visualizer sensitivity"),
        "visualizer_reactivity": ("반응 속도", "Response speed"),
        "visualizer_noise_gate": ("노이즈 게이트", "Noise gate"),
        "visualizer_min_level": ("최소 높이", "Minimum level"),
        "visualizer_max_level": ("최대 높이", "Maximum level"),
        "visualizer_attack": ("상승 속도", "Attack speed"),
        "visualizer_release": ("하강 속도", "Release speed"),
        "visualizer_smoothing": ("밴드 평활화", "Band smoothing"),
        "visualizer_curve": ("다이내믹 커브", "Dynamic curve"),
    }

    SECTION_TITLES = {
        "vz_display": ("표시", "Display"),
        "vz_response": ("반응", "Response"),
        "vz_range": ("레벨 범위", "Level range"),
    }

    # Visualizer-only help. style/sensitivity/attack/release/min_level/max_level
    # are shared with the level meter and stay in SourceInspector's common table.
    HELP = {
        "bars": ("표시할 막대 또는 점의 개수입니다. 많을수록 세밀하지만 렌더링 부하가 늘어납니다.", "Number of bars or dots. More detail can increase rendering cost."),
        "line_width": ("선을 그리는 두께입니다. 값이 클수록 효과가 굵고 강하게 보입니다.", "Stroke width. Larger values make the effect heavier and stronger."),
        "inner_radius": ("원형 스타일에서 막대가 시작되는 원의 크기입니다. 가운데 앨범 커버가 들어갈 공간입니다.", "Radial style: size of the ring the bars grow from, leaving room for a centred cover."),
        "reactivity": ("오디오 변화에 따라 움직이는 민감도입니다. 높을수록 움직임이 빠르고 역동적입니다.", "Movement response to audio changes. Higher values feel faster and more dynamic."),
        "noise_gate": ("이 값보다 작은 입력은 무음으로 처리합니다. 0이면 게이트를 사용하지 않습니다.", "Treats input below this value as silence. Set to 0 to disable the gate."),
        "smoothing": ("인접한 주파수 구간의 높이 차이를 평균화해 움직임을 부드럽게 합니다.", "Averages neighboring frequency bands for smoother movement."),
        "curve": ("작은 소리와 큰 소리 중 어느 영역의 움직임을 더 강조할지 조정합니다.", "Balances emphasis between quiet detail and loud peaks."),
    }

    def __init__(self, spin: Callable[[float, float, float], QDoubleSpinBox]) -> None:
        style = VisualPresetComboBox("visualizer")
        for label, value in (
            ("Bars", "bars"), ("Wave", "wave"), ("Dots", "dots"),
            ("Line", "line"), ("Mirror", "mirror"), ("Spectrum", "spectrum"),
            ("LED bars", "led"), ("Center bars", "center"), ("Capsules", "capsule"),
            ("Arc", "arc"), ("Radial", "radial"),
        ):
            style.addItem(label, value)
        bars = QSpinBox()
        bars.setRange(4, 96)
        noise_gate = spin(0.0, 0.1, 0.001)
        noise_gate.setDecimals(3)
        self.widgets: dict[str, QWidget] = {
            "visualizer_style": style,
            "visualizer_bars": bars,
            "visualizer_line_width": spin(1, 30, 0.5),
            "visualizer_inner_radius": spin(0.1, 0.95, 0.05),
            "visualizer_sensitivity": spin(0.25, 3.0, 0.05),
            "visualizer_reactivity": spin(0.05, 0.8, 0.05),
            "visualizer_noise_gate": noise_gate,
            "visualizer_min_level": spin(0.0, 0.5, 0.01),
            "visualizer_max_level": spin(0.1, 1.0, 0.01),
            "visualizer_attack": spin(0.01, 1.0, 0.05),
            "visualizer_release": spin(0.01, 1.0, 0.05),
            "visualizer_smoothing": spin(0.0, 1.0, 0.05),
            "visualizer_curve": spin(0.25, 3.0, 0.05),
        }

    def hidden_when_off(self, source: Source) -> dict[str, bool]:
        radial = source.visualizer_style == "radial"
        return {"visualizer_inner_radius": radial, "visualizer_center_cover": radial}

    def notes(self, korean: bool) -> dict[str, str]:
        return {
            "visualizer_min_level": (
                "완전히 평평한 대기 파형을 원하면 0으로 설정하세요."
                if korean else "Set to 0 for a completely flat idle wave."
            ),
            "visualizer_curve": (
                "1보다 작으면 작은 소리를 강조하고, 1보다 크면 큰 소리를 강조합니다."
                if korean else "Below 1 emphasizes quiet detail; above 1 emphasizes strong peaks."
            ),
        }


_OWN_FIELD_KEYS = (
    *(key for key, _section in VisualizerSection.ROWS),
    # A SourceInspector button, not a model field (see _add_center_cover).
    "visualizer_center_cover",
)


def edit(inspector: "SourceInspector", source: Source) -> None:
    with editing(inspector, source):
        show_fields(inspector, _OWN_FIELD_KEYS)
