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
        "requested_seconds": 8.0,
        "warmup_seconds": 3.0,
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

    def test_stage_p95_reports_real_samples_and_nonadditive_timings(self):
        runs = [_sample() for _ in range(4)]
        for run, capture_ms, upload_ms, count in zip(
            runs, (12.0, 9.0, 8.0, 11.0), (5.0, 3.0, 4.0, 5.0),
            (100, 110, 120, 130),
        ):
            run["profile"] = {
                "canvas.capture_seconds": {
                    "count": count, "recent_p95": capture_ms / 1000.0,
                },
                "preview.texture_prepare_upload_seconds": {
                    "count": count + 30, "recent_p95": upload_ms / 1000.0,
                },
            }
        result = compare([runs[0], runs[3]], [runs[1], runs[2]])
        stages = result["profile_stage_p95_ms"]
        capture = stages["canvas.capture_seconds"]
        self.assertEqual(capture["unit"], "ms")
        self.assertEqual(capture["baseline_p95_mean_ms"], 11.5)
        self.assertEqual(capture["candidate_p95_mean_ms"], 8.5)
        self.assertEqual(capture["sample_counts_abba"], [100, 110, 120, 130])
        self.assertEqual(
            stages["preview.texture_prepare_upload_seconds"]["candidate_p95_mean_ms"],
            3.5,
        )
        self.assertIn("must not be summed", " ".join(result["notes"]))
        self.assertNotIn("video.cpu_filter_seconds", stages)

    def test_stage_p95_does_not_fabricate_missing_or_unusable_samples(self):
        runs = [_sample() for _ in range(4)]
        for row in runs:
            row["profile"] = {
                "video.cpu_filter_seconds": {"count": 1, "recent_p95": 0.02},
            }
        runs[2]["profile"]["video.cpu_filter_seconds"]["recent_p95"] = float("nan")
        self.assertEqual(
            compare([runs[0], runs[3]], [runs[1], runs[2]])["profile_stage_p95_ms"],
            {},
        )
        runs[2]["profile"]["video.cpu_filter_seconds"]["recent_p95"] = 0.01
        runs[1]["profile"]["video.cpu_filter_seconds"]["count"] = 0
        self.assertEqual(
            compare([runs[0], runs[3]], [runs[1], runs[2]])["profile_stage_p95_ms"],
            {},
        )
        runs[1]["profile"]["video.cpu_filter_seconds"]["count"] = 1
        self.assertEqual(
            compare([runs[0], runs[3]], [runs[1], runs[2]])["profile_stage_p95_ms"][
                "video.cpu_filter_seconds"
            ]["baseline_p95_mean_ms"],
            20.0,
        )

    def test_legacy_preview_results_without_profiles_keep_existing_metrics(self):
        results = compare([_sample(30), _sample(32)], [_sample(35), _sample(37)])
        self.assertEqual(results["profile_stage_p95_ms"], {})
        self.assertEqual(results["metrics"]["presented_scene_fps"]["baseline_mean"], 31)
        self.assertEqual(results["metrics"]["presented_scene_fps"]["candidate_mean"], 36)

    def test_rejects_different_requested_lengths_or_warmup(self):
        for field, value in (("requested_seconds", 12.0), ("warmup_seconds", 0.0)):
            with self.subTest(field=field):
                mismatch = _sample()
                mismatch[field] = value
                with self.assertRaisesRegex(ValueError, "Incompatible"):
                    compare([_sample(), _sample()], [_sample(), mismatch])

    def test_actual_elapsed_duration_allows_small_drift_but_not_large_gap(self):
        report = compare(
            [_sample(seconds=8.0), _sample(seconds=8.2)],
            [_sample(seconds=8.3), _sample(seconds=8.1)],
        )
        self.assertIn("presented_scene_fps", report["metrics"])
        with self.assertRaisesRegex(ValueError, "measured durations"):
            compare(
                [_sample(seconds=8.0), _sample(seconds=8.2)],
                [_sample(seconds=8.3), _sample(seconds=12.0)],
            )

    def test_rejects_unusable_elapsed_duration_from_reader_and_comparator(self):
        for value in (float("nan"), float("inf"), True, -1.0, 0):
            with self.subTest(value=value):
                sample = _sample(seconds=value)
                with self.assertRaisesRegex(ValueError, "invalid real_video_preview"):
                    compare([_sample(), sample], [_sample(), _sample()])
                with TemporaryDirectory() as directory:
                    path = Path(directory) / "preview.json"
                    path.write_text(json.dumps(sample), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "duration"):
                        _read(path)

    def test_invalid_nan_infinite_or_boolean_metrics_do_not_enter_report(self):
        baseline = [_sample(), _sample()]
        candidate = [_sample(), _sample()]
        candidate[0]["presented_scene_fps"] = float("nan")
        candidate[1]["rss_peak_bytes"] = float("inf")
        baseline[0]["heartbeat_max_ms"] = True
        baseline[1]["decode_drops"] = -1
        report = compare(baseline, candidate)
        metrics = report["metrics"]
        for name in ("presented_scene_fps", "rss_peak_bytes", "heartbeat_max_ms",
                     "decode_drops_per_second"):
            self.assertNotIn(name, metrics)
        self.assertIn("scene_frame_time_p95_ms", metrics)

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
