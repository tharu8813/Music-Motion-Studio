"""Standard-library verification of the P1 full-frame Rust GPU benchmark."""

from __future__ import annotations

from argparse import Namespace
import hashlib
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import native_gpu_p1_benchmark as benchmark


def case(label: str, *, video: str = "same", audio: str = "same",
         pts: str = "same", wall: float = 40.0) -> dict:
    return {
        "label": label, "wall_seconds": wall,
        "media": {"width": 1920, "height": 1080, "fps": 60,
                  "frames": 1800, "pts_sha256": pts},
        "decoded_video_sha256": video,
        "decoded_pcm_sha256": audio, "stage_timings": {},
    }


class NativeGpuP1BenchmarkTests(unittest.TestCase):
    def test_transport_abba_compares_full_decoded_streams_and_wall(self) -> None:
        runs = [case("a1", wall=40), case("b1", wall=32),
                case("b2", wall=34), case("a2", wall=42)]
        report = benchmark._summarize(runs)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["baseline_mean_seconds"], 41)
        self.assertEqual(report["candidate_mean_seconds"], 33)
        self.assertEqual(report["wall_time_reduction_percent"], 19.51)
        self.assertIn("overlap", report["timing_notice"])

    def test_rejects_video_audio_and_pts_mismatches(self) -> None:
        runs = [case("a1"), case("b1", video="changed"),
                case("b2", audio="changed"), case("a2", pts="changed")]
        report = benchmark._summarize(runs)
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(len(report["mismatches"]), 3)

    def test_rejects_partial_or_misordered_benchmark(self) -> None:
        with self.assertRaisesRegex(ValueError, "four ordered"):
            benchmark._summarize([case("a1"), case("b1")])
        with self.assertRaisesRegex(ValueError, "four ordered"):
            benchmark._summarize([case("b1"), case("a1"), case("b2"), case("a2")])

    def test_command_keeps_same_project_profile_and_changes_only_ab_flags(self) -> None:
        args = Namespace(project=Path("project.pvsproj"),
                         native_exe=Path("gpu.exe"), output_dir=Path("new"),
                         fps=60, seconds=30, scale=1.5, ffmpeg="ffmpeg")
        before = benchmark.build_command(args, "a1")
        after = benchmark.build_command(args, "b1")
        self.assertIn("tools.native_gpu_full_export", before)
        self.assertIn("--no-gpu-shared-packet-transport", before)
        self.assertIn("--no-gpu-parallel-packet-validation", before)
        self.assertNotIn("--no-gpu-shared-packet-transport", after)
        self.assertNotIn("--no-gpu-parallel-packet-validation", after)
        for flag in ("--project", "--native-exe", "--fps", "--scale",
                     "--smoke-seconds", "--gpu-profile"):
            i, j = before.index(flag), after.index(flag)
            self.assertEqual(before[i + 1], after[j + 1])

    def test_alpha_trim_abba_preserves_all_other_gpu_settings(self) -> None:
        args = Namespace(project=Path("giggles.pvsproj"),
                         native_exe=Path("native.exe"), output_dir=Path("fresh"),
                         fps=60, seconds=30, scale=1.5,
                         ffmpeg="ffmpeg", experiment="alpha_trim")
        before = benchmark.build_command(args, "a1")
        after = benchmark.build_command(args, "b1")
        expected = [part.replace("a1.mp4", "b1.mp4").replace("a1.json", "b1.json")
                    for part in before] + ["--gpu-trim-transparent-bands"]
        self.assertEqual(after, expected)
        self.assertNotIn("--no-gpu-shared-packet-transport", before)
        self.assertNotIn("--no-gpu-parallel-packet-validation", before)
        self.assertNotIn("--gpu-trim-transparent-bands", before)
        with self.assertRaisesRegex(ValueError, "Unknown P1 experiment"):
            benchmark.build_command(
                Namespace(**{**vars(args), "experiment": "unsupported"}), "b1",
            )

    def test_raw_media_hash_never_reads_uncompressed_stream_into_python(self) -> None:
        digest = "c" * 64
        with patch.object(benchmark, "_execute", return_value=f"SHA256={digest}\n") as run:
            value = benchmark._hash_stream("ffmpeg", Path("out.mp4"), "video")
        self.assertEqual(value, digest)
        command = run.call_args.args[0]
        self.assertIn("-f", command)
        self.assertEqual(command[command.index("-f") + 1], "hash")
        self.assertIn("-pix_fmt", command)
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        with patch.object(benchmark, "_execute", return_value=f"SHA256={digest}\n") as run:
            self.assertEqual(benchmark._hash_stream("ffmpeg", Path("out.mp4"), "audio"), digest)
        self.assertIn("pcm_s16le", run.call_args.args[0])

    def test_which_pts_are_hashed_is_complete_and_order_sensitive(self) -> None:
        import json
        # H.264 first frames may carry SEI side data. ffprobe CSV yields
        # "0," but its JSON frame PTS remains a valid integer.
        frames = {"frames": [
            {"best_effort_timestamp": 0, "side_data_list": [{}]},
            {"best_effort_timestamp": 512},
            {"best_effort_timestamp": 1024},
        ]}
        with patch.object(benchmark, "_execute", return_value=json.dumps(frames)):
            n, hash_value = benchmark._video_pts("ffprobe", Path("a.mp4"))
        self.assertEqual(n, 3)
        self.assertEqual(hash_value, hashlib.sha256(b"0\n512\n1024\n").hexdigest())
        frames["frames"][1]["best_effort_timestamp"] = 1024
        frames["frames"][2]["best_effort_timestamp"] = 512
        with patch.object(benchmark, "_execute", return_value=json.dumps(frames)):
            _, changed = benchmark._video_pts("ffprobe", Path("b.mp4"))
        self.assertNotEqual(hash_value, changed)
        frames["frames"][1].pop("best_effort_timestamp")
        with patch.object(benchmark, "_execute", return_value=json.dumps(frames)):
            with self.assertRaisesRegex(ValueError, "presentation timestamp"):
                benchmark._video_pts("ffprobe", Path("broken.mp4"))

    def test_media_validation_requires_complete_cfr_video_and_audio(self) -> None:
        import json
        fixture = {
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080,
                 "avg_frame_rate": "60/1", "nb_read_frames": "1800"},
                {"codec_type": "audio"},
            ], "format": {"duration": "30.00"},
        }
        with patch.object(benchmark, "_execute", return_value=json.dumps(fixture)), \
                patch.object(benchmark, "_video_pts", return_value=(1800, "sha")):
            found = benchmark._probe_media("ffprobe", Path("a.mp4"), width=1920,
                                           height=1080, fps=60, seconds=30)
        self.assertEqual(found["frames"], 1800)
        fixture["streams"][0]["nb_read_frames"] = "1799"
        with patch.object(benchmark, "_execute", return_value=json.dumps(fixture)):
            with self.assertRaisesRegex(ValueError, "Frame count"):
                benchmark._probe_media("ffprobe", Path("broken.mp4"), width=1920,
                                       height=1080, fps=60, seconds=30)

    def test_real_ffmpeg_decoded_hash_and_all_pts(self) -> None:
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG")
        if not ffmpeg:
            self.skipTest("Only runs after verified FFmpeg is installed by Windows CI")
        ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe"))
        if not Path(ffprobe).is_file():
            self.skipTest("Verified FFmpeg package has no ffprobe binary")
        with TemporaryDirectory(prefix="mms-p1-media-") as raw:
            root = Path(raw)
            output = root / "reference.mp4"
            subprocess.run([
                ffmpeg, "-hide_banner", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "1", "-c:v", "libx264", "-c:a", "aac",
                str(output),
            ], check=True, capture_output=True, timeout=30)
            media = benchmark._probe_media(
                ffprobe, output, width=64, height=64, fps=30, seconds=1,
            )
            self.assertEqual(media["frames"], 30)
            self.assertEqual(benchmark._hash_stream(ffmpeg, output, "video"),
                             benchmark._hash_stream(ffmpeg, output, "video"))
            self.assertEqual(benchmark._hash_stream(ffmpeg, output, "audio"),
                             benchmark._hash_stream(ffmpeg, output, "audio"))
            self.assertEqual(len(media["pts_sha256"]), 64)


    def test_preflight_rejects_720p_scale_before_four_gpu_exports(self) -> None:
        from types import SimpleNamespace
        canvas = SimpleNamespace(width=1280, height=720)
        project = SimpleNamespace(canvas=canvas)
        with patch("app.services.project_service.ProjectService.load", return_value=project) as load:
            with self.assertRaisesRegex(
                ValueError, r"produces 1280x720, not the required 1920x1080.*--scale 1\.5",
            ):
                benchmark.verify_output_geometry(Path("giggles.pvsproj"), 1.0, 1920, 1080)
            self.assertEqual(
                benchmark.verify_output_geometry(Path("giggles.pvsproj"), 1.5, 1920, 1080),
                (1920, 1080),
            )
        self.assertEqual(load.call_count, 2)

    def test_preflight_rejects_aspect_mismatch_without_fabricated_scale(self) -> None:
        from types import SimpleNamespace
        project = SimpleNamespace(canvas=SimpleNamespace(width=1280, height=800))
        with patch("app.services.project_service.ProjectService.load", return_value=project):
            with self.assertRaisesRegex(ValueError, "aspect ratio differs"):
                benchmark.verify_output_geometry(Path("mismatched.json"), 1.5, 1920, 1080)


    def test_native_stages_are_separate_and_not_summed(self) -> None:
        stages = benchmark._native_stages({
            "frame_production_seconds": 20,
            "frame_ack_wait_seconds": 10,
            "raster_cache": {"qt_canvas_paint_stage_seconds": 14,
                             "qt_canvas_state_setup_seconds": 9},
        })
        self.assertEqual(stages["qt_canvas_paint_stage_seconds"], 14)
        self.assertEqual(stages["frame_ack_wait_seconds"], 10)
        self.assertNotIn("stage_sum_seconds", stages)


if __name__ == "__main__":
    unittest.main()
