"""Frame Evaluation Layer: pure per-source state resolvers shared by the
live Canvas preview (CanvasSnapshot.capture_track) and export's frame
coalescing (ExportCanvasCapturer._source_state_key).

Both call sites used to independently re-derive the same timing math -- one
to paint it, one to build a cache key -- with a real risk of the two
drifting apart. resolve_lyrics_cue_state(), resolve_now_playing_exit_state(),
and resolve_timeline_window_phase() pull that duplicated computation out
into pure, testable functions; both call sites now resolve the same
FrameState value instead of duplicating the formula. Background cross-fade
blend and text template expansion were checked and found to already be
single-line/shared, not worth extracting.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence

from app.animation.curves import ease_in_out_cubic
from app.animation.lyrics import lyric_progress
from app.models.playlist import PlaylistTrack
from app.models.source import Source
from app.services.lyrics_service import LyricsService


def lyric_instrumental_windows(track: PlaylistTrack, source: Source) -> tuple[tuple[float, float], ...]:
    """Audio-clock candidates; LRC holds each cue until the next start.

    A long held LRC cue needs measured vocals to find the actual phrase end.
    Explicit subtitle ends remain the lower bound for an interlude.
    """
    offset = track.lyrics_timing_offset_seconds + source.subtitle_timing_offset
    cues = [cue for cue in track.lyrics if str(cue.get("text", "")).strip()]
    if source.subtitle_karaoke_enabled:
        cues = [cue for cue in cues if cue.get("format") == "ttml"]
        # Measure silence between the union of intervals; a later, shorter cue
        # ending must not turn an older sustained lyric into an interlude.
        windows = []
        running_end = 0.0
        for cue in cues:
            start, end = float(cue["start"]), float(cue["end"])
            lower, upper = max(0.0, running_end - offset), min(track.duration_seconds, start - offset)
            if running_end and upper - lower >= source.subtitle_intro_gap:
                windows.append((lower, upper))
            running_end = max(running_end, end)
        return tuple(windows)
    windows = []
    for previous, following in zip(cues, cues[1:]):
        start, end = float(previous["start"]), float(previous["end"])
        next_start = float(following["start"])
        lower = end if source.subtitle_karaoke_enabled or end < next_start - 0.001 else start + min(2.0, end - start)
        lower, upper = max(0.0, lower - offset), min(track.duration_seconds, next_start - offset)
        if upper - lower >= source.subtitle_intro_gap:
            windows.append((lower, upper))
    return tuple(windows)


@dataclass(slots=True, frozen=True)
class LyricsIntroState:
    elapsed: float
    remaining: float
    opacity: float
    before_first: bool = False
    raw_progress: float = 1.0
    progress: float = 1.0
    exiting: bool = False
    held_cue_index: int | None = None
    held_slot: int = -1


def resolve_lyrics_intro_state(track: PlaylistTrack, source: Source, elapsed: float,
                              instrumental_spans=()) -> LyricsIntroState | None:
    """First-cue wait or a measured instrumental interlude, on the audio clock."""
    if not (source.subtitle_intro_enabled or source.subtitle_intro_midtrack):
        return None
    if source.subtitle_karaoke_enabled and not any(cue.get("format") == "ttml" for cue in track.lyrics):
        return None  # Ordinary real-time lyrics display the first complete line immediately.
    first_cue = next((cue for cue in track.lyrics if str(cue.get("text", "")).strip()), None)
    if first_cue is None:
        return None  # Missing lyrics are not an intro.
    first = float(first_cue["start"]) - track.lyrics_timing_offset_seconds - source.subtitle_timing_offset
    duration = source.subtitle_animation_duration if source.subtitle_animation != "none" else 0.0
    before_first = source.subtitle_intro_enabled and first > 0 and 0.0 <= elapsed < first + duration
    window = (0.0, first) if before_first else None
    if window is None and source.subtitle_intro_midtrack:
        window = next(((a, b) for a, b in instrumental_spans
                       if b - a >= source.subtitle_intro_gap and a <= elapsed < b + duration), None)
    if window is None:
        return None
    age, remaining = elapsed - window[0], window[1] - elapsed
    exiting = remaining <= 0
    raw = min(1.0, (elapsed - window[1] if exiting else age) / duration) if duration else 1.0
    progress = lyric_progress(source, raw)
    held = None if before_first else LyricsService.display_cue_index(
        track.lyrics, window[0] + track.lyrics_timing_offset_seconds + source.subtitle_timing_offset,
    )
    if source.subtitle_karaoke_enabled and held is not None:
        held = LyricsService.last_ending_cue_indices(track.lyrics,
            window[0] + track.lyrics_timing_offset_seconds + source.subtitle_timing_offset)[0]
    return LyricsIntroState(age, remaining, 1 - progress if exiting else progress,
                           before_first, raw, progress, exiting, held)


@dataclass(slots=True, frozen=True)
class LyricsCueState:
    """Which lyric cue is current at a point in playback, and how far its
    entrance transition has progressed (raw, unEASED -- callers that need an
    eased value apply their own easing curve to transition_progress)."""

    cue_index: int | None
    active_cue_index: int | None
    transitioning: bool
    transition_progress: float
    cue_start_seconds: float | None
    # Raw progress of the held cue's highlight fading out after its end, while
    # a gap leaves it on screen (None: not in such a gap, or no animation).
    release_progress: float | None = None
    # Highlight the previous cue still had when this cue started: 1.0 for
    # back-to-back cues, lower when a gap had already released it.
    previous_emphasis: float = 1.0
    active_cue_indices: tuple[int, ...] = ()
    latest_started_cue_index: int | None = None
    gap_unblurred: bool = False
    held_cue_indices: tuple[int, ...] = ()


def resolve_lyrics_cue_state(
    track: PlaylistTrack,
    elapsed_seconds: float,
    lyrics_timing_offset_seconds: float,
    subtitle_timing_offset: float,
    subtitle_animation: str,
    subtitle_animation_duration: float,
    *, whole_lines: bool = False,
) -> LyricsCueState:
    """Resolve shared preview/export clocks, including overlapping TTML cues."""
    effective_offset = lyrics_timing_offset_seconds + subtitle_timing_offset
    lyric_elapsed = max(0.0, elapsed_seconds + effective_offset)
    active_cue_index = LyricsService.current_cue_index(track.lyrics, lyric_elapsed)
    cue_index = LyricsService.display_cue_index(track.lyrics, lyric_elapsed)
    lyric_cue = track.lyrics[cue_index] if cue_index is not None else None
    latest_started = cue_index
    active_indices: tuple[int, ...] = ()
    held_indices: tuple[int, ...] = ()
    realtime_ttml = whole_lines and lyric_cue is not None and lyric_cue.get("format") == "ttml"
    if realtime_ttml:
        active_indices = LyricsService.active_cue_indices(track.lyrics, lyric_elapsed)
        if active_indices:
            cue_index = active_cue_index = active_indices[0]
            lyric_cue = track.lyrics[cue_index]
        elif lyric_elapsed >= float(lyric_cue["end"]):
            # Keep the last lyric that actually finished, rather than jumping
            # back to a later-starting phrase that had already ended earlier.
            held_indices = LyricsService.last_ending_cue_indices(track.lyrics, lyric_elapsed)
            cue_index = held_indices[0]
            lyric_cue = track.lyrics[cue_index]
    continuous = whole_lines and lyric_cue is not None and lyric_cue.get("format") != "ttml"
    if continuous:
        active_cue_index = cue_index
    transitioning = (
        cue_index is not None
        and active_cue_index == cue_index
        and lyric_cue is not None
        and subtitle_animation != "none"
        and len(active_indices) <= 1
    )
    duration = max(0.05, subtitle_animation_duration)
    transition_progress = 1.0
    cue_start_seconds: float | None = None
    release_progress: float | None = None
    previous_emphasis = 1.0
    if transitioning:
        cue_start_seconds = float(lyric_cue.get("start", lyric_elapsed)) - effective_offset
        if continuous and cue_index == 0:
            cue_start_seconds = -duration  # The first line is already fully visible at playback start.
        transition_progress = max(0.0, min(1.0, (
            (elapsed_seconds - cue_start_seconds) / duration
        )))
        if cue_index > 0 and not continuous:
            previous = track.lyrics[cue_index - 1]
            previous_start = float(previous.get("start", 0.0))
            gap = float(lyric_cue.get("start", 0.0)) - float(previous.get("end", previous_start + 8.0))
            previous_emphasis = 1.0 - ease_in_out_cubic(gap / duration)
    elif (
        lyric_cue is not None and active_cue_index is None
        and subtitle_animation != "none"
    ):
        cue_start = float(lyric_cue.get("start", 0.0))
        cue_end = float(lyric_cue.get("end", cue_start + 8.0))
        if lyric_elapsed >= cue_end:
            release_progress = min(1.0, (lyric_elapsed - cue_end) / duration)
    return LyricsCueState(
        cue_index=cue_index,
        active_cue_index=active_cue_index,
        transitioning=transitioning,
        transition_progress=transition_progress,
        cue_start_seconds=cue_start_seconds,
        release_progress=release_progress,
        previous_emphasis=previous_emphasis,
        active_cue_indices=active_indices,
        latest_started_cue_index=latest_started,
        gap_unblurred=bool(realtime_ttml and not active_indices and lyric_elapsed >= float(lyric_cue["end"])),
        held_cue_indices=held_indices,
    )


class _LyricDisplayGroups(Sequence):
    """Prepare text only for the small context window actually being painted."""

    def __init__(self, cues, groups):
        self.cues, self.groups, self.prepared = cues, groups, {}
        self.positions = {original: index for index, group in enumerate(groups) for original in group}

    def __len__(self):
        return len(self.groups)

    def index_for_cue(self, index):
        return self.positions[index]

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[position] for position in range(*index.indices(len(self))))
        if index not in self.prepared:
            indices = self.groups[index]
            parts = [self.cues[position] for position in indices]
            self.prepared[index] = {
                "display_cue_indices": indices,
                "text": "\n".join(LyricsService.decode_line_breaks(cue.get("text", "")).strip() for cue in parts),
                "display_line_positions": tuple(position for cue in parts for position, _line in enumerate(
                    line for line in LyricsService.decode_line_breaks(cue.get("text", "")).strip().splitlines() if line.strip())),
            }
        return self.prepared[index]


def lyrics_display_groups(track: PlaylistTrack, state: LyricsCueState):
    """Treat all current TTML cues as one centered block in the visual window.

    Ended cues between two active intervals belong to history, never to the
    current block. This does not rewrite or merge the track's timing data.
    """
    if not state.active_cue_indices and not state.gap_unblurred:
        return track.lyrics, state.cue_index
    displayed = state.active_cue_indices or state.held_cue_indices or (state.cue_index,)
    if displayed == (state.latest_started_cue_index,):
        return track.lyrics, state.cue_index  # Most frames have one ordinary active interval.
    active = set(displayed)
    last = state.latest_started_cue_index
    previous = [(index,) for index in range(last + 1) if index not in active]
    groups = previous + [displayed] + [(index,) for index in range(last + 1, len(track.lyrics))]
    return _LyricDisplayGroups(track.lyrics, groups), len(previous)


def resolve_lyrics_word_reveal(
    track: PlaylistTrack, source: Source, elapsed: float,
    cue_state: LyricsCueState, intro_state: LyricsIntroState | None,
) -> tuple[float, ...]:
    """Use the same word clock and interlude suppression in preview and export."""
    if (not source.subtitle_karaoke_enabled or cue_state.cue_index is None
            or (intro_state is not None and not intro_state.exiting)):
        return ()
    cue = track.lyrics[cue_state.cue_index]
    if cue.get("format") != "ttml":
        # Legacy LRC words and previous audio-analysis results cannot enable
        # word animation. Ordinary lyrics stay fully visible until the next cue.
        return tuple(float(len(line)) for line in LyricsService.decode_line_breaks(
            cue.get("text", "")).strip().splitlines() if line.strip())
    clock = max(0.0, elapsed + track.lyrics_timing_offset_seconds
                + source.subtitle_timing_offset + source.subtitle_karaoke_offset)
    return tuple(value for index in (cue_state.active_cue_indices or cue_state.held_cue_indices or (cue_state.cue_index,))
                 for value in LyricsService.word_reveal(track.lyrics[index], clock,
                     line_positions=tuple(source.subtitle_karaoke_lines)))


@dataclass(slots=True, frozen=True)
class NowPlayingExitState:
    """Whether a NOW_PLAYING card is visible at all, and how far its exit
    animation has progressed (raw, unEASED -- callers apply their own easing
    curve to exit_progress for the actual motion/opacity)."""

    visible: bool
    exit_progress: float | None


def resolve_now_playing_exit_state(
    elapsed_seconds: float,
    now_playing_duration: float,
    now_playing_exit_duration: float,
) -> NowPlayingExitState:
    """Resolve the NOW_PLAYING source's visibility/exit-progress state at
    elapsed_seconds. Pulled out of CanvasSnapshot.capture_track's inline
    now-playing branch verbatim; behavior is unchanged."""
    visible = elapsed_seconds <= now_playing_duration
    if not visible:
        return NowPlayingExitState(visible=False, exit_progress=None)
    exit_duration = min(now_playing_exit_duration, now_playing_duration)
    exit_start = now_playing_duration - exit_duration
    exit_progress: float | None = None
    if elapsed_seconds >= exit_start and exit_duration > 0.0:
        exit_progress = max(0.0, min(1.0, (elapsed_seconds - exit_start) / exit_duration))
    return NowPlayingExitState(visible=True, exit_progress=exit_progress)


@dataclass(slots=True, frozen=True)
class MixJunction:
    """Where a frame sits relative to crossfade/AutoMix handovers of the Canvas.

    ``visual_elapsed``: seconds since this track took over the Canvas in the
    middle of an overlap (``None``: it started plainly). ``visual_remaining``:
    seconds until it hands over in the middle of the next overlap (``None``:
    it ends plainly). ``mix_in_seconds``/``mix_out_seconds``: the overlap
    after/before those handovers, which "fit to the mix" animations last.
    See render_plan.visual_segments.
    """

    visual_elapsed: float | None = None
    visual_remaining: float | None = None
    mix_in_seconds: float = 0.0
    mix_out_seconds: float = 0.0


def segment_junction(segment, global_seconds: float) -> MixJunction | None:
    """The MixJunction of a render_plan.VisualSegment at ``global_seconds``."""
    if segment is None or not (segment.mixed_in or segment.mixed_out):
        return None
    return MixJunction(
        global_seconds - segment.start if segment.mixed_in else None,
        segment.end - global_seconds if segment.mixed_out else None,
        segment.mix_in_seconds, segment.mix_out_seconds,
    )


def mix_animation(
    source: Source, phase: str, junction: MixJunction | None = None,
) -> tuple[str, float]:
    """(style, seconds) of a source's entrance ("in") or exit ("out") at a mix handover.

    The same styles as at a plain track change; with ``animation_fit_mix`` they
    last as long as the audio mix on that side of the handover.
    """
    style, duration, span = (
        (source.animation_in, source.animation_in_duration,
         junction.mix_in_seconds if junction else 0.0)
        if phase == "in" else
        (source.animation_out, source.animation_out_duration,
         junction.mix_out_seconds if junction else 0.0)
    )
    return style, (span if source.animation_fit_mix and span > 0.0 else duration)


def mix_phase_durations(sources, junction: MixJunction | None = None) -> tuple[float, float]:
    """The longest mix entrance and exit among ``sources`` (0.0 where none animates)."""
    durations = []
    for phase in ("in", "out"):
        durations.append(max(
            (duration for style, duration in (
                mix_animation(source, phase, junction) for source in sources)
             if style != "none"),
            default=0.0,
        ))
    return durations[0], durations[1]


def resolve_mix_phase(
    sources, junction: MixJunction | None,
) -> tuple[str | None, float, float]:
    """(phase, progress, phase seconds) of the whole frame at a mix handover, like
    the plain track-edge phase: an entrance right after it, an exit right before."""
    if junction is None:
        return None, 1.0, 0.0
    intro, outro = mix_phase_durations(sources, junction)
    if junction.visual_elapsed is not None and junction.visual_elapsed < intro:
        return "in", junction.visual_elapsed / intro, intro
    if junction.visual_remaining is not None and junction.visual_remaining < outro:
        return "out", 1.0 - junction.visual_remaining / outro, outro
    return None, 1.0, 0.0


def resolve_edge_animation(
    source: Source, phase: str | None, *,
    elapsed_seconds: float, track_duration: float,
    phase_progress: float = 1.0, phase_duration: float | None = None,
    junction: MixJunction | None = None,
) -> tuple[str, str, float] | None:
    """(phase, style, raw progress) of a source's entrance/exit at a track edge, or None.

    The one formula for Canvas capture and export's frame cache keys. At a mix
    handover every source times its entrance/exit from the handover itself (see
    mix_animation); elsewhere the plain track-start/end animation applies.
    """
    if junction is not None:
        for edge, seconds in (("in", junction.visual_elapsed), ("out", junction.visual_remaining)):
            if seconds is None:
                continue
            style, duration = mix_animation(source, edge, junction)
            if style == "none" or duration <= 0.0 or seconds >= duration:
                continue
            progress = seconds / duration if edge == "in" else 1.0 - seconds / duration
            return edge, style, max(0.0, min(1.0, progress))
        if phase in {"in", "out"} and (
            (phase == "in" and junction.visual_elapsed is not None)
            or (phase == "out" and junction.visual_remaining is not None)
        ):
            return None  # that edge is a mix handover: the plain animation never plays there
    if phase is None:
        return None
    style = source.animation_in if phase == "in" else source.animation_out
    if style == "none":
        return None
    if phase_duration is None:
        # Isolated callers that only provide normalized progress.
        return phase, style, max(0.0, min(1.0, phase_progress))
    configured = source.animation_in_duration if phase == "in" else source.animation_out_duration
    duration = max(0.001, min(configured, phase_duration))
    if phase == "in":
        raw = elapsed_seconds / duration
    else:
        raw = (elapsed_seconds - max(0.0, track_duration - duration)) / duration
    return phase, style, max(0.0, min(1.0, raw))


def resolve_timeline_window_phase(
    source: Source, global_seconds: float,
) -> tuple[str | None, float]:
    """Return the in/out phase for a source at its own timeline-window edge.

    Sources restricted to a portion of the playlist previously appeared and
    vanished with a hard cut. When they carry an entrance or exit style,
    animate them across that style's duration on either side of the window.
    Pulled out of CanvasSnapshot._timeline_window_phase verbatim; behavior is
    unchanged. ExportCanvasCapturer._source_state_key now calls this too, so
    a source with both a timeline window and an animation style gets a cache
    key that reflects the window animation instead of silently coalescing
    visually different frames.
    """
    start = source.timeline_start
    duration = source.timeline_duration
    if start <= 0.0 and duration <= 0.0:
        return None, 1.0
    in_duration = (
        source.animation_in_duration if source.animation_in != "none" else 0.0
    )
    out_duration = (
        source.animation_out_duration if source.animation_out != "none" else 0.0
    )
    if in_duration > 0.0 and start <= global_seconds < start + in_duration:
        return "in", (global_seconds - start) / in_duration
    if duration > 0.0 and out_duration > 0.0:
        end = start + duration
        if end - out_duration <= global_seconds < end:
            return "out", (global_seconds - (end - out_duration)) / out_duration
    return None, 1.0
