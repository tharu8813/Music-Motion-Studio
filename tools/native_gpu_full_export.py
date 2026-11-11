"""Explicit full-playlist native GPU MP4 export (Windows developer CLI).

This is an actual end-to-end PRODUCT export path, not a 6-frame diagnostic:
* render all Qt Canvas + audio-reactive layer frames for every compiled PTS
* boundedly submit them to the persistent Rust D3D11/NVENC encoder
* mux its COMPLETE H264 with the existing prepared playlist audio + chapters

The existing graphical Export dialog is deliberately not switched on by this
CLI. An isolated RTX hardware test is required before surfacing that option.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
import json
import math
from time import monotonic
import hashlib
from pathlib import Path
import sys

from PySide6.QtWidgets import QApplication

from app.models.project import ProjectDocument
from app.services.project_service import ProjectService
from app.renderer.native_project_frames import produce_complete_project_frames
from app.renderer.native_project_export import export_project_document
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations


def make_short_smoke_project(
    original: ProjectDocument, seconds: float, *, fps: int,
) -> ProjectDocument:
    """Detach a true single-song clip for FAST real MP4 integration testing.

    A real full export of a long playlist may take hours of Qt rasterization.
    This opt-in mode exercises the *entire* GPU -> native H264 -> AAC/MP4 path
    using a shortened first song. It is deliberately not represented as a
    complete export of the original project. Neither the source file nor the
    original ProjectDocument is mutated.
    """
    if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
            or not math.isfinite(seconds) or not 1 <= seconds <= 30
            or not isinstance(fps, int) or not 1 <= fps <= 120
            or abs(seconds * fps - round(seconds * fps)) > 1e-7):
        raise ValueError(
            "A smoke clip must be 1..30 seconds on an exact export FPS frame boundary."
        )
    original_first = next(
        (track for track in original.playlist
         if track.enabled and track.duration_seconds > 0), None,
    )
    if original_first is None:
        raise ValueError("The project has no enabled audio track for the smoke clip.")
    if seconds > original_first.duration_seconds:
        raise ValueError(
            "Smoke clip duration cannot exceed the first enabled audio track."
        )
    clip = deepcopy(original)
    first = next(track for track in clip.playlist if track.id == original_first.id)
    first.duration_seconds = seconds
    first.start_time_seconds = None
    clip.playlist = [first]
    clip.settings.title = original.settings.title + " [short GPU smoke clip]"
    return clip


def export_complete_project(
    project_path: Path, output_path: Path, *, native_exe: Path | None = None,
    fps: int = 60, scale: float = 0.5, ffmpeg: str | None = None,
    smoke_seconds: float | None = None, report_path: Path | None = None,
    gpu_profile: str | None = None,
    gpu_profile_qt_paint: bool = False,
) -> Path:
    """Run actual complete GPU encoding + MP4 mux with the existing soundtrack."""
    source = Path(project_path)
    output = Path(output_path)
    if not source.is_file() or source.is_symlink() or source.suffix.lower() not in {
        ".pvsproj", ".json",
    }:
        raise ValueError("Choose a regular, existing .pvsproj or JSON project file")
    if output.suffix.lower() != ".mp4" or output.exists():
        raise ValueError("Choose a new .mp4 output path; existing videos will not be overwritten")
    if not 1 <= fps <= 120:
        raise ValueError("Full native export FPS must be 1..120")
    if report_path is not None and (Path(report_path).exists() or Path(report_path).is_symlink()
                                    or not Path(report_path).parent.is_dir()):
        raise ValueError("Choose a new diagnostic report in an existing directory")
    # Developer-only A/B switch. The regular application exposes no
    # optimization options, and always uses the automatic GPU profile.
    optimizations = (
        NativeGpuOptimizations.for_profile(gpu_profile)
        if gpu_profile is not None else None
    )
    if gpu_profile_qt_paint:
        if gpu_profile != "automatic":
            raise ValueError("--gpu-profile-qt-paint requires --gpu-profile automatic")
        optimizations = replace(optimizations, profile_canvas_source_paint=True)
    started = monotonic()
    project = ProjectService.load(source)
    if smoke_seconds is not None:
        project = make_short_smoke_project(project, smoke_seconds, fps=fps)
        print(
            f"[NATIVE GPU SMOKE] The first track was shortened to {smoke_seconds:g}s. "
            "This is a real short MP4, NOT a full export of the original project.",
            flush=True,
        )
    first_frame_at = None

    def report_progress(done: int, total: int) -> None:
        nonlocal first_frame_at
        if first_frame_at is None:
            first_frame_at = monotonic()
        if done == total or done == 1 or done % max(1, fps * 5) == 0:
            elapsed = max(1e-9, monotonic() - first_frame_at)
            measured_fps = (done - 1) / elapsed if done > 1 else 0.0
            # Approximate throughput, not an actual GPU utilization metric.
            suffix = (f" | observed {measured_fps:.2f} FPS "
                      f"({measured_fps / fps:.2f}x realtime)") if done > 1 else ""
            print(f"Native GPU frames accepted: {done}/{total}{suffix}", flush=True)
    result = export_project_document(
        project, output, native_exe=native_exe, fps=fps, scale=scale,
        ffmpeg=ffmpeg, progress_callback=report_progress,
        optimizations=optimizations,
    )
    from app.utils.performance import ENABLED, PROFILE
    if ENABLED:
        elapsed = monotonic() - started
        PROFILE.observe("export.total_wall_seconds", elapsed)
        PROFILE.observe("export.completed_wall_seconds", elapsed)
    if report_path is not None:
        with Path(report_path).open("x", encoding="utf-8") as writer:
            json.dump(dict(status="completed", source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                output=str(result.output_path), native_report=result.native_report,
                complete_cli_wall_seconds=monotonic()-started,
                scope="short_clip_gpu_smoke" if smoke_seconds is not None else "full_original_project"), writer, indent=2)
    return result.output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the COMPLETE project with real Qt Canvas and Rust D3D11 NVENC"
    )
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--native-exe", type=Path, default=None)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--scale", type=float, default=0.5)
    parser.add_argument("--ffmpeg", type=str, default=None)
    parser.add_argument(
        "--gpu-profile", choices=("automatic", "accelerated", "cpu_parallel", "standard"),
        default=None,
        help="Developer A/B only: automatic matches the normal application's Rust GPU engine",
    )
    parser.add_argument(
        "--gpu-profile-qt-paint", action="store_true",
        help="Developer-only Qt SourceItem.paint timing by source type (adds profiling overhead)",
    )
    parser.add_argument("--report", type=Path, default=None, help="Preserve developer diagnostics outside temporary staging")
    parser.add_argument(
        "--smoke-seconds", type=float, default=None,
        help="Create a real 1..30 second first-song MP4 test instead of the full playlist",
    )
    args = parser.parse_args(argv)
    try:
        if not getattr(sys, "frozen", False):
            from scripts.rust_prerequisites import require_developer_environment
            require_developer_environment()
        qt_app = QApplication.instance() or QApplication([])
        output = export_complete_project(
            args.project, args.output, native_exe=args.native_exe,
            fps=args.fps, scale=args.scale, ffmpeg=args.ffmpeg,
            smoke_seconds=args.smoke_seconds, report_path=args.report,
            gpu_profile=args.gpu_profile, gpu_profile_qt_paint=args.gpu_profile_qt_paint,
        )
    except Exception as exc:
        if args.report is not None and not args.report.exists():
            try:
                with args.report.open("x", encoding="utf-8") as writer:
                    json.dump(dict(status="failed", reason=str(exc)), writer, ensure_ascii=False, indent=2)
            except OSError:
                pass  # stderr remains the fallback if diagnostics cannot be created.
        print(json.dumps({
            "status": "native_full_export_failed",
            "reason": str(exc),
            "production_export_changed": False,
        }, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "completed",
        "output": str(output),
        "production_export_changed": True,
        "scope": "short_clip_gpu_smoke" if args.smoke_seconds is not None else "full_original_project",
        "smoke_seconds": args.smoke_seconds,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
