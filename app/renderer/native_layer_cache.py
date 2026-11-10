"""Export-local bounded Qt raster cache; raster misses still paint on CPU."""
from dataclasses import replace

from app.canvas.source_item import SourceItem
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.native_canvas_layer_packets import MAX_NATIVE_CANVAS_LAYERS


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

    def __init__(self, scene, bands, duration):
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
        # CanvasSnapshot already has this worker-only cache contract.  The
        # detached native project is immutable throughout the export.
        self.frozen_lyric_settings_cache = {}
        self.layers = {}
        self.hits = self.rasterizations = self.retained_bytes = 0
        self.peak_retained_bytes = 0

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

    def clear(self):
        self.layers.clear()
        self.frozen_lyric_settings_cache.clear()
        self.retained_bytes = 0
