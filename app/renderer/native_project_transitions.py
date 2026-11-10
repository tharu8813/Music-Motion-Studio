"""Native frame ownership follows the existing export's compiled visual handover."""
from app.timeline.render_plan import visual_segments, visual_segment_at


def frame_window(plan, seconds):
    segments = visual_segments(plan)
    segment = visual_segment_at(segments, seconds)
    if segment is None or not segment.start <= seconds < segment.end:
        raise ValueError("Selected frame is outside compiled visual timeline")
    window = plan.presentation.windows[segment.window_index]
    if seconds >= window.timeline_end:
        raise ValueError("Selected frame is in a playlist presentation gap")
    return window


def transition_preflight_reason(mode):
    if mode == "none":
        return None
    return (
        f"Native GPU export cannot reproduce transition_mode={mode}: "
        "crossfade/AutoMix require the same compiled audio plan and junction "
        "frame evaluation as the existing engine. Use the existing FFmpeg "
        "export engine for this entire project; no partial GPU output is supported."
    )
