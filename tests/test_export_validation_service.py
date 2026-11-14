import json
from concurrent.futures import CancelledError
from subprocess import CompletedProcess
from threading import Event
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.export_validation_service import (
    EXPORT_FPS_OPTIONS,
    select_export_work_mode,
    validate_audio_export_output,
    validate_export_output,
)


class ExportValidationServiceTests(unittest.TestCase):
    def test_cancellation_is_not_reported_as_unavailable_metadata(self):
        with patch("app.services.export_validation_service._find_ffprobe", return_value=Path("ffprobe")), \
             patch("app.services.export_validation_service.run_probe", side_effect=CancelledError()):
            with self.assertRaises(CancelledError):
                validate_export_output("video.mp4", expected_width=64, expected_height=64,
                                       expected_fps=30, cancel_event=Event())

    def test_fps_options_are_playlist_friendly(self) -> None:
        self.assertEqual(EXPORT_FPS_OPTIONS, (24, 30, 50, 60))

    def test_work_mode_scales_with_export_cost(self) -> None:
        with patch("app.services.export_validation_service.os.cpu_count", return_value=8):
            self.assertEqual(select_export_work_mode(3840, 2160, 60), "stable")
            self.assertEqual(select_export_work_mode(1920, 1080, 30), "max_speed")
        with patch("app.services.export_validation_service.os.cpu_count", return_value=4):
            self.assertEqual(select_export_work_mode(1920, 1080, 30), "auto")

    def test_probe_reports_mismatch_without_reencoding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "video.mp4"
            output.touch()
            payload = {
                "streams": [{
                    "width": 1920, "height": 1080, "avg_frame_rate": "60/1",
                    "nb_frames": "120", "codec_name": "h264", "pix_fmt": "yuv420p",
                }],
                "format": {"duration": "2.0"},
            }
            completed = type("Completed", (), {
                "stdout": json.dumps(payload), "stderr": "", "returncode": 0,
            })()
            with patch("app.services.export_validation_service._find_ffprobe", return_value=Path("ffprobe")), \
                 patch("app.services.export_validation_service.subprocess.run", return_value=completed):
                result = validate_export_output(
                    output, expected_width=1920, expected_height=1080,
                    expected_fps=30, expected_duration_seconds=2.0,
                )
        self.assertTrue(result.available)
        self.assertFalse(result.passed)
        self.assertIn("FPS", result.warnings[0])

    def test_video_probe_uses_nominal_rate_when_average_or_frame_count_is_unavailable(self):
        payload = {
            "streams": [{"width": 640, "height": 360, "avg_frame_rate": "0/0",
                         "r_frame_rate": "30000/1001", "nb_frames": "N/A"}],
            "format": {"duration": "2.0"},
        }
        completed = CompletedProcess([], 0, json.dumps(payload), "")
        with patch("app.services.export_validation_service._find_ffprobe", return_value=Path("ffprobe")), \
             patch("app.services.export_validation_service.subprocess.run", return_value=completed):
            result = validate_export_output(
                "video.mkv", expected_width=640, expected_height=360, expected_fps=30,
                expected_duration_seconds=2,
            )
        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.fps, 30000 / 1001)
        self.assertEqual(result.frame_count, 0)

    def test_video_probe_rejects_missing_or_malformed_video_streams(self):
        for streams in ([], [{}], {"unexpected": "shape"}, None):
            with self.subTest(streams=streams):
                payload = {"streams": streams, "format": {"duration": "2.0"}}
                completed = CompletedProcess([], 0, json.dumps(payload), "")
                with patch("app.services.export_validation_service._find_ffprobe", return_value=Path("ffprobe")), \\
                     patch("app.services.export_validation_service.subprocess.run", return_value=completed):
                    result = validate_export_output(
                        "no-video.mp4", expected_width=640, expected_height=360,
                        expected_fps=30, expected_duration_seconds=2,
                    )
                self.assertFalse(result.available)
                self.assertFalse(result.passed)
                self.assertEqual(result.error, "FFprobe found no video stream.")

    def test_video_probe_rejects_unexpected_json_root(self):
        completed = CompletedProcess([], 0, "[]", "")
        with patch("app.services.export_validation_service._find_ffprobe", return_value=Path("ffprobe")), \\
             patch("app.services.export_validation_service.subprocess.run", return_value=completed):
            result = validate_export_output("bad-video.mp4", expected_width=640,
                                            expected_height=360, expected_fps=30)
        self.assertFalse(result.available)
        self.assertEqual(result.error, "FFprobe returned invalid video metadata.")

    def test_failed_probe_surfaces_its_error_for_video_and_audio(self):
        completed = CompletedProcess([], 1, "{}", "Invalid data found\n")
        with patch("app.services.export_validation_service._find_ffprobe", return_value=Path("ffprobe")), \
             patch("app.services.export_validation_service.subprocess.run", return_value=completed):
            video = validate_export_output(
                "video.mp4", expected_width=640, expected_height=360, expected_fps=30,
            )
            audio = validate_audio_export_output("audio.mp3", 2)
        for result in (video, audio):
            self.assertFalse(result.available)
            self.assertEqual(result.error, "Invalid data found")


if __name__ == "__main__":
    unittest.main()
