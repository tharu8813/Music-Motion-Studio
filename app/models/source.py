"""Data models for visual sources on the video canvas."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from enum import Enum
from math import isfinite
from re import fullmatch
from typing import Any
from uuid import uuid4

from app.models.source_components import LyricsComponent, VideoComponent


SUBTITLE_ADVANCED_GROUPS = {
    "motion": ("subtitle_animation", "subtitle_animation_duration", "subtitle_flow_direction",
               "subtitle_motion_easing", "subtitle_motion_distance", "subtitle_stagger",
               "subtitle_stagger_order", "subtitle_zoom_amount", "subtitle_glow_strength", "subtitle_glow_radius"),
    "layout": ("subtitle_context_lines", "subtitle_next_lines", "subtitle_line_spacing", "subtitle_anchor", "text_alignment"),
    "styles": ("subtitle_role_styles", "subtitle_previous_distance_fade", "subtitle_next_distance_fade"),
    "intro": ("subtitle_intro_enabled", "subtitle_intro_midtrack", "subtitle_intro_style", "subtitle_intro_exit",
              "subtitle_intro_gap", "subtitle_intro_period", "subtitle_intro_scale"),
}


class SourceType(str, Enum):
    """Kinds of visual source supported by the editor."""

    IMAGE = "image"
    VIDEO = "video"
    TEXT = "text"
    SHAPE = "shape"
    PROGRESS_BAR = "progress_bar"
    TIME = "time"
    ALBUM_COVER = "album_cover"
    LOGO = "logo"
    WATERMARK = "watermark"
    BACKGROUND = "background"
    AUDIO_VISUALIZER = "audio_visualizer"
    LYRICS = "lyrics"
    TRACK_LIST = "track_list"
    NOW_PLAYING = "now_playing"
    AUDIO_WAVEFORM = "audio_waveform"
    AUDIO_LEVEL_METER = "audio_level_meter"
    PARTICLE_OVERLAY = "particle_overlay"


TEXT_SOURCE_TYPES = frozenset({
    SourceType.TEXT, SourceType.TIME, SourceType.LYRICS,
    SourceType.TRACK_LIST, SourceType.NOW_PLAYING,
})


@dataclass(slots=True)
class Shadow:
    """Drop-shadow appearance settings."""

    enabled: bool = False
    color: str = "#000000"
    blur_radius: float = 12.0
    offset_x: float = 3.0
    offset_y: float = 3.0
    opacity: float = 0.35
    personal_color_enabled: bool = False
    personal_color_brightness: float = 0.0
    personal_color_saturation: float = 0.0
    personal_color_hue_shift: float = 0.0
    personal_color_strength: float = 1.0


SUBTITLE_ANIMATIONS = ("glow", "rise", "crossfade", "slide", "zoom", "cascade", "bounce", "none")
TEXT_CASES = ("none", "upper", "lower", "capitalize", "small_caps")
PROGRESS_KNOBS = ("auto", "none", "circle", "bar")
LOOP_MOTIONS = ("none", "float", "breathe", "pulse", "sway", "spin", "drift", "wobble",
                "orbit", "shake", "nod", "blink")
SHAPE_KINDS = ("rectangle", "circle", "triangle", "diamond", "hexagon", "star", "heart", "arch", "pill")
MUSIC_REACTIVE_EFFECTS = ("bass_scale", "bass_bounce", "bass_stretch", "bass_tilt")
MUSIC_REACTIVE_BANDS = ("bass", "mid", "treble", "full")
MUSIC_REACTIVE_CURVES = ("soft", "linear", "punchy")
MASK_SHAPES = (
    "none", "circle", "pill", "arch", "diamond", "triangle", "hexagon", "star", "heart",
)

# Projects saved before the lyric transitions were reduced to two distinct
# styles used these identifiers. Map each onto its closest survivor.
_LEGACY_SUBTITLE_ANIMATIONS = {
    "apple_music": "glow",
    "blur_reveal": "glow",
    "fade": "glow",
    "spotify": "rise",
    "scroll_up": "rise",
    "scroll_down": "rise",
    "slide_up": "rise",
    "pop": "rise",
}


@dataclass(slots=True)
class Gradient:
    """Simple two-stop linear gradient definition."""

    enabled: bool = False
    start_color: str = "#7C3AED"
    end_color: str = "#06B6D4"
    angle: float = 0.0


@dataclass(slots=True)
class Source:
    """Serializable source state shared by the canvas and inspector."""

    source_type: SourceType
    name: str
    x: float = 0.0
    y: float = 0.0
    width: float = 240.0
    height: float = 100.0
    rotation: float = 0.0
    scale: float = 1.0
    opacity: float = 1.0
    border_radius: float = 0.0
    # Decorations apply to every source, independently of type-specific outlines.
    flip_horizontal: bool = False
    flip_vertical: bool = False
    frame_width: float = 0.0
    frame_color: str = "#FFFFFF"
    frame_inset: float = 0.0
    shadow: Shadow = field(default_factory=Shadow)
    blur: float = 0.0
    outline_color: str = "#FFFFFF"
    outline_width: float = 0.0
    gradient: Gradient = field(default_factory=Gradient)
    z_index: int = 0
    visible: bool = True
    locked: bool = False
    fill_color: str = "#7C3AED"
    personal_color_enabled: bool = False
    personal_color_brightness: float = 0.0
    personal_color_saturation: float = 0.0
    personal_color_hue_shift: float = 0.0
    personal_color_strength: float = 1.0
    text: str = "Text"
    shape_kind: str = "rectangle"
    progress_style: str = "rounded"
    time_format: str = "HH:mm"
    font_family: str = "Segoe UI"
    font_path: str = ""
    font_size: float = 24.0
    font_weight: int = 600
    text_alignment: str = "center"
    text_overflow: str = "wrap"
    subtitle_auto_shrink: bool = True
    subtitle_auto_wrap: bool = False
    subtitle_wrap_to_source: bool = True
    subtitle_wrap_width: float = 600.0
    text_stroke_color: str = "#000000"
    text_stroke_width: float = 0.0
    # Typography shared by every text-bearing source.
    text_letter_spacing: float = 0.0
    # Extra space between wrapped/multi-line rows of one text block.
    text_line_gap: float = 0.0
    text_italic: bool = False
    text_case: str = "none"
    # Paint the fill gradient into the glyphs instead of the box behind them.
    text_gradient: bool = False
    # Cast the shadow from the glyph shapes instead of the source box.
    text_shadow_glyph: bool = False
    content_path: str = ""
    video_paths: list[str] = field(default_factory=list)
    video_timing_mode: str = "timeline"
    video_repeat_mode: str = "once"
    video_cycle_count: int = 1
    video_cycle_unlimited: bool = False
    video_speed: float = 1.0
    video_muted: bool = True
    video_saturation: float = 1.0
    video_grayscale: bool = False
    video_random_seed: int = 17
    image_fit_mode: str = "cover"
    background_mode: str = "color"
    background_ambient: bool = False
    background_ambient_blur: float = 24.0
    background_ambient_motion: float = 1.0
    background_bass_reactive: bool = False
    background_bass_strength: float = 0.06
    # When on, an album-art background cross-fades from the previous track's
    # artwork to the new one over background_track_transition_seconds at each
    # track change instead of switching in a single frame.
    background_track_transition: bool = False
    background_track_transition_seconds: float = 0.7
    brightness: float = 0.0
    contrast: float = 0.0
    progress_value: float = 0.62
    progress_track_color: str = "#303842"
    progress_mode: str = "track"
    # "auto" keeps each style's own handle (only Spotify draws one).
    progress_knob: str = "auto"
    group_id: str | None = None
    visualizer_style: str = "bars"
    visualizer_bars: int = 32
    visualizer_line_width: float = 3.0
    visualizer_sensitivity: float = 1.2
    visualizer_reactivity: float = 0.22
    visualizer_noise_gate: float = 0.003
    visualizer_min_level: float = 0.0
    visualizer_max_level: float = 0.96
    visualizer_attack: float = 0.55
    visualizer_release: float = 0.16
    visualizer_smoothing: float = 0.18
    visualizer_curve: float = 0.9
    # Radial style: ring radius as a share of the circle that fits the source.
    visualizer_inner_radius: float = 0.55
    subtitle_fallback: str = "Lyrics are not available for this track."
    subtitle_animation: str = "glow"
    subtitle_animation_duration: float = 0.36
    subtitle_flow_direction: str = "up"
    subtitle_motion_easing: str = "auto"
    subtitle_motion_distance: float = 22.0
    subtitle_stagger: float = 0.3
    subtitle_stagger_order: str = "near_first"
    subtitle_anchor: float = 0.5
    subtitle_zoom_amount: float = 0.12
    subtitle_glow_strength: float = 0.3
    subtitle_glow_radius: float = 7.0
    subtitle_role_styles: dict[str, dict[str, object]] = field(default_factory=dict)
    subtitle_advanced_categories: list[str] = field(default_factory=list)
    subtitle_advanced_settings: dict[str, object] = field(default_factory=dict)
    subtitle_context_lines: int = 1
    subtitle_next_lines: int = 1
    subtitle_line_spacing: float = 14.0
    subtitle_previous_opacity: float = 0.34
    subtitle_previous_blur: float = 1.5
    subtitle_previous_distance_fade: float = 1.0
    subtitle_next_distance_fade: float = 1.0
    subtitle_intro_enabled: bool = True
    subtitle_intro_midtrack: bool = False
    subtitle_intro_style: str = "dots"
    subtitle_intro_exit: str = "shrink"
    subtitle_intro_gap: float = 5.0
    subtitle_intro_period: float = 3.0
    subtitle_intro_scale: float = 1.0
    subtitle_current_line: int = -1
    subtitle_current_line_count: int = 1
    subtitle_scroll_offset: float = 0.0
    subtitle_timing_offset: float = 0.0
    # Size of the current lyric row relative to the context rows.
    subtitle_current_scale: float = 1.08
    subtitle_accent_enabled: bool = False
    subtitle_karaoke_enabled: bool = False
    subtitle_karaoke_offset: float = 0.0
    subtitle_accent_color: str = "#FFE08A"
    subtitle_karaoke_pending_mode: str = "dim"
    subtitle_karaoke_pending_color: str = "#FFFFFF"
    subtitle_karaoke_pending_brightness: float = 0.35
    subtitle_karaoke_lines: list[int] = field(default_factory=list)
    # Optional overrides by physical line position inside each multi-line cue.
    # Empty entries inherit the source's shared typography.
    subtitle_line_styles: list[dict[str, object]] = field(default_factory=list)
    track_list_count: int = 5
    track_list_style: str = "compact"
    track_list_window: str = "centered"
    track_list_show_number: bool = True
    track_list_show_artist: bool = True
    track_list_show_album: bool = False
    track_list_marker: str = "play"
    track_list_row_spacing: float = 6.0
    track_list_item_padding: float = 10.0
    track_list_current_color: str = "#FFFFFF"
    track_list_inactive_color: str = "#94A3B8"
    track_list_current_background: str = "#1685D1"
    track_list_inactive_opacity: float = 0.62
    track_list_current_scale: float = 1.05
    track_list_show_dividers: bool = False
    track_list_current_row: int = -1
    now_playing_style: str = "card"
    now_playing_duration: float = 3.0
    now_playing_exit_animation: str = "fade"
    now_playing_exit_duration: float = 0.35
    # Small heading above the title; empty hides it.
    now_playing_label: str = "NOW PLAYING"
    now_playing_align: str = "left"
    album_frame_style: str = "rounded"
    waveform_style: str = "line"
    level_meter_mode: str = "stereo"
    level_meter_style: str = "gradient"
    level_meter_orientation: str = "vertical"
    level_meter_sensitivity: float = 1.2
    level_meter_attack: float = 0.65
    level_meter_release: float = 0.18
    level_meter_min_level: float = 0.0
    level_meter_max_level: float = 1.0
    level_meter_segments: int = 16
    level_meter_gap: float = 4.0
    level_meter_show_peak: bool = True
    level_meter_peak_hold: float = 0.35
    level_meter_peak_decay: float = 0.7
    level_meter_track_color: str = "#263244"
    level_meter_low_color: str = "#22C55E"
    level_meter_mid_color: str = "#FACC15"
    level_meter_high_color: str = "#EF4444"
    particle_style: str = "dust"
    particle_density: int = 42
    particle_speed: float = 1.0
    particle_min_size: float = 1.0
    particle_max_size: float = 4.0
    particle_opacity: float = 0.62
    particle_direction: float = -90.0
    particle_drift: float = 0.25
    particle_twinkle: float = 0.25
    particle_glow: float = 0.2
    particle_secondary_color: str = "#7DD3FC"
    particle_seed: int = 17
    animation_in: str = "none"
    animation_out: str = "none"
    # Kept for loading projects and presets created before entrance/exit timing
    # was split. New code should use the two phase-specific values below.
    animation_duration: float = 0.45
    animation_in_duration: float = 0.45
    animation_out_duration: float = 0.45
    # Crossfade/AutoMix: the Canvas changes track in the middle of each audio
    # overlap, where animation_in/out play. True stretches them over the mix:
    # the exit over the overlap's first half, the entrance over its second.
    animation_fit_mix: bool = False
    # Idle motion repeated for as long as the source is shown.
    loop_motion: str = "none"
    loop_motion_period: float = 4.0
    loop_motion_amount: float = 1.0
    music_reactive_enabled: bool = False
    music_reactive_effect: str = "bass_scale"
    music_reactive_strength: float = 0.25
    music_reactive_attack: float = 0.06
    music_reactive_release: float = 0.4
    music_reactive_band: str = "bass"
    music_reactive_sensitivity: float = 1.0
    music_reactive_threshold: float = 0.0
    music_reactive_curve: str = "linear"
    music_reactive_offset: float = 0.0
    # Cut the whole source to a shape (see app.utils.mask_shapes).
    mask_shape: str = "none"
    timeline_start: float = 0.0
    timeline_duration: float = 0.0
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        """Honor the former single-duration constructor argument."""
        if (
            self.animation_duration != 0.45
            and self.animation_in_duration == 0.45
            and self.animation_out_duration == 0.45
        ):
            self.animation_in_duration = self.animation_duration
            self.animation_out_duration = self.animation_duration
        if self.source_type is SourceType.ALBUM_COVER:
            # Cover art is rendered through square frame styles. Normalize old
            # or externally generated rectangular values at the model boundary
            # so Canvas, Inspector, Preview, and export all agree.
            self.height = self.width

    def to_dict(self) -> dict[str, Any]:
        """Convert the model to JSON-compatible data."""
        result = asdict(self)
        result["source_type"] = self.source_type.value
        # Older project versions only understand one duration. The
        # longer phase is the least surprising compatibility fallback.
        result["animation_duration"] = max(
            self.animation_in_duration, self.animation_out_duration,
        )
        return result

    @property
    def uses_personal_color(self) -> bool:
        return self.personal_color_enabled or (self.shadow.enabled and self.shadow.personal_color_enabled)

    @property
    def personal_color_fields(self) -> tuple[str, ...]:
        """Primary channels controlled by the source-wide artwork color policy."""
        if self.source_type in {SourceType.TEXT, SourceType.TIME, SourceType.LYRICS}:
            return ("outline_color",)
        if self.source_type is SourceType.TRACK_LIST:
            return ("track_list_current_color",)
        if self.source_type is SourceType.AUDIO_LEVEL_METER:
            return ("level_meter_low_color", "level_meter_mid_color", "level_meter_high_color")
        if self.source_type is SourceType.PARTICLE_OVERLAY:
            return ("fill_color", "particle_secondary_color")
        return ("fill_color",)

    @property
    def uses_bass_reaction(self) -> bool:
        return (self.source_type is SourceType.BACKGROUND and self.background_bass_reactive) or (
            self.source_type in {SourceType.TEXT, SourceType.LYRICS} and self.music_reactive_enabled
            and self.music_reactive_strength > 0
        )

    @property
    def music_reaction_profile(self) -> tuple:
        return (self.music_reactive_band, self.music_reactive_attack,
                self.music_reactive_release, self.music_reactive_sensitivity,
                self.music_reactive_threshold, self.music_reactive_curve)

    def resolved_lyrics(self) -> Source:
        """Resolve active overrides without changing the saved basic settings."""
        if self.source_type is not SourceType.LYRICS or not self.subtitle_advanced_categories:
            return self
        keys = {key for category in self.subtitle_advanced_categories for key in SUBTITLE_ADVANCED_GROUPS[category]}
        values = {key: deepcopy(value) for key, value in self.subtitle_advanced_settings.items() if key in keys}
        if "styles" in self.subtitle_advanced_categories:
            # Keep old projects readable without letting former role typography
            # override basic text or per-line styles.
            values["subtitle_role_styles"] = {
                role: {key: value for key, value in style.items() if key in {"scale", "opacity", "blur"}}
                for role, style in values.get("subtitle_role_styles", self.subtitle_role_styles).items()
            }
        return replace(self, **values, subtitle_advanced_categories=[])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        """Restore a source from JSON-compatible data."""
        if not isinstance(data, dict):
            raise ValueError("Project sources must be objects.")
        source_data = data.copy()
        # Retired model selector: keep projects saved with either choice readable.
        source_data.pop("subtitle_karaoke_analysis_model", None)
        source_data["source_type"] = SourceType(source_data["source_type"])
        if "subtitle_advanced_enabled" in source_data:
            # The former master switch gated the category switches. Preserve
            # that effective state while retaining saved advanced drafts.
            legacy_advanced_enabled = source_data.pop("subtitle_advanced_enabled")
            if not isinstance(legacy_advanced_enabled, bool):
                raise ValueError("Invalid legacy subtitle advanced mode.")
            if legacy_advanced_enabled:
                source_data.setdefault("subtitle_advanced_categories", list(SUBTITLE_ADVANCED_GROUPS))
            else:
                source_data["subtitle_advanced_categories"] = []
        # ``subtitle_style`` was a small preset selector that overrode the
        # actual text colour while previewing/exporting lyrics.  Keep old
        # projects visually stable by migrating its effective colour into the
        # same colour property used by normal text, then discard the obsolete
        # key before constructing the current model.
        legacy_subtitle_style = source_data.pop("subtitle_style", None)
        if source_data["source_type"] is SourceType.LYRICS:
            legacy_subtitle_colors = {
                "karaoke": "#FFE08A",
                "minimal": "#FFFFFF",
                "neon": "#72E8FF",
            }
            if legacy_subtitle_style in legacy_subtitle_colors:
                source_data["outline_color"] = legacy_subtitle_colors[
                    legacy_subtitle_style
                ]
        source_data["subtitle_animation"] = _LEGACY_SUBTITLE_ANIMATIONS.get(
            source_data.get("subtitle_animation"),
            source_data.get("subtitle_animation", "glow"),
        )
        if source_data["subtitle_animation"] not in SUBTITLE_ANIMATIONS:
            source_data["subtitle_animation"] = "glow"
        legacy_animation_duration = source_data.get("animation_duration", 0.45)
        source_data.setdefault("animation_in_duration", legacy_animation_duration)
        source_data.setdefault("animation_out_duration", legacy_animation_duration)
        # A short-lived build had separate mix-handover styles; they now reuse
        # animation_in/out, so drop them instead of failing to load.
        for legacy_key in ("mix_animation_in", "mix_animation_out", "mix_animation_duration"):
            source_data.pop(legacy_key, None)
        source_data["shadow"] = Shadow(**source_data.get("shadow", {}))
        source_data["gradient"] = Gradient(**source_data.get("gradient", {}))
        source = cls(**source_data)
        if not isinstance(source.id, str) or not source.id.strip():
            raise ValueError("Every project source must have a non-empty string ID.")
        if not isinstance(source.name, str) or not source.name.strip():
            raise ValueError("Every project source must have a non-empty name.")
        for model_field in fields(cls):
            default = model_field.default
            if default is MISSING or isinstance(default, bool):
                continue
            value = getattr(source, model_field.name)
            if isinstance(default, (int, float)):
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not isfinite(float(value))):
                    raise ValueError(
                        f"Source '{source.name}' has an invalid numeric value for "
                        f"'{model_field.name}'."
                    )
        if not 0.0 <= source.opacity <= 1.0:
            raise ValueError(f"Source '{source.name}' opacity must be between 0 and 1.")
        for name in ("flip_horizontal", "flip_vertical"):
            if not isinstance(getattr(source, name), bool):
                raise ValueError(f"Source '{source.name}' has an invalid {name}.")
        if (not isinstance(source.frame_color, str)
                or fullmatch(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", source.frame_color) is None):
            raise ValueError(f"Source '{source.name}' has an invalid frame color.")
        if not isinstance(source.music_reactive_enabled, bool):
            raise ValueError("Music reaction flag must be true or false.")
        personal_color_ranges = {
            "personal_color_brightness": (-100.0, 100.0),
            "personal_color_saturation": (-100.0, 100.0),
            "personal_color_hue_shift": (-180.0, 180.0),
            "personal_color_strength": (0.0, 1.0),
        }
        for policy in (source, source.shadow):
            if not isinstance(policy.personal_color_enabled, bool):
                raise ValueError(f"Source '{source.name}' personal color flag must be true or false.")
            for name, (minimum, maximum) in personal_color_ranges.items():
                value = getattr(policy, name)
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not minimum <= value <= maximum):
                    raise ValueError(
                        f"Source '{source.name}' value for '{name}' must be between "
                        f"{minimum:g} and {maximum:g}."
                    )
        if source.width <= 0 or source.height <= 0 or source.scale <= 0:
            raise ValueError(f"Source '{source.name}' dimensions and scale must be positive.")
        if source.width > 100_000 or source.height > 100_000 or source.scale > 100:
            raise ValueError(f"Source '{source.name}' dimensions are unexpectedly large.")
        if source.timeline_start < 0 or source.timeline_duration < 0:
            raise ValueError(f"Source '{source.name}' timeline values cannot be negative.")
        # Flat legacy documents retain inactive video fields for round trips.
        VideoComponent.from_source(source).validate(source.name)
        LyricsComponent.validate_styles(source.subtitle_line_styles, source.subtitle_role_styles, source.name)
        if not 0.0 <= float(source.text_stroke_width) <= 12.0:
            raise ValueError(
                f"Source '{source.name}' text stroke width must be between 0 and 12."
            )
        for name, allowed in (
            ("text_case", TEXT_CASES), ("progress_knob", PROGRESS_KNOBS),
            ("loop_motion", LOOP_MOTIONS), ("mask_shape", MASK_SHAPES),
            ("music_reactive_effect", MUSIC_REACTIVE_EFFECTS),
            ("music_reactive_band", MUSIC_REACTIVE_BANDS),
            ("music_reactive_curve", MUSIC_REACTIVE_CURVES),
            ("now_playing_align", ("left", "center", "right")),
            ("subtitle_flow_direction", ("up", "down", "left", "right")),
            ("subtitle_motion_easing", ("auto", "linear", "ease_in", "ease_out", "smooth")),
            ("subtitle_stagger_order", ("near_first", "far_first")),
            ("subtitle_intro_style", ("dots", "breathing", "wave", "bars", "ring", "orbit", "ripple", "comet")),
            ("subtitle_intro_exit", ("shrink", "fade", "drift", "spread", "wipe")),
            ("subtitle_karaoke_pending_mode", ("custom", "dim")),
        ):
            if getattr(source, name) not in allowed:
                raise ValueError(f"Source '{source.name}' has an invalid {name}.")
        if not isinstance(source.now_playing_label, str):
            raise ValueError(f"Source '{source.name}' has an invalid now-playing label.")
        if (not isinstance(source.subtitle_karaoke_pending_color, str)
                or fullmatch(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", source.subtitle_karaoke_pending_color) is None):
            raise ValueError(f"Source '{source.name}' has an invalid pending lyric color.")
        if (not isinstance(source.subtitle_karaoke_lines, list)
                or any(not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 12
                       for index in source.subtitle_karaoke_lines)):
            raise ValueError(f"Source '{source.name}' has invalid karaoke line positions.")
        for name in ("subtitle_intro_enabled", "subtitle_intro_midtrack", "subtitle_karaoke_enabled",
                     "subtitle_auto_shrink", "subtitle_auto_wrap", "subtitle_wrap_to_source"):
            if not isinstance(getattr(source, name), bool):
                raise ValueError(f"Source '{source.name}' has an invalid {name}.")
        for name, (minimum, maximum) in {
            "frame_width": (0.0, 80.0),
            "frame_inset": (0.0, 200.0),
            "text_letter_spacing": (-10.0, 60.0),
            "text_line_gap": (-40.0, 200.0),
            "subtitle_current_scale": (1.0, 2.0),
            "subtitle_wrap_width": (16.0, 100_000.0),
            "subtitle_karaoke_pending_brightness": (0.0, 1.0),
            "subtitle_karaoke_offset": (-5.0, 5.0),
            "subtitle_animation_duration": (0.05, 3.0),
            "subtitle_motion_distance": (0.0, 200.0),
            "subtitle_stagger": (0.0, 0.8),
            "subtitle_anchor": (0.0, 1.0),
            "subtitle_zoom_amount": (0.0, 0.5),
            "subtitle_glow_strength": (0.0, 1.0),
            "subtitle_glow_radius": (0.0, 30.0),
            "subtitle_previous_distance_fade": (0.0, 1.0),
            "subtitle_next_distance_fade": (0.0, 1.0),
            "subtitle_intro_gap": (2.0, 30.0),
            "subtitle_intro_period": (1.0, 10.0),
            "subtitle_intro_scale": (0.25, 3.0),
            "visualizer_inner_radius": (0.1, 0.95),
            "background_ambient_blur": (0.0, 80.0),
            "background_ambient_motion": (0.0, 3.0),
            "background_bass_strength": (0.0, 0.2),
            "loop_motion_period": (0.2, 60.0),
            "loop_motion_amount": (0.0, 5.0),
            "music_reactive_strength": (0.0, 1.0),
            "music_reactive_attack": (0.0, 3.0),
            "music_reactive_release": (0.0, 5.0),
            "music_reactive_sensitivity": (0.0, 4.0),
            "music_reactive_threshold": (0.0, 0.95),
            "music_reactive_offset": (-2.0, 2.0),
        }.items():
            if not minimum <= float(getattr(source, name)) <= maximum:
                raise ValueError(
                    f"Source '{source.name}' value for '{name}' must be between "
                    f"{minimum:g} and {maximum:g}."
                )
        bounded_integers = {
            "visualizer_bars": (4, 96),
            # -1 means that lyric context is derived from source height. Track
            # lists use 0 because zero was never a valid visible-track count.
            "subtitle_context_lines": (-1, 6),
            "subtitle_next_lines": (-1, 6),
            "track_list_count": (0, 15),
            "level_meter_segments": (3, 64),
            "particle_density": (4, 500),
            "particle_seed": (0, 999_999),
        }
        for name, (minimum, maximum) in bounded_integers.items():
            value = getattr(source, name)
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(
                    f"Source '{source.name}' value for '{name}' must be between "
                    f"{minimum} and {maximum}."
                )
        if source.group_id is not None and not isinstance(source.group_id, str):
            raise ValueError(f"Source '{source.name}' has an invalid group ID.")
        if "subtitle_advanced_categories" not in source_data and source.subtitle_role_styles:
            # Older role overrides were already advanced settings. Keep their
            # appearance and expose their ownership instead of silently hiding
            # why the basic style controls do not affect those roles.
            source.subtitle_advanced_categories = ["styles"]
            source.subtitle_advanced_settings = {
                "subtitle_role_styles": deepcopy(source.subtitle_role_styles),
                "subtitle_previous_distance_fade": source.subtitle_previous_distance_fade,
                "subtitle_next_distance_fade": source.subtitle_next_distance_fade,
            }
            source.subtitle_role_styles = {}
        categories = source.subtitle_advanced_categories
        if (not isinstance(categories, list) or not all(isinstance(key, str) and key in SUBTITLE_ADVANCED_GROUPS for key in categories)
                or len(set(categories)) != len(categories)):
            raise ValueError("Invalid subtitle advanced categories.")
        values = source.subtitle_advanced_settings
        allowed = {key for keys in SUBTITLE_ADVANCED_GROUPS.values() for key in keys}
        if not isinstance(values, dict) or not set(values) <= allowed:
            raise ValueError("Invalid subtitle advanced settings.")
        if values:
            if "subtitle_animation" in values and values["subtitle_animation"] not in SUBTITLE_ANIMATIONS:
                raise ValueError("Invalid advanced subtitle effect.")
            if "text_alignment" in values and values["text_alignment"] not in {"left", "center", "right"}:
                raise ValueError("Invalid advanced subtitle alignment.")
            # Reuse the same validation for basic and advanced values, including
            # inactive drafts, so an invalid saved override cannot surface later.
            cls.from_dict(source.to_dict() | values | {"subtitle_advanced_settings": {},
                          "subtitle_advanced_categories": []})
        return source
