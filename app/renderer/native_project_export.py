"""UI-callable full project GPU export, independent of developer tools.

On a render worker pass a GuiProjectFrameSource created by the GUI, sharing
cancel_event. On the GUI/CLI thread the default lazy producer can be used
directly. No editor scenes or source models are mutated or passed to workers.
"""
from copy import deepcopy
from dataclasses import replace
from time import monotonic
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event

from app.models.source import SourceType
from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer, ExportMetadata, RenderSettings, RenderError, RenderCancelledError,
)
from app.renderer.native_gpu_stream import NativeGpuPreflightError, probe_native_gpu, render_native_gpu_playlist
from app.renderer.native_project_frames import produce_complete_project_frames, _check_cancel
from app.renderer.native_dynamic_layers import build_dynamic_stack
from app.renderer.native_project_support import validate_project_for_gpu_capture
from app.renderer.native_text_layers import supports_gpu_text
from app.renderer.native_project_video import prepare_native_video_sources
from app.services.video_encoder_service import DEFAULT_NATIVE_NVENC_PRESET
from app.timeline.compiler import compile_playlist


NATIVE_EXECUTABLE = "d3d11_qt_canvas_stream_export.exe"


def _require_developer_runtime():
    if not getattr(sys, "frozen", False):
        from scripts.rust_prerequisites import require_developer_environment
        require_developer_environment()
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QApplication
    if not isinstance(QApplication.instance(), QApplication):
        raise RenderError("Create a QApplication before native Canvas export.")
    if not QFontDatabase.families():
        raise RenderError("Qt has no fonts available. On Windows use QT_QPA_PLATFORM=windows, "
                          "or remove the offscreen override before exporting; missing glyphs cannot be exported safely.")


def resolve_native_executable(explicit=None):
    """Find bundled executable or this checkout's release build; never PATH."""
    if explicit is not None:
        candidates = [Path(explicit)]
    elif getattr(sys, "frozen", False):
        roots = [Path(sys.executable).parent]
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            roots.insert(0, Path(bundle))
        candidates = [root / "native" / NATIVE_EXECUTABLE for root in roots]
    else:
        root = Path(__file__).resolve().parents[2]
        candidates = [root / "native/gpu_export_core/target/release" / NATIVE_EXECUTABLE]
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    raise RenderError("Native GPU executable is missing. Expected: " + ", ".join(map(str, candidates)))


def export_project_document(
    project, output_path, *, native_exe=None, fps=60, scale=0.5,
    ffmpeg=None, cancel_event=None, progress_callback=None, frame_source=None,
    mux_progress_callback=None, audio_bitrate="192k", storage_path_callback=None,
    preparation_progress_callback=None, optimizations=None,
    native_nvenc_preset=DEFAULT_NATIVE_NVENC_PRESET, native_nvenc_cq=0,
):
    """Render a detached project; always release the supplied GUI frame source."""
    try:
        return _export_project_document(
            project, output_path, native_exe=native_exe, fps=fps, scale=scale,
            ffmpeg=ffmpeg, cancel_event=cancel_event,
            progress_callback=progress_callback, frame_source=frame_source,
            mux_progress_callback=mux_progress_callback, audio_bitrate=audio_bitrate,
            storage_path_callback=storage_path_callback,
            preparation_progress_callback=preparation_progress_callback,
            optimizations=optimizations,
            native_nvenc_preset=native_nvenc_preset, native_nvenc_cq=native_nvenc_cq,
        )
    finally:
        if frame_source is not None:
            frame_source.close()


def _export_project_document(
    project, output_path, *, native_exe=None, fps=60, scale=0.5,
    ffmpeg=None, cancel_event=None, progress_callback=None, frame_source=None,
    mux_progress_callback=None, audio_bitrate="192k", storage_path_callback=None,
    preparation_progress_callback=None, optimizations=None,
    native_nvenc_preset=DEFAULT_NATIVE_NVENC_PRESET, native_nvenc_cq=0,
):
    """Render the entire detached document and return the usual RenderResult.

    progress_callback receives (accepted_frames, total_frames). Cancellation
    is cooperative. Worker callers must supply the GUI-owned frame bridge and
    keep the Qt event loop processing until its closed signal is delivered.
    """
    started = monotonic()
    cancel = cancel_event or Event()
    _check_cancel(cancel)
    output = Path(output_path)
    if output.suffix.lower() != ".mp4" or output.exists():
        raise ValueError("Choose a new .mp4 output path; existing videos will not be overwritten")
    if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 120:
        raise ValueError("Full native export FPS must be 1..120")
    project = deepcopy(project)
    gpu_video = bool(optimizations and optimizations.gpu_video)
    validate_project_for_gpu_capture(project, scale=scale, source_policy="dynamic-audio", gpu_video=gpu_video)
    has_video = gpu_video and any(s.visible and s.source_type is SourceType.VIDEO for s in project.sources)
    stack = build_dynamic_stack(project, scale, gpu_video=has_video)
    if frame_source is not None and (
        frame_source.project.to_dict() != project.to_dict()
        or frame_source.fps != fps or frame_source.scale != scale
        or frame_source.cancel_event is not cancel
    ):
        raise RenderError("Frame source must match the export snapshot, settings and cancellation event.")
    _require_developer_runtime()
    executable = resolve_native_executable(native_exe)
    renderer = FFmpegRenderer(ffmpeg)
    tracks = [t for t in project.playlist if t.enabled and t.duration_seconds > 0]
    plan = compile_playlist(project.playlist, enabled_only=True)
    try:
        video_sources = prepare_native_video_sources(project, plan, scale, renderer.executable, cancel) if has_video else {}
    except RenderCancelledError:
        raise
    except RenderError as error:
        raise NativeGpuPreflightError(str(error)) from error
    settings = RenderSettings(
        fps=fps, video_codec="h264_nvenc",
        audio_bitrate=audio_bitrate,
        native_nvenc_preset=native_nvenc_preset, native_nvenc_cq=native_nvenc_cq,
        output_width=round(project.canvas.width * scale),
        output_height=round(project.canvas.height * scale),
    )
    required_schema = 2
    if optimizations is not None:
        if optimizations.gpu_particles and any(
            source.visible and source.source_type is SourceType.PARTICLE_OVERLAY
            and source.particle_style == "neon" for source in project.sources
        ):
            required_schema = 3
        if optimizations.gpu_text and any(
            source.visible and supports_gpu_text(source) for source in project.sources
        ):
            required_schema = 4
    if video_sources:
        required_schema = 5
    query_radial = bool(
        optimizations is not None and optimizations.gpu_radial and any(
            source.visible and source.source_type is SourceType.AUDIO_VISUALIZER
            and source.visualizer_style == "radial" for source in project.sources
        )
    )
    capabilities = {} if required_schema >= 4 or query_radial else None
    adapter_luid = probe_native_gpu(
        executable, settings, cancel_event=cancel, required_canvas_schema=required_schema,
        **({"capabilities": capabilities} if capabilities is not None else {}),
        **({"video_sources": tuple(video_sources.values())} if video_sources else {}),
        **({"query_radial": True} if query_radial else {}),
    )
    if capabilities is not None:
        layer_limit = (capabilities["canvas_layer_limit"] if required_schema >= 4
                       else optimizations.canvas_layer_limit)
        optimizations = replace(
            optimizations, canvas_layer_limit=layer_limit,
            gpu_radial=query_radial and capabilities.get("gpu_radial", False),
        )
        if video_sources and len(stack.order) > optimizations.canvas_layer_limit:
            raise NativeGpuPreflightError("This GPU has too few Canvas layers for the project's video and effect order.")
        if frame_source is not None:
            frame_source.optimizations = optimizations
            frame_source.video_sources = video_sources
    _check_cancel(cancel)
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = frame_source
    raster_counters = frame_source.raster_counters if frame_source is not None else {}
    try:
        with TemporaryDirectory(prefix="mms-native-audio-") as directory:
            if storage_path_callback is not None:
                storage_path_callback("audio", Path(directory))
            if preparation_progress_callback is not None:
                preparation_progress_callback("Preparing audio", 0.0, "Preparing GPU export audio and analysis")
            audio_started = monotonic()
            audio = renderer.prepare_playlist_audio(
                tracks, Path(directory), settings, transition_mode="none", cancel_event=cancel,
                progress_callback=preparation_progress_callback,
                audio_effects=project.settings.audio_effects,
            )
            audio_seconds = monotonic() - audio_started
            _check_cancel(cancel)
            if preparation_progress_callback is not None:
                preparation_progress_callback("Preparing visual frames", 1.0, "Analyzing audio for Canvas effects")
            if frames is None:
                frames = produce_complete_project_frames(
                    project, fps=fps, scale=scale, prepared_audio=audio,
                    ffmpeg=str(renderer.executable), cancel_event=cancel,
                    raster_counters=raster_counters,
                    optimizations=optimizations,
                    video_sources=video_sources,
                )
            else:
                frames.prepare(audio, str(renderer.executable))
            result = render_native_gpu_playlist(
                renderer, frames, executable=executable, tracks=tracks,
                compiled_plan=plan, prepared_audio_path=audio,
                output_path=output, settings=settings, cancel_event=cancel,
                progress_callback=progress_callback,
                mux_progress_callback=mux_progress_callback,
                optimizations=optimizations,
                adapter_luid=adapter_luid,
                **({"video_sources": tuple(video_sources.values())} if video_sources else {}),
                storage_path_callback=storage_path_callback,
                metadata=ExportMetadata(title=project.settings.title,
                    artist=project.settings.author, comment=project.settings.description),
            )
            report = dict(result.native_report or {})
            report.update(raster_cache=dict(raster_counters), audio_prepare_seconds=audio_seconds,
                          completed_wall_seconds=monotonic() - started)
            return replace(result, native_report=report)
    finally:
        if frames is not None:
            frames.close()
