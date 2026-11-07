"""Pure diagnostic tests; no GPU, ffmpeg, Qt, or psutil installation required."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.export_bottleneck_profile import (
    assess_capture, classify_process, load_stage_profiles, parse_gpu_output, summarize,
)
from app.utils import performance


class ExportBottleneckProfileTests(unittest.TestCase):
    def test_crashed_target_is_not_reported_as_a_successful_export(self) -> None:
        status, warning = assess_capture(1, [{"process_counts": {}, "gpu": [
            {"gpu_percent": 30, "encoder_percent": 40}]}], {"stages": {}})
        self.assertEqual(status, "target_failed")
        self.assertIn("code 1", warning)

    def test_idle_gpu_is_not_evidence_of_export(self) -> None:
        status, warning = assess_capture(0, [{"process_counts": {}, "gpu": [
            {"gpu_percent": 30, "encoder_percent": 40}]}], {"stages": {}})
        self.assertEqual(status, "no_export_observed")
        self.assertIn("No FFmpeg", warning)

    def test_detects_an_export_from_ffmpeg_or_stage_timing(self) -> None:
        self.assertEqual(assess_capture(None, [{"process_counts": {"ffmpeg": 1}}],
                                        {"stages": {}}), ("export_observed", None))
        self.assertEqual(assess_capture(0, [], {"stages": {
            "export.piped_capture_session_seconds": {"calls": 12}}}),
            ("export_observed", None))

    def test_process_classification(self) -> None:
        self.assertEqual(classify_process("Music Motion Studio.exe", [], True), "editor")
        self.assertEqual(classify_process("ffmpeg.exe", ["-i", "pipe"], False), "ffmpeg")
        self.assertEqual(classify_process("python.exe", ["-c", "from multiprocessing.spawn import spawn_main"]), "canvas_workers")
        self.assertEqual(classify_process("Music Motion Studio.exe", ["--multiprocessing-fork"]), "canvas_workers")
        self.assertEqual(classify_process("helper.exe", []), "other")

    def test_gpu_csv_handles_unsupported_and_multiple_cards(self) -> None:
        rows = parse_gpu_output("0, 28 %, 13 %, 1120 MiB\n1, 50, 0, 2048\n2, N/A, N/A, 20\n")
        self.assertEqual([row["index"] for row in rows], [0, 1])
        self.assertEqual(rows[0]["encoder_percent"], 13.0)
        self.assertEqual(rows[1]["memory_mib"], 2048.0)

    def test_summary_respects_elapsed_intervals_and_missing_gpu(self) -> None:
        data = [
            {"interval_seconds": 0.5, "cpu_cores": {"editor": 1.0, "ffmpeg": 2.0},
             "rss_mib": {"editor": 100, "ffmpeg": 200}, "gpu": [{"gpu_percent": 30, "encoder_percent": 40}]},
            {"interval_seconds": 0.5, "cpu_cores": {"editor": 2.0, "ffmpeg": 0.0},
             "rss_mib": {"editor": 150, "ffmpeg": 200}, "gpu": None},
        ]
        result = summarize(data, {"profile_files": 0, "stages": {}})
        self.assertEqual(result["observed_cpu_core_seconds"]["editor"], 1.5)
        self.assertEqual(result["observed_cpu_core_seconds"]["ffmpeg"], 1.0)
        self.assertEqual(result["peak_process_tree_rss_mib"], 350)
        self.assertEqual(result["device_nvenc_mean_percent"], 40)

    def test_combines_editor_and_worker_timings_without_overwriting(self) -> None:
        with TemporaryDirectory() as directory:
            parent = Path(directory) / "export.timings.json"
            child = Path(directory) / "export.timings.worker-42.json"
            parent.write_text(json.dumps({"metrics": {
                "export.renderer_seconds": {"count": 1, "total": 5.0},
                "unknown": {"count": 1, "total": 100.0},
            }}), encoding="utf-8")
            child.write_text(json.dumps({"metrics": {
                "export.frame_generation_seconds": {"count": 12, "total": 2.5},
            }}), encoding="utf-8")
            profile = load_stage_profiles(parent)
            self.assertEqual(profile["profile_files"], 2)
            self.assertEqual(profile["stages"]["export.frame_generation_seconds"]["calls"], 12)
            self.assertEqual(profile["stages"]["export.renderer_seconds"]["inclusive_wall_seconds"], 5.0)
            self.assertNotIn("unknown", profile["stages"])

    def test_worker_profile_save_does_not_replace_editor_profile(self) -> None:
        with TemporaryDirectory() as directory:
            parent = Path(directory) / "export.timings.json"
            parent.write_text("parent remains", encoding="utf-8")
            profile = performance.PerformanceProfile()
            profile.observe("canvas.scene_render_seconds", 0.25)
            with (patch.object(performance, "ENABLED", True),
                  patch.object(performance, "ROOT_PID", "100"),
                  patch.object(performance, "OUTPUT", str(parent)),
                  patch.object(performance, "PROFILE", profile),
                  patch.object(performance.os, "getpid", return_value=200)):
                performance.save()
            self.assertEqual(parent.read_text(encoding="utf-8"), "parent remains")
            output = parent.with_name("export.timings.worker-200.json")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["pid"], 200)


if __name__ == "__main__":
    unittest.main()
