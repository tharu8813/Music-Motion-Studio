"""Serializable, random-access numeric animation. No Qt or frame-rate dependency."""

from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass, field, replace
from math import isfinite, sqrt


MODES = ("global", "single_track", "per_track")
INTERPOLATIONS = ("hold", "linear", "ease_in", "ease_out", "ease_in_out", "bezier")
TIME_EPSILON = 1e-6
DEFAULT_BEZIER = (.25, .1, .25, 1.0)
BEZIER_ITERATIONS = 28
CANVAS_TYPES = ("image", "text", "shape", "progress_bar", "time", "album_cover", "logo",
                "watermark", "background", "lyrics", "track_list", "now_playing")


@dataclass(frozen=True, slots=True)
class AnimatedProperty:
    key: str
    labels: tuple[str, str]
    minimum: float
    maximum: float
    step: float
    source_types: tuple[str, ...] = ()


# One registry for validation, the inspector, and timeline lanes.
PROPERTIES = {
    spec.key: spec for spec in (
        AnimatedProperty("x", ("위치 X", "Position X"), -100000, 100000, 1),
        AnimatedProperty("y", ("위치 Y", "Position Y"), -100000, 100000, 1),
        AnimatedProperty("scale", ("크기", "Scale"), .01, 10, .01),
        AnimatedProperty("rotation", ("회전", "Rotation"), -36000, 36000, 1),
        AnimatedProperty("opacity", ("투명도", "Opacity"), 0, 1, .01),
        AnimatedProperty("width", ("너비", "Width"), 32, 5000, 1, CANVAS_TYPES),
        AnimatedProperty("height", ("높이", "Height"), 24, 5000, 1, CANVAS_TYPES),
        AnimatedProperty("blur", ("블러", "Blur"), 0, 40, .1, ("image", "background", "album_cover", "logo", "watermark")),
        AnimatedProperty("font_size", ("글자 크기", "Font size"), 8, 120, 1, ("text", "time", "lyrics", "track_list", "now_playing")),
        AnimatedProperty("border_radius", ("모서리 반경", "Corner radius"), 0, 300, 1, CANVAS_TYPES),
        AnimatedProperty("outline_width", ("테두리 두께", "Outline width"), 0, 40, .1, ("shape",)),
        AnimatedProperty("shadow.opacity", ("그림자 투명도", "Shadow opacity"), 0, 1, .01, CANVAS_TYPES),
        AnimatedProperty("shadow.blur_radius", ("그림자 흐림", "Shadow blur"), 0, 50, 1, CANVAS_TYPES),
        AnimatedProperty("shadow.offset_x", ("그림자 X", "Shadow X"), -100, 100, 1, CANVAS_TYPES),
        AnimatedProperty("shadow.offset_y", ("그림자 Y", "Shadow Y"), -100, 100, 1, CANVAS_TYPES),
    )
}


@dataclass(frozen=True, slots=True)
class Keyframe:
    time: float
    value: float
    interpolation: str = "ease_in_out"
    bezier: tuple[float, float, float, float] = DEFAULT_BEZIER


Curves = dict[str, list[Keyframe]]


def property_value(source, name):
    value = source
    for part in name.split("."):
        value = getattr(value, part)
    return value


def supports_property(source, name):
    spec = PROPERTIES.get(name)
    kind = getattr(source.source_type, "value", source.source_type)
    return bool(spec and (not spec.source_types or kind in spec.source_types)
                and not (name == "blur" and kind == "background" and (source.background_mode == "color"
                    or (source.background_mode == "album_art" and source.background_ambient))))


def properties_for(source):
    return {name: spec for name, spec in PROPERTIES.items() if source and supports_property(source, name)}


def resolved_source(source, values):
    """A transient rendering copy; the saved Source and its Shadow stay intact."""
    changes = {name: value for name, value in values.items()
               if name not in {"x", "y", "scale", "rotation", "opacity"} and "." not in name}
    shadow = {name.split(".", 1)[1]: value for name, value in values.items() if name.startswith("shadow.")}
    if shadow:
        changes["shadow"] = replace(source.shadow, **shadow)
    return replace(source, **changes) if changes else source


def normalize_bezier(value):
    if not isinstance(value, (tuple, list)) or len(value) != 4 or not all(_number(v) for v in value):
        return DEFAULT_BEZIER
    return tuple(min(1.0 if i % 2 == 0 else 3.0, max(0.0 if i % 2 == 0 else -2.0, float(v)))
                 for i, v in enumerate(value))


def cubic_coordinate(t, first, second):
    return 3 * (1 - t) ** 2 * t * first + 3 * (1 - t) * t ** 2 * second + t ** 3


def bezier_progress(progress, handles=DEFAULT_BEZIER):
    """Invert the monotone time coordinate, including flat endpoint tangents."""
    if progress <= 0 or progress >= 1:
        return min(1.0, max(0.0, progress))
    x1, y1, x2, y2 = handles
    low, high = 0.0, 1.0
    for _ in range(BEZIER_ITERATIONS):
        middle = (low + high) / 2
        if cubic_coordinate(middle, x1, x2) < progress:
            low = middle
        else:
            high = middle
    return cubic_coordinate((low + high) / 2, y1, y2)


def curve_maximum(points):
    """Include interior Bezier extrema when reserving the rotated video envelope."""
    maximum = max((point.value for point in points), default=0)
    for left, right in zip(points, points[1:]):
        if left.interpolation != "bezier":
            continue
        y1, y2 = left.bezier[1], left.bezier[3]
        a, b, c = 3 * (1 + 3 * y1 - 3 * y2), 6 * (y2 - 2 * y1), 3 * y1
        roots = []
        if abs(a) < 1e-12:
            if abs(b) > 1e-12:
                roots = [-c / b]
        elif b * b - 4 * a * c >= 0:
            delta = sqrt(b * b - 4 * a * c)
            roots = [(-b - delta) / (2 * a), (-b + delta) / (2 * a)]
        for t in roots:
            if 0 < t < 1:
                maximum = max(maximum, left.value + (right.value - left.value) * cubic_coordinate(t, y1, y2))
    return maximum


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
                handles = point.bezier
            elif isinstance(point, dict):
                time, value, easing = point.get("time"), point.get("value"), point.get("interpolation")
                handles = point.get("bezier", DEFAULT_BEZIER)
            else:
                continue
            if not _number(time) or not 0 <= time <= 8640000 or not _number(value):
                continue
            time = round(float(time), 6)
            unique[time] = Keyframe(time, min(spec.maximum, max(spec.minimum, float(value))),
                                   easing if easing in INTERPOLATIONS else "ease_in_out", normalize_bezier(handles))
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
    elif left.interpolation == "bezier":
        t = bezier_progress(t, left.bezier)
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
    values = {name: min(PROPERTIES[name].maximum, max(PROPERTIES[name].minimum,
                       evaluate_curve(points, seconds, property_value(source, name))))
              for name, points in animation.curves(track_id).items() if points and supports_property(source, name)}
    if getattr(source.source_type, "value", source.source_type) == "album_cover":
        if "width" in values:
            values["height"] = values["width"]
        elif "height" in values:
            values["width"] = values["height"]
    return values


def point_at(points: list[Keyframe], seconds: float) -> Keyframe | None:
    return next((point for point in points if abs(point.time - seconds) <= TIME_EPSILON), None)


def put_point(curves: Curves, name: str, point: Keyframe) -> None:
    """Validated insertion also resolves collisions from paste/drag deterministically."""
    curves[name] = normalize_curves({name: [p for p in curves.get(name, [])
        if abs(p.time - point.time) > TIME_EPSILON] + [point]}).get(name, [])


def transform_positions(animation: KeyframeAnimation, x_scale: float, y_scale: float,
                        dx: float = 0.0, dy: float = 0.0, *, size_scale: float | None = None) -> KeyframeAnimation:
    """Adapt every saved bank when scaling a canvas or offsetting a source copy."""
    result = deepcopy(animation)
    banks = [result.global_curves, *result.single_track_curves.values(), *result.per_track_curves.values()]
    for curves in banks:
        for name, scale, offset in (("x", x_scale, dx), ("y", y_scale, dy)):
            if name in curves:
                spec = PROPERTIES[name]
                curves[name] = [replace(p, value=min(spec.maximum, max(spec.minimum, p.value * scale + offset))) for p in curves[name]]
        if size_scale is not None:
            for name in ("width", "height", "font_size", "border_radius", "outline_width",
                         "shadow.blur_radius", "shadow.offset_x", "shadow.offset_y"):
                if name in curves:
                    spec = PROPERTIES[name]
                    curves[name] = [replace(p, value=min(spec.maximum, max(spec.minimum, p.value * size_scale)))
                                    for p in curves[name]]
    return result
