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

from dataclasses import dataclass
from concurrent.futures import Future
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Sequence

from PySide6.QtGui import QImage

from app.canvas.live_canvas import CanvasScene
from app.preview.canvas_snapshot import CanvasSnapshot


MAX_NATIVE_CANVAS_LAYERS = 8
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

    def __post_init__(self) -> None:
        if not 0 < self.width <= 3840 or not 0 < self.height <= 2160:
            raise ValueError("Native Canvas layer dimensions must be positive")
        if len(self.pixels) != self.width * self.height * 4:
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
        if type(self.schema) is not int or self.schema not in (1, 2):
            raise ValueError("Native Canvas schema must be 1 or 2")
        full_clip = (0, 0, self.width, self.height)
        for index, layer in enumerate(self.layers):
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
    raster = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = raster.width(), raster.height()
    stride = raster.bytesPerLine()
    raw = bytes(raster.constBits())
    if len(raw) != raster.sizeInBytes() or stride < width * 4:
        raise ValueError("Unexpected Qt Canvas QImage memory layout")
    if stride == width * 4:
        return raw
    return b"".join(raw[y * stride:y * stride + width * 4] for y in range(height))


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
        if frame.schema == 2:
            for info, layer in zip(layer_info, frame.layers):
                info.update(width=layer.width, height=layer.height)
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
    raster_cache: object | None = None,
) -> NativeCanvasFrame | DeferredNativeCanvasFrame:
    """Capture a REAL track/lyrics state through the established export painter.

    This is a diagnostics-only counterpart of capture_native_canvas_frame().
    Unlike that static helper, each Z band calls CanvasSnapshot.capture_track,
    which resolves TTML overlap, cue timing, lyric animations, keyframes and
    track-dependent text with the SAME state logic as the normal exporter.
    It still rasterizes the resulting Qt pixels on the CPU.
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
        cached = raster_cache.get(key) if raster_cache is not None else None
        if cached is not None:
            layers.append(cached)
            continue
        # Leave the base opaque artboard untouched. Dynamic transparent Z
        # bands can paint a smaller rectangle but MUST expand back to the
        # exact original BGRA plane before entering the D3D11 protocol.
        partial = bool(raster_cache is not None
                       and raster_cache.partial_band_enabled and index != 0)
        compact = bool(partial and raster_cache.compact_band_enabled)
        capture_metrics: dict[str, object] = {}
        image = CanvasSnapshot.capture_track(
            scene, track, track_number, track_total, track_start_seconds,
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
            output_scale=output_scale,
            z_min=z_min, z_max=z_max,
            transparent=index != 0,
            partial_render=partial,
            partial_render_pixel_aligned=partial,
            partial_render_keep_cropped=compact,
            render_metrics=capture_metrics,
            # Immutable export scene: shared worker-only lyric settings and
            # preselected source items avoid repeated per-frame Qt traversal.
            **({
                "band_source_items": raster_cache.band_source_items[index],
                "_frozen_lyric_settings_cache": raster_cache.frozen_lyric_settings_cache,
            } if raster_cache is not None else {}),
        )
        inverse = None
        clip = None
        is_compact = bool(compact and capture_metrics.get("partial_render"))
        if is_compact:
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
                is_compact = False
            else:
                inverse = (1, 0, 0, 1, -origin_x, -origin_y)
                clip = (
                    origin_x, origin_y,
                    origin_x + image.width(), origin_y + image.height(),
                )
        # A finished QImage can be converted on a CPU worker while the GUI
        # paints the next frame. Invariant layers stay synchronous so their
        # export-local cache is never modified off the owner thread.
        use_parallel = bool(
            raster_cache is not None
            and raster_cache.parallel_bgra_enabled
            and key not in raster_cache.keys
        )
        if use_parallel:
            future = raster_cache.submit_bgra(QImage(image), straight_bgra_bytes)
            layers.append(DeferredNativeCanvasLayer(
                stream_key=key, width=image.width(), height=image.height(),
                pixels_future=future, inverse_transform=inverse, clip=clip,
            ))
            raster_cache.record_uncached_raster()
        else:
            layer = NativeCanvasBgraLayer(
                stream_key=key, width=image.width(), height=image.height(),
                pixels=straight_bgra_bytes(image),
                inverse_transform=inverse, clip=clip,
            )
            layers.append(layer)
            if raster_cache is not None:
                raster_cache.record(layer)
        if raster_cache is not None:
            raster_cache.record_capture_region(
                layers[0].width, layers[0].height,
                index != 0, capture_metrics,
                compact_bytes=(image.width() * image.height() * 4 if is_compact else None),
            )
    packet = dict(
        width=layers[0].width, height=layers[0].height,
        layers=tuple(layers),
        schema=2 if any(layer.inverse_transform is not None for layer in layers) else 1,
    )
    if any(isinstance(layer, DeferredNativeCanvasLayer) for layer in layers):
        return DeferredNativeCanvasFrame(**packet)
    return NativeCanvasFrame(**packet)
