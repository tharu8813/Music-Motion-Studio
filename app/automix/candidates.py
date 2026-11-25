"""Transition candidate generation and explainable scoring.

Pure data/functions only (roadmap Phase 3 section 12): no Qt, no FFmpeg
process, no project mutation, and no CompiledRenderPlan yet -- Phase 4
consumes this module's output to build one. Given identical
TrackAnalysis/settings input, output order and scores are always the
same: bar lengths are tried in one fixed order and nothing here uses
randomness (roadmap section 10, "Determinism").

Scoring weights, for a later developer tuning them. The base five (roadmap
Phase 3) sum to 1.0; the Phase 7 advanced three are additive bonuses/
penalties on top, applied only when both tracks actually have that data --
with none of it, the score is identical to Phase 3's (roadmap Phase 7
section 8 / section 13's "same basic result when advanced data
unavailable"):

    Factor                                          Weight   Effect
    ---------------------------------------------------------------
    Average BPM confidence                          0.35     +
    Downbeat/meter confidence (BEAT_MATCH only)      0.15     +
    Measured playback-rate shift vs. allowed budget  0.25     -
    Requested bar length actually used                0.15     +
    Cue proximity to the ideal anchor (beat/downbeat) 0.10     +
    Compatible key (Camelot wheel), if both known     0.06     + (never a blocker; a clash only shortens the overlap, below)
    Similar energy level, if both known               0.04     +
    Neither side sings in the overlap (outro over     0.12     + (only where both are *measured*;
      intro)                                                   two voices are rejected, not scored)

Commit C adds five more, same discipline -- additive, and each independently
a no-op without the relevant structure/trim data, never changing a result
that had none of it:

    Structure anchor alignment (outgoing or incoming, each) 0.06  + (bonus/hint only, see generate_candidates)
    Local energy continuity at the actual cue points        0.05  + (more precise than the global scalar above)
    Incoming trim beyond INCOMING_TRIM_SOFT_LIMIT_SECONDS   0.08  -
    Outgoing tail beyond MAXIMUM_OUTGOING_TAIL_TRIM_SECONDS 0.05  -

The pair-level factors above (confidence, key, global energy) are the same
for every candidate of a pair. The measured tempo cost and the following
candidate-level factors compare the actual overlap windows, each a no-op
without its data:

    Outgoing cue on an 8-bar phrase start (measured bar grid)  0.06  +
    Keys clash (and no re-pitch fixes it): per overlap length  0.30  - x severity x key confidence
    Local energy jump >= ENERGY_FLOW_THRESHOLD: per length     0.10  + on a rise (build-up), - on a drop
    Combined measured beat-grid error inside the overlap       0.20  - (reject above a quarter beat)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.automix.analysis.key import camelot_compatible, harmonic_shift, key_clash_severity
from app.automix.beatgrid import fit_beat_grid
from app.automix.compatibility import TransitionCompatibility
from app.automix.models import RELIABLE_BPM_CONFIDENCE, TrackAnalysis
from app.automix.music import character, context_score, music_tags_at
from app.automix.phrases import PHRASE_BARS, phrase_starts
from app.automix.settings import AutoMixTransitionSettings
from app.automix.structure.models import TrackStructureAnalysis

BAR_LENGTHS = (4, 8, 16)
DEFAULT_METER_NUMERATOR = 4

WEIGHT_CONFIDENCE = 0.35
WEIGHT_METER = 0.15
WEIGHT_TEMPO = 0.25
WEIGHT_LENGTH = 0.15
WEIGHT_PROXIMITY = 0.10

WEIGHT_HARMONIC_BONUS = 0.06
WEIGHT_ENERGY_CONTINUITY = 0.04

MIN_AFTER_VOCAL_SECONDS = 1.0
"""Shortest overlap allowed between the outgoing singer's last word and the
track's end: many songs sing almost to the end, and a short blend there still
beats a hard cut. Other overlaps keep ``settings.min_transition_seconds``."""

VOCAL_EDGE_TOLERANCE_SECONDS = 0.1
"""Vocal spans are measured in 100 ms frames (app/automix/analysis/vocals.py):
a downbeat within one frame of the last word counts as "right after" it."""

WEIGHT_INSTRUMENTAL_OVERLAP = 0.12
"""Bonus when both sides are measured not to sing anywhere in the overlap: the
outgoing outro under the incoming intro, the most natural blend there is. It
favours the bar length that ends before the incoming singer instead of one that
reaches into the first line."""

HANDOFF_BARS = (2, 1)
HANDOFF_MAX_SECONDS = 4.0
"""When both tracks sing across the junction -- the outgoing one to its last
sound, the incoming one from its first -- the longest of HANDOFF_BARS within
this many seconds (else the shortest) blends them instead of joining them back
to back, so the two voices overlap only briefly. A third of real pop tracks
sing to their last sound, and most of those start singing right away too; two
bars of a 64-80 BPM ballad were 6-7.5 s of both singing."""

FIXED_HANDOFF_MAX_SECONDS = 2.0
"""Without shared beats, keep simultaneous voices to a brief handoff."""

LOCAL_GRID_SECONDS = 60.0
"""Beats this far into each side's mix region fit its local tempo: a live or
drifting track is matched on the tempo it actually has around the cue."""

MAX_PAIR_PHASE_ERROR_BEATS = 0.25
"""Combined rhythm error above a quarter beat cannot sustain a clean beat match."""
WEIGHT_RHYTHM_INSTABILITY = 0.20
"""Prefer the overlap that stays steady, even when a longer length was requested."""

# Commit C: structure-aware additions. Same discipline as the Phase 7 block
# above -- additive bonuses/penalties applied only when the relevant
# structure data actually exists, so a candidate's score is identical to
# the pre-Commit-C result whenever no TrackStructureAnalysis is available
# for either side (roadmap: "structure媛 ?놁쑝硫?湲곗〈 planner 寃곌낵 ?좎?").
WEIGHT_STRUCTURE_ANCHOR_BONUS = 0.06
"""Bonus when a candidate's cue lands near a structure anchor (outro/
section boundary for outgoing, intro_end/section boundary for incoming) --
a hint the anchor was actually musically meaningful for this candidate,
never a requirement (roadmap: "structure anchor瑜?臾댁“嫄?transition point濡?
?곗? 留먭퀬 candidate bonus/hint濡쒕쭔 ?ъ슜")."""
WEIGHT_LOCAL_ENERGY_CONTINUITY = 0.05
"""Local (structure energy_curve, time-resolved) energy continuity bonus,
additive on top of WEIGHT_ENERGY_CONTINUITY's existing global-scalar
comparison -- more precise when available, since it compares energy at the
actual cue points instead of each track's single overall energy figure."""
WEIGHT_INCOMING_TRIM_PENALTY = 0.08
WEIGHT_OUTGOING_TAIL_TRIM_PENALTY = 0.05

STRUCTURE_ANCHOR_PROXIMITY_TOLERANCE_SECONDS = 6.0
"""How close a candidate's cue must land to a structure anchor to count as
"anchored" for scoring -- generous enough to survive bar-length/downbeat-
snap rounding, tight enough not to credit an unrelated candidate."""
INCOMING_TRIM_SOFT_LIMIT_SECONDS = 30.0
"""Beyond this much of the incoming track skipped before the transition
even starts, penalize increasingly -- trimming away most of an intro is a
much more aggressive edit than the bar-length-based candidates ever
produce on their own; only a structure anchor (a long intro_end) can push
a candidate this far in, and it should not win purely on that anchor's
say-so."""
MAXIMUM_OUTGOING_TAIL_TRIM_SECONDS = 60.0
"""A structure anchor (outro_start/late section) suggesting a transition
start more than this many seconds before the track's own natural end is
treated with rising skepticism -- a plausible outro is usually tens of
seconds, not minutes; an anchor this far from the end more likely reflects
a structure-analysis error than a deliberately long instrumental outro."""

WEIGHT_PHRASE_ALIGNMENT = 0.06
"""A blend that starts on an 8-bar phrase boundary sounds intended; one on bar
3 of a phrase sounds like a mistake. Outweighs the preferred-length bonus
(0.15 x 0.3), so a phrase-aligned 6-bar blend beats a mid-phrase 8-bar one, but
not WEIGHT_INSTRUMENTAL_OVERLAP: keeping the incoming singer out comes first."""
WEIGHT_KEY_CLASH = 0.3
"""Per full-length (max_transition_seconds) overlap of two clashing keys: the
shorter the harmonies share the room, the less the clash is heard. A sure, far
clash (severity 1.0) outweighs phrase alignment and prefers 4 bars; a near miss
(0.5) still lets a phrase-aligned blend win."""
KEY_SHIFT_MIN_CONFIDENCE = 0.7
"""Both key estimates must be at least this sure before a tail is re-pitched:
a shift chosen from a wrong key makes the clash worse, not better."""
WEIGHT_ENERGY_FLOW = 0.10
ENERGY_FLOW_THRESHOLD = 0.3
"""A local energy jump this large (transition_style.ENERGY_JUMP_THRESHOLD) sets
the blend's length: a rise wants a long build-up into the bigger track, a drop a
short blend so the loud tail does not bury the quiet intro."""

_DURATION_EPSILON_SECONDS = 1e-6
"""Floating-point tolerance for the exact-duration invariant (Commit C.1):
a candidate whose source-space window would overrun the track by more than
this is rejected outright, never silently clamped -- clamping would quietly
shrink the transition below the ``duration_seconds`` it was scored for,
recreating the same "scored 16s, rendered something else" bug in miniature."""


class TransitionStrategy(str, Enum):
    """A planning-time classification, not a DSP instruction.

    Distinct from ``app.timeline.models.TransitionType``: that enum
    describes what an ``AudioRenderTransition`` should eventually render;
    this one describes which family of candidate a planner considered and
    why, before any render-facing type is chosen (Phase 4's concern).
    """

    BEAT_MATCH = "beat_match"
    BEAT_ALIGNED_CROSSFADE = "beat_aligned_crossfade"
    FIXED_CROSSFADE = "fixed_crossfade"
    CUT = "cut"
    PHRASE_EXIT = "phrase_exit"
    """No shared tempo: leave the outgoing track on a phrase boundary (app/automix/exits.py)."""


@dataclass(frozen=True, slots=True)
class TransitionCandidate:
    """One possible transition point between two tracks, with its own score.

    Execution geometry (Commit C.1): ``duration_seconds`` is the
    authoritative *timeline* length of the transition -- the planner
    applies it directly as ``AudioRenderTransition.duration`` and trims the
    outgoing clip's ``source_out`` to ``outgoing_source_out``, rather than
    deriving the actual overlap from clip timeline arithmetic (which could
    silently diverge from what was scored whenever a structure anchor sat
    well before the track's natural end -- see planner.py's module
    docstring). ``outgoing_source_out`` and ``incoming_source_time`` are
    both in *source* (original media) seconds, same as
    ``outgoing_source_time``.

    Rates: ``incoming_rate`` is always 1.0 -- the next track plays at its
    own tempo from its first beat to its end. ``outgoing_rate`` is the rate
    the outgoing track holds *inside* the overlap (BEAT_MATCH: its beat
    period over the incoming one, so the beats coincide; 1.0 otherwise), so
    ``outgoing_source_out - outgoing_source_time == duration_seconds *
    outgoing_rate``. The planner eases the outgoing clip onto that rate
    before the overlap with a TempoRamp.
    """

    from_track_id: str
    to_track_id: str

    outgoing_source_time: float
    outgoing_source_out: float
    incoming_source_time: float

    bars: int
    duration_seconds: float

    outgoing_bpm: float | None
    incoming_bpm: float | None
    target_bpm: float | None

    outgoing_rate: float
    incoming_rate: float

    score: float
    confidence: float

    strategy: TransitionStrategy
    reasons: tuple[str, ...]
    refined_duration: bool = False


def select_best_candidate(candidates: list[TransitionCandidate]) -> TransitionCandidate | None:
    """Highest-scoring candidate, first one seen on a tie (stable, no randomness)."""
    if not candidates:
        return None
    best = candidates[0]
    for candidate in candidates[1:]:
        if candidate.score > best.score:
            best = candidate
    return best


def generate_candidates(
    outgoing: TrackAnalysis, incoming: TrackAnalysis,
    compatibility: TransitionCompatibility, settings: AutoMixTransitionSettings,
    *,
    outgoing_playback_rate: float = 1.0,
    outgoing_structure: TrackStructureAnalysis | None = None,
    incoming_structure: TrackStructureAnalysis | None = None,
) -> list[TransitionCandidate]:
    """Generate every transition candidate worth considering for this pair.

    Degrades through, from best to worst (roadmap section 9):
    BEAT_MATCH (both tracks have reliable BPM and actual beat anchors) ->
    BEAT_ALIGNED_CROSSFADE (BPM usable but beat/bar confidence is not) ->
    FIXED_CROSSFADE (incompatible tempo or missing BPM, but both tracks
    have enough source duration for a plain timed crossfade) -> CUT (not
    enough room for even a fixed crossfade).

    ``outgoing_playback_rate`` is the rate the *outgoing* clip is already
    playing at (fixed by whatever transition placed it, 1.0 for a clip
    that was never rate-shifted) -- required to convert this candidate's
    authoritative *timeline* ``duration_seconds`` into how much *source*
    audio the outgoing side actually needs (Commit C.1; see
    ``TransitionCandidate``'s docstring). Defaults to 1.0 for callers
    (mostly tests) that plan a single pair in isolation.

    ``outgoing_structure``/``incoming_structure`` are optional and
    independent of each other (one-sided structure data is used safely --
    neither side requires the other): when given, they add extra
    structure-anchored candidates (outro/section-boundary-based for
    outgoing, intro_end/section-boundary-based for incoming) on top of the
    regular bar-length candidates, and contribute scoring bonuses/
    penalties (see _score_beat_candidate) -- never a replacement for them,
    and never a forced transition point. With neither given, the result is
    byte-for-byte identical to calling this without the keyword arguments
    at all (roadmap: "structure媛 ?놁쑝硫?湲곗〈 planner 寃곌낵 ?좎?").
    """
    if outgoing.bpm is None or incoming.bpm is None:
        return _fallback_candidates(
            outgoing, incoming, settings, outgoing_playback_rate=outgoing_playback_rate,
            reasons=("- BPM is unknown for one or both tracks",),
        )
    bridge = compatibility.bridgeable and all(
        a.bpm_confidence >= RELIABLE_BPM_CONFIDENCE and len(a.beats) >= 4 and a.beat_alignment_quality() == "reliable"
        for a in (outgoing, incoming))
    if not compatibility.compatible and not bridge:
        return _fallback_candidates(
            outgoing, incoming, settings, outgoing_playback_rate=outgoing_playback_rate,
            reasons=compatibility.reasons,
        )
    if bridge:
        # Two reliable grids a little too far apart: match them anyway, over a longer ramp.
        settings = replace(settings, max_tempo_change_percent=settings.max_bridge_tempo_percent)
        compatibility = replace(compatibility, reasons=compatibility.reasons + (
            f"+ tempo bridge: the outgoing track eases {compatibility.tempo_shift_percent:.1f}% over a long ramp",))

    outgoing_quality = outgoing.beat_alignment_quality()
    incoming_quality = incoming.beat_alignment_quality()
    if outgoing_quality == "insufficient" or incoming_quality == "insufficient":
        return _fallback_candidates(
            outgoing, incoming, settings, outgoing_playback_rate=outgoing_playback_rate,
            reasons=("- beat confidence is too low to align a transition",),
        )

    # A provisional bar grid is not a reason to leave two reliable beat
    # tempos drifting apart. Match beats; only trust downbeats when measured.
    if all(a.bpm_confidence >= RELIABLE_BPM_CONFIDENCE and len(a.beats) >= 4
           for a in (outgoing, incoming)):
        strategy = TransitionStrategy.BEAT_MATCH
    else:
        strategy = TransitionStrategy.BEAT_ALIGNED_CROSSFADE

    # A short, clean handoff must compete even when a long blend fits. Rich
    # audio measurements decide whether the extra lengths actually sound better.
    bar_lengths = (1, 2, 3, 4, 6, 8, 12, 16) if outgoing.rms_curve and incoming.rms_curve else BAR_LENGTHS
    if not all(_has_reliable_downbeats(a) for a in (outgoing, incoming)):
        # The bar phase is a guess (the light analyzer's normal case), so a
        # blend may start mid-phrase: halve the preset's length (never below
        # the shortest) to keep any misplaced phrase start brief.
        shortest = min(bar_lengths)
        cap = max(shortest, settings.preferred_bars // 2)
        settings = replace(settings, preferred_bars=cap)
        bar_lengths = tuple(bars for bars in bar_lengths if bars <= cap)

    candidates = [
        candidate
        for bars in bar_lengths
        if (candidate := _beat_based_candidate(
            outgoing, incoming, compatibility, bars, strategy, settings,
            outgoing_playback_rate=outgoing_playback_rate,
            outgoing_structure=outgoing_structure, incoming_structure=incoming_structure,
        )) is not None
    ]

    outgoing_anchor = _structure_outgoing_anchor(outgoing_structure)
    incoming_anchor = _structure_incoming_anchor(incoming_structure)
    if outgoing_anchor is not None or incoming_anchor is not None:
        # Additional candidates anchored at the structure hint(s) instead of
        # the default tail/head positions -- still snapped to the nearest
        # real beat/downbeat and scored like any other candidate, so a bad
        # anchor simply loses to a better-scoring regular candidate rather
        # than being trusted outright.
        candidates.extend(
            candidate
            for bars in bar_lengths
            if (candidate := _beat_based_candidate(
                outgoing, incoming, compatibility, bars, strategy, settings,
                outgoing_naive_override=outgoing_anchor, incoming_naive_override=incoming_anchor,
                outgoing_playback_rate=outgoing_playback_rate,
                outgoing_structure=outgoing_structure, incoming_structure=incoming_structure,
            )) is not None
        )

    # Every phrase start near the end, at the bar count that reaches it: the
    # bar-length candidates only land a whole 4/8/16 bars before the end,
    # which is mid-phrase whenever the song does not end on a phrase boundary.
    # Phrases are 8 bars apart, so one always starts 4..12 bars before the end.
    outgoing_bar = (outgoing.meter_numerator or DEFAULT_METER_NUMERATOR) * 60.0 / outgoing.bpm
    longest = min(max(bar_lengths), settings.preferred_bars + PHRASE_BARS // 2)
    for start in phrase_starts(outgoing, outgoing_structure):
        distance = round((audible_end(outgoing) - start) / outgoing_bar)
        candidates.extend(
            candidate
            for bars in (distance, distance - 1)  # the cue lands within [bars, bars + 1] bars of the end
            if min(bar_lengths) <= bars <= longest
            and (candidate := _beat_based_candidate(
                outgoing, incoming, compatibility, bars, strategy, settings,
                outgoing_naive_override=start,
                outgoing_playback_rate=outgoing_playback_rate,
                outgoing_structure=outgoing_structure, incoming_structure=incoming_structure,
            )) is not None
        )

    if outgoing.rms_curve and incoming.rms_curve:
        # Land the END of the blend on a measured vocal/section/groove entry,
        # rather than always starting both songs at their first available beat.
        for base in tuple(candidates):
            for landing in incoming_landmarks(incoming, incoming_structure):
                cue = landing - base.duration_seconds
                if cue < audible_start(incoming) - VOCAL_EDGE_TOLERANCE_SECONDS or any(
                        a < cue and b > audible_start(incoming) for a, b in sung_spans(incoming)):
                    continue  # a trimmed instrumental pickup is OK; discarding words is not
                candidate = _beat_based_candidate(
                    outgoing, incoming, compatibility, base.bars, strategy, settings,
                    outgoing_naive_override=base.outgoing_source_time, incoming_naive_override=max(0.0, cue),
                    outgoing_playback_rate=outgoing_playback_rate,
                    outgoing_structure=outgoing_structure, incoming_structure=incoming_structure,
                )
                if candidate is not None:
                    candidates.append(candidate)
        # Even if a long instrumental blend exists, compare a brief voice
        # handoff. The selector will account for words obscured by its envelopes.
        if _sings_to_end(outgoing):
            candidates.extend(c for bars in HANDOFF_BARS if (c := _beat_based_candidate(
                outgoing, incoming, compatibility, bars, strategy, settings, handoff=True,
                outgoing_playback_rate=outgoing_playback_rate,
                outgoing_structure=outgoing_structure, incoming_structure=incoming_structure,
            )) is not None and c.duration_seconds <= HANDOFF_MAX_SECONDS + _DURATION_EPSILON_SECONDS)

    candidates = _deduplicate_candidates(candidates)

    if not candidates and _sings_to_end(outgoing):
        # Neither room after the last line nor an instrumental intro to mix it over.
        handoffs = [
            candidate
            for bars in HANDOFF_BARS
            if (candidate := _beat_based_candidate(
                outgoing, incoming, compatibility, bars, strategy, settings, handoff=True,
                outgoing_playback_rate=outgoing_playback_rate,
                outgoing_structure=outgoing_structure, incoming_structure=incoming_structure,
            )) is not None
        ]
        fitting = [c for c in handoffs if c.duration_seconds <= HANDOFF_MAX_SECONDS + _DURATION_EPSILON_SECONDS]
        candidates = fitting[:1] or sorted(handoffs, key=lambda c: c.duration_seconds)[:1]

    if not candidates:
        return _fallback_candidates(
            outgoing, incoming, settings, outgoing_playback_rate=outgoing_playback_rate,
            reasons=("- no bar length fit within the transition-length and track-duration limits",),
        )
    if outgoing.rms_curve and incoming.rms_curve:
        candidates.extend(_fallback_candidates(
            outgoing, incoming, settings, outgoing_playback_rate=outgoing_playback_rate,
            reasons=("+ natural-tempo handoff competes with beat matching",)))
    return candidates


def incoming_landmarks(analysis: TrackAnalysis, structure: TrackStructureAnalysis | None = None) -> tuple[float, ...]:
    """Early measured entries, not invented drop/chorus classifications."""
    start = audible_start(analysis)
    limit = min(audible_end(analysis), start + INCOMING_TRIM_SOFT_LIMIT_SECONDS)
    points = [a for a, b in sung_spans(analysis) if b > start][:1]
    if structure is not None:
        points.extend(s.start_seconds for s in structure.sections)
        if structure.intro_end_seconds is not None:
            points.append(structure.intro_end_seconds)
    # A sustained onset-strength rise marks drums arriving, even without an
    # optional structure model. Require three measured seconds on each side.
    drums = analysis.percussive_curve
    points.extend(float(i) for i in range(3, min(len(drums) - 2, int(limit) + 1))
                  if sum(drums[i:i + 3]) / 3 - sum(drums[i - 3:i]) / 3 >= 0.35)
    anchors = analysis.downbeats if _has_reliable_downbeats(analysis) else analysis.beats
    beat = 60.0 / analysis.bpm if analysis.bpm else 0.5
    snapped = []
    for point in sorted(set(points)):
        if not start + 1.0 <= point <= limit:
            continue
        anchor, distance = _nearest_anchor(anchors, point)
        # Keep an off-grid vocal pickup at its real time; never call a distant
        # downbeat its entry. Candidate snapping remains independently bounded.
        snapped.append(anchor if distance <= beat / 2 else point)
    return tuple(sorted(set(snapped)))[:8]


def _deduplicate_candidates(candidates: list[TransitionCandidate]) -> list[TransitionCandidate]:
    """Drop a structure-anchored candidate that snapped to the exact same
    geometry as a regular one (e.g. the structure anchor and the plain
    tail position land on the same downbeat) -- order-preserving (keeps
    the first occurrence) so this stays fully deterministic."""
    seen: set[tuple[float, float, TransitionStrategy]] = set()
    deduplicated: list[TransitionCandidate] = []
    for candidate in candidates:
        # Bars excluded: several bar lengths can land on the same cue after a vocal.
        key = (candidate.outgoing_source_time, candidate.incoming_source_time, candidate.strategy)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(candidate)
    return deduplicated


def _structure_outgoing_anchor(structure: TrackStructureAnalysis | None) -> float | None:
    """A candidate cue position near the end of the track, from structure
    data: outro_start if known, else the start of the last section, else
    None -- a hint only, always subject to downbeat/beat snapping and
    ordinary scoring like any other candidate (never a forced cut point)."""
    if structure is None:
        return None
    if structure.outro_start_seconds is not None:
        return structure.outro_start_seconds
    if structure.sections:
        return structure.sections[-1].start_seconds
    return None


def _structure_incoming_anchor(structure: TrackStructureAnalysis | None) -> float | None:
    """A candidate cue position near the start of the track, from structure
    data: intro_end if known, else the end of the first section, else
    None -- same "hint only" discipline as _structure_outgoing_anchor."""
    if structure is None:
        return None
    if structure.intro_end_seconds is not None:
        return structure.intro_end_seconds
    if structure.sections:
        return structure.sections[0].end_seconds
    return None


def _beat_based_candidate(
    outgoing: TrackAnalysis, incoming: TrackAnalysis, compatibility: TransitionCompatibility,
    bars: int, strategy: TransitionStrategy, settings: AutoMixTransitionSettings,
    *,
    outgoing_naive_override: float | None = None,
    incoming_naive_override: float | None = None,
    outgoing_playback_rate: float = 1.0,
    outgoing_structure: TrackStructureAnalysis | None = None,
    incoming_structure: TrackStructureAnalysis | None = None,
    handoff: bool = False,
) -> TransitionCandidate | None:
    """``handoff``: both tracks sing across the junction; blend over them anyway (HANDOFF_BARS)."""
    assert outgoing.bpm is not None and incoming.bpm is not None and compatibility.incoming_effective_bpm is not None

    # The incoming track always plays at its own tempo. For BEAT_MATCH the
    # outgoing one is the one that moves: the planner ramps it onto the
    # incoming beat before the overlap (TempoRamp) and it holds
    # ``outgoing_rate`` through it. Periods come from grids fitted to each
    # side's beats around its own cue, not from the frame-quantized BPM.
    incoming_rate = 1.0
    outgoing_grid = incoming_grid = None
    if strategy is TransitionStrategy.BEAT_MATCH:
        outgoing_grid = fit_beat_grid(outgoing.beats, audible_end(outgoing) - LOCAL_GRID_SECONDS, audible_end(outgoing))
        incoming_grid_start = (incoming_naive_override if incoming_naive_override is not None
                               else audible_start(incoming))
        incoming_grid = fit_beat_grid(incoming.beats, incoming_grid_start, incoming_grid_start + LOCAL_GRID_SECONDS)
        if outgoing_grid is None or incoming_grid is None:
            return None
        outgoing_rate = _nearest_octave_rate(outgoing_grid.period / incoming_grid.period, settings)
        if abs(outgoing_rate - 1.0) * 100.0 > settings.max_tempo_change_percent + 1e-9:
            return None
        target_bpm = 60.0 / incoming_grid.period
        seconds_per_bar = (incoming.meter_numerator or DEFAULT_METER_NUMERATOR) * incoming_grid.period
    else:
        target_bpm = None
        outgoing_rate = 1.0
        seconds_per_bar = (outgoing.meter_numerator or DEFAULT_METER_NUMERATOR) * 60.0 / outgoing.bpm
    # Source seconds of the outgoing track per timeline second inside the overlap.
    overlap_rate = outgoing_rate if strategy is TransitionStrategy.BEAT_MATCH else outgoing_playback_rate

    # Start with the requested bar length; after snapping, include the
    # remaining audible tail before scoring the final timeline duration.
    duration_seconds = bars * seconds_per_bar
    if not (settings.min_transition_seconds <= duration_seconds <= settings.max_transition_seconds):
        return None

    outgoing_source_span = duration_seconds * overlap_rate
    incoming_source_span = duration_seconds * incoming_rate
    if (outgoing_source_span > outgoing.duration_seconds
            or incoming_source_span > incoming.duration_seconds):
        return None

    # The tail ends where the sound does, not where the file does: a window
    # over trailing digital silence would mix against nothing (Phase 02).
    outgoing_end = audible_end(outgoing)
    naive_outgoing_time = (
        outgoing_naive_override if outgoing_naive_override is not None
        else max(0.0, outgoing_end - outgoing_source_span)
    )
    naive_outgoing_time = max(0.0, min(naive_outgoing_time, outgoing.duration_seconds))
    outgoing_anchors = (outgoing.downbeats if strategy is TransitionStrategy.BEAT_MATCH
                        and _has_reliable_downbeats(outgoing) else outgoing.beats)
    # Bounded so the source window this candidate actually needs
    # (outgoing_source_span, fixed by duration_seconds) never overruns the
    # track -- nor its audible end, when there is room before it --
    # regardless of which side of the naive position the nearest anchor
    # happens to fall on -- see _nearest_bounded_anchor.
    outgoing_source_time, outgoing_snap = _nearest_bounded_anchor(
        outgoing_anchors, naive_outgoing_time, max(0.0, outgoing_end - outgoing_source_span),
    )
    # Preserve the audible ending, including an off-grid last syllable.
    # Finalize the candidate's duration here, BEFORE scoring/compilation;
    # C.1 still renders exactly the duration that was scored. Early structure
    # hints requiring an overlong overlap are rejected, never used to cut audio.
    # Allow at most one extra bar for snapping, not an arbitrary long fade.
    if strategy is TransitionStrategy.BEAT_MATCH and outgoing_source_time not in outgoing_anchors:
        return None
    # Two voices must not share the blend. While the outgoing track sings, mix
    # only under an instrumental incoming intro; otherwise a cue inside its last
    # phrase moves to the first beat after that phrase ends -- mixing starts
    # right when the singer stops, even if that leaves only a short tail.
    naive_incoming_time = (
        incoming_naive_override if incoming_naive_override is not None else audible_start(incoming)
    )
    naive_incoming_time = max(0.0, min(naive_incoming_time, incoming.duration_seconds))
    vocal_end = _last_vocal_end(outgoing, outgoing_source_time, outgoing_end)
    over_intro = vocal_end is not None and not handoff and _instrumental(
        incoming, naive_incoming_time, naive_incoming_time + (bars + 1) * seconds_per_bar)
    after_vocals = vocal_end is not None and not over_intro and not handoff
    if after_vocals:
        earliest = vocal_end - VOCAL_EDGE_TOLERANCE_SECONDS
        later = ([anchor for anchor in outgoing_anchors if earliest <= anchor < outgoing_end]
                 or [beat for beat in outgoing.beats if earliest <= beat < outgoing_end])  # no downbeat left: a beat
        if not later:
            return None  # sung to the very end: nothing to mix over
        outgoing_source_time = min(later)
    if strategy is TransitionStrategy.BEAT_MATCH:
        # Detected beats sit on a ~20 ms frame grid; the fitted grid does not.
        outgoing_source_time = min(outgoing_grid.snap(outgoing_source_time), outgoing_end)
    outgoing_source_out = outgoing_end
    outgoing_source_span = outgoing_end - outgoing_source_time
    duration_seconds = outgoing_source_span / overlap_rate
    incoming_source_span = duration_seconds * incoming_rate
    shortest = MIN_AFTER_VOCAL_SECONDS if after_vocals else settings.min_transition_seconds
    if (not shortest <= duration_seconds <= settings.max_transition_seconds
            or duration_seconds > (bars + 1) * seconds_per_bar + _DURATION_EPSILON_SECONDS
            or incoming_source_span > incoming.duration_seconds):
        return None

    incoming_anchors = (incoming.downbeats if strategy is TransitionStrategy.BEAT_MATCH
                        and _has_reliable_downbeats(incoming) else incoming.beats)
    incoming_source_time, incoming_snap = _nearest_bounded_anchor(
        incoming_anchors, naive_incoming_time, incoming.duration_seconds - incoming_source_span,
    )
    if strategy is TransitionStrategy.BEAT_MATCH:
        if incoming_source_time not in incoming_anchors:
            return None
        incoming_source_time = max(0.0, incoming_grid.snap(incoming_source_time))
        if incoming_source_time + incoming_source_span > incoming.duration_seconds + _DURATION_EPSILON_SECONDS:
            return None
    if over_intro and not _instrumental(incoming, incoming_source_time, incoming_source_time + incoming_source_span):
        return None  # the snapped window reaches the incoming singer
    if any(max(a, audible_start(incoming)) + VOCAL_EDGE_TOLERANCE_SECONDS < min(b, incoming_source_time)
           for a, b in sung_spans(incoming)):
        # Snapping to a beat must not drop a sung pickup or an entire earlier line.
        return None
    # Otherwise the incoming track may already sing here: the outgoing one no longer does.

    phase_error = None
    if strategy is TransitionStrategy.BEAT_MATCH:
        out_character = character(music_tags_at(outgoing, outgoing_source_time, outgoing_source_out))
        in_character = character(music_tags_at(incoming, incoming_source_time, incoming_source_time + incoming_source_span))
        gentle = max(out_character[0], in_character[0])
        driven = max(out_character[1], in_character[1])
        if gentle >= 0.55 and gentle > driven and abs(outgoing_rate - 1.0) * 100.0 > 3.0 + 1e-9:
            return None  # expressive music keeps its phrasing; use the natural fade instead of a large tempo move
        outgoing_error = outgoing_grid.phase_error(outgoing.beats, outgoing_source_time, outgoing_source_out)
        incoming_error = incoming_grid.phase_error(
            incoming.beats, incoming_source_time, incoming_source_time + incoming_source_span)
        if outgoing_error is not None and incoming_error is not None:
            phase_error = outgoing_error + incoming_error
            if phase_error > MAX_PAIR_PHASE_ERROR_BEATS:
                return None  # a shorter candidate or the existing safe fallback handles this pair

    confidence = min(outgoing.bpm_confidence, incoming.bpm_confidence)
    if phase_error is not None:
        confidence *= 1.0 - phase_error
    score, reasons = _score_beat_candidate(
        outgoing, incoming, compatibility, bars, strategy, settings, outgoing_snap, incoming_snap,
        outgoing_source_time, incoming_source_time, duration_seconds,
        outgoing_source_span=outgoing_source_span, incoming_source_span=incoming_source_span,
        outgoing_structure=outgoing_structure, incoming_structure=incoming_structure,
        outgoing_rate=outgoing_rate,
        phase_error=phase_error,
    )
    if strategy is TransitionStrategy.BEAT_MATCH:
        alignment = "downbeat" if all(_has_reliable_downbeats(a) for a in (outgoing, incoming)) else "beat (bar phase uncertain)"
        reasons += (f"+ outgoing eased to the incoming tempo ({(outgoing_rate - 1.0) * 100:+.1f}%), "
                    f"aligned at {alignment} cues",)
    if after_vocals:
        reasons += (f"+ mixing starts after the outgoing vocals end ({vocal_end:.1f}s)",)
    elif over_intro:
        reasons += ("+ blending under the outgoing vocals: the incoming intro is instrumental",)
    elif handoff:
        reasons += (f"- both tracks sing across the junction: a short {bars}-bar blend hands the voice over",)
    elif outgoing.vocal_activity:
        reasons += ("+ outgoing track does not sing in the overlap",)
    reasons += ("+ audible outgoing ending preserved",)
    return TransitionCandidate(
        from_track_id=outgoing.track_id, to_track_id=incoming.track_id,
        outgoing_source_time=outgoing_source_time, outgoing_source_out=outgoing_source_out,
        incoming_source_time=incoming_source_time,
        bars=bars, duration_seconds=duration_seconds,
        outgoing_bpm=outgoing.bpm, incoming_bpm=incoming.bpm, target_bpm=target_bpm,
        outgoing_rate=outgoing_rate, incoming_rate=incoming_rate,
        score=score, confidence=confidence, strategy=strategy, reasons=reasons,
    )


def _has_reliable_downbeats(analysis: TrackAnalysis) -> bool:
    """Only a measured bar grid may supply downbeat anchors."""
    return analysis.beat_alignment_quality() == "reliable" and bool(analysis.downbeats)


def _nearest_anchor(anchors: tuple[float, ...], naive_time: float) -> tuple[float, float]:
    """The closest beat/downbeat to ``naive_time``, and how far it was (seconds)."""
    if not anchors:
        return naive_time, 0.0
    nearest = min(anchors, key=lambda anchor: abs(anchor - naive_time))
    return nearest, abs(nearest - naive_time)


def _nearest_bounded_anchor(
    anchors: tuple[float, ...], naive_time: float, upper_bound: float,
) -> tuple[float, float]:
    """Like ``_nearest_anchor``, but never returns a position past
    ``upper_bound`` (Commit C.1's exact-duration invariant: the nearest
    *unconstrained* anchor to a tail-based naive position lands after it
    about as often as before it -- a plain ``_nearest_anchor`` snap could
    then push ``outgoing_source_time``/``incoming_source_time`` far enough
    that ``+ source_span`` overruns the track, even for perfectly ordinary
    candidates, not just structure-anchored edge cases).

    Prefers the nearest anchor that still satisfies the bound; if none do
    (a very short track, or an override already past the bound), clamps
    directly to ``upper_bound`` instead of snapping to any anchor at all --
    still guarantees the caller's downstream ``... + source_span <=
    duration + epsilon`` check always holds, by construction.
    """
    eligible = tuple(anchor for anchor in anchors if anchor <= upper_bound + _DURATION_EPSILON_SECONDS)
    if eligible:
        nearest = min(eligible, key=lambda anchor: abs(anchor - naive_time))
        return nearest, abs(nearest - naive_time)
    clamped = max(0.0, min(naive_time, upper_bound))
    return clamped, abs(clamped - naive_time)


def _score_beat_candidate(
    outgoing: TrackAnalysis, incoming: TrackAnalysis, compatibility: TransitionCompatibility,
    bars: int, strategy: TransitionStrategy, settings: AutoMixTransitionSettings,
    outgoing_snap: float, incoming_snap: float,
    outgoing_source_time: float, incoming_source_time: float, duration_seconds: float,
    *,
    outgoing_source_span: float | None = None,
    incoming_source_span: float | None = None,
    outgoing_structure: TrackStructureAnalysis | None = None,
    incoming_structure: TrackStructureAnalysis | None = None,
    outgoing_rate: float = 1.0,
    phase_error: float | None = None,
) -> tuple[float, tuple[str, ...]]:
    reasons: list[str] = list(compatibility.reasons)
    score = 0.0

    confidence_component = (outgoing.bpm_confidence + incoming.bpm_confidence) / 2.0
    score += WEIGHT_CONFIDENCE * confidence_component
    reasons.append(
        f"{'+' if confidence_component >= 0.5 else '-'} average beat confidence {confidence_component:.2f}"
    )

    if strategy is TransitionStrategy.BEAT_MATCH:
        meter_component = (outgoing.meter_confidence + incoming.meter_confidence) / 2.0
        score += WEIGHT_METER * meter_component
        reasons.append(
            f"{'+' if meter_component >= 0.5 else '-'} downbeat confidence {meter_component:.2f}"
        )

    tempo_budget = max(0.01, settings.max_tempo_change_percent)
    tempo_shift = (abs(outgoing_rate - 1.0) * 100.0 if strategy is TransitionStrategy.BEAT_MATCH
                   else compatibility.tempo_shift_percent)
    if tempo_shift < 1e-7:
        tempo_shift = 0.0  # fitting an unchanged tempo must not disturb stable score ties
    tempo_component = max(0.0, 1.0 - tempo_shift / tempo_budget)
    score += WEIGHT_TEMPO * tempo_component

    if phase_error is not None:
        score -= WEIGHT_RHYTHM_INSTABILITY * phase_error / MAX_PAIR_PHASE_ERROR_BEATS
        reasons.append(f"{'+' if phase_error < 0.05 else '-'} measured overlap rhythm error {phase_error:.3f} beats")

    length_component = 1.0 if bars == settings.preferred_bars else 0.7
    score += WEIGHT_LENGTH * length_component
    reasons.append(f"+ {bars} bars available")

    snap_total = outgoing_snap + incoming_snap
    proximity_component = max(0.0, 1.0 - snap_total / 4.0)
    score += WEIGHT_PROXIMITY * proximity_component
    anchor_name = "downbeat" if strategy is TransitionStrategy.BEAT_MATCH and _has_reliable_downbeats(incoming) else "beat"
    if incoming_snap > 1.0:
        reasons.append(f"- incoming cue is {incoming_snap:.1f}s from the nearest {anchor_name}")
    else:
        reasons.append(f"+ incoming cue close to the nearest {anchor_name}")

    if outgoing.key is not None and incoming.key is not None:
        if camelot_compatible(outgoing.key, incoming.key):
            score += WEIGHT_HARMONIC_BONUS
            reasons.append("+ compatible key")
        # No penalty for an incompatible key: key is a bonus modifier only,
        # never a blocker (roadmap Phase 7 section 3).

    if outgoing.energy is not None and incoming.energy is not None:
        energy_similarity = max(0.0, 1.0 - abs(outgoing.energy - incoming.energy))
        score += WEIGHT_ENERGY_CONTINUITY * energy_similarity
        if energy_similarity >= 0.8:
            reasons.append("+ similar energy level")

    # Vocals inside the window are otherwise not scored: _beat_based_candidate
    # rejects two voices outright.
    if (outgoing_source_span is not None and incoming_source_span is not None
            and _instrumental(outgoing, outgoing_source_time, outgoing_source_time + outgoing_source_span)
            and _instrumental(incoming, incoming_source_time, incoming_source_time + incoming_source_span)):
        score += WEIGHT_INSTRUMENTAL_OVERLAP
        reasons.append("+ outro over intro: neither track sings in the overlap")

    # -- Commit C: structure-aware bonuses/penalties, additive on top of the
    # above and each independently no-op without the relevant data (roadmap:
    # "structure媛 ?놁쑝硫?湲곗〈 planner 寃곌낵 ?좎?", "one-sided structure??
    # ?덉쟾?섍쾶 ?ъ슜").
    outgoing_anchor = _structure_outgoing_anchor(outgoing_structure)
    if outgoing_anchor is not None and abs(outgoing_source_time - outgoing_anchor) <= STRUCTURE_ANCHOR_PROXIMITY_TOLERANCE_SECONDS:
        score += WEIGHT_STRUCTURE_ANCHOR_BONUS
        reasons.append("+ outgoing cue aligns with a structure anchor (outro/late section)")

    incoming_anchor = _structure_incoming_anchor(incoming_structure)
    if incoming_anchor is not None and abs(incoming_source_time - incoming_anchor) <= STRUCTURE_ANCHOR_PROXIMITY_TOLERANCE_SECONDS:
        score += WEIGHT_STRUCTURE_ANCHOR_BONUS
        reasons.append("+ incoming cue aligns with a structure anchor (intro end/early section)")

    if outgoing_structure is not None and incoming_structure is not None:
        outgoing_local_energy = outgoing_structure.energy_at(outgoing_source_time)
        incoming_local_energy = incoming_structure.energy_at(incoming_source_time)
        if outgoing_local_energy is not None and incoming_local_energy is not None:
            # More precise than the global-scalar energy comparison above:
            # this compares energy at the actual cue points, not each
            # track's single overall figure.
            local_energy_similarity = max(0.0, 1.0 - abs(outgoing_local_energy - incoming_local_energy))
            score += WEIGHT_LOCAL_ENERGY_CONTINUITY * local_energy_similarity
            if local_energy_similarity >= 0.8:
                reasons.append("+ similar local energy at the cue points")
            else:
                reasons.append("- local energy jumps across the transition")
            rise = incoming_local_energy - outgoing_local_energy
            if abs(rise) >= ENERGY_FLOW_THRESHOLD:
                reach = min(1.0, duration_seconds / settings.max_transition_seconds)
                score += WEIGHT_ENERGY_FLOW * (reach if rise > 0.0 else -reach)
                reasons.append(f"+ energy rises {rise:.2f}: a long build-up into the next track" if rise > 0.0
                               else f"- energy drops {-rise:.2f}: a short blend keeps the loud tail off the intro")

    if outgoing.bpm and any(abs(outgoing_source_time - start) <= 30.0 / outgoing.bpm  # half a beat
                            for start in phrase_starts(outgoing, outgoing_structure)):
        score += WEIGHT_PHRASE_ALIGNMENT
        reasons.append("+ blend starts on an 8-bar phrase boundary")

    clash = _heard_key_clash(outgoing, incoming, strategy, outgoing_rate)
    if clash:
        score -= WEIGHT_KEY_CLASH * clash * min(1.0, duration_seconds / settings.max_transition_seconds)
        reasons.append(f"- keys clash ({outgoing.key} -> {incoming.key}): a shorter overlap is preferred")

    if outgoing_source_span is not None and incoming_source_span is not None:
        music_score, music_reasons = context_score(
            outgoing, incoming, outgoing_source_time, outgoing_source_time + outgoing_source_span,
            incoming_source_time, incoming_source_time + incoming_source_span,
            duration_seconds, settings.max_transition_seconds)
        score += music_score
        reasons.extend(music_reasons)

    if incoming_source_time > INCOMING_TRIM_SOFT_LIMIT_SECONDS:
        # Deliberately uncapped (unlike the other components above): a
        # moderate overshoot should cost a little, but an extreme one (an
        # implausible structure anchor, not a real intro) must be able to
        # outweigh even a simultaneous structure-anchor bonus and drive the
        # final clamped score toward 0 -- see
        # test_falls_back_to_the_regular_bar_candidate_when_the_anchor_is_implausible.
        trim_severity = (incoming_source_time - INCOMING_TRIM_SOFT_LIMIT_SECONDS) / INCOMING_TRIM_SOFT_LIMIT_SECONDS
        score -= WEIGHT_INCOMING_TRIM_PENALTY * trim_severity
        reasons.append(f"- incoming cue trims {incoming_source_time:.1f}s off the start of the track")

    outgoing_tail_unused = audible_end(outgoing) - outgoing_source_time
    if outgoing_tail_unused > MAXIMUM_OUTGOING_TAIL_TRIM_SECONDS:
        trim_severity = (outgoing_tail_unused - MAXIMUM_OUTGOING_TAIL_TRIM_SECONDS) / MAXIMUM_OUTGOING_TAIL_TRIM_SECONDS
        score -= WEIGHT_OUTGOING_TAIL_TRIM_PENALTY * trim_severity
        reasons.append(f"- outgoing cue starts {outgoing_tail_unused:.1f}s before the track's own end")

    # No upper clamp (Commit C.1): score is a ranking signal, not a
    # probability/confidence (see `confidence`, a separate field, for
    # that). The base five weights above already sum to 1.0 on their own,
    # so a highly reliable candidate can legitimately reach ~1.0 before any
    # bonus is even considered -- clamping to 1.0 there made every
    # advanced bonus (key/energy/structure/local-energy) a no-op for
    # exactly the candidates confident enough to matter most, and made two
    # candidates that only differ by a bonus tie instead of the bonus
    # actually breaking the tie in ranking.
    return max(0.0, score), tuple(reasons)


def key_shift(outgoing: TrackAnalysis, incoming: TrackAnalysis) -> int | None:
    """Semitones to glide the outgoing tail by so the two keys mix (None: leave it)."""
    if (outgoing.key is None or incoming.key is None
            or min(outgoing.key_confidence, incoming.key_confidence) < KEY_SHIFT_MIN_CONFIDENCE):
        return None
    return harmonic_shift(outgoing.key, incoming.key, max_semitones=1)


def _heard_key_clash(
    outgoing: TrackAnalysis, incoming: TrackAnalysis, strategy: TransitionStrategy, outgoing_rate: float,
) -> float:
    """How hard the keys clash as the overlap will sound (0.0: no clash, or unknown).

    A rate-matched tail the planner re-pitches into a compatible key does not
    clash; otherwise the wheel distance counts as far as both keys are trusted."""
    if outgoing.key is None or incoming.key is None:
        return 0.0
    if (strategy is TransitionStrategy.BEAT_MATCH and abs(outgoing_rate - 1.0) > 1e-9
            and key_shift(outgoing, incoming) is not None):
        return 0.0
    severity = key_clash_severity(outgoing.key, incoming.key) or 0.0
    return severity * min(outgoing.key_confidence, incoming.key_confidence)


def audible_end(analysis: TrackAnalysis) -> float:
    """Where the track's sound ends (its duration when unknown)."""
    end = analysis.audible_end_seconds
    return analysis.duration_seconds if end is None else min(end, analysis.duration_seconds)


def audible_start(analysis: TrackAnalysis) -> float:
    """Where the track's sound starts (0 when unknown)."""
    return analysis.audible_start_seconds or 0.0


def _has_activity_in_range(spans: tuple[tuple[float, float], ...], start: float, end: float) -> bool:
    return any(span_start < end and span_end > start for span_start, span_end in spans)


def _nearest_octave_rate(rate: float, settings: AutoMixTransitionSettings) -> float:
    """``rate``, or its double/half when that is closer to 1.0 (a half-time beat grid)."""
    options = (rate, rate * 2.0, rate / 2.0) if settings.allow_half_double_tempo else (rate,)
    return min(options, key=lambda option: abs(option - 1.0))


def _last_vocal_end(analysis: TrackAnalysis, start: float, end: float) -> float | None:
    """When the singing inside ``[start, end]`` stops for good; None if there is none.

    Lyric lines count too: they can only say "still singing", never "silent"."""
    ends = [min(b, end) for a, b in (*analysis.vocal_activity, *analysis.lyric_vocal_spans)
            if a < end and b > start]
    return max(ends) if ends else None


def measured_vocal_regions(analysis: TrackAnalysis) -> tuple[tuple[float, float], ...]:
    """Where ``analysis`` actually measured vocal activity (see TrackAnalysis.vocal_coverage)."""
    if analysis.vocal_coverage is not None:
        return analysis.vocal_coverage
    return ((0.0, analysis.duration_seconds),) if analysis.vocal_activity else ()


def vocals_measured(analysis: TrackAnalysis, start: float, end: float) -> bool:
    """Whether vocal activity was measured over all of ``[start, end]``."""
    return any(a <= start + VOCAL_EDGE_TOLERANCE_SECONDS and b >= end - VOCAL_EDGE_TOLERANCE_SECONDS
               for a, b in measured_vocal_regions(analysis))


def sung_spans(analysis: TrackAnalysis) -> tuple[tuple[float, float], ...]:
    """Measured singing plus synced-lyric lines (which can only ever add singing)."""
    return (*analysis.vocal_activity, *analysis.lyric_vocal_spans)


def _instrumental(analysis: TrackAnalysis, start: float, end: float) -> bool:
    """Whether ``analysis`` is known not to sing in ``[start, end]``.

    Only a measured span can say so: an unmeasured stretch, a failed detection
    and a light analyzer's empty result are all "unknown", never "silent"."""
    return vocals_measured(analysis, start, end) and not any(
        a < end - VOCAL_EDGE_TOLERANCE_SECONDS and b > start for a, b in sung_spans(analysis))


def vocal_intro_end(analysis: TrackAnalysis) -> float | None:
    """Where the first singing starts (the instrumental intro's end); None if unknown.

    The whole track when it is measured end to end without a sung second."""
    start = audible_start(analysis)
    head = next(((a, b) for a, b in measured_vocal_regions(analysis) if a <= start + VOCAL_EDGE_TOLERANCE_SECONDS), None)
    if head is None:
        return None
    first = min((a for a, b in sung_spans(analysis) if b > start), default=None)
    if first is not None and first < head[1]:
        return float(max(start, first))
    return head[1] if head[1] >= audible_end(analysis) - VOCAL_EDGE_TOLERANCE_SECONDS else None


def vocal_outro_start(analysis: TrackAnalysis) -> float | None:
    """Where the last singing ends, confirmed silent from there to the end; None if unknown.

    A gap mid-song is not an outro: only a measured stretch that runs to the
    track's last sound can show that no voice comes back."""
    end = audible_end(analysis)
    tail = next(((a, b) for a, b in measured_vocal_regions(analysis) if b >= end - VOCAL_EDGE_TOLERANCE_SECONDS), None)
    if tail is None:
        return None
    last = max((min(b, end) for a, b in sung_spans(analysis) if a < end), default=None)
    if last is not None and last > tail[0]:
        return float(last)
    return tail[0] if tail[0] <= audible_start(analysis) + VOCAL_EDGE_TOLERANCE_SECONDS else None


def _sings_to_end(analysis: TrackAnalysis) -> bool:
    """Whether the singing leaves less than MIN_AFTER_VOCAL_SECONDS before the track's last sound."""
    end = audible_end(analysis)
    vocal_end = _last_vocal_end(analysis, 0.0, end)
    return vocal_end is not None and end - vocal_end < MIN_AFTER_VOCAL_SECONDS + VOCAL_EDGE_TOLERANCE_SECONDS


def _fallback_candidates(
    outgoing: TrackAnalysis, incoming: TrackAnalysis, settings: AutoMixTransitionSettings,
    reasons: tuple[str, ...], outgoing_playback_rate: float = 1.0,
) -> list[TransitionCandidate]:
    """Without shared tempo, measured music still gets a choice of handoff lengths."""
    lengths = ((settings.fallback_crossfade_seconds, 1.0, 2.0, 4.0, 6.0, 8.0)
               if outgoing.rms_curve and incoming.rms_curve else (settings.fallback_crossfade_seconds,))
    refinements = ()
    if outgoing.band_power_curve and incoming.band_power_curve:
        # Halfway lengths let the measured envelopes avoid immediate kick or
        # vocal entries between the usual whole-second handoffs.
        ordered = sorted(set(lengths))
        refinements = tuple((left + right) / 2 for left, right in zip(ordered, ordered[1:])
                            if settings.min_transition_seconds <= left < right <= settings.max_transition_seconds)
    candidates = _deduplicate_candidates([
        replace(candidate, refined_duration=True) if length in refinements else candidate
        for length in (*lengths, *refinements)
        for handoff in ((False, True) if outgoing.rms_curve and incoming.rms_curve else (False,))
        for candidate in _fixed_candidates(outgoing, incoming, replace(settings, fallback_crossfade_seconds=length),
                                            reasons, outgoing_playback_rate, handoff=handoff)
    ])
    fades = [candidate for candidate in candidates if candidate.strategy is not TransitionStrategy.CUT]
    if outgoing.rms_curve and incoming.rms_curve:
        # Tempo preservation and measured pair confidence apply to a natural
        # handoff too. A missing shared beat is not a 0.3 ceiling on quality.
        fades = [replace(candidate, score=candidate.score + WEIGHT_TEMPO + WEIGHT_CONFIDENCE *
                         (outgoing.bpm_confidence + incoming.bpm_confidence) / 2) for candidate in fades]
    return fades or candidates


def _fixed_candidates(
    outgoing: TrackAnalysis, incoming: TrackAnalysis, settings: AutoMixTransitionSettings,
    reasons: tuple[str, ...], outgoing_playback_rate: float = 1.0,
    *, handoff: bool = False,
) -> list[TransitionCandidate]:
    """FIXED_CROSSFADE if both tracks have room for one, otherwise CUT.

    The crossfade spans the outgoing track's last *audible* seconds and the
    incoming one's first (trailing/leading silence is trimmed): on real
    masters a fixed fade over the file's very end overlapped only silence.
    """
    outgoing_end, incoming_start = audible_end(outgoing), audible_start(incoming)
    available = min((outgoing_end - audible_start(outgoing)) / outgoing_playback_rate,
                    audible_end(incoming) - incoming_start, settings.max_transition_seconds)
    shortest = settings.min_transition_seconds
    # Two voices must not share the fade: it starts once the outgoing singer
    # stops, unless the incoming intro is instrumental. Sung to the very end on
    # both sides, the plain fade stays as a short handoff (HANDOFF_BARS' reason).
    vocal_end = _last_vocal_end(outgoing, 0.0, outgoing_end)
    tail = (outgoing_end - vocal_end) / outgoing_playback_rate if vocal_end is not None else None
    fade = min(settings.fallback_crossfade_seconds, available)
    if tail is not None and tail < fade and not handoff:
        if _instrumental(incoming, incoming_start, incoming_start + fade):
            reasons += ("+ fading under the outgoing vocals: the incoming intro is instrumental",)
        else:
            intro_end = vocal_intro_end(incoming)
            intro = max(0.0, intro_end - incoming_start) if intro_end is not None else 0.0
            safe_window = max(tail, intro)
            if safe_window >= MIN_AFTER_VOCAL_SECONDS:
                available = min(available, safe_window)
                shortest = min(shortest, MIN_AFTER_VOCAL_SECONDS)
                reasons += ("+ shorter fade keeps the two vocal phrases apart",)
            else:
                available = min(available, FIXED_HANDOFF_MAX_SECONDS)
                shortest = min(shortest, MIN_AFTER_VOCAL_SECONDS)
                reasons += ("- both tracks sing across the junction: a short fade hands the voice over",)
    duration_seconds = min(settings.fallback_crossfade_seconds, available)
    if duration_seconds < shortest:
        # A cut still drops the silence between the two sounds.
        return [TransitionCandidate(
            from_track_id=outgoing.track_id, to_track_id=incoming.track_id,
            outgoing_source_time=outgoing_end, outgoing_source_out=outgoing_end,
            incoming_source_time=incoming_start,
            bars=0, duration_seconds=0.0,
            outgoing_bpm=outgoing.bpm, incoming_bpm=incoming.bpm, target_bpm=None,
            outgoing_rate=1.0, incoming_rate=1.0,
            score=0.1, confidence=0.0, strategy=TransitionStrategy.CUT,
            reasons=reasons + ("- not enough source duration for even a fixed crossfade",),
        )]
    # The incoming rate is 1.0; the outgoing clip may retain a prior match.
    return [TransitionCandidate(
        from_track_id=outgoing.track_id, to_track_id=incoming.track_id,
        outgoing_source_time=max(0.0, outgoing_end - duration_seconds * outgoing_playback_rate),
        outgoing_source_out=outgoing_end,
        incoming_source_time=incoming_start,
        bars=0, duration_seconds=duration_seconds,
        outgoing_bpm=outgoing.bpm, incoming_bpm=incoming.bpm, target_bpm=None,
        outgoing_rate=1.0, incoming_rate=1.0,
        score=0.3, confidence=0.0, strategy=TransitionStrategy.FIXED_CROSSFADE,
        reasons=reasons + (f"+ {duration_seconds:.1f}s fixed crossfade fits both tracks",),
    )]
