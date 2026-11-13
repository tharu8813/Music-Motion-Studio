"""Project GPU capture eligibility and complete timeline resolution."""
from math import isfinite
from pathlib import Path
from app.models.project import ProjectDocument
from app.models.source import SourceType
from app.renderer.export_timeline import ExportTimelinePlanner
from app.timeline.compiler import compile_playlist
from app.renderer.native_dynamic_layers import DYNAMIC_TYPES
from app.renderer.native_project_transitions import frame_window, transition_preflight_reason
from app.renderer.native_project_video import native_video_rect, supports_native_video, video_preflight_reason

class TimelineValidationError(ValueError):
    """Project cannot be represented faithfully by native GPU capture."""

UNSUPPORTED_SOURCES = frozenset({
    SourceType.VIDEO, SourceType.AUDIO_VISUALIZER,
    SourceType.AUDIO_WAVEFORM, SourceType.AUDIO_LEVEL_METER,
    SourceType.PARTICLE_OVERLAY,
})


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
    project: ProjectDocument, *, scale: float, source_policy: str = "strict", gpu_video: bool = False,
) -> list[dict[str, str]]:
    """Validate the frame capture and return any explicitly excluded sources.

    The default remains fail-closed. canvas-only must be opted into by the
    caller and is always reported as a PARTIAL project visual diagnostic.
    """
    if source_policy not in {"strict", "canvas-only", "dynamic-audio"}:
        raise TimelineValidationError("Source policy must be strict, canvas-only, or dynamic-audio")
    transition_reason = transition_preflight_reason(project.settings.transition_mode)
    if transition_reason:
        raise TimelineValidationError(transition_reason)
    visible = [source for source in project.sources if source.visible]
    if any(source.source_type not in set(SourceType) for source in visible):
        raise TimelineValidationError("Project has an unknown visible source type")
    if not visible or len(visible) > 64:
        raise TimelineValidationError("Project GPU probe supports 1..64 visible sources")
    omitted = unsupported_visible_sources(project)
    if source_policy == "dynamic-audio":
        unsupported = [s for s in visible if s.source_type in UNSUPPORTED_SOURCES
                       and s.source_type not in DYNAMIC_TYPES
                       and not (s.source_type is SourceType.VIDEO and gpu_video and supports_native_video(s))]
        reactive = [
            s for s in visible
            if s.source_type not in DYNAMIC_TYPES and s.uses_bass_reaction
            and s.source_type not in {
                SourceType.BACKGROUND, SourceType.TEXT, SourceType.LYRICS,
            }
        ]
        if unsupported or reactive:
            details = [
                video_preflight_reason(s) if s.source_type is SourceType.VIDEO
                else f"{s.name or s.id} [{s.source_type.value}; {s.id}]"
                for s in unsupported
            ] + [
                f"{s.name or s.id} [{s.source_type.value}; {s.id}; audio-reactive Canvas]"
                for s in reactive if s not in unsupported
            ]
            raise TimelineValidationError(
                "dynamic-audio has unsupported project sources: "
                + ", ".join(details)
                + " Use --inspect-sources to list all project sources; "
                "--source-policy canvas-only is explicitly PARTIAL."
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
        } and (not source.content_path or not Path(source.content_path).is_file()):
            raise TimelineValidationError(
                f"Image asset not found for project source {source.id}"
            )
    # Production Qt Canvas capture supports upscaling as well as downscaling.
    # The final even output dimensions remain bounded to 3840x2160 below.
    # The old diagnostic-only 1.0 ceiling incorrectly rejected e.g. a
    # 1280x720 project exported as 1920x1080 in the actual application.
    if not isfinite(scale) or not 0.25 <= scale <= 64.0:
        raise TimelineValidationError("GPU project output scale must be 0.25..64.0")
    width = round(project.canvas.width * scale)
    height = round(project.canvas.height * scale)
    if width < 64 or width > 3840 or height < 64 or height > 2160 or width % 2 or height % 2:
        raise TimelineValidationError(
            "Scaled project Canvas must be even and between 64x64 and 3840x2160"
        )
    if gpu_video and source_policy == "dynamic-audio":
        for source in visible:
            if source.source_type is SourceType.VIDEO:
                x, y, w, h = native_video_rect(source, scale)
                # FFmpeg's YUV 4:2:0 overlay aligns positions to a two-pixel grid.
                if not (0 <= x <= width - w and 0 <= y <= height - h
                        and all(value % 2 == 0 for value in (x, y, w, h))):
                    raise TimelineValidationError(video_preflight_reason(source))
    if not any(track.enabled and track.duration_seconds > 0 for track in project.playlist):
        raise TimelineValidationError("Project has no enabled tracks with positive duration")
    return omitted


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
    try:
        window = frame_window(plan, global_seconds)
    except ValueError as exc:
        raise TimelineValidationError(str(exc)) from exc
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
