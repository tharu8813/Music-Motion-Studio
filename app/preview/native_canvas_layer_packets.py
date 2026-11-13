"""Opt-in Qt Canvas -> native D3D11 layer-input contract.

The existing CanvasSnapshot code remains the single source of truth for
source layout, text, timed lyrics and source animations. This module captures
its *already prepared* scene in the export planner's Z bands, preserving
straight alpha for the native GPU video compositor. Nothing changes the
production exporter or causes a GPU export to become enabled.

Important: current Qt QPainter rasterization is CPU-side. BGRA byte packets
must still be uploaded to the D3D11 device. Native GPU painting of glyphs,
effects, and texture-backed video layers is NOT implemented by this module.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from concurrent.futures import Future
from time import monotonic
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import ClassVar, Sequence

import numpy as np
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage

from app.animation.curves import TrackTempo
from app.canvas.live_canvas import CanvasScene
from app.preview.canvas_snapshot import CanvasSnapshot


MAX_NATIVE_CANVAS_LAYERS = 8
MAX_NATIVE_TEXT_QUADS = 64  # Keep all eight layers inside the 64 KiB wire header.
_MAX_DIMENSION = (3840, 2160)
_SAFE_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")


@dataclass(frozen=True, slots=True)
class NativeCanvasBgraLayer:
    """An ordered BGRA source plane with straight alpha and optional v2 geometry."""

    stream_key: str
    width: int
    height: int
    pixels: bytes
    opacity: float = 1.0
    inverse_transform: tuple[float, ...] | None = None
    clip: tuple[int, ...] | None = None
    packet_kind: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        if not 0 < self.width <= 3840 or not 0 < self.height <= 2160:
            raise ValueError("Native Canvas layer dimensions must be positive")
        if len(self.pixels) != self.payload_bytes:
            raise ValueError("Native Canvas layer BGRA bytes do not match dimensions")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("Native Canvas layer opacity must be in [0, 1]")
        if self.inverse_transform is not None:
            m = self.inverse_transform
            if (len(m) != 6 or any(isinstance(x, bool) or not math.isfinite(x) or abs(x) > 1e6 for x in m)
                    or abs(m[0] * m[3] - m[1] * m[2]) < 1e-12):
                raise ValueError("Native inverse transform must be finite, bounded and invertible")
        if self.clip is not None and (len(self.clip) != 4 or any(type(x) is not int for x in self.clip)):
            raise ValueError("Native clip must contain four integer bounds")

    @property
    def payload_bytes(self) -> int:
        return self.width * self.height * 4


@dataclass(frozen=True, slots=True)
class NativeCanvasParticleLayer(NativeCanvasBgraLayer):
    """Schema 3 sprite rows, each carrying three f32 values and packed RGBA."""
    packet_kind: ClassVar[str] = "particle_sprites"

    def __post_init__(self) -> None:
        NativeCanvasBgraLayer.__post_init__(self)
        if (self.width != 4 or not 1 <= self.height <= 1000
                or self.inverse_transform is not None or self.clip is None):
            raise ValueError("Native particle sprites require 4×N data and an output clip")


@dataclass(frozen=True, slots=True)
class NativeCanvasRadialLayer(NativeCanvasBgraLayer):
    """Schema 6 rows of round-capped line segments and packed color."""
    packet_kind: ClassVar[str] = "radial_segments"

    def __post_init__(self) -> None:
        NativeCanvasBgraLayer.__post_init__(self)
        if (self.width != 6 or not 8 <= self.height <= 192 or self.height % 2
                or self.inverse_transform is not None or self.clip is None):
            raise ValueError("Native radial bars require 6×N data and an output clip")


@dataclass(frozen=True, slots=True)
class NativeCanvasTextLayer(NativeCanvasBgraLayer):
    """Premultiplied BGRA glyph tiles and ordered source/target/clip/opacity quads."""
    packet_kind: ClassVar[str] = "text_quads"
    quads: tuple[tuple, ...] = ()

    def __post_init__(self) -> None:
        NativeCanvasBgraLayer.__post_init__(self)
        if (not 1 <= len(self.quads) <= MAX_NATIVE_TEXT_QUADS or self.opacity != 1.0
                or self.inverse_transform is not None or self.clip is not None):
            raise ValueError("Native text layers require an atlas and 1..64 ordered quads")


@dataclass(frozen=True, slots=True)
class NativeCanvasVideoLayer(NativeCanvasBgraLayer):
    """A GPU decoder request with no CPU pixel plane."""
    packet_kind: ClassVar[str] = "video_source"
    asset: int = 0
    media_time: int | None = None
    media_limit: int | None = None
    crop: bool = False
    flip_horizontal: bool = False
    flip_vertical: bool = False
    border_radius: float = 0.0
    saturation: float = 1.0

    @property
    def payload_bytes(self) -> int:
        return 0

    def __post_init__(self) -> None:
        NativeCanvasBgraLayer.__post_init__(self)
        if ((self.width, self.height) != (1, 1) or self.inverse_transform is not None
                or self.clip is None or not 0 <= self.asset < MAX_NATIVE_CANVAS_LAYERS
                or not math.isfinite(self.border_radius) or self.border_radius < 0
                or not 0.0 <= self.saturation <= 3.0
                or (self.media_limit is not None and not 0 < self.media_limit <= 86400 * 10_000_000)
                or (self.media_time is not None and not 0 <= self.media_time <= 86400 * 10_000_000)):
            raise ValueError("Invalid native video decoder request")



@dataclass(frozen=True, slots=True)
class NativeCanvasFrame:
    """One source-order-stable Qt scene snapshot ready for D3D11 upload."""

    width: int
    height: int
    layers: tuple[NativeCanvasBgraLayer, ...]
    schema: int = 1

    def __post_init__(self) -> None:
        if not 1 <= len(self.layers) <= MAX_NATIVE_CANVAS_LAYERS:
            raise ValueError("Native Canvas supports 1..8 GPU layer streams")
        if self.width < 2 or self.height < 2 or self.width % 2 or self.height % 2:
            raise ValueError("Native Canvas export requires even dimensions >=2")
        if self.width > _MAX_DIMENSION[0] or self.height > _MAX_DIMENSION[1]:
            raise ValueError("Native Canvas export resolution exceeds 3840x2160")
        if type(self.schema) is not int or self.schema not in (1, 2, 3, 4, 5, 6):
            raise ValueError("Native Canvas schema must be 1..6")
        full_clip = (0, 0, self.width, self.height)
        for index, layer in enumerate(self.layers):
            if isinstance(layer, NativeCanvasParticleLayer) and (self.schema < 3 or index == 0):
                raise ValueError("Native particle sprites require a schema 3 overlay layer")
            if isinstance(layer, NativeCanvasTextLayer) and (self.schema < 4 or index == 0):
                raise ValueError("Native text quads require a schema 4 overlay layer")
            if isinstance(layer, NativeCanvasVideoLayer) and (self.schema < 5 or index == 0):
                raise ValueError("Native video requires a schema 5 overlay layer")
            if isinstance(layer, NativeCanvasRadialLayer) and (self.schema < 6 or index == 0):
                raise ValueError("Native radial bars require a schema 6 overlay layer")
            identity = layer.inverse_transform is None or layer.inverse_transform == (1, 0, 0, 1, 0, 0)
            if (self.schema == 1 or index == 0) and (
                    (layer.width, layer.height) != (self.width, self.height) or not identity
                    or layer.clip not in (None, full_clip)):
                raise ValueError("v1 layers and the v2 base require full-frame identity geometry")
            if layer.clip is not None:
                l, t, r, b = layer.clip
                if not 0 <= l <= r <= self.width or not 0 <= t <= b <= self.height:
                    raise ValueError("Native clip must be an output half-open rectangle")
        if self.layers[0].opacity != 1.0:
            raise ValueError("First native Canvas layer must be opaque at global alpha 1")


@dataclass(frozen=True, slots=True)
class DeferredNativeCanvasLayer:
    """One completed Qt snapshot with byte conversion running on a CPU worker."""

    stream_key: str
    width: int
    height: int
    pixels_future: Future[bytes]
    opacity: float = 1.0
    inverse_transform: tuple[float, ...] | None = None
    clip: tuple[int, ...] | None = None

    def resolve(self) -> NativeCanvasBgraLayer:
        return NativeCanvasBgraLayer(
            stream_key=self.stream_key, width=self.width, height=self.height,
            pixels=self.pixels_future.result(), opacity=self.opacity,
            inverse_transform=self.inverse_transform, clip=self.clip,
        )


@dataclass(frozen=True, slots=True)
class DeferredNativeCanvasFrame:
    """Painted on GUI, independently converted on CPU, serialized on sender.

    Only QImage data is handed to CPU workers; the Qt scene, source objects,
    lyrics and animations remain exclusively on the QApplication thread.
    """

    width: int
    height: int
    layers: tuple[NativeCanvasBgraLayer | DeferredNativeCanvasLayer, ...]
    schema: int = 1

    def resolve(self) -> NativeCanvasFrame:
        return NativeCanvasFrame(
            width=self.width, height=self.height,
            layers=tuple(
                layer.resolve() if isinstance(layer, DeferredNativeCanvasLayer)
                else layer for layer in self.layers
            ),
            schema=self.schema,
        )


def straight_bgra_bytes(image: QImage) -> bytes:
    """Convert QImage raster bytes to Windows D3D11 BGRA8 *straight* alpha.

    QImage.Format_ARGB32_Premultiplied cannot be sent directly as D3D11
    source alpha: multiplying by alpha a second time darkens text edges.
    On little-endian Windows Format_ARGB32 stores bytes as B, G, R, A.
    """
    if sys.byteorder != "little":
        raise RuntimeError("Native D3D11 BGRA packets require little-endian QImage storage")
    if image.isNull():
        raise ValueError("Cannot serialize a null Qt Canvas image")
    # The native Qt painter produces an opaque QImage.Format_RGB32 base
    # layer: on little-endian systems it is already packed B,G,R,0xFF.
    # Converting it to Format_ARGB32 allocated/copied another full-resolution
    # image even though the output BGRA bytes are identical. Format_ARGB32
    # is likewise straight-alpha already. Only other formats (especially
    # premultiplied transparent glyph bands) need the Qt conversion.
    if image.format() in (
        QImage.Format.Format_RGB32, QImage.Format.Format_ARGB32,
    ):
        raster = image
    else:
        raster = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = raster.width(), raster.height()
    stride = raster.bytesPerLine()
    raw = bytes(raster.constBits())
    if len(raw) != raster.sizeInBytes() or stride < width * 4:
        raise ValueError("Unexpected Qt Canvas QImage memory layout")
    if stride == width * 4:
        return raw
    return b"".join(raw[y * stride:y * stride + width * 4] for y in range(height))


def _nontransparent_crop(image: QImage) -> QRect | None:
    """Find an exact output-pixel crop when Qt needs to paint the entire band."""
    width, height = image.width(), image.height()
    if (sys.byteorder != "little" or image.format() not in (
            QImage.Format.Format_ARGB32_Premultiplied, QImage.Format.Format_ARGB32)):
        return None
    rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(height, image.bytesPerLine())
    alpha = rows[:, 3:width * 4:4]
    # Skip only tiles proven empty, and stop scanning once a painted edge
    # is found. Tile-aligned bounds also help reuse GPU allocations.
    top, bottom = _painted_extent(alpha)
    if top == bottom:
        return QRect(0, 0, 1, 1)
    left, right = _painted_extent(alpha[top:bottom].T)
    if (right - left) * (bottom - top) * 10 >= width * height * 9:
        return None
    return QRect(left, top, right - left, bottom - top)


def _painted_extent(alpha: np.ndarray) -> tuple[int, int]:
    tile = 32
    size = len(alpha)
    for start in range(0, size, tile):
        if alpha[start:start + tile].any():
            break
    else:
        return 0, 0
    for end in range((size - 1) // tile * tile, start, -tile):
        if alpha[end:end + tile].any():
            return start, min(size, end + tile)
    return start, min(size, start + tile)


def _validate_bands(
    bands: Sequence[tuple[float | None, float | None]],
) -> None:
    if not 1 <= len(bands) <= MAX_NATIVE_CANVAS_LAYERS:
        raise ValueError("Native Canvas requires between 1 and 8 Z bands")
    if bands[0][0] is not None:
        raise ValueError("The first native Canvas Z band must include the bottom")
    for i, (lower, upper) in enumerate(bands):
        if lower is not None and upper is not None and lower > upper:
            raise ValueError("Native Canvas Z band has reversed limits")
        if i > 0:
            previous_upper = bands[i-1][1]
            if previous_upper is None or lower is None or lower <= previous_upper:
                raise ValueError("Native Canvas Z bands must be ordered and disjoint")
        if i < len(bands) - 1 and upper is None:
            raise ValueError("Unbounded native Canvas band cannot precede another band")


def capture_native_canvas_frame(
    scene: CanvasScene,
    z_bands: Sequence[tuple[float | None, float | None]],
    *,
    output_scale: float = 1.0,
) -> NativeCanvasFrame:
    """Capture actual rendered SourceItems (including timed lyrics).

    The supplied scene must already be updated to the exact export time,
    e.g. using ExportCanvasCapturer's existing timeline state resolution.
    This function does not independently advance the playlist or lyrics.
    Each band is rasterized independently for later D3D11 alpha composition,
    rather than flattening the entire Canvas into a CPU QImage.
    """
    _validate_bands(z_bands)
    if not 0.25 <= output_scale <= 64.0:
        raise ValueError("Invalid native Canvas scene output scale")
    layers: list[NativeCanvasBgraLayer] = []
    for index, (z_min, z_max) in enumerate(z_bands):
        image = CanvasSnapshot.capture(
            scene, output_scale=output_scale,
            z_min=z_min, z_max=z_max,
            transparent=index != 0,
            capture_rect=None,  # always cover the entire NVENC frame
        )
        layers.append(NativeCanvasBgraLayer(
            stream_key="base" if index == 0 else f"layer:{index - 1}",
            width=image.width(), height=image.height(),
            pixels=straight_bgra_bytes(image),
        ))
    return NativeCanvasFrame(
        width=layers[0].width, height=layers[0].height,
        layers=tuple(layers),
    )


def save_native_canvas_frame(
    frame: NativeCanvasFrame, destination: Path, *, stem: str = "canvas-frame",
) -> Path:
    """Write an inspectable, immutable raw layer packet for Rust diagnostics.

    All paths are relative to one manifest directory. Existing packet or
    manifest files are never silently replaced. Production export does not
    call this disk-based serialization path.
    """
    if not _SAFE_STEM.fullmatch(stem):
        raise ValueError("Native Canvas packet filename stem is invalid")
    destination = Path(destination)
    filenames = [f"{stem}-layer-{i:02d}.bgra" for i in range(len(frame.layers))]
    manifest_path = destination / f"{stem}.json"
    if manifest_path.exists() or any((destination / name).exists() for name in filenames):
        raise FileExistsError("Native Canvas destination already contains this frame packet")
    destination.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        layer_info = []
        for index, (layer, name) in enumerate(zip(frame.layers, filenames)):
            path = destination / name
            with path.open("xb") as writer:
                created.append(path)
                writer.write(layer.pixels)
            layer_info.append({
                "stream_key": layer.stream_key,
                "file": name,
                "opacity": layer.opacity,
                "bytes": len(layer.pixels),
                "sha256": hashlib.sha256(layer.pixels).hexdigest(),
                "z_order": index,
            })
        if frame.schema >= 2:
            for info, layer in zip(layer_info, frame.layers):
                info.update(width=layer.width, height=layer.height)
                if layer.packet_kind is not None:
                    info["kind"] = layer.packet_kind
                if isinstance(layer, NativeCanvasTextLayer):
                    info["quads"] = layer.quads
                if isinstance(layer, NativeCanvasVideoLayer):
                    info.update(asset=layer.asset, media_time=layer.media_time, media_limit=layer.media_limit,
                                crop=layer.crop,
                                flip_horizontal=layer.flip_horizontal, flip_vertical=layer.flip_vertical,
                                border_radius=layer.border_radius, saturation=layer.saturation)
                if layer.inverse_transform is not None:
                    info["inverse_transform"] = layer.inverse_transform
                if layer.clip is not None:
                    info["clip"] = layer.clip
        manifest = {
            "schema": frame.schema,
            "format": f"mms_canvas_bgra_layers_v{frame.schema}",
            "pixel_format": "BGRA8_STRAIGHT",
            "width": frame.width, "height": frame.height,
            "coordinate_origin": "top_left",
            "qt_canvas_rasterized_on_cpu": True,
            "native_gpu_alpha_composition_required": len(frame.layers) > 1,
            "production_export_changed": False,
            "layers": layer_info,
        }
        with manifest_path.open("x", encoding="utf-8") as writer:
            created.append(manifest_path)
            json.dump(manifest, writer, ensure_ascii=False, indent=2)
            writer.write("\n")
        return manifest_path
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


def capture_native_canvas_track_frame(
    scene: CanvasScene,
    track: "PlaylistTrack",
    z_bands: Sequence[tuple[float | None, float | None]],
    *,
    elapsed_seconds: float,
    output_scale: float = 1.0,
    track_number: int = 1,
    track_total: int = 1,
    track_start_seconds: float = 0.0,
    playlist_duration_seconds: float | None = None,
    playlist_tracks: Sequence["PlaylistTrack"] | None = None,
    timeline_seconds: float | None = None,
    animation_phase: str | None = None,
    animation_progress: float = 1.0,
    animation_phase_duration: float | None = None,
    compiled_plan: object | None = None,
    keyframe_active: bool = True,
    bass_level: float = 0.0,
    music_levels: dict[str, float] | None = None,
    track_tempos: Mapping[str, TrackTempo] | None = None,
    lyric_instrumental_spans_by_source: dict[str, tuple] | None = None,
    raster_cache: object | None = None,
    band_states: Sequence[tuple] | None = None,
) -> NativeCanvasFrame | DeferredNativeCanvasFrame:
    """Capture a REAL track/lyrics state through the established export painter.

    CanvasSnapshot.capture_track resolves lyrics, keyframes and track-dependent
    text with the shared export painter. A held band_state from the detached
    export schedule can reuse its already painted layer.
    """
    from math import isfinite

    from app.models.playlist import PlaylistTrack

    _validate_bands(z_bands)
    if not isinstance(track, PlaylistTrack):
        raise TypeError("Expected an existing PlaylistTrack")
    if not isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("Elapsed track time must be finite and nonnegative")
    if not isfinite(output_scale) or not 0.25 <= output_scale <= 64.0:
        raise ValueError("Invalid native track capture scale")
    if track_number < 1 or track_total < track_number:
        raise ValueError("Invalid track index or total")
    if not isfinite(track_start_seconds) or track_start_seconds < 0:
        raise ValueError("Invalid track timeline start")
    duration = (
        track.duration_seconds
        if playlist_duration_seconds is None
        else playlist_duration_seconds
    )
    if not isfinite(duration) or duration <= 0:
        raise ValueError("Invalid playlist duration")

    layers: list[NativeCanvasBgraLayer | DeferredNativeCanvasLayer] = []
    for index, (z_min, z_max) in enumerate(z_bands):
        key = "base" if index == 0 else f"layer:{index - 1}"
        band_state = band_states[index] if band_states is not None else None
        cache_state = (
            band_state if raster_cache is not None and raster_cache.reuse_canvas_samples else None
        )
        cached = raster_cache.get(key, cache_state) if raster_cache is not None else None
        if cached is not None:
            layers.append(cached)
            continue
        if index and raster_cache is not None and band_state is not None and raster_cache.cover_items:
            sample = band_state[0]
            cover = raster_cache.cover_motion_layer(
                scene, key, sample, output_scale, layers[0].width, layers[0].height,
                track_tempos.get(sample.track.id) if track_tempos else None,
            )
            if cover is not None:
                layers.append(cover)
                continue
        if index and raster_cache is not None and band_state is not None and raster_cache.translation_items:
            translated = raster_cache.translated_layer(
                scene, key, band_state[0], z_min, z_max, output_scale,
                layers[0].width, layers[0].height,
            )
            if translated is not None:
                layers.append(translated)
                continue
        # The base stays opaque and full size. Transparent bands may paint
        # a smaller rectangle aligned to the full-frame pixel grid.
        partial = bool(raster_cache is not None
                       and raster_cache.partial_band_enabled and index != 0)
        compact = bool(partial and raster_cache.compact_band_enabled)
        capture_metrics: dict[str, object] = {}
        paint_started = monotonic() if raster_cache is not None else 0.0
        capture_state = dict(
            track=track, track_number=track_number, track_total=track_total,
            start_seconds=track_start_seconds,
            elapsed_seconds=elapsed_seconds,
            timeline_seconds=(track_start_seconds + elapsed_seconds
                              if timeline_seconds is None else timeline_seconds),
            playlist_duration_seconds=duration,
            playlist_tracks=([track] if playlist_tracks is None else list(playlist_tracks)),
            animation_phase=animation_phase,
            animation_progress=animation_progress,
            animation_phase_duration=animation_phase_duration,
            compiled_plan=compiled_plan,
            keyframe_active=keyframe_active,
            bass_level=bass_level,
            music_levels=music_levels,
            track_tempo=track_tempos.get(track.id) if track_tempos else None,
            lyric_instrumental_spans_by_source=lyric_instrumental_spans_by_source,
        )
        if band_state is not None:
            sample, band_bass, band_music, band_instrumental = band_state
            capture_state.update(
                track=sample.track, track_number=sample.track_number,
                start_seconds=sample.track_start_seconds, elapsed_seconds=sample.elapsed_seconds,
                timeline_seconds=sample.timeline_seconds, animation_phase=sample.animation_phase,
                animation_progress=sample.animation_progress,
                animation_phase_duration=sample.animation_phase_duration,
                keyframe_active=sample.keyframe_active, junction=sample.junction,
                bass_level=band_bass, music_levels=band_music,
                track_tempo=track_tempos.get(sample.track.id) if track_tempos else None,
                lyric_instrumental_spans_by_source=band_instrumental,
            )
        render_prepared_band = None
        if index and raster_cache is not None and key in raster_cache.text.bands:
            def render_prepared_band():
                return raster_cache.text.capture(key, scene.artboard_rect, layers[0].width, layers[0].height)
        image = CanvasSnapshot.capture_track(
            scene, **capture_state,
            output_scale=output_scale,
            z_min=z_min, z_max=z_max,
            transparent=index != 0,
            partial_render=partial,
            partial_render_pixel_aligned=partial,
            partial_render_keep_cropped=compact,
            render_metrics=capture_metrics,
            render_prepared_band=render_prepared_band,
            # Immutable export scene: shared worker-only lyric settings and
            # preselected source items avoid repeated per-frame Qt traversal.
            **({
                "band_source_items": raster_cache.band_source_items[index],
                "_frozen_lyric_settings_cache": raster_cache.frozen_lyric_settings_cache,
                "personal_color_cache": raster_cache.personal_colors,
            } if raster_cache is not None else {}),
        )
        if raster_cache is not None:
            capture_elapsed = monotonic() - paint_started
            raster_cache.qt_canvas_capture_seconds += capture_elapsed
            raster_cache.qt_canvas_state_setup_seconds += capture_metrics["state_setup_seconds"]
            raster_cache.qt_canvas_paint_stage_seconds += capture_metrics["paint_stage_seconds"]
            raster_cache.qt_canvas_state_restore_seconds += capture_metrics["state_restore_seconds"]
            raster_cache.record_band_capture(key, capture_metrics, capture_elapsed)
        if isinstance(image, NativeCanvasBgraLayer):
            layers.append(image)
            continue
        inverse = None
        clip = None
        pixel_rect = None
        if compact and capture_metrics.get("partial_render"):
            rect = capture_metrics["capture_rect"]
            artboard = scene.artboard_rect
            # Match CanvasSnapshot._expand_partial_capture exactly: no scaling
            # of glyph pixels, only an integer destination translation.
            origin_x = round((rect.left() - artboard.left()) * output_scale)
            origin_y = round((rect.top() - artboard.top()) * output_scale)
            full_width, full_height = layers[0].width, layers[0].height
            if (origin_x < 0 or origin_y < 0
                    or origin_x + image.width() > full_width
                    or origin_y + image.height() > full_height
                    or image.width() * image.height() >= full_width * full_height):
                # Defensive fallback for arbitrary, noninteger artboard
                # dimensions. Never transmit incorrectly positioned pixels.
                image = CanvasSnapshot._expand_partial_capture(
                    image, rect, artboard, output_scale,
                )
            else:
                pixel_rect = QRect(origin_x, origin_y, image.width(), image.height())
        elif compact and raster_cache.trim_transparent_bands:
            # Developer A/B only: inspect actual QImage alpha, never assume
            # album-art/lyrics bounds or clip glyph shadows approximately.
            # Count scan/copy overhead so a smaller wire packet cannot be
            # mistaken for a faster end-to-end export.
            source_bytes = image.width() * image.height() * 4
            bucket = raster_cache.alpha_trim_band_timing.setdefault(key, {
                "attempts": 0, "cropped": 0, "source_bytes": 0,
                "candidate_bytes": 0, "scan_seconds": 0.0,
                "copy_seconds": 0.0,
            })
            bucket["attempts"] += 1
            bucket["source_bytes"] += source_bytes
            started = monotonic()
            pixel_rect = _nontransparent_crop(image)
            bucket["scan_seconds"] += monotonic() - started
            if pixel_rect is not None:
                started = monotonic()
                image = image.copy(pixel_rect)
                bucket["copy_seconds"] += monotonic() - started
                bucket["cropped"] += 1
            bucket["candidate_bytes"] += image.width() * image.height() * 4
        if pixel_rect is not None:
            x, y, width, height = pixel_rect.getRect()
            inverse = (1, 0, 0, 1, -x, -y)
            clip = (x, y, x + width, y + height)
        # A finished QImage can be converted on a CPU worker while the GUI
        # paints the next frame. Invariant layers stay synchronous so their
        # export-local cache is never modified off the owner thread.
        use_parallel = bool(
            raster_cache is not None
            and raster_cache.parallel_bgra_enabled
            and key not in raster_cache.keys
        )
        future = None
        if use_parallel:
            future = (
                raster_cache.maybe_submit_bgra(image, straight_bgra_bytes)
                if raster_cache.adaptive_bgra_enabled
                else raster_cache.submit_bgra(QImage(image), straight_bgra_bytes)
            )
        if future is not None:
            layer = DeferredNativeCanvasLayer(
                stream_key=key, width=image.width(), height=image.height(),
                pixels_future=future, inverse_transform=inverse, clip=clip,
            )
        else:
            inline_started = monotonic() if raster_cache is not None else 0.0
            layer = NativeCanvasBgraLayer(
                stream_key=key, width=image.width(), height=image.height(),
                pixels=straight_bgra_bytes(image),
                inverse_transform=inverse, clip=clip,
            )
            if raster_cache is not None:
                raster_cache.qt_canvas_inline_bgra_seconds += (
                    monotonic() - inline_started
                )
        layers.append(layer)
        if raster_cache is not None:
            raster_cache.record(layer, cache_state)
            raster_cache.record_capture_region(
                layers[0].width, layers[0].height,
                index != 0, capture_metrics,
                compact_bytes=(image.width() * image.height() * 4 if pixel_rect is not None else None),
            )
    packet = dict(
        width=layers[0].width, height=layers[0].height,
        layers=tuple(layers),
        schema=(4 if any(isinstance(layer, NativeCanvasTextLayer) for layer in layers)
                else 2 if any(layer.inverse_transform is not None or layer.clip is not None for layer in layers)
                else 1),
    )
    if any(isinstance(layer, DeferredNativeCanvasLayer) for layer in layers):
        return DeferredNativeCanvasFrame(**packet)
    return NativeCanvasFrame(**packet)
