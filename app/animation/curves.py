"""Shared motion curves and geometry for Canvas preview and video export."""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, exp, pi, sin


def clamp_progress(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def ease_out_quint(value: float) -> float:
    """Fast response with a long, soft landing for entrances."""
    progress = clamp_progress(value)
    return 1.0 - (1.0 - progress) ** 5


def ease_out_cubic(value: float) -> float:
    """Balanced reveal used for lyric changes where quint feels too abrupt."""
    progress = clamp_progress(value)
    return 1.0 - (1.0 - progress) ** 3


def ease_in_quint(value: float) -> float:
    """Soft departure that accelerates naturally toward the end."""
    progress = clamp_progress(value)
    return progress ** 5


def ease_in_out_cubic(value: float) -> float:
    """Balanced motion that stays visible throughout an exit transition."""
    progress = clamp_progress(value)
    if progress < 0.5:
        return 4.0 * progress ** 3
    return 1.0 - ((-2.0 * progress + 2.0) ** 3) / 2.0


def ease_out_back(value: float, overshoot: float = 1.9) -> float:
    """Overshoots past the target, then settles back onto it."""
    progress = clamp_progress(value) - 1.0
    return 1.0 + (overshoot + 1.0) * progress ** 3 + overshoot * progress ** 2


def ease_out_bounce(value: float) -> float:
    """Lands, then bounces twice with shrinking height."""
    progress = clamp_progress(value)
    if progress < 1 / 2.75:
        return 7.5625 * progress ** 2
    if progress < 2 / 2.75:
        progress -= 1.5 / 2.75
        return 7.5625 * progress ** 2 + 0.75
    if progress < 2.5 / 2.75:
        progress -= 2.25 / 2.75
        return 7.5625 * progress ** 2 + 0.9375
    progress -= 2.625 / 2.75
    return 7.5625 * progress ** 2 + 0.984375


def slide_distance(width: float, height: float) -> float:
    """Return restrained travel that scales without flying across the Canvas."""
    return min(112.0, max(28.0, max(float(width), float(height)) * 0.095))


# Entrance/exit styles, in the order the Inspector lists them.
ANIMATION_STYLES = (
    "none", "fade", "slide_left", "slide_right", "slide_up", "slide_down",
    "zoom", "zoom_out", "pop", "bounce", "rise", "drop", "rotate", "spin",
    "swing", "flip",
)

_SLIDE_STYLES = frozenset(
    {"slide_left", "slide_right", "slide_up", "slide_down"}
)
# Styles whose opacity snaps in fast so their motion reads as the effect.
_QUICK_REVEAL = frozenset({"pop", "bounce", "drop", "spin", "swing", "flip"})


@dataclass(frozen=True, slots=True)
class AnimationPose:
    """An offset from a source's resting placement at one moment.

    Offsets and rotation are added, scales multiply. ``scale_x`` squeezes the
    source horizontally around its centre (the flip). Every consumer -- Canvas
    capture, the editor preview and the reactive export layers -- applies one
    of these, so a style looks the same everywhere.
    """

    dx: float = 0.0
    dy: float = 0.0
    scale: float = 1.0
    scale_x: float = 1.0
    rotation: float = 0.0
    opacity: float = 1.0

    def combined(self, other: "AnimationPose") -> "AnimationPose":
        return AnimationPose(
            self.dx + other.dx, self.dy + other.dy, self.scale * other.scale,
            self.scale_x * other.scale_x, self.rotation + other.rotation,
            self.opacity * other.opacity,
        )

    @property
    def is_identity(self) -> bool:
        return self == AnimationPose()


def entrance_opacity(style: str, progress: float) -> float:
    """Opacity 0..1 while a source appears, shaped to match its motion style."""
    progress = clamp_progress(progress)
    if style in _SLIDE_STYLES or style == "rise":
        # Become readable quickly, then let the slide finish underneath.
        return progress ** 0.6
    if style in {"zoom", "zoom_out"}:
        # Grows into place, so the reveal trails the scale slightly.
        return progress ** 1.6
    if style in _QUICK_REVEAL:
        # Snap in almost instantly for a punchy arrival.
        return progress ** 0.35
    if style == "rotate":
        # Steady reveal while it unwinds.
        return progress
    return ease_in_out_cubic(progress)


def exit_opacity(style: str, progress: float) -> float:
    """Remaining opacity 1..0 while a source leaves, shaped per motion style.

    Every curve is monotonically decreasing and reaches near-zero well before
    the end, so no style snaps out on the final frame; they differ only in how
    the fade is weighted across the exit.
    """
    progress = clamp_progress(progress)
    if style in _SLIDE_STYLES or style in {"rise", "drop"}:
        # Stay legible as it travels, then fade over the second half.
        return (1.0 - progress) ** 1.3
    if style in {"zoom", "zoom_out"}:
        # Dissolve quickly and early while it shrinks away.
        return (1.0 - progress) ** 3.0
    if style in {"pop", "bounce", "flip"}:
        # Hold, then drop out for a punchy exit.
        if progress < 0.5:
            return 1.0
        return 1.0 - ((progress - 0.5) / 0.5) ** 1.5
    if style in {"rotate", "spin", "swing"}:
        # Spin away at a constant, linear fade.
        return 1.0 - progress
    return 1.0 - ease_in_out_cubic(progress)


def animation_pose(
    style: str, progress: float, entering: bool, width: float, height: float,
) -> AnimationPose:
    """The pose of an entrance (``entering``) or exit at raw ``progress`` 0..1."""
    if style == "none":
        return AnimationPose()
    progress = clamp_progress(progress)
    opacity = (
        entrance_opacity(style, progress) if entering else exit_opacity(style, progress)
    )
    # 1 = at rest, 0 = fully hidden; entrances land softly, exits accelerate.
    shown = ease_out_quint(progress) if entering else 1.0 - ease_in_quint(progress)
    hidden = 1.0 - shown
    distance = slide_distance(width, height)
    if style in _SLIDE_STYLES:
        dx, dy = {
            "slide_left": (-distance, 0.0), "slide_right": (distance, 0.0),
            "slide_up": (0.0, -distance), "slide_down": (0.0, distance),
        }[style]
        return AnimationPose(dx * hidden, dy * hidden, opacity=opacity)
    if style == "zoom":
        return AnimationPose(scale=0.90 + 0.10 * shown, opacity=opacity)
    if style == "zoom_out":
        return AnimationPose(scale=1.0 + 0.18 * hidden, opacity=opacity)
    if style == "pop":
        return AnimationPose(scale=0.82 + 0.18 * shown, opacity=opacity)
    if style == "rotate":
        return AnimationPose(
            scale=0.96 + 0.04 * shown, rotation=(-12.0 if entering else 12.0) * hidden,
            opacity=opacity,
        )
    if style == "rise":
        return AnimationPose(
            dy=distance * 0.6 * hidden, scale=1.0 - 0.08 * hidden, opacity=opacity,
        )
    if style == "bounce":
        # Springs past full size and settles; the exit simply shrinks away.
        scale = 0.5 + 0.5 * (ease_out_back(progress) if entering else shown)
        return AnimationPose(scale=max(0.01, scale), opacity=opacity)
    if style == "drop":
        if entering:
            return AnimationPose(
                dy=-distance * 1.4 * (1.0 - ease_out_bounce(progress)), opacity=opacity,
            )
        return AnimationPose(dy=distance * 1.4 * progress ** 2, opacity=opacity)
    if style == "spin":
        return AnimationPose(
            scale=0.6 + 0.4 * shown, rotation=(-360.0 if entering else 360.0) * hidden,
            opacity=opacity,
        )
    if style == "swing":
        if entering:
            # A damped pendulum that comes to rest upright.
            rotation = 22.0 * exp(-4.5 * progress) * cos(3.0 * pi * progress) * (1.0 - progress)
        else:
            rotation = -16.0 * progress ** 2
        return AnimationPose(rotation=rotation, opacity=opacity)
    if style == "flip":
        # A card turning on its vertical axis.
        return AnimationPose(scale_x=max(0.01, cos(hidden * pi / 2.0)), opacity=opacity)
    return AnimationPose(opacity=opacity)  # fade and unknown styles


def loop_pose(
    motion: str, seconds: float, period: float, amount: float,
    width: float, height: float,
) -> AnimationPose:
    """The pose of a repeating idle motion at playback time ``seconds``."""
    if motion == "none" or amount <= 0.0:
        return AnimationPose()
    cycles = float(seconds) / max(0.1, float(period))
    phase = 2.0 * pi * cycles
    if motion == "float":
        return AnimationPose(dy=-amount * max(4.0, height * 0.04) * sin(phase))
    if motion == "breathe":
        return AnimationPose(scale=1.0 + 0.035 * amount * (0.5 - 0.5 * cos(phase)))
    if motion == "pulse":
        # A heartbeat: a quick swell at the start of each cycle, then rest.
        beat = exp(-8.0 * (cycles % 1.0))
        return AnimationPose(scale=1.0 + 0.06 * amount * beat)
    if motion == "sway":
        return AnimationPose(rotation=4.0 * amount * sin(phase))
    if motion == "spin":
        # One steady turn per period; the amount only switches it on.
        return AnimationPose(rotation=360.0 * (cycles % 1.0))
    if motion == "drift":
        return AnimationPose(
            dx=amount * max(6.0, width * 0.02) * sin(phase),
            dy=amount * max(4.0, height * 0.02) * 0.5 * sin(2.0 * phase),
        )
    if motion == "wobble":
        return AnimationPose(
            rotation=2.5 * amount * sin(2.0 * phase),
            scale=1.0 + 0.02 * amount * sin(phase),
        )
    if motion == "orbit":
        return AnimationPose(
            dx=amount * max(6.0, width * 0.04) * sin(phase),
            dy=amount * max(4.0, height * 0.04) * cos(phase),
        )
    if motion == "shake":
        return AnimationPose(dx=amount * max(4.0, width * 0.015) * sin(3.0 * phase))
    if motion == "nod":
        return AnimationPose(
            dy=amount * max(4.0, height * 0.04) * (0.5 - 0.5 * cos(phase)),
            rotation=3.0 * amount * sin(phase),
        )
    if motion == "blink":
        return AnimationPose(opacity=1.0 - min(1.0, amount * 0.5) * (0.5 - 0.5 * cos(phase)))
    return AnimationPose()


def music_reactive_pose(effect: str, level: float, strength: float,
                        font_size: float) -> AnimationPose:
    """Deterministic text pose from the shared fast-attack, slow-release bass envelope."""
    amount = clamp_progress(level) * strength
    if effect == "bass_scale":
        return AnimationPose(scale=1.0 + amount)
    if effect == "bass_bounce":
        return AnimationPose(dy=-font_size * 0.6 * amount)
    if effect == "bass_stretch":
        return AnimationPose(scale_x=1.0 + amount)
    if effect == "bass_tilt":
        return AnimationPose(rotation=-10.0 * amount)
    return AnimationPose()


def motion_padding(styles: set[str] | frozenset[str], loop: str, loop_amount: float,
                   width: float, height: float) -> float:
    """How far a source can travel outside its resting box while animating."""
    distance = slide_distance(width, height)
    diagonal = (width ** 2 + height ** 2) ** 0.5
    padding = 0.0
    if styles & _SLIDE_STYLES:
        padding = max(padding, distance)
    if styles & {"rise"}:
        padding = max(padding, distance * 0.6)
    if styles & {"drop"}:
        padding = max(padding, distance * 1.4)
    if styles & {"zoom_out", "bounce"}:
        padding = max(padding, diagonal * 0.18)
    if styles & {"rotate", "spin", "swing"}:
        padding = max(padding, diagonal * 0.22 if styles & {"rotate", "swing"} else diagonal * 0.5)
    if loop != "none" and loop_amount > 0.0:
        padding = max(padding, diagonal * (0.5 if loop == "spin" else 0.06 * loop_amount)
                      + max(6.0, width * 0.02, height * 0.04) * loop_amount)
    return padding
