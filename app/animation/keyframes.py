"""Serializable, random-access numeric animation. No Qt or frame-rate dependency."""

from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite


MODES = ("global", "single_track", "per_track")
INTERPOLATIONS = ("hold", "linear", "ease_in", "ease_out", "ease_in_out")
TIME_EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class AnimatedProperty:
    key: str
    labels: tuple[str, str]
    minimum: float
    maximum: float
    step: float


# One registry for validation, the inspector, and timeline lanes.
PROPERTIES = {
    spec.key: spec for spec in (
        AnimatedProperty("x", ("위치 X", "Position X"), -100000, 100000, 1),
        AnimatedProperty("y", ("위치 Y", "Position Y"), -100000, 100000, 1),
        AnimatedProperty("scale", ("크기", "Scale"), .01, 10, .01),
        AnimatedProperty("rotation", ("회전", "Rotation"), -36000, 36000, 1),
        AnimatedProperty("opacity", ("투명도", "Opacity"), 0, 1, .01),
    )
}


@dataclass(frozen=True, slots=True)
class Keyframe:
    time: float
    value: float
    interpolation: str = "ease_in_out"


Curves = dict[str, list[Keyframe]]


def _number(value) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
    except OverflowError:
        return False


def normalize_curves(data) -> Curves:
    """Discard malformed points; the last point at a duplicate time wins."""
    if not isinstance(data, dict):
        return {}
    result = {}
    for name, points in data.items():
        if name not in PROPERTIES or not isinstance(points, list):
            continue
        spec = PROPERTIES[name]
        unique = {}
        for point in points[:100000]:
            if isinstance(point, Keyframe):
                time, value, easing = point.time, point.value, point.interpolation
            elif isinstance(point, dict):
                time, value, easing = point.get("time"), point.get("value"), point.get("interpolation")
            else:
                continue
            if not _number(time) or not 0 <= time <= 8640000 or not _number(value):
                continue
            time = round(float(time), 6)
            unique[time] = Keyframe(time, min(spec.maximum, max(spec.minimum, float(value))),
                                   easing if easing in INTERPOLATIONS else "ease_in_out")
        if unique:
            result[name] = sorted(unique.values(), key=lambda point: point.time)
    return result


@dataclass(slots=True)
class KeyframeAnimation:
    enabled: bool = True
    mode: str = "global"
    target_track_id: str = ""
    global_curves: Curves = field(default_factory=dict)
    single_track_curves: dict[str, Curves] = field(default_factory=dict)
    per_track_curves: dict[str, Curves] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data) -> KeyframeAnimation:
        if isinstance(data, cls):
            return deepcopy(data)
        if not isinstance(data, dict):
            return cls()
        def tracks(key):
            value = data.get(key, {})
            return {identifier: normalize_curves(curves) for identifier, curves in value.items()
                    if isinstance(identifier, str) and identifier} if isinstance(value, dict) else {}
        return cls(data.get("enabled", True) is True,
                   data.get("mode") if data.get("mode") in MODES else "global",
                   data.get("target_track_id") if isinstance(data.get("target_track_id"), str) else "",
                   normalize_curves(data.get("global_curves")), tracks("single_track_curves"),
                   tracks("per_track_curves"))

    def curves(self, track_id: str = "", *, create: bool = False) -> Curves:
        if self.mode == "global":
            return self.global_curves
        identifier = self.target_track_id if self.mode == "single_track" else track_id
        mapping = self.single_track_curves if self.mode == "single_track" else self.per_track_curves
        if not identifier:
            return {}
        return mapping.setdefault(identifier, {}) if create else mapping.get(identifier, {})

    @property
    def animated(self) -> bool:
        if not self.enabled:
            return False
        if self.mode == "per_track":
            return any(any(curves.values()) for curves in self.per_track_curves.values())
        return any(self.curves().values())


def evaluate_curve(points: list[Keyframe], seconds: float, default: float) -> float:
    if not points or not _number(seconds):
        return default
    index = bisect_right(points, seconds, key=lambda point: point.time)
    if index == 0:
        return points[0].value
    if index == len(points):
        return points[-1].value
    left, right = points[index - 1], points[index]
    span = right.time - left.time
    t = min(1.0, max(0.0, (seconds - left.time) / span)) if span > 0 else 1.0
    if left.interpolation == "hold":
        t = 0.0
    elif left.interpolation == "ease_in":
        t = t ** 3
    elif left.interpolation == "ease_out":
        t = 1 - (1 - t) ** 3
    elif left.interpolation == "ease_in_out":
        t = 4 * t ** 3 if t < .5 else 1 - (-2 * t + 2) ** 3 / 2
    return left.value + (right.value - left.value) * t


def evaluate(source, global_seconds: float, track_id: str = "", local_seconds: float | None = None) -> dict[str, float]:
    """Inactive/missing tracks use base values; never combine two track curves."""
    animation = source.keyframes
    if not animation.enabled:
        return {}
    if animation.mode != "global" and (local_seconds is None or not track_id or
            (animation.mode == "single_track" and track_id != animation.target_track_id)):
        return {}
    seconds = global_seconds if animation.mode == "global" else local_seconds
    return {name: evaluate_curve(points, seconds, getattr(source, name))
            for name, points in animation.curves(track_id).items() if points and name in PROPERTIES}


def point_at(points: list[Keyframe], seconds: float) -> Keyframe | None:
    return next((point for point in points if abs(point.time - seconds) <= TIME_EPSILON), None)


def put_point(curves: Curves, name: str, point: Keyframe) -> None:
    """Validated insertion also resolves collisions from paste/drag deterministically."""
    curves[name] = normalize_curves({name: [p for p in curves.get(name, [])
        if abs(p.time - point.time) > TIME_EPSILON] + [point]}).get(name, [])


def transform_positions(animation: KeyframeAnimation, x_scale: float, y_scale: float,
                        dx: float = 0.0, dy: float = 0.0) -> KeyframeAnimation:
    """Adapt every saved bank when scaling a canvas or offsetting a source copy."""
    result = deepcopy(animation)
    banks = [result.global_curves, *result.single_track_curves.values(), *result.per_track_curves.values()]
    for curves in banks:
        for name, scale, offset in (("x", x_scale, dx), ("y", y_scale, dy)):
            if name in curves:
                curves[name] = [Keyframe(p.time, p.value * scale + offset, p.interpolation) for p in curves[name]]
    return result
