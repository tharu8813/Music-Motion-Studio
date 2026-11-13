"""Production audio-reactive native frame layers, in original Canvas Z order."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from math import inf, nextafter
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage, QPainter

from app.models.source import SourceType
from app.preview.native_canvas_layer_packets import (
    MAX_NATIVE_CANVAS_LAYERS, NativeCanvasBgraLayer, NativeCanvasFrame,
    DeferredNativeCanvasLayer, DeferredNativeCanvasFrame, NativeCanvasParticleLayer, straight_bgra_bytes,
    NativeCanvasVideoLayer, NativeCanvasRadialLayer,
)
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderSettings
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.timeline.render_plan import reactive_layer_windows
from app.utils.visualizer_geometry import visualizer_export_rect
from app.renderer.native_dynamic_motion import dynamic_layer_frame, dynamic_gpu_layer
from app.renderer.native_particle_layers import particle_layers, supports_native_particles
from app.renderer.native_radial_layers import radial_layers, supports_native_radial


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


@dataclass(frozen=True)
class PreparedProjectFrameAudio:
    overlays: list
    levels: object
    waveform: object
    stereo: object
    envelopes: dict


def prepare_project_frame_audio(project, plan, *, fps, scale, prepared_audio, ffmpeg, cancel_event):
    """Run expensive decoding/analysis on the render worker, without a scene."""
    from app.renderer.native_project_overlays import project_visualizers
    tracks = [t for t in project.playlist if t.enabled and t.duration_seconds > 0]
    overlays = project_visualizers(project, tracks, scale, plan)
    levels = waveform = stereo = None
    if overlays:
        analyzer = PythonVisualizerRenderer(FFmpegRenderer(ffmpeg).executable)
        levels, waveform, stereo = analyzer._audio_levels(
            Path(prepared_audio), overlays, fps, cancel_event, None,
        )
    envelopes = analyze_reactive_canvas_tracks(
        project, tracks, fps=fps, ffmpeg=ffmpeg, cancel_event=cancel_event,
    )
    return PreparedProjectFrameAudio(overlays, levels, waveform, stereo, envelopes)


def build_dynamic_stack(project, scale: float, *, gpu_video: bool = False) -> DynamicStack:
    """Match FFmpeg's dynamic-before-static tie order at every Z boundary."""
    dynamic_types = DYNAMIC_TYPES | {SourceType.VIDEO} if gpu_video else DYNAMIC_TYPES
    dynamic = [s for s in project.sources if s.visible and s.source_type in dynamic_types]
    if not dynamic:
        return DynamicStack(((None, None),), (("canvas", 0),), ())
    geometry = {source.id: visualizer_export_rect(source, scale) for source in dynamic}
    for source in dynamic:
        _x, _y, width, height = geometry[source.id]
        if width > 3840 or height > 2160:
            raise ValueError(f"{source.name or source.id}: GPU dynamic source texture exceeds 3840x2160; use existing FFmpeg export")
    dynamic.sort(key=lambda s: (
        s.z_index, geometry[s.id][1], geometry[s.id][0],
    ))
    if gpu_video and any(
        first.z_index == second.z_index
        and SourceType.VIDEO in (first.source_type, second.source_type)
        for first, second in zip(dynamic, dynamic[1:])
    ):
        raise ValueError("Native video requires a distinct Z position from other effects.")
    static = [s for s in project.sources if s.visible and s.source_type not in dynamic_types]
    bands: list[tuple[float | None, float | None]] = [
        (None, nextafter(dynamic[0].z_index, -inf)),
    ]
    order: list[tuple[str, int]] = [("canvas", 0)]
    for index, source in enumerate(dynamic):
        order.append(("dynamic", index))
        next_z = dynamic[index + 1].z_index if index + 1 < len(dynamic) else None
        if any(s.z_index >= source.z_index and
               (next_z is None or s.z_index < next_z) for s in static):
            bands.append((source.z_index, None if next_z is None else nextafter(next_z, -inf)))
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
    static_frame: NativeCanvasFrame | DeferredNativeCanvasFrame, stack: DynamicStack,
    rendered: list[tuple[object, QImage | NativeCanvasParticleLayer | NativeCanvasVideoLayer | NativeCanvasRadialLayer]],
    *, seconds: float = 0.0, output_scale: float = 1.0, gpu_affine: bool = False,
    raster_cache=None,
) -> NativeCanvasFrame | DeferredNativeCanvasFrame:
    """Keep native packet key convention when interleaving effect layers."""
    if len(rendered) != len(stack.source_ids):
        raise ValueError("Missing dynamic layers at a presentation timestamp")
    layers = []
    schema = max(static_frame.schema, 2 if gpu_affine else 1)
    for kind, index in stack.order:
        if kind == "canvas":
            entry = static_frame.layers[index]
        else:
            overlay, image = rendered[index]
            if isinstance(image, NativeCanvasVideoLayer):
                entry = image
                schema = max(schema, 5)
            elif isinstance(image, NativeCanvasRadialLayer):
                entry = image
                schema = 6
            elif isinstance(image, NativeCanvasParticleLayer):
                entry = image
                schema = max(schema, 3)
            else:
                entry = (dynamic_gpu_layer if gpu_affine else dynamic_layer_frame)(
                    image, overlay,
                    static_frame.width, static_frame.height,
                    seconds=seconds, output_scale=output_scale,
                    **({
                        "submit_bgra": (
                            raster_cache.maybe_submit_bgra
                            if raster_cache.adaptive_bgra_enabled
                            else raster_cache.submit_bgra
                        ),
                    } if gpu_affine and raster_cache is not None
                         and raster_cache.parallel_bgra_enabled else {}),
                )
        key = "base" if not layers else f"layer:{len(layers) - 1}"
        layers.append(replace(entry, stream_key=key))
    output = dict(
        width=static_frame.width, height=static_frame.height,
        layers=tuple(layers), schema=schema,
    )
    if any(isinstance(layer, DeferredNativeCanvasLayer) for layer in layers):
        return DeferredNativeCanvasFrame(**output)
    return NativeCanvasFrame(**output)


def analyze_reactive_canvas_tracks(
    project, tracks, *, fps: int, ffmpeg: str | None = None, cancel_event: Event | None = None,
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
    cancel = cancel_event or Event()
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
    cancel_event: Event | None = None,
    analysis: PreparedProjectFrameAudio | None = None,
    gpu_particles: bool = False,
    gpu_radial: bool = False,
) -> Iterator[tuple[list[object], list[Iterator[QImage | NativeCanvasParticleLayer | NativeCanvasRadialLayer]], str]]:
    """Decode the *real* normalized playlist mix and create export painter streams.

    This retains the producer's temporal attack/release history: seeking by
    slicing the audio at start_seconds would change spectrum response.
    """
    if abs(start_seconds * fps - round(start_seconds * fps)) > 1e-6:
        raise ValueError("Dynamic diagnostic start must be on an exact CFR frame boundary")
    # The audio envelope is analyzed from track start as in production, but
    # QPainter no longer has to draw every skipped frame before this seek.
    # Frame-count/disk budgets are separately enforced by the project probe.
    from app.renderer.native_project_overlays import project_visualizers

    active_tracks = [t for t in project.playlist if t.enabled and t.duration_seconds > 0]
    overlays = (analysis.overlays if analysis is not None
                else project_visualizers(project, active_tracks, scale, plan))
    if not overlays:
        yield [], [], "no_dynamic_layers"
        return
    cancel = cancel_event or Event()
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
                audio_codec="aac", cancel_event=cancel,
            )
            origin = "application_export_mixed_audio"
        analyzer = PythonVisualizerRenderer(renderer.executable)
        if analysis is None:
            levels, waveform, stereo = analyzer._audio_levels(audio_path, overlays, fps, cancel, None)
        else:
            levels, waveform, stereo = analysis.levels, analysis.waveform, analysis.stereo
        windows = reactive_layer_windows(plan)
        streams = []
        start_frame = round(start_seconds * fps)
        width = round(project.canvas.width * scale)
        height = round(project.canvas.height * scale)
        for overlay in overlays:
            source_levels = waveform if overlay.kind == "waveform" else levels
            source_levels = source_levels if source_levels is not None else levels
            if gpu_particles and supports_native_particles(overlay, width, height):
                streams.append(particle_layers(overlay, start_frame, len(source_levels), fps, cancel))
                continue
            if gpu_radial and supports_native_radial(overlay, width, height):
                streams.append(radial_layers(overlay, source_levels, start_frame, fps, windows, cancel,
                                             output_size=(width, height)))
                continue
            stream = analyzer._layer_frames(
                overlay, source_levels,
                fps, cancel, None, stereo, windows,
                start_frame=start_frame,
            )
            streams.append(stream)
        try:
            yield overlays, streams, origin
        finally:
            for stream in streams:
                stream.close()
