"""Musical outcomes of measured cue/EQ search, including real rendering."""
from __future__ import annotations

import os
import subprocess
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from app.automix.analysis.basic import BasicAnalysisProvider, SAMPLE_RATE
from app.automix.candidates import TransitionStrategy, generate_candidates, incoming_landmarks
from app.automix.eq_response import eq_power_response
from app.automix.compatibility import evaluate_compatibility
from app.automix.intelligence import _prefer_smoother_fade, choose_transition, optimize_handoff
from app.automix.models import TrackAnalysis
from app.automix.overrides import TransitionOverride
from app.automix.planner import compile_automix
from app.automix.renderer import AutoMixAudioPipeline, BAND_ENVELOPES, band_windows_of, build_filter_graph
from app.automix.structure.timbre import band_power_curve
from app.automix.transition_style import TransitionDspDecision
from app.models.playlist import track_audio_filter, track_eq_filters
from app.timeline.render_plan import AudioRenderClip, AudioRenderPlan, TempoRamp, TransitionDsp
from app.timeline.models import TransitionType
from tests.test_automix_planner import ENABLED, _analysis, _track
from tests.test_automix_transition_style import _candidate


def _profile(track_id: str, **fields) -> TrackAnalysis:
    return replace(_analysis(track_id, 120.0, 60.0), rms_curve=(0.2,) * 60,
                   bass_curve=(0.7,) * 60, brightness_curve=(0.1,) * 60,
                   percussive_curve=(0.9,) * 60, vocal_coverage=((0.0, 60.0),), **fields)


def _plan(outgoing, incoming, settings=ENABLED):
    return compile_automix([_track("a", 60.0), _track("b", 60.0)],
                           {"a": outgoing, "b": incoming}, settings, log_diagnostics=False)


class AcousticPlanningTests(unittest.TestCase):
    def test_batch_size_does_not_change_the_chosen_handoff_or_diagnostics(self):
        a = _profile("a", band_power_curve=((0.025, 0.01, 0.005),) * 600)
        b = replace(_profile("b", band_power_curve=((0.001, 0.004, 0.005),) * 60
                            + ((0.025, 0.01, 0.005),) * 540), bpm=140.0, vocal_coverage=())
        expected = _plan(a, b)
        with patch("app.automix.intelligence.CANDIDATE_BATCH_SIZE", 1):
            actual = _plan(a, b)
        self.assertEqual(actual, expected)

    def test_close_rank_prefers_smoother_fade_without_trading_voice_or_bass_protection(self):
        long = _candidate(TransitionStrategy.FIXED_CROSSFADE, duration=8.0, score=0.395)
        short = replace(long, outgoing_source_time=106.0, duration_seconds=2.0, score=0.389)
        baseline = TransitionDspDecision(TransitionDsp.VOCAL_SAFE_EQ, (), (
            ("predicted_weakest_block_db", -19.0), ("obscured_vocal_seconds", 0.0), ("voice_collision_seconds", 0.0),
            ("predicted_bump_db", 0.0), ("predicted_low_excess_db", 0.0),
        ))
        balanced = replace(baseline, metrics=(("predicted_weakest_block_db", -17.0), *baseline.metrics[1:]))
        picked, decision = _prefer_smoother_fade(long, baseline, [(long, baseline), (short, balanced)])
        self.assertEqual(picked.duration_seconds, 2.0)
        self.assertEqual(dict(decision.metrics)["continuity_tiebreak_db"], -19.0)
        for key, value in (("obscured_vocal_seconds", 0.2), ("voice_collision_seconds", 0.2),
                           ("predicted_low_excess_db", 4.0), ("predicted_bump_db", 1.5)):
            with self.subTest(key=key):
                worse = replace(balanced, metrics=tuple({**dict(balanced.metrics), key: value}.items()))
                self.assertEqual(_prefer_smoother_fade(long, baseline, [(short, worse)])[0], long)
        trimmed = replace(short, incoming_source_time=1.0)
        self.assertEqual(_prefer_smoother_fade(long, baseline, [(trimmed, balanced)])[0], long)

    def test_measured_high_band_can_enter_earlier_without_extending_voice_overlap(self):
        a = replace(_profile("a", band_power_curve=((0.004, 0.004, 0.03),) * 565
                             + ((0.004, 0.004, 0.0001),) * 35), vocal_coverage=())
        b = replace(_profile("b", band_power_curve=((0.004, 0.004, 0.004),) * 600), vocal_coverage=())
        candidate = _candidate(TransitionStrategy.FIXED_CROSSFADE, duration=4.0,
                               outgoing_source_time=56.0, outgoing_source_out=60.0)
        _, decision = optimize_handoff(candidate, TransitionDspDecision(TransitionDsp.VOCAL_SAFE_EQ, (), ()), a, b, None)
        self.assertIs(decision.dsp, TransitionDsp.VOCAL_SAFE_EQ)
        mid, high = decision.band_windows[1:]
        self.assertLess(high[1][1], BAND_ENVELOPES[decision.dsp]["high"][1][1])
        self.assertEqual(mid[0], mid[1])

    def test_mid_handoff_reduces_fade_dip_without_changing_cues_or_other_bands(self):
        full, quiet_mid = (0.003, 0.04, 0.002), (0.003, 0.0001, 0.002)
        a = _profile("a", band_power_curve=(full,) * 575 + (quiet_mid,) * 25, key_confidence=0.9)
        b = _profile("b", band_power_curve=(quiet_mid,) * 15 + (full,) * 585, key_confidence=0.9)
        candidate = _candidate(TransitionStrategy.FIXED_CROSSFADE, duration=4.0,
                               outgoing_source_time=56.0, outgoing_source_out=60.0)
        decision = TransitionDspDecision(TransitionDsp.VOCAL_SAFE_EQ, (), (("key_clash", True),))
        with patch("app.automix.intelligence.MAX_MID_HANDOFF_EXTENSION_SECONDS", 0.0):
            initial_candidate, initial = optimize_handoff(candidate, decision, a, b, None)
        adjusted_candidate, adjusted = optimize_handoff(candidate, decision, a, b, None)
        before, after = dict(initial.metrics), dict(adjusted.metrics)
        self.assertEqual(replace(adjusted_candidate, reasons=()), replace(initial_candidate, reasons=()))
        self.assertEqual(adjusted.dsp, initial.dsp)
        self.assertEqual(adjusted.band_windows[::2], initial.band_windows[::2])
        self.assertLessEqual(after["predicted_mid_high_dip_db"], before["predicted_mid_high_dip_db"] - 1.0)
        before_mid, after_mid = initial.band_windows[1][0], adjusted.band_windows[1][0]
        self.assertAlmostEqual(((after_mid[1] - after_mid[0]) - (before_mid[1] - before_mid[0])) * 4.0, 0.35)
        self.assertGreaterEqual(after["predicted_weakest_block_db"], before["predicted_weakest_block_db"] - 0.1)

        a = replace(a, vocal_activity=((56.0, 60.0),))
        b = replace(b, vocal_activity=((0.0, 4.0),))
        _, protected = optimize_handoff(candidate, decision, a, b, None)
        self.assertIsNone(dict(protected.metrics)["predicted_mid_high_dip_db"])

    def test_trimming_into_a_ramp_keeps_the_original_rates_and_audition_cue(self):
        from tools.automix_listening_report import excerpt_plan
        from app.timeline.render_plan import CompiledRenderPlan, AudioRenderTransition, build_presentation_and_metadata

        original = AudioRenderClip("a", "a", 0.0, 0.0, 60.0, tempo_ramp=TempoRamp(30.0, 46.0, 1.08))
        cue_time = original.timeline_at(46.0)
        incoming = AudioRenderClip("b", "b", cue_time, 0.0, 60.0)
        transition = AudioRenderTransition("a", "b", cue_time, original.timeline_end - cue_time,
                                           TransitionType.BEAT_MATCH, TransitionDsp.BASS_SWAP)
        presentation, metadata, duration = build_presentation_and_metadata((original, incoming))
        plan = CompiledRenderPlan(audio=AudioRenderPlan((original, incoming), (transition,)),
                                  presentation=presentation, metadata=metadata, duration_seconds=duration)
        excerpt, lead = excerpt_plan(plan, transition, "bass_swap")
        clipped = excerpt.clips[0]
        self.assertAlmostEqual(lead, 8.0)
        self.assertAlmostEqual(clipped.source_at(lead), 46.0)
        for point in np.linspace(0.0, clipped.duration, 30):
            self.assertAlmostEqual(clipped.source_at(float(point)), original.source_at(cue_time - lead + float(point)))

    def test_excerpt_after_a_ramp_holds_rate_and_pitch_without_an_empty_command_filter(self):
        clip = AudioRenderClip("a", "a", 0.0, 20.0, 30.0,
                               tempo_ramp=TempoRamp(5.0, 15.0, 1.1, end_pitch=1.06))
        graph, _ = build_filter_graph((clip,), ())
        self.assertNotIn("asendcmd=c=''", graph)
        self.assertIn("tempo=1.100000:pitch=1.060000", graph)

    def test_measurements_are_extracted_without_structure_model_and_cached(self):
        t = np.arange(4 * SAMPLE_RATE) / SAMPLE_RATE
        signal = (0.3 * np.sin(2 * np.pi * 60 * t)).astype(np.float32)
        provider = BasicAnalysisProvider(Path("ffmpeg"))
        with patch("app.automix.analysis.basic.analyze_music", return_value=()):
            analysis = provider.analyze_signal(_track("a", 4.0), signal,
                                               cancel_event=threading.Event(), rhythm=False)
        self.assertEqual(len(analysis.rms_curve), 4)
        self.assertEqual(len(analysis.band_power_curve), 40)
        self.assertGreater(min(analysis.bass_curve), 0.9)
        self.assertLess(max(analysis.percussive_curve), 0.2)
        self.assertEqual(TrackAnalysis.from_cache_fields("a", "a.mp3", analysis.to_cache_fields()), analysis)
        for field, bad in (("rms_curve", float("nan")), ("bass_curve", 1.1), ("percussive_curve", -0.1)):
            with self.assertRaises(ValueError):
                replace(analysis, **{field: (bad,)})
        with self.assertRaises(ValueError):
            replace(analysis, band_power_curve=((1.0, 0.0),))
        legacy = analysis.to_cache_fields()
        legacy.pop("band_power_curve")
        self.assertEqual(TrackAnalysis.from_cache_fields("a", "a.mp3", legacy).band_power_curve, ())

    def test_band_power_preserves_spectral_balance_and_partial_block_level(self):
        lengths = (2205, 2205, 1102)
        tones = [0.2 * np.sin(2 * np.pi * frequency * np.arange(count) / SAMPLE_RATE)
                 for frequency, count in zip((70, 700, 4000), lengths, strict=True)]
        measured = np.array(band_power_curve(np.concatenate(tones), SAMPLE_RATE))
        np.testing.assert_array_equal(np.argmax(measured, axis=1), [0, 1, 2])
        np.testing.assert_allclose(measured.sum(axis=1), [np.mean(tone ** 2) for tone in tones], atol=1e-8)
        self.assertTrue(np.all(np.diag(measured) > measured.sum(axis=1) * 0.99))

    def test_blend_lands_on_the_first_vocal_without_skipping_its_words(self):
        incoming = _profile("b", vocal_activity=((12.0, 60.0),))
        plan = _plan(_profile("a"), incoming)
        transition = plan.audio.transitions[0]
        self.assertAlmostEqual(plan.audio.clips[1].source_in + transition.duration, 12.0)
        self.assertAlmostEqual(plan.audio.clips[1].source_in, 0.0)
        self.assertAlmostEqual(dict(transition.details)["obscured_vocal_seconds"], 0.0)
        self.assertEqual(plan.audio.clips[0].source_out, 60.0)

    def test_vocal_pickup_before_the_first_downbeat_can_choose_a_natural_tempo_handoff(self):
        incoming = _profile("b", vocal_activity=((0.0, 60.0),))
        incoming = replace(incoming, beats=tuple(b for b in incoming.beats if b >= 2.0),
                           downbeats=tuple(b for b in incoming.downbeats if b >= 2.0))
        plan = _plan(_profile("a", vocal_activity=((40.0, 60.0),)), incoming)
        self.assertAlmostEqual(plan.audio.clips[1].source_in, 0.0)
        self.assertEqual(dict(plan.audio.transitions[0].details)["skipped_vocal_seconds"], 0.0)

    def test_a_short_handoff_competes_even_when_a_long_blend_fits(self):
        outgoing, incoming = _profile("a"), _profile("b")
        candidates = generate_candidates(outgoing, incoming, evaluate_compatibility(outgoing, incoming, ENABLED), ENABLED)
        self.assertTrue(any(c.duration_seconds < 5.0 for c in candidates))
        self.assertTrue(any(c.duration_seconds > 10.0 for c in candidates))

    def test_incompatible_tempos_still_compare_multiple_mixing_lengths(self):
        a, b = replace(_profile("a"), bpm=100.0), replace(_profile("b"), bpm=140.0)
        candidates = generate_candidates(a, b, evaluate_compatibility(a, b, ENABLED), ENABLED)
        self.assertGreaterEqual(len({c.duration_seconds for c in candidates if c.duration_seconds > 0}), 4)

    def test_only_measured_fades_get_intermediate_lengths(self):
        a, b = replace(_profile("a"), bpm=100.0), replace(_profile("b"), bpm=140.0)
        compatible = evaluate_compatibility(a, b, ENABLED)
        self.assertFalse(any(c.refined_duration for c in generate_candidates(a, b, compatible, ENABLED)))
        power = ((0.025, 0.01, 0.005),) * 600
        measured = generate_candidates(replace(a, band_power_curve=power), replace(b, band_power_curve=power),
                                       compatible, ENABLED)
        self.assertEqual({c.duration_seconds for c in measured if c.refined_duration}, {2.5, 3.5, 5.0, 7.0})

    def test_unknown_shared_tempo_does_not_echo_away_the_last_words(self):
        a = replace(_profile("a", vocal_activity=((40.0, 60.0),)), bpm=100.0)
        b = replace(_profile("b", vocal_activity=((0.0, 60.0),)), bpm=140.0)
        plan = _plan(a, b)
        self.assertNotIn(plan.audio.transitions[0].dsp, (TransitionDsp.ECHO_OUT, TransitionDsp.TAPE_STOP))
        self.assertEqual(plan.audio.clips[0].source_out, 60.0)

    def test_acoustic_selection_cannot_cut_off_a_tail_line_or_sung_pickup(self):
        a = replace(_profile("a", vocal_activity=((58.0, 60.0),)), bpm=100.0)
        b = replace(_profile("b", vocal_activity=((0.2, 1.3),)), bpm=140.0,
                    beats=tuple(t / 2 for t in range(4, 120)), downbeats=tuple(float(t) for t in range(2, 60, 2)))
        plan = _plan(a, b)
        self.assertEqual(plan.audio.clips[0].source_out, 60.0)
        self.assertEqual(plan.audio.clips[1].source_in, 0.0)
        self.assertGreater(plan.audio.transitions[0].duration, 0.05)

    def test_an_eligible_effect_does_not_override_a_clean_measured_fade(self):
        from app.automix.exits import phrase_exits
        from tests.test_automix_variety import _structure

        outgoing, incoming = (_analysis(track_id, bpm, 60.0) for track_id, bpm in (("a", 100.0), ("b", 140.0)))
        a = replace(_profile("a"), bpm=outgoing.bpm, beats=outgoing.beats, downbeats=outgoing.downbeats)
        b = replace(_profile("b"), bpm=incoming.bpm, beats=incoming.beats, downbeats=incoming.downbeats)
        structures = {track_id: _structure(track_id, 60, 0.6, 0.9) for track_id in ("a", "b")}
        self.assertIn(TransitionDsp.DOWNBEAT_CUT, {exit.decision.dsp for exit in phrase_exits(a, b, *structures.values())})
        plan = compile_automix([_track("a", 60), _track("b", 60)], {"a": a, "b": b}, ENABLED, structures)
        self.assertEqual(dict(plan.audio.transitions[0].details)["strategy"], "fixed_crossfade")
        self.assertEqual(plan.audio.clips[0].source_out, 60.0)

    def test_track_volume_changes_the_acoustic_level_comparison(self):
        a, b = _profile("a"), _profile("b")
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        flat = dict(_plan(a, b).audio.transitions[0].details)
        tracks[1].volume_db = -18
        adjusted = compile_automix(tracks, {"a": a, "b": b}, ENABLED, log_diagnostics=False)
        details = dict(adjusted.audio.transitions[0].details)
        self.assertAlmostEqual(flat["source_level_delta_db"], 0.0)
        self.assertAlmostEqual(details["source_level_delta_db"], -18.0)
        self.assertNotAlmostEqual(details["acoustic_cost"], flat["acoustic_cost"])

    def test_a_low_shelf_changes_the_predicted_bass_handoff(self):
        a, b = _profile("a"), _profile("b")
        tracks = [_track("a", 60.0), _track("b", 60.0)]
        flat = dict(_plan(a, b).audio.transitions[0].details)
        tracks[1].bass_db = 9.0
        adjusted = compile_automix(tracks, {"a": a, "b": b}, ENABLED, log_diagnostics=False)
        details = dict(adjusted.audio.transitions[0].details)
        self.assertAlmostEqual(flat["source_bass_delta_db"], 0.0)
        self.assertGreater(details["source_bass_delta_db"], details["source_level_delta_db"])
        self.assertGreater(details["source_level_delta_db"], 0.0)

    def test_same_bpm_out_of_phase_costs_more_through_a_fixed_manual_window(self):
        a, b = _profile("a"), _profile("b")
        shifted = replace(b, beats=tuple(t + 0.25 for t in b.beats), downbeats=tuple(t + 0.25 for t in b.downbeats))
        settings = ENABLED.with_overrides({"a>b": TransitionOverride(52.0, 0.0, 4.0, tempo_match=False)})
        phase = dict(_plan(a, b, settings).audio.transitions[0].details)
        off_phase = dict(_plan(a, shifted, settings).audio.transitions[0].details)
        self.assertAlmostEqual(phase["overlap_beat_offset_ms"], 0.0)
        self.assertAlmostEqual(off_phase["overlap_beat_offset_ms"], 250.0)
        self.assertGreater(off_phase["rhythm_collision_cost"], phase["rhythm_collision_cost"])

    def test_rhythm_refinement_uses_measured_bands_without_replacing_the_selected_cue(self):
        bass, bright = (0.016, 0.001, 0.001), (0.001, 0.006, 0.018)
        a = _profile("a", band_power_curve=(bass,) * 570 + (bright,) * 15 + (bass,) * 15)
        b = _profile("b", band_power_curve=(bright,) * 15 + (bass,) * 585)
        b = replace(b, beats=tuple(t + 0.125 for t in b.beats), downbeats=tuple(t + 0.125 for t in b.downbeats))
        candidate = _candidate(TransitionStrategy.FIXED_CROSSFADE, duration=3.0,
                               outgoing_source_time=57.0, outgoing_source_out=60.0)
        compatibility = evaluate_compatibility(a, b, ENABLED)
        with patch("app.automix.intelligence.MIN_RHYTHM_REFINEMENT_SECONDS", float("inf")):
            initial, base = choose_transition([candidate], compatibility, a, b, None, None)
        selected, refined = choose_transition([candidate], compatibility, a, b, None, None)

        self.assertEqual(selected, initial)
        self.assertEqual(refined.dsp, base.dsp)
        self.assertEqual(refined.band_windows[0], base.band_windows[0])
        self.assertNotEqual(refined.band_windows[1:], base.band_windows[1:])
        before, after = dict(base.metrics), dict(refined.metrics)
        self.assertEqual(after["overlap_beat_offset_ms"], before["overlap_beat_offset_ms"])
        self.assertLessEqual(after["predicted_rhythm_overlap_seconds"], before["predicted_rhythm_overlap_seconds"] - 0.04)
        self.assertGreaterEqual(after["predicted_weakest_block_db"], before["predicted_weakest_block_db"] - 0.1)

    def test_quiet_transition_endpoints_do_not_hide_a_hole_compared_with_solo_context(self):
        a, b = _profile("a"), _profile("b")
        settings = ENABLED.with_overrides({"a>b": TransitionOverride(52.0, 0.0, 4.0, tempo_match=False)})
        flat = dict(_plan(a, b, settings).audio.transitions[0].details)
        quiet_tail = replace(a, rms_curve=(0.2,) * 52 + (0.02,) * 8)
        quiet_head = replace(b, rms_curve=(0.02,) * 6 + (0.2,) * 54)
        quiet = dict(_plan(quiet_tail, quiet_head, settings).audio.transitions[0].details)
        self.assertGreater(flat["predicted_weakest_block_db"], -3.0)
        self.assertLess(quiet["predicted_weakest_block_db"], -15.0)
        self.assertGreater(quiet["acoustic_cost"], flat["acoustic_cost"] + 0.25)

    def test_measured_band_power_exposes_a_gap_hidden_by_one_second_averages(self):
        a, b = _profile("a"), _profile("b")
        full, quiet = (0.025, 0.01, 0.005), (0.00025, 0.0001, 0.00005)
        a = replace(a, band_power_curve=(full,) * 537 + (quiet,) * 6 + (full,) * 57)
        b = replace(b, band_power_curve=(full,) * 17 + (quiet,) * 6 + (full,) * 577)
        settings = ENABLED.with_overrides({"a>b": TransitionOverride(52.0, 0.0, 4.0, tempo_match=False)})
        details = dict(_plan(a, b, settings).audio.transitions[0].details)
        self.assertLess(details["predicted_weakest_block_db"], -16.0)

    def test_two_singers_get_a_brief_handoff_instead_of_a_long_overlay(self):
        transition = _plan(_profile("a", vocal_activity=((40.0, 60.0),)),
                           _profile("b", vocal_activity=((0.0, 60.0),))).audio.transitions[0]
        self.assertLessEqual(transition.duration, 4.0 + 1e-6)
        self.assertLess(dict(transition.details)["voice_collision_seconds"], 1.0)

    def test_band_handoffs_follow_the_actual_audio_instead_of_a_fixed_template(self):
        incoming = replace(_profile("b", vocal_activity=((2.0, 60.0),)), rms_curve=(0.1,) * 8 + (0.2,) * 52,
                           bass_curve=(0.02,) * 8 + (0.7,) * 52,
                           percussive_curve=(0.0,) * 8 + (0.9,) * 52)
        transition = _plan(_profile("a"), incoming).audio.transitions[0]
        windows = band_windows_of(transition)
        self.assertIsNotNone(transition.band_windows)
        self.assertNotEqual(windows[0], windows[1])
        self.assertGreater(windows[0][1][0] * transition.duration, 6.0)
        self.assertLess(dict(transition.details)["predicted_hole_db"], 1.0)
        self.assertIn(8.0, incoming_landmarks(incoming))

    def test_unknown_vocals_and_clashing_keys_keep_voice_protection(self):
        for fields in ({"vocal_coverage": ()}, {"key": "F# major", "key_confidence": 0.9}):
            outgoing = _profile("a", key="C major", key_confidence=0.9)
            if "vocal_coverage" in fields:
                outgoing = replace(outgoing, vocal_coverage=())
            transition = _plan(outgoing, replace(_profile("b"), **fields)).audio.transitions[0]
            if transition.duration >= 4.0:
                self.assertIs(transition.dsp, TransitionDsp.VOCAL_SAFE_EQ)
                self.assertLessEqual(transition.band_windows[1][0][1] - transition.band_windows[1][0][0], 0.25)

    def test_search_is_deterministic_and_manual_edits_stay_exact(self):
        a, b = _profile("a"), _profile("b")
        self.assertEqual(_plan(a, b), _plan(a, b))
        override = TransitionOverride(outgoing_cue=50.0, incoming_cue=2.0, duration=6.0,
                                      style="short_fade", tempo_match=False)
        manual = _plan(a, b, ENABLED.with_overrides({"a>b": override}))
        transition = manual.audio.transitions[0]
        self.assertEqual(transition.timeline_start, 50.0)
        self.assertEqual(transition.duration, 6.0)
        self.assertEqual(manual.audio.clips[1].source_in, 2.0)
        self.assertIsNone(transition.band_windows)
        self.assertNotIn("acoustic_cost", dict(transition.details))

    def test_measured_playlists_with_short_tracks_and_overrides_keep_plan_invariants(self):
        from tests.test_automix_invariants import _case, PlannerInvariantTests
        check = PlannerInvariantTests()
        for seed in range(20):
            tracks, analyses, overrides = _case(seed)
            profiles = {key: replace(a, rms_curve=(0.2,) * (int(a.duration_seconds) + 1),
                                    bass_curve=(0.5,) * (int(a.duration_seconds) + 1),
                                    brightness_curve=(0.2,) * (int(a.duration_seconds) + 1),
                                    percussive_curve=(0.6,) * (int(a.duration_seconds) + 1))
                        for key, a in analyses.items()}
            with self.subTest(seed=seed):
                plan = compile_automix(tracks, profiles, ENABLED.with_overrides(overrides), log_diagnostics=False)
                check.assert_plan_invariants(seed, tracks, plan)

    def test_partial_or_absent_profiles_use_existing_safe_rules(self):
        a, b = _analysis("a", 120.0, 60.0), _analysis("b", 120.0, 60.0)
        baseline = _plan(a, b)
        for incoming in (replace(b, rms_curve=(0.2,)), replace(_profile("b"), bass_curve=())):
            transition = _plan(a, incoming).audio.transitions[0]
            self.assertNotIn("acoustic_cost", dict(transition.details))
            self.assertIsNone(transition.band_windows)
        self.assertNotIn("acoustic_cost", dict(baseline.audio.transitions[0].details))


@unittest.skipUnless(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG"), "Set PLAYLIST_CANVAS_TEST_FFMPEG")
class MeasuredRenderTests(unittest.TestCase):
    def test_eq_power_response_matches_ffmpeg_at_low_middle_and_high_frequencies(self):
        ffmpeg = os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"]
        frequencies = np.array([50.0, 110.0, 600.0, 2000.0, 8000.0, 15500.0])
        eq = [2.0, -1.0, 3.0, 0.0, -2.0, 4.0, 0.0, -3.0]
        source = "aevalsrc=" + "+".join(f"0.03*sin(2*PI*{f}*t)" for f in frequencies) + ":s=48000:d=2"

        def magnitudes(filters: str) -> np.ndarray:
            args = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", source, "-af", filters, "-f", "f32le", "-"]
            pcm = np.frombuffer(subprocess.check_output(args), dtype="<f4")
            return np.abs(np.fft.rfft(pcm[-48000:]))[frequencies.astype(int)]

        actual_db = 20 * np.log10(magnitudes(track_audio_filter(0.0, eq, bass_db=9.0)) / magnitudes("anull"))
        estimated_power = np.prod([eq_power_response(kind, hz, db, frequencies)
                                   for kind, hz, db in track_eq_filters(eq, 9.0)], axis=0)
        np.testing.assert_allclose(actual_db, 10 * np.log10(estimated_power), atol=0.15)

    def test_custom_handoffs_reach_ffmpeg_without_changing_duration_or_clipping(self):
        import wave
        ffmpeg = Path(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"])
        chosen = _plan(_profile("a"), _profile("b", vocal_activity=((12.0, 60.0),))).audio.transitions[0]
        with TemporaryDirectory() as root:
            root = Path(root)
            paths = {}
            for i, track_id in enumerate(("a", "b")):
                t = np.arange(16 * 48000) / 48000
                signal = 0.3 * np.sin(2 * np.pi * (80 + i * 30) * t) + 0.1 * np.sin(2 * np.pi * (440 + i * 220) * t)
                path = root / f"{track_id}.wav"
                with wave.open(str(path), "wb") as file:
                    file.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                    file.writeframes((signal * 32767).astype("<i2").tobytes())
                paths[track_id] = path
            duration = chosen.duration
            clips = (AudioRenderClip("a", "a", 0.0, 0.0, 16.0),
                     AudioRenderClip("b", "b", 16.0 - duration, 0.0, 16.0))
            transition = replace(chosen, clip_a="a", clip_b="b", timeline_start=16.0 - duration,
                                 type=TransitionType.BEAT_MATCH)
            rendered = AutoMixAudioPipeline(ffmpeg).render(AudioRenderPlan(clips, (transition,)), paths, root)
            self.assertAlmostEqual(rendered.duration_seconds, 32.0 - duration, delta=0.15)
            import subprocess
            raw = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(rendered.path),
                                  "-f", "f32le", "pipe:1"], capture_output=True, check=True).stdout
            pcm = np.frombuffer(raw, dtype="<f4")
            self.assertTrue(np.all(np.isfinite(pcm)))
            self.assertLess(float(np.max(np.abs(pcm))), 0.98)


if __name__ == "__main__":
    unittest.main()
