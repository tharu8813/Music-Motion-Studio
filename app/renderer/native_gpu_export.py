"""Production finalization for a COMPLETE native D3D11/NVENC playlist stream.

Unlike tools.native_canvas_project_probe, this adapter is part of the actual
export renderer boundary. It does not generate frames. It deliberately refuses
6-frame diagnostic captures, partial playlists, mismatched timestamps or
unverified GPU reports.

A future continuous native frame producer will provide the full-length H264
and a production-specific manifest; this adapter then reuses the existing
FFmpegRenderer production audio/metadata mux without re-encoding video.
"""
from __future__ import annotations

import json
from concurrent.futures import CancelledError
from dataclasses import replace
from fractions import Fraction
import math
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Event
from typing import TYPE_CHECKING
from app.utils.cancellable_subprocess import run_probe

from app.renderer.ffmpeg_renderer import (
    ExportMetadata, FFmpegRenderer, PreparedVideoInput, RenderCancelledError,
    RenderError, RenderResult, RenderSettings,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from app.models.playlist import PlaylistTrack
    from app.timeline.render_plan import CompiledRenderPlan


PRODUCTION_NATIVE_SCOPE = "production_complete_playlist"


def _load_native_production_report(
    report_path: Path, *, video_path: Path, width: int, height: int,
    fps: int, total_frames: int, duration_seconds: float,
) -> dict:
    if not report_path.is_file() or report_path.is_symlink():
        raise RenderError("The native GPU production report is missing.")
    if report_path.stat().st_size > 128 * 1024:
        raise RenderError("The native GPU production report is unreasonably large.")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RenderError("The native GPU production report is unreadable.") from exc
    if not isinstance(report, dict):
        raise RenderError("The native GPU production report must be an object.")
    # The earlier 6-frame diagnostic reports intentionally say
    # production_export_changed=false and omit production_export_scope.
    # Never treat their successful 6-frame parity as a full movie export.
    if (report.get("production_export_scope") != PRODUCTION_NATIVE_SCOPE
            or report.get("production_export_changed") is not True
            or report.get("status") != "measured"
            or report.get("d3d11_alpha_composition_on_gpu") is not True
            or report.get("nvenc_output_synchronously_completed") is not True
            or report.get("gpu_nv12_to_cpu_readbacks") != 0
            or report.get("source") != "real_qt_canvas_snapshot_cpu_raster_packets"
            or report.get("resolution") != [width, height]
            or report.get("fps") != fps
            or report.get("frames_encoded") != total_frames
            or report.get("expected_frames") != total_frames
            or report.get("pts_monotonic") is not True
            or not isinstance(report.get("duration_seconds"), (int, float))
            or not math.isfinite(report["duration_seconds"])
            or abs(report["duration_seconds"] - duration_seconds) > 1e-6):
        raise RenderError(
            "A complete, measured native GPU playlist export is required. "
            "Six-frame diagnostic reports cannot be converted into a complete MP4."
        )
    if report.get("h264_file") != str(video_path):
        raise RenderError("The GPU report does not refer to the requested video file.")
    return report


def _probe_complete_h264(
    ffprobe: str | Path, path: Path, *,
    width: int, height: int, fps: int, frame_count: int,
    cancel_event: Event | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RenderCancelledError("Native GPU export was cancelled.")
    try:
        arguments = [
            str(ffprobe), "-v", "error", "-f", "h264", "-count_frames",
            "-show_entries", "stream=codec_name,width,height,nb_read_frames,has_b_frames",
            "-of", "json", str(path),
        ]
        # Full frame counting is retained for correctness. A fixed 180s cap
        # incorrectly rejects long timelines even when decoding is progressing.
        process = run_probe(arguments, cancel_event=cancel_event or Event(),
                            timeout=max(180, 2 * frame_count / fps))
    except CancelledError as exc:
        raise RenderCancelledError("Native GPU export was cancelled.") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"Cannot probe the native GPU H264 stream: {exc}") from exc
    if process.returncode != 0:
        raise RenderError(
            "The native GPU H264 stream could not be decoded by FFprobe: "
            + process.stderr.decode("utf-8", "replace")[-500:]
        )
    try:
        stream, = json.loads(process.stdout)["streams"]
        if not isinstance(stream, dict):
            raise ValueError("stream must be an object")
        counted = int(stream["nb_read_frames"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RenderError("Native H264 stream metadata is invalid.") from exc
    # An Annex-B stream is NOT timestamped. ffprobe may report a doubled
    # r_frame_rate for true CFR H264, so the native report is the FPS
    # authority. Our synchronous NVENC encoder is configured with
    # frameIntervalP=1 (no B-frames), guaranteeing reorder-free GOPs for
    # elementary-stream PTS stamping. Require zero B-frames here before
    # FFmpegRenderer sets -r fps on input and validates the final MP4.
    if (stream.get("codec_name") != "h264"
            or stream.get("width") != width
            or stream.get("height") != height
            or counted != frame_count
            or stream.get("has_b_frames") != 0):
        raise RenderError(
            "Native GPU H264 does not match the complete playlist: "
            f"{counted}/{frame_count} frames, "
            f"{stream.get('width')}x{stream.get('height')}."
        )


def finalize_native_gpu_mp4(
    renderer: FFmpegRenderer, *, h264_path: Path, gpu_report_path: Path,
    prepared_audio_path: Path, tracks: list["PlaylistTrack"],
    compiled_plan: "CompiledRenderPlan", output_path: Path,
    settings: RenderSettings, ffprobe: str | Path = "ffprobe",
    metadata: ExportMetadata | None = None,
    cancel_event: Event | None = None,
    progress_callback: "Callable[[str, float, str], None] | None" = None,
    storage_path_callback=None,
) -> RenderResult:
    """Make the app's final MP4 from a complete GPU video and prepared AAC mix.

    The same FFmpegRenderer.render production code performs metadata, chapters,
    audio muxing, temporary output staging, cancellation and final validation.
    A native encoder must first produce EVERY CFR frame and report full scope.
    """
    cancel_event = cancel_event or Event()
    if cancel_event.is_set():
        raise RenderCancelledError("Native GPU export was cancelled.")
    if settings.video_codec != "h264_nvenc":
        raise RenderError("Native D3D11 export currently requires the h264_nvenc output codec.")
    renderer._validate_settings(settings)
    if compiled_plan is None or compiled_plan.duration_seconds <= 0:
        raise RenderError("Native GPU export requires the compiled playlist plan.")
    if (not tracks or any(not track.enabled or track.duration_seconds <= 0 for track in tracks)):
        raise RenderError("Native GPU export requires valid enabled playlist tracks.")
    video = Path(h264_path)
    if not video.is_file() or video.is_symlink() or video.suffix.lower() != ".h264":
        raise RenderError("A complete .h264 native GPU file is required.")
    audio = Path(prepared_audio_path)
    if not audio.is_file() or audio.is_symlink():
        raise RenderError("The prepared export soundtrack is missing.")
    duration = compiled_plan.duration_seconds
    expected_frames = math.ceil(duration * settings.fps - 1e-9)
    _load_native_production_report(
        Path(gpu_report_path), video_path=video,
        width=settings.output_width, height=settings.output_height,
        fps=settings.fps, total_frames=expected_frames, duration_seconds=duration,
    )
    _probe_complete_h264(
        ffprobe, video,
        width=settings.output_width, height=settings.output_height,
        fps=settings.fps, frame_count=expected_frames, cancel_event=cancel_event,
    )
    if cancel_event.is_set():
        raise RenderCancelledError("Native GPU export was cancelled.")
    target = Path(output_path).expanduser().resolve()
    if target.suffix.lower() != ".mp4":
        raise RenderError("Native GPU finalization requires an MP4 destination.")
    target.parent.mkdir(parents=True, exist_ok=True)

    def mux_progress(stage: str, fraction: float, message: str) -> None:
        if progress_callback is not None:
            progress_callback("Finalizing export" if stage == "Complete" else stage,
                              min(0.99, fraction), message)

    # Same filesystem: preserve an existing destination until all checks pass.
    with tempfile.TemporaryDirectory(prefix=".mms-native-mux-", dir=target.parent) as staging:
        if storage_path_callback is not None:
            storage_path_callback("output", Path(staging))
        staged = Path(staging) / "complete.mp4"
        try:
            result = renderer.render(
                PreparedVideoInput(
                    path=video, duration_seconds=duration,
                    width=settings.output_width, height=settings.output_height,
                    fps=settings.fps, ready_for_mux=True, encoded_codec="h264_nvenc",
                ),
                tracks, staged, settings,
                progress_callback=mux_progress, cancel_event=cancel_event,
                visualizers=[], static_layers=[], video_clips=[],
                metadata=metadata, compiled_plan=compiled_plan,
                prepared_audio_path=audio,
            )
            probe = run_probe([
                str(ffprobe), "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height,nb_frames,avg_frame_rate,duration",
                "-of", "json", str(staged),
            ], cancel_event=cancel_event, timeout=30)
            streams = json.loads(probe.stdout)["streams"]
            if not isinstance(streams, list) or any(not isinstance(s, dict) for s in streams):
                raise ValueError("MP4 streams must be objects")
            v, = [s for s in streams if s.get("codec_type") == "video"]
            a, = [s for s in streams if s.get("codec_type") == "audio"]
            video_duration = float(v["duration"])
            audio_duration = float(a["duration"])
            if (probe.returncode or v.get("codec_name") != "h264"
                    or v.get("width") != settings.output_width
                    or v.get("height") != settings.output_height
                    or int(v["nb_frames"]) != expected_frames
                    or Fraction(v["avg_frame_rate"]) != settings.fps
                    or not math.isfinite(video_duration)
                    or abs(video_duration - expected_frames / settings.fps) > 1 / settings.fps
                    or a.get("codec_name") != "aac"
                    or not math.isfinite(audio_duration)
                    or abs(audio_duration - duration) > max(0.12, 2 / settings.fps)):
                raise RenderError("Native GPU MP4 does not match the CFR video/audio timeline.")
            if cancel_event.is_set():
                raise RenderCancelledError("Native GPU export was cancelled.")
            # Windows rename refuses replacement; other hosts use an exclusive link.
            if os.name == "nt":
                staged.rename(target)
            else:
                os.link(staged, target)
            if progress_callback is not None:
                progress_callback("Complete", 1.0, "Export completed")
            return replace(result, output_path=target)
        except CancelledError as exc:
            raise RenderCancelledError("Native GPU export was cancelled.") from exc
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError,
                subprocess.TimeoutExpired) as exc:
            raise RenderError(f"Cannot finalize native GPU MP4: {exc}") from exc
