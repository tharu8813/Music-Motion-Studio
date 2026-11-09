"""Tests for moving real Qt source layers and H.264 full-frame parity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from tools.native_canvas_timeline_probe import (
    TimelineValidationError, capture_timeline, load_timeline,
    verify_video_parity,
)


class QtCanvasDynamicTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_qt_source_changes_and_strict_order(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "frames"
            timeline = capture_timeline(folder, frames=6, fps=60, scale=0.25)
            meta, references = load_timeline(timeline)
            self.assertEqual((meta["width"], meta["height"]), (320, 180))
            self.assertEqual([x["pts"] for x in meta["frames"]], list(range(6)))
            self.assertEqual([x["cue_index"] for x in meta["frames"]], [0, 0, 1, 1, 2, 2])
            self.assertEqual(len(references), 6)
            self.assertEqual(len({x["reference_sha256"] for x in meta["frames"]}), 6)
            self.assertEqual(len({x["manifest"] for x in meta["frames"]}), 6)
            with self.assertRaisesRegex(TimelineValidationError, "Refusing nonempty"):
                capture_timeline(folder, frames=6, scale=0.25)

    def test_corrupt_sha_and_pts_fail_before_any_gpu_use(self):
        with TemporaryDirectory() as tmp:
            timeline = capture_timeline(Path(tmp) / "frames", frames=4, scale=0.25)
            meta, _ = load_timeline(timeline)
            tampered = dict(meta)
            tampered["frames"] = [dict(f) for f in meta["frames"]]
            tampered["frames"][2]["pts"] = 1
            timeline.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(TimelineValidationError, "PTS or cue"):
                load_timeline(timeline)
            timeline.write_text(json.dumps(meta), encoding="utf-8")
            reference = timeline.parent / "reference-0000.png"
            reference.write_bytes(reference.read_bytes() + b"tampered")
            with self.assertRaisesRegex(TimelineValidationError, "SHA-256"):
                load_timeline(timeline)


class QtCanvasDynamicVideoVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("Software H264/FFmpeg not available")
        adjacent = Path(cls.ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        cls.ffprobe = str(adjacent) if adjacent.is_file() else shutil.which("ffprobe")
        if not cls.ffprobe:
            raise unittest.SkipTest("ffprobe not available")

    def test_soft_h264_matches_all_six_moving_qt_frames(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp) / "frames"
            timeline = capture_timeline(folder, frames=6, fps=60, scale=0.25)
            video = folder / "soft-reference.h264"
            cmd = [
                self.ffmpeg, "-v", "error", "-nostdin", "-y",
                "-framerate", "60", "-i", str(folder / "reference-%04d.png"),
                "-frames:v", "6", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p",
                "-x264-params",
                "bframes=0:colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=on",
                "-color_range", "pc",
                "-colorspace", "bt709",
                "-color_trc", "bt709",
                "-color_primaries", "bt709",
                "-f", "h264", str(video),
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0,
                             result.stderr.decode("utf8", "replace")[-1500:])
            report = verify_video_parity(
                timeline, video, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
            )
            self.assertEqual(report["frames_compared"], 6)
            self.assertEqual(report["status"], "verified")
            self.assertTrue(report["full_frame_lossy_parity_verified"])
            self.assertLess(report["worst_mae_codes"], 5.0)
            video.unlink()
            # The decoder must not mistake a missing bitstream for an
            # acceptable hardware-unavailable outcome.
            with self.assertRaisesRegex(TimelineValidationError, "Missing or empty"):
                verify_video_parity(timeline, video,
                                    ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)


if __name__ == "__main__":
    unittest.main()
