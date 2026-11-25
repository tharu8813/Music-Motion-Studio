"""AutoMixPlanner: Timeline + analyses + settings -> CompiledRenderPlan.

Preview and Export never see how a transition was decided (roadmap Phase 4
section 1) -- they only ever read a CompiledRenderPlan, exactly as they do
for the Sequential compiler in app/timeline/compiler.py. This module is a
second, independent producer of that same shape; it does not touch
compile_timeline()/compile_playlist() at all, so legacy (non-AutoMix)
playback is provably unaffected by anything here.

Tempo policy (DJ-style): every track plays at its own tempo from its first
beat until its last bars. For BEAT_MATCH the *outgoing* track eases onto
the incoming track's tempo over up to RAMP_BARS bars before their overlap
(AudioRenderClip.tempo_ramp) and holds it through the overlap; the
incoming track never changes speed. An earlier policy instead played the
whole incoming track at the outgoing tempo, which then carried into the
next pair, so a playlist drifted further from its tracks' real tempos with
every transition and never came back.

Silence: the first track starts at its first sound, the last stops at its
last, and a CUT or unplanned junction butts the two sounds together.

Candidate duration is authoritative (Commit C.1): the planner applies
`TransitionCandidate.duration_seconds` and `outgoing_source_out` directly.
Candidates now retain the audible outgoing ending and finalize their actual
source span and timeline duration before scoring. An early structure hint
cannot discard the remaining audio or inflate the rendered overlap after
scoring. Presentation and chapter ownership still follow transition starts.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace

from app.automix.candidates import (
    TransitionCandidate,
    TransitionStrategy,
    _structure_incoming_anchor,
    _nearest_octave_rate,
    _structure_outgoing_anchor,
    audible_end,
    audible_start,
    generate_candidates,
    key_shift as _key_shift,
    select_best_candidate,
    vocal_intro_end,
    vocal_outro_start,
)
from app.automix.analysis.key import shift_key
from app.automix.compatibility import evaluate_compatibility
from app.automix.eq_response import track_band_gains
from app.automix.exits import CUT_SECONDS, phrase_exits
from app.automix.intelligence import choose_transition, optimize_handoff
from app.automix.models import TrackAnalysis
from app.automix.overrides import (
    MAX_DURATION_SECONDS, STYLE_AUTO, STYLE_CUT, STYLE_EQ, STYLE_LEGACY, TransitionOverride,
)
from app.automix.settings import AutoMixTransitionSettings
from app.automix.structure.models import TrackStructureAnalysis
from app.automix.transition_style import describe_transition, select_transition_dsp
from app.models.playlist import PlaylistTrack
from app.timeline.models import TransitionType
from app.timeline.render_plan import (
    AudioRenderClip,
    AudioRenderPlan,
    AudioRenderTransition,
    CompiledRenderPlan,
    TempoRamp,
    TransitionDsp,
    build_presentation_and_metadata,
    validate_compiled_render_plan,
)

LOGGER = logging.getLogger(__name__)
_GAP_EPSILON = 1e-6

_STRATEGY_TRANSITION_TYPES = {
    TransitionStrategy.BEAT_MATCH: TransitionType.BEAT_MATCH,
    TransitionStrategy.BEAT_ALIGNED_CROSSFADE: TransitionType.EQUAL_POWER,
    TransitionStrategy.FIXED_CROSSFADE: TransitionType.CROSSFADE,
    TransitionStrategy.PHRASE_EXIT: TransitionType.EQUAL_POWER,
}
"""CUT is deliberately absent: a CUT candidate means no overlap at all, so
no AudioRenderTransition is emitted for that boundary (see _place_tracks).
TransitionType.AUTOMIX is reserved until a renderer gives it a distinct
meaning (roadmap section 9)."""


def compile_automix(
    tracks: Sequence[PlaylistTrack],
    analyses: Mapping[str, TrackAnalysis],
    settings: AutoMixTransitionSettings,
    structures: Mapping[str, TrackStructureAnalysis] | None = None,
    *, log_diagnostics: bool = True, pending_analysis: Collection[str] = (),
) -> CompiledRenderPlan:
    """Compile enabled ``tracks`` into a CompiledRenderPlan with AutoMix overlaps.

    Disabled tracks are skipped entirely (never occupy a clip or influence
    placement), matching Sequential's ``enabled_only=True`` behavior. A
    track with an explicit ``start_time_seconds`` gap preserves that gap
    verbatim rather than being overlapped into (roadmap section 12: "For
    v1, preserving explicit requested gaps ... is a reasonable conservative
    policy"). Missing or incompatible analysis on any one pair only
    degrades that pair -- it never prevents earlier or later pairs from
    getting a full AutoMix transition. Completed analysis without a result
    uses a bounded crossfade. A progressive render may leave pending pairs
    sequential until both tracks finish analysis.

    ``structures`` is optional (defaults to ``None``, meaning "no structure
    data at all") and independent per track: a track missing from it, or
    entirely absent, only ever removes the structure-aware
    bonuses/candidates for that side of a pair (see
    ``app.automix.candidates.generate_candidates``) -- rhythm-only
    planning behaves exactly as it did before this parameter existed.

    A track of zero length (its duration could not be read) has nothing to
    place; it is skipped like a disabled one instead of producing a window
    that starts together with the next and failing plan validation. Export
    rejects such a playlist up front; Preview still has to plan it.
    """
    selected = [track for track in tracks if track.enabled and track.duration_seconds > 0.0]
    structures = dict(structures or {})
    analyses = {
        track_id: _with_lyric_vocals(track, analyses[track_id])
        for track in selected if (track_id := track.id) in analyses
    }
    for track_id, analysis in analyses.items():
        if analysis.rms_curve:
            structure = structures.get(track_id) or TrackStructureAnalysis(
                track_id=track_id, source_path=analysis.source_path, duration_seconds=analysis.duration_seconds)
            # Measured energy/timbre remains available without Sonara. Keep its
            # real section boundaries and curves whenever it supplied them.
            structures[track_id] = replace(
                structure, energy_curve=structure.energy_curve or tuple(min(1.0, v / 0.3) for v in analysis.rms_curve),
                energy_curve_hop_seconds=structure.energy_curve_hop_seconds if structure.energy_curve else 1.0,
                bass_curve=structure.bass_curve or analysis.bass_curve,
                brightness_curve=structure.brightness_curve or analysis.brightness_curve,
                percussive_curve=structure.percussive_curve or analysis.percussive_curve)
    clips, transitions = _place_tracks(selected, analyses, structures, settings, log_diagnostics, pending_analysis)
    presentation, metadata, duration = build_presentation_and_metadata(clips)
    plan = CompiledRenderPlan(
        audio=AudioRenderPlan(clips=clips, transitions=transitions),
        presentation=presentation,
        metadata=metadata,
        duration_seconds=duration,
    )
    validate_compiled_render_plan(plan)
    return plan


LYRIC_LINE_MAX_SECONDS = 8.0
"""A synced-lyrics line counts as sung until the next line or this long, so a
long instrumental break before the next line is not taken for singing."""


def _with_lyric_vocals(track: PlaylistTrack, analysis: TrackAnalysis) -> TrackAnalysis:
    """``analysis`` plus where ``track``'s synced lyrics say it is sung.

    Cue times are lyric time; lyric time = audio time + the track's lyric
    offset (see app.preview.frame_state.resolve_lyrics_cue_state).
    """
    offset = float(track.lyrics_timing_offset_seconds)
    spans = []
    for cue in track.lyrics:
        if not str(cue.get("text", "")).strip():
            continue
        start = max(0.0, float(cue["start"]) - offset)
        end = min(analysis.duration_seconds, float(cue["end"]) - offset, start + LYRIC_LINE_MAX_SECONDS)
        if end > start:
            spans.append((start, end))
    if not spans:
        return analysis
    return replace(
        analysis, lyric_vocal_spans=tuple(spans),
        audible_start_seconds=min(audible_start(analysis), min(a for a, _ in spans)),
        audible_end_seconds=max(audible_end(analysis), max(b for _, b in spans)),
    )


def _place_tracks(
    tracks: list[PlaylistTrack],
    analyses: Mapping[str, TrackAnalysis],
    structures: Mapping[str, TrackStructureAnalysis],
    settings: AutoMixTransitionSettings,
    log_diagnostics: bool = True,
    pending_analysis: Collection[str] = (),
) -> tuple[tuple[AudioRenderClip, ...], tuple[AudioRenderTransition, ...]]:
    clips: list[AudioRenderClip] = []
    transitions: list[AudioRenderTransition] = []
    natural_cursor = 0.0
    actual_cursor = 0.0

    for index, track in enumerate(tracks):
        natural_floor = natural_cursor
        requested_start = track.start_time_seconds
        natural_start = (
            max(natural_floor, requested_start) if requested_start is not None else natural_floor
        )
        explicit_gap = natural_start - natural_floor

        if index == 0:
            timeline_start = natural_start
            source_in = _audible_start(analyses.get(track.id), settings)  # no dead air before the mix
        elif explicit_gap > _GAP_EPSILON:
            timeline_start = actual_cursor + explicit_gap
            source_in = 0.0
        else:
            previous = clips[-1]
            into_previous = transitions[-1] if transitions and transitions[-1].clip_b == previous.clip_id else None
            # The outgoing tempo ramp must not start while the previous transition is still mixing.
            ramp_floor = (previous.source_at(into_previous.timeline_start + into_previous.duration)
                          if into_previous is not None else previous.source_in)
            outgoing_clip, timeline_start, source_in, transition = _plan_overlap(
                previous, tracks[index - 1], track, analyses, structures, settings, ramp_floor, log_diagnostics,
                previous_style=into_previous.dsp if into_previous is not None else None,
                analysis_pending=previous.track_id in pending_analysis or track.id in pending_analysis,
            )
            # AudioRenderClip is immutable: the outgoing clip's trimmed tail
            # (and tempo ramp) replaces the one appended last iteration.
            clips[-1] = outgoing_clip
            if transition is not None:
                transitions.append(transition)

        clip = AudioRenderClip(
            clip_id=f"automix:{track.id}",
            track_id=track.id,
            timeline_start=timeline_start,
            source_in=source_in,
            source_out=track.duration_seconds,
        )
        clips.append(clip)
        natural_cursor = natural_start + track.duration_seconds
        actual_cursor = clip.timeline_end

    if clips:  # nor after it
        last = clips[-1]
        clips[-1] = replace(last, source_out=_audible_end(analyses.get(last.track_id), settings, last))
    return tuple(clips), tuple(transitions)


RAMP_BARS = 8
"""The outgoing track eases onto the incoming tempo over this many of its own
bars before their overlap (fewer when the track has no room): one phrase, the
way a DJ rides the pitch fader instead of jumping it."""


BRIDGE_RAMP_BARS = 16
"""A tempo bridge (beyond max_tempo_change_percent) spreads its larger change over
two phrases, so no single bar moves more than a direct match's ramp would."""
KEY_GLIDE_SECONDS = 8.0
"""A hand-set key shift on a track without a tempo glides over this long before the cue."""


def _audible_start(analysis: TrackAnalysis | None, settings: AutoMixTransitionSettings) -> float:
    return audible_start(analysis) if analysis is not None and settings.enabled else 0.0


def _audible_end(analysis: TrackAnalysis | None, settings: AutoMixTransitionSettings, clip: AudioRenderClip) -> float:
    """Where ``clip`` should stop at the latest: its track's audible end, never before it starts."""
    if analysis is None or not settings.enabled:
        return clip.source_out
    return max(clip.source_in, min(clip.source_out, audible_end(analysis)))


def _plan_overlap(
    previous_clip: AudioRenderClip,
    previous_track: PlaylistTrack,
    track: PlaylistTrack,
    analyses: Mapping[str, TrackAnalysis],
    structures: Mapping[str, TrackStructureAnalysis],
    settings: AutoMixTransitionSettings,
    ramp_floor: float,
    log_diagnostics: bool = True,
    previous_style: TransitionDsp | None = None,
    analysis_pending: bool = False,
) -> tuple[AudioRenderClip, float, float, AudioRenderTransition | None]:
    """Decide how ``track`` follows ``previous_clip`` with no explicit gap.

    Returns (outgoing clip -- ``previous_clip`` with its tail trimmed and,
    for BEAT_MATCH, its tempo ramp --, incoming timeline_start, incoming
    source_in, transition_or_none). The incoming clip always plays at its
    own tempo. Without a transition (pending analysis, CUT) the two tracks
    butt together with the silence between their sounds removed.
    """
    outgoing_analysis = analyses.get(previous_track.id)
    incoming_analysis = analyses.get(track.id)

    def adjacent(outgoing_source_out: float, incoming_source_in: float):
        outgoing_clip = replace(previous_clip, source_out=max(previous_clip.source_in, outgoing_source_out))
        return outgoing_clip, outgoing_clip.timeline_end, incoming_source_in, None

    fallback = adjacent(_audible_end(outgoing_analysis, settings, previous_clip),
                        _audible_start(incoming_analysis, settings))
    override = settings.override_for(previous_track.id, track.id) if settings.enabled else None

    def manual(recommendation: tuple[tuple[str, object], ...] = ()):
        return _plan_manual(
            previous_clip, previous_track, track, override, outgoing_analysis, incoming_analysis,
            structures, settings, ramp_floor, fallback, log_diagnostics, recommendation,
        )

    if override is not None and not override.recommend:
        return manual()
    if not settings.enabled:
        return fallback
    if outgoing_analysis is None or incoming_analysis is None:
        if override is not None:
            return manual((("recommendation", "no_analysis"),))
        if analysis_pending:
            return fallback
        outgoing, end, source_in, _ = fallback
        available_out = outgoing.timeline_end - outgoing.timeline_at(max(ramp_floor, outgoing.source_in))
        incoming_end = min(track.duration_seconds, audible_end(incoming_analysis)) if incoming_analysis else track.duration_seconds
        overlap = min(settings.fallback_crossfade_seconds, available_out, outgoing.duration / 2,
                      (incoming_end - source_in) / 2)
        if overlap <= _GAP_EPSILON:
            return fallback
        start = end - overlap
        transition = AudioRenderTransition(
            outgoing.clip_id, f"automix:{track.id}", start, overlap, TransitionType.EQUAL_POWER,
            details=(("mode", "auto"), ("strategy", "fixed_crossfade"), ("missing_analysis", True)),
        )
        return outgoing, start, source_in, transition

    compatibility = evaluate_compatibility(outgoing_analysis, incoming_analysis, settings)
    candidates = generate_candidates(
        outgoing_analysis, incoming_analysis, compatibility, settings,
        outgoing_playback_rate=previous_clip.playback_rate,
        outgoing_structure=structures.get(previous_track.id),
        incoming_structure=structures.get(track.id),
    )
    unlocked = select_best_candidate(candidates)
    if override is not None:  # a recommendation that keeps the user's locked values
        candidates = _honour_locks(candidates, override, outgoing_analysis, incoming_analysis)
    # Never rank a window that collides with the preceding junction. A later,
    # shorter candidate can still fit even when the preferred long one cannot.
    candidates = [c for c in candidates if c.outgoing_source_time >= max(ramp_floor, previous_clip.source_in) - _GAP_EPSILON]
    band_gains = (track_band_gains(previous_track), track_band_gains(track))
    best, planned_decision = choose_transition(
        candidates, compatibility, outgoing_analysis, incoming_analysis,
        structures.get(previous_track.id), structures.get(track.id), previous_style=previous_style,
        band_gains=band_gains, allow_pitch_shift=settings.allow_pitch_shift)
    phrase_exit = None
    if best is None or best.strategy in (TransitionStrategy.FIXED_CROSSFADE, TransitionStrategy.CUT):
        # An effect must beat the safe fade on the same acoustic scale.
        fixed_score = max((c.score for c in candidates if c.strategy is TransitionStrategy.FIXED_CROSSFADE), default=0.3)
        for exit_option in phrase_exits(
            outgoing_analysis, incoming_analysis,
            structures.get(previous_track.id), structures.get(track.id),
            outgoing_rate=previous_clip.playback_rate, earliest=ramp_floor, previous_style=previous_style,
            allow_tape_stop=settings.allow_tape_stop,
        ):
            candidate = replace(exit_option.candidate, score=fixed_score)
            if override is not None:
                unlocked = unlocked or candidate
                held = _honour_locks([candidate], override, outgoing_analysis, incoming_analysis)
                if not held or any(abs(getattr(held[0], name) - getattr(candidate, name)) > _GAP_EPSILON
                                   for name in ("outgoing_source_time", "incoming_source_time", "duration_seconds")):
                    continue
            candidate, decision = optimize_handoff(
                candidate, exit_option.decision, outgoing_analysis, incoming_analysis, structures.get(track.id),
                previous_style=previous_style, band_gains=band_gains,
                exit_beat=exit_option.beat_seconds,
            )
            if any(name == "acoustic_cost" for name, _ in decision.metrics) and (best is None or candidate.score > best.score):
                best = candidate
                phrase_exit = replace(exit_option, candidate=candidate, decision=decision)
    if override is not None and (best is None or best.duration_seconds <= 0.0):
        # Nothing analysis would pick keeps the locked values: play the stored ones, and say so.
        nearest = (() if unlocked is None else
                   (("nearest_outgoing_cue", unlocked.outgoing_source_time),
                    ("nearest_incoming_cue", unlocked.incoming_source_time),
                    ("nearest_duration", unlocked.duration_seconds)))
        return manual((("recommendation", "no_fit"), *nearest))
    if best is None or best.duration_seconds <= 0.0:
        return fallback
    if best.strategy is TransitionStrategy.CUT:
        return adjacent(best.outgoing_source_out, best.incoming_source_time)

    source_in = best.incoming_source_time
    cue = best.outgoing_source_time
    outgoing_clip = replace(previous_clip, source_out=best.outgoing_source_out)
    ramp_seconds = 0.0
    key_shift = None
    selector_outgoing = outgoing_analysis
    if best.strategy is TransitionStrategy.BEAT_MATCH and abs(best.outgoing_rate - 1.0) > 1e-9:
        # Walk the outgoing track onto the incoming tempo over its last bars
        # before the cue, then hold that rate through the overlap: its beats
        # land on the incoming ones, and the incoming track never changes speed.
        # A tempo bridge (beyond the direct budget) takes a longer ramp.
        bridge = abs(best.outgoing_rate - 1.0) * 100.0 > settings.max_tempo_change_percent + 1e-9
        bars = BRIDGE_RAMP_BARS if bridge else RAMP_BARS
        bar = (outgoing_analysis.meter_numerator or 4) * 60.0 / outgoing_analysis.bpm
        ramp_start = min(cue, max(cue - bars * bar, ramp_floor, previous_clip.source_in))
        ramp_seconds = cue - ramp_start
        key_shift = _key_shift(outgoing_analysis, incoming_analysis) if settings.allow_pitch_shift else None
        end_pitch = 2.0 ** (key_shift / 12.0) if key_shift else 1.0
        if key_shift:
            # The selector hears the tail as it will play: in the shifted, compatible key.
            selector_outgoing = replace(outgoing_analysis, key=shift_key(outgoing_analysis.key, key_shift))
        outgoing_clip = replace(outgoing_clip, tempo_ramp=TempoRamp(
            ramp_start, cue, best.outgoing_rate, end_pitch=end_pitch))

    # Anchor timestamps are in the original media, not the playlist clock.
    timeline_start = outgoing_clip.timeline_at(cue)
    if timeline_start <= previous_clip.timeline_start:
        return fallback if override is None else manual((("recommendation", "no_fit"),))

    # duration_seconds is authoritative (Commit C.1): the candidate already
    # sized it as the outgoing tail from the cue at the overlap rate, which
    # is exactly outgoing_clip.timeline_end - timeline_start.
    overlap = best.duration_seconds

    transition_type = _STRATEGY_TRANSITION_TYPES[best.strategy]
    # DSP Phase 2: the mixing style is decided here, where the analysis and
    # the exact window both exist, and travels in the plan -- the renderer
    # never re-derives it. Timing above is already final and is not touched.
    decision = phrase_exit.decision if phrase_exit is not None else planned_decision or select_transition_dsp(
        best, compatibility, selector_outgoing, incoming_analysis,
        structures.get(previous_track.id), structures.get(track.id), previous_style=previous_style,
    )
    outgoing_structure = structures.get(previous_track.id)
    incoming_structure = structures.get(track.id)
    details = (
        ("mode", "auto"),
        ("strategy", best.strategy.value),
        ("bars", best.bars),
        ("score", best.score),
        ("outgoing_bpm", outgoing_analysis.bpm),
        ("incoming_bpm", incoming_analysis.bpm),
        ("target_bpm", best.target_bpm),
        ("outgoing_rate", best.outgoing_rate),
        ("tempo_ramp_seconds", ramp_seconds),
        ("incoming_rate", best.incoming_rate),
        ("tempo_delta_percent", compatibility.tempo_shift_percent),
        ("half_double_tempo", compatibility.used_half_double),
        ("outgoing_beat_confidence", outgoing_analysis.bpm_confidence),
        ("incoming_beat_confidence", incoming_analysis.bpm_confidence),
        ("outgoing_downbeat_confidence", outgoing_analysis.meter_confidence),
        ("incoming_downbeat_confidence", incoming_analysis.meter_confidence),
        # "basic" = no downbeat model and no vocal detection; "*-novocals" =
        # vocal detection failed. Preview surfaces both (AutoMixDetailsPanel).
        ("outgoing_analyzer", outgoing_analysis.analyzer_id),
        ("incoming_analyzer", incoming_analysis.analyzer_id),
        ("outgoing_cue", best.outgoing_source_time),
        ("outgoing_cut", best.outgoing_source_out),
        ("incoming_cue", best.incoming_source_time),
        ("outgoing_tail_trimmed", outgoing_analysis.duration_seconds - best.outgoing_source_out),
        # Vocal-based sections: None = not measured (never "no vocals").
        ("outgoing_vocal_outro_start", vocal_outro_start(outgoing_analysis)),
        ("incoming_vocal_intro_end", vocal_intro_end(incoming_analysis)),
        ("outgoing_structure_anchor", _structure_outgoing_anchor(outgoing_structure)),
        ("incoming_structure_anchor", _structure_incoming_anchor(incoming_structure)),
        ("outgoing_key", outgoing_analysis.key),
        ("incoming_key", incoming_analysis.key),
        ("key_shift_semitones", key_shift),
        *decision.metrics,
        *((("recommendation", "locked"), ("locked", override.locked)) if override is not None else ()),
    )
    transition = AudioRenderTransition(
        clip_a=previous_clip.clip_id, clip_b=f"automix:{track.id}",
        timeline_start=timeline_start, duration=overlap, type=transition_type,
        dsp=decision.dsp, dsp_reasons=decision.reasons, details=details, vocal_handoff=decision.vocal_handoff,
        band_windows=decision.band_windows,
        beat_seconds=phrase_exit.beat_seconds if phrase_exit is not None else None,
    )
    # One line per transition in the app log, for tuning against real music.
    if log_diagnostics:
        LOGGER.info("AutoMix transition: %s", describe_transition(
            transition, previous_track.title or previous_track.id, track.title or track.id,
        ))
    return outgoing_clip, timeline_start, source_in, transition


MIN_MANUAL_OVERLAP_SECONDS = 0.05
"""A manual window shorter than this (or one squeezed to it by the tracks'
ends) is played as a cut: an overlap FFmpeg cannot fade is not a mix."""
LOCK_REACH_BARS = 1.0
"""A recommendation may hold a locked cue when its own cue is at most this far from it."""
LOCK_DISTANCE_WEIGHT = 0.1
"""Score lost per ``LOCK_REACH_BARS`` a candidate had to move: the nearest good one wins."""


def _honour_locks(
    candidates: Sequence[TransitionCandidate], override: TransitionOverride,
    outgoing: TrackAnalysis, incoming: TrackAnalysis,
) -> list[TransitionCandidate]:
    """``candidates`` moved onto ``override``'s locked values; one that cannot keep them is dropped.

    A locked cue takes a candidate whose own cue is within a bar of it, and the
    other (unlocked) cue moves by the same time so the two songs' beats stay
    as aligned as the candidate had them; an unlocked length then ends where
    the songs do. A locked length takes any candidate both songs have room
    for. A cut has no window to hold anything.
    """
    locked = set(override.locked)
    reach = (LOCK_REACH_BARS * (outgoing.meter_numerator or 4) * 60.0 / outgoing.bpm) if outgoing.bpm else 0.25
    held = []
    for candidate in candidates:
        rate = candidate.outgoing_rate
        if candidate.strategy is TransitionStrategy.CUT or candidate.duration_seconds <= 0.0 or rate <= 0.0:
            continue
        out_shift = override.outgoing_cue - candidate.outgoing_source_time if "outgoing_cue" in locked else None
        in_shift = override.incoming_cue - candidate.incoming_source_time if "incoming_cue" in locked else None
        if any(abs(shift) > reach for shift in (out_shift, in_shift) if shift is not None):
            continue
        if out_shift is None:
            out_shift = (in_shift or 0.0) * rate
        if in_shift is None:
            in_shift = out_shift / rate
        cue = candidate.outgoing_source_time + out_shift
        incoming_cue = candidate.incoming_source_time + in_shift
        duration = override.duration if "duration" in locked else min(
            candidate.duration_seconds, (outgoing.duration_seconds - cue) / rate,
            incoming.duration_seconds - incoming_cue)
        outgoing_out = cue + duration * rate
        if (cue < 0.0 or incoming_cue < 0.0 or duration < MIN_MANUAL_OVERLAP_SECONDS
                or outgoing_out > outgoing.duration_seconds + _GAP_EPSILON
                or incoming_cue + duration > incoming.duration_seconds + _GAP_EPSILON):
            continue
        bar = (incoming.meter_numerator or 4) * 60.0 / incoming.bpm if incoming.bpm else None
        held.append(replace(
            candidate, outgoing_source_time=cue, outgoing_source_out=outgoing_out,
            incoming_source_time=incoming_cue, duration_seconds=duration,
            bars=round(duration / bar) if bar else candidate.bars,
            score=candidate.score - LOCK_DISTANCE_WEIGHT * max(abs(out_shift), abs(in_shift) * rate) / reach,
        ))
    return held


def _plan_manual(
    previous_clip: AudioRenderClip,
    previous_track: PlaylistTrack,
    track: PlaylistTrack,
    override: TransitionOverride,
    outgoing_analysis: TrackAnalysis | None,
    incoming_analysis: TrackAnalysis | None,
    structures: Mapping[str, TrackStructureAnalysis],
    settings: AutoMixTransitionSettings,
    ramp_floor: float,
    fallback: tuple[AudioRenderClip, float, float, AudioRenderTransition | None],
    log_diagnostics: bool = True,
    recommendation: tuple[tuple[str, object], ...] = (),
) -> tuple[AudioRenderClip, float, float, AudioRenderTransition | None]:
    """``_plan_overlap`` for a junction the user set by hand.

    ``recommendation``: extra details when a recommendation could not keep the
    locked values and the stored ones play instead (see ``_plan_overlap``).

    The user's cues, length and style are kept as far as the two tracks
    allow: the cue never goes before the previous transition has finished
    (nor the clip's start), the window is shortened to what both files still
    have -- their real ends, not the analysed end of their sound: a hand-set
    window may take in a quiet tail -- and tempo matching needs both BPMs.
    Analysis is optional; without it "auto" style is a plain crossfade.
    """
    outgoing_end = max(previous_clip.source_in, previous_clip.source_out)
    incoming_end = track.duration_seconds
    cue = min(max(float(override.outgoing_cue), ramp_floor, previous_clip.source_in), outgoing_end)
    incoming_cue = min(max(0.0, float(override.incoming_cue)), max(0.0, incoming_end - MIN_MANUAL_OVERLAP_SECONDS))

    def cut() -> tuple[AudioRenderClip, float, float, None]:
        outgoing_clip = replace(previous_clip, source_out=max(previous_clip.source_in, cue))
        if outgoing_clip.timeline_end <= previous_clip.timeline_start:
            return fallback
        return outgoing_clip, outgoing_clip.timeline_end, incoming_cue, None

    if override.style == STYLE_CUT:
        return cut()

    bpms_known = (outgoing_analysis is not None and incoming_analysis is not None
                  and outgoing_analysis.bpm is not None and incoming_analysis.bpm is not None)
    rate = 1.0
    if override.tempo_match and bpms_known:
        # Asked for by hand: always matched, however far apart (automatic mixes keep their limits).
        rate = _nearest_octave_rate(incoming_analysis.bpm / outgoing_analysis.bpm, settings)
    # An echo out takes only the window's first beat from the song; its repeats may
    # ring on past the file's end (the renderer pads the clip with silence).
    echo = override.style == TransitionDsp.ECHO_OUT.value
    outgoing_room = MAX_DURATION_SECONDS if echo else (outgoing_end - cue) / rate
    duration = min(float(override.duration), outgoing_room, incoming_end - incoming_cue)
    if override.style == TransitionDsp.DOWNBEAT_CUT.value:
        duration = min(duration, CUT_SECONDS)  # a cut: the window only keeps it from clicking
    if duration < MIN_MANUAL_OVERLAP_SECONDS:
        return cut()
    outgoing_out = cue + duration * rate if echo else min(outgoing_end, cue + duration * rate)

    outgoing_clip = replace(previous_clip, source_out=outgoing_out)
    ramp_seconds = 0.0
    # Key: a hand-set shift always applies; "automatic" does what the planner
    # does -- only alongside a tempo match, and only for confident key estimates.
    key_shift = override.key_shift
    if key_shift is None:
        key_shift = (_key_shift(outgoing_analysis, incoming_analysis)
                     if settings.allow_pitch_shift and abs(rate - 1.0) > 1e-9 and bpms_known
                     else None)
    end_pitch = 2.0 ** (key_shift / 12.0) if key_shift else 1.0
    if abs(rate - 1.0) > 1e-9 or end_pitch != 1.0:
        bpm = outgoing_analysis.bpm if outgoing_analysis is not None else None
        bar = (outgoing_analysis.meter_numerator or 4) * 60.0 / bpm if bpm else KEY_GLIDE_SECONDS / RAMP_BARS
        bars = BRIDGE_RAMP_BARS if abs(rate - 1.0) * 100.0 > settings.max_tempo_change_percent + 1e-9 else RAMP_BARS
        # The editor's "tempo change starts" handle sets the length; else a phrase, as automatic does.
        length = override.ramp_seconds if override.ramp_seconds is not None else bars * bar
        ramp_start = min(cue, max(cue - length, ramp_floor, previous_clip.source_in))
        ramp_seconds = cue - ramp_start
        # A key glide alone keeps the clip's rate (TempoRamp to the same rate).
        end_rate = rate if abs(rate - 1.0) > 1e-9 else previous_clip.playback_rate
        outgoing_clip = replace(outgoing_clip, tempo_ramp=TempoRamp(ramp_start, cue, end_rate, end_pitch=end_pitch))
    timeline_start = outgoing_clip.timeline_at(cue)
    if timeline_start <= previous_clip.timeline_start:
        return fallback
    duration = outgoing_clip.timeline_end - timeline_start

    outgoing_structure = structures.get(previous_track.id)
    incoming_structure = structures.get(track.id)
    bar_seconds = (60.0 * (incoming_analysis.meter_numerator or 4) / incoming_analysis.bpm
                   if incoming_analysis is not None and incoming_analysis.bpm else None)
    bars = round(duration / bar_seconds) if bar_seconds else 0
    metrics: tuple[tuple[str, object], ...] = ()
    reasons: tuple[str, ...] = ()
    handoff = override.vocal_handoff
    band_windows = None
    if override.style == STYLE_AUTO:
        if outgoing_analysis is not None and incoming_analysis is not None:
            strategy = (TransitionStrategy.BEAT_MATCH if rate != 1.0
                        else TransitionStrategy.BEAT_ALIGNED_CROSSFADE if bpms_known
                        else TransitionStrategy.FIXED_CROSSFADE)
            candidate = TransitionCandidate(
                from_track_id=previous_track.id, to_track_id=track.id,
                outgoing_source_time=cue, outgoing_source_out=outgoing_out, incoming_source_time=incoming_cue,
                bars=bars, duration_seconds=duration,
                outgoing_bpm=outgoing_analysis.bpm, incoming_bpm=incoming_analysis.bpm,
                target_bpm=incoming_analysis.bpm if rate != 1.0 else None,
                outgoing_rate=rate, incoming_rate=1.0, score=0.0, confidence=0.0,
                strategy=strategy, reasons=("manual",),
            )
            compatibility = evaluate_compatibility(outgoing_analysis, incoming_analysis, settings)
            heard = (replace(outgoing_analysis, key=shift_key(outgoing_analysis.key, key_shift))
                     if key_shift and outgoing_analysis.key and settings.allow_pitch_shift else outgoing_analysis)
            decision = select_transition_dsp(
                candidate, compatibility, heard, incoming_analysis,
                outgoing_structure, incoming_structure,
            )
            if override.vocal_handoff is None:
                _, decision = optimize_handoff(candidate, decision, heard, incoming_analysis, incoming_structure,
                                               band_gains=(track_band_gains(previous_track), track_band_gains(track)))
                band_windows = decision.band_windows
            dsp, reasons, metrics = decision.dsp, decision.reasons, decision.metrics
            handoff = handoff if handoff is not None else decision.vocal_handoff
        else:
            dsp = TransitionDsp.SHORT_FADE if duration < 4.0 else None
    elif override.style == STYLE_EQ:
        # Hand-set band timing renders through the band splitter; the windows replace the style's.
        from app.automix.renderer import default_eq_bands

        dsp = TransitionDsp.BASS_SWAP
        band_windows = override.eq_bands if override.eq_bands is not None else default_eq_bands()
    else:
        dsp = None if override.style == STYLE_LEGACY else TransitionDsp(override.style)
    if dsp is not TransitionDsp.VOCAL_SAFE_EQ:
        handoff = None
    # BEAT_MATCH without a dsp renders the bass swap; a plain crossfade must stay EQUAL_POWER.
    transition_type = TransitionType.BEAT_MATCH if rate != 1.0 and dsp is not None else TransitionType.EQUAL_POWER
    details = (
        ("mode", "manual"),
        ("manual_style", override.style),
        ("strategy", "manual"),
        ("bars", bars),
        ("outgoing_bpm", outgoing_analysis.bpm if outgoing_analysis is not None else None),
        ("incoming_bpm", incoming_analysis.bpm if incoming_analysis is not None else None),
        ("target_bpm", incoming_analysis.bpm if rate != 1.0 else None),
        ("outgoing_rate", rate),
        ("tempo_ramp_seconds", ramp_seconds),
        ("incoming_rate", 1.0),
        ("tempo_match_requested", override.tempo_match),
        ("outgoing_analyzer", outgoing_analysis.analyzer_id if outgoing_analysis is not None else None),
        ("incoming_analyzer", incoming_analysis.analyzer_id if incoming_analysis is not None else None),
        ("outgoing_cue", cue),
        ("outgoing_cut", outgoing_out),
        ("incoming_cue", incoming_cue),
        ("outgoing_vocal_outro_start",
         vocal_outro_start(outgoing_analysis) if outgoing_analysis is not None else None),
        ("incoming_vocal_intro_end",
         vocal_intro_end(incoming_analysis) if incoming_analysis is not None else None),
        ("outgoing_structure_anchor", _structure_outgoing_anchor(outgoing_structure)),
        ("incoming_structure_anchor", _structure_incoming_anchor(incoming_structure)),
        ("outgoing_key", outgoing_analysis.key if outgoing_analysis is not None else None),
        ("incoming_key", incoming_analysis.key if incoming_analysis is not None else None),
        ("key_shift_semitones", key_shift or None),
        *metrics,
        *recommendation,
    )
    style_name = STYLE_EQ if band_windows is not None else dsp.value if dsp is not None else "legacy"
    transition = AudioRenderTransition(
        clip_a=previous_clip.clip_id, clip_b=f"automix:{track.id}",
        timeline_start=timeline_start, duration=duration, type=transition_type,
        dsp=dsp, dsp_reasons=(f"* manual: {style_name}", *reasons[1:]), details=details, vocal_handoff=handoff,
        band_windows=band_windows,
        beat_seconds=(60.0 / outgoing_analysis.bpm / rate *
                      (1.0 if dsp is TransitionDsp.BEAT_ROLL else override.echo_beats)
                      if outgoing_analysis is not None and outgoing_analysis.bpm else None),
        echo_feedback=override.echo_feedback, echo_low_cut=override.echo_low_cut, tape_entry=override.tape_entry,
        roll_beats=override.roll_beats, filter_cutoff_hz=override.filter_cutoff_hz,
    )
    if log_diagnostics:
        LOGGER.info("AutoMix transition (manual): %s", describe_transition(
            transition, previous_track.title or previous_track.id, track.title or track.id,
        ))
    return outgoing_clip, timeline_start, incoming_cue, transition


def plan_manual_junction(
    outgoing_clip: AudioRenderClip,
    outgoing_track: PlaylistTrack,
    incoming_track: PlaylistTrack,
    override: TransitionOverride,
    analyses: Mapping[str, TrackAnalysis],
    structures: Mapping[str, TrackStructureAnalysis] | None = None,
    settings: AutoMixTransitionSettings | None = None,
    ramp_floor: float | None = None,
) -> tuple[AudioRenderClip, AudioRenderClip, AudioRenderTransition | None]:
    """One manual junction on its own, exactly as ``compile_automix`` would place it.

    For an editor's live draft: ``outgoing_clip`` is the outgoing clip of the
    current plan (its head is kept; its tail and tempo ramp are re-planned),
    ``ramp_floor`` the outgoing source second before which the window may
    not start (where the previous transition ends; default the clip's
    start). Returns (outgoing clip, incoming clip, transition or None).
    """
    settings = settings or AutoMixTransitionSettings(enabled=True)
    structures = structures or {}
    outgoing_analysis = analyses.get(outgoing_track.id)
    incoming_analysis = analyses.get(incoming_track.id)
    head = replace(outgoing_clip, source_out=max(outgoing_clip.source_in, outgoing_track.duration_seconds),
                   tempo_ramp=None)
    ramp_floor = head.source_in if ramp_floor is None else ramp_floor
    tail = replace(head, source_out=_audible_end(outgoing_analysis, settings, head))
    fallback = (tail, tail.timeline_end, _audible_start(incoming_analysis, settings), None)
    outgoing, start, source_in, transition = _plan_manual(
        head, outgoing_track, incoming_track, override, outgoing_analysis, incoming_analysis,
        structures, settings, ramp_floor, fallback, log_diagnostics=False,
    )
    incoming = AudioRenderClip(
        clip_id=f"automix:{incoming_track.id}", track_id=incoming_track.id,
        timeline_start=start, source_in=source_in, source_out=incoming_track.duration_seconds,
    )
    return outgoing, incoming, transition
