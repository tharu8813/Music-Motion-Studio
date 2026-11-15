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

    def test_preview_profile_is_frozen_before_resource_teardown(self) -> None:
        # This is intentionally a structural guard, not a GUI smoke test:
        # background decoder and OpenGL cleanup can be expensive and are not
        # part of the A/B measurement's opt-in profile sample window.
        import ast
        import inspect
        from textwrap import dedent

        from tools.performance_benchmark import preview_benchmark

        function = ast.parse(dedent(inspect.getsource(preview_benchmark))).body[0]
        snapshots = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "PROFILE"
            and node.func.attr == "snapshot"
        ]
        self.assertEqual(len(snapshots), 1)
        teardown_tries = [
            node for node in ast.walk(function)
            if isinstance(node, ast.Try)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_stop_preview"
                for statement in node.finalbody
                for call in ast.walk(statement)
            )
        ]
        self.assertEqual(len(teardown_tries), 1)
        before_cleanup = [
            node for statement in teardown_tries[0].body
            for node in ast.walk(statement)
        ]
        self.assertIn(snapshots[0], before_cleanup)
        self.assertTrue(
            any(
                isinstance(stmt, ast.Assign)
                and any(isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "result"
                        for target in stmt.targets)
                and isinstance(stmt.value, ast.Call)
                and stmt.value is snapshots[0]
                for stmt in before_cleanup
            )
        )

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
