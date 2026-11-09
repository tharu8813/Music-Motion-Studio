"""Opt-in real-audio dynamic layers for the isolated Qt -> D3D11 GPU diagnostic.

The drawing and audio pipeline deliberately reuse the application's production
PythonVisualizerRenderer and ExportController overlay definitions. No production
exporter switch is enabled. This is a *diagnostic*, not yet visual parity against
FFmpeg's final layered video.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace

from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage, QPainter

from app.models.source import SourceType
from app.preview.native_canvas_layer_packets import (
    MAX_NATIVE_CANVAS_LAYERS, NativeCanvasBgraLayer, NativeCanvasFrame,
    straight_bgra_bytes,
)
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderSettings
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.timeline.render_plan import reactive_layer_windows
from app.utils.visualizer_geometry import visualizer_export_rect


DYNAMIC_TYPES = frozenset({
    SourceType.AUDIO_VISUALIZER, SourceType.AUDIO_WAVEFORM,
    SourceType.AUDIO_LEVEL_METER, SourceType.PARTICLE_OVERLAY,
})


@dataclass(frozen=True)
class DynamicStack:
    """Canvas bands, and the actual bottom-to-top native packet order."""

    bands: tuple[tuple[float | None, float | None], ...]
    order: tuple[tuple[str, int], ...]
    source_ids: tuple[str, ...]


def build_dynamic_stack(project, scale: float) -> DynamicStack:
    """Split static Canvas at each reactive Z position, preserving interleaving.

    Same-Z static/dynamic ordering is intentionally unsupported until it can
    match FFmpeg's tie-breaks exactly. The opaque Canvas base always comes first.
    """
    dynamic = [s for s in project.sources if s.visible and s.source_type in DYNAMIC_TYPES]
    if not dynamic:
        raise ValueError("Dynamic GPU diagnostic requires a visible audio overlay")
    dynamic.sort(key=lambda s: (
        s.z_index, visualizer_export_rect(s, scale)[1],
        visualizer_export_rect(s, scale)[0],
    ))
    static = [s for s in project.sources if s.visible and s.source_type not in DYNAMIC_TYPES]
    if any(s.z_index == d.z_index for s in static for d in dynamic):
        raise ValueError("Dynamic GPU diagnostic does not support static/dynamic equal-Z layers")

    bands: list[tuple[float | None, float | None]] = [(None, dynamic[0].z_index - 0.5)]
    order: list[tuple[str, int]] = [("canvas", 0)]
    for index, source in enumerate(dynamic):
        order.append(("dynamic", index))
        next_z = dynamic[index + 1].z_index if index + 1 < len(dynamic) else None
        if any(s.z_index > source.z_index and
               (next_z is None or s.z_index < next_z) for s in static):
            bands.append((source.z_index + 0.5, None if next_z is None else next_z - 0.5))
            order.append(("canvas", len(bands) - 1))
    if len(order) > MAX_NATIVE_CANVAS_LAYERS:
        raise ValueError("Dynamic GPU diagnostic exceeds the eight-layer NVENC packet limit")
    return DynamicStack(tuple(bands), tuple(order), tuple(s.id for s in dynamic))


def place_dynamic_overlay(image: QImage, x: int, y: int, width: int, height: int) -> NativeCanvasBgraLayer:
    """Place the production-sized RGBA layer on a full-frame straight-BGRA plane."""
    if image.isNull():
        raise ValueError("Cannot composite a missing dynamic overlay frame")
    canvas = QImage(width, height, QImage.Format.Format_ARGB32)
    canvas.fill(0)
    painter = QPainter(canvas)
    painter.drawImage(QPoint(x, y), image)
    painter.end()
    return NativeCanvasBgraLayer("pending", width, height, straight_bgra_bytes(canvas))


def interleave_dynamic_layers(
    static_frame: NativeCanvasFrame, stack: DynamicStack,
    rendered: list[tuple[object, QImage]],
) -> NativeCanvasFrame:
    """Keep native packet key convention while interleaving QPainter layers."""
    if len(rendered) != len(stack.source_ids):
        raise ValueError("Missing dynamic layers at a presentation timestamp")
    layers = []
    for kind, index in stack.order:
        if kind == "canvas":
            entry = static_frame.layers[index]
        else:
            overlay, image = rendered[index]
            entry = place_dynamic_overlay(
                image, int(overlay.x), int(overlay.y),
                static_frame.width, static_frame.height,
            )
        key = "base" if not layers else f"layer:{len(layers) - 1}"
        layers.append(NativeCanvasBgraLayer(
            key, static_frame.width, static_frame.height, entry.pixels, entry.opacity,
        ))
    return NativeCanvasFrame(static_frame.width, static_frame.height, tuple(layers))


def analyze_reactive_canvas_tracks(
    project, tracks, *, fps: int, ffmpeg: str | None = None,
) -> dict[object, object]:
    """Use the *production export* per-track music analysis, not mixed-audio FFT.

    ExportCanvasCapturer samples each track's own 24-band audio envelope
    (including source-specific lyric/text response profiles), while separate
    visualizer overlays analyze the normalized playlist mix. Combining those
    two analysis domains would silently change music-reactive Canvas pixels.
    Only tracks intersecting the requested diagnostic frames are decoded.
    Every source is still painted by the existing CanvasSnapshot renderer.
    """
    from app.models.source import SourceType

    reactive_sources = [
        source for source in project.sources
        if source.visible and source.uses_bass_reaction
    ]
    if not reactive_sources:
        return {}
    profiles = {
        source.music_reaction_profile
        for source in reactive_sources
        if source.source_type in {SourceType.TEXT, SourceType.LYRICS}
    }
    analyzer = PythonVisualizerRenderer(FFmpegRenderer(ffmpeg).executable)
    cancel = Event()
    envelopes: dict[object, object] = {}
    by_path: dict[Path, tuple[object, dict[tuple, object]]] = {}
    for track in tracks:
        path = Path(track.file_path).resolve()
        if not path.is_file():
            raise ValueError(
                f"Missing original track audio for music-reactive Canvas: {path}"
            )
        if path not in by_path:
            samples = analyzer._decode_mono_audio(path, cancel)
            levels = analyzer._analyze_levels(samples, fps, 24, cancel)
            bass = analyzer.bass_envelope(levels, fps)
            per_profile = {
                profile: analyzer.music_envelope(levels, fps, profile, cancel)
                for profile in profiles
            }
            by_path[path] = (bass, per_profile)
        bass, per_profile = by_path[path]
        envelopes[track.id] = bass
        for profile, values in per_profile.items():
            envelopes[(track.id, profile)] = values
    return envelopes


def canvas_reaction_at(
    track, elapsed_seconds: float, fps: int,
    sources, envelopes: dict[object, object],
) -> tuple[float, dict[str, float]]:
    """Resolve Canvas music signals by the same frame rounding as export."""
    if not envelopes:
        return 0.0, {}
    bass = envelopes.get(track.id)
    if bass is None:
        raise ValueError(f"Missing analyzed bass envelope for {track.title}")
    frame = max(0, round(elapsed_seconds * fps))
    bass_level = float(bass[min(frame, len(bass) - 1)])
    music_levels = {
        source.id: PythonVisualizerRenderer.music_reaction_level(
            envelopes.get((track.id, source.music_reaction_profile), bass),
            fps, elapsed_seconds, source.music_reactive_offset,
        )
        for source in sources
        if source.visible and source.source_type in {SourceType.TEXT, SourceType.LYRICS}
        and source.uses_bass_reaction
    }
    return bass_level, music_levels


@contextmanager
def real_audio_dynamic_frames(
    project, plan, *, fps: int, scale: float, start_seconds: float,
    prepared_audio: Path | None = None, ffmpeg: str | None = None,
) -> Iterator[tuple[list[object], list[Iterator[QImage]], str]]:
    """Decode the *real* normalized playlist mix and create export painter streams.

    This retains the producer's temporal attack/release history: seeking by
    slicing the audio at start_seconds would change spectrum response.
    """
    if abs(start_seconds * fps - round(start_seconds * fps)) > 1e-6:
        raise ValueError("Dynamic diagnostic start must be on an exact CFR frame boundary")
    # The audio envelope is analyzed from track start as in production, but
    # QPainter no longer has to draw every skipped frame before this seek.
    # Frame-count/disk budgets are separately enforced by the project probe.
    from app.controllers.export_controller import ExportOrchestrator

    active_tracks = [t for t in project.playlist if t.enabled and t.duration_seconds > 0]
    mock_window = SimpleNamespace(
        store=SimpleNamespace(sources=lambda: project.sources),
        playlist_service=SimpleNamespace(tracks=active_tracks),
    )
    overlays = ExportOrchestrator.visualizers(
        SimpleNamespace(window=mock_window),
        tracks=active_tracks, render_scale=1.0, compiled_plan=plan,
    )
    # Export production renders at >=1x and its geometry helper intentionally
    # clamps downscaling. Native diagnostic packets may be 0.25x, so scale
    # the *already resolved* export geometry rather than misplace effects.
    if scale != 1.0:
        overlays = [
            replace(
                item, x=round(item.x * scale), y=round(item.y * scale),
                width=max(8, round(item.width * scale)),
                height=max(8, round(item.height * scale)),
                line_width=item.line_width * scale,
                level_meter_gap=item.level_meter_gap * scale,
                particle_min_size=item.particle_min_size * scale,
                particle_max_size=item.particle_max_size * scale,
                frame_width=item.frame_width * scale,
                frame_inset=item.frame_inset * scale,
            )
            for item in overlays
        ]
    if len(overlays) == 0:
        raise ValueError("No export-compatible dynamic overlay was generated")
    renderer = FFmpegRenderer(ffmpeg)
    with TemporaryDirectory(prefix="mms-native-overlay-audio-") as temporary_dir:
        if prepared_audio is not None:
            audio_path = Path(prepared_audio)
            if not audio_path.is_file() or audio_path.is_symlink():
                raise ValueError("Prepared audio file must exist and cannot be a symlink")
            origin = "provided_prepared_audio"
        else:
            audio_path = renderer.prepare_playlist_audio(
                active_tracks, Path(temporary_dir),
                RenderSettings(fps=fps), transition_mode="none",
                audio_codec="aac",
            )
            origin = "application_export_mixed_audio"
        analyzer = PythonVisualizerRenderer(renderer.executable)
        levels, waveform, stereo = analyzer._audio_levels(audio_path, overlays, fps, Event(), None)
        windows = reactive_layer_windows(plan)
        streams = []
        for overlay in overlays:
            source_levels = waveform if overlay.kind == "waveform" else levels
            stream = analyzer._layer_frames(
                overlay, source_levels if source_levels is not None else levels,
                fps, Event(), None, stereo, windows,
                start_frame=round(start_seconds * fps),
            )
            streams.append(stream)
        yield overlays, streams, origin
