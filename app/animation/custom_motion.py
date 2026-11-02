"""Short, reusable entrance/exit motions in normalized time and relative poses."""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from math import isfinite

from app.animation.curves import AnimationPose
from app.animation.keyframes import DEFAULT_BEZIER, INTERPOLATIONS, Keyframe, evaluate_curve, normalize_bezier

MOTION_FIELDS = {"dx": (-100000, 100000, 0), "dy": (-100000, 100000, 0),
                 "scale": (.01, 10, 1), "rotation": (-36000, 36000, 0), "opacity": (0, 1, 1)}


@dataclass(frozen=True, slots=True)
class MotionPoint:
    progress: float
    dx: float = 0.0
    dy: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0
    opacity: float = 1.0
    interpolation: str = "ease_in_out"
    bezier: tuple[float, float, float, float] = DEFAULT_BEZIER


@dataclass(frozen=True, slots=True)
class MotionCurve:
    field: str
    keys: tuple[Keyframe, ...]
    custom_segments: tuple[float, ...] = ()
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class CustomMotion:
    points: tuple[MotionPoint, ...]
    curves: tuple[MotionCurve, ...] = ()

    @classmethod
    def default(cls, entering=True):
        return cls((MotionPoint(0, opacity=0 if entering else 1), MotionPoint(1, opacity=1 if entering else 0)))

    @classmethod
    def from_dict(cls, data, entering=True):
        if isinstance(data, cls):
            return data
        points = data.get("points", []) if isinstance(data, dict) else []
        if not isinstance(points, (list, tuple)):
            points = []
        valid = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            progress = point.get("progress")
            try:
                if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not isfinite(progress) or not 0 <= progress <= 1:
                    continue
                values = {}
                for name, (low, high, default) in MOTION_FIELDS.items():
                    value = point.get(name, default)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                        raise ValueError("Invalid motion value")
                    values[name] = min(high, max(low, float(value)))
                t = round(float(progress), 6)
                easing = point.get("interpolation", "ease_in_out")
                valid[t] = MotionPoint(t, **values,
                    interpolation=easing if easing in INTERPOLATIONS else "ease_in_out",
                    bezier=normalize_bezier(point.get("bezier", DEFAULT_BEZIER)))
            except (ValueError, OverflowError):
                continue
        if not valid:
            valid = {p.progress: p for p in cls.default(entering).points}
        # Endpoints are mandatory; preserve valid user poses when repairing a file.
        ordered = sorted(valid.values(), key=lambda point: point.progress)
        valid.setdefault(0.0, replace(ordered[0], progress=0))
        valid.setdefault(1.0, replace(ordered[-1], progress=1))
        motion = cls(tuple(sorted(valid.values(), key=lambda point: point.progress)))
        curves = data.get("curves", []) if isinstance(data, dict) else []
        if not isinstance(curves, (list, tuple)):
            curves = []
        for raw in curves:
            if not isinstance(raw, dict) or raw.get("field") not in MOTION_FIELDS:
                continue
            name, keys = raw["field"], {}
            low, high, _ = MOTION_FIELDS[name]
            entries = raw.get("keys", [])
            if not isinstance(entries, (list, tuple)):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                t, value = item.get("time"), item.get("value")
                try:
                    invalid = any(isinstance(v, bool) or not isinstance(v, (float, int)) or not isfinite(v) for v in (t, value))
                except OverflowError:
                    invalid = True
                if invalid:
                    continue
                if not 0 <= t <= 1:
                    continue
                t = round(t, 6)
                easing = item.get("interpolation", "ease_in_out")
                keys[t] = Keyframe(t, min(high, max(low, value)),
                    easing if easing in INTERPOLATIONS else "ease_in_out", normalize_bezier(item.get("bezier", DEFAULT_BEZIER)))
            if not keys:
                continue
            ordered = sorted(keys.values(), key=lambda k: k.time)
            keys.setdefault(0, replace(ordered[0], time=0))
            keys.setdefault(1, replace(ordered[-1], time=1))
            custom = raw.get("custom_segments", ())
            custom = tuple(sorted({t for t in custom if isinstance(t, (float, int)) and not isinstance(t, bool)
                and t in keys and t < 1})) if isinstance(custom, (list, tuple)) else ()
            motion = motion.with_curve(name, tuple(sorted(keys.values(), key=lambda k: k.time)), custom)
            motion = motion.set_field_enabled(name, raw.get("enabled") is not False)
        return motion

    def keys(self, name):
        return next((c.keys for c in self.curves if c.field == name), motion_curves(self)[name])

    def field_enabled(self, name):
        return next((c.enabled for c in self.curves if c.field == name), True)

    def set_field_enabled(self, name, enabled):
        marks = next((c.custom_segments for c in self.curves if c.field == name), ())
        result = self.with_curve(name, self.keys(name), marks)
        return replace(result, curves=tuple(replace(c, enabled=bool(enabled)) if c.field == name else c for c in result.curves))

    def with_curve(self, name, keys, custom_segments=()):
        curves = {c.field: c for c in self.curves}
        curves[name] = MotionCurve(name, tuple(keys), tuple(custom_segments), self.field_enabled(name))
        return replace(self, curves=tuple(curves[n] for n in MOTION_FIELDS if n in curves))

    def with_key(self, name, progress, *, custom=None, **changes):
        progress = round(min(1, max(0, progress)), 6)
        keys = {k.time: k for k in self.keys(name)}
        key = keys.get(progress, Keyframe(progress, getattr(self.at(progress), name)))
        keys[progress] = replace(key, **changes)
        marks = set(next((c.custom_segments for c in self.curves if c.field == name), ()))
        if custom is True:
            marks.add(progress)
        elif custom is False:
            marks.discard(progress)
        return self.with_curve(name, sorted(keys.values(), key=lambda k: k.time), sorted(marks))

    def without_key(self, name, progress):
        if progress in (0, 1):
            return self
        marks = next((c.custom_segments for c in self.curves if c.field == name), ())
        return self.with_curve(name, [k for k in self.keys(name) if k.time != progress], [t for t in marks if t != progress])

    def move_key(self, name, old, new):
        if old in (0, 1) or not 0 < new < 1 or any(abs(k.time-new) <= 1e-6 for k in self.keys(name)):
            return self
        keys = [replace(k, time=new) if k.time == old else k for k in self.keys(name)]
        marks = next((c.custom_segments for c in self.curves if c.field == name), ())
        return self.with_curve(name, sorted(keys, key=lambda k: k.time), sorted(new if t == old else t for t in marks))

    def segment(self, name, progress):
        keys = self.keys(name)
        if not 0 <= progress <= 1:
            return None
        for first, following in zip(keys, keys[1:]):
            if first.time <= progress < following.time:
                return first, following
        return (keys[-2], keys[-1]) if len(keys) >= 2 else None

    def at(self, progress):
        values = {name: min(high, max(low, evaluate_curve(curves[name], progress, default)))
                  for name, (low, high, default) in MOTION_FIELDS.items()
                  for curves in (motion_curves(self),)}
        return AnimationPose(**values)

    def with_point(self, progress, **changes):
        progress = round(min(1, max(0, progress)), 6)
        existing = next((p for p in self.points if p.progress == progress), None)
        pose = self.at(progress)
        values = {name: getattr(pose, name) for name in MOTION_FIELDS}
        point = replace(existing, **values) if existing else MotionPoint(progress, **values)
        point = replace(point, **changes)
        data = {p.progress: p for p in self.points}
        data[progress] = point
        result = replace(self, points=tuple(sorted(data.values(), key=lambda p: p.progress)))
        for curve in self.curves:
            updates = {"value": getattr(point, curve.field)}
            updates.update({k: v for k, v in changes.items() if k in {"interpolation", "bezier"}})
            result = result.with_key(curve.field, progress, **updates)
        return result

    def without_point(self, progress):
        if progress in (0, 1):
            return self
        result = replace(self, points=tuple(p for p in self.points if p.progress != progress))
        for curve in self.curves:
            result = result.without_key(curve.field, progress)
        return result


@lru_cache(maxsize=256)
def motion_curves(motion):
    result = {name: tuple(Keyframe(p.progress, getattr(p, name), p.interpolation, p.bezier) for p in motion.points)
              for name in MOTION_FIELDS}
    result.update({curve.field: curve.keys if curve.enabled else
        (Keyframe(0, MOTION_FIELDS[curve.field][2]), Keyframe(1, MOTION_FIELDS[curve.field][2])) for curve in motion.curves})
    return result


def has_custom_motion(source):
    return source.animation_in == "custom" or source.animation_out == "custom"


def source_motion(source, progress, entering):
    from app.animation.curves import animation_pose
    style = source.animation_in if entering else source.animation_out
    if style == "custom":
        motion = source.custom_intro_keyframes if entering else source.custom_outro_keyframes
        return (motion or CustomMotion.default(entering)).at(progress)
    return animation_pose(style, progress, entering, source.width, source.height)
