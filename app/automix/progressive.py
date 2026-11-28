"""Progressive AutoMix for Preview: apply transitions as analysis completes.

Pure policy, no Qt, no FFmpeg -- the Qt shell lives in
``app.controllers.progressive_automix_controller``.

Why a partial plan is safe to play: ``compile_automix`` places tracks
strictly left to right, and a pair whose analysis is missing falls back to
plain sequential adjacency. Compiling the *full* track list with analyses for
only the analyzed prefix therefore yields "AutoMix up to the frontier,
sequential after it", and every transition it plans is already exactly the
one the final, fully analyzed plan will contain -- later analyses only
append, they never move an earlier transition. The one thing a new
transition changes is the previous clip's tail (its outgoing cue), which is
exactly where :func:`divergence_seconds` places the first difference.

Render policy (the O(n^2) guard): analysis and planning react to every
event, rendering does not. A partial mix is rendered only when the listener
is about to reach a junction whose AutoMix audio is not rendered yet, after a
short debounce so bursts of analysis events collapse into one render, and
never while another render is running. Once analysis is complete the partial
path stops and the unmodified export pipeline renders the final mix.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from itertools import zip_longest
from typing import NamedTuple

from app.automix.models import TrackAnalysis
from app.automix.planner import compile_automix
from app.automix.settings import AutoMixTransitionSettings
from app.automix.structure.models import TrackStructureAnalysis
from app.models.playlist import PlaylistTrack
from app.automix.renderer import BAND_ENVELOPES, transition_dsp_style
from app.renderer import loudness
from app.timeline.render_plan import AudioRenderPlan, CompiledRenderPlan, TransitionDsp

URGENT_HORIZON_SECONDS = 60.0
"""Render as soon as an unrendered transition starts within this much of the playhead."""
RENDER_DEBOUNCE_SECONDS = 0.5
"""Wait this long after the latest analysis event before rendering, to coalesce bursts."""
RENDER_MAX_DELAY_SECONDS = 2.0
"""...but never delay an urgent render longer than this after the first pending event."""
SWAP_MARGIN_SECONDS = 2.0
"""While playing, a swap must land this far before any change point or transition."""
PREVIEW_LOUDNESS_TARGET_LUFS = loudness.TARGET_LUFS
PREVIEW_PEAK_CEILING_DBFS = loudness.TRUE_PEAK_DBTP
"""Same targets as the export's normalization, so partial -> final swaps stay level."""


class ProgressiveAnalysis:
    """Which tracks have finished rhythm (and, when enabled, structure) analysis."""

    def __init__(self, tracks: Sequence[PlaylistTrack], *, structure_enabled: bool) -> None:
        self.track_ids = tuple(track.id for track in tracks)
        self.structure_enabled = structure_enabled
        self.rhythm: dict[str, TrackAnalysis] = {}
        self.structures: dict[str, TrackStructureAnalysis] = {}
        self._rhythm_done: set[str] = set()
        self._structure_done: set[str] = set()

    def record_rhythm(self, track_id: str, analysis: TrackAnalysis | None) -> None:
        self._rhythm_done.add(track_id)
        if analysis is not None:
            self.rhythm[track_id] = analysis

    def record_structure(self, track_id: str, structure: TrackStructureAnalysis | None) -> None:
        self._structure_done.add(track_id)
        if structure is not None:
            self.structures[track_id] = structure

    def _done(self, track_id: str) -> bool:
        return track_id in self._rhythm_done and (
            not self.structure_enabled or track_id in self._structure_done
        )

    def completed_count(self) -> int:
        return sum(1 for track_id in self.track_ids if self._done(track_id))

    def rhythm_count(self) -> int:
        return len(self._rhythm_done)

    def structure_count(self) -> int:
        return len(self._structure_done)

    def failed_count(self) -> int:
        """Tracks whose rhythm analysis finished without a result: they join with a plain transition."""
        return len(self._rhythm_done - self.rhythm.keys())

    def frontier(self) -> int:
        """How many leading tracks are fully analyzed (the prefix a plan can use)."""
        count = 0
        for track_id in self.track_ids:
            if not self._done(track_id):
                break
            count += 1
        return count

    def complete(self) -> bool:
        return self.frontier() == len(self.track_ids)


def partial_plan(
    tracks: Sequence[PlaylistTrack], state: ProgressiveAnalysis, settings: AutoMixTransitionSettings,
) -> CompiledRenderPlan:
    """AutoMix through the resolved prefix, sequential after it (see module docstring)."""
    frontier = state.frontier()
    prefix = state.track_ids[:frontier]
    return compile_automix(
        tracks,
        {track_id: state.rhythm[track_id] for track_id in prefix if track_id in state.rhythm},
        settings,
        structures={track_id: state.structures[track_id] for track_id in prefix if track_id in state.structures},
        log_diagnostics=False,
        pending_analysis=set(state.track_ids[frontier:]),
    )


def divergence_seconds(old: CompiledRenderPlan, new: CompiledRenderPlan) -> float:
    """First timeline point where the two plans' audio can differ; ``inf`` if identical.

    A clip that only changed its ``source_out`` (the outgoing clip of a newly
    planned transition) is identical up to where either version ends.
    Gain is ignored: partial mixes and the final mix share one level policy.
    """
    # Compared by what is heard, not by clip_id: the sequential compiler and
    # the AutoMix planner name clips differently for the very same placement.
    def placement(clip):
        return clip.track_id, clip.timeline_start, clip.source_in, clip.playback_rate

    def tail_start(clip):
        """Source second where the clip's tail (tempo ramp, then its end) begins."""
        return clip.tempo_ramp.source_start if clip.tempo_ramp is not None else clip.source_out

    points: list[float] = []
    for before, after in zip_longest(old.audio.clips, new.audio.clips):
        if before is not None and after is not None and placement(before) == placement(after) \
                and (before.source_out, before.tempo_ramp) == (after.source_out, after.tempo_ramp):
            continue
        if before is None or after is None:
            points.append((before or after).timeline_start)
        elif placement(before) != placement(after):
            points.append(min(before.timeline_start, after.timeline_start))
        else:
            # Same head: identical until the earlier of the two tails starts.
            points.append(before.timeline_at(min(tail_start(before), tail_start(after))))
        break

    def windows(plan):
        return {(t.timeline_start, t.duration, t.type, t.dsp, t.vocal_handoff, t.band_windows) for t in plan.audio.transitions}

    points.extend(start for start, *_rest in windows(old) ^ windows(new))
    return min(points, default=math.inf)


def swap_playhead(
    committed: CompiledRenderPlan, candidate: CompiledRenderPlan, playhead: float, playing: bool,
) -> float | None:
    """Where Preview's playhead lands if it replaces ``committed`` with ``candidate`` now; None = not now.

    Before the point where the two plans differ the playhead stays put.
    At/after it (the listener outran analysis) the same global second is
    different music, so the playhead moves to the same track and source
    second in ``candidate`` instead: the music continues, only the timeline
    shifts. While playing, never inside -- or about to enter -- a transition
    in either plan: an overlap already sounding keeps its current audio and
    the new mix takes over at a solo stretch.
    """
    margin = SWAP_MARGIN_SECONDS if playing else 0.0
    target: float | None = playhead
    if playhead + margin >= divergence_seconds(committed, candidate):
        target = None
        clip = next((c for c in committed.audio.clips if c.timeline_start <= playhead < c.timeline_end), None)
        if clip is not None:
            source = clip.source_at(playhead)
            match = next((c for c in candidate.audio.clips
                          if c.track_id == clip.track_id and c.source_in <= source < c.source_out), None)
            if match is not None:
                target = match.timeline_at(source)
    if target is None:
        return None
    if playing:
        for plan, at in ((committed, playhead), (candidate, target)):
            for transition in plan.audio.transitions:
                if transition.timeline_start - margin <= at < transition.timeline_start + transition.duration:
                    return None
    return target


def render_prefix(plan: CompiledRenderPlan, track_count: int, gain: float) -> tuple[AudioRenderPlan, float]:
    """The partial mix to render: the first ``track_count`` clips, at ``gain``.

    Returns the render plan and the timeline second its audio covers up to;
    Preview plays per-track audio after that point.
    """
    clips = plan.audio.clips[:track_count]
    clip_ids = {clip.clip_id for clip in clips}
    render = AudioRenderPlan(
        clips=tuple(replace(clip, gain=clip.gain * gain) for clip in clips),
        transitions=tuple(t for t in plan.audio.transitions if t.clip_a in clip_ids and t.clip_b in clip_ids),
    )
    return render, max((clip.timeline_end for clip in clips), default=0.0)


AUDITION_PREROLL_SECONDS = 4.0
AUDITION_POSTROLL_SECONDS = 3.0


class AuditionWindow(NamedTuple):
    """One junction rendered on its own (see ``render_window``)."""

    plan: AudioRenderPlan
    origin: float
    """Timeline second of the rendered file's first sample."""
    start: float
    end: float
    """The timeline seconds worth hearing."""
    resting_dsp: dict[str, tuple[bool, bool]]
    """``build_filter_graph``'s filters the neighbouring junctions run over these clips."""


def render_window(
    plan: CompiledRenderPlan, index: int, gain: float,
    preroll: float = AUDITION_PREROLL_SECONDS, postroll: float = AUDITION_POSTROLL_SECONDS,
) -> AuditionWindow:
    """Junction ``index`` (0-based: clips ``index`` and ``index + 1``) alone, with a run-up and a tail.

    Second ``t`` of the rendered file plays timeline second ``origin + t``,
    and ``start..end`` is the part worth hearing. Only the outgoing clip's
    head and the incoming clip's tail are cut, and the window never reaches
    into the neighbouring transitions: it starts no later than the outgoing
    tempo ramp and stops before the incoming one's, so every sample is placed
    by the same clips, ramp steps and transition as in the full mix. An
    outgoing clip with a tempo ramp keeps its whole head (``origin`` is then
    its start): the stretcher's state depends on where it began, and cutting
    it there put the ramp ~0.15 ms off the full mix. The band split or sweep
    highpass a neighbouring junction runs over either clip comes along at
    rest (``resting_dsp``): without it the phase differed from the full mix.
    """
    clips = plan.audio.clips
    outgoing, incoming = clips[index], clips[index + 1]
    by_pair = {(t.clip_a, t.clip_b): t for t in plan.audio.transitions}
    transition = by_pair.get((outgoing.clip_id, incoming.clip_id))
    before = by_pair.get((clips[index - 1].clip_id, outgoing.clip_id)) if index > 0 else None
    after = by_pair.get((incoming.clip_id, clips[index + 2].clip_id)) if index + 2 < len(clips) else None
    mix_start = transition.timeline_start if transition is not None else incoming.timeline_start
    mix_end = mix_start + transition.duration if transition is not None else incoming.timeline_start

    start = mix_start - preroll
    if outgoing.tempo_ramp is not None:
        start = min(start, outgoing.timeline_at(outgoing.tempo_ramp.source_start))
    start = max(start, before.timeline_start + before.duration if before is not None else outgoing.timeline_start)
    end = min(mix_end + postroll, incoming.timeline_end)
    if after is not None:
        end = min(end, after.timeline_start)
    if incoming.tempo_ramp is not None:
        end = min(end, incoming.timeline_at(incoming.tempo_ramp.source_start))
    end = max(end, mix_end, incoming.timeline_start)

    origin = outgoing.timeline_start if outgoing.tempo_ramp is not None else start
    head = replace(outgoing, timeline_start=0.0, source_in=outgoing.source_at(origin), gain=outgoing.gain * gain)
    tail = replace(incoming, timeline_start=incoming.timeline_start - origin, source_out=incoming.source_at(end),
                   tempo_ramp=None, gain=incoming.gain * gain)
    transitions = () if transition is None else (replace(transition, timeline_start=mix_start - origin),)

    def at_rest(neighbour) -> tuple[bool, bool]:
        style = transition_dsp_style(neighbour) if neighbour is not None else None
        return style in BAND_ENVELOPES, style is TransitionDsp.FILTER_SWEEP

    resting = {clip.clip_id: at_rest(neighbour) for clip, neighbour in ((head, before), (tail, after))}
    return AuditionWindow(AudioRenderPlan(clips=(head, tail), transitions=transitions), origin, start, end,
                          {clip_id: dsp for clip_id, dsp in resting.items() if any(dsp)})


def preview_gain(measurements: Mapping[str, tuple[float, float]], durations: Mapping[str, float]) -> float:
    """Linear gain approximating the export loudnorm pass for a partial mix.

    ``measurements`` maps track id -> (integrated LUFS, sample peak dBFS).
    Integrated loudness is combined energy-weighted by duration, then capped
    so the loudest peak stays under the export ceiling -- what linear-mode
    loudnorm would apply to the whole mix. 1.0 when nothing is measurable.
    """
    energy = total = 0.0
    peak = -math.inf
    for track_id, (lufs, peak_dbfs) in measurements.items():
        if not math.isfinite(lufs):
            continue
        seconds = max(durations.get(track_id, 0.0), 1e-3)
        energy += seconds * 10 ** (lufs / 10)
        total += seconds
        peak = max(peak, peak_dbfs)
    if total <= 0.0 or energy <= 0.0:
        return 1.0
    gain_db = PREVIEW_LOUDNESS_TARGET_LUFS - 10 * math.log10(energy / total)
    if math.isfinite(peak):
        gain_db = min(gain_db, PREVIEW_PEAK_CEILING_DBFS - peak)
    return 10 ** (gain_db / 20)


class Action(str, Enum):
    NONE = "none"
    RENDER = "render"
    FINAL = "final"


@dataclass
class RenderScheduler:
    """Decides when to render a partial mix, when to finalize, and when to wait.

    Fed by the controller: plan updates (every analysis event), playhead
    reports from Preview, and render start/finish. ``decide(now)`` is the
    only output; call it again after ``wake_after(now)`` seconds.
    """

    attached: bool = False
    playhead: float = 0.0
    playing: bool = False
    latest: CompiledRenderPlan | None = None
    latest_frontier: int = 0
    rendered: CompiledRenderPlan | None = None
    rendered_frontier: int = 0
    rendering: bool = False
    analysis_complete: bool = False
    final_started: bool = False
    _first_pending: float | None = field(default=None, repr=False)
    _last_update: float = field(default=0.0, repr=False)

    def plan_updated(self, plan: CompiledRenderPlan, frontier: int, now: float) -> None:
        self.latest, self.latest_frontier = plan, frontier
        if self._first_pending is None:
            self._first_pending = now
        self._last_update = now

    def playhead_changed(self, seconds: float, playing: bool) -> None:
        self.attached = True
        self.playhead, self.playing = seconds, playing

    def render_started(self, *, final: bool = False) -> None:
        """A render of ``latest`` (or, with ``final``, the export pipeline) began."""
        self.rendering = True
        self._first_pending = None
        if final:
            self.final_started = True
        else:
            self.rendered, self.rendered_frontier = self.latest, self.latest_frontier

    def render_finished(self) -> None:
        self.rendering = False

    def _unrendered_change(self) -> float:
        """Start of the first planned-but-unrendered transition still ahead of the listener (inf if none).

        Transitions only ever append (module docstring), so the unrendered ones
        are the tail past what ``rendered`` holds. One the listener already
        passed is skipped, not a reason to stop: a later one can still be
        swapped in via :func:`swap_playhead`. The new plan's timeline never
        runs later than the committed one, so "ahead" here is conservative.
        """
        if self.latest is None or self.latest_frontier <= self.rendered_frontier:
            return math.inf
        done = len(self.rendered.audio.transitions) if self.rendered is not None else 0
        return min((t.timeline_start for t in self.latest.audio.transitions[done:]
                    if t.timeline_start > self.playhead), default=math.inf)

    def _urgent(self) -> bool:
        change = self._unrendered_change()
        if not math.isfinite(change):
            return False
        if change - self.playhead <= URGENT_HORIZON_SECONDS:
            return True
        # The listener's very next junction is the unrendered one.
        rendered = self.rendered.audio.transitions if self.rendered is not None else ()
        return not any(self.playhead < t.timeline_start + t.duration and t.timeline_start < change for t in rendered)

    def decide(self, now: float) -> Action:
        if self.rendering or self.final_started:
            return Action.NONE
        if self.analysis_complete:
            return Action.FINAL
        if not self.attached or not self._urgent():
            return Action.NONE
        settled = now - self._last_update >= RENDER_DEBOUNCE_SECONDS
        overdue = self._first_pending is not None and now - self._first_pending >= RENDER_MAX_DELAY_SECONDS
        return Action.RENDER if settled or overdue else Action.NONE

    def wake_after(self, now: float) -> float | None:
        """Seconds until ``decide`` could change on its own (debounce expiry), else None."""
        if self.rendering or self.final_started or self.analysis_complete or not self.attached:
            return None
        if not self._urgent():
            return None
        waits = [RENDER_DEBOUNCE_SECONDS - (now - self._last_update)]
        if self._first_pending is not None:
            waits.append(RENDER_MAX_DELAY_SECONDS - (now - self._first_pending))
        return max(0.0, min(waits))
