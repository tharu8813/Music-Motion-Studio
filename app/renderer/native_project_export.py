"""UI-callable full project GPU export, independent of developer tools.

On a render worker pass a GuiProjectFrameSource created by the GUI, sharing
cancel_event. On the GUI/CLI thread the default lazy producer can be used
directly. No editor scenes or source models are mutated or passed to workers.
"""
from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event

from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer, ExportMetadata, RenderSettings, RenderError,
)
from app.renderer.native_gpu_stream import render_native_gpu_playlist
from app.renderer.native_project_frames import produce_complete_project_frames, _check_cancel
from app.renderer.native_dynamic_layers import build_dynamic_stack
from app.renderer.native_project_support import validate_project_for_gpu_capture
from app.timeline.compiler import compile_playlist


NATIVE_EXECUTABLE = "d3d11_qt_canvas_stream_export.exe"


def _require_developer_runtime():
    if not getattr(sys, "frozen", False):
        from scripts.rust_prerequisites import require_developer_environment
        require_developer_environment()


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
):
    """Render a detached project; always release the supplied GUI frame source."""
    try:
        return _export_project_document(
            project, output_path, native_exe=native_exe, fps=fps, scale=scale,
            ffmpeg=ffmpeg, cancel_event=cancel_event,
            progress_callback=progress_callback, frame_source=frame_source,
        )
    finally:
        if frame_source is not None:
            frame_source.close()


def _export_project_document(
    project, output_path, *, native_exe=None, fps=60, scale=0.5,
    ffmpeg=None, cancel_event=None, progress_callback=None, frame_source=None,
):
    """Render the entire detached document and return the usual RenderResult.

    progress_callback receives (accepted_frames, total_frames). Cancellation
    is cooperative. Worker callers must supply the GUI-owned frame bridge and
    keep the Qt event loop processing until its closed signal is delivered.
    """
    cancel = cancel_event or Event()
    _check_cancel(cancel)
    output = Path(output_path)
    if output.suffix.lower() != ".mp4" or output.exists():
        raise ValueError("Choose a new .mp4 output path; existing videos will not be overwritten")
    if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= 120:
        raise ValueError("Full native export FPS must be 1..120")
    project = deepcopy(project)
    validate_project_for_gpu_capture(project, scale=scale, source_policy="dynamic-audio")
    build_dynamic_stack(project, scale)
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
    settings = RenderSettings(
        fps=fps, video_codec="h264_nvenc",
        output_width=round(project.canvas.width * scale),
        output_height=round(project.canvas.height * scale),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = frame_source
    try:
        with TemporaryDirectory(prefix="mms-native-audio-") as directory:
            audio = renderer.prepare_playlist_audio(
                tracks, Path(directory), settings, transition_mode="none", cancel_event=cancel,
            )
            _check_cancel(cancel)
            if frames is None:
                frames = produce_complete_project_frames(
                    project, fps=fps, scale=scale, prepared_audio=audio,
                    ffmpeg=str(renderer.executable), cancel_event=cancel,
                )
            else:
                frames.prepare(audio, str(renderer.executable))
            return render_native_gpu_playlist(
                renderer, frames, executable=executable, tracks=tracks,
                compiled_plan=plan, prepared_audio_path=audio,
                output_path=output, settings=settings, cancel_event=cancel,
                progress_callback=progress_callback,
                metadata=ExportMetadata(title=project.settings.title,
                    artist=project.settings.author, comment=project.settings.description),
            )
    finally:
        if frames is not None:
            frames.close()
