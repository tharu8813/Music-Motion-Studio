"""Playhead scheduling for private motion-editor video items, using live decoders."""
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from app.video.timeline import resolve_video_position, source_video_paths
from app.video.preview_proxy import PreviewProxyCache


class MotionPreviewMedia(QObject):
    changed = Signal()

    def __init__(self, items, durations, parent):
        super().__init__(parent)
        self.items = tuple(items)
        self.durations = dict(durations)
        self.queue = []
        self.worker = None
        self.closed = False
        self.last_requests = {}
        self.proxy_cache = PreviewProxyCache()
        self.proxies = {}
        for item in self.items:
            item.video_frame_ready.connect(self.changed)

    def sync(self, track, local, global_time, track_start, *, playing=False, enabled=True, scale=1, clock_rate=1):
        if self.closed:
            return
        for item in self.items:
            source = item.source
            item.set_video_preview_budget(scale, 30)
            item.set_video_preview_playback(playing and enabled, source.video_speed*clock_rate)
            if not enabled:
                continue  # Keep the last frame as the explicit still fallback.
            if not source.visible or not item.isVisible():
                item.set_video_preview_position(None)
                continue
            outside = global_time < source.timeline_start or (source.timeline_duration > 0 and
                global_time >= source.timeline_start + source.timeline_duration)
            paths = source_video_paths(source, track.video_paths)
            for path in paths:
                if path not in self.durations and path not in self.queue and (self.worker is None or self.worker.path != path):
                    if Path(path).is_file():
                        self.queue.append(path)
                    else:
                        self.durations[path] = 0
            elapsed = global_time - max(track_start, source.timeline_start) if source.video_timing_mode == "track" else global_time-source.timeline_start
            position = None if outside else resolve_video_position(source, paths, self.durations, elapsed)
            # While a duration is being probed, retain the seeded poster. Once
            # known, the production scheduler owns loop/once/rate decisions.
            if not outside and paths and any(path not in self.durations for path in paths):
                continue
            if not outside and paths and all(self.durations.get(path, 0) <= 0 for path in paths):
                item.set_video_preview_playback(False, source.video_speed)
                continue  # Unreadable metadata keeps the poster/placeholder.
            request = (position.path, position.seconds) if position else (None, 0)
            previous = self.last_requests.get(source.id)
            force = previous != request and (not playing or previous is None or request[0] != previous[0] or request[1] < previous[1])
            path = request[0]
            if path:
                if path not in self.proxies:
                    cached = self.proxy_cache.cached_path(Path(path))
                    self.proxies[path] = str(cached) if cached is not None else path
                path = self.proxies[path]
            item.set_video_preview_position(path, request[1], force_seek=force, playback_rate=source.video_speed*clock_rate)
            self.last_requests[source.id] = request
        self.start_probe()

    def start_probe(self):
        if self.closed or self.worker is not None or not self.queue:
            return
        from app.dialogs.export_preview_dialog import VideoDurationProbeWorker
        self.worker = VideoDurationProbeWorker(self.queue.pop(0), self)
        self.worker.ready.connect(self.probed)
        self.worker.finished.connect(self.probe_finished)
        self.worker.start()

    def probed(self, path, duration):
        if not self.closed:
            self.durations[path] = duration
            self.changed.emit()

    def probe_finished(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()
        self.start_probe()

    def pause(self):
        if self.closed:
            return
        for item in self.items:
            item.set_video_preview_playback(False, item.source.video_speed)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.queue.clear()
        if self.worker is not None:
            worker, self.worker = self.worker, None
            worker.requestInterruption()
            worker.ready.disconnect(self.probed)
            worker.finished.disconnect(self.probe_finished)
            # FFprobe has a bounded subprocess timeout. Closing the editor
            # must neither wait for it nor destroy a running QThread.
            worker.setParent(QApplication.instance())
            worker.finished.connect(worker.deleteLater)
        for item in self.items:
            item.video_frame_ready.disconnect(self.changed)
            item.release_video_decoder()
