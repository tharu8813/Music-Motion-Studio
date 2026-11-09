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
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Iterator

from PySide6.QtWidgets import QApplication

from app.canvas.scene import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.project import ProjectDocument
from app.preview.native_canvas_layer_packets import (
    NativeCanvasFrame, capture_native_canvas_track_frame,
)
from app.renderer.ffmpeg_renderer import ExportMetadata, FFmpegRenderer, RenderSettings
from app.renderer.native_gpu_stream import render_native_gpu_playlist
from app.services.project_service import ProjectService
from app.timeline.compiler import compile_playlist
from tools.native_canvas_dynamic_overlays import (
    DYNAMIC_TYPES, analyze_reactive_canvas_tracks, build_dynamic_stack,
    canvas_reaction_at, interleave_dynamic_layers, real_audio_dynamic_frames,
)
from tools.native_canvas_project_probe import (
    TimelineValidationError, resolve_project_frame,
    validate_project_for_gpu_capture,
)


def produce_complete_project_frames(
    project: ProjectDocument, *, fps: int, scale: float,
    prepared_audio: Path, ffmpeg: str,
) -> Iterator[NativeCanvasFrame]:
    """Yield *every* playlist CFR frame lazily in true source Z order.

    Unlike the diagnostic project probe, this function does not create
    reference PNGs, packet JSONs, an in-memory frame array or a 30-frame
    cap. The Rust transport takes each completed packet one at a time.
    """
    validate_project_for_gpu_capture(
        project, scale=scale, source_policy="dynamic-audio",
    )
    plan = compile_playlist(project.playlist, enabled_only=True)
    tracks = [t for t in project.playlist if t.enabled and t.duration_seconds > 0]
    if not tracks or plan.duration_seconds <= 0:
        raise TimelineValidationError("A complete native export needs enabled playlist tracks")
    stack = build_dynamic_stack(project, scale)
    included = [
        s for s in project.sources
        if s.visible and s.source_type not in DYNAMIC_TYPES
    ]
    reactive = [s for s in included if s.uses_bass_reaction]
    QApplication.instance() or QApplication([])
    scene = CanvasScene()
    scene.set_artboard_size(project.canvas.width, project.canvas.height)
    scene.show_grid = False
    for source in sorted(included, key=lambda item: item.z_index):
        scene.addItem(SourceItem(source))
    before = project.to_dict()
    try:
        with ExitStack() as resources:
            overlays, dynamic_streams, _origin = resources.enter_context(
                real_audio_dynamic_frames(
                    project, plan, fps=fps, scale=scale, start_seconds=0.0,
                    prepared_audio=prepared_audio, ffmpeg=ffmpeg,
                )
            )
            if len(overlays) != len(stack.source_ids):
                raise TimelineValidationError("Dynamic overlay count differs from original project")
            envelopes = analyze_reactive_canvas_tracks(
                project, tracks, fps=fps, ffmpeg=ffmpeg,
            ) if reactive else {}
            total_frames = math.ceil(plan.duration_seconds * fps - 1e-9)
            for frame_index in range(total_frames):
                state = resolve_project_frame(project, frame_index / fps, plan=plan)
                bass, music = canvas_reaction_at(
                    state["track"], state["track_elapsed_seconds"], fps,
                    reactive, envelopes,
                )
                static = capture_native_canvas_track_frame(
                    scene, state["track"], stack.bands,
                    elapsed_seconds=state["track_elapsed_seconds"],
                    output_scale=scale,
                    track_number=state["track_number"],
                    track_total=len(state["tracks"]),
                    track_start_seconds=state["window_start_seconds"],
                    timeline_seconds=state["timeline_seconds"],
                    playlist_duration_seconds=plan.duration_seconds,
                    playlist_tracks=state["tracks"],
                    animation_phase=state["animation_phase"],
                    animation_progress=state["animation_progress"],
                    animation_phase_duration=state["animation_phase_duration"],
                    compiled_plan=plan,
                    bass_level=bass,
                    music_levels=music,
                )
                try:
                    layers = [
                        (overlay, next(stream))
                        for overlay, stream in zip(overlays, dynamic_streams, strict=True)
                    ]
                except StopIteration as exc:
                    raise TimelineValidationError(
                        f"Audio-reactive overlay ended at frame {frame_index}/{total_frames}"
                    ) from exc
                yield interleave_dynamic_layers(static, stack, layers)
            if project.to_dict() != before:
                raise TimelineValidationError("Native export unexpectedly modified project sources")
    finally:
        scene.clear()


def export_complete_project(
    project_path: Path, output_path: Path, *, native_exe: Path,
    fps: int = 60, scale: float = 0.5, ffmpeg: str | None = None,
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
    project = ProjectService.load(source)
    validate_project_for_gpu_capture(
        project, scale=scale, source_policy="dynamic-audio",
    )
    renderer = FFmpegRenderer(ffmpeg)
    tracks = [track for track in project.playlist if track.enabled and track.duration_seconds > 0]
    plan = compile_playlist(project.playlist, enabled_only=True)
    if not tracks or plan.duration_seconds <= 0:
        raise ValueError("At least one enabled soundtrack with positive duration is required")
    width, height = round(project.canvas.width * scale), round(project.canvas.height * scale)
    settings = RenderSettings(
        fps=fps, video_codec="h264_nvenc", output_width=width,
        output_height=height,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mms-native-audio-") as raw_directory:
        prepared_audio = renderer.prepare_playlist_audio(
            tracks, Path(raw_directory), settings, transition_mode="none",
        )
        frames = produce_complete_project_frames(
            project, fps=fps, scale=scale,
            prepared_audio=prepared_audio, ffmpeg=str(renderer.executable),
        )
        def report_progress(done: int, total: int) -> None:
            if done == total or done == 1 or done % max(1, fps * 5) == 0:
                print(f"Native GPU frames accepted: {done}/{total}", flush=True)
        result = render_native_gpu_playlist(
            renderer, frames, executable=Path(native_exe), tracks=tracks,
            compiled_plan=plan, prepared_audio_path=prepared_audio,
            output_path=output, settings=settings, progress_callback=report_progress,
            metadata=ExportMetadata(
                title=project.settings.title,
                artist=project.settings.author,
                comment=project.settings.description,
            ),
        )
    return result.output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the COMPLETE project with real Qt Canvas and Rust D3D11 NVENC"
    )
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--native-exe", required=True, type=Path)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--scale", type=float, default=0.5)
    parser.add_argument("--ffmpeg", type=str, default=None)
    args = parser.parse_args(argv)
    try:
        output = export_complete_project(
            args.project, args.output, native_exe=args.native_exe,
            fps=args.fps, scale=args.scale, ffmpeg=args.ffmpeg,
        )
    except Exception as exc:
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
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
