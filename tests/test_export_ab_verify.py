"""Pure standard-library tests for export A/B performance and video verification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools.export_ab_verify import (
    _read_profile, compare_frame_samples, compare_media_metadata,
    compare_profiles, inspect_video, main, render_markdown, sample_frame_hash,
)
from tools.export_bottleneck_profile import run_configuration


def profile(*, renderer: float, ffmpeg_cpu: float, canvas_cpu: float, peak: float,
            flags: dict | None = None) -> dict:
    return {
        "schema": 1,
        "capture_status": "export_observed",
        "summary": {
            "observed_cpu_core_seconds": {
                "ffmpeg": ffmpeg_cpu, "canvas_workers": canvas_cpu,
                "editor": 10.0, "other": 0.0,
            },
            "peak_process_tree_rss_mib": peak,
            "device_nvenc_mean_percent": 18.0,
            "stage_timings": {
                "export.renderer_seconds": {
                    "calls": 1, "inclusive_wall_seconds": renderer,
                },
                "export.frame_delivery_seconds": {
                    "calls": 18000, "inclusive_wall_seconds": 3 * renderer,
                },
            },
        },
        "run_configuration": {
            "python_version": "3.14.0",
            "optimization_environment": flags if flags is not None else {},
        },
    }


def media(*, duration=10.0, fps="60/1", frames=600, width=1920,
          height=1080, audio=True) -> dict:
    return {
        "video_codec": "h264",
        "width": width, "height": height, "fps": fps,
        "frame_count": frames,
        "duration_seconds": duration, "video_duration_seconds": duration,
        "pix_fmt": "yuv420p",
        "sample_aspect_ratio": "1:1",
        "audio_stream_count": int(audio),
        "audio": [{
            "codec": "aac", "sample_rate": "48000", "channels": 2,
            "duration_seconds": duration,
        }] if audio else [],
        "bytes": 1_000_000,
    }


class ExportABVerificationTests(unittest.TestCase):
    def test_rejects_failed_or_idle_profiles(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            example = profile(renderer=200, ffmpeg_cpu=500, canvas_cpu=300, peak=2000)
            for status in ("target_failed", "no_export_observed"):
                example["capture_status"] = status
                path.write_text(json.dumps(example), encoding="utf-8")
                with self.subTest(status=status), self.assertRaisesRegex(ValueError, "Export not verified"):
                    _read_profile(path)

    def test_rejects_incomplete_video_exports_when_timer_is_available(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cancelled.json"
            example = profile(renderer=100, ffmpeg_cpu=50, canvas_cpu=80, peak=1024)
            example["summary"]["stage_timings"]["export.total_wall_seconds"] = {
                "calls": 1, "inclusive_wall_seconds": 180,
            }
            example["summary"]["stage_timings"]["export.incomplete_wall_seconds"] = {
                "calls": 1, "inclusive_wall_seconds": 180,
            }
            path.write_text(json.dumps(example), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "No successfully completed video export"):
                _read_profile(path)

    def test_compares_real_renderer_time_and_labels_parallel_stages(self) -> None:
        previous = profile(
            renderer=300, ffmpeg_cpu=1200, canvas_cpu=700, peak=1500,
            flags={"PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH": "1"},
        )
        current = profile(
            renderer=240, ffmpeg_cpu=950, canvas_cpu=620, peak=1370,
            flags={"PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH": None},
        )
        report = compare_profiles(previous, current)
        self.assertEqual(report["renderer_wall_seconds"]["change_percent"], -20.0)
        self.assertEqual(
            report["stage_timings_seconds"]["export.frame_delivery_seconds"]["change_percent"], -20.0,
        )
        self.assertTrue(
            report["stage_timings_seconds"]["export.frame_delivery_seconds"]["inclusive_or_parallel"]
        )
        self.assertEqual(report["observed_cpu_core_seconds"]["ffmpeg"]["change_percent"], -20.83)
        self.assertEqual(report["peak_process_tree_rss_mib"]["change_percent"], -8.67)
        self.assertIn("FFmpeg renderer wall time", render_markdown({
            "verdict": "measurement_only", "profile_comparison": report,
        }))

    def test_end_to_end_export_wall_time_takes_priority_over_renderer(self) -> None:
        prior = profile(renderer=290, ffmpeg_cpu=1175, canvas_cpu=694, peak=2400)
        current = profile(renderer=250, ffmpeg_cpu=1015, canvas_cpu=650, peak=2310)
        prior["summary"]["stage_timings"]["export.completed_wall_seconds"] = {
            "calls": 1, "inclusive_wall_seconds": 360.0,
        }
        current["summary"]["stage_timings"]["export.completed_wall_seconds"] = {
            "calls": 1, "inclusive_wall_seconds": 300.0,
        }
        comparison = compare_profiles(prior, current)
        self.assertEqual(comparison["total_export_wall_seconds"]["change_percent"], -16.67)
        self.assertNotEqual(comparison["total_export_wall_seconds"],
                            comparison["renderer_wall_seconds"])
        document = render_markdown({
            "verdict": "measurement_only", "profile_comparison": comparison,
        })
        self.assertIn("Completed video export wall time", document)

    def test_identical_flags_or_missing_run_metadata_generate_warnings(self) -> None:
        original = profile(renderer=5, ffmpeg_cpu=5, canvas_cpu=5, peak=5)
        compared = compare_profiles(original, original)
        self.assertTrue(any("same optimization flags" in w for w in compared["warnings"]))
        del original["run_configuration"]
        compared = compare_profiles(original, profile(renderer=5, ffmpeg_cpu=5, canvas_cpu=5, peak=5))
        self.assertTrue(any("lacks optimization settings" in w for w in compared["warnings"]))

    def test_zero_baseline_does_not_create_fake_improvement(self) -> None:
        prior = profile(renderer=0, ffmpeg_cpu=0, canvas_cpu=0, peak=0)
        now = profile(renderer=3, ffmpeg_cpu=3, canvas_cpu=3, peak=3)
        self.assertIsNone(compare_profiles(prior, now)["renderer_wall_seconds"]["change_percent"])

    def test_media_metadata_consistency_and_structural_regressions(self) -> None:
        old = media()
        self.assertTrue(compare_media_metadata(old, media())["passed"])
        for new in (
            media(width=1280), media(fps="30/1"), media(frames=599),
            media(duration=9.0), media(audio=False),
        ):
            with self.subTest(new=new):
                check = compare_media_metadata(old, new)
                self.assertFalse(check["passed"])
                self.assertTrue(check["failures"])
        missing_frames = compare_media_metadata(
            {**old, "frame_count": None},
            {**media(), "frame_count": None},
        )
        self.assertTrue(missing_frames["passed"])
        self.assertTrue(any("not verified" in w for w in missing_frames["warnings"]))

    def test_ffprobe_parses_streams_without_full_video_decode(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "sample.mp4"
            path.write_bytes(b"file")
            probe = {
                "format": {"duration": "10.0"},
                "streams": [
                    {"codec_type": "video", "codec_name": "h264",
                     "width": 64, "height": 48, "nb_frames": "300",
                     "avg_frame_rate": "30/1", "sample_aspect_ratio": "1:1"},
                    {"codec_type": "audio", "codec_name": "aac",
                     "sample_rate": "44100", "channels": 2, "duration": "10.0"},
                ],
            }
            with patch("tools.export_ab_verify._run", return_value=json.dumps(probe).encode()) as run:
                info = inspect_video(path, "ffprobe")
                self.assertEqual(info["frame_count"], 300)
                self.assertEqual(info["audio_stream_count"], 1)
                self.assertEqual(info["duration_seconds"], 10.0)
                self.assertEqual(info["bytes"], 4)
                self.assertIn("-show_streams", run.call_args.args[0])

    def test_frame_sampling_is_advisory_not_a_hard_failure(self) -> None:
        with patch("tools.export_ab_verify.sample_frame_hash", side_effect=[
            "same", "same", "different", "different", "old", "new",
        ]):
            result = compare_frame_samples(Path("before.mp4"), Path("after.mp4"), "ffmpeg", 200)
        self.assertEqual(result["status"], "review_samples")
        self.assertEqual(len(result["samples"]), 3)
        self.assertFalse(result["samples"][-1]["pixel_hash_equal"])
        self.assertIn("NOT automatic proof", result["warning"])

    def test_hashes_decoded_frame_bytes_not_compressed_file(self) -> None:
        with patch("tools.export_ab_verify._run", return_value=b"decoded pixels"):
            result = sample_frame_hash(Path("file.mp4"), "ffmpeg", 1.0)
            import hashlib
            self.assertEqual(result, hashlib.sha256(b"decoded pixels").hexdigest())

    def test_cli_writes_inspectable_json_and_markdown(self) -> None:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            before = root / "old.json"
            after = root / "new.json"
            before.write_text(json.dumps(profile(
                renderer=300, ffmpeg_cpu=1200, canvas_cpu=600, peak=1800,
                flags={"PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH": "1"},
            )), encoding="utf-8")
            after.write_text(json.dumps(profile(
                renderer=270, ffmpeg_cpu=1000, canvas_cpu=500, peak=1700,
                flags={"PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH": None},
            )), encoding="utf-8")
            json_path = root / "report.json"
            md_path = root / "report.md"
            result = main([
                "--baseline-profile", str(before),
                "--optimized-profile", str(after),
                "--output", str(json_path), "--markdown", str(md_path),
            ])
            self.assertEqual(result, 0)
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["verdict"], "measurement_only")
            self.assertIsNone(data["media_comparison"])
            self.assertEqual(data["profile_comparison"]["renderer_wall_seconds"]["change_percent"], -10)
            self.assertIn("FFmpeg renderer wall time", md_path.read_text(encoding="utf-8"))

    def test_real_ffprobe_and_ffmpeg_compare_identical_short_mp4(self) -> None:
        executable = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG")
        if not executable:
            self.skipTest("Real FFmpeg checks run after managed FFmpeg installation.")
        ffprobe = Path(executable).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        )
        if not ffprobe.is_file():
            self.skipTest("Managed FFmpeg installation does not include ffprobe.")
        with TemporaryDirectory(prefix="export-verify-media-") as directory:
            before = Path(directory) / "before.mp4"
            after = Path(directory) / "after.mp4"
            subprocess.run([
                executable, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=48000",
                "-t", "1", "-c:v", "mpeg4", "-c:a", "aac",
                str(before),
            ], check=True, capture_output=True, timeout=30)
            shutil.copyfile(before, after)
            a = inspect_video(before, str(ffprobe))
            b = inspect_video(after, str(ffprobe))
            self.assertTrue(compare_media_metadata(a, b)["passed"])
            self.assertEqual(a["width"], 64)
            self.assertEqual(a["audio_stream_count"], 1)
            comparison = compare_frame_samples(before, after, executable, a["duration_seconds"])
            self.assertEqual(comparison["status"], "identical_samples")

    def test_profile_run_configuration_contains_only_known_non_sensitive_flags(self) -> None:
        env = {
            "PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB": "1",
            "PLAYLIST_CANVAS_DISABLE_WORKER_LYRIC_CACHE": "1",
            "SECRET_TOKEN": "must-not-be-reported",
        }
        config = run_configuration(env)
        self.assertEqual(config["optimization_environment"]["PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB"], "1")
        self.assertNotIn("SECRET_TOKEN", str(config))
        self.assertIsNone(config["optimization_environment"]["PLAYLIST_CANVAS_DISABLE_IDENTITY_SCALE"])


if __name__ == "__main__":
    unittest.main()
