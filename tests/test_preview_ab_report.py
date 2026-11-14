"""A/B/B/A preview diagnostics compare matching runs only, without Qt."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.preview_ab_report import _read, compare


def _sample(fps=40.0, *, drops=8, seconds=8.0, scale=0.55):
    return {
        "benchmark": "real_video_preview",
        "backend": "gpu_layers",
        "preview_quality": "balanced",
        "width": 1920,
        "height": 1080,
        "video_count": 2,
        "target_fps": 30,
        "render_path": "GPU ordered canvas",
        "transport_exercised": False,
        "lyrics": True,
        "integrated": True,
        "known_fixture_beats": True,
        "track_count": 3,
        "analysis_workers": 0,
        "python": "3.14",
        "qt": "6.12",
        "numpy": "2.3",
        "platform": "Windows-11",
        "gpu_backend": "OpenGL",
        "gpu_active_at_end": True,
        "proxy_count": 2,
        "blended_audio_ready": True,
        "seconds": seconds,
        "canvas_scale_min": scale,
        "canvas_scale_max": scale,
        "presented_scene_fps": fps,
        "scene_frame_time_p50_ms": 1000 / fps,
        "scene_frame_time_p95_ms": 1000 / fps * 1.2,
        "heartbeat_max_ms": 110,
        "mean_parent_cpu_percent_of_one_core": 94,
        "rss_peak_bytes": 300 * 1024 * 1024,
        "frame_buffer_peak_bytes": 10 * 1024 * 1024,
        "decode_drops": drops,
    }


class PreviewAbReportTests(unittest.TestCase):
    def test_abba_reports_real_scene_fps_and_normalized_drops(self):
        report = compare([_sample(40), _sample(42)], [_sample(50, drops=4), _sample(52, drops=4)])
        self.assertEqual(report["method"], "A1-B1-B2-A2 paired preview measurement")
        self.assertTrue(report["performance_only"])
        self.assertTrue(report["same_effective_canvas_scale"])
        self.assertEqual(report["matched_configuration"]["preview_quality"], "balanced")
        metric = report["metrics"]["presented_scene_fps"]
        self.assertEqual(metric["baseline_mean"], 41)
        self.assertEqual(metric["candidate_mean"], 51)
        self.assertAlmostEqual(metric["candidate_change_percent"], 24.39, places=2)
        drop_metric = report["metrics"]["decode_drops_per_second"]
        self.assertEqual(drop_metric["baseline_mean"], 1)
        self.assertEqual(drop_metric["candidate_mean"], 0.5)
        self.assertEqual(drop_metric["candidate_change_percent"], -50)
        self.assertEqual(report["metrics"]["rss_peak_bytes"]["candidate_change_percent"], 0)

    def test_config_mismatch_rejects_comparing_different_quality_and_hardware(self):
        for name, value in (
            ("preview_quality", "quality"), ("width", 1280),
            ("target_fps", 60), ("platform", "Linux"),
            ("gpu_backend", "CPU"), ("render_path", "CPU"),
            ("proxy_count", 0),
        ):
            with self.subTest(field=name):
                changed = _sample()
                changed[name] = value
                with self.assertRaisesRegex(ValueError, "Incompatible"):
                    compare([_sample(), _sample()], [_sample(), changed])

    def test_adaptive_scale_change_is_reported_as_quality_confounder(self):
        report = compare([_sample(), _sample()], [_sample(scale=0.45), _sample(scale=0.45)])
        self.assertFalse(report["same_effective_canvas_scale"])
        self.assertIn("confounds", " ".join(report["notes"]))

    def test_optional_missing_percentiles_are_not_fabricated(self):
        runs = [_sample() for _ in range(4)]
        for run in runs:
            run["scene_frame_time_p95_ms"] = None
        report = compare([runs[0], runs[3]], [runs[1], runs[2]])
        self.assertNotIn("scene_frame_time_p95_ms", report["metrics"])

    def test_incomplete_auto_mix_and_nonpreview_results_are_rejected(self):
        run = _sample()
        run["blended_audio_ready"] = False
        with self.assertRaisesRegex(ValueError, "AutoMix not ready"):
            compare([_sample(), _sample()], [_sample(), run])
        invalid = _sample()
        invalid["benchmark"] = "native_export"
        with self.assertRaisesRegex(ValueError, "invalid real_video_preview"):
            compare([invalid, _sample()], [_sample(), _sample()])

    def test_reader_checks_benchmark_and_duration(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "result.json"
            target.write_text(json.dumps(_sample()), encoding="utf-8")
            self.assertEqual(_read(target)["target_fps"], 30)
            bad = _sample()
            bad["seconds"] = 0
            target.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duration"):
                _read(target)


if __name__ == "__main__":
    unittest.main()
