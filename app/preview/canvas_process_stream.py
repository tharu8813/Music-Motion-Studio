"""Render independent Canvas bands in their own Qt processes and video pipes."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from queue import Empty, Full
from time import monotonic

from app.renderer.canvas_pipe import CanvasPipeCancelledError, CanvasPipeError, PipedCanvasStream


def parallel_canvas_supported(capturer, stream_count: int) -> bool:
    from app.canvas.live_canvas import CanvasScene
    from app.canvas.source_item import SourceItem

    if not isinstance(getattr(capturer, 'scene', None), CanvasScene):
        return False
    if not 2 <= stream_count <= min(4, max(1, (os.cpu_count() or 1) // 2)):
        return False
    if capturer.playlist_duration < 5.0:
        return False
    # shortcut: equal-Z or transient Qt poses keep serial capture until pixel equivalence is proven.
    z_values = set()
    for item in capturer.scene.items():
        if not isinstance(item, SourceItem) or not item.isVisible() or not item.source.visible:
            continue
        source = item.source
        if (source.z_index in z_values or item.zValue() != source.z_index
                or item.x() != source.x or item.y() != source.y
                or item.scale() != source.scale or item.rotation() != source.rotation
                or item.opacity() != 1.0 or not item.transform().isIdentity()
                or item.transformOriginPoint().x() != source.width / 2
                or item.transformOriginPoint().y() != source.height / 2):
            return False
        z_values.add(source.z_index)
    return True


def _render_band(jobs, replies, stop, status) -> None:
    """Only this process touches its scene; images go straight to FFmpeg."""
    pipe = None
    application = None
    scene = None
    try:
        # Large analysis arrays use the queue feeder, keeping process.start() off the GUI hot path.
        while not stop.is_set():
            try:
                config = jobs.get(timeout=0.05)
                break
            except Empty:
                continue
        else:
            return
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QApplication
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.models.source import Source
        from app.preview.export_canvas_capture import ExportCanvasCapturer
        from app.renderer.ffmpeg_renderer import RenderFrame
        from app.ui.design_system import apply_studio_style
        from app.utils.font_loader import load_application_font

        application = QApplication([])
        apply_studio_style(application)
        for path in config['fonts']:
            load_application_font(path)
        scene = CanvasScene()
        scene.set_artboard_size(config['rect'][2], config['rect'][3])
        scene.artboard_rect = QRectF(*config['rect'])
        scene.placeholder_labels_korean = config['korean']
        for name, color in config['colors'].items():
            setattr(scene, name, QColor.fromRgba(color))
        # Scene.items() is top-first; restore bottom-first for equal-Z stacking.
        for source in reversed(config['sources']):
            scene.addItem(SourceItem(Source.from_dict(source)))

        def check():
            if stop.is_set():
                raise CanvasPipeCancelledError('Canvas rendering was cancelled.')

        def update_status():
            status[:] = (pipe.last_activity, pipe.connected, pipe.reader_closed, pipe.pending_frames)
            check()

        pipe = PipedCanvasStream(
            Path(config['directory']), config['key'], config['fps'],
            preserve_alpha=config['key'] != 'base', queue_capacity=2,
            producer_cancel_event=stop, producer_wait_callback=update_status,
        )

        def stage(image, duration, _key):
            pipe.submit(image, duration)
            return RenderFrame(image, duration)

        capturer = ExportCanvasCapturer(
            scene, config['tracks'], config['duration'], config['dynamic'], config['bands'],
            stage, check, lambda *_: None, retain_static_frames=False,
            output_scale=config['scale'], bass_envelopes=config['bass'],
            bass_fps=config['bass_fps'], output_fps=config['fps'],
            instrumental_spans_by_track=config['instrumental'],
        )
        ready = False
        while True:
            update_status()
            try:
                sample = jobs.get(timeout=0.05)
            except Empty:
                continue
            if sample is None:
                result = pipe.finish()
                update_status()
                metrics = {name: getattr(capturer, name) for name in (
                    'partial_render_capture_count', 'scene_render_source_pixels',
                    'full_frame_source_pixels', 'skipped_cfr_captures',
                )}
                replies.put(('done', (result, metrics)))
                return
            capturer.capture_stream(sample, config['key'])
            update_status()
            if not ready:
                replies.put(('ready', (pipe.width, pipe.height, pipe.input_arguments())))
                ready = True
    except BaseException as error:
        replies.put(('error', f'{type(error).__name__}: {error}'))
    finally:
        if pipe is not None:
            pipe.cancel()
        if scene is not None:
            scene.clear()


class CanvasProcessStream:
    """Bounded sample queue to one renderer, with the existing pipe diagnostics."""

    def __init__(self, directory, stream_key, fps, *, capturer,
                 producer_cancel_event, producer_wait_callback) -> None:
        from app.canvas.source_item import SourceItem
        from app.models.source import SourceType
        from app.services.lyrics_intro_service import instrumental_spans
        from app.utils.font_loader import application_font_paths

        self.stream_key = stream_key
        self.width = self.height = 0
        self._arguments = None
        self._result = None
        self.capture_metrics = {}
        self._cancel = producer_cancel_event
        self._wait_callback = producer_wait_callback
        self._closed = False
        context = multiprocessing.get_context('spawn')
        self._jobs = context.Queue(maxsize=2)
        self._jobs._ignore_epipe = True  # A cancelled reader can close during a queued write.
        self._replies = context.Queue()
        self._stop = context.Event()
        self._status = context.Array('d', (monotonic(), 0, 0, 0))
        scene = capturer.scene
        sources = [item.source.to_dict() | {'visible': item.isVisible() and item.source.visible}
                   for item in scene.items() if isinstance(item, SourceItem)]
        config = dict(
            directory=str(directory), key=stream_key, fps=fps,
            rect=scene.artboard_rect.getRect(), korean=scene.placeholder_labels_korean,
            colors={name: getattr(scene, name).rgba() for name in (
                'workspace_color', 'artboard_color', 'artboard_border_color',
            )}, fonts=application_font_paths(), sources=sources,
            tracks=capturer.tracks, duration=capturer.playlist_duration,
            dynamic=capturer.dynamic_source_ids, bands=capturer.z_bands,
            scale=capturer.output_scale, bass=capturer.bass_envelopes, bass_fps=capturer.bass_fps,
            instrumental=(capturer.instrumental_spans_by_track
                          if capturer.instrumental_spans_by_track is not None else {
                track.id: {item.source.id: instrumental_spans(track, item.source)
                           for item in scene.items() if isinstance(item, SourceItem)
                           and item.source.source_type is SourceType.LYRICS and item.isVisible()}
                for track in capturer.tracks
            }),
        )
        self._process = context.Process(
            target=_render_band, args=(self._jobs, self._replies, self._stop, self._status),
            name=f'canvas-render-{stream_key}', daemon=True,
        )
        try:
            self._process.start()
            # Only the child reads jobs; retaining a reader here strands the feeder after a crash.
            self._jobs._reader.close()
            self._jobs.put(config)
        except Exception as error:
            self.cancel()
            raise CanvasPipeError(f'Could not start Canvas worker: {error}') from error

    @property
    def started(self):
        return self._arguments is not None

    @property
    def connected(self):
        return bool(self._status[1])

    @property
    def reader_closed(self):
        return bool(self._status[2])

    @property
    def last_activity(self):
        return self._status[0]

    @property
    def pending_frames(self):
        try:
            queued = self._jobs.qsize() if not self._closed else 0
        except NotImplementedError:
            queued = 0
        return int(self._status[3]) + queued

    def diagnostics(self):
        return dict(process=self._process.pid, connected=self.connected,
                    pending=self.pending_frames, idle_seconds=monotonic() - self.last_activity)

    def _poll(self):
        exit_code = self._process.exitcode
        while True:
            try:
                kind, data = self._replies.get(timeout=0.05 if exit_code is not None else 0)
            except Empty:
                # The worker may flush its final reply between the empty read and exit check.
                stopped = self._process.exitcode
                if exit_code is None and stopped is not None:
                    exit_code = stopped
                    continue
                break
            if kind == 'error':
                raise CanvasPipeError(f'Canvas worker {self.stream_key}: {data}')
            if kind == 'ready':
                self.width, self.height, self._arguments = data
            elif kind == 'done':
                self._result, self.capture_metrics = data
        if exit_code is not None and self._result is None:
            raise CanvasPipeError(
                f'Canvas worker {self.stream_key} stopped (exit {exit_code}).'
            )

    def _check(self):
        if self._closed or self._cancel.is_set():
            raise CanvasPipeCancelledError('Canvas rendering was cancelled.')
        self._poll()

    def _wait(self):
        self._check()
        self._wait_callback()
        self._check()

    def submit_sample(self, sample):
        self._check()
        while True:
            try:
                self._jobs.put(sample, timeout=0.05)
                break
            except Full:
                self._wait()
        deadline = monotonic() + 30.0
        while not self.started:
            self._wait()
            if monotonic() >= deadline:
                raise CanvasPipeError(f'Canvas worker {self.stream_key} took too long to start.')
            self._process.join(0.01)

    def input_arguments(self):
        if not self.started:
            raise CanvasPipeError('Canvas worker has no frames yet.')
        return list(self._arguments)

    def finish(self):
        self.submit_sample(None)
        while self._result is None:
            self._wait()
            self._process.join(0.01)
        result = self._result
        self.cancel()
        return result

    def cancel(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._process.pid is not None:
            self._process.join(2.0)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(2.0)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(2.0)
        for queue in (self._jobs, self._replies):
            queue.cancel_join_thread()
            queue.close()
        if self._jobs._thread is not None:
            self._jobs._thread.join(2.0)
