"""Ready-made, track-aware visual layouts for playlist videos."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.models.source import Gradient, Shadow, Source, SourceType


@dataclass(frozen=True, slots=True)
class PresetDefinition:
    """Describes a selectable, complete canvas layout."""

    identifier: str
    korean_name: str
    english_name: str
    korean_description: str
    english_description: str
    builder: Callable[[], list[Source]]
    # Coordinate space the builder authors in; the target canvas is adapted from
    # these dimensions. User presets record the canvas they were captured on.
    source_width: float = 1280.0
    source_height: float = 720.0
    # True for user-saved presets, which the picker lets you delete or export.
    editable: bool = False

    def name(self, language: str) -> str:
        return self.korean_name if language == "ko" else self.english_name

    def description(self, language: str) -> str:
        return self.korean_description if language == "ko" else self.english_description


TRANSPARENT = "#00000000"


def _background(color: str, end_color: str | None = None, *,
                mode: str = "color", ambient: bool = False) -> Source:
    source = Source(
        SourceType.BACKGROUND,
        "Background",
        width=1280,
        height=720,
        fill_color=color,
        locked=False,
        z_index=-20,
        text="",
        background_mode=mode,
        background_ambient=ambient,
    )
    if end_color:
        source.gradient = Gradient(True, color, end_color)
    return source


def _panel(
    name: str,
    x: float,
    y: float,
    width: float,
    height: float,
    color: str,
    z: int,
    radius: float = 20,
    opacity: float = 1.0,
) -> Source:
    return Source(
        SourceType.SHAPE,
        name,
        x=x,
        y=y,
        width=width,
        height=height,
        fill_color=color,
        opacity=opacity,
        border_radius=radius,
        z_index=z,
        locked=True,
    )


def _text(
    name: str,
    text: str,
    x: float,
    y: float,
    width: float,
    height: float,
    text_color: str,
    size: float,
    z: int,
    *,
    surface: str = TRANSPARENT,
    alignment: str = "left",
    weight: int = 600,
    animation_in: str = "fade",
    animation_out: str = "fade",
    animation_duration: float = 0.45,
) -> Source:
    return Source(
        SourceType.TEXT,
        name,
        x=x,
        y=y,
        width=width,
        height=height,
        fill_color=surface,
        outline_color=text_color,
        text=text,
        font_size=size,
        font_weight=weight,
        text_alignment=alignment,
        z_index=z,
        animation_in=animation_in,
        animation_out=animation_out,
        animation_duration=animation_duration,
    )


def _cover(
    x: float,
    y: float,
    size: float,
    color: str,
    z: int,
    *,
    radius: float = 24,
    frame: str = "rounded",
    animation_in: str = "zoom",
    animation_out: str = "zoom",
) -> Source:
    return Source(
        SourceType.ALBUM_COVER,
        "Album Cover",
        x=x,
        y=y,
        width=size,
        height=size,
        fill_color=color,
        border_radius=radius,
        album_frame_style=frame,
        shadow=Shadow(True, "#000000", 20, 8, 10, 0.32),
        z_index=z,
        animation_in=animation_in,
        animation_out=animation_out,
        animation_duration=0.55,
    )


def _progress(
    x: float,
    y: float,
    width: float,
    color: str,
    z: int,
    *,
    style: str = "rounded",
    track_color: str = "#344050",
) -> Source:
    return Source(
        SourceType.PROGRESS_BAR,
        "Progress",
        x=x,
        y=y,
        width=width,
        height=12,
        fill_color=color,
        progress_track_color=track_color,
        border_radius=8,
        progress_style=style,
        z_index=z,
        animation_in="fade",
        animation_out="fade",
        animation_duration=0.35,
    )


def _visualizer(
    x: float,
    y: float,
    width: float,
    height: float,
    color: str,
    z: int,
    *,
    style: str = "bars",
    bars: int = 44,
) -> Source:
    return Source(
        SourceType.AUDIO_VISUALIZER,
        "Audio Visualizer",
        x=x,
        y=y,
        width=width,
        height=height,
        fill_color=color,
        visualizer_style=style,
        visualizer_bars=bars,
        visualizer_line_width=3.0,
        z_index=z,
        animation_in="fade",
        animation_out="fade",
        animation_duration=0.5,
    )


def _waveform(x: float, y: float, width: float, height: float, color: str, z: int, *,
              style: str = "line", points: int = 64) -> Source:
    """Create an audio-reactive horizontal waveform source."""
    return Source(
        SourceType.AUDIO_WAVEFORM, "Audio Waveform", x=x, y=y, width=width, height=height,
        fill_color=color, waveform_style=style, visualizer_bars=points,
        visualizer_line_width=3.0, z_index=z, animation_in="fade", animation_out="fade",
    )


def _meter(x: float, y: float, width: float, height: float, color: str, z: int, *,
           mode: str = "stereo") -> Source:
    """Create a compact audio level meter source."""
    return Source(
        SourceType.AUDIO_LEVEL_METER, "Audio Level Meter", x=x, y=y, width=width, height=height,
        fill_color=color, level_meter_mode=mode, z_index=z, animation_in="fade", animation_out="fade",
    )


def _particles(color: str, z: int, *, style: str = "dust", density: int = 38,
               speed: float = 0.75, opacity: float = 0.45) -> Source:
    """Create a full-artboard animated texture overlay."""
    return Source(
        SourceType.PARTICLE_OVERLAY, "Particles", width=1280, height=720, fill_color=color,
        opacity=opacity, particle_style=style, particle_density=density, particle_speed=speed,
        z_index=z, locked=True,
    )


def _track_list(x: float, y: float, width: float, height: float, color: str, z: int, *,
                count: int = 4, style: str = "compact") -> Source:
    """Create a dynamic previous/current/next track list."""
    card_style = style in {"cards", "glass", "pills"}
    return Source(
        SourceType.TRACK_LIST, "Track List", x=x, y=y, width=width, height=height,
        fill_color=TRANSPARENT, outline_color=color, font_size=15, text_alignment="left",
        text="▶ 01. Current track\n  02. Next track", track_list_count=count,
        track_list_style=style, track_list_current_color="#FFFFFF" if card_style else color,
        track_list_inactive_color=color,
        track_list_current_background=color if card_style else "#1685D1",
        track_list_inactive_opacity=0.58, track_list_row_spacing=5.0,
        z_index=z,
    )


def _now_playing(x: float, y: float, width: float, height: float, color: str, z: int, *,
                 style: str = "card", seconds: float = 3.0,
                 exit_style: str = "fade") -> Source:
    """Create a track-start announcement card."""
    return Source(
        SourceType.NOW_PLAYING, "Now Playing", x=x, y=y, width=width, height=height,
        fill_color=color, outline_color="#FFFFFF", border_radius=18, font_size=18,
        text="NOW PLAYING\nTrack title\nArtist", now_playing_style=style,
        now_playing_duration=seconds, now_playing_exit_animation=exit_style,
        now_playing_exit_duration=min(0.45, seconds * 0.35), z_index=z,
        animation_in="slide_right", animation_out="fade", animation_duration=0.35,
    )


def _lyrics(x: float, y: float, width: float, height: float, color: str, z: int) -> Source:
    """Create a streaming-style previous/current/next lyric source."""
    return Source(
        SourceType.LYRICS, "Lyrics", x=x, y=y, width=width, height=height,
        fill_color=TRANSPARENT, outline_color=color, text="Lyrics are not available for this track.",
        font_size=23, font_weight=600, text_alignment="center",
        subtitle_animation="rise", subtitle_animation_duration=0.28,
        subtitle_context_lines=1, subtitle_next_lines=1, subtitle_previous_opacity=0.34,
        subtitle_previous_blur=1.5, z_index=z,
    )


def aurora() -> list[Source]:
    """Teal-to-violet gradient with a large centred cover and mirrored waveform."""
    return [
        _background("#0B1F2A", "#3B1F55"),
        _particles("#59E0C7", -1, style="dust", density=30, speed=0.4, opacity=0.2),
        replace(_cover(475, 54, 330, "#2FC6B6", 2, radius=28, animation_in="rise", animation_out="zoom_out"),
                loop_motion="float", loop_motion_period=6.0, loop_motion_amount=0.6),
        replace(_text("Playlist label", "AURORA MIX · %track% / %track_total%", 300, 400, 680, 28,
                      "#7FE9D8", 14, 3, alignment="center", weight=700), text_letter_spacing=3.0),
        replace(_text("Song title", "%title%", 200, 436, 880, 78, "#FFFFFF", 38, 4, alignment="center",
                      weight=700, animation_in="rise", animation_out="fade"),
                text_shadow_glyph=True, shadow=_glow("#59E0C7", 18, 0.55)),
        _text("Artist", "%artist% — %album%", 220, 518, 840, 32, "#C9B6E6", 18, 5, alignment="center",
              animation_in="rise", animation_out="fade"),
        _waveform(140, 574, 1000, 66, "#59E0C7", 6, style="mirror", points=80),
        _progress(240, 652, 800, "#B98CE8", 7, style="apple", track_color="#2A3A4A"),
        _text("Time", "%current_time% / %total_time%", 240, 676, 800, 22, "#8FA6B8", 12, 8, alignment="center"),
    ]


def cassette() -> list[Source]:
    """Retro mixtape layout on a warm cream sleeve with a scrolling track list."""
    return [
        _background("#2B2016", "#4A3722"),
        _panel("Sleeve", 66, 88, 700, 512, "#F2E4CC", 0, 16),
        replace(_text("Label", "SIDE A · MIXTAPE", 108, 126, 540, 34, "#8A6A3C", 15, 3, weight=700),
                text_letter_spacing=3.0),
        _text("Song title", "%title%", 108, 176, 620, 96, "#2B2016", 40, 4, weight=800, animation_in="slide_right", animation_out="slide_left"),
        _text("Artist", "%artist%", 108, 282, 560, 34, "#6E5636", 19, 5, animation_in="slide_right", animation_out="slide_left"),
        _text("Album", "%album%", 108, 322, 560, 28, "#9A8055", 14, 6),
        _visualizer(108, 398, 616, 58, "#C98A3C", 7, style="bars", bars=32),
        replace(_progress(108, 494, 616, "#8A6A3C", 8, style="rounded", track_color="#D8C4A2"),
                progress_knob="bar"),
        _text("Time", "%current_time% / %total_time%", 108, 520, 616, 26, "#9A8055", 13, 9),
        # A polaroid pinned to the wall, gently swaying.
        replace(_cover(820, 132, 320, "#C98A3C", 2, radius=14, frame="polaroid",
                       animation_in="swing", animation_out="slide_right"),
                loop_motion="sway", loop_motion_period=6.0, loop_motion_amount=0.35),
        replace(_track_list(820, 496, 340, 132, "#E7C79A", 10, count=3, style="scroll"),
                track_list_show_artist=False, text_overflow="ellipsis"),
    ]


def midnight() -> list[Source]:
    """Deep-navy typographic layout with quiet lyrics and a hairline progress bar."""
    return [
        _background("#0A1120", "#111C33"),
        replace(_text("Playlist", "PLAYLIST / %track%", 130, 110, 700, 32, "#5B7CB0", 13, 1, weight=700),
                text_letter_spacing=4.0),
        replace(_text("Song title", "%title%", 128, 150, 940, 110, "#F4F7FF", 54, 2, weight=700,
                      animation_in="slide_right", animation_out="slide_left"),
                text_shadow_glyph=True, shadow=_glow("#3E64A8", 22, 0.5)),
        _text("Artist", "%artist%  ·  %album%", 130, 262, 820, 40, "#AEBEDC", 22, 3,
              animation_in="fade", animation_out="fade"),
        # The current line and the one after it, quietly under the type.
        # The current row sits at the box centre, so the box leaves room below it.
        replace(_lyrics(130, 318, 880, 170, "#6E7F9E", 4),
                font_size=22, font_weight=600, text_alignment="left", subtitle_animation="rise",
                subtitle_context_lines=0, subtitle_next_lines=1, subtitle_line_spacing=12,
                subtitle_previous_opacity=0.45, subtitle_current_scale=1.12,
                subtitle_accent_enabled=True, subtitle_accent_color="#F4F7FF",
                subtitle_current_line=0),
        _visualizer(130, 496, 880, 36, "#3E64A8", 5, style="line", bars=48),
        _progress(130, 544, 880, "#7FA8E6", 6, style="youtube", track_color="#1E2C46"),
        _text("Time", "%current_time%", 130, 566, 420, 24, "#6E7F9E", 12, 7),
        _text("Total", "%total_time% · %track% / %track_total%", 660, 566, 350, 24, "#6E7F9E", 12, 7, alignment="right"),
    ]


def bubblegum() -> list[Source]:
    """Pastel pink and mint card with a gradient title and a plump capsule visualizer."""
    return [
        _background("#FFE3F1", "#DDF6F0"),
        _panel("Card", 150, 88, 980, 544, "#FFFFFF", 0, 40, 0.92),
        _particles("#FFC1DE", 1, style="dust", density=26, speed=0.35, opacity=0.3),
        replace(_cover(210, 162, 300, "#FF9CC9", 2, radius=40, animation_in="bounce", animation_out="zoom"),
                loop_motion="wobble", loop_motion_period=5.0, loop_motion_amount=0.6),
        replace(_text("Playlist", "SWEET BEATS · %track% / %track_total%", 560, 165, 480, 32,
                      "#FF6FB0", 14, 3, weight=800), text_letter_spacing=2.0),
        # Opaque text colour: the glyph gradient inherits its alpha.
        replace(_text("Song title", "%title%", 560, 216, 500, 100, "#FF6FB0", 36, 4, weight=800,
                      animation_in="bounce", animation_out="slide_left"),
                text_gradient=True, gradient=Gradient(True, "#FF6FB0", "#3FC5B3")),
        _text("Artist", "%artist%", 560, 326, 480, 34, "#8A6E80", 19, 5, animation_in="slide_right", animation_out="slide_left"),
        replace(_visualizer(560, 396, 480, 74, "#57D2C0", 6, style="capsule", bars=22),
                loop_motion="breathe", loop_motion_period=3.0),
        replace(_progress(210, 548, 860, "#FF8FC4", 7, style="apple", track_color="#F0D7E5"),
                progress_knob="circle", height=14),
        _text("Time", "%current_time% / %total_time%", 210, 574, 860, 26, "#8A6E80", 13, 8, alignment="center"),
    ]


def noir() -> list[Source]:
    """Black-and-white cinema layout: serif subtitles above a lower third."""
    serif = "Georgia"
    return [
        _background("#050505", "#1E1E1E"),
        _particles("#FFFFFF", -1, style="dust", density=18, speed=0.22, opacity=0.10),
        _panel("Top bar", 0, 0, 1280, 66, "#000000", 0, 0),
        _panel("Bottom bar", 0, 654, 1280, 66, "#000000", 1, 0),
        # Film subtitles in the open frame above the lower third.
        replace(_lyrics(140, 250, 1000, 110, "#F2F2F2", 2),
                font_family=serif, font_size=28, font_weight=400, text_italic=True,
                subtitle_animation="rise", subtitle_animation_duration=0.3,
                subtitle_context_lines=0, subtitle_next_lines=0, subtitle_line_spacing=8,
                subtitle_current_scale=1.0, subtitle_current_line=0,
                text_shadow_glyph=True, shadow=Shadow(True, "#000000", 10, 0, 2, 0.8)),
        _panel("Lower third", 0, 430, 1280, 224, "#0A0A0A", 2, 0, 0.72),
        _cover(90, 456, 168, "#8C8C8C", 3, radius=4, animation_in="slide_right", animation_out="slide_left"),
        replace(_text("Playlist", "NOIR SESSION  ·  TRACK %track% / %track_total%", 300, 452, 780, 26,
                      "#9C9C9C", 12, 4, weight=700), text_letter_spacing=3.0),
        replace(_text("Song title", "%title%", 300, 486, 840, 66, "#FFFFFF", 34, 5, weight=700,
                      animation_in="slide_up", animation_out="slide_down"), font_family=serif),
        _text("Artist album", "%artist%  —  %album%", 300, 560, 780, 30, "#C8C8C8", 16, 6, animation_in="fade", animation_out="fade"),
        _progress(300, 608, 840, "#FFFFFF", 7, style="rounded", track_color="#333333"),
        _text("Time", "%current_time% / %total_time%", 300, 626, 840, 22, "#8C8C8C", 12, 8, alignment="right"),
    ]


def sunset() -> list[Source]:
    """Warm orange-to-purple scene with a glowing sun cover and a one-line lyric."""
    return [
        _background("#FF7E3D", "#5B2A86"),
        _panel("Info", 636, 118, 500, 484, "#2A123F", 0, 30, 0.7),
        replace(_cover(118, 160, 400, "#FFB067", 2, radius=200, animation_in="rise", animation_out="fade"),
                mask_shape="circle", shadow=_glow("#FFB067", 40, 0.45),
                loop_motion="breathe", loop_motion_period=6.0, loop_motion_amount=1.2),
        replace(_text("Playlist", "SUNSET DRIVE", 688, 172, 420, 30, "#FFC48A", 14, 3, weight=700),
                text_letter_spacing=4.0),
        replace(_text("Song title", "%title%", 688, 228, 424, 96, "#FFFFFF", 31, 4, weight=700,
                      animation_in="slide_left", animation_out="slide_right"),
                text_shadow_glyph=True, shadow=_glow("#FF9F5A", 16, 0.5)),
        _text("Artist", "%artist%  ·  %album%", 688, 326, 420, 32, "#F0CBE6", 17, 5, animation_in="slide_left", animation_out="slide_right"),
        replace(_lyrics(688, 366, 420, 56, "#FFE2C4", 6),
                font_size=17, font_weight=600, text_alignment="left", subtitle_animation="rise",
                subtitle_context_lines=0, subtitle_next_lines=0, subtitle_line_spacing=6,
                subtitle_current_scale=1.0, subtitle_current_line=0),
        _visualizer(688, 436, 420, 58, "#FFB067", 7, style="dots", bars=26),
        replace(_progress(688, 526, 420, "#FF9F5A", 8, style="rounded", track_color="#4A2A5E"),
                progress_knob="circle", height=14),
        _text("Track", "%track% / %track_total% · %current_time%", 688, 554, 420, 26, "#C79ECB", 13, 9),
    ]


def terminal() -> list[Source]:
    """CRT-green monospace console: spectrum readout and lyrics printed as output."""
    def mono(source: Source) -> Source:
        return replace(source, font_family="Consolas")

    return [
        _background("#02110A", "#04240F"),
        _panel("Console", 68, 60, 1144, 600, "#031A0E", 0, 8, 0.9),
        mono(_text("Prompt", "user@playlist:~$ now-playing --track %track%", 108, 92, 940, 28, "#3BE07A", 14, 2, animation_in="fade", animation_out="fade")),
        replace(mono(_text("Song title", "%title%", 108, 138, 940, 66, "#B6FFD1", 33, 3, weight=700,
                           animation_in="fade", animation_out="fade")),
                text_shadow_glyph=True, shadow=_glow("#3BE07A", 14, 0.6)),
        mono(_text("Artist album", "  artist: %artist%    album: %album%", 108, 214, 940, 26, "#59C98B", 15, 4, animation_in="fade", animation_out="fade")),
        _visualizer(108, 270, 1000, 118, "#3BE07A", 5, style="spectrum", bars=64),
        mono(_text("Lyric prompt", "$ tail -f lyrics.lrc", 108, 396, 940, 24, "#3BE07A", 14, 6)),
        # Scrolls up like console output: the previous line dims, no scaling.
        replace(mono(_lyrics(108, 422, 1000, 160, "#2F8F57", 7)),
                font_size=19, font_weight=600, text_alignment="left", subtitle_animation="rise",
                subtitle_animation_duration=0.2, subtitle_context_lines=1, subtitle_next_lines=0,
                subtitle_line_spacing=6, subtitle_previous_opacity=0.5, subtitle_previous_blur=0.0,
                subtitle_current_scale=1.0, subtitle_accent_enabled=True,
                subtitle_accent_color="#B6FFD1", subtitle_current_line=0),
        _progress(108, 590, 1000, "#3BE07A", 8, style="youtube", track_color="#0C3A1E"),
        mono(_text("Time", "[ %current_time% / %total_time% ]", 108, 614, 1000, 24, "#59C98B", 12, 9, alignment="right")),
    ]


def gallery() -> list[Source]:
    """Quiet white-gallery layout with a framed cover, museum caption and a quoted lyric."""
    serif = "Georgia"
    return [
        _background("#F4F1EA"),
        _panel("Wall shadow", 456, 78, 372, 360, "#E4DFD4", 0, 4),
        _cover(474, 90, 336, "#C9BFA8", 2, radius=6, frame="polaroid", animation_in="fade", animation_out="fade"),
        replace(_text("Index", "No. %track% / %track_total%", 340, 466, 600, 24, "#9A9484", 12, 5,
                      alignment="center"), text_letter_spacing=2.0),
        replace(_text("Caption title", "%title%", 340, 494, 600, 44, "#20201C", 25, 3, alignment="center",
                      weight=700, animation_in="fade", animation_out="fade"), font_family=serif),
        replace(_text("Caption meta", "%artist%,  %album%", 340, 540, 600, 28, "#6A665C", 15, 4,
                      alignment="center", animation_in="fade", animation_out="fade"),
                font_family=serif, text_italic=True),
        replace(_lyrics(340, 574, 600, 40, "#8A8475", 6),
                font_family=serif, font_size=15, font_weight=400, text_italic=True,
                subtitle_animation="glow", subtitle_context_lines=0, subtitle_next_lines=0,
                subtitle_line_spacing=4, subtitle_current_scale=1.0, subtitle_current_line=0),
        replace(_progress(430, 628, 420, "#20201C", 7, style="rounded", track_color="#D8D2C4"), height=4),
        _text("Time", "%current_time% / %total_time%", 430, 642, 420, 22, "#9A9484", 12, 8, alignment="center"),
    ]


def pulse() -> list[Source]:
    """Bold colour-block layout with a beating cover and a large mirrored visualizer."""
    return [
        _background("#12121A"),
        _panel("Color block", 0, 0, 468, 720, "#FF3366", 0, 0),
        _panel("Accent block", 0, 520, 468, 200, "#1F1F2E", 1, 0),
        replace(_cover(70, 118, 328, "#FFFFFF", 2, radius=12, animation_in="pop", animation_out="slide_left"),
                loop_motion="pulse", loop_motion_period=1.0, loop_motion_amount=0.6),
        replace(_text("Playlist", "PULSE · %track% / %track_total%", 520, 108, 700, 32, "#FF3366", 14, 3,
                      weight=800), text_letter_spacing=4.0),
        # A hard, offset pop-art shadow in the block colour.
        replace(_text("Song title", "%title%", 520, 158, 700, 132, "#FFFFFF", 52, 4, weight=800,
                      animation_in="slide_right", animation_out="slide_left"),
                text_shadow_glyph=True, shadow=Shadow(True, "#FF3366", 1, 5, 5, 1.0)),
        _text("Artist", "%artist%", 520, 320, 660, 40, "#B7B7C6", 21, 5, animation_in="fade", animation_out="fade"),
        _visualizer(520, 398, 700, 150, "#FF3366", 6, style="mirror", bars=52),
        _progress(520, 598, 700, "#FF3366", 7, style="spotify", track_color="#2A2A3A"),
        _text("Time", "%current_time% / %total_time%", 520, 624, 700, 26, "#8E8E9E", 13, 8),
    ]


def frost() -> list[Source]:
    """Frosted-glass blue layout with lyrics on the glass, a stereo meter and a start toast."""
    return [
        _background("#1C3E5A", "#2E5A7A", mode="album_art", ambient=True),
        _panel("Glass", 150, 92, 980, 532, "#DDEEFF", 0, 30, 0.16),
        replace(_cover(210, 162, 300, "#9FC4E0", 2, radius=26, frame="glass", animation_in="zoom", animation_out="fade"),
                loop_motion="float", loop_motion_period=7.0, loop_motion_amount=0.4),
        replace(_text("Playlist", "FROST · %track% / %track_total%", 560, 162, 470, 32, "#E6F3FF", 13, 3,
                      weight=700), text_letter_spacing=3.0),
        _text("Song title", "%title%", 560, 204, 480, 90, "#FFFFFF", 36, 4, weight=700, animation_in="slide_right", animation_out="slide_left"),
        _text("Artist", "%artist%  ·  %album%", 560, 298, 470, 34, "#CFE3F5", 18, 5, animation_in="slide_right", animation_out="slide_left"),
        replace(_lyrics(560, 350, 470, 150, "#CFE3F5", 6),
                font_size=19, font_weight=700, text_alignment="left", subtitle_animation="glow",
                subtitle_context_lines=1, subtitle_next_lines=1, subtitle_line_spacing=10,
                subtitle_previous_opacity=0.4, subtitle_previous_blur=1.5,
                subtitle_current_scale=1.08, subtitle_accent_enabled=True,
                subtitle_accent_color="#FFFFFF", subtitle_current_line=0),
        _meter(1058, 162, 34, 300, "#BFE0F5", 7, mode="stereo"),
        # A small toast above the glass while each track starts.
        replace(_now_playing(490, 16, 300, 64, "#2E5A7A", 8, style="glass", seconds=2.6, exit_style="slide_up"),
                font_size=14, now_playing_align="center"),
        _progress(210, 544, 820, "#FFFFFF", 9, style="apple", track_color="#BFE0F5"),
        _text("Time", "%current_time% / %total_time%", 210, 570, 820, 26, "#CFE3F5", 13, 10, alignment="center"),
    ]


def vinyl() -> list[Source]:
    """A spinning record: circular cover inside a radial visualizer ring."""
    ring = _visualizer(400, 40, 480, 480, "#F5D0FE", 3, style="radial", bars=48)
    ring.visualizer_inner_radius = 0.56
    ring.visualizer_line_width = 5.0
    cover_size = 480 * 0.56 * 0.92
    cover = _cover(640 - cover_size / 2, 280 - cover_size / 2, cover_size, "#1E1B2E", 4,
                   frame="circle", animation_in="spin", animation_out="zoom_out")
    cover.loop_motion = "spin"
    cover.loop_motion_period = 14.0
    # The mask keeps the placeholder (no artwork yet) round as well.
    cover.mask_shape = "circle"
    title = replace(_text("Song title", "%title%", 240, 532, 800, 60, "#FFFFFF", 32, 5,
                          alignment="center", weight=800, animation_in="rise", animation_out="fade"),
                    text_letter_spacing=1.0, text_shadow_glyph=True, shadow=_glow("#F5D0FE", 16, 0.5))
    return [
        _background("#1E1B2E", mode="album_art", ambient=True),
        _particles("#F5D0FE", -1, style="dust", density=26, speed=0.35, opacity=0.18),
        ring,
        cover,
        title,
        _text("Artist", "%artist%", 240, 594, 800, 32, "#E9D5FF", 17, 6,
              alignment="center", animation_in="rise", animation_out="fade"),
        _progress(440, 650, 400, "#F5D0FE", 7, style="apple", track_color="#4C3D66"),
    ]


def _glow(color: str, blur: float = 20.0, opacity: float = 0.95) -> Shadow:
    """A glyph-following glow: a centred (zero-offset) shadow."""
    return Shadow(True, color, blur, 0.0, 0.0, opacity)


def karaoke() -> list[Source]:
    """Lyrics-first stage: big glowing lines with a timed yellow word fill."""
    return [
        replace(_background("#120A1F", mode="album_art", ambient=True), brightness=-38),
        _particles("#FFE066", -1, style="bokeh", density=18, speed=0.3, opacity=0.16),
        replace(_cover(70, 50, 88, "#FFE066", 2, frame="circle", animation_in="pop", animation_out="fade"),
                shadow=Shadow(), mask_shape="circle", loop_motion="spin", loop_motion_period=18.0),
        _text("Song title", "%title%", 176, 54, 760, 42, "#FFFFFF", 22, 3, weight=800,
              animation_in="slide_right", animation_out="fade"),
        _text("Artist", "%artist%", 176, 94, 760, 30, "#E9D8FF", 15, 4,
              animation_in="slide_right", animation_out="fade"),
        replace(_lyrics(120, 168, 1040, 380, "#FFFFFF", 5),
                font_size=34, font_weight=800, subtitle_animation="glow",
                subtitle_animation_duration=0.4, subtitle_context_lines=1, subtitle_next_lines=1,
                subtitle_line_spacing=18, subtitle_previous_opacity=0.32, subtitle_previous_blur=2.0,
                subtitle_current_scale=1.16, subtitle_accent_enabled=True, subtitle_karaoke_enabled=True,
                subtitle_accent_color="#FFE066", subtitle_current_line=0,
                text_shadow_glyph=True, shadow=Shadow(True, "#000000", 14, 0, 2, 0.55)),
        _progress(240, 628, 800, "#FFE066", 6, style="apple", track_color="#3B2A55"),
        _text("Time", "%current_time%  /  %total_time%", 240, 650, 800, 24, "#CDBDE6", 12, 7,
              alignment="center"),
    ]


def cinema() -> list[Source]:
    """Letterboxed film frame with outlined subtitles on the lower edge."""
    return [
        replace(_background("#0B0B0E", mode="album_art", ambient=True), brightness=-22),
        _panel("Top bar", 0, 0, 1280, 78, "#000000", 0, 0),
        _panel("Bottom bar", 0, 642, 1280, 78, "#000000", 1, 0),
        replace(_cover(490, 118, 300, "#2A2A30", 2, radius=6, animation_in="zoom_out", animation_out="fade"),
                loop_motion="breathe", loop_motion_period=8.0, loop_motion_amount=0.8),
        replace(_text("Title", "%title%", 60, 21, 760, 36, "#F5F5F5", 15, 3, weight=700),
                text_letter_spacing=3.0, text_case="upper"),
        _text("Time", "%current_time% / %total_time%", 820, 21, 400, 36, "#9A9AA2", 13, 4,
              alignment="right"),
        replace(_lyrics(140, 456, 1000, 150, "#FFFFFF", 5),
                font_size=30, font_weight=600, subtitle_animation="rise",
                subtitle_animation_duration=0.25, subtitle_context_lines=0, subtitle_next_lines=0,
                subtitle_line_spacing=8, subtitle_current_line=0,
                text_stroke_width=2.5, text_stroke_color="#000000",
                text_shadow_glyph=True, shadow=Shadow(True, "#000000", 10, 0, 3, 0.7)),
        _text("Artist", "%artist%  —  %album%", 140, 664, 1000, 30, "#B8B8C0", 14, 6,
              alignment="center"),
    ]


def poster() -> list[Source]:
    """Editorial print layout: arched cover, serif type and left-aligned lyrics."""
    serif = "Georgia"
    return [
        _background("#F3EDE2"),
        replace(_cover(96, 150, 380, "#C9B79C", 2, radius=0, animation_in="rise", animation_out="fade"),
                mask_shape="arch", shadow=Shadow(),
                loop_motion="float", loop_motion_period=9.0, loop_motion_amount=0.5),
        replace(_text("Label", "NOW LISTENING  ·  %track% / %track_total%", 540, 150, 640, 26,
                      "#9C3D2E", 12, 3, weight=700), text_letter_spacing=3.0),
        replace(_text("Song title", "%title%", 540, 180, 660, 70, "#221A14", 40, 4, weight=700,
                      animation_in="rise", animation_out="fade"), font_family=serif),
        replace(_text("Artist", "%artist%", 540, 250, 660, 32, "#6B5A48", 17, 5,
                      animation_in="rise", animation_out="fade"), font_family=serif, text_italic=True),
        _panel("Rule", 540, 296, 640, 2, "#CBBFAE", 6, 0),
        replace(_lyrics(540, 316, 640, 250, "#3A2E24", 7),
                font_family=serif, font_size=22, font_weight=600, text_alignment="left",
                subtitle_animation="rise", subtitle_context_lines=1, subtitle_next_lines=2,
                subtitle_line_spacing=12, subtitle_previous_opacity=0.28,
                subtitle_previous_blur=0.5, subtitle_current_scale=1.1,
                subtitle_accent_enabled=True, subtitle_accent_color="#9C3D2E",
                subtitle_current_line=0),
        replace(_progress(540, 600, 640, "#221A14", 8, track_color="#DDD2C0"), height=4),
        _text("Time", "%current_time% / %total_time%", 540, 614, 640, 24, "#8A7A66", 12, 9,
              alignment="right"),
    ]


def flow() -> list[Source]:
    """Streaming-app lyrics view: floating cover on the left, lyrics filling the right."""
    return [
        _background("#1B1F2E", mode="album_art", ambient=True),
        replace(_cover(110, 140, 340, "#3A3F55", 2, radius=18, animation_in="zoom", animation_out="fade"),
                loop_motion="float", loop_motion_period=7.0, loop_motion_amount=0.6),
        _text("Song title", "%title%", 110, 500, 340, 40, "#FFFFFF", 22, 3, weight=700,
              animation_in="rise", animation_out="fade"),
        _text("Artist", "%artist%", 110, 540, 340, 30, "#D6DAE6", 16, 4,
              animation_in="rise", animation_out="fade"),
        replace(_progress(110, 590, 340, "#FFFFFF", 5, style="apple", track_color="#5A6078"), height=6),
        _text("Time", "%current_time%", 110, 606, 170, 22, "#AEB4C6", 12, 6),
        _text("Total", "%total_time%", 280, 606, 170, 22, "#AEB4C6", 12, 6, alignment="right"),
        replace(_lyrics(520, 80, 700, 560, "#FFFFFF", 7),
                font_size=26, font_weight=800, text_alignment="left", subtitle_animation="glow",
                subtitle_animation_duration=0.42, subtitle_context_lines=-1, subtitle_next_lines=-1,
                subtitle_line_spacing=20, subtitle_previous_opacity=0.3, subtitle_previous_blur=2.5,
                subtitle_current_scale=1.06, subtitle_current_line=0),
    ]


def neon() -> list[Source]:
    """Night-club neon: glowing type, cyan current lyric and a mirrored visualizer."""
    pink, cyan = "#FF3DCB", "#4DF3FF"
    return [
        _background("#07051A", "#1B0930"),
        _particles(cyan, -1, style="dust", density=30, speed=0.45, opacity=0.25),
        replace(_text("Song title", "%title%", 140, 64, 1000, 72, "#FFE9FA", 40, 3,
                      alignment="center", weight=800, animation_in="flip", animation_out="fade"),
                text_case="upper", text_letter_spacing=4.0, text_shadow_glyph=True,
                shadow=_glow(pink, 24)),
        replace(_text("Artist", "%artist%", 140, 138, 1000, 32, "#BFFBFF", 15, 4,
                      alignment="center", animation_in="fade", animation_out="fade"),
                text_case="upper", text_letter_spacing=6.0, text_shadow_glyph=True,
                shadow=_glow(cyan, 14, 0.8)),
        replace(_lyrics(160, 200, 960, 270, "#F6E9FF", 5),
                font_size=30, font_weight=700, subtitle_animation="glow",
                subtitle_context_lines=1, subtitle_next_lines=1, subtitle_line_spacing=16,
                subtitle_previous_blur=1.5, subtitle_current_scale=1.14,
                subtitle_accent_enabled=True, subtitle_accent_color=cyan, subtitle_current_line=0,
                text_shadow_glyph=True, shadow=_glow(pink, 16, 0.7)),
        _visualizer(140, 498, 1000, 96, pink, 6, style="mirror", bars=64),
        replace(_progress(340, 622, 600, cyan, 7, style="rounded", track_color="#2A1745"),
                height=14, progress_knob="circle"),
        _text("Time", "%current_time% / %total_time%", 340, 648, 600, 24, "#B9A6D8", 12, 8,
              alignment="center"),
    ]


def pastel() -> list[Source]:
    """Soft pastel dream: heart-masked cover, gradient title and a track-start card."""
    return [
        _background("#FDE7F3", "#E5E9FF"),
        _particles("#FFFFFF", 0, style="bokeh", density=20, speed=0.3, opacity=0.5),
        replace(_cover(150, 170, 330, "#F9B4D6", 2, radius=0, animation_in="bounce", animation_out="zoom_out"),
                mask_shape="heart", shadow=Shadow(True, "#C77DA8", 20, 0, 10, 0.3),
                loop_motion="float", loop_motion_period=5.0, loop_motion_amount=0.8),
        replace(_now_playing(560, 150, 520, 110, "#FFFFFF", 3, style="card", seconds=4.0,
                             exit_style="slide_up"),
                outline_color="#7A4E8C", font_size=22, now_playing_label="♪ 지금 재생 중"),
        # The text colour must stay opaque: the glyph gradient inherits its alpha.
        replace(_text("Song title", "%title%", 560, 288, 600, 80, "#FF6FB5", 42, 4, weight=800,
                      animation_in="rise", animation_out="fade"),
                text_gradient=True, gradient=Gradient(True, "#FF6FB5", "#8B7BFF")),
        _text("Artist", "%artist%", 560, 368, 600, 34, "#9A7BB0", 18, 5,
              animation_in="rise", animation_out="fade"),
        replace(_visualizer(560, 426, 520, 72, "#FF9CCB", 6, style="capsule", bars=24),
                loop_motion="breathe", loop_motion_period=4.0),
        replace(_progress(560, 540, 520, "#FF8FC4", 7, style="apple", track_color="#F3D5E8"),
                progress_knob="circle"),
        _text("Time", "%current_time% / %total_time%", 560, 566, 520, 24, "#9A7BB0", 13, 8),
    ]


class PresetService:
    """Exposes the track-aware visual preset catalog."""

    _presets = [
        PresetDefinition("aurora", "오로라", "Aurora", "청록–보라 그라데이션과 중앙 대형 커버, 미러 파형", "Teal-violet gradient with a centred cover and mirrored waveform", aurora),
        PresetDefinition("cassette", "카세트", "Cassette", "크림·브라운 레트로 카세트와 스크롤 트랙 목록", "Retro cream-and-brown mixtape with a scrolling track list", cassette),
        PresetDefinition("midnight", "미드나잇", "Midnight", "딥네이비 타이포 중심 화면과 조용한 2줄 가사, 얇은 진행 표시줄", "Deep-navy type-first screen with quiet two-line lyrics and a hairline progress bar", midnight),
        PresetDefinition("bubblegum", "버블검", "Bubblegum", "파스텔 핑크·민트 카드와 그라데이션 제목, 캡슐 비주얼라이저", "Pastel pink-and-mint card with a gradient title and a capsule visualizer", bubblegum),
        PresetDefinition("noir", "느와르", "Noir", "흑백 시네마 레터박스와 세리프 자막, 로우어서드 타이포", "Black-and-white letterbox with serif subtitles and a lower third", noir),
        PresetDefinition("sunset", "선셋", "Sunset", "오렌지→퍼플 하늘과 빛나는 태양 커버, 한 줄 가사", "Orange-to-purple sky with a glowing sun cover and a one-line lyric", sunset),
        PresetDefinition("terminal", "터미널", "Terminal", "CRT 그린 모노스페이스 콘솔과 스펙트럼, 콘솔 출력형 가사", "CRT-green monospace console with a spectrum and lyrics printed as output", terminal),
        PresetDefinition("gallery", "갤러리", "Gallery", "화이트 갤러리 벽의 액자형 커버와 캡션, 인용구 같은 가사", "Framed cover on a white gallery wall with a caption and a quoted lyric", gallery),
        PresetDefinition("pulse", "펄스", "Pulse", "볼드 컬러블록과 박동하는 커버, 대형 미러 비주얼라이저", "Bold colour-block layout with a beating cover and a large mirrored visualizer", pulse),
        PresetDefinition("vinyl", "바이닐", "Vinyl", "원형 비주얼라이저 안에서 도는 원형 앨범 커버", "A spinning circular cover inside a radial visualizer ring", vinyl),
        PresetDefinition("frost", "프로스트", "Frost", "프로스트 글래스 위의 가사와 스테레오 레벨미터, 곡 시작 알림", "Lyrics on frosted glass with a stereo meter and a track-start toast", frost),
        PresetDefinition("karaoke", "노래방", "Karaoke", "크게 빛나는 가사와 단어별 노란 강조, 앨범 아트 배경", "Big glowing lyrics with yellow word highlighting over the album art", karaoke),
        PresetDefinition("flow", "플로우", "Flow", "떠 있는 커버와 오른쪽을 채우는 스트리밍 앱 스타일 가사", "Streaming-app lyrics view beside a floating cover", flow),
        PresetDefinition("cinema", "시네마 자막", "Cinema", "레터박스 영화 화면과 테두리 있는 하단 자막", "Letterboxed film frame with outlined subtitles", cinema),
        PresetDefinition("poster", "리릭 포스터", "Lyric Poster", "아치형 커버와 세리프 타이포, 왼쪽 정렬 가사의 에디토리얼 레이아웃", "Editorial layout with an arched cover, serif type and left-aligned lyrics", poster),
        PresetDefinition("neon", "네온 나이트", "Neon Night", "빛나는 네온 타이포와 시안 현재 가사, 미러 비주얼라이저", "Glowing neon type, a cyan current lyric and a mirrored visualizer", neon),
        PresetDefinition("pastel", "파스텔 드림", "Pastel Dream", "하트 마스크 커버와 그라데이션 제목, 곡 시작 카드", "Heart-masked cover, gradient title and a track-start card", pastel),
    ]

    @classmethod
    def all(cls) -> list[PresetDefinition]:
        """Return all supported preset definitions."""
        return list(cls._presets)
