"""Editing policy shared by the inspector and the dedicated timeline view."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from app.animation.keyframes import Keyframe, MODES, PROPERTIES, evaluate, point_at, put_point, property_value, supports_property


class KeyframeController(QObject):
    changed = Signal()
    position_changed = Signal(float)
    editor_requested = Signal()
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
        self._keyframe_windows = ()
        store.selection_set_changed.connect(lambda *_: self.changed.emit())
        store.source_changed.connect(lambda *_: self.changed.emit())
        store.sources_replaced.connect(self.changed.emit)

    @property
    def source(self):
        return self.store.selected

    @property
    def windows(self):
        entries = list(self.tracks_provider())
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
                return min(self.duration, max(0, window.source_time_at_start + (self.global_seconds - window.timeline_start) * window.playback_rate))
            return min(self.duration, max(0.0, self.global_seconds - self.window[1])) if self.window else 0.0
        return self.global_seconds

    @property
    def duration(self):
        if self.source and self.source.keyframes.mode != "global":
            return self.window[0].duration_seconds if self.window else 0.0
        return max((end for _, _, end in self.windows), default=60.0)

    @property
    def editable(self):
        return bool(self.source and not self.source.locked and len(self.store.selected_ids) == 1
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
            local = min(self.duration, max(0.0, seconds))
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
        animation = deepcopy(self.source.keyframes)
        curves = animation.curves(self.track_id, create=True)
        point = point_at(curves.get(name, []), self.time)
        if point:
            curves[name] = [entry for entry in curves[name] if entry != point]
        else:
            put_point(curves, name, Keyframe(self.time, self.evaluated().get(name, property_value(self.source, name))))
        self._commit(animation)

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
        animation = deepcopy(self.source.keyframes)
        put_point(animation.curves(self.track_id, create=True), name,
                  replace(point, value=value) if point else Keyframe(self.time, value))
        self._commit(animation)
        return True

    def navigate(self, direction, name=None):
        times = sorted({p.time for key, points in self.curves().items()
                        if name is None or key == name for p in points})
        candidates = [time for time in times if (time - self.time) * direction > 1e-6]
        if candidates:
            self.seek(min(candidates) if direction > 0 else max(candidates))

    def replace_points(self, selected, replacements=()):
        if not self.editable:
            return
        animation = deepcopy(self.source.keyframes)
        curves = animation.curves(self.track_id, create=True)
        for name, time in selected:
            curves[name] = [p for p in curves.get(name, []) if abs(p.time - time) > 1e-6]
        occupied = {(name, point.time) for name, points in curves.items() for point in points}
        for name, point in replacements:
            if any(key == name and abs(time - point.time) <= 1e-6 for key, time in occupied):
                self.changed.emit()
                self.edit_rejected.emit()
                return
            occupied.add((name, point.time))
            put_point(curves, name, point)
        self._commit(animation)

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
