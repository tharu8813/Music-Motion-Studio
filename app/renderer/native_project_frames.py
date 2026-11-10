"""Complete project native frames. Qt scenes and iterators stay on the GUI thread.

Workers consume GuiProjectFrameSource: a pull requests exactly one immutable
BGRA packet, with no speculative frame production or GUI-owned Qt objects.
The GUI event loop must remain running until close has been delivered.
"""
from __future__ import annotations
from collections.abc import Iterator
from contextlib import ExitStack
from copy import deepcopy
import math
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt
from PySide6.QtWidgets import QApplication
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.project import ProjectDocument
from app.preview.native_canvas_layer_packets import NativeCanvasFrame, capture_native_canvas_track_frame
from app.renderer.ffmpeg_renderer import RenderError, RenderCancelledError
from app.renderer.native_dynamic_layers import (
    DYNAMIC_TYPES, analyze_reactive_canvas_tracks, build_dynamic_stack,
    canvas_reaction_at, interleave_dynamic_layers, real_audio_dynamic_frames,
    prepare_project_frame_audio, PreparedProjectFrameAudio,
)
from app.renderer.native_project_support import (
    TimelineValidationError, resolve_project_frame, validate_project_for_gpu_capture,
)
from app.timeline.compiler import compile_playlist
from app.renderer.native_layer_cache import NativeRasterCache, split_resident_bands

def _check_cancel(cancel):
    if cancel.is_set():
        raise RenderCancelledError("Native GPU export was cancelled.")

def produce_complete_project_frames(
    project: ProjectDocument, *, fps: int, scale: float,
    prepared_audio: Path, ffmpeg: str, cancel_event: Event | None = None,
    analysis: PreparedProjectFrameAudio | None = None,
    raster_counters: dict | None = None,
) -> Iterator[NativeCanvasFrame]:
    """Yield *every* playlist CFR frame lazily in true source Z order.

    Unlike the diagnostic project probe, this function does not create
    reference PNGs, packet JSONs, an in-memory frame array or a 30-frame
    cap. The Rust transport takes each completed packet one at a time.
    """
    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise RenderError("Native Canvas frames must be produced on the QApplication GUI thread.")
    cancel = cancel_event or Event()
    project = deepcopy(project)
    if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 120:
        raise TimelineValidationError("Full native export FPS must be 1..120")
    _check_cancel(cancel)
    validate_project_for_gpu_capture(
        project, scale=scale, source_policy="dynamic-audio",
    )
    plan = compile_playlist(project.playlist, enabled_only=True)
    tracks = [t for t in project.playlist if t.enabled and t.duration_seconds > 0]
    if not tracks or plan.duration_seconds <= 0:
        raise TimelineValidationError("A complete native export needs enabled playlist tracks")
    stack = build_dynamic_stack(project, scale)
    included = [
        s for s in project.sources
        if s.visible and s.source_type not in DYNAMIC_TYPES
    ]
    reactive = [s for s in included if s.uses_bass_reaction]
    scene = CanvasScene()
    raster_cache = None
    try:
        scene.set_artboard_size(project.canvas.width, project.canvas.height)
        scene.show_grid = False
        for source in sorted(included, key=lambda item: item.z_index):
            scene.addItem(SourceItem(source))
        stack = split_resident_bands(scene, stack, plan.duration_seconds)
        raster_cache = NativeRasterCache(scene, stack.bands, plan.duration_seconds)
        before = project.to_dict()
        with ExitStack() as resources:
            overlays, dynamic_streams, _origin = resources.enter_context(
                real_audio_dynamic_frames(
                    project, plan, fps=fps, scale=scale, start_seconds=0.0,
                    prepared_audio=prepared_audio, ffmpeg=ffmpeg, cancel_event=cancel, analysis=analysis,
                )
            )
            if len(overlays) != len(stack.source_ids):
                raise TimelineValidationError("Dynamic overlay count differs from original project")
            envelopes = analysis.envelopes if analysis is not None else analyze_reactive_canvas_tracks(
                project, tracks, fps=fps, ffmpeg=ffmpeg, cancel_event=cancel,
            ) if reactive else {}
            total_frames = math.ceil(plan.duration_seconds * fps - 1e-9)
            for frame_index in range(total_frames):
                _check_cancel(cancel)
                state = resolve_project_frame(project, frame_index / fps, plan=plan)
                bass, music = canvas_reaction_at(
                    state["track"], state["track_elapsed_seconds"], fps,
                    reactive, envelopes,
                )
                static = capture_native_canvas_track_frame(
                    scene, state["track"], stack.bands,
                    elapsed_seconds=state["track_elapsed_seconds"],
                    output_scale=scale,
                    track_number=state["track_number"],
                    track_total=len(state["tracks"]),
                    track_start_seconds=state["window_start_seconds"],
                    timeline_seconds=state["timeline_seconds"],
                    playlist_duration_seconds=plan.duration_seconds,
                    playlist_tracks=state["tracks"],
                    animation_phase=state["animation_phase"],
                    animation_progress=state["animation_progress"],
                    animation_phase_duration=state["animation_phase_duration"],
                    compiled_plan=plan,
                    bass_level=bass,
                    music_levels=music,
                    raster_cache=raster_cache,
                )
                try:
                    layers = [
                        (overlay, next(stream))
                        for overlay, stream in zip(overlays, dynamic_streams, strict=True)
                    ]
                except StopIteration as exc:
                    raise TimelineValidationError(
                        f"Audio-reactive overlay ended at frame {frame_index}/{total_frames}"
                    ) from exc
                _check_cancel(cancel)
                if raster_counters is not None:
                    raster_counters.update(
                        rasterizations=raster_cache.rasterizations,
                        reuse_hits=raster_cache.hits,
                        retained_bytes=raster_cache.retained_bytes,
                        peak_retained_bytes=raster_cache.peak_retained_bytes,
                        advanced_lyric_cache_entries=len(raster_cache.frozen_lyric_settings_cache),
                        partial_band_enabled=raster_cache.partial_band_enabled,
                        partial_band_captures=raster_cache.partial_band_captures,
                        scene_render_source_pixels=raster_cache.scene_render_source_pixels,
                        full_frame_source_pixels=raster_cache.full_frame_source_pixels,
                    )
                yield interleave_dynamic_layers(
                    static, stack, layers, seconds=state["timeline_seconds"],
                    output_scale=scale, gpu_affine=True,
                )
            if project.to_dict() != before:
                raise TimelineValidationError("Native export unexpectedly modified project sources")
    except Exception:
        _check_cancel(cancel)
        raise
    finally:
        if raster_cache is not None:
            raster_cache.clear()
            if raster_counters is not None:
                raster_counters["retained_bytes"] = 0
        scene.clear()


class GuiProjectFrameSource(QObject):
    """Create on GUI thread, prepare/consume/close from a single worker.

    No frame is produced until next() is called. At most one request/packet is
    in flight; consumers must not call next concurrently. close is nonblocking
    from a worker; keep this QObject alive until its closed signal is emitted.
    Never join a worker from the GUI while it is awaiting a packet.
    """

    requested = Signal()
    close_requested = Signal()
    closed = Signal()

    def __init__(self, project, *, fps, scale, cancel_event=None, request_timeout=90.0):
        app = QApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RenderError("Create GuiProjectFrameSource on the GUI thread.")
        super().__init__()
        if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 120:
            raise TimelineValidationError("Full native export FPS must be 1..120")
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("Frame request timeout must be positive and finite.")
        validate_project_for_gpu_capture(project, scale=scale, source_policy="dynamic-audio")
        build_dynamic_stack(project, scale)
        self.project = deepcopy(project)
        self.fps, self.scale = fps, scale
        self.cancel_event = cancel_event or Event()
        self.request_timeout = request_timeout
        self._ready = Event()
        self._closing = Event()
        self._lock = Lock()
        self._iterator = None
        self._audio = None
        self._ffmpeg = None
        self._analysis = None
        self.raster_counters = {}
        self._result = None
        self._error = None
        self._closed = False
        self.requested.connect(self._produce, Qt.ConnectionType.QueuedConnection)
        self.close_requested.connect(self._close_gui, Qt.ConnectionType.QueuedConnection)

    def prepare(self, prepared_audio, ffmpeg):
        """Set plain values before worker consumption begins."""
        if self._iterator is not None or self._closed:
            raise RenderError("Native frame source is already started or closed.")
        self._audio, self._ffmpeg = Path(prepared_audio), str(ffmpeg)
        _check_cancel(self.cancel_event)
        try:
            self._analysis = prepare_project_frame_audio(
                self.project, compile_playlist(self.project.playlist, enabled_only=True),
                fps=self.fps, prepared_audio=self._audio, ffmpeg=self._ffmpeg,
                cancel_event=self.cancel_event,
            )
        except Exception:
            _check_cancel(self.cancel_event)
            raise

    def __iter__(self):
        return self

    def __next__(self):
        if QThread.currentThread() == self.thread():
            raise RenderError("Consume GuiProjectFrameSource from the render worker.")
        if not self._lock.acquire(blocking=False):
            raise RenderError("Concurrent native frame requests are unsupported.")
        try:
            _check_cancel(self.cancel_event)
            if self._closed:
                raise StopIteration
            self._result = self._error = None
            self._ready.clear()
            self.requested.emit()
            deadline = monotonic() + self.request_timeout
            while not self._ready.wait(0.05):
                _check_cancel(self.cancel_event)
                if monotonic() >= deadline:
                    self.close()
                    raise RenderError("Timed out waiting for the GUI to produce a native frame.")
            _check_cancel(self.cancel_event)
            if self._error is not None:
                raise self._error
            result, self._result = self._result, None
            if result is None:
                raise StopIteration
            return result
        finally:
            self._lock.release()

    @Slot()
    def _produce(self):
        try:
            _check_cancel(self.cancel_event)
            if self._closed or self._closing.is_set():
                self._close_gui()
                return
            if self._audio is None:
                raise RenderError("Prepare native frame source audio before consumption.")
            if self._iterator is None:
                self._iterator = produce_complete_project_frames(
                    self.project, fps=self.fps, scale=self.scale,
                    prepared_audio=self._audio, ffmpeg=self._ffmpeg,
                    cancel_event=self.cancel_event,
                    analysis=self._analysis,
                    raster_counters=self.raster_counters,
                )
            self._result = next(self._iterator)
        except StopIteration:
            self._close_gui()
        except Exception as error:
            self._error = error
            self._close_gui()
        finally:
            self._ready.set()

    def close(self):
        self._closing.set()
        if QThread.currentThread() == self.thread():
            self._close_gui()
        else:
            self.close_requested.emit()

    @Slot()
    def _close_gui(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._iterator is not None:
                self._iterator.close()
        finally:
            self._iterator = None
            self._result = None
            self._analysis = None
            self._ready.set()
            self.closed.emit()
