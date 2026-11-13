"""Export-local Qt glyph tiles and GPU placement commands."""
from collections import OrderedDict
from dataclasses import replace
from math import ceil, floor, inf, isfinite, nextafter

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from app.canvas.source_item import SourceItem
from app.models.source import SourceType
from app.preview.native_canvas_layer_packets import (
    MAX_NATIVE_CANVAS_LAYERS, MAX_NATIVE_TEXT_QUADS, NativeCanvasBgraLayer, NativeCanvasTextLayer,
)


_FORMAT = QImage.Format.Format_ARGB32_Premultiplied
_CACHE_BYTES = 16 * 1024 * 1024


def supports_gpu_text(source):
    return (
        source.source_type in {SourceType.TEXT, SourceType.LYRICS}
        and source.mask_shape == "none" and not source.gradient.enabled and source.frame_width == 0
        and QColor(source.fill_color).alpha() == 0 and source.outline_width == 0
        and not (source.shadow.enabled and not source.text_shadow_glyph)
        and not (source.flip_horizontal or source.flip_vertical or source.keyframes.animated)
        and not source.uses_personal_color
    )


def split_gpu_text_bands(scene, stack, owners, layer_limit):
    """Reserve contiguous text bands while preserving equal-Z groups and clocks."""
    items = [
        item.source for item in reversed(scene.items())
        if isinstance(item, SourceItem) and item.isVisible() and item.source.visible
    ]
    remaining = max(0, min(MAX_NATIVE_CANVAS_LAYERS, layer_limit) - len(stack.order))
    bands, order, schedule = [], [], []
    for kind, index in stack.order:
        if kind != "canvas":
            order.append((kind, index))
            continue
        lower, upper = stack.bands[index]
        by_z = {}
        for source in items:
            z = source.z_index
            if (lower is None or z >= lower) and (upper is None or z <= upper):
                by_z[z] = by_z.get(z, True) and supports_gpu_text(source)
        groups = []
        for z, text in sorted(by_z.items()):
            if not groups or text != groups[-1][1]:
                groups.append((z, text))
        cuts = set()
        for group, (z, text) in enumerate(groups):
            if not text:
                continue
            edges = ({z} if group or index == 0 else set())
            if group + 1 < len(groups):
                edges.add(groups[group + 1][0])
            if len(edges - cuts) <= remaining - len(cuts):
                cuts.update(edges)
        remaining -= len(cuts)
        for end in (*sorted(cuts), None):
            order.append(("canvas", len(bands)))
            bands.append((lower, upper if end is None else nextafter(float(end), -inf)))
            schedule.append(owners[index])
            lower = end
    return replace(stack, bands=tuple(bands), order=tuple(order)), tuple(schedule)


class NativeTextCache:
    """Keep small Qt rasters and the latest atlas for each eligible Canvas band."""

    def __init__(self, items, *, enabled):
        self.bands = {
            f"layer:{index - 1}": band for index, band in enumerate(items)
            if index and band and all(supports_gpu_text(item.source) for item in band)
        } if enabled else {}
        self.tiles = OrderedDict()
        self.tile_bytes = 0
        self.next_id = 0
        self.atlases = {}
        self.frames = self.fallbacks = self.tile_renders = self.atlas_uploads = 0

    def tile(self, key, create):
        if key in self.tiles:
            self.tiles.move_to_end(key)
            return self.tiles[key]
        entry = create()
        self.tile_renders += 1
        if entry is None:
            return None
        size = entry[0].sizeInBytes()
        while self.tiles and (len(self.tiles) >= 512 or self.tile_bytes + size > _CACHE_BYTES):
            _, old = self.tiles.popitem(last=False)
            self.tile_bytes -= old[1].sizeInBytes()
        entry = (self.next_id, *entry)
        self.next_id += 1
        self.tiles[key] = entry
        self.tile_bytes += size
        return entry

    def capture(self, key, artboard, width, height):
        active = [item for item in reversed(self.bands[key]) if item.isVisible() and item.opacity() > 0]
        active.sort(key=lambda item: item.zValue())
        if any(not supports_gpu_text(item.source) or item._subtitle_intro_state is not None
               or -2 in item._subtitle_line_style_indices for item in active):
            self.fallbacks += 1
            return None
        if not active:
            self.frames += 1
            return self._layer(key, ())
        layout = QImage(1, 1, _FORMAT)
        layout.fill(0)
        painter = QPainter(layout)
        sx, sy = width / artboard.width(), height / artboard.height()
        output = QTransform(sx, 0, 0, sy, -artboard.left() * sx, -artboard.top() * sy)
        recorder = _TextQuads(self, painter, sx, sy, width, height)
        try:
            for item in active:
                painter.save()
                try:
                    painter.setWorldTransform(item.sceneTransform() * output)
                    painter.setOpacity(item.opacity())
                    item._export_text_quads = recorder
                    item.paint(painter, None)
                finally:
                    del item._export_text_quads
                    painter.restore()
                if recorder.unsupported:
                    break
        finally:
            painter.end()
        if not recorder.unsupported:
            layer = self._layer(key, recorder.draws)
            if layer is not None:
                self.frames += 1
                return layer
        self.fallbacks += 1
        return None

    def _layer(self, key, draws):
        if not draws:
            return NativeCanvasBgraLayer(key, 1, 1, bytes(4), clip=(0, 0, 1, 1))
        tiles = {token: image for token, image, *_ in draws}
        old = self.atlases.get(key)
        if old is not None and tiles.keys() <= old[3].keys():
            w, h, pixels, positions = old
        else:
            # Rows normally pack vertically. Use a shelf when many short tiles
            # coexist in a caption or are needed by neighboring title sources.
            w = max(max(image.width() for image in tiles.values()), 1024)
            positions = {}
            x = y = row_height = used_width = 0
            for token, image in sorted(tiles.items()):
                tw, th = image.width(), image.height()
                if x + tw > w:
                    x, y, row_height = 0, y + row_height, 0
                positions[token] = (x, y, x + tw, y + th)
                x += tw
                row_height = max(row_height, th)
                used_width = max(used_width, x)
            w, h = used_width, y + row_height
            if h > 2160 or w * h * 4 > _CACHE_BYTES:
                return None
            atlas = QImage(w, h, _FORMAT)
            if atlas.isNull():
                return None
            atlas.fill(0)
            painter = QPainter(atlas)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            for token, image in tiles.items():
                painter.drawImage(*positions[token][:2], image)
            painter.end()
            pixels = bytes(atlas.constBits())
            self.atlases[key] = w, h, pixels, positions
            self.atlas_uploads += 1
        return NativeCanvasTextLayer(
            key, w, h, pixels,
            quads=tuple((positions[token], target, clip, opacity)
                        for token, _, target, clip, opacity in draws),
        )


class _TextQuads:
    def __init__(self, cache, painter, sx, sy, width, height):
        self.cache, self.painter = cache, painter
        self.sx, self.sy = sx, sy
        self.width, self.height = width, height
        self.draws = []
        self.unsupported = False

    def _raster(self, width, height, pad, paint):
        w, h = ceil((width + 2 * pad) * self.sx), ceil((height + 2 * pad) * self.sy)
        if w <= 0 or h <= 0:
            return None
        if w > 3840 or h > 2160 or w * h * 4 > _CACHE_BYTES:
            self.unsupported = True
            return None
        image = QImage(w, h, _FORMAT)
        if image.isNull():
            self.unsupported = True
            return None
        image.fill(0)
        painter = QPainter(image)
        try:
            painter.setRenderHints(self.painter.renderHints())
            painter.scale(self.sx, self.sy)
            painter.translate(pad, pad)
            paint(painter)
        finally:
            painter.end()
        return self._trim(image, self.sx, self.sy, -pad, -pad)

    @staticmethod
    def _trim(image, sx, sy, x, y):
        alpha = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4)[..., 3]
        rows, cols = np.flatnonzero(alpha.any(axis=1)), np.flatnonzero(alpha.any(axis=0))
        if not rows.size:
            return None
        # Keep one transparent texel on either side for fractional sampling.
        left, top = max(0, int(cols[0]) - 1), max(0, int(rows[0]) - 1)
        right = min(image.width(), int(cols[-1]) + 2)
        bottom = min(image.height(), int(rows[-1]) + 2)
        tile = image.copy(left, top, right - left, bottom - top)
        # QPainter packs into the atlas in physical texels. Logical sizing
        # from a high-DPI blur pixmap is applied only when placing the quad.
        tile.setDevicePixelRatio(1.0)
        return tile, sx, sy, x + left / sx, y + top / sy

    def _draw(self, painter, origin, tile, opacity):
        if tile is None or self.unsupported or opacity <= 0:
            return
        transform = painter.worldTransform()
        if transform.type().value > QTransform.TransformationType.TxScale.value or min(transform.m11(), transform.m22()) <= 0:
            self.unsupported = True
            return
        token, image, sx, sy, dx, dy = tile
        target = transform.mapRect(QRectF(origin.x() + dx, origin.y() + dy,
                                         image.width() / sx, image.height() / sy))
        if painter.hasClipping():
            clip = transform.mapRect(painter.clipBoundingRect())
            bounds = (max(0, floor(clip.left() + 0.5)), max(0, floor(clip.top() + 0.5)),
                      min(self.width, floor(clip.right() + 0.5)), min(self.height, floor(clip.bottom() + 0.5)))
        else:
            bounds = (0, 0, self.width, self.height)
        l, t, r, b = bounds
        if l < r and t < b and target.intersects(QRectF(l, t, r - l, b - t)):
            coordinates = tuple(round(value, 5) for value in target.getCoords())
            if (len(self.draws) == MAX_NATIVE_TEXT_QUADS
                    or not all(isfinite(value) and abs(value) <= 1e6 for value in coordinates)):
                self.unsupported = True
                return
            if coordinates[0] < coordinates[2] and coordinates[1] < coordinates[3]:
                self.draws.append((token, image, coordinates, bounds, round(opacity, 6)))

    def glyphs(self, item, painter, rect, flags, text):
        source = item.source
        shadow = source.shadow
        # Qt applies opacity to each overlapping primitive of a styled glyph.
        # Bake it only where a stroke or shadow would otherwise change the result.
        opacity = painter.opacity()
        baked = opacity if source.text_stroke_width or shadow.enabled else 1.0
        font, pen = painter.font(), painter.pen()
        margin = max(4.0, source.text_stroke_width + 2.0)
        if shadow.enabled:
            margin += shadow.blur_radius * 2 + max(abs(shadow.offset_x), abs(shadow.offset_y))
        key = ("glyph", font.key(), pen.color().getRgbF(), text,
               round(rect.width(), 6), round(rect.height(), 6), int(flags),
               source.text_line_gap, source.text_stroke_width, source.text_stroke_color,
               shadow.enabled, source.text_shadow_glyph, shadow.color,
               shadow.blur_radius, shadow.offset_x, shadow.offset_y, shadow.opacity, baked)

        def paint(tile_painter):
            tile_painter.setFont(font)
            tile_painter.setPen(pen)
            tile_painter.setOpacity(baked)
            item._draw_text(tile_painter, QRectF(0, 0, rect.width(), rect.height()), flags, text)

        tile = self.cache.tile(key, lambda: self._raster(rect.width(), rect.height(), margin, paint))
        self._draw(painter, rect.topLeft(), tile, 1.0 if baked != 1.0 else opacity)

    def accent(self, painter, path, color, key):
        bounds = path.boundingRect()

        def paint(tile_painter):
            tile_painter.translate(-bounds.topLeft())
            tile_painter.fillPath(path, color)

        tile = self.cache.tile(("accent", key, color.rgba()),
                               lambda: self._raster(bounds.width(), bounds.height(), 2, paint))
        self._draw(painter, bounds.topLeft(), tile, painter.opacity())

    def pixmap(self, painter, origin, pixmap):
        dpr = pixmap.devicePixelRatio()
        if pixmap.width() * pixmap.height() * 4 > _CACHE_BYTES:
            self.unsupported = True
            return
        tile = self.cache.tile(("blur", pixmap.cacheKey()), lambda: self._trim(
            pixmap.toImage().convertToFormat(_FORMAT), dpr, dpr, 0, 0,
        ))
        self._draw(painter, origin, tile, painter.opacity())
