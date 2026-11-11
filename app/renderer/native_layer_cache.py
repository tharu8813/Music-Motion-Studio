"""Export-local bounded Qt raster cache; raster misses still paint on CPU."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from PySide6.QtGui import QImage
from time import monotonic
import os

from app.canvas.source_item import SourceItem
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.native_canvas_layer_packets import MAX_NATIVE_CANVAS_LAYERS
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations


def split_resident_bands(scene, stack, duration):
    """Keep equal-Z sources together and leave room for reactive streams."""
    bands = CanvasSnapshot.split_mixed_capture_bands(
        scene, set(stack.source_ids), list(stack.bands), duration,
        max_streams=MAX_NATIVE_CANVAS_LAYERS - len(stack.source_ids),
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


class NativeRasterCache:
    """Own only invariant planes, scoped to one detached project export.

    No disk/global cache: a new project, scale or export gets a fresh owner.
    At most eight full-frame planes (253.2 MiB at 4K) can be retained.
    """

    def __init__(self, scene, bands, duration, *, optimizations: NativeGpuOptimizations | None = None):
        self.keys = CanvasSnapshot.invariant_stream_keys(scene, set(), list(bands), duration)
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
        # Exported source Z order is fixed in this detached scene. Avoid
        # repeating scene.items() plus band comparisons in every Qt paint.
        # Keep isVisible() checks at capture time for timeline-masked items.
        self.band_excluded_items = tuple(
            tuple(item for item in visible_items
                  if (lower is not None and item.source.z_index < lower)
                  or (upper is not None and item.source.z_index > upper))
            for lower, upper in bands
        )
        # CanvasSnapshot already has this worker-only cache contract.  The
        # detached native project is immutable throughout the export.
        self.frozen_lyric_settings_cache = {}
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
        self.compact_band_captures = 0
        self.compact_canvas_bytes_saved = 0
        self.scene_render_source_pixels = 0
        self.full_frame_source_pixels = 0
        self.layers = {}
        self.hits = self.rasterizations = self.retained_bytes = 0
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

    def record_uncached_raster(self):
        self.rasterizations += 1

    def get(self, key):
        layer = self.layers.get(key)
        if layer is not None:
            self.hits += 1
        return layer

    def record(self, layer):
        self.rasterizations += 1
        if layer.stream_key in self.keys:
            self.layers[layer.stream_key] = layer
            self.retained_bytes = sum(len(entry.pixels) for entry in self.layers.values())
            self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)

    def record_capture_region(self, full_width, full_height, transparent, metrics, *, compact_bytes=None):
        """Report actual Qt scene pixels; staging BGRA planes stay full-size."""
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
        self.frozen_lyric_settings_cache.clear()
        self.retained_bytes = 0
