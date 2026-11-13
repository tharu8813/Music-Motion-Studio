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
from app.models.source import SourceType
from app.preview.native_canvas_layer_packets import (
    MAX_NATIVE_CANVAS_LAYERS, NativeCanvasFrame, capture_native_canvas_track_frame,
)
from app.renderer.ffmpeg_renderer import RenderError, RenderCancelledError
from app.renderer.native_dynamic_layers import (
    DYNAMIC_TYPES, analyze_reactive_canvas_tracks, build_dynamic_stack,
    canvas_reaction_at, interleave_dynamic_layers, real_audio_dynamic_frames,
    prepare_project_frame_audio, PreparedProjectFrameAudio,
)
from app.renderer.native_project_support import (
    TimelineValidationError, build_native_frame_lookup, resolve_project_frame,
    validate_project_for_gpu_capture,
)
from app.timeline.compiler import compile_playlist
from app.services.lyrics_intro_service import prepare_instrumental_spans
from app.renderer.native_layer_cache import NativeRasterCache, split_gpu_cover_bands, split_resident_bands
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations
from app.renderer.native_text_layers import split_gpu_text_bands
from app.renderer.native_project_video import NativeVideoSource
from app.renderer.export_timeline import ExportTimelinePlanner
from app.renderer.cfr_sample_pruning import iter_cfr_emitting_samples

def _check_cancel(cancel):
    if cancel.is_set():
        raise RenderCancelledError("Native GPU export was cancelled.")


class _ProfiledNativeSourceItem(SourceItem):
    """Developer diagnostics only: measure actual Qt item renderer calls.

    This subclass is instantiated solely for detached native CLI profile
    exports. Normal GUI, preview, and production renderer objects remain
    the unchanged SourceItem implementation. Times include Python/Qt work
    inside each item paint; scene clear/fill/composite overhead is excluded.
    """

    def __init__(self, source, paint_costs: dict[str, dict[str, int | float]],
                 text_stage_costs: dict[str, dict[str, dict[str, int | float]]] | None = None):
        super().__init__(source)
        self._native_paint_costs = paint_costs
        self._native_text_stage_costs = text_stage_costs

    def _measure_text_stage(self, stage, callable_, *args):
        # The SourceItem base constructor may call a text helper before
        # these opt-in profiling fields exist. Preserve that path exactly.
        stages = getattr(self, "_native_text_stage_costs", None)
        if stages is None:
            return callable_(*args)
        started = monotonic()
        try:
            return callable_(*args)
        finally:
            category = self.source.source_type.value
            bucket = stages.setdefault(category, {})
            cost = bucket.setdefault(stage, {"calls": 0, "seconds": 0.0})
            cost["calls"] += 1
            cost["seconds"] += monotonic() - started

    def text_font(self, size, weight):
        return self._measure_text_stage("font", super().text_font, size, weight)

    def _render_text(self):
        return self._measure_text_stage("template", super()._render_text)

    def _draw_text(self, painter, rect, flags, text):
        return self._measure_text_stage(
            "draw_text", super()._draw_text, painter, rect, flags, text,
        )

    def _text_layout_path(self, painter, rect, flags, text):
        return self._measure_text_stage(
            "layout_path", super()._text_layout_path, painter, rect, flags, text,
        )

    def _draw_glyph_shadow(self, painter, rect, flags, text, stroke):
        return self._measure_text_stage(
            "glyph_shadow", super()._draw_glyph_shadow, painter, rect, flags, text, stroke,
        )

    def paint(self, painter, option, widget=None):
        category = self.source.source_type.value
        started = monotonic()
        try:
            return super().paint(painter, option, widget)
        finally:
            elapsed = monotonic() - started
            bucket = self._native_paint_costs.setdefault(
                category, {"calls": 0, "seconds": 0.0},
            )
            bucket["calls"] += 1
            bucket["seconds"] += elapsed


def produce_complete_project_frames(
    project: ProjectDocument, *, fps: int, scale: float,
    prepared_audio: Path, ffmpeg: str, cancel_event: Event | None = None,
    analysis: PreparedProjectFrameAudio | None = None,
    instrumental_by_track: dict[str, dict[str, tuple]] | None = None,
    raster_counters: dict | None = None,
    optimizations: NativeGpuOptimizations | None = None,
    video_sources: dict[str, NativeVideoSource] | None = None,
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
    gpu_video = bool(video_sources)
    validate_project_for_gpu_capture(project, scale=scale, source_policy="dynamic-audio", gpu_video=gpu_video)
    plan = compile_playlist(project.playlist, enabled_only=True)
    tracks = [t for t in project.playlist if t.enabled and t.duration_seconds > 0]
    if not tracks or plan.duration_seconds <= 0:
        raise TimelineValidationError("A complete native export needs enabled playlist tracks")
    frame_lookup = build_native_frame_lookup(project, tracks)
    stack = build_dynamic_stack(project, scale, gpu_video=gpu_video)
    included = [
        s for s in project.sources
        if s.visible and s.source_type not in DYNAMIC_TYPES
        and (not gpu_video or s.source_type is not SourceType.VIDEO)
    ]
    reactive = [s for s in included if s.uses_bass_reaction]
    scene = CanvasScene()
    raster_cache = None
    # A per-export, bounded map indexed by enum source types, never by
    # project-specific names, IDs or contents. Only development CLI enables.
    profile_paint = bool(
        optimizations is not None and optimizations.profile_canvas_source_paint
    )
    paint_costs: dict[str, dict[str, int | float]] = {}
    text_stage_costs: dict[str, dict[str, dict[str, int | float]]] = {}
    try:
        scene.set_artboard_size(project.canvas.width, project.canvas.height)
        scene.show_grid = False
        for source in sorted(included, key=lambda item: item.z_index):
            scene.addItem(
                _ProfiledNativeSourceItem(source, paint_costs, text_stage_costs)
                if profile_paint else SourceItem(source)
            )
        layer_limit = (optimizations.canvas_layer_limit if gpu_video and optimizations is not None
                       else MAX_NATIVE_CANVAS_LAYERS)
        stack = split_resident_bands(scene, stack, plan.duration_seconds, layer_limit=layer_limit)
        schedule_bands = stack.bands
        schedule_owners = range(len(schedule_bands))
        if optimizations is not None and optimizations.gpu_album_cover_motion and len(stack.order) < layer_limit:
            stack, schedule_owners = split_gpu_cover_bands(scene, stack)
        if optimizations is not None and optimizations.gpu_text:
            stack, schedule_owners = split_gpu_text_bands(
                scene, stack, schedule_owners, optimizations.canvas_layer_limit,
            )
        raster_cache = NativeRasterCache(
            scene, stack.bands, plan.duration_seconds, optimizations=optimizations,
        )
        schedules = ExportTimelinePlanner.build_by_z_band(
            tracks, included, set(), schedule_bands, fps, plan, lazy=True,
        )
        band_schedules = [iter_cfr_emitting_samples(samples, fps) for samples in schedules.values()]
        band_samples = [next(schedule) for schedule in band_schedules]
        if instrumental_by_track is None:
            instrumental_by_track = prepare_instrumental_spans(
                tracks, (s for s in included if s.source_type is SourceType.LYRICS),
                lambda: _check_cancel(cancel),
            )
        before = project.to_dict()
        with ExitStack() as resources:
            overlays, dynamic_streams, _origin = resources.enter_context(
                real_audio_dynamic_frames(
                    project, plan, fps=fps, scale=scale, start_seconds=0.0,
                    prepared_audio=prepared_audio, ffmpeg=ffmpeg, cancel_event=cancel, analysis=analysis,
                    gpu_particles=optimizations is not None and optimizations.gpu_particles,
                    gpu_radial=optimizations is not None and optimizations.gpu_radial,
                )
            )
            video_sources = video_sources or {}
            if len(overlays) + len(video_sources) != len(stack.source_ids):
                raise TimelineValidationError("Dynamic overlay count differs from original project")
            envelopes = analysis.envelopes if analysis is not None else analyze_reactive_canvas_tracks(
                project, tracks, fps=fps, ffmpeg=ffmpeg, cancel_event=cancel,
            ) if reactive else {}
            total_frames = math.ceil(plan.duration_seconds * fps - 1e-9)
            band_states = [None] * len(band_schedules)
            for frame_index in range(total_frames):
                _check_cancel(cancel)
                state = resolve_project_frame(
                    project, frame_index / fps, plan=plan, frame_lookup=frame_lookup,
                )
                bass, music = canvas_reaction_at(
                    state["track"], state["track_elapsed_seconds"], fps,
                    reactive, envelopes,
                )
                for index, schedule in enumerate(band_schedules):
                    while band_samples[index].cfr_target_frame_count <= frame_index:
                        next_sample = next(schedule, None)
                        if next_sample is None:
                            break
                        band_samples[index] = next_sample
                    sample = band_samples[index]
                    if band_states[index] is not None and band_states[index][0] is sample:
                        continue
                    band_bass, band_music = canvas_reaction_at(
                        sample.track, sample.elapsed_seconds, fps, reactive, envelopes,
                    )
                    # A held CFR sample keeps the same evaluated state object.
                    band_states[index] = (sample, band_bass, band_music,
                                          instrumental_by_track[sample.track.id])
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
                    lyric_instrumental_spans_by_source=instrumental_by_track[state["track"].id],
                    raster_cache=raster_cache,
                    band_states=[band_states[owner] for owner in schedule_owners],
                )
                try:
                    audio_layers = iter(zip(overlays, dynamic_streams, strict=True))
                    layers = []
                    for source_id in stack.source_ids:
                        if source_id in video_sources:
                            layers.append((None, video_sources[source_id].layer_at(frame_index / fps)))
                        else:
                            overlay, stream = next(audio_layers)
                            layers.append((overlay, next(stream)))
                except StopIteration as exc:
                    raise TimelineValidationError(
                        f"Audio-reactive overlay ended at frame {frame_index}/{total_frames}"
                    ) from exc
                _check_cancel(cancel)
                frame_packet = interleave_dynamic_layers(
                    static, stack, layers, seconds=state["timeline_seconds"],
                    output_scale=scale, gpu_affine=True,
                    raster_cache=raster_cache,
                )
                yield frame_packet
            if project.to_dict() != before:
                raise TimelineValidationError("Native export unexpectedly modified project sources")
    except Exception:
        _check_cancel(cancel)
        raise
    finally:
        if raster_cache is not None:
            # Final report only: do not reconstruct 40+ diagnostic fields or
            # nested per-layer paint dictionaries for *every* exported frame.
            # Record resident bytes before clear() drops cache allocations.
            if raster_counters is not None:
                if profile_paint:
                    raster_counters["qt_canvas_source_type_paint"] = {
                        category: dict(costs)
                        for category, costs in paint_costs.items()
                    }
                    raster_counters["qt_canvas_text_stage_costs"] = {
                        category: {stage: dict(cost)
                                   for stage, cost in stages.items()}
                        for category, stages in text_stage_costs.items()
                    }
                raster_counters.update(
                    rasterizations=raster_cache.rasterizations,
                    reuse_hits=raster_cache.hits,
                    sample_reuse_hits=raster_cache.sample_reuse_hits,
                    gpu_canvas_translation_frames=raster_cache.translation_frames,
                    gpu_canvas_translation_textures=len(raster_cache.translation_textures),
                    gpu_canvas_translation_texture_bytes=raster_cache.translation_texture_bytes,
                    gpu_album_cover_frames=raster_cache.cover_motion_frames,
                    gpu_album_cover_texture_captures=raster_cache.cover_texture_captures,
                    gpu_album_cover_texture_bytes=raster_cache.cover_texture_bytes,
                    gpu_text_frames=raster_cache.text.frames,
                    gpu_text_fallbacks=raster_cache.text.fallbacks,
                    gpu_text_tile_renders=raster_cache.text.tile_renders,
                    gpu_text_atlases_built=raster_cache.text.atlas_uploads,
                    cpu_parallel_bgra_enabled=raster_cache.parallel_bgra_enabled,
                    cpu_parallel_bgra_submissions=raster_cache.parallel_bgra_submissions,
                    cpu_parallel_bgra_peak_workers=raster_cache.parallel_bgra_peak_workers,
                    cpu_parallel_bgra_work_seconds=raster_cache.parallel_bgra_work_seconds,
                    adaptive_bgra_enabled=raster_cache.adaptive_bgra_enabled,
                    parallel_bgra_inline_small=raster_cache.parallel_bgra_inline_small,
                    parallel_bgra_inline_busy=raster_cache.parallel_bgra_inline_busy,
                    qt_canvas_capture_seconds=raster_cache.qt_canvas_capture_seconds,
                    qt_canvas_state_setup_seconds=raster_cache.qt_canvas_state_setup_seconds,
                    qt_canvas_paint_stage_seconds=raster_cache.qt_canvas_paint_stage_seconds,
                    qt_canvas_state_restore_seconds=raster_cache.qt_canvas_state_restore_seconds,
                    qt_canvas_inline_bgra_seconds=raster_cache.qt_canvas_inline_bgra_seconds,
                qt_canvas_band_timing={
                    key: dict(costs)
                    for key, costs in raster_cache.qt_canvas_band_timing.items()
                },
                    retained_bytes=raster_cache.retained_bytes,
                    peak_retained_bytes=raster_cache.peak_retained_bytes,
                    advanced_lyric_cache_entries=len(raster_cache.frozen_lyric_settings_cache),
                    partial_band_enabled=raster_cache.partial_band_enabled,
                    partial_band_captures=raster_cache.partial_band_captures,
                    compact_band_enabled=raster_cache.compact_band_enabled,
                    compact_band_captures=raster_cache.compact_band_captures,
                    compact_canvas_bytes_saved=raster_cache.compact_canvas_bytes_saved,
                    scene_render_source_pixels=raster_cache.scene_render_source_pixels,
                    full_frame_source_pixels=raster_cache.full_frame_source_pixels,
                )
            raster_cache.clear()
            if raster_counters is not None:
                # After the converter pool joins, report final worker totals
                # instead of a snapshot with the final BGRA packet pending.
                raster_counters.update(
                    retained_bytes=0,
                    cpu_parallel_bgra_enabled=raster_cache.parallel_bgra_enabled,
                    cpu_parallel_bgra_submissions=raster_cache.parallel_bgra_submissions,
                    cpu_parallel_bgra_peak_workers=raster_cache.parallel_bgra_peak_workers,
                    cpu_parallel_bgra_work_seconds=raster_cache.parallel_bgra_work_seconds,
                    adaptive_bgra_enabled=raster_cache.adaptive_bgra_enabled,
                    parallel_bgra_inline_small=raster_cache.parallel_bgra_inline_small,
                    parallel_bgra_inline_busy=raster_cache.parallel_bgra_inline_busy,
                    qt_canvas_capture_seconds=raster_cache.qt_canvas_capture_seconds,
                    qt_canvas_state_setup_seconds=raster_cache.qt_canvas_state_setup_seconds,
                    qt_canvas_paint_stage_seconds=raster_cache.qt_canvas_paint_stage_seconds,
                    qt_canvas_state_restore_seconds=raster_cache.qt_canvas_state_restore_seconds,
                    qt_canvas_inline_bgra_seconds=raster_cache.qt_canvas_inline_bgra_seconds,
                    qt_canvas_band_timing={
                        key: dict(costs)
                        for key, costs in raster_cache.qt_canvas_band_timing.items()
                    },
                )
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

    def __init__(self, project, *, fps, scale, cancel_event=None, request_timeout=90.0,
                 optimizations: NativeGpuOptimizations | None = None):
        app = QApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RenderError("Create GuiProjectFrameSource on the GUI thread.")
        super().__init__()
        if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 120:
            raise TimelineValidationError("Full native export FPS must be 1..120")
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("Frame request timeout must be positive and finite.")
        gpu_video = bool(optimizations and optimizations.gpu_video)
        validate_project_for_gpu_capture(project, scale=scale, source_policy="dynamic-audio", gpu_video=gpu_video)
        build_dynamic_stack(project, scale, gpu_video=gpu_video)
        self.project = deepcopy(project)
        self.fps, self.scale = fps, scale
        self.cancel_event = cancel_event or Event()
        self.request_timeout = request_timeout
        self.optimizations = optimizations
        self._ready = Event()
        self._closing = Event()
        self._lock = Lock()
        self._iterator = None
        self._audio = None
        self._ffmpeg = None
        self._analysis = None
        self.video_sources = None
        self._instrumental_by_track = None
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
            self._instrumental_by_track = prepare_instrumental_spans(
                [t for t in self.project.playlist if t.enabled and t.duration_seconds > 0],
                (s for s in self.project.sources if s.visible and s.source_type is SourceType.LYRICS),
                lambda: _check_cancel(self.cancel_event),
            )
            self._analysis = prepare_project_frame_audio(
                self.project, compile_playlist(self.project.playlist, enabled_only=True),
                fps=self.fps, scale=self.scale, prepared_audio=self._audio, ffmpeg=self._ffmpeg,
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
                    instrumental_by_track=self._instrumental_by_track,
                    raster_counters=self.raster_counters,
                    optimizations=self.optimizations,
                    video_sources=self.video_sources,
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
            self._instrumental_by_track = None
            self._ready.set()
            self.closed.emit()
