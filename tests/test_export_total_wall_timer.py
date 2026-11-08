"""End-to-end video export timer is accurate and records at most once."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.controllers.export_controller import ExportOrchestrator


class VideoExportWallTimerTests(unittest.TestCase):
    def test_records_elapsed_time_once_and_resets(self) -> None:
        orchestrator = ExportOrchestrator.__new__(ExportOrchestrator)
        orchestrator._video_export_started_at = 100.0
        with patch("app.utils.performance.ENABLED", True), \
                patch("app.utils.performance.PROFILE.observe") as observe, \
                patch("app.controllers.export_controller.monotonic", return_value=127.25):
            orchestrator.record_video_export_total_wall_time(completed=True)
            orchestrator.record_video_export_total_wall_time(completed=True)
        self.assertEqual(observe.call_count, 2)
        observe.assert_any_call("export.total_wall_seconds", 27.25)
        observe.assert_any_call("export.completed_wall_seconds", 27.25)
        self.assertIsNone(orchestrator._video_export_started_at)

    def test_cancelled_video_export_is_recorded_as_incomplete(self) -> None:
        orchestrator = ExportOrchestrator.__new__(ExportOrchestrator)
        orchestrator._video_export_started_at = 40.0
        with patch("app.utils.performance.ENABLED", True), \
                patch("app.utils.performance.PROFILE.observe") as observe, \
                patch("app.controllers.export_controller.monotonic", return_value=46.0):
            orchestrator.record_video_export_total_wall_time()
        observe.assert_any_call("export.total_wall_seconds", 6.0)
        observe.assert_any_call("export.incomplete_wall_seconds", 6.0)

    def test_does_not_record_audio_only_or_preflight_rejection(self) -> None:
        orchestrator = ExportOrchestrator.__new__(ExportOrchestrator)
        orchestrator._video_export_started_at = None
        with patch("app.utils.performance.ENABLED", True), \
                patch("app.utils.performance.PROFILE.observe") as observe:
            orchestrator.record_video_export_total_wall_time()
        observe.assert_not_called()

    def test_profiling_disabled_does_not_allocate_measurements(self) -> None:
        orchestrator = ExportOrchestrator.__new__(ExportOrchestrator)
        orchestrator._video_export_started_at = 50.0
        with patch("app.utils.performance.ENABLED", False), \
                patch("app.utils.performance.PROFILE.observe") as observe:
            orchestrator.record_video_export_total_wall_time()
        observe.assert_not_called()
        self.assertIsNone(orchestrator._video_export_started_at)


if __name__ == "__main__":
    unittest.main()
