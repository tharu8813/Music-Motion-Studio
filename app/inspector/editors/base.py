"""Shared field-visibility helpers for per-type Inspector editors.

SourceInspector._update_legacy_source_specific_fields sets one field's
visibility per line in a single flat pass gated by source_type -- unlike
the Canvas paint dispatch, it has no per-type branch to lift out directly.
A per-type editor here instead: hides every type-conditional field first
(hide_type_specific_fields), shows only the fields its own type owns,
applies the handful of fields every type applies the same way
(apply_shared_fields), then runs the same closing steps the legacy
function did (finish).

Every editor follows that same frame, so it is written once as the
``editing`` context manager; an editor only fills in the middle step::

    def edit(inspector, source):
        with editing(inspector, source):
            show_fields(inspector, ("shape",))
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QLineEdit, QPushButton, QWidget,
)

from app.models.source import (
    LOOP_MOTIONS, MASK_SHAPES, MUSIC_REACTIVE_EFFECTS, MUSIC_REACTIVE_BANDS,
    MUSIC_REACTIVE_CURVES, TEXT_CASES, TEXT_SOURCE_TYPES, Source, SourceType,
)

if TYPE_CHECKING:
    from app.inspector.source_inspector import SourceInspector


class FieldSection:
    """One source type's own Inspector fields, keyed by the Source attribute each edits.

    Because a field key is also its model attribute, rows, signal wiring,
    multi-selection bindings and filling all follow from ``widgets`` and the
    widget kind: QComboBox (item data), QCheckBox, QPushButton (colour picker)
    or a spin box. SourceInspector calls each hook exactly where its phase used
    to handle these fields inline, so form order, tab order and tooltip
    precedence are unchanged. Subclasses build ``widgets`` and fill the tables.
    """

    FAMILY: ClassVar[tuple[str, str]]
    """(Korean, English) help-text family name for the section's key prefix."""
    ROWS: ClassVar[tuple[tuple[str, str | None], ...]]
    """(field key, collapsible sub-section or None), in form order."""
    LABELS: ClassVar[dict[str, tuple[str, str]]]
    SECTION_TITLES: ClassVar[dict[str, tuple[str, str]]]
    HELP: ClassVar[dict[str, tuple[str, str]]]
    """Help details keyed by the field key without the family prefix."""
    ATTRIBUTES: ClassVar[dict[str, str]] = {}
    """Model attribute for a field key that is not itself the attribute name."""

    widgets: dict[str, QWidget]

    def attribute(self, key: str) -> str:
        return self.ATTRIBUTES.get(key, key)

    @staticmethod
    def kind(widget: QWidget) -> str:
        if isinstance(widget, QComboBox):
            return "combo"
        if isinstance(widget, QCheckBox):
            return "check"
        if isinstance(widget, QPushButton):
            return "color"
        if isinstance(widget, QLineEdit):
            return "line"
        return "spin"

    def add_rows(
        self, add_row: Callable[..., None], form: QFormLayout, keys: Collection[str] | None = None,
    ) -> None:
        """Add the rows (in ROWS order); ``keys`` adds just those, for sections
        whose rows the form places in more than one spot."""
        for key, section in self.ROWS:
            if keys is None or key in keys:
                add_row(form, key, self.widgets[key], section=section)

    def connect(
        self, update: Callable[[str, object], None], *,
        choose_color: Callable[[str, QPushButton], None] | None = None,
        apply_mixed_checkbox: Callable[[str, bool], None] | None = None,
    ) -> None:
        for field, widget in self.widgets.items():
            key = self.attribute(field)
            kind = self.kind(widget)
            if kind == "combo":
                widget.currentIndexChanged.connect(
                    lambda _index, key=key, combo=widget: update(key, combo.currentData())
                )
            elif kind == "check":
                widget.toggled.connect(lambda value, key=key: update(key, value))
                # A click on a mixed multi-selection value must still apply it.
                if apply_mixed_checkbox is not None:
                    widget.clicked.connect(
                        lambda checked=False, key=key: apply_mixed_checkbox(key, checked)
                    )
            elif kind == "color":
                widget.clicked.connect(
                    lambda _checked=False, key=key, button=widget: choose_color(key, button)
                )
            elif kind == "line":
                widget.editingFinished.connect(
                    lambda key=key, edit=widget: update(key, edit.text())
                )
            else:
                widget.valueChanged.connect(lambda value, key=key: update(key, value))

    def bindings(self) -> dict[str, tuple[str, QWidget, str]]:
        """Multi-selection bindings: model path -> (path, control, kind)."""
        return {
            self.attribute(key): (self.attribute(key), widget, self.kind(widget))
            for key, widget in self.widgets.items()
        }

    def displayed_value(self, source: Source, key: str) -> object:
        """The value a field shows for ``source``; override to present legacy data."""
        return getattr(source, self.attribute(key))

    def fill(
        self, source: Source, *,
        set_color: Callable[[QPushButton, str], None] | None = None,
    ) -> None:
        for key, widget in self.widgets.items():
            value = self.displayed_value(source, key)
            kind = self.kind(widget)
            if kind == "combo":
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif kind == "check":
                widget.setChecked(value)
            elif kind == "color":
                set_color(widget, value)
            elif kind == "line":
                widget.setText(str(value))
            else:
                widget.setValue(value)

    def hidden_when_off(self, source: Source) -> dict[str, bool]:
        """Fields to hide while the toggle/value they depend on is off."""
        return {}

    def retranslate(self, korean: bool) -> None:
        """Localize texts inside the widgets (item texts, special values)."""

    def notes(self, korean: bool) -> dict[str, str]:
        """Field-specific guidance merged into each field's hover help."""
        return {}


class TypographySection(FieldSection):
    """Typography and glyph effects shared by every text-bearing source.

    Its rows live in three tabs, so the Inspector adds them with ``keys``:
    TEXT_KEYS to the text tab, and the glyph shadow / gradient toggles next to
    the shadow and gradient settings they redirect.
    """

    FAMILY = ("글자", "typography")
    TEXT_KEYS = ("text_letter_spacing", "text_line_gap", "text_italic", "text_case")
    ROWS = (
        ("text_letter_spacing", None),
        ("text_line_gap", None),
        ("text_italic", None),
        ("text_case", None),
        ("text_shadow_glyph", None),
        ("text_gradient", None),
    )
    LABELS = {
        "text_letter_spacing": ("자간", "Letter spacing"),
        "text_line_gap": ("줄 간격", "Line spacing"),
        "text_italic": ("기울임꼴", "Italic"),
        "text_case": ("대소문자", "Letter case"),
        "text_shadow_glyph": ("글자 모양 그림자", "Glyph-shaped shadow"),
        "text_gradient": ("글자에 그라데이션", "Gradient on text"),
    }
    SECTION_TITLES: ClassVar[dict[str, tuple[str, str]]] = {}
    HELP = {
        "letter_spacing": ("글자 사이 간격(px)입니다. 음수는 좁히고 양수는 넓힙니다.", "Extra space between letters in pixels. Negative values tighten, positive values widen."),
        "line_gap": ("여러 줄 텍스트의 줄 사이에 더할 간격(px)입니다. 음수는 줄을 좁힙니다.", "Extra space in pixels between the lines of multi-line text. Negative values tighten them."),
        "italic": ("글자를 기울임꼴로 표시합니다.", "Shows the text in italics."),
        "case": ("원문은 그대로 두고 대문자, 소문자, 단어 첫 글자 대문자 또는 작은 대문자로 표시합니다.", "Displays the text in upper, lower, title case or small caps without changing it."),
        "shadow_glyph": ("그림자를 요소 상자가 아닌 글자 모양을 따라 드리웁니다. 오프셋을 0으로 두면 글로우처럼 보입니다.", "Casts the shadow from the letters instead of the source box. With zero offset it reads as a glow."),
        "gradient": ("채우기 그라데이션을 배경 대신 글자에 칠합니다.", "Paints the fill gradient into the letters instead of the background."),
    }
    def __init__(self, spin: Callable[[float, float, float], QWidget]) -> None:
        case = QComboBox()
        for value in TEXT_CASES:
            case.addItem(value, value)
        self.widgets: dict[str, QWidget] = {
            "text_letter_spacing": spin(-10, 60, 0.5),
            "text_line_gap": spin(-40, 200, 1),
            "text_italic": QCheckBox(),
            "text_case": case,
            "text_shadow_glyph": QCheckBox(),
            "text_gradient": QCheckBox(),
        }

    def hidden_when_off(self, source: Source) -> dict[str, bool]:
        return {
            "text_shadow_glyph": source.shadow.enabled,
            "text_gradient": source.gradient.enabled,
        }

    def retranslate(self, korean: bool) -> None:
        labels = (
            ("그대로", "대문자", "소문자", "단어 첫 글자 대문자", "작은 대문자")
            if korean else
            ("As typed", "UPPERCASE", "lowercase", "Title Case", "Small caps")
        )
        for index, label in enumerate(labels):
            self.widgets["text_case"].setItemText(index, label)
        for key, text in (
            ("text_italic", "사용" if korean else "Enabled"),
            ("text_shadow_glyph", "사용" if korean else "Enabled"),
            ("text_gradient", "사용" if korean else "Enabled"),
        ):
            self.widgets[key].setText(text)


class MotionSection(FieldSection):
    """Looping idle motion and the clip mask, shared by every source.

    LOOP_KEYS go to the animation tab and ``mask_shape`` to the appearance
    tab. Help is keyed by the full field name (no family prefix).
    """

    FAMILY = ("모션", "motion")
    LOOP_KEYS = ("loop_motion", "loop_motion_period", "loop_motion_amount")
    REACTIVE_KEYS = ("music_reactive_enabled", "music_reactive_effect", "music_reactive_band",
                     "music_reactive_strength", "music_reactive_attack", "music_reactive_release",
                     "music_reactive_sensitivity", "music_reactive_threshold",
                     "music_reactive_curve", "music_reactive_offset")
    ROWS = (
        ("loop_motion", None),
        ("loop_motion_period", None),
        ("loop_motion_amount", None),
        ("mask_shape", None),
        ("music_reactive_enabled", "music_reaction"),
        ("music_reactive_effect", "music_reaction"),
        ("music_reactive_band", "music_reaction"),
        ("music_reactive_strength", "music_reaction"),
        ("music_reactive_attack", "music_response"),
        ("music_reactive_release", "music_response"),
        ("music_reactive_sensitivity", "music_response"),
        ("music_reactive_threshold", "music_response"),
        ("music_reactive_curve", "music_response"),
        ("music_reactive_offset", "music_response"),
    )
    LABELS = {
        "loop_motion": ("반복 모션", "Loop motion"),
        "loop_motion_period": ("반복 주기 (초)", "Loop period (s)"),
        "loop_motion_amount": ("모션 세기", "Motion strength"),
        "mask_shape": ("마스크 모양", "Mask shape"),
        "music_reactive_enabled": ("음악에 맞춰 반응", "React to music"),
        "music_reactive_effect": ("반응 애니메이션", "Reaction effect"),
        "music_reactive_strength": ("반응 강도", "Reaction strength"),
        "music_reactive_band": ("반응 음역", "Frequency range"),
        "music_reactive_attack": ("커지는 시간 (초)", "Attack time (s)"),
        "music_reactive_release": ("돌아오는 시간 (초)", "Release time (s)"),
        "music_reactive_sensitivity": ("반응 감도", "Sensitivity"),
        "music_reactive_threshold": ("최소 반응 기준", "Threshold"),
        "music_reactive_curve": ("반응 곡선", "Response curve"),
        "music_reactive_offset": ("반응 시간 보정 (초)", "Reaction offset (s)"),
    }
    SECTION_TITLES = {"music_reaction": ("음악 반응", "Music reaction"),
                      "music_response": ("반응 조절", "Response tuning")}
    HELP = {
        "loop_motion": ("요소가 화면에 있는 동안 계속 반복되는 움직임입니다. 등장·퇴장 애니메이션과 함께 적용됩니다.", "Movement repeated for as long as the source is on screen, on top of its entrance and exit."),
        "loop_motion_period": ("움직임 한 번에 걸리는 시간입니다. 회전은 이 시간마다 한 바퀴 돕니다.", "Seconds per cycle. Spin turns once per period."),
        "loop_motion_amount": ("움직임의 크기입니다. 0이면 멈추고, 1이 기본입니다.", "Size of the movement. 0 stops it; 1 is the default."),
        "mask_shape": ("요소 전체를 원·별·하트 같은 모양으로 잘라 냅니다. 그림자도 이 모양을 따릅니다.", "Cuts the whole source to a shape such as a circle, star or heart. Its shadow follows the shape."),
        "music_reactive_enabled": ("실제 음악에 맞춰 움직입니다. 자막은 현재 가사 묶음에만 적용되며 이전·다음 가사와 인트로는 영향을 받지 않습니다. 전체 재생 미리보기와 내보내기에 적용됩니다.", "React to music. Subtitles affect only the current cue, leaving previous/next cues and intro indicators unchanged. Applies to Full Preview and export."),
        "music_reactive_effect": ("확대·복귀, 위로 튀기, 가로 늘리기, 기울이기 중 선택합니다. 선택한 음역에 반응하며 등장·퇴장 및 반복 모션과 함께 적용됩니다.", "Choose scale pulse, upward bounce, horizontal stretch or tilt. Follows the selected frequency range and combines with entrance, exit and loop motion."),
        "music_reactive_strength": ("0은 반응 없음, 1은 최대 반응입니다. 베이스 확대에서는 0.25가 최대 25% 확대를 뜻합니다. 음악이 없거나 무음이면 기본 크기를 유지합니다.", "Zero disables the response; one is strongest. For bass scale, 0.25 adds up to 25% size. Missing or silent audio leaves text at rest."),
        "music_reactive_band": ("저음·중음·고음·전체 중 반응할 음역을 선택합니다. 저음은 킥과 베이스에 잘 반응합니다.", "Choose bass, midrange, treble or the full spectrum. Bass follows kicks and bass instruments."),
        "music_reactive_attack": ("음악이 강해질 때 반응이 목표 크기의 약 90%에 도달하는 시간입니다. 작을수록 빠르게 커지고 0이면 즉시 반응합니다. 확대 외 효과에도 같은 속도가 적용됩니다.", "Time to reach about 90% of a stronger signal. Smaller is faster; zero reacts instantly. Also controls other effects."),
        "music_reactive_release": ("음악이 약해지면 반응이 약 90% 줄어드는 시간입니다. 클수록 천천히 원래 모습으로 돌아오며 0이면 즉시 돌아옵니다.", "Time to decay by about 90% after the signal fades. Larger returns more slowly; zero returns instantly."),
        "music_reactive_sensitivity": ("분석된 음악 신호를 증폭합니다. 1이 기본이며 약한 음악에는 높이고 너무 자주 반응하면 낮추세요. 최대 움직임은 반응 강도로 정합니다.", "Amplifies the analyzed signal. One is normal; raise for quiet passages or lower for fewer reactions. Strength sets maximum movement."),
        "music_reactive_threshold": ("이 값보다 약한 신호는 무시합니다. 0은 모든 신호에 반응하며 높일수록 강한 비트에만 반응합니다.", "Ignores signals below this value. Zero accepts every signal; higher values isolate stronger beats."),
        "music_reactive_curve": ("부드럽게는 약한 소리도 살리고, 선형은 신호에 비례하며, 강한 비트 중심은 작은 반응을 줄입니다.", "Soft boosts subtle sounds; linear follows signal strength; punchy suppresses smaller reactions."),
        "music_reactive_offset": ("양수는 음악 반응을 늦추고 음수는 앞당깁니다. 가사 시간 보정과 별개로 적용됩니다.", "Positive delays the reaction; negative advances it. Independent of lyric timing offset."),
    }
    LOOP_LABELS = {
        "none": ("없음", "None"), "float": ("둥실 떠다니기", "Float"),
        "breathe": ("숨쉬기", "Breathe"), "pulse": ("맥박", "Pulse"),
        "sway": ("흔들림", "Sway"), "spin": ("회전", "Spin"),
        "drift": ("떠돌기", "Drift"), "wobble": ("출렁임", "Wobble"),
    }
    REACTIVE_LABELS = {
        "bass_scale": ("확대·복귀", "Scale pulse"),
        "bass_bounce": ("위로 튀기", "Upward bounce"),
        "bass_stretch": ("가로 늘리기", "Horizontal stretch"),
        "bass_tilt": ("기울이기", "Tilt"),
    }
    BAND_LABELS = {"bass": ("저음 · 베이스", "Bass"), "mid": ("중음", "Midrange"),
                   "treble": ("고음", "Treble"), "full": ("전체 음역", "Full spectrum")}
    CURVE_LABELS = {"soft": ("부드럽게", "Soft"), "linear": ("선형", "Linear"),
                    "punchy": ("강한 비트 중심", "Punchy")}
    MASK_LABELS = {
        "none": ("없음", "None"), "circle": ("원", "Circle"), "pill": ("알약", "Pill"),
        "arch": ("아치", "Arch"), "diamond": ("다이아몬드", "Diamond"),
        "triangle": ("삼각형", "Triangle"), "hexagon": ("육각형", "Hexagon"),
        "star": ("별", "Star"), "heart": ("하트", "Heart"),
    }

    def __init__(self, spin: Callable[[float, float, float], QWidget]) -> None:
        loop = QComboBox()
        for value in LOOP_MOTIONS:
            loop.addItem(value, value)
        mask = QComboBox()
        for value in MASK_SHAPES:
            mask.addItem(value, value)
        reactive = QComboBox()
        for value in MUSIC_REACTIVE_EFFECTS:
            reactive.addItem(value, value)
        band, curve = QComboBox(), QComboBox()
        for value in MUSIC_REACTIVE_BANDS:
            band.addItem(value, value)
        for value in MUSIC_REACTIVE_CURVES:
            curve.addItem(value, value)
        self.widgets: dict[str, QWidget] = {
            "loop_motion": loop,
            "loop_motion_period": spin(0.2, 60.0, 0.25),
            "loop_motion_amount": spin(0.0, 5.0, 0.1),
            "mask_shape": mask,
            "music_reactive_enabled": QCheckBox(),
            "music_reactive_effect": reactive,
            "music_reactive_strength": spin(0.0, 1.0, 0.05),
            "music_reactive_band": band,
            "music_reactive_attack": spin(0.0, 3.0, 0.01),
            "music_reactive_release": spin(0.0, 5.0, 0.05),
            "music_reactive_sensitivity": spin(0.0, 4.0, 0.1),
            "music_reactive_threshold": spin(0.0, 0.95, 0.05),
            "music_reactive_curve": curve,
            "music_reactive_offset": spin(-2.0, 2.0, 0.01),
        }

    def hidden_when_off(self, source: Source) -> dict[str, bool]:
        moving = source.loop_motion != "none"
        return {"loop_motion_period": moving, "loop_motion_amount": moving,
                **{key: source.music_reactive_enabled for key in self.REACTIVE_KEYS[1:]}}

    def retranslate(self, korean: bool) -> None:
        for key, labels in (("loop_motion", self.LOOP_LABELS), ("mask_shape", self.MASK_LABELS),
                            ("music_reactive_effect", self.REACTIVE_LABELS),
                            ("music_reactive_band", self.BAND_LABELS),
                            ("music_reactive_curve", self.CURVE_LABELS)):
            combo = self.widgets[key]
            for index in range(combo.count()):
                combo.setItemText(index, labels[combo.itemData(index)][0 if korean else 1])


def shows_mask(source: Source) -> bool:
    """Video is composited by FFmpeg outside the Canvas, so it cannot be masked."""
    return source.source_type is not SourceType.VIDEO

# Every field key _update_legacy_source_specific_fields toggles purely by
# source_type. Excludes shadow_* (every source shows it, see
# apply_shared_fields) and the toggle-dependent rows
# _hide_inactive_dependent_fields already owns (gradient_start/end,
# outline_color, animation_in/out_duration) -- those are unaffected by
# which type is selected.
TYPE_SPECIFIC_FIELD_KEYS: tuple[str, ...] = (
    "text", "font_size", "font_weight", "font_family",
    "text_stroke_color", "text_stroke_width", "text_alignment", "text_overflow",
    *(key for key, _section in TypographySection.ROWS),
    *MotionSection.REACTIVE_KEYS,
    "file", "image_fit", "blur", "brightness", "contrast",
    "shape", "video_settings",
    "progress_style", "progress_value", "progress_track_color", "progress_mode",
    "progress_knob",
    "visualizer_style", "visualizer_bars", "visualizer_line_width",
    "visualizer_sensitivity", "visualizer_reactivity", "visualizer_noise_gate",
    "visualizer_min_level", "visualizer_max_level", "visualizer_attack",
    "visualizer_release", "visualizer_smoothing", "visualizer_curve",
    "visualizer_inner_radius", "visualizer_center_cover",
    "background_mode", "background_ambient", "background_ambient_blur",
    "background_ambient_motion", "background_bass_reactive",
    "background_bass_strength", "background_track_transition",
    "background_track_transition_seconds",
    "album_frame",
    "track_list_count", "track_list_style", "track_list_window",
    "track_list_show_number", "track_list_show_artist", "track_list_show_album",
    "track_list_marker", "track_list_row_spacing", "track_list_item_padding",
    "track_list_current_color", "track_list_inactive_color",
    "track_list_current_background", "track_list_inactive_opacity",
    "track_list_current_scale", "track_list_show_dividers",
    "now_playing_style", "now_playing_duration", "now_playing_exit",
    "now_playing_exit_duration", "now_playing_label", "now_playing_align",
    "subtitle_animation", "subtitle_animation_duration", "subtitle_context_lines",
    "subtitle_next_lines", "subtitle_line_spacing", "subtitle_previous_opacity",
    "subtitle_previous_blur", "subtitle_timing_offset", "subtitle_current_scale",
    "subtitle_accent_enabled", "subtitle_accent_color", "subtitle_line_styles",
    "subtitle_karaoke_enabled", "subtitle_karaoke_pending_mode",
    "subtitle_karaoke_offset", "subtitle_karaoke_lines",
    "subtitle_karaoke_pending_color", "subtitle_karaoke_pending_brightness",
    "waveform_style",
    "level_meter_mode", "level_meter_style", "level_meter_orientation",
    "level_meter_sensitivity", "level_meter_attack", "level_meter_release",
    "level_meter_min_level", "level_meter_max_level", "level_meter_segments",
    "level_meter_gap", "level_meter_show_peak", "level_meter_peak_hold",
    "level_meter_peak_decay", "level_meter_track_color", "level_meter_low_color",
    "level_meter_mid_color", "level_meter_high_color",
    "particle_style", "particle_density", "particle_speed", "particle_min_size",
    "particle_max_size", "particle_opacity", "particle_direction",
    "particle_drift", "particle_twinkle", "particle_glow",
    "particle_secondary_color", "particle_seed",
)

# Fields visible whenever a source is selected at all, regardless of type.
_ALWAYS_VISIBLE_FIELD_KEYS: tuple[str, ...] = (
    "shadow", "shadow_color", "shadow_opacity", "shadow_blur", "shadow_x", "shadow_y",
)


def show_fields(inspector: "SourceInspector", keys: Iterable[str], visible: bool = True) -> None:
    """Set the same visibility on every field in ``keys``."""
    for key in keys:
        inspector._set_field_visible(key, visible)


def hide_type_specific_fields(inspector: "SourceInspector") -> None:
    """Hide every field a per-type editor might show, before it shows its own."""
    show_fields(inspector, TYPE_SPECIFIC_FIELD_KEYS, False)


def apply_shared_fields(inspector: "SourceInspector", source: Source) -> None:
    """Apply the fields every type applies the same way.

    text_color depends on a cross-type helper rather than one type owning
    it, and the shadow group is visible for any selected source.
    """
    inspector._set_field_visible("text_color", inspector._uses_primary_text_color(source))
    show_fields(
        inspector, (key for key, _section in TypographySection.ROWS),
        source.source_type in TEXT_SOURCE_TYPES,
    )
    show_fields(inspector, _ALWAYS_VISIBLE_FIELD_KEYS)
    show_fields(inspector, MotionSection.LOOP_KEYS)
    inspector._set_field_visible("mask_shape", shows_mask(source))


def apply_image_backed_fields(inspector: "SourceInspector", source: Source, *, show_file: bool) -> None:
    """Apply the fields every image-backed type applies the same way
    (image_fit/blur/brightness/contrast), plus file (whose own visibility
    condition differs per type, e.g. BACKGROUND gates it on background_mode)."""
    inspector._set_field_visible("file", show_file)
    show_fields(inspector, ("image_fit", "blur", "brightness", "contrast"))


def finish(inspector: "SourceInspector", source: Source) -> None:
    """Run the same closing steps _update_legacy_source_specific_fields did."""
    inspector._hide_inactive_dependent_fields(source)
    inspector._refresh_property_tabs([source])


@contextmanager
def editing(inspector: "SourceInspector", source: Source) -> Iterator[None]:
    """Frame one per-type edit: hide every type-specific field on entry, then
    apply the shared fields and run ``finish`` on exit.

    The body only shows the fields its own type owns. Like the flat legacy
    pass it replaces, an exception in the body skips the closing steps.
    """
    hide_type_specific_fields(inspector)
    yield
    apply_shared_fields(inspector, source)
    finish(inspector, source)
