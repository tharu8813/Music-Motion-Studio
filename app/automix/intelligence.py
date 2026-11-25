"""Compare cue, length and EQ handoff together using measured source audio.

This is a deterministic acoustic cost model, not a learned quality score.
The renderer executes the selected envelopes unchanged in preview and export.
"""
from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from math import isclose

import numpy as np

from app.automix.analysis.key import shift_key
from app.automix.analysis.vocals import merge_spans
from app.automix.beatgrid import compare_overlap_beats
from app.automix.candidates import (
    TransitionCandidate, TransitionStrategy, audible_start, incoming_landmarks, key_shift, sung_spans,
)
from app.automix.compatibility import TransitionCompatibility
from app.automix.eq_response import BandGains, UNITY_POWER
from app.automix.models import BAND_POWER_HOP_SECONDS, HANDOFF_LEVEL_BLOCK_SECONDS, RELIABLE_BPM_CONFIDENCE, TrackAnalysis
from app.automix.music import character, music_tags_at
from app.automix.renderer import BAND_ENVELOPES, TAPE_STOP_ENTRY, TAPE_STOP_FADE, echo_taps
from app.automix.structure.models import TrackStructureAnalysis
from app.automix.transition_style import MAX_BEAT_DRIFT_SECONDS, TransitionDspDecision, select_transition_dsp, vocal_map
from app.timeline.render_plan import TransitionDsp

SAMPLES = 96
CANDIDATE_BATCH_SIZE = 128
_PROGRESS = (np.arange(SAMPLES) + 0.5) / SAMPLES
SOLO_LEVEL_DROP_TOLERANCE_DB = 4.0
"""Only penalize a sizeable drop relative to the surrounding solo passages."""
CONTINUITY_TIE_DB = 1.0
CONTINUITY_SCORE_PER_DB = 0.05
VOICE_GUARD_SECONDS = 0.05
BASS_BUILDUP_TOLERANCE_DB = 3.0
MIN_TREBLE_REFINEMENT_GAIN = 0.004
EQ_DIP_TOLERANCE_DB = 2.0
MAX_MID_HANDOFF_EXTENSION_SECONDS = 0.35
MIN_RHYTHM_REFINEMENT_SECONDS = 0.04


def choose_transition(
    candidates: list[TransitionCandidate], compatibility: TransitionCompatibility,
    outgoing: TrackAnalysis, incoming: TrackAnalysis,
    outgoing_structure: TrackStructureAnalysis | None, incoming_structure: TrackStructureAnalysis | None,
    *, previous_style: TransitionDsp | None = None,
    band_gains: tuple[BandGains, BandGains] = (UNITY_POWER, UNITY_POWER), allow_pitch_shift: bool = True,
) -> tuple[TransitionCandidate | None, TransitionDspDecision | None]:
    """Joint selection; without complete measurements keep the existing rules."""
    if not candidates:
        return None, None

    def rate(candidate: TransitionCandidate, *, refine_rhythm: bool = False) -> tuple[TransitionCandidate, TransitionDspDecision]:
        heard = outgoing
        shift = key_shift(outgoing, incoming) if allow_pitch_shift and abs(candidate.outgoing_rate - 1.0) > 1e-9 else None
        if candidate.strategy is TransitionStrategy.BEAT_MATCH and shift:
            heard = replace(outgoing, key=shift_key(outgoing.key, shift))
        selected = select_transition_dsp(candidate, compatibility, heard, incoming,
                                         outgoing_structure, incoming_structure, previous_style=previous_style)
        return optimize_handoff(candidate, selected, heard, incoming, incoming_structure,
                                previous_style=previous_style, band_gains=band_gains, refine_rhythm=refine_rhythm)

    ordinary = [candidate for candidate in candidates if not candidate.refined_duration]
    rated = [rate(candidate) for candidate in ordinary or candidates]
    best, decision = max(rated, key=lambda result: result[0].score)
    if best.strategy is TransitionStrategy.FIXED_CROSSFADE and outgoing.band_power_curve and incoming.band_power_curve:
        # Intermediate lengths are only needed when the best ordinary fade
        # still leaves a sizeable gap. Keep the common path inexpensive.
        weakest = dict(decision.metrics).get("predicted_weakest_block_db")
        if ordinary and weakest is not None and weakest < -SOLO_LEVEL_DROP_TOLERANCE_DB:
            rated.extend(rate(candidate) for candidate in candidates if candidate.refined_duration)
        best, decision = _prefer_smoother_fade(best, decision, rated)
    overlap = dict(decision.metrics).get("predicted_rhythm_overlap_seconds")
    if overlap is not None and overlap >= MIN_RHYTHM_REFINEMENT_SECONDS:
        # Keep the cue and length ranking intact. Only refine the winning envelope.
        _, refined = rate(best, refine_rhythm=True)
        if refined.band_windows != decision.band_windows:
            reasons = (refined.reasons[0], *decision.reasons[1:], "* reduced overlap of misaligned rhythms")
            extra = tuple(item for item in decision.metrics if item[0] == "continuity_tiebreak_db")
            decision = replace(refined, reasons=reasons, metrics=(*refined.metrics, *extra))
    return best, decision


def _prefer_smoother_fade(
    best: TransitionCandidate, decision: TransitionDspDecision,
    rated: list[tuple[TransitionCandidate, TransitionDspDecision]],
) -> tuple[TransitionCandidate, TransitionDspDecision]:
    """A clear continuity gain can decide a close ranking without removing any more music."""
    facts = dict(decision.metrics)
    weakest = facts.get("predicted_weakest_block_db")
    if weakest is None or weakest >= -CONTINUITY_TIE_DB:
        return best, decision
    preferred = best, decision
    best_value = best.score
    for candidate, option in rated:
        if (candidate.strategy is not TransitionStrategy.FIXED_CROSSFADE
                or not isclose(candidate.incoming_source_time, best.incoming_source_time, abs_tol=1e-6, rel_tol=0.0)
                or not isclose(candidate.outgoing_source_out, best.outgoing_source_out, abs_tol=1e-6, rel_tol=0.0)):
            continue
        measured = dict(option.metrics)
        floor = measured.get("predicted_weakest_block_db")
        if floor is None or floor < weakest + CONTINUITY_TIE_DB or any(
                measured[key] > facts[key] + VOICE_GUARD_SECONDS
                for key in ("obscured_vocal_seconds", "voice_collision_seconds")):
            continue
        if measured["predicted_bump_db"] > facts["predicted_bump_db"] + CONTINUITY_TIE_DB:
            continue
        if measured["predicted_low_excess_db"] > max(
                BASS_BUILDUP_TOLERANCE_DB, facts["predicted_low_excess_db"] + CONTINUITY_TIE_DB):
            continue
        # Limit how much of the acoustic ranking the half-second metric can
        # overturn. The original score still governs strong style preferences.
        value = candidate.score + CONTINUITY_SCORE_PER_DB * min(2.0, floor - weakest)
        if value > best_value:
            preferred = candidate, option
            best_value = value
    if preferred[0] is best:
        return preferred
    candidate, option = preferred
    return replace(candidate, score=best_value), replace(
        option, metrics=(*option.metrics, ("continuity_tiebreak_db", weakest)),
        reasons=(*option.reasons, "* preferred a steadier half-second handoff"),
    )


def _features(analysis: TrackAnalysis, times: np.ndarray) -> np.ndarray | None:
    curves = (analysis.rms_curve, analysis.bass_curve, analysis.brightness_curve, analysis.percussive_curve)
    indices = times.astype(int)
    if np.any(times < 0.0) or any(not curve or np.max(indices) >= len(curve) for curve in curves):
        return None
    return np.array([[curve[index] for index in indices] for curve in curves])


def _band_power(analysis: TrackAnalysis, times: np.ndarray, features: np.ndarray | None) -> np.ndarray | None:
    indices = (times / BAND_POWER_HOP_SECONDS).astype(int)
    curve = analysis.band_power_curve
    if curve and len(indices) and 0 <= np.min(indices) and np.max(indices) < len(curve):
        return np.array([curve[index] for index in indices]).T
    if features is None:
        return None
    # Analyses without the finer curve retain the previous timbre estimate.
    bass = features[1]
    high = np.minimum(1 - bass, features[2])
    return np.array([bass, 1 - bass - high, high]) * features[0] ** 2


def _solo_band_power(analysis: TrackAnalysis, times: np.ndarray, gain: np.ndarray) -> np.ndarray:
    available = times[(times >= 0.0) & (times < analysis.duration_seconds)]
    if not len(available):
        return np.empty((3, 0))
    power = _band_power(analysis, available, _features(analysis, available))
    return power * gain if power is not None else np.empty((3, 0))


@lru_cache(maxsize=512)
def _gains(window: tuple[float, float], incoming: bool) -> np.ndarray:
    start, end = window
    phase = np.clip((_PROGRESS - start) / max(1e-6, end - start), 0.0, 1.0) * np.pi / 2
    return np.sin(phase) if incoming else np.cos(phase)


def _singing(analysis: TrackAnalysis, times: np.ndarray) -> np.ndarray:
    return np.array([any(a <= time < b for a, b in sung_spans(analysis)) for time in times], dtype=float)


def optimize_handoff(
    candidate: TransitionCandidate, decision: TransitionDspDecision,
    outgoing: TrackAnalysis, incoming: TrackAnalysis, incoming_structure: TrackStructureAnalysis | None,
    *, previous_style: TransitionDsp | None = None,
    band_gains: tuple[BandGains, BandGains] = (UNITY_POWER, UNITY_POWER), exit_beat: float | None = None,
    refine_rhythm: bool = False,
) -> tuple[TransitionCandidate, TransitionDspDecision]:
    """Compare audible holes, bass/voice collisions and lost words across envelopes.

    ponytail: power/centroid proxy, not waveform audition or a perceptual ML
    judge; use listening ratings to calibrate before claiming human DJ quality.
    """
    duration = candidate.duration_seconds
    if duration <= 0.0:
        return candidate, decision
    phrase_exit = candidate.strategy is TransitionStrategy.PHRASE_EXIT
    out_times = candidate.outgoing_source_time + _PROGRESS * duration * candidate.outgoing_rate
    in_times = candidate.incoming_source_time + _PROGRESS * duration * candidate.incoming_rate
    out, inc = _features(outgoing, out_times), _features(incoming, in_times)
    if out is None or inc is None:
        return candidate, decision

    vocals = vocal_map(candidate, outgoing, incoming)
    unknown = vocals is None
    out_voice, in_voice = _singing(outgoing, out_times), _singing(incoming, in_times)
    facts = dict(decision.metrics)
    clash = bool(facts.get("key_clash")) * min(outgoing.key_confidence, incoming.key_confidence)
    gentle = max(character(music_tags_at(a, float(t[0]), float(t[-1])))[0]
                 for a, t in ((outgoing, out_times), (incoming, in_times))) >= 0.55
    beat = 60.0 / incoming.bpm if incoming.bpm else 0.5
    centers = [0.1, 0.25, 0.5, 0.525, 0.75, 0.9, 0.95]
    if incoming.beat_alignment_quality() == "reliable":
        for target in centers[:]:
            downbeats = [(b - candidate.incoming_source_time) / duration for b in incoming.downbeats
                         if 0.12 <= (b - candidate.incoming_source_time) / duration <= 0.88]
            if downbeats:
                centers.append(min(downbeats, key=lambda p: abs(p - target)))
    for a, b in sung_spans(outgoing):
        point = (b - candidate.outgoing_source_time) / candidate.outgoing_rate / duration
        if 0.12 <= point <= 0.88:
            centers.append(point)
    for a, b in sung_spans(incoming):
        point = (a - candidate.incoming_source_time) / duration
        if 0.12 <= point <= 0.88:
            centers.append(point)
    centers = sorted(set(round(c, 6) for c in centers))
    width = min(0.25, max(0.06, beat / duration))

    def window(center: float) -> tuple[float, float]:
        return max(0.0, center - width / 2), min(1.0, center + width / 2)

    options = []
    measured_bands = bool(outgoing.band_power_curve and incoming.band_power_curve)
    if phrase_exit or decision.dsp is TransitionDsp.DROP_IN:
        options.append((decision.dsp, None))
    search_fades = not phrase_exit and (measured_bands or decision.dsp is not TransitionDsp.DROP_IN)
    if search_fades and (duration < 2.0 or (gentle and not unknown and not clash)):
        options.append((TransitionDsp.SHORT_FADE, None))
    elif search_fades:
        if duration < 4.0 or (not unknown and not clash):
            options.append((TransitionDsp.SHORT_FADE, None))
        allowed = (TransitionDsp.VOCAL_SAFE_EQ,) if unknown or clash or facts.get("vocal_overlap", 0) else tuple(BAND_ENVELOPES)
        for style in allowed:
            default = tuple(BAND_ENVELOPES[style][band] for band in ("low", "mid", "high"))
            if style is TransitionDsp.VOCAL_SAFE_EQ and decision.vocal_handoff is not None:
                mid = (decision.vocal_handoff - 0.125, decision.vocal_handoff + 0.125)
                default = (default[0], (mid, mid), default[2])
            options.append((style, default))
            for low_center in centers:
                for voice_center in centers:
                    low, mid = window(low_center), window(voice_center)
                    options.append((style, ((low, low), (mid, mid), default[2])))

    out_gain, in_gain = (np.asarray(gains)[:, None] for gains in band_gains)
    out_power = _band_power(outgoing, out_times, out) * out_gain
    in_power = _band_power(incoming, in_times, inc) * in_gain
    adjusted_bass = np.minimum(out_power[0] / np.maximum(1e-10, out_power.sum(axis=0)),
                               in_power[0] / np.maximum(1e-10, in_power.sum(axis=0)))
    # Compare against the solo music immediately after the transition. An
    # intro whose drums arrive at the END of the blend must not be mistaken
    # for the quiet level the next track will keep playing at.
    after_times = np.minimum(incoming.duration_seconds - 1e-6,
                             candidate.incoming_source_time + (duration + (np.arange(6) + 0.5) * beat / 3)
                             * candidate.incoming_rate)
    after = _band_power(incoming, after_times, _features(incoming, after_times))
    after_power = after * in_gain if after is not None else in_power[:, -6:]
    def reference_to(out_level, in_level):
        return np.exp((1 - _PROGRESS) * np.log(max(1e-10, float(out_level)))
                      + _PROGRESS * np.log(max(1e-10, float(in_level))))
    first_slice = out_power[:, :SAMPLES // 8]
    outgoing_level = max(1e-10, float(np.mean(np.sum(first_slice, axis=0))))
    incoming_level = max(1e-10, float(np.mean(np.sum(after_power, axis=0))))
    reference = reference_to(outgoing_level, incoming_level)
    outgoing_bass = max(1e-10, float(np.mean(first_slice[0])))
    incoming_bass = max(1e-10, float(np.mean(after_power[0])))
    bass_reference = reference_to(outgoing_bass, incoming_bass)

    # Compare against the surrounding solos as well as the fade's endpoints.
    # A cue moved into an already quiet tail must not make the hole look normal.
    context_hop = BAND_POWER_HOP_SECONDS if measured_bands else 0.5
    context_offsets = (np.arange(round(5.0 / context_hop)) + 0.5) * context_hop
    solo_levels = []
    solo_bass = []
    for analysis, times, gain in (
        (outgoing, candidate.outgoing_source_time - context_offsets * candidate.outgoing_rate, out_gain),
        (incoming, candidate.incoming_source_time + (duration + context_offsets) * candidate.incoming_rate, in_gain),
    ):
        power = _solo_band_power(analysis, times, gain)
        solo_levels.extend(power.sum(axis=0))
        solo_bass.extend(power[0])
    continuity_reference = max(1e-10, float(np.mean(solo_levels)) if solo_levels else (outgoing_level + incoming_level) / 2)
    solo_bass_reference = max(1e-10, float(np.mean(solo_bass)) if solo_bass else (outgoing_bass + incoming_bass) / 2)
    block_samples = max(1, round(SAMPLES * HANDOFF_LEVEL_BLOCK_SECONDS / duration))
    edge_power = (np.empty(0), np.empty(0))
    if block_samples > SAMPLES:
        # A click-free 50 ms cut can land on a loud transient followed by a
        # gap. Compare the same half-second of audio as for longer fades.
        lead = (block_samples - SAMPLES) // 2
        edge_power = tuple(
            _solo_band_power(analysis, times, gain).sum(axis=0)
            for analysis, times, gain in (
                (outgoing, candidate.outgoing_source_time - (np.arange(lead)[::-1] + 0.5)
                 * duration / SAMPLES * candidate.outgoing_rate, out_gain),
                (incoming, candidate.incoming_source_time + (duration + (np.arange(block_samples - SAMPLES - lead) + 0.5)
                 * duration / SAMPLES) * candidate.incoming_rate, in_gain),
            )
        )
        block_samples = SAMPLES + sum(len(edge) for edge in edge_power)
    block_weights = np.full(block_samples, 1 / block_samples)

    overlap_beats = None
    if not phrase_exit and min(outgoing.bpm_confidence, incoming.bpm_confidence) >= RELIABLE_BPM_CONFIDENCE:
        overlap_beats = compare_overlap_beats(
            outgoing.beats, incoming.beats, duration, cues=(candidate.outgoing_source_time, candidate.incoming_source_time),
            rates=(candidate.outgoing_rate, candidate.incoming_rate),
        )
    beat_offset_ms = None
    drift: float | np.ndarray = 0.0
    if overlap_beats is not None:
        positions, offsets, period = overlap_beats
        beat_offset_ms = float(np.quantile(offsets, 0.9) * 1000)
        drift = np.interp(_PROGRESS * duration, positions, offsets)
        drift = np.clip((drift - MAX_BEAT_DRIFT_SECONDS) / max(period / 2 - MAX_BEAT_DRIFT_SECONDS, 0.05), 0, 1)
    elif candidate.strategy is not TransitionStrategy.BEAT_MATCH and outgoing.bpm and incoming.bpm:
        ratio = incoming.bpm / outgoing.bpm
        effective = min((ratio, ratio / 2, ratio * 2), key=lambda r: abs(r - 1.0))
        drift = min(1.0, duration * outgoing.bpm / 60 * abs(effective - 1.0))

    def envelopes(style, windows):
        echo_power = None
        if windows is None:
            go = np.tile(_gains((0.0, 1.0), False), (3, 1))
            gi = np.ones_like(go) if style is TransitionDsp.DROP_IN else np.tile(_gains((0.0, 1.0), True), (3, 1))
            if style is TransitionDsp.TAPE_STOP:
                go = np.tile(_gains(TAPE_STOP_FADE, False), (3, 1))
                gi = np.tile(_PROGRESS >= TAPE_STOP_ENTRY, (3, 1))
            elif style is TransitionDsp.ECHO_OUT and exit_beat is not None:
                echo_beat = min(2.0, max(0.1, exit_beat))
                feed = min(echo_beat / duration, 0.5)
                go = np.tile(_gains((feed, min(1.0, feed + echo_beat / duration / 2)), False), (3, 1))
                gi = np.ones_like(go)
                echo_power = np.zeros_like(out_power)
                for delay, level in echo_taps(duration, echo_beat):
                    position = _PROGRESS - delay / duration
                    echo_power[1:] += [np.interp(position, _PROGRESS, band) * (level ** 2)
                                       * ((0 <= position) & (position < feed)) for band in out_power[1:]]
                echo_power *= _gains((1.0 - feed, 1.0), False) ** 2
        else:
            go = np.array([_gains(w[0], False) for w in windows])
            gi = np.array([_gains(w[1], True) for w in windows])
        return go, gi, echo_power

    def score_options(variants):
        """Evaluate small batches on (candidate, band, time) to bound temporary arrays."""
        for offset in range(0, len(variants), CANDIDATE_BATCH_SIZE):
            batch = variants[offset:offset + CANDIDATE_BATCH_SIZE]
            left, right, echoes = zip(*(envelopes(style, windows) for style, windows in batch), strict=True)
            go, gi = np.stack(left), np.stack(right)
            bands = out_power * go ** 2 + in_power * gi ** 2
            for index, echo_power in enumerate(echoes):
                if echo_power is not None:
                    bands[index] += echo_power
            power = bands.sum(axis=1)
            delta = 10 * np.log10(np.maximum(power, 1e-10) / reference)
            holes = np.maximum(0.0, -delta - 1.5).mean(axis=1)
            bumps = np.maximum(0.0, delta - 2.0).mean(axis=1)
            out_mid, in_mid = go[:, 1], gi[:, 1]
            out_low, in_low = go[:, 0], gi[:, 0]
            lost = (out_voice * (1 - out_mid) ** 2 + in_voice * (1 - in_mid) ** 2).mean(axis=1) * duration
            voice_collision = (out_voice * in_voice * out_mid * in_mid).mean(axis=1) * duration
            bass_collision = (adjusted_bass * out[3] * inc[3] * out_low * in_low).mean(axis=1) * duration
            bass_level = out_power[0] * out_low ** 2 + in_power[0] * in_low ** 2
            bass_hole = np.maximum(0.0, -10 * np.log10(np.maximum(bass_level, 1e-10) / bass_reference) - 3).mean(axis=1)
            if block_samples > SAMPLES:
                head, tail = (np.broadcast_to(edge, (len(batch), len(edge))) for edge in edge_power)
                power = np.concatenate((head, power, tail), axis=1)
            weakest = np.array([np.min(np.convolve(row, block_weights, mode="valid")) for row in power])
            continuity_db = 10 * np.log10(np.maximum(weakest, 1e-10) / continuity_reference)
            mid_overlap = out_mid * in_mid
            harmony = mid_overlap.mean(axis=1) * clash * duration
            rhythm_overlap = np.maximum(mid_overlap, out_low * in_low) if overlap_beats is not None else mid_overlap
            rhythm = duration * (drift * out[3] * inc[3] * rhythm_overlap).mean(axis=1)
            low_delta_db = 10 * np.log10(np.maximum(1e-10, bass_level.mean(axis=1)) / solo_bass_reference)
            cost = (0.055 * holes + 0.04 * bumps + 0.06 * lost + 0.15 * voice_collision
                    + 0.035 * bass_collision + 0.035 * harmony + 0.025 * bass_hole + 0.06 * rhythm
                    + 0.025 * np.maximum(0.0, -continuity_db - SOLO_LEVEL_DROP_TOLERANCE_DB))
            if measured_bands:
                cost += 0.04 * np.maximum(0.0, low_delta_db - BASS_BUILDUP_TOLERANCE_DB)
            if phrase_exit:
                cost += 0.015 * float(facts.get("skipped_seconds", 0.0))
            if unknown:
                # Keep the voice handoff near the middle when singing is unknown.
                cost += 0.004 * duration
            for index, (style, windows) in enumerate(batch):
                score = cost[index]
                if unknown and windows is not None:
                    score += 0.02 * abs(sum(windows[1][0]) / 2 - 0.525)
                if style is previous_style:
                    score += 0.008
                yield ((score, holes[index], bumps[index], lost[index], voice_collision[index],
                        bass_collision[index], rhythm[index], continuity_db[index], low_delta_db[index]), style, windows)

    scored = []
    if measured_bands and decision.dsp is TransitionDsp.DROP_IN and not phrase_exit:
        result = next(score_options(options[:1]))
        drop_in = result[0]
        scored.append(result)
        floor, bass_excess = drop_in[-2:]
        if floor >= -SOLO_LEVEL_DROP_TOLERANCE_DB and bass_excess <= BASS_BUILDUP_TOLERANCE_DB:
            options = []
        else:
            options = options[1:]
    scored.extend(score_options(options))
    if measured_bands:
        # Refine only the best EQ shapes. Giving the high band its own timing
        # keeps transients audible through quiet openings without adding a
        # third dimension to the full bass/mid search.
        finalists = sorted((row for row in scored if row[2] is not None), key=lambda row: row[0][0])[:3]
        tried = set(options)
        thresholds = {}
        for original, style, windows in finalists:
            for high in (windows[1], ((0.25, 0.85), (0.0, 0.65)), ((0.15, 0.75), (0.15, 0.75))):
                shaped = (*windows[:2], high)
                if (style, shaped) in tried:
                    continue
                tried.add((style, shaped))
                thresholds[style, shaped] = original[0]
        for result in score_options(list(thresholds)):
            refined, style, shaped = result
            if refined[0] + MIN_TREBLE_REFINEMENT_GAIN < thresholds[style, shaped]:
                scored.append(result)
    metrics, style, windows = min(scored, key=lambda row: row[0][0])
    selection_cost = metrics[0]
    eq_dip_db = None
    rhythm_exposure = None
    if (windows is not None and measured_bands and vocals is not None and not vocals.overlap_ratio
            and block_samples <= SAMPLES):
        # The comparison keeps the bass envelope and cues. Ignore reference
        # blocks more than 12 dB quieter than the neighboring solo sections.
        full = (0.0, 1.0)
        low, mid, high = windows

        def band_mix(band, pair):
            return out_power[band] * _gains(pair[0], False) ** 2 + in_power[band] * _gains(pair[1], True) ** 2

        bass_power = band_mix(0, low)
        control = np.convolve(bass_power + band_mix(1, (full, full)) + band_mix(2, (full, full)),
                              block_weights, mode="valid")
        audible = control >= continuity_reference * 10 ** (-12 / 10)

        def extra_dip(mid_window, high_window):
            mix = bass_power + band_mix(1, mid_window) + band_mix(2, high_window)
            blocks = np.convolve(mix, block_weights, mode="valid")
            return float(max(0.0, 10 * np.log10(np.max(control[audible] / np.maximum(blocks[audible], 1e-10)))))

        if np.any(audible):
            eq_dip_db = extra_dip(mid, high)
        if eq_dip_db is not None and eq_dip_db > EQ_DIP_TOLERANCE_DB and mid[0] == mid[1]:
            mid_start, mid_end = mid[0]
            mid_width = mid_end - mid_start
            nearby = sorted({*centers, (mid_start + mid_end) / 2})
            alternatives = []
            for width in (mid_width, min(1.0, mid_width + MAX_MID_HANDOFF_EXTENSION_SECONDS / duration)):
                for center in nearby:
                    start = min(max(center - width / 2, 0.0), 1.0 - width)
                    shifted = ((start, start + width),) * 2
                    dip = extra_dip(shifted, high)
                    if dip <= eq_dip_db - 1.0:
                        alternatives.append((style, (low, shifted, high)))
            _, _, bumps, lost, voices, _, rhythm, floor, _ = metrics
            # Keep the original cue/length ranking and guard the existing
            # continuity, voice, rhythm and gain constraints during refinement.
            for alternate, _, adjusted in score_options(alternatives):
                _, _, other_bumps, other_lost, other_voices, _, other_rhythm, other_floor, _ = alternate
                if (other_floor < floor - 0.1 or other_bumps > bumps + 0.1 or other_lost > lost + 1e-9
                        or other_voices > voices + 1e-9 or other_rhythm > rhythm + 0.005):
                    continue
                dip = extra_dip(*adjusted[1:])
                if dip < eq_dip_db:
                    eq_dip_db, metrics, windows = dip, alternate, adjusted
        if eq_dip_db is not None and overlap_beats is not None:
            # Weight each fade by the measured energy it lets through. The
            # bass and mid can swap cleanly while percussion above them still overlaps.
            out_shares, in_shares = (power / np.maximum(power.sum(axis=0), 1e-10) for power in (out_power, in_power))
            drift_weight = duration * drift * out[3] * inc[3] / SAMPLES

            def exposed_rhythm(shape):
                left, right, _ = envelopes(style, shape)
                overlap = np.sqrt((out_shares * left ** 2).sum(axis=0) * (in_shares * right ** 2).sum(axis=0))
                return float(np.dot(overlap, drift_weight))

            rhythm_exposure = exposed_rhythm(windows) if np.any(drift) else 0.0
            if refine_rhythm:
                limit = rhythm_exposure - MIN_RHYTHM_REFINEMENT_SECONDS
                _, _, bumps, lost, voices, _, _, floor, _ = metrics
                allowed_dip = eq_dip_db + 0.25
                mid_overlap = np.dot(_gains(windows[1][0], False), _gains(windows[1][1], True))
                for alternate, other_style, shape in scored:
                    _, _, other_bumps, other_lost, other_voices, _, _, other_floor, _ = alternate
                    if (other_style is not style or shape is None or shape[0] != low or other_floor < floor - 0.1
                            or other_bumps > bumps + 0.25 or other_lost > lost + 1e-9 or other_voices > voices + 1e-9):
                        continue
                    if clash and np.dot(_gains(shape[1][0], False), _gains(shape[1][1], True)) > mid_overlap + 0.1:
                        continue
                    exposure = exposed_rhythm(shape)
                    if exposure > limit:
                        continue
                    dip = extra_dip(*shape[1:])
                    if dip <= allowed_dip:
                        limit, rhythm_exposure, eq_dip_db, metrics, windows = exposure, exposure, dip, alternate, shape
    _, holes, bumps, lost, voices, bass, rhythm, continuity_db, low_delta_db = metrics
    landing = candidate.incoming_source_time + duration
    landmarks = incoming_landmarks(incoming, incoming_structure)
    distance = min((abs(landing - point) for point in landmarks), default=None)
    landing_bonus = 0.10 * max(0.0, 1 - distance / max(beat, 0.1)) if distance is not None else 0.0
    trim = max(0.0, candidate.incoming_source_time - audible_start(incoming))
    skipped_words = sum(max(0.0, min(b, candidate.incoming_source_time) - max(a, audible_start(incoming)))
                        for a, b in merge_spans(list(sung_spans(incoming)), incoming.duration_seconds))
    # A head trim is paid for even below the old 30-second soft limit.
    score = candidate.score - selection_cost + landing_bonus - 0.015 * trim - 0.10 * skipped_words
    reason = (f"* acoustic search: {style.value}, predicted hole {holes:.2f}dB, "
              f"voice collision {voices:.2f}s; "
              + ("band-by-band handoff" if windows else "full-band handoff"))
    handoff = sum(windows[1][0]) / 2 if windows is not None and style is TransitionDsp.VOCAL_SAFE_EQ else None
    decision = replace(decision, dsp=style, band_windows=windows, vocal_handoff=handoff,
                       reasons=(reason, *(decision.reasons if phrase_exit else decision.reasons[1:])), metrics=(
                       *((name, handoff if name == "vocal_handoff" else value) for name, value in decision.metrics),
                       ("acoustic_cost", selection_cost), ("predicted_hole_db", holes), ("predicted_bump_db", bumps),
                       ("obscured_vocal_seconds", lost), ("voice_collision_seconds", voices),
                       ("skipped_vocal_seconds", skipped_words),
                       ("source_level_delta_db", float(10 * np.log10(incoming_level / outgoing_level))),
                       ("source_bass_delta_db", float(10 * np.log10(incoming_bass / outgoing_bass))),
                       ("predicted_weakest_block_db", float(continuity_db)),
                       ("predicted_mid_high_dip_db", eq_dip_db),
                       ("predicted_low_excess_db", float(low_delta_db)),
                       ("overlap_beat_offset_ms", beat_offset_ms),
                       ("predicted_rhythm_overlap_seconds", rhythm_exposure),
                       ("rhythm_collision_cost", rhythm),
                       ("bass_collision_cost", bass), ("landing_distance_seconds", distance)))
    return replace(candidate, score=score, reasons=(*candidate.reasons, reason)), decision
