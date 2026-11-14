"""Inspector section for SourceType.PARTICLE_OVERLAY.

``edit`` decides which fields a particle overlay shows (split out of
SourceInspector._update_legacy_source_specific_fields). ``ParticleSection``
owns the overlay's own fields -- the ``Source.particle_*`` properties --
end to end (see FieldSection).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QPushButton, QSpinBox, QWidget

from app.inspector.editors.base import FieldSection, editing, show_fields
from app.models.source import Source
from app.widgets.visual_preset_combo import VisualPresetComboBox

if TYPE_CHECKING:
    from app.inspector.source_inspector import SourceInspector


class ParticleSection(FieldSection):
    """The particle overlay's Inspector fields (``Source.particle_*``)."""

    FAMILY = ("파티클 효과", "particle effect")

    ROWS = (
        ("particle_style", "pt_display"),
        ("particle_density", "pt_display"),
        ("particle_opacity", "pt_display"),
        ("particle_glow", "pt_display"),
        ("particle_speed", "pt_motion"),
        ("particle_direction", "pt_motion"),
        ("particle_drift", "pt_motion"),
        ("particle_twinkle", "pt_motion"),
        ("particle_min_size", "pt_shape"),
        ("particle_max_size", "pt_shape"),
        ("particle_secondary_color", "pt_shape"),
        ("particle_seed", "pt_shape"),
    )

    LABELS = {
        "particle_style": ("파티클 스타일", "Particle style"),
        "particle_density": ("파티클 밀도", "Particle density"),
        "particle_speed": ("파티클 속도", "Particle speed"),
        "particle_min_size": ("최소 크기", "Minimum size"),
        "particle_max_size": ("최대 크기", "Maximum size"),
        "particle_opacity": ("파티클 투명도", "Particle opacity"),
        "particle_direction": ("이동 방향", "Direction"),
        "particle_drift": ("흔들림", "Drift"),
        "particle_twinkle": ("반짝임", "Twinkle"),
        "particle_glow": ("글로우", "Glow"),
        "particle_secondary_color": ("보조 색상", "Secondary color"),
        "particle_seed": ("배치 시드", "Layout seed"),
    }

    SECTION_TITLES = {
        "pt_display": ("표시", "Display"),
        "pt_motion": ("움직임", "Motion"),
        "pt_shape": ("모양", "Shape"),
    }

    # "style" is shared with other families and stays in SourceInspector's table.
    HELP = {
        "density": ("화면에 동시에 나타나는 파티클 수입니다. 높은 값은 렌더링 부하를 늘릴 수 있습니다.", "Number of particles on screen. High values can increase rendering cost."),
        "speed": ("파티클이 이동하는 기본 속도입니다. 0이면 위치 변화가 멈춥니다.", "Base particle movement speed. Set to 0 to stop positional movement."),
        "min_size": ("무작위로 생성되는 파티클의 최소 크기입니다.", "Minimum size of randomly generated particles."),
        "max_size": ("무작위로 생성되는 파티클의 최대 크기입니다. 최소 크기보다 작게 설정되지 않습니다.", "Maximum random particle size; it cannot be smaller than the minimum."),
        "opacity": ("효과 전체가 보이는 정도입니다. 0은 완전히 투명하고 1은 완전히 보입니다.", "Overall effect opacity. 0 is fully transparent and 1 is fully visible."),
        "direction": ("파티클이 이동하는 기준 각도입니다. 0°는 오른쪽, 90°는 아래쪽입니다.", "Base movement angle. 0° is right and 90° is down."),
        "drift": ("기본 이동 방향에서 좌우로 흔들리는 무작위 움직임의 강도입니다.", "Strength of random side-to-side movement away from the base direction."),
        "twinkle": ("파티클 밝기가 시간에 따라 반짝이는 정도입니다.", "Amount of brightness variation over time."),
        "glow": ("파티클 주변의 빛 번짐 강도입니다. 높은 값은 렌더링 부하를 늘릴 수 있습니다.", "Glow around particles. High values can increase rendering cost."),
        "secondary_color": ("주 채우기 색과 섞어서 사용할 두 번째 파티클 색상입니다.", "Second particle color mixed with the primary fill color."),
        "seed": ("파티클의 초기 배치를 결정합니다. 값을 바꾸면 같은 설정으로 새 배치를 만듭니다.", "Determines initial particle placement. Change it for a new layout with the same settings."),
    }

    def __init__(
        self, spin: Callable[[float, float, float], QDoubleSpinBox],
        color_button: Callable[[], QPushButton],
    ) -> None:
        style = VisualPresetComboBox("particle")
        for label, value in (
            ("Dust", "dust"), ("Neon", "neon"), ("Noise", "noise"),
            ("Snow", "snow"), ("Stars", "stars"), ("Bokeh", "bokeh"),
            ("Confetti", "confetti"),
        ):
            style.addItem(label, value)
        density = QSpinBox()
        density.setRange(4, 500)
        speed = spin(0.0, 5.0, 0.1)
        min_size = spin(0.5, 40.0, 0.5)
        max_size = spin(0.5, 80.0, 0.5)
        opacity = spin(0.0, 1.0, 0.05)
        direction = spin(-180.0, 180.0, 5.0)
        drift = spin(0.0, 2.0, 0.05)
        twinkle = spin(0.0, 1.0, 0.05)
        glow = spin(0.0, 1.0, 0.05)
        secondary_color = color_button()
        seed = QSpinBox()
        seed.setRange(0, 999_999)
        self.widgets: dict[str, QWidget] = {
            "particle_style": style,
            "particle_density": density,
            "particle_speed": speed,
            "particle_min_size": min_size,
            "particle_max_size": max_size,
            "particle_opacity": opacity,
            "particle_direction": direction,
            "particle_drift": drift,
            "particle_twinkle": twinkle,
            "particle_glow": glow,
            "particle_secondary_color": secondary_color,
            "particle_seed": seed,
        }

    def retranslate(self, korean: bool) -> None:
        style = self.widgets["particle_style"]
        for index, label in enumerate(
            ("먼지", "네온", "노이즈", "눈", "별", "보케", "색종이") if korean
            else ("Dust", "Neon", "Noise", "Snow", "Stars", "Bokeh", "Confetti")
        ):
            style.setItemText(index, label)


_OWN_FIELD_KEYS = tuple(key for key, _section in ParticleSection.ROWS)


def edit(inspector: "SourceInspector", source: Source) -> None:
    with editing(inspector, source):
        show_fields(inspector, _OWN_FIELD_KEYS)
