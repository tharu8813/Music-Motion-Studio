"""Export-local bounded Qt raster cache; raster misses still paint on CPU."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from math import ceil, floor, inf, isclose, nextafter
from threading import BoundedSemaphore, Lock
from PySide6.QtGui import QImage, QTransform
from time import monotonic
import os

from app.animation.curves import loop_pose
from app.animation.custom_motion import source_motion
from app.animation.keyframes import evaluate as evaluate_keyframes
from app.canvas.source_item import SourceItem
from app.models.source import SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.frame_state import resolve_edge_animation
from app.preview.native_canvas_layer_packets import (
    MAX_NATIVE_CANVAS_LAYERS, NativeCanvasBgraLayer, straight_bgra_bytes,
)
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations
from app.renderer.native_text_layers import NativeTextCache


def split_resident_bands(scene, stack, duration, *, layer_limit=MAX_NATIVE_CANVAS_LAYERS):
    """Keep equal-Z sources together and leave room for reactive streams."""
    bands = CanvasSnapshot.split_mixed_capture_bands(
        scene, set(stack.source_ids), list(stack.bands), duration,
        max_streams=min(MAX_NATIVE_CANVAS_LAYERS, layer_limit) - len(stack.source_ids),
    )
    order = []
    for kind, index in stack.order:
        if kind != "canvas":
            order.append((kind, index))
            continue
        lower, upper = stack.bands[index]
        order.extend(("canvas", i) for i, (lo, hi) in enumerate(bands)
                     if (lower is None or (lo is not None and lo >= lower))
                     and (upper is None or (hi is not None and hi <= upper)))
    return replace(stack, bands=tuple(bands), order=tuple(order))


def _position_keyframes_only(source):
    animation = source.keyframes
    if (
        source.source_type not in {SourceType.IMAGE, SourceType.SHAPE}
        or not animation.animated
        or source.animation_in != "none" or source.animation_out != "none"
        or source.loop_motion != "none" or source.uses_personal_color
        or source.timeline_start != 0 or source.timeline_duration != 0
        or source.rotation != 0 or source.scale != 1 or source.shadow.enabled
    ):
        return False
    curves = (animation.global_curves, *animation.single_track_curves.values(),
              *animation.per_track_curves.values())
    return all(name in {"x", "y"} for bank in curves for name, points in bank.items() if points)


def _moving_album_cover(source):
    return (
        source.source_type is SourceType.ALBUM_COVER
        and (source.animation_in != "none" or source.animation_out != "none"
             or source.loop_motion != "none")
        and "custom" not in (source.animation_in, source.animation_out)
        and not (source.keyframes.animated or source.uses_personal_color or source.shadow.enabled)
        and source.timeline_start == source.timeline_duration == source.rotation == 0
    )


def _cover_capture_bounds(item):
    padding = (item.source.outline_width / 2 + 2) * max(1.0, item.scale())
    return item.mapRectToScene(item.boundingRect()).adjusted(-padding, -padding, padding, padding)


def split_gpu_cover_bands(scene, stack):
    """Give a lone cover an opaque base while preserving its sample clock."""
    lower, upper = stack.bands[0]
    base = [
        item for item in scene.items()
        if isinstance(item, SourceItem) and item.isVisible() and item.source.visible
        and (lower is None or item.source.z_index >= lower)
        and (upper is None or item.source.z_index <= upper)
    ]
    owners = tuple(range(len(stack.bands)))
    # Moving a cover out of a mixed band adds another stream while the
    # remaining band may still produce a full-frame plane on every sample.
    if (len(base) != 1 or not _moving_album_cover(base[0].source)
            or not scene.artboard_rect.contains(_cover_capture_bounds(base[0]))
            or len(stack.order) >= MAX_NATIVE_CANVAS_LAYERS):
        return stack, owners
    z = base[0].source.z_index
    bands = ((lower, nextafter(float(z), -inf)), (z, upper), *stack.bands[1:])
    order = (("canvas", 0),) + tuple(
        (kind, index + 1 if kind == "canvas" else index) for kind, index in stack.order
    )
    return replace(stack, bands=bands, order=order), (0, *owners)


class NativeRasterCache:
    """Own invariant planes or the latest scheduled plane per Canvas band.

    No disk/global cache: a new project, scale or export gets a fresh owner.
    Each band owns at most one captured plane and one cropped motion texture.
    """

    def __init__(self, scene, bands, duration, *, optimizations: NativeGpuOptimizations | None = None):
        self.keys = CanvasSnapshot.invariant_stream_keys(scene, set(), list(bands), duration)
        self.reuse_canvas_samples = optimizations is not None and optimizations.reuse_canvas_samples
        # Each native export owns a frozen, detached scene.  Determine band
        # membership once instead of enumerating the whole QGraphicsScene
        # three or more times for every single CFR frame.
        visible_items = tuple(
            item for item in scene.items()
            if isinstance(item, SourceItem)
            and item.isVisible() and item.source.visible
        )
        self.band_source_items = tuple(
            tuple(item for item in visible_items
                  if (lower is None or item.source.z_index >= lower)
                  and (upper is None or item.source.z_index <= upper))
            for lower, upper in bands
        )
        self.text = NativeTextCache(
            self.band_source_items, enabled=optimizations is not None and optimizations.gpu_text,
        )
        # Preserve the original blend order and band boundaries.
        self.translation_items = (
            {f"layer:{index - 1}": items[0]
             for index, items in enumerate(self.band_source_items)
             if index and len(items) == 1 and _position_keyframes_only(items[0].source)}
            if optimizations is not None and optimizations.gpu_canvas_translation else {}
        )
        self.translation_textures: dict[str, tuple[float, float, NativeCanvasBgraLayer]] = {}
        self.translation_frames = 0
        self.translation_texture_bytes = 0
        self.cover_items = (
            {f"layer:{index - 1}": items[0]
             for index, items in enumerate(self.band_source_items)
             if index and len(items) == 1 and _moving_album_cover(items[0].source)
             and scene.artboard_rect.contains(_cover_capture_bounds(items[0]))}
            if optimizations is not None and optimizations.gpu_album_cover_motion else {}
        )
        self.cover_textures: dict[str, tuple[tuple, NativeCanvasBgraLayer]] = {}
        self.cover_motion_frames = 0
        self.cover_texture_captures = 0
        self.cover_texture_bytes = 0
        # CanvasSnapshot already has this worker-only cache contract.  The
        # detached native project is immutable throughout the export.
        self.frozen_lyric_settings_cache = {}
        # Asset lookup is shared by all bands of this detached export.
        self.personal_colors = {}
        # QGraphicsScene and all QPainter calls remain GUI-thread-only.
        # Only independent, immutable QImage -> BGRA byte conversions are
        # allowed on up to two CPU workers. Semaphore prevents unbounded
        # 4K image tasks (a full BGRA plane is ~31.6 MiB).
        self.parallel_bgra_enabled = (
            optimizations.parallel_bgra if optimizations is not None
            else os.environ.get(
                "PLAYLIST_CANVAS_NATIVE_PARALLEL_BGRA", "",
            ).strip().lower() in {"1", "true", "yes"}
        )
        self.adaptive_bgra_enabled = bool(
            optimizations is not None and optimizations.adaptive_bgra
        )
        # Converting a small Qt image is usually cheaper than submitting a
        # Future and waiting on its result. Tune on real RTX benchmarks only.
        self._min_async_bgra_bytes = 1024 * 1024
        self.parallel_bgra_inline_small = 0
        self.parallel_bgra_inline_busy = 0
        self.qt_canvas_capture_seconds = 0.0
        # Nonoverlapping portions of the Qt capture call. No effect on pixels.
        self.qt_canvas_state_setup_seconds = 0.0
        self.qt_canvas_paint_stage_seconds = 0.0
        self.qt_canvas_state_restore_seconds = 0.0
        self.qt_canvas_inline_bgra_seconds = 0.0
        # Diagnostics only: bounded by the native export's <=8 Canvas Z bands.
        # Each bucket measures real captures of one existing stream key; no
        # frames, pixels or QObjects are retained.
        self.qt_canvas_band_timing: dict[str, dict[str, int | float]] = {}
        self._bgra_executor = (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="mms-bgra")
            if self.parallel_bgra_enabled else None
        )
        self._bgra_slots = BoundedSemaphore(2)
        self._bgra_stats_lock = Lock()
        self._bgra_active_workers = 0
        self.parallel_bgra_submissions = 0
        self.parallel_bgra_peak_workers = 0
        self.parallel_bgra_work_seconds = 0.0
        # Explicit opt-in: native ROI paints only a smaller rectangle for
        # transparent bands, then expands to the original full BGRA packet.
        # Never change the GPU wire protocol or baseline output by default.
        self.partial_band_enabled = (
            optimizations.partial_bands if optimizations is not None
            else os.environ.get(
                "PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS", "",
            ).strip().lower() in {"1", "true", "yes"}
        )
        # Baseline and mocked runtime contracts must not require extra
        # QGraphicsScene geometry. The scene is real and GUI-owned only when
        # the explicitly opt-in ROI mode will need its artboard coordinates.
        if self.partial_band_enabled:
            artboard = scene.artboard_rect
            self.artboard_width = max(1.0, float(artboard.width()))
            self.artboard_height = max(1.0, float(artboard.height()))
        else:
            self.artboard_width = self.artboard_height = 1.0
        self.partial_band_captures = 0
        # Compact ROI only makes sense in conjunction with pixel-aligned Qt
        # partial painting. Keep both flags opt-in until RTX NVENC parity.
        self.compact_band_enabled = self.partial_band_enabled and (
            optimizations.compact_bands if optimizations is not None
            else os.environ.get(
                "PLAYLIST_CANVAS_NATIVE_COMPACT_BANDS", "",
            ).strip().lower() in {"1", "true", "yes"}
        )
        self.trim_transparent_bands = bool(optimizations and optimizations.trim_transparent_bands)
        self.compact_band_captures = 0
        self.compact_canvas_bytes_saved = 0
        # Only the opt-in real-hardware experiment updates this diagnostic.
        # No image pixels are retained by the accounting buckets.
        self.alpha_trim_band_timing: dict[str, dict[str, int | float]] = {}
        self.scene_render_source_pixels = 0
        self.full_frame_source_pixels = 0
        self.layers = {}
        self.hits = self.rasterizations = self.retained_bytes = 0
        self.sample_reuse_hits = 0
        self.peak_retained_bytes = 0

    def submit_bgra(self, image, converter):
        """Legacy explicit CPU-parallel mode: bounded, possibly blocking."""
        if self._bgra_executor is None:
            raise RuntimeError("Native CPU conversion pool is disabled")
        self._bgra_slots.acquire()
        try:
            return self._submit_bgra_acquired(image, converter)
        except BaseException:
            self._bgra_slots.release()
            raise

    def maybe_submit_bgra(self, image, converter):
        """Automatic mode: never block the Qt paint thread on worker capacity.

        Only large images qualify. When both workers are busy, convert this
        independent snapshot inline instead of stalling the GUI and delaying
        frame N+1. Worker tasks retain only their own copied QImage wrapper.
        """
        if not self.parallel_bgra_enabled:
            return None
        if image.width() * image.height() * 4 < self._min_async_bgra_bytes:
            self.parallel_bgra_inline_small += 1
            return None
        if not self._bgra_slots.acquire(blocking=False):
            self.parallel_bgra_inline_busy += 1
            return None
        try:
            return self._submit_bgra_acquired(QImage(image), converter)
        except BaseException:
            self._bgra_slots.release()
            raise

    def _submit_bgra_acquired(self, image, converter):
        """The caller owns exactly one semaphore slot on entry."""
        def convert():
            started = monotonic()
            with self._bgra_stats_lock:
                self._bgra_active_workers += 1
                self.parallel_bgra_peak_workers = max(
                    self.parallel_bgra_peak_workers, self._bgra_active_workers,
                )
            try:
                return converter(image)
            finally:
                seconds = monotonic() - started
                with self._bgra_stats_lock:
                    self.parallel_bgra_work_seconds += seconds
                    self._bgra_active_workers -= 1
                self._bgra_slots.release()

        future = self._bgra_executor.submit(convert)
        self.parallel_bgra_submissions += 1
        return future

    def get(self, key, state=None):
        cached = self.layers.get(key)
        if cached is None:
            return None
        cached_state, layer = cached
        if cached_state is not None:
            if cached_state is not state:
                del self.layers[key]
                self.retained_bytes -= layer.width * layer.height * 4
                return None
            self.sample_reuse_hits += 1
        self.hits += 1
        bucket = self.qt_canvas_band_timing.get(key)
        if bucket is not None:
            bucket["reuse_hits"] += 1
        return layer

    def translated_layer(self, scene, key, sample, z_min, z_max, output_scale, width, height):
        """Reuse a Qt-painted texture when its move is an exact output-pixel copy."""
        item = self.translation_items.get(key)
        if item is None or sample is None:
            return None
        source = item.source
        seconds = max(0.0, sample.timeline_seconds)
        local = (
            sample.elapsed_seconds if sample.keyframe_active
            and seconds >= sample.track_start_seconds
            and 0 <= sample.elapsed_seconds < sample.track.duration_seconds else None
        )
        values = evaluate_keyframes(source, seconds, sample.track.id, local)
        artboard = scene.artboard_rect
        sx, sy = width / artboard.width(), height / artboard.height()
        pos_x = item.x() + values.get("x", source.x) - source.x
        pos_y = item.y() + values.get("y", source.y) - source.y
        cached_texture = self.translation_textures.get(key)
        if cached_texture is None:
            # Capture once on the original full-frame Qt raster grid. Crop only
            # afterwards so image sampling and antialiasing match the old path.
            padding = source.outline_width / 2 + 2
            bounds = item.mapRectToScene(item.boundingRect()).adjusted(
                -padding, -padding, padding, padding,
            ).translated(pos_x - item.x() - artboard.left(), pos_y - item.y() - artboard.top())
            left, top = floor(bounds.left() * sx), floor(bounds.top() * sy)
            right, bottom = ceil(bounds.right() * sx), ceil(bounds.bottom() * sy)
            if (not (0 <= left < right <= width and 0 <= top < bottom <= height)
                    or (right - left, bottom - top) == (width, height)):
                return None
            shift_x = shift_y = 0
        else:
            anchor_x, anchor_y, texture = cached_texture
            dx, dy = (pos_x - anchor_x) * sx, (pos_y - anchor_y) * sy
            shift_x, shift_y = round(dx), round(dy)
            if not (isclose(dx, shift_x, rel_tol=0, abs_tol=1e-8)
                    and isclose(dy, shift_y, rel_tol=0, abs_tol=1e-8)):
                return None
            left, top, right, bottom = texture.clip

        x, y, r, b = left + shift_x, top + shift_y, right + shift_x, bottom + shift_y
        if not (0 <= x < r <= width and 0 <= y < b <= height):
            return None
        if cached_texture is None:
            started = monotonic()
            original_pos, suppressed = item.pos(), item._suppress_position_sync
            item._suppress_position_sync = True
            try:
                item.setPos(pos_x, pos_y)
                image = CanvasSnapshot.capture(
                    scene, output_scale, z_min=z_min, z_max=z_max, transparent=True,
                )
            finally:
                item.setPos(original_pos)
                item._suppress_position_sync = suppressed
            elapsed = monotonic() - started
            self.qt_canvas_capture_seconds += elapsed
            self.qt_canvas_paint_stage_seconds += elapsed
            self.record_band_capture(key, {
                "state_setup_seconds": 0.0, "paint_stage_seconds": elapsed,
                "state_restore_seconds": 0.0, "partial_render": False,
            }, elapsed)
            self.record_capture_region(width, height, True, {})
            self.rasterizations += 1
            started = monotonic()
            pixels = straight_bgra_bytes(image.copy(left, top, right - left, bottom - top))
            self.qt_canvas_inline_bgra_seconds += monotonic() - started
            texture = NativeCanvasBgraLayer(
                key, right - left, bottom - top, pixels, clip=(left, top, right, bottom),
            )
            self.translation_textures[key] = (pos_x, pos_y, texture)
            self.translation_texture_bytes += len(pixels)
            self.retained_bytes += len(pixels)
            self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)
        self.translation_frames += 1
        return replace(texture, inverse_transform=(1, 0, 0, 1, -x, -y), clip=(x, y, r, b))

    def cover_motion_layer(self, scene, key, sample, output_scale, width, height):
        item = self.cover_items.get(key)
        if item is None:
            return None
        source = item.source
        seconds = max(0.0, sample.timeline_seconds)
        pose = loop_pose(
            source.loop_motion, seconds, source.loop_motion_period,
            source.loop_motion_amount, source.width, source.height,
        )
        edge = resolve_edge_animation(
            source, sample.animation_phase, elapsed_seconds=sample.elapsed_seconds,
            track_duration=sample.track.duration_seconds,
            phase_progress=sample.animation_progress, phase_duration=sample.animation_phase_duration,
            junction=sample.junction,
        )
        if edge is not None:
            pose = source_motion(source, edge[2], edge[0] == "in").combined(pose)
        # The cover painter applies fade to separate background/image draws.
        # Compositing a flattened texture at partial opacity changes their overlap.
        if pose.opacity != 1.0:
            return None

        artboard = scene.artboard_rect
        sx, sy = width / artboard.width(), height / artboard.height()
        identity = () if source.content_path else (sample.track.file_path, sample.track.cover_path)
        cached = self.cover_textures.get(key)
        if cached is None or cached[0] != identity:
            bounds = _cover_capture_bounds(item).translated(-artboard.left(), -artboard.top())
            left, top = floor(bounds.left() * sx), floor(bounds.top() * sy)
            right, bottom = ceil(bounds.right() * sx), ceil(bounds.bottom() * sy)
            if (not (0 <= left < right <= width and 0 <= top < bottom <= height)
                    or (right - left, bottom - top) == (width, height)):
                return None
            metrics = {}
            started = monotonic()
            item.source = replace(source, animation_in="none", animation_out="none", loop_motion="none")
            try:
                image = CanvasSnapshot.capture_track(
                    scene, sample.track, sample.track_number, sample.track_number,
                    sample.track_start_seconds, elapsed_seconds=sample.elapsed_seconds,
                    timeline_seconds=seconds, output_scale=output_scale,
                    z_min=source.z_index, z_max=source.z_index, transparent=True,
                    band_source_items=(item,), render_metrics=metrics,
                )
            finally:
                item.source = source
            elapsed = monotonic() - started
            self.qt_canvas_capture_seconds += elapsed
            self.qt_canvas_state_setup_seconds += metrics["state_setup_seconds"]
            self.qt_canvas_paint_stage_seconds += metrics["paint_stage_seconds"]
            self.qt_canvas_state_restore_seconds += metrics["state_restore_seconds"]
            self.record_band_capture(key, metrics, elapsed)
            self.record_capture_region(width, height, True, metrics)
            self.rasterizations += 1
            started = monotonic()
            pixels = straight_bgra_bytes(image.copy(left, top, right - left, bottom - top))
            self.qt_canvas_inline_bgra_seconds += monotonic() - started
            texture = NativeCanvasBgraLayer(
                key, right - left, bottom - top, pixels, clip=(left, top, right, bottom),
            )
            change = len(pixels) - (len(cached[1].pixels) if cached is not None else 0)
            self.cover_textures[key] = (identity, texture)
            self.cover_texture_captures += 1
            self.cover_texture_bytes += change
            self.retained_bytes += change
            self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)
        else:
            texture = cached[1]
            left, top, right, bottom = texture.clip

        if pose.is_identity:
            inverse = (1, 0, 0, 1, -left, -top)
            clip = (left, top, right, bottom)
        else:
            x = item.x() + source.width / 2 - artboard.left()
            y = item.y() + source.height / 2 - artboard.top()
            transform = QTransform.fromScale(sx, sy)
            transform.translate(x + pose.dx, y + pose.dy)
            transform.rotate(pose.rotation)
            transform.scale(pose.scale * pose.scale_x, pose.scale)
            transform.translate(-x, -y)
            transform.scale(1 / sx, 1 / sy)
            transform.translate(left, top)
            matrix, invertible = transform.inverted()
            if not invertible:
                return None
            inverse = (matrix.m11(), matrix.m12(), matrix.m21(), matrix.m22(), matrix.dx(), matrix.dy())
            clip = (0, 0, width, height)
        self.cover_motion_frames += 1
        return replace(texture, inverse_transform=inverse, clip=clip)

    def record_band_capture(self, key, metrics, elapsed):
        """Account for a completed Qt Canvas capture, not BGRA conversion.

        The layer is painted exactly as before. Stage timers are provided by
        CanvasSnapshot and are nonoverlapping within the capture call.
        """
        bucket = self.qt_canvas_band_timing.setdefault(key, {
            "captures": 0, "reuse_hits": 0,
            "capture_seconds": 0.0, "state_setup_seconds": 0.0,
            "paint_stage_seconds": 0.0, "state_restore_seconds": 0.0,
            "partial_captures": 0,
        })
        bucket["captures"] += 1
        bucket["capture_seconds"] += elapsed
        bucket["state_setup_seconds"] += metrics["state_setup_seconds"]
        bucket["paint_stage_seconds"] += metrics["paint_stage_seconds"]
        bucket["state_restore_seconds"] += metrics["state_restore_seconds"]
        bucket["partial_captures"] += int(bool(metrics.get("partial_render")))

    def record(self, layer, state=None):
        self.rasterizations += 1
        if layer.stream_key in self.keys:
            state = None
        elif state is None:
            return
        self.layers[layer.stream_key] = (state, layer)
        self.retained_bytes = self.translation_texture_bytes + self.cover_texture_bytes + sum(
            entry.width * entry.height * 4 for _, entry in self.layers.values()
        )
        self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)

    def record_capture_region(self, full_width, full_height, transparent, metrics, *, compact_bytes=None):
        """Track Qt paint area separately from compact BGRA packet savings."""
        full = full_width * full_height
        self.full_frame_source_pixels += full
        area = full
        if transparent and metrics.get("partial_render"):
            rect = metrics.get("capture_rect")
            if rect is not None:
                area = min(full, max(1, round(rect.width() * full_width /
                    self.artboard_width) * round(rect.height() * full_height /
                    self.artboard_height)))
                self.partial_band_captures += 1
        if compact_bytes is not None:
            full_bytes = full * 4
            if not 0 < compact_bytes < full_bytes:
                raise ValueError("Native compact ROI must reduce the BGRA plane")
            self.compact_band_captures += 1
            self.compact_canvas_bytes_saved += full_bytes - compact_bytes
        self.scene_render_source_pixels += area

    def clear(self):
        # Only the owned converter pool is joined. No GUI/Qt event is needed
        # by any converter, so this cannot deadlock with the render worker.
        pool, self._bgra_executor = self._bgra_executor, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        self.layers.clear()
        self.translation_textures.clear()
        self.translation_texture_bytes = 0
        self.cover_textures.clear()
        self.cover_texture_bytes = 0
        self.frozen_lyric_settings_cache.clear()
        self.personal_colors.clear()
        self.retained_bytes = 0
