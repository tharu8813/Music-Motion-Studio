"""Editing policy shared by the inspector and the dedicated timeline view."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from app.animation.keyframes import (Keyframe, MODES, TIME_EPSILON, valid_edit_point,
    evaluate, point_at, normalize_curves, property_value, supports_property)


class KeyframeController(QObject):
    changed = Signal()
    position_changed = Signal(float)
    edit_started = Signal()
    edit_finished = Signal()
    edit_rejected = Signal()

    def __init__(self, store, tracks_provider, parent=None):
        super().__init__(parent)
        self.store = store
        self.tracks_provider = tracks_provider
        self.global_seconds = 0.0
        self.edit_track_id = ""
        self.auto_key = False
        self._clipboard = None
        self.plan = None
        self.rejection_reason = ""
        self._keyframe_windows = ()
        store.selection_set_changed.connect(lambda *_: self.changed.emit())
        store.source_changed.connect(lambda *_: self.changed.emit())
        store.sources_replaced.connect(self.changed.emit)

    @property
    def source(self):
        return self.store.selected

    @property
    def windows(self):
        entries = [(track, start, end) for track, start, end in self.tracks_provider()
                   if track.enabled and end > start and track.duration_seconds > 0]
        if self.plan is None:
            return entries
        by_id = {track.id: track for track, _, _ in entries}
        return [(by_id[w.track_id], w.timeline_start, w.timeline_end) for w in self._keyframe_windows
                if w.track_id in by_id]

    def set_plan(self, plan):
        from app.animation.keyframe_render import keyframe_windows
        self.plan = plan
        self._keyframe_windows = keyframe_windows(plan)
        self.changed.emit()

    def local_window(self, identifier):
        return next((w for w in self._keyframe_windows if w.track_id == identifier), None)

    @property
    def track_id(self):
        source = self.source
        if source and source.keyframes.mode == "single_track":
            return source.keyframes.target_track_id
        return self.edit_track_id

    @property
    def window(self):
        return next((entry for entry in self.windows if entry[0].id == self.track_id), None)

    @property
    def time(self):
        source = self.source
        if source and source.keyframes.mode != "global":
            window = self.local_window(self.track_id)
            if window:
                return min(self.duration, max(self.minimum_time, window.source_time_at_start + (self.global_seconds - window.timeline_start) * window.playback_rate))
            return min(self.duration, max(0.0, self.global_seconds - self.window[1])) if self.window else 0.0
        return self.global_seconds

    @property
    def duration(self):
        if self.source and self.source.keyframes.mode != "global":
            window = self.local_window(self.track_id)
            if window and self.window:
                return min(self.window[0].duration_seconds,
                           window.source_time_at_start + (window.timeline_end - window.timeline_start) * window.playback_rate)
            return self.window[0].duration_seconds if self.window else 0.0
        return self.plan.duration_seconds if self.plan is not None else max((end for _, _, end in self.windows), default=0.0)

    @property
    def minimum_time(self):
        window = self.local_window(self.track_id) if self.source and self.source.keyframes.mode != "global" else None
        return window.source_time_at_start if window else 0.0

    @property
    def editable(self):
        return bool(self.source and not self.source.locked and len(self.store.selected_ids) == 1
                    and self.source.animation_in != "custom" and self.source.animation_out != "custom"
                    and self.duration > self.minimum_time
                    and (self.source.keyframes.mode == "global" or self.window))

    def curves(self):
        return self.source.keyframes.curves(self.track_id) if self.source else {}

    def seek_global(self, seconds):
        if abs(self.global_seconds - max(0.0, float(seconds))) < 1e-9:
            return
        self.global_seconds = max(0.0, float(seconds))
        self.position_changed.emit(self.global_seconds)

    def seek(self, seconds):
        window = self.local_window(self.track_id)
        if self.source and self.source.keyframes.mode != "global" and window:
            local = min(self.duration, max(self.minimum_time, seconds))
            self.seek_global(window.timeline_start + (local - window.source_time_at_start) / window.playback_rate)
            return
        offset = self.window[1] if self.source and self.source.keyframes.mode != "global" and self.window else 0.0
        self.seek_global(offset + min(self.duration, max(0.0, seconds)))

    def evaluated(self, source=None):
        source = source or self.source
        if source is None:
            return {}
        active = next(((track, start, end) for track, start, end in self.windows
                       if start <= self.global_seconds < end), None)
        window = self.local_window(active[0].id) if active else None
        local = (window.source_time_at_start + (self.global_seconds - window.timeline_start) * window.playback_rate
                 if window else self.global_seconds - active[1] if active else None)
        return evaluate(source, self.global_seconds, active[0].id if active else "",
                        local)

    def edit_values(self):
        """Values in the bank being edited, independently of the Canvas owner."""
        if not self.source:
            return {}
        return evaluate(self.source, self.time, self.track_id, self.time)

    def _reject(self, reason):
        self.rejection_reason = reason
        self.changed.emit()
        self.edit_rejected.emit()
        return False

    def _commit(self, animation):
        if not self.editable or animation == self.source.keyframes:
            return
        self.edit_started.emit()
        self.store.update(self.source.id, keyframes=animation)
        self.edit_finished.emit()

    def configure(self, *, mode=None, enabled=None, target=None):
        if not self.source or self.source.locked or len(self.store.selected_ids) != 1:
            return
        animation = deepcopy(self.source.keyframes)
        if mode in MODES:
            animation.mode = mode
        if enabled is not None:
            animation.enabled = bool(enabled)
        if target is not None:
            animation.target_track_id = target
        if animation == self.source.keyframes:
            return
        # Configuration remains possible even when the chosen track was removed.
        self.edit_started.emit()
        self.store.update(self.source.id, keyframes=animation)
        self.edit_finished.emit()

    def choose_track(self, identifier):
        if self.source and self.source.keyframes.mode == "single_track":
            self.configure(target=identifier)
        else:
            self.edit_track_id = identifier
        self.changed.emit()
        if self.window:
            self.seek(0.0)
        else:
            self.changed.emit()

    def toggle(self, name):
        if not self.editable or not supports_property(self.source, name):
            return
        point = point_at(self.curves().get(name, []), self.time)
        if point:
            self.replace_points({(name, point.time)})
        else:
            self.replace_points((), [(name, Keyframe(self.time, self.edit_values().get(name, property_value(self.source, name))))])

    def edit_value(self, name, value) -> bool:
        """Auto OFF: update an existing point, otherwise the base value.

        Return False to let the ordinary (including multi-selection) binding run.
        """
        if not self.editable or not self.source.keyframes.enabled or not supports_property(self.source, name):
            return False
        points = self.curves().get(name, [])
        point = point_at(points, self.time)
        if point is None and not self.auto_key:
            return False
        self.replace_points({(name, point.time)} if point else (), [(name,
            replace(point, value=value) if point else Keyframe(self.time, value))])
        return True

    def navigate(self, direction, name=None):
        times = sorted({p.time for key, points in self.curves().items()
                        if name is None or key == name for p in points})
        candidates = [time for time in times if (time - self.time) * direction > 1e-6]
        if candidates:
            self.seek(min(candidates) if direction > 0 else max(candidates))

    def replace_points(self, selected, replacements=()):
        if not self.editable:
            return False
        selected, replacements = set(selected), list(replacements)
        # Validate the entire operation before removing any point. File loading
        # is forgiving; an interactive edit must never silently discard data.
        normalized = []
        for name, point in replacements:
            if not supports_property(self.source, name):
                return self._reject("unsupported")
            if not valid_edit_point(name, point):
                return self._reject("invalid")
            point = normalize_curves({name: [point]})[name][0]
            unchanged_time = any(key == name and abs(time - point.time) <= TIME_EPSILON
                                 and point_at(self.curves().get(name, []), time) for key, time in selected)
            if (point.time > self.duration + TIME_EPSILON or point.time < self.minimum_time - TIME_EPSILON) and not unchanged_time:
                return self._reject("range")
            normalized.append((name, point))
        animation = deepcopy(self.source.keyframes)
        curves = animation.curves(self.track_id, create=True)
        for name, time in selected:
            curves[name] = [p for p in curves.get(name, []) if abs(p.time - time) > 1e-6]
        names = {name for name, _ in normalized}
        for name, point in normalized:
            curves.setdefault(name, []).append(point)
        for name in names:
            points = sorted(curves[name], key=lambda point: point.time)
            if any(right.time - left.time <= TIME_EPSILON for left, right in zip(points, points[1:])):
                return self._reject("collision")
            curves[name] = points
        self.rejection_reason = ""
        self._commit(animation)
        return True

    def copy(self, selected=None):
        self._clipboard = [(name, deepcopy(point)) for name, points in self.curves().items()
                           for point in points if selected is None or (name, point.time) in selected]

    def paste(self, *, whole_track=False):
        if not self._clipboard or not self.editable:
            return
        offset = 0 if whole_track else self.time - min(p.time for _, p in self._clipboard)
        self.replace_points((), [(name, replace(point, time=point.time + offset))
                                 for name, point in self._clipboard])

    def clear_track(self):
        self.replace_points([(name, point.time) for name, points in self.curves().items() for point in points])
