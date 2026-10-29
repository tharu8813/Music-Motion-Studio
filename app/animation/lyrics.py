"""Lyric motion and role defaults shared by the renderer and its editor."""

from math import pi, sin
from typing import NamedTuple
from app.animation.curves import clamp_progress, ease_in_out_cubic, ease_out_quint
from app.models.source import Source

LYRIC_EFFECT_LABELS = {
    "glow": ("글로우", "Glow"), "rise": ("라이즈", "Rise"),
    "crossfade": ("페이드", "Fade"), "slide": ("슬라이드", "Slide"),
    "zoom": ("줌", "Zoom"), "cascade": ("순차 이동", "Staggered flow"),
    "bounce": ("바운스", "Bounce"), "none": ("없음", "None"),
}

INTRO_STYLE_LABELS = {
    "dots": ("세 점 · 차례로 밝아짐", "Dots · sequential light"),
    "breathing": ("세 점 · 숨쉬기", "Dots · breathing"),
    "wave": ("세 점 · 웨이브", "Dots · wave"),
    "bars": ("리듬 막대", "Rhythm bars"),
    "ring": ("회전 링", "Spinning ring"),
    "orbit": ("공전하는 점", "Orbiting dots"),
    "ripple": ("퍼지는 파동", "Ripples"),
    "comet": ("흐르는 빛", "Comet trail"),
}

INTRO_EXIT_LABELS = {
    "shrink": ("부드럽게 축소", "Soft shrink"),
    "fade": ("서서히 사라짐", "Fade out"),
    "drift": ("흘러 사라짐", "Drift away"),
    "spread": ("퍼지며 사라짐", "Spread out"),
    "wipe": ("차례로 지워짐", "Wipe away"),
}


class IntroExitPose(NamedTuple):
    opacity: float = 1.0
    scale: float = 1.0
    travel: float = 0.0
    spread: float = 1.0
    reveal: float = 1.0


def intro_exit_pose(kind: str, progress: float) -> IntroExitPose:
    """A monotone exit, independent of the lyric's bounce/glow pulse."""
    amount = ease_in_out_cubic(clamp_progress(progress))
    return IntroExitPose(
        opacity=1 - amount,
        scale=1 - .25 * amount if kind == "shrink" else 1.0,
        travel=-.35 * amount if kind == "drift" else 0.0,
        spread=1 + .8 * amount if kind == "spread" else 1.0,
        reveal=1 - amount if kind == "wipe" else 1.0,
    )


def lyric_progress(source: Source, progress: float) -> float:
    t = clamp_progress(progress)
    easing = source.subtitle_motion_easing
    if easing == "auto":
        easing = "ease_out" if source.subtitle_animation in {"rise", "zoom", "bounce"} else (
            "linear" if source.subtitle_animation == "crossfade" else "smooth"
        )
    if easing == "ease_in":
        return t ** 3
    if easing == "ease_out":
        return ease_out_quint(t)
    return ease_in_out_cubic(t) if easing == "smooth" else t


def cue_effect_pose(source: Source, progress: float, emphasis: float, *, current=True,
                    previous=False, incoming_visible=True, transitioning=True):
    """Travel, scale, glow, reveal and opacity for both lyric and temporary rows."""
    pulse = sin(pi * progress) if transitioning else 0.0
    travel, scale, glow, reveal, opacity = 0.0, 1.0, 0.0, 0.0, 1.0
    effect = source.subtitle_animation
    if effect == "glow" and (current or previous):
        glow = source.subtitle_glow_strength * (emphasis + (pulse if current else 0.0))
        reveal = pulse * 0.35 if current and source.subtitle_glow_radius > 0 else 0.0
        scale = 1.0 - source.subtitle_zoom_amount * 0.3 * pulse if current else 1.0
    elif effect == "rise" and current and transitioning:
        travel = source.subtitle_motion_distance * (pulse if incoming_visible else 1.0 - progress)
    elif effect == "slide":
        travel = source.subtitle_motion_distance * pulse
    elif effect == "zoom" and (current or previous):
        scale = 1.0 - source.subtitle_zoom_amount * pulse
    elif effect == "bounce":
        travel = source.subtitle_motion_distance * pulse * sin(2 * pi * progress)
    elif effect == "crossfade":
        opacity = 1.0 - 0.65 * pulse
    return travel, scale, glow, reveal, opacity


def role_style(source: Source, role: str) -> dict[str, object]:
    current = role == "current"
    defaults = {
        "color": source.subtitle_accent_color if current and source.subtitle_accent_enabled else source.outline_color,
        "scale": source.subtitle_current_scale if current else 1.0,
        "opacity": 1.0 if current else source.subtitle_previous_opacity,
        "blur": source.subtitle_previous_blur if role == "previous" else 0.0,
        "font_weight": source.font_weight,
        "italic": source.text_italic,
    }
    return defaults | {key: value for key, value in source.subtitle_role_styles.get(role, {}).items()
                       if key in {"scale", "opacity", "blur"}}


def stagger_progress(source: Source, raw: float, slot: int, first: int, last: int) -> float:
    rank = last - slot if source.subtitle_stagger_order == "far_first" else slot - first
    delay = source.subtitle_stagger * (rank + 1) / max(1, last - first + 1)
    return lyric_progress(source, (raw - delay) / (1.0 - delay))


def context_style_at_distance(style: dict, current: dict, distance: float,
                              span: int, strength: float) -> dict:
    """Near cues approach current opacity and zero blur; far cues keep their style."""
    amount = 1.0 - strength * (
        1.0 - clamp_progress(distance / max(1, span))
    )
    return style | {
        "blur": float(style["blur"]) * amount,
        "opacity": float(current["opacity"]) + (float(style["opacity"]) - float(current["opacity"])) * amount,
    }
