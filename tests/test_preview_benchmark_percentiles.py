"""Regression tests for preview timing percentiles without starting a Qt GUI."""

from __future__ import annotations

import unittest

from tools.performance_benchmark import _nearest_rank_ms


class PreviewBenchmarkPercentileTests(unittest.TestCase):
    def test_no_intervals_does_not_fabricate_timing(self) -> None:
        for fraction in (0.50, 0.95, 0.99):
            self.assertIsNone(_nearest_rank_ms([], fraction))

    def test_single_interval_is_the_observed_frame_time(self) -> None:
        for fraction in (0.50, 0.95, 0.99):
            self.assertEqual(_nearest_rank_ms([0.033], fraction), 33)

    def test_nearest_rank_p95_ceil_does_not_hide_a_late_frame(self) -> None:
        # 21 intervals: ceil(21*0.95)=20th (20 ms), not the 19th.
        intervals = [n / 1000 for n in range(1, 22)]
        self.assertEqual(_nearest_rank_ms(intervals, 0.95), 20.0)
        self.assertEqual(_nearest_rank_ms(intervals, 0.50), 11.0)
        self.assertEqual(_nearest_rank_ms(intervals, 0.99), 21.0)

    def test_two_intervals_capture_tail_at_p95(self) -> None:
        self.assertEqual(_nearest_rank_ms([0.016, 0.300], 0.95), 300.0)

    def test_nearest_rank_p95_matches_profile_snapshot_method(self) -> None:
        from app.utils.performance import PerformanceProfile

        intervals = [n / 1000 for n in range(1, 43)]
        profile = PerformanceProfile()
        for duration in intervals:
            profile.observe("preview.test_interval", duration)
        self.assertEqual(
            _nearest_rank_ms(intervals, 0.95),
            profile.snapshot()["preview.test_interval"]["recent_p95"] * 1000,
        )


if __name__ == "__main__":
    unittest.main()
