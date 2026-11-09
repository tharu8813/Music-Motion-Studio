"""Bounded real-project Qt Canvas -> D3D11/NVENC packet diagnostic.

Loads an actual .pvsproj or legacy project JSON using ProjectService,
retains the document's own sources (including lyrics/animations/keyframes),
and maps 3..30 exact CFR PTS to the sequential compiled playlist.

The native GPU encoder remains the existing isolated
d3d11_qt_canvas_packet_probe. No production exporter/UI path is touched.

Example:
  python -m tools.native_canvas_project_probe \
      --project "My Playlist.pvsproj" --output-dir qt-project \
      --start-seconds 2.0 --frames 6 --fps 60 --scale 0.5

Then pass every qt-project/frame-*.json as --manifest to
d3d11_qt_canvas_packet_probe and check the H264 with
tools.native_canvas_timeline_probe verify.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
import hashlib
import json
from math import isfinite
from pathlib import Path
import sys

from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.project import ProjectDocument
from app.models.source import SourceType
from app.preview.frame_state import resolve_lyrics_cue_state
from app.preview.native_canvas_layer_packets import (
    capture_native_canvas_track_frame, save_native_canvas_frame,
)
from app.renderer.export_timeline import ExportTimelinePlanner
from app.services.project_service import ProjectService
from app.timeline.compiler import compile_playlist
from tools.native_canvas_real_lyrics_probe import _make_reference
from tools.native_canvas_timeline_probe import (
    FORMAT, MAX_FRAMES, TimelineValidationError, load_timeline,
)

# These sources have no independently decoded video, music-reactive signal,
# particle clock, or external FFmpeg overlay that this short GPU diagnostic
# can faithfully reproduce.
UNSUPPORTED_SOURCES = frozenset({
    SourceType.VIDEO, SourceType.AUDIO_VISUALIZER,
    SourceType.AUDIO_WAVEFORM, SourceType.AUDIO_LEVEL_METER,
    SourceType.PARTICLE_OVERLAY,
})


def validate_project_for_gpu_capture(project: ProjectDocument, *, scale: float) -> None:
    """Fail explicitly instead of silently omitting unsupported project layers."""
    if project.settings.transition_mode != "none":
        raise TimelineValidationError(
            "Project GPU probe currently requires transition_mode=none "
            "(crossfade/AutoMix require dedicated junction evaluation)"
        )
    visible = [source for source in project.sources if source.visible]
    if not visible or len(visible) > 64:
        raise TimelineValidationError("Project GPU probe supports 1..64 visible sources")
    if any(source.source_type in UNSUPPORTED_SOURCES for source in visible):
        names = sorted({
            source.source_type.value for source in visible
            if source.source_type in UNSUPPORTED_SOURCES
        })
        raise TimelineValidationError(
            f"Project contains separately-rendered dynamic sources: {', '.join(names)}"
        )
    if any(source.uses_bass_reaction for source in visible):
        raise TimelineValidationError(
            "Project contains music-reactive sources requiring an audio envelope"
        )
    if any(source.source_type is SourceType.BACKGROUND
           and source.background_bass_reactive for source in visible):
        raise TimelineValidationError(
            "Project contains an audio-reactive background without a sound envelope"
        )
    for source in visible:
        if source.source_type in {
            SourceType.IMAGE, SourceType.LOGO, SourceType.WATERMARK,
        } and source.content_path and not Path(source.content_path).is_file():
            raise TimelineValidationError(
                f"Image asset not found for project source {source.id}"
            )
    if not isfinite(scale) or not 0.25 <= scale <= 1.0:
        raise TimelineValidationError("Project diagnostic scale must be 0.25..1.0")
    width = round(project.canvas.width * scale)
    height = round(project.canvas.height * scale)
    if width < 64 or width > 3840 or height < 64 or height > 2160 or width % 2 or height % 2:
        raise TimelineValidationError(
            "Scaled project Canvas must be even and between 64x64 and 3840x2160"
        )
    if not any(track.enabled and track.duration_seconds > 0 for track in project.playlist):
        raise TimelineValidationError("Project has no enabled tracks with positive duration")


def _z_bands(project: ProjectDocument) -> list[tuple[int | None, int | None]]:
    distinct = sorted({source.z_index for source in project.sources if source.visible})
    if len(distinct) < 2:
        return [(None, None)]
    # Qt draws all lowest-Z items on an opaque artboard. All higher
    # items form one straight-alpha foreground; preserve their order
    # *inside* the band using Qt's actual SourceItem painter.
    return [(None, distinct[0]), (distinct[0] + 1, None)]


def resolve_project_frame(
    project: ProjectDocument,
    global_seconds: float,
    *,
    plan=None,
) -> dict:
    """Map one exact CFR presentation instant onto the compiled track clock."""
    if not isfinite(global_seconds) or global_seconds < 0:
        raise TimelineValidationError("Invalid nonnegative project presentation timestamp")
    plan = plan or compile_playlist(project.playlist, enabled_only=True)
    windows = plan.presentation.windows
    if not windows:
        raise TimelineValidationError("Compiled project has no presentation windows")
    starts = [window.timeline_start for window in windows]
    idx = bisect_right(starts, global_seconds) - 1
    if idx < 0:
        raise TimelineValidationError("Selected frame precedes project presentation")
    window = windows[idx]
    if not window.timeline_start <= global_seconds < window.timeline_end:
        raise TimelineValidationError(
            "Selected frame is in a playlist presentation gap"
        )
    tracks = [track for track in project.playlist if track.enabled and track.duration_seconds > 0]
    track_by_id = {track.id: track for track in tracks}
    track = track_by_id.get(window.track_id)
    if track is None:
        raise TimelineValidationError("Compiled presentation references missing playlist track")
    elapsed = window.source_time_at_start + (
        global_seconds - window.timeline_start
    ) * window.playback_rate
    if elapsed < -1e-9 or elapsed >= track.duration_seconds + 1e-9:
        raise TimelineValidationError("Compiled track elapsed time outside source duration")
    intro, outro = ExportTimelinePlanner._animation_durations(track, project.sources)
    phase = None
    progress = 1.0
    phase_duration = 0.0
    if intro > 0 and elapsed < intro:
        phase = "in"
        progress = min(1.0, elapsed / intro)
        phase_duration = intro
    elif outro > 0 and elapsed >= track.duration_seconds - outro:
        phase = "out"
        progress = min(1.0, (elapsed - (track.duration_seconds - outro)) / outro)
        phase_duration = outro
    return {
        "track": track,
        "track_number": tracks.index(track) + 1,
        "tracks": tracks,
        "window_start_seconds": window.timeline_start,
        "source_time_at_start": window.source_time_at_start,
        "track_elapsed_seconds": max(0.0, elapsed),
        "timeline_seconds": global_seconds,
        "animation_phase": phase,
        "animation_progress": progress,
        "animation_phase_duration": phase_duration,
        "compiled_plan": plan,
    }


def capture_project(
    project_path: Path, output_dir: Path, *,
    start_seconds: float = 0.0, frames: int = 6,
    fps: int = 60, scale: float = 0.5,
) -> Path:
    if (not 3 <= frames <= MAX_FRAMES or not 1 <= fps <= 120
            or not isfinite(start_seconds) or start_seconds < 0):
        raise TimelineValidationError("Requires 3..30 frames, 1..120 FPS, nonnegative start")
    project_path = Path(project_path)
    if not project_path.is_file() or project_path.is_symlink():
        raise TimelineValidationError("Project file missing or symlinked")
    if project_path.suffix.lower() not in {".pvsproj", ".json"}:
        raise TimelineValidationError("Select an existing .pvsproj or project JSON file")
    # ProjectService also resolves packaged media assets using the same
    # application-native loader as the GUI.
    project = ProjectService.load(project_path)
    validate_project_for_gpu_capture(project, scale=scale)
    plan = compile_playlist(project.playlist, enabled_only=True)
    if start_seconds + (frames - 1) / fps >= plan.duration_seconds:
        raise TimelineValidationError("Frame range exceeds compiled project duration")
    resolved = [
        resolve_project_frame(project, start_seconds + index / fps, plan=plan)
        for index in range(frames)
    ]
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise TimelineValidationError(f"Refusing nonempty project capture folder: {out}")
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    scene = CanvasScene()
    scene.set_artboard_size(project.canvas.width, project.canvas.height)
    scene.show_grid = False
    for source in sorted(
        (src for src in project.sources if src.visible),
        key=lambda source: source.z_index,
    ):
        scene.addItem(SourceItem(source))

    bands = _z_bands(project)
    original_document = project.to_dict()
    entries = []
    try:
        for index, state in enumerate(resolved):
            # A normal export worker keeps source state frozen; verify the
            # diagnostic did not mutate it between PTS captures.
            if project.to_dict() != original_document:
                raise TimelineValidationError("Project source state mutated between frames")
            packet = capture_native_canvas_track_frame(
                scene, state["track"], bands,
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
            )
            manifest = save_native_canvas_frame(
                packet, out, stem=f"frame-{index:04d}",
            )
            png = out / f"reference-{index:04d}.png"
            if png.exists() or not _make_reference(packet).save(str(png), "PNG"):
                raise TimelineValidationError(f"Cannot write project reference PNG {index}")
            lyric_states = []
            for src in project.sources:
                if src.visible and src.source_type is SourceType.LYRICS:
                    lyric = src.resolved_lyrics()
                    cue = resolve_lyrics_cue_state(
                        state["track"], state["track_elapsed_seconds"],
                        state["track"].lyrics_timing_offset_seconds,
                        lyric.subtitle_timing_offset, lyric.subtitle_animation,
                        lyric.subtitle_animation_duration,
                        whole_lines=lyric.subtitle_karaoke_enabled,
                    )
                    lyric_states.append({
                        "source_id": src.id,
                        "cue_index": cue.cue_index,
                        "active_cue_indices": list(cue.active_cue_indices),
                        "held_cue_indices": list(cue.held_cue_indices),
                        "before_first": cue.before_first,
                    })
            entries.append({
                "pts": index,
                "time_seconds": round(index / fps, 9),
                "timeline_seconds": round(state["timeline_seconds"], 9),
                "window_start_seconds": state["window_start_seconds"],
                "source_time_at_start": state["source_time_at_start"],
                "track_elapsed_seconds": round(state["track_elapsed_seconds"], 9),
                "track_id": state["track"].id,
                "track_number": state["track_number"],
                "animation_phase": state["animation_phase"],
                "manifest": manifest.name,
                "reference_png": png.name,
                "reference_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
                "lyric_states": lyric_states,
            })
        if project.to_dict() != original_document:
            raise TimelineValidationError("Project source state not restored by Qt renderer")
        packet_info = json.loads((out / entries[0]["manifest"]).read_text("utf8"))
        timeline = {
            "schema": 1, "format": FORMAT, "kind": "project_timeline",
            "width": packet_info["width"], "height": packet_info["height"],
            "fps": fps, "start_seconds": start_seconds,
            "frames": entries,
            "source_project_sha256": hashlib.sha256(project_path.read_bytes()).hexdigest(),
            "source_count": len([s for s in project.sources if s.visible]),
            "compiled_plan_duration_seconds": plan.duration_seconds,
            "qt_canvas_cpu_rasterized": True,
            "native_gpu_text_painting_implemented": False,
            "production_export_changed": False,
        }
        path = out / "timeline.json"
        with path.open("x", encoding="utf8") as writer:
            json.dump(timeline, writer, indent=2, ensure_ascii=False)
            writer.write("\n")
        load_timeline(path)
        return path
    except BaseException:
        for p in out.iterdir():
            if p.is_file() and not p.is_symlink():
                p.unlink()
        raise
    finally:
        scene.clear()
        _ = app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start-seconds", default=0.0, type=float)
    parser.add_argument("--frames", default=6, type=int)
    parser.add_argument("--fps", default=60, type=int)
    parser.add_argument("--scale", default=0.5, type=float)
    args = parser.parse_args(argv)
    try:
        path = capture_project(
            args.project, args.output_dir,
            start_seconds=args.start_seconds,
            frames=args.frames, fps=args.fps, scale=args.scale,
        )
        timeline, _ = load_timeline(path)
        print(json.dumps({
            "status": "captured", "schema": 1,
            "frames": len(timeline["frames"]),
            "source_count": timeline["source_count"],
            "track_ids": [f["track_id"] for f in timeline["frames"]],
            "timeline": str(path),
            "resolution": [timeline["width"], timeline["height"]],
            "fps": timeline["fps"],
            "production_export_changed": False,
        }, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({
            "status": "validation_failed", "reason": str(exc),
            "production_export_changed": False,
        }, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
