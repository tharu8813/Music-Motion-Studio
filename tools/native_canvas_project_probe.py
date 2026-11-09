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
from contextlib import ExitStack
from bisect import bisect_right
import hashlib
import json
from math import isfinite
from pathlib import Path
import subprocess
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
from tools.native_canvas_dynamic_overlays import (
    DYNAMIC_TYPES, build_dynamic_stack, interleave_dynamic_layers,
    real_audio_dynamic_frames,
)
from app.renderer.export_timeline import ExportTimelinePlanner
from app.renderer.ffmpeg_renderer import FFmpegNotFoundError, RenderError
from app.renderer.python_visualizer import PythonVisualizerError
from app.services.project_service import ProjectService
from app.timeline.compiler import compile_playlist
from tools.native_canvas_real_lyrics_probe import _make_reference
from tools.native_canvas_timeline_probe import (
    FORMAT, MAX_FRAMES, MAX_DECODED_BYTES, TimelineValidationError, load_timeline,
)

# These sources have no independently decoded video, music-reactive signal,
# particle clock, or external FFmpeg overlay that this short GPU diagnostic
# can faithfully reproduce.
UNSUPPORTED_SOURCES = frozenset({
    SourceType.VIDEO, SourceType.AUDIO_VISUALIZER,
    SourceType.AUDIO_WAVEFORM, SourceType.AUDIO_LEVEL_METER,
    SourceType.PARTICLE_OVERLAY,
})


def project_source_inventory(project: ProjectDocument) -> list[dict[str, object]]:
    """Show project source eligibility without hiding media or reactive effects.

    This is a read-only diagnostic, never a way to downgrade a strict
    capture failure or bypass the explicit source policy.
    """
    inventory = []
    for source in project.sources:
        if not source.visible:
            status = "hidden"
        elif source.source_type in DYNAMIC_TYPES:
            status = "dynamic_audio_supported"
        elif source.source_type in UNSUPPORTED_SOURCES:
            status = "unsupported_video"
        elif source.uses_bass_reaction or (
            source.source_type is SourceType.BACKGROUND and source.background_bass_reactive
        ):
            status = "unsupported_audio_reactive_canvas"
        else:
            status = "canvas"
        inventory.append({
            "source_id": source.id,
            "name": source.name,
            "source_type": source.source_type.value,
            "z_index": source.z_index,
            "visible": source.visible,
            "dynamic_audio_status": status,
        })
    return inventory


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unsupported_visible_sources(project: ProjectDocument) -> list[dict[str, str]]:
    """List export-only dynamic layers that the Qt packet painter cannot reproduce.

    Export uses independent FFmpeg/Python overlay streams for these sources.
    Rendering the editor's static visualizer placeholder would create a
    deceptively valid H264 file, so such sources are excluded *only* by an
    explicitly selected canvas-only diagnostic policy.
    """
    omitted: list[dict[str, str]] = []
    for source in project.sources:
        if not source.visible:
            continue
        reason = None
        if source.source_type in UNSUPPORTED_SOURCES:
            reason = "requires_separate_export_overlay"
        elif source.uses_bass_reaction or (
            source.source_type is SourceType.BACKGROUND and source.background_bass_reactive
        ):
            reason = "requires_audio_analysis"
        if reason:
            omitted.append({
                "source_id": source.id,
                "source_type": source.source_type.value,
                "reason": reason,
            })
    return omitted


def validate_project_for_gpu_capture(
    project: ProjectDocument, *, scale: float, source_policy: str = "strict",
) -> list[dict[str, str]]:
    """Validate the frame capture and return any explicitly excluded sources.

    The default remains fail-closed. canvas-only must be opted into by the
    caller and is always reported as a PARTIAL project visual diagnostic.
    """
    if source_policy not in {"strict", "canvas-only", "dynamic-audio"}:
        raise TimelineValidationError("Source policy must be strict, canvas-only, or dynamic-audio")
    if project.settings.transition_mode != "none":
        raise TimelineValidationError(
            "Project GPU probe currently requires transition_mode=none "
            "(crossfade/AutoMix require dedicated junction evaluation)"
        )
    visible = [source for source in project.sources if source.visible]
    if not visible or len(visible) > 64:
        raise TimelineValidationError("Project GPU probe supports 1..64 visible sources")
    omitted = unsupported_visible_sources(project)
    if source_policy == "dynamic-audio":
        dynamic = [s for s in visible if s.source_type in DYNAMIC_TYPES]
        if not dynamic:
            raise TimelineValidationError(
                "dynamic-audio requires a visible audio visualizer, waveform, level meter or particle layer"
            )
        unsupported = [s for s in visible if s.source_type in UNSUPPORTED_SOURCES
                       and s.source_type not in DYNAMIC_TYPES]
        reactive = [s for s in visible if s.source_type not in DYNAMIC_TYPES and (
            s.uses_bass_reaction or
            (s.source_type is SourceType.BACKGROUND and s.background_bass_reactive)
        )]
        if unsupported or reactive:
            details = [
                f"{s.name or s.id} [{s.source_type.value}; {s.id}]"
                for s in unsupported
            ] + [
                f"{s.name or s.id} [{s.source_type.value}; {s.id}; audio-reactive Canvas]"
                for s in reactive if s not in unsupported
            ]
            raise TimelineValidationError(
                "dynamic-audio has unsupported project sources: "
                + ", ".join(details)
                + ". Video and audio-reactive Canvas sources require separate capture. "
                "Use --inspect-sources to list all project sources; "
                "--source-policy canvas-only is explicitly PARTIAL."
            )
        unsupported_motion = [
            s for s in dynamic
            if s.keyframes.animated or s.animation_in == "custom"
            or s.animation_out == "custom" or s.rotation
        ]
        if unsupported_motion:
            raise TimelineValidationError(
                "dynamic-audio cannot yet reproduce custom keyframes or rotation: "
                + ", ".join(f"{s.name or s.id} [{s.source_type.value}; {s.id}]"
                            for s in unsupported_motion)
            )
        omitted = []
    if omitted and source_policy == "strict":
        names = sorted({entry["source_type"] for entry in omitted})
        raise TimelineValidationError(
            f"Project contains separately-rendered dynamic sources: {', '.join(names)}. "
            "For a PARTIAL canvas-only GPU test, explicitly pass "
            "--source-policy canvas-only; omitted effects are NOT validated."
        )
    excluded_ids = {entry["source_id"] for entry in omitted}
    included = [source for source in visible if source.id not in excluded_ids]
    if not included:
        raise TimelineValidationError("No non-dynamic Canvas sources remain for this GPU test")
    for source in included:
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
    return omitted


def _z_bands(
    project: ProjectDocument, excluded_ids: set[str] | None = None,
) -> list[tuple[int | None, int | None]]:
    excluded_ids = excluded_ids or set()
    distinct = sorted({
        source.z_index for source in project.sources
        if source.visible and source.id not in excluded_ids
    })
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
    source_policy: str = "strict",
    prepared_audio: Path | None = None,
    ffmpeg: str | None = None,
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
    omitted = validate_project_for_gpu_capture(
        project, scale=scale, source_policy=source_policy,
    )
    excluded_ids = {item["source_id"] for item in omitted}
    dynamic_mode = source_policy == "dynamic-audio"
    dynamic_sources = [
        source for source in project.sources
        if dynamic_mode and source.visible and source.source_type in DYNAMIC_TYPES
    ]
    dynamic_ids = {source.id for source in dynamic_sources}
    included_sources = [
        source for source in project.sources
        if source.visible and source.id not in excluded_ids and source.id not in dynamic_ids
    ]
    plan = compile_playlist(project.playlist, enabled_only=True)
    if start_seconds + (frames - 1) / fps >= plan.duration_seconds:
        raise TimelineValidationError("Frame range exceeds compiled project duration")
    resolved = [
        resolve_project_frame(project, start_seconds + index / fps, plan=plan)
        for index in range(frames)
    ]
    stack = build_dynamic_stack(project, scale) if dynamic_mode else None
    bands = stack.bands if stack else _z_bands(project, excluded_ids)
    width = round(project.canvas.width * scale)
    height = round(project.canvas.height * scale)
    if width * height * 3 * frames > MAX_DECODED_BYTES:
        raise TimelineValidationError("Project RGB frame parity exceeds 256 MiB decode budget")
    layer_count = len(stack.order) if stack else len(bands)
    if width * height * 4 * layer_count * frames > 512 * 1024 * 1024:
        raise TimelineValidationError("Project native BGRA packets exceed 512 MiB disk budget")
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise TimelineValidationError(f"Refusing nonempty project capture folder: {out}")
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    scene = CanvasScene()
    scene.set_artboard_size(project.canvas.width, project.canvas.height)
    scene.show_grid = False
    for source in sorted(
        included_sources,
        key=lambda source: source.z_index,
    ):
        scene.addItem(SourceItem(source))

    original_document = project.to_dict()
    entries = []
    try:
        with ExitStack() as resources:
            dynamic_overlays, dynamic_streams, audio_origin = ([], [], None)
            if dynamic_mode:
                dynamic_overlays, dynamic_streams, audio_origin = resources.enter_context(
                    real_audio_dynamic_frames(
                        project, plan, fps=fps, scale=scale,
                        start_seconds=start_seconds, prepared_audio=prepared_audio,
                        ffmpeg=ffmpeg,
                    )
                )
                if len(dynamic_overlays) != len(stack.source_ids):
                    raise TimelineValidationError(
                        "Dynamic source/overlay count changed during export overlay creation"
                    )
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
                if dynamic_mode:
                    rendered = []
                    try:
                        for overlay, stream in zip(dynamic_overlays, dynamic_streams, strict=True):
                            rendered.append((overlay, next(stream)))
                    except StopIteration as exc:
                        raise TimelineValidationError(
                            "The real prepared audio ended before the requested frame window"
                        ) from exc
                    packet = interleave_dynamic_layers(packet, stack, rendered)
                manifest = save_native_canvas_frame(
                    packet, out, stem=f"frame-{index:04d}",
                )
                png = out / f"reference-{index:04d}.png"
                if png.exists() or not _make_reference(packet).save(str(png), "PNG"):
                    raise TimelineValidationError(f"Cannot write project reference PNG {index}")
                lyric_states = []
                for src in included_sources:
                    if src.source_type is SourceType.LYRICS:
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
                "source_project_sha256": _file_sha256(project_path),
                "source_count": len(included_sources) + len(dynamic_sources),
                "dynamic_sources": ([{"source_id": s.id, "source_type": s.source_type.value}
                                    for s in dynamic_sources] if dynamic_mode else []),
                "dynamic_audio_origin": audio_origin,
                "excluded_source_count": len(omitted),
                "excluded_sources": omitted,
                "capture_scope": (
                    "dynamic_overlay_audio_diagnostic" if dynamic_mode else
                    "partial_canvas_only" if omitted else "all_eligible_canvas_sources"
                ),
                "full_project_visual_parity_verified": False,
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




def _encode_and_verify(timeline_path: Path, args: argparse.Namespace) -> int:
    """Single fail-fast GPU pipeline. Never run Rust/FFprobe after capture fails."""
    from tools.native_canvas_timeline_probe import verify_video_parity

    timeline, _ = load_timeline(timeline_path)
    directory = timeline_path.parent
    video = directory / "project.h264"
    gpu_report = directory / "gpu-report.json"
    parity_report = directory / "parity.json"
    if any(file.exists() for file in (video, gpu_report, parity_report)):
        raise TimelineValidationError("Refusing to overwrite prior GPU probe output")
    command = [
        args.cargo, "run", "--release", "--manifest-path",
        "native/gpu_export_core/Cargo.toml",
        "--bin", "d3d11_qt_canvas_packet_probe", "--",
    ]
    for i in range(len(timeline["frames"])):
        command += ["--manifest", str(directory / f"frame-{i:04d}.json")]
    command += [
        "--repeat", "1", "--slots", "2",
        "--video-output", str(video), "--output", str(gpu_report),
    ]
    try:
        process = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=420,
        )
    except OSError as exc:
        raise TimelineValidationError(f"Cannot launch native encoder: {exc}") from exc
    if process.returncode:
        raise TimelineValidationError(
            "Native D3D11 encoder did not succeed: "
            + (process.stderr or process.stdout)[-1500:]
        )
    if not gpu_report.is_file():
        raise TimelineValidationError("Native encoder did not produce a GPU report")
    gpu = json.loads(gpu_report.read_text(encoding="utf-8"))
    if gpu.get("status") != "measured":
        raise TimelineValidationError(
            "Native H264 encoder did not run on compatible GPU: "
            f"{gpu.get('status')}: {gpu.get('reason', '')}"
        )
    if (gpu.get("frames_encoded") != len(timeline["frames"])
            or gpu.get("pts") != list(range(len(timeline["frames"])))
            or gpu.get("gpu_nv12_to_cpu_readbacks") != 0
            or not video.is_file()):
        raise TimelineValidationError("Native encoder reported incomplete PTS or missing H264")
    parity = verify_video_parity(
        timeline_path, video, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe,
    )
    with parity_report.open("x", encoding="utf-8") as output:
        json.dump(parity, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(json.dumps({
        "status": "verified", "gpu_report": str(gpu_report),
        "h264_file": str(video), "parity_report": str(parity_report),
        "frames_encoded": gpu["frames_encoded"],
        "capture_scope": timeline["capture_scope"],
        "excluded_sources": timeline["excluded_sources"],
        "dynamic_sources": timeline.get("dynamic_sources", []),
        "dynamic_audio_origin": timeline.get("dynamic_audio_origin"),
        "full_project_visual_parity_verified": False,
        "mean_rgb_mae": parity["mean_mae_codes"],
        "production_export_changed": False,
    }, indent=2, ensure_ascii=False))
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start-seconds", default=0.0, type=float)
    parser.add_argument("--frames", default=6, type=int)
    parser.add_argument("--fps", default=60, type=int)
    parser.add_argument("--scale", default=0.5, type=float)
    parser.add_argument(
        "--source-policy", choices=("strict", "canvas-only", "dynamic-audio"), default="strict",
        help="Default strict rejects unsupported effects; canvas-only explicitly "
             "omits them; dynamic-audio uses actual export audio and painter (diagnostics only)",
    )
    parser.add_argument(
        "--encode-and-verify", action="store_true",
        help="On Windows, run Rust D3D11/NVENC and per-frame H264 parity ONLY after successful capture",
    )
    parser.add_argument("--prepared-audio", type=Path, default=None,
                        help="Optional already normalized/mixed playlist audio for dynamic-audio")
    parser.add_argument("--inspect-sources", action="store_true",
                        help="Read-only source inventory; do not render, write or encode frames")
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)
    try:
        if args.inspect_sources:
            path = args.project
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in {".pvsproj", ".json"}:
                raise TimelineValidationError("Select an existing regular .pvsproj or project JSON")
            project = ProjectService.load(path)
            print(json.dumps({
                "status": "inspected",
                "project": str(path),
                "source_count": len(project.sources),
                "sources": project_source_inventory(project),
                "production_export_changed": False,
            }, indent=2, ensure_ascii=False))
            return 0
        path = capture_project(
            args.project, args.output_dir,
            start_seconds=args.start_seconds,
            frames=args.frames, fps=args.fps, scale=args.scale,
            source_policy=args.source_policy,
            prepared_audio=args.prepared_audio,
            ffmpeg=args.ffmpeg,
        )
        timeline, _ = load_timeline(path)
        print(json.dumps({
            "status": "captured", "schema": 1,
            "frames": len(timeline["frames"]),
            "source_count": timeline["source_count"],
            "excluded_sources": timeline["excluded_sources"],
            "capture_scope": timeline["capture_scope"],
            "dynamic_sources": timeline.get("dynamic_sources", []),
            "dynamic_audio_origin": timeline.get("dynamic_audio_origin"),
            "full_project_visual_parity_verified": False,
            "track_ids": [f["track_id"] for f in timeline["frames"]],
            "timeline": str(path),
            "resolution": [timeline["width"], timeline["height"]],
            "fps": timeline["fps"],
            "production_export_changed": False,
        }, indent=2, ensure_ascii=False))
        if args.encode_and_verify:
            if sys.platform != "win32":
                raise TimelineValidationError("Native D3D11/NVENC integration requires Windows")
            return _encode_and_verify(path, args)
        return 0
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired,
            FFmpegNotFoundError, RenderError, PythonVisualizerError) as exc:
        print(json.dumps({
            "status": "validation_failed", "reason": str(exc),
            "production_export_changed": False,
        }, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
