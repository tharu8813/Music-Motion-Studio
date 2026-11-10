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
import hashlib
import json
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
    """An ordered, full-frame BGRA layer with straight per-pixel alpha."""

    stream_key: str
    width: int
    height: int
    pixels: bytes
    opacity: float = 1.0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Native Canvas layer dimensions must be positive")
        if len(self.pixels) != self.width * self.height * 4:
            raise ValueError("Native Canvas layer BGRA bytes do not match dimensions")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("Native Canvas layer opacity must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class NativeCanvasFrame:
    """One source-order-stable Qt scene snapshot ready for D3D11 upload."""

    width: int
    height: int
    layers: tuple[NativeCanvasBgraLayer, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.layers) <= MAX_NATIVE_CANVAS_LAYERS:
            raise ValueError("Native Canvas supports 1..8 GPU layer streams")
        if self.width < 2 or self.height < 2 or self.width % 2 or self.height % 2:
            raise ValueError("Native Canvas export requires even dimensions >=2")
        if self.width > _MAX_DIMENSION[0] or self.height > _MAX_DIMENSION[1]:
            raise ValueError("Native Canvas export resolution exceeds 3840x2160")
        for layer in self.layers:
            if layer.width != self.width or layer.height != self.height:
                raise ValueError("All native GPU Canvas layers must be full-frame")
        if self.layers[0].opacity != 1.0:
            raise ValueError("First native Canvas layer must be opaque at global alpha 1")


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
        manifest = {
            "schema": 1,
            "format": "mms_canvas_bgra_layers_v1",
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
) -> NativeCanvasFrame:
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

    layers: list[NativeCanvasBgraLayer] = []
    for index, (z_min, z_max) in enumerate(z_bands):
        key = "base" if index == 0 else f"layer:{index - 1}"
        cached = raster_cache.get(key) if raster_cache is not None else None
        if cached is not None:
            layers.append(cached)
            continue
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
        )
        layers.append(NativeCanvasBgraLayer(
            stream_key="base" if index == 0 else f"layer:{index - 1}",
            width=image.width(), height=image.height(),
            pixels=straight_bgra_bytes(image),
        ))
        if raster_cache is not None:
            raster_cache.record(layers[-1])
    return NativeCanvasFrame(
        width=layers[0].width, height=layers[0].height,
        layers=tuple(layers),
    )
