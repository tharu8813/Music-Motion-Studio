"""Fixed overlaps shared by live preview and the FFmpeg export."""

from collections.abc import Sequence
from math import isfinite

from app.models.playlist import PlaylistTrack
from app.timeline.models import TransitionType
from app.timeline.render_plan import (
    AudioRenderClip, AudioRenderPlan, AudioRenderTransition, CompiledRenderPlan,
    build_presentation_and_metadata,
)


def compile_crossfade(tracks: Sequence[PlaylistTrack], seconds: float) -> CompiledRenderPlan:
    if isinstance(seconds, bool) or not isfinite(seconds) or seconds < 0:
        raise ValueError("Crossfade duration must be finite and non-negative.")
    clips = []
    transitions = []
    natural_cursor = actual_cursor = 0.0
    for track in tracks:
        if not track.enabled:
            continue
        natural_start = max(natural_cursor, track.start_time_seconds or 0.0)
        gap = natural_start - natural_cursor
        start = actual_cursor + gap
        if clips and gap <= 1e-6:
            previous = clips[-1]
            overlap = min(seconds, previous.duration / 2, track.duration_seconds / 2)
            start -= overlap
            if overlap > 1e-6:
                transitions.append(AudioRenderTransition(
                    clip_a=previous.clip_id, clip_b=f"crossfade:{track.id}",
                    timeline_start=start, duration=overlap, type=TransitionType.EQUAL_POWER,
                ))
        clip = AudioRenderClip(
            clip_id=f"crossfade:{track.id}", track_id=track.id, timeline_start=start,
            source_in=0.0, source_out=track.duration_seconds,
        )
        clips.append(clip)
        natural_cursor = natural_start + track.duration_seconds
        actual_cursor = clip.timeline_end
    presentation, metadata, duration = build_presentation_and_metadata(clips)
    return CompiledRenderPlan(AudioRenderPlan(tuple(clips), tuple(transitions)),
                              presentation, metadata, duration)
