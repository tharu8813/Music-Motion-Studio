import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.export_validation_service import (
    EXPORT_FPS_OPTIONS,
    select_export_work_mode,
    validate_export_output,
)


class ExportValidationServiceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
