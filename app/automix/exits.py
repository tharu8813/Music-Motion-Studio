"""Phrase exits: how to leave a track the next one cannot be beat-matched with.

Two tracks too far apart in tempo cannot share their beats, and a plain
3-second fade at the very end is the same move every time. A DJ instead
leaves the outgoing track on a phrase boundary with a move that needs no
shared tempo, chosen by what the music is doing there:

- ECHO_OUT: the track plays to its last downbeat; that final
  hit sounds and then only its echo rings on, under the next track's first
  downbeat -- the echo comes in as the song ends, not a phrase before it.
- DOWNBEAT_CUT: both sides are drum-driven at about the same energy -- cut
  straight from one downbeat to the other and keep the momentum.
- TAPE_STOP: the energy drops a long way into the next track -- a turntable
  brake marks the change instead of blurring it.

A track whose own ending is natural (a final chord ringing out for
NATURAL_ENDING_SECONDS after its decay starts) keeps it: the regular
fade/drop-in plays that ending. Effects require measured, unsung audio in
any skipped source range and compete with the regular fade in the planner.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from app.automix.beatgrid import fit_beat_grid
from app.automix.candidates import (
    TransitionCandidate,
    TransitionStrategy,
    audible_end,
    audible_start,
    sung_spans,
    vocals_measured,
)
from app.automix.models import TrackAnalysis
from app.automix.music import character, music_tags_at
from app.automix.phrases import in_peak, phrase_starts
from app.automix.renderer import TAPE_STOP_ENTRY
from app.automix.structure.models import TrackStructureAnalysis
from app.automix.structure.timbre import mean_over
from app.automix.transition_style import TransitionDspDecision
from app.timeline.render_plan import TransitionDsp

NATURAL_ENDING_SECONDS = (2.5, 8.0)
"""A decay this long after the track's last full-level moment is a natural ending
(a chord ringing out) and is kept; shorter is an abrupt stop, longer a radio fade."""
MAX_SKIPPED_SECONDS = 12.0
"""At most this much of the outgoing track's end (not counting a radio fade-out) is
left out to reach a phrase boundary: a playlist is listened to whole, so the planner
otherwise keeps every audible ending."""
ECHO_BARS = 2
ECHO_MAX_SECONDS = 6.0
ECHO_MIN_BEATS = 4
"""An echo out needs at least this many outgoing beats of file after its downbeat."""
CUT_SECONDS = 0.05
TAPE_STOP_SECONDS = (1.2, 2.5)
"""One bar of the outgoing track, within these bounds."""
DRUMS = 0.5
"""Percussive level (timbre curve) from which a stretch is drum-driven (real pop
tails measure 0.4-0.65, dense grooves 1.0, acoustic outros ~0)."""
ENERGY_HOLD = 0.2
ENERGY_DROP = 0.35
SUNG_MARGIN_SECONDS = 0.2
LOOKAROUND_SECONDS = 4.0


@dataclass(frozen=True, slots=True)
class PhraseExit:
    candidate: TransitionCandidate
    decision: TransitionDspDecision
    beat_seconds: float
    """ECHO_OUT's delay: one outgoing beat on the timeline."""


def phrase_exits(
    outgoing: TrackAnalysis, incoming: TrackAnalysis,
    outgoing_structure: TrackStructureAnalysis | None, incoming_structure: TrackStructureAnalysis | None,
    *, outgoing_rate: float = 1.0, earliest: float = 0.0, previous_style: TransitionDsp | None = None,
    allow_tape_stop: bool = True,
) -> Iterator[PhraseExit]:
    """Effects with enough measured source audio to protect the head and tail.

    ``outgoing_rate``: the rate the outgoing clip already plays at;
    ``earliest``: the outgoing source second the window may not start before
    (the previous transition's end); ``previous_style``: the junction before
    this one, so the same move is not made twice in a row when another fits.
    """
    if outgoing.bpm is None or outgoing.beat_alignment_quality() != "reliable" or not outgoing.downbeats:
        return
    end = audible_end(outgoing)
    incoming_start = audible_start(incoming)
    a = character(music_tags_at(outgoing, max(audible_start(outgoing), end - 20.0), end))
    b = character(music_tags_at(incoming, incoming_start, min(audible_end(incoming), incoming_start + 20.0)))
    if max(a[0], b[0]) > max(a[1], b[1]):
        return  # gentle music keeps the natural fade instead of a cut, echo or turntable brake
    decay = outgoing.decay_start_seconds
    natural_min, natural_max = NATURAL_ENDING_SECONDS
    if decay is not None and natural_min <= end - decay <= natural_max:
        return  # a natural ending: let it ring out (drop-in/fade)
    limit = decay if decay is not None and end - decay > natural_max else end
    grid = fit_beat_grid(outgoing.beats, limit - 60.0, limit)
    beat = grid.period if grid is not None else 60.0 / outgoing.bpm
    bar = beat * (outgoing.meter_numerator or 4)

    incoming_cue = _incoming_cue(incoming)
    styles = _styles(outgoing, incoming, outgoing_structure, incoming_structure, incoming_cue, limit, previous_style)
    for style, rule in styles:
        if style is TransitionDsp.ECHO_OUT and previous_style is TransitionDsp.ECHO_OUT:
            continue
        if style is TransitionDsp.TAPE_STOP and not allow_tape_stop:
            continue
        duration = _window(style, bar, beat)
        if style is TransitionDsp.ECHO_OUT:
            # Not a phrase early: the echo takes the song's own last hit, as it ends.
            ending = _echo_at_end(outgoing, limit, beat, bar, outgoing_rate, earliest)
            if ending is None:
                continue
            exit_point, duration = ending
            rule = "no shared tempo: the last downbeat echoes out as the song ends"
        else:
            # An automatic cut can tidy the last bar without jumping over a
            # clearly audible ending to reach an earlier phrase boundary.
            floor = max(earliest, end - bar - 1e-6) if style is TransitionDsp.DOWNBEAT_CUT else earliest
            exit_point = _exit_point(outgoing, outgoing_structure, limit, duration * outgoing_rate, bar, floor)
        if exit_point is None:
            continue
        dry_end = (exit_point + min(beat, duration * outgoing_rate / 2)
                   if style is TransitionDsp.ECHO_OUT else exit_point)
        if not _safe_to_skip(outgoing, dry_end, end):
            continue
        drums = _timbre(outgoing_structure, "percussive_curve", exit_point, min(end, exit_point + bar))
        if drums is None or drums < DRUMS:
            continue
        source_in = incoming_cue
        entry = source_in
        if style is TransitionDsp.TAPE_STOP:
            # The incoming downbeat lands where it enters (TAPE_STOP_ENTRY of the window).
            source_in = max(audible_start(incoming), incoming_cue - TAPE_STOP_ENTRY * duration)
            entry = source_in + TAPE_STOP_ENTRY * duration
        if source_in + duration > incoming.duration_seconds or not _safe_to_skip(incoming, incoming_start, entry):
            continue
        candidate = TransitionCandidate(
            from_track_id=outgoing.track_id, to_track_id=incoming.track_id,
            outgoing_source_time=exit_point, outgoing_source_out=exit_point + duration * outgoing_rate,
            incoming_source_time=source_in, bars=0, duration_seconds=duration,
            outgoing_bpm=outgoing.bpm, incoming_bpm=incoming.bpm, target_bpm=None,
            outgoing_rate=outgoing_rate, incoming_rate=1.0, score=0.3, confidence=outgoing.meter_confidence,
            strategy=TransitionStrategy.PHRASE_EXIT,
            reasons=(f"+ leaves on the downbeat at {exit_point:.1f}s "
                     f"({max(0.0, end - exit_point):.1f}s before the end)",),
        )
        metrics = (("exit_point", exit_point), ("skipped_seconds", max(0.0, end - dry_end)),
                   ("outgoing_energy", _energy(outgoing_structure, limit - LOOKAROUND_SECONDS)),
                   ("incoming_energy", _energy(incoming_structure, incoming_cue + LOOKAROUND_SECONDS)),
                   ("outgoing_percussive", _timbre(outgoing_structure, "percussive_curve",
                                                   exit_point - LOOKAROUND_SECONDS, exit_point)),
                   ("incoming_percussive", _timbre(incoming_structure, "percussive_curve",
                                                   incoming_cue, incoming_cue + LOOKAROUND_SECONDS)))
        yield PhraseExit(candidate, TransitionDspDecision(style, (f"* {style.value}: {rule}",), metrics),
                         beat / outgoing_rate)


def _safe_to_skip(analysis: TrackAnalysis, start: float, end: float) -> bool:
    if end <= start + 1e-6:
        return True
    return vocals_measured(analysis, start, end) and not any(a < end and b > start for a, b in sung_spans(analysis))


def _incoming_cue(incoming: TrackAnalysis) -> float:
    """The incoming track's first downbeat (else beat) after its first sound."""
    start = audible_start(incoming)
    anchors = incoming.downbeats if incoming.beat_alignment_quality() == "reliable" else incoming.beats
    return next((anchor for anchor in anchors if anchor >= start - 0.05), start)


def _window(style: TransitionDsp, bar: float, beat: float) -> float:
    if style is TransitionDsp.DOWNBEAT_CUT:
        return CUT_SECONDS
    if style is TransitionDsp.TAPE_STOP:
        return min(max(bar, TAPE_STOP_SECONDS[0]), TAPE_STOP_SECONDS[1])
    return max(2 * beat, min(ECHO_BARS * bar, ECHO_MAX_SECONDS))


def _echo_at_end(
    outgoing: TrackAnalysis, limit: float, beat: float, bar: float, rate: float, earliest: float,
) -> tuple[float, float] | None:
    """(last downbeat, window seconds) for an echo out as the track ends.

    The window starts on the last downbeat whose beat still sounds before
    ``limit``: that hit plays through, then only its echo is left. The window
    runs on into the file's own tail (the dry track is gone by then) for up to
    ECHO_BARS bars, and needs room for at least ECHO_MIN_BEATS repeats' worth.
    """
    for downbeat in reversed(outgoing.downbeats):
        if downbeat < earliest:
            return None
        room = (outgoing.duration_seconds - downbeat) / rate
        if downbeat + beat <= limit + 1e-6 and room >= ECHO_MIN_BEATS * beat / rate - 1e-6:
            return downbeat, min(ECHO_BARS * bar / rate, ECHO_MAX_SECONDS, room)
    return None


def _exit_point(
    outgoing: TrackAnalysis, structure: TrackStructureAnalysis | None,
    limit: float, source_span: float, bar: float, earliest: float,
) -> float | None:
    """The latest phrase start (else downbeat) that leaves the window before ``limit``
    and (unless ``over_vocals``) does not cut a sung line; prefers one outside a peak section."""
    latest = limit - source_span
    floor = max(earliest, limit - MAX_SKIPPED_SECONDS)
    sung = sung_spans(outgoing)

    def clean(point: float) -> bool:
        return floor <= point <= latest and not any(
            a < point - SUNG_MARGIN_SECONDS < b for a, b in sung)

    phrases = [point for point in phrase_starts(outgoing, structure) if clean(point)]
    if phrases:
        calm = [point for point in phrases if in_peak(structure, point) is False]
        return max(calm) if calm and max(phrases) - max(calm) <= 2 * bar * 8 else max(phrases)
    downbeats = [point for point in outgoing.downbeats if clean(point) and point >= latest - 4 * bar]
    return max(downbeats) if downbeats else None


def _timbre(structure: TrackStructureAnalysis | None, curve: str, start: float, end: float) -> float | None:
    return mean_over(getattr(structure, curve), start, end) if structure is not None else None


def _energy(structure: TrackStructureAnalysis | None, seconds: float) -> float | None:
    return structure.energy_at(seconds) if structure is not None else None


def _styles(
    outgoing: TrackAnalysis, incoming: TrackAnalysis,
    outgoing_structure: TrackStructureAnalysis | None, incoming_structure: TrackStructureAnalysis | None,
    incoming_cue: float, limit: float, previous_style: TransitionDsp | None,
) -> list[tuple[TransitionDsp, str]]:
    """Styles to try, best first, each with the rule that chose it."""
    out_drums = _timbre(outgoing_structure, "percussive_curve", limit - 16.0, limit)
    in_drums = _timbre(incoming_structure, "percussive_curve", incoming_cue, incoming_cue + LOOKAROUND_SECONDS * 2)
    out_energy = _energy(outgoing_structure, limit - LOOKAROUND_SECONDS)
    in_energy = _energy(incoming_structure, incoming_cue + LOOKAROUND_SECONDS)
    delta = None if out_energy is None or in_energy is None else out_energy - in_energy

    options: list[tuple[TransitionDsp, str]] = []
    if delta is not None and delta >= ENERGY_DROP:
        options.append((TransitionDsp.TAPE_STOP, f"energy drops {delta:.2f} into the next track"))
    if (out_drums is not None and in_drums is not None and min(out_drums, in_drums) >= DRUMS
            and delta is not None and abs(delta) < ENERGY_HOLD):
        options.append((TransitionDsp.DOWNBEAT_CUT, f"drums on both sides (out {out_drums:.2f}, in {in_drums:.2f}) "
                                                    f"at the same energy: keep the momentum"))
    options.append((TransitionDsp.ECHO_OUT, "no shared tempo: echo the last beat out"))
    if previous_style is not None and len(options) > 1 and options[0][0] is previous_style:
        # Playlist flow: the same move twice in a row when another fits is monotonous.
        options.append(options.pop(0))
    return options
