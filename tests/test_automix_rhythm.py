"""AutoMix chooses a sustainable overlap from the rhythm at the actual cues."""
from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from app.automix.beatgrid import compare_overlap_beats, fit_beat_grid
from app.automix.candidates import TransitionStrategy, generate_candidates, select_best_candidate
from app.automix.compatibility import evaluate_compatibility
from app.automix.planner import compile_automix
from app.automix.settings import AutoMixTransitionSettings
from app.timeline.models import TransitionType
from app.timeline.render_plan import validate_compiled_render_plan
from tests.test_automix_candidates import _analysis, _structure
from tests.test_automix_planner import _track

SETTINGS = AutoMixTransitionSettings(enabled=True)


def _candidates(outgoing, incoming, **kwargs):
    return generate_candidates(
        outgoing, incoming, evaluate_compatibility(outgoing, incoming, SETTINGS), SETTINGS, **kwargs)


class RhythmAwareTransitionTests(unittest.TestCase):
    def test_overlap_measures_phase_even_when_bpm_matches(self):
        grid = tuple(np.arange(0.0, 20.0, 0.5))
        shifted = tuple(t + 0.25 for t in grid)
        _, errors, period = compare_overlap_beats(grid, shifted, 4.0, cues=(8.0, 4.0), rates=(1.0, 1.0))
        self.assertAlmostEqual(period, 0.5)
        np.testing.assert_allclose(errors, 0.25)

    def test_overlap_uses_timeline_rates_and_half_time_grid(self):
        outgoing = tuple(np.arange(0.0, 20.0, 1.0))
        incoming = tuple(np.arange(0.0, 20.0, 0.4))
        _, errors, period = compare_overlap_beats(outgoing, incoming, 4.0, cues=(8.0, 4.0), rates=(1.25, 1.0))
        self.assertAlmostEqual(period, 0.4)
        np.testing.assert_allclose(errors, 0.0, atol=1e-12)
        self.assertIsNone(compare_overlap_beats(outgoing, incoming, 0.5, cues=(8.0, 4.0), rates=(1.25, 1.0)))

    def test_unsteady_late_intro_shortens_the_blend_without_losing_the_ending(self):
        outgoing = _analysis("a", 120.0, 192.0)
        incoming = _analysis("b", 120.0, 180.0)
        stable = select_best_candidate(_candidates(outgoing, incoming))
        self.assertEqual(stable.bars, 8)
        # High reported confidence does not make these actual beats steady.
        beats = tuple(t + (0.14 if i % 2 else -0.14) if t >= 8.5 else t
                      for i, t in enumerate(incoming.beats))
        incoming = replace(incoming, beats=beats, downbeats=beats[::4])
        candidates = _candidates(outgoing, incoming)
        best = select_best_candidate(candidates)
        self.assertIs(best.strategy, TransitionStrategy.BEAT_MATCH)
        self.assertLess(best.duration_seconds, 10.0)
        self.assertLess(best.confidence, stable.confidence)
        self.assertTrue(any("rhythm error" in reason for reason in best.reasons))
        self.assertEqual(candidates, _candidates(outgoing, incoming))

        plan = compile_automix([_track("a", 192.0), _track("b", 180.0)],
                               {"a": outgoing, "b": incoming}, SETTINGS)
        validate_compiled_render_plan(plan)
        self.assertEqual(plan.audio.clips[0].source_out, 192.0)
        self.assertAlmostEqual(plan.audio.transitions[0].duration, best.duration_seconds)

    def test_unsteady_whole_intro_does_not_force_a_beat_match(self):
        outgoing = _analysis("a", 120.0, 192.0)
        incoming = _analysis("b", 120.0, 180.0)
        beats = tuple(t + (0.14 if i % 2 else -0.14) if i else t
                      for i, t in enumerate(incoming.beats))
        incoming = replace(incoming, beats=beats, downbeats=beats[::4])
        self.assertIs(select_best_candidate(_candidates(outgoing, incoming)).strategy,
                      TransitionStrategy.FIXED_CROSSFADE)
        plan = compile_automix([_track("a", 192.0), _track("b", 180.0)],
                               {"a": outgoing, "b": incoming}, SETTINGS)
        validate_compiled_render_plan(plan)
        self.assertTrue(all(t.type is not TransitionType.BEAT_MATCH for t in plan.audio.transitions))

    def test_structure_cue_uses_its_own_tempo_instead_of_the_track_head(self):
        outgoing = _analysis("a", 120.0, 192.0)
        incoming = _analysis("b", 120.0, 180.0)
        beats = tuple(np.arange(0.0, 80.0, 0.5)) + tuple(np.arange(80.0, 180.0, 60.0 / 124.0))
        incoming = replace(incoming, beats=beats, downbeats=beats[::4])
        candidates = _candidates(outgoing, incoming, incoming_structure=_structure("b", 180.0, intro_end=100.0))
        anchored = [c for c in candidates if abs(c.incoming_source_time - 100.0) < 2.0]
        self.assertTrue(anchored)
        for candidate in anchored:
            self.assertIs(candidate.strategy, TransitionStrategy.BEAT_MATCH)
            self.assertAlmostEqual(candidate.outgoing_rate, 124.0 / 120.0, places=6)
            self.assertAlmostEqual(candidate.target_bpm, 124.0, places=6)
        regular = [c for c in candidates if c.incoming_source_time < 1.0]
        self.assertTrue(regular)
        self.assertTrue(all(abs(c.outgoing_rate - 1.0) < 1e-6 for c in regular))

    def test_tempo_cost_uses_measured_rate_instead_of_a_quantized_bpm_label(self):
        outgoing = _analysis("a", 120.0, 192.0)
        incoming = _analysis("b", 120.0, 180.0)
        stable = _candidates(outgoing, incoming)
        mislabeled = _candidates(outgoing, replace(incoming, bpm=125.0))
        self.assertEqual([c.score for c in stable], [c.score for c in mislabeled])
        self.assertEqual([c.outgoing_rate for c in stable], [c.outgoing_rate for c in mislabeled])

    def test_frame_jitter_and_isolated_bad_detections_still_allow_matching(self):
        outgoing = _analysis("a", 128.0, 192.0)
        incoming = _analysis("b", 128.0, 180.0)
        beats = np.round(np.asarray(incoming.beats) * 50.0) / 50.0
        beats = np.delete(beats, 12)  # one missed beat
        beats = np.sort(np.append(beats, beats[5] + 0.1))  # one doubled detection
        incoming = replace(incoming, beats=tuple(beats))
        candidates = _candidates(outgoing, incoming)
        self.assertTrue(all(c.strategy is TransitionStrategy.BEAT_MATCH for c in candidates))
        self.assertAlmostEqual(select_best_candidate(candidates).outgoing_rate, 1.0, delta=0.001)

    def test_phase_error_requires_local_measurements_and_ignores_missing_beats(self):
        beats = tuple(t for t in np.arange(0.0, 30.0, 0.5) if t != 5.0)
        grid = fit_beat_grid(beats)
        self.assertIsNotNone(grid)
        self.assertEqual(grid.phase_error(beats, 0.0, 30.0), 0.0)
        self.assertIsNone(grid.phase_error(beats, 29.0, 30.0))
        self.assertIsNone(grid.phase_error(beats, 40.0, 50.0))


if __name__ == "__main__":
    unittest.main()
