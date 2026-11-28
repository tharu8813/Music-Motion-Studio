from __future__ import annotations

import csv
import io
import json
import math
import unittest

import numpy as np

from app.automix.diagnostics import rows_to_csv, rows_to_json, transition_rows, window_metrics
from app.automix.planner import compile_automix
from tests.test_automix_planner import ENABLED, _analysis, _track


class TransitionRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        tracks = [_track("a", 96.0), _track("b", 96.0), _track("c", 96.0, start=400.0)]
        analyses = {track.id: _analysis(track.id, 120.0, 96.0) for track in tracks}
        self.plan = compile_automix(tracks, analyses, ENABLED)
        self.rows = transition_rows(self.plan, {"a": "Song A", "b": "Song B"})

    def test_every_junction_gets_a_row_with_the_planned_numbers(self) -> None:
        mixed, gap = self.rows
        transition = self.plan.audio.transitions[0]
        self.assertEqual((mixed["from"], mixed["to"]), ("Song A", "Song B"))
        self.assertEqual((mixed["timeline_start"], mixed["duration"]), (transition.timeline_start, transition.duration))
        self.assertEqual((mixed["strategy"], mixed["dsp"]), ("beat_match", "vocal_safe_eq"))
        self.assertEqual(mixed["outgoing_bpm"], 120.0)
        self.assertIsNone(mixed["vocal_overlap"])  # unknown stays unknown, never "no vocals"
        self.assertTrue(mixed["reasons"].startswith("* vocal_safe_eq: vocal activity unknown"))
        self.assertEqual((gap["to"], gap["type"], gap["duration"]), ("c", "gap", 0.0))

    def test_details_are_deterministic_and_carried_by_the_plan(self) -> None:
        again = compile_automix([_track("a", 96.0), _track("b", 96.0), _track("c", 96.0, start=400.0)],
                                {i: _analysis(i, 120.0, 96.0) for i in "abc"}, ENABLED)
        self.assertEqual(again, self.plan)
        self.assertIn(("bars", 8), self.plan.audio.transitions[0].details)

    def test_csv_and_json_exports_round_trip(self) -> None:
        parsed = json.loads(rows_to_json(self.rows))
        self.assertEqual(parsed[0]["strategy"], "beat_match")
        table = list(csv.DictReader(io.StringIO(rows_to_csv(self.rows))))
        self.assertEqual([row["type"] for row in table], ["beat_match", "gap"])
        self.assertEqual(table[1]["strategy"], "")  # a column the gap row does not have


class WindowMetricsTests(unittest.TestCase):
    RATE = 8000

    def noise(self, seconds: float) -> np.ndarray:
        return np.random.default_rng(1).normal(0.0, 0.1, int(seconds * self.RATE))

    def test_steady_audio_measures_flat(self) -> None:
        metrics = window_metrics(self.noise(30.0), self.RATE, 10.0, 8.0)
        for name in ("level_dip_db", "level_peak_db", "level_mean_db", "low_excess_db"):
            self.assertLess(abs(metrics[name]), 1.5, name)

    def test_a_hole_is_measured(self) -> None:
        samples = self.noise(30.0)
        samples[int(13 * self.RATE):int(14 * self.RATE)] *= 0.05  # -26 dB for one second
        metrics = window_metrics(samples, self.RATE, 10.0, 8.0)
        self.assertLess(metrics["level_dip_db"], -20.0)
        self.assertLess(abs(metrics["level_peak_db"]), 1.5)

    def test_a_bass_build_up_is_measured(self) -> None:
        samples = self.noise(30.0)
        seconds = np.arange(int(8 * self.RATE)) / self.RATE
        samples[int(10 * self.RATE):int(18 * self.RATE)] += 0.1 * np.sin(2 * np.pi * 60 * seconds)
        metrics = window_metrics(samples, self.RATE, 10.0, 8.0)
        self.assertGreater(metrics["low_excess_db"], 10.0)
        self.assertLess(metrics["level_mean_db"], 3.0)  # barely louder overall: the build-up is all low end

    def test_no_context_is_nan(self) -> None:
        self.assertTrue(math.isnan(window_metrics(self.noise(8.0), self.RATE, 0.0, 8.0)["level_dip_db"]))

    def test_short_crossfade_and_cut_both_measure_the_junction(self) -> None:
        samples = self.noise(4.0)
        for duration in (0.0, 0.05):
            metrics = window_metrics(samples, self.RATE, 2.0, duration)
            self.assertTrue(all(math.isfinite(value) for value in metrics.values()))

    def test_a_loud_cut_does_not_hide_the_quiet_audio_on_either_side(self) -> None:
        samples = self.noise(4.0)
        samples[round(1.75 * self.RATE):round(2.0 * self.RATE)] *= 0.05
        samples[round(2.05 * self.RATE):round(2.3 * self.RATE)] *= 0.05
        metrics = window_metrics(samples, self.RATE, 2.0, 0.05)
        self.assertGreater(metrics["level_dip_db"], -2.0)
        self.assertLess(metrics["handoff_dip_db"], -6.0)


if __name__ == "__main__":
    unittest.main()
