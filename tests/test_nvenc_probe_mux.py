"""CPU-only tests for experimental H.264-to-MP4 mux/validation.

A synthetic FFmpeg H.264 source exercises timestamp creation, stream
copy, metadata, audio mux and overwrite safeguards. No NVIDIA GPU needed.
"""
from __future__ import annotations

from fractions import Fraction
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from tools.nvenc_probe_mux import (
    VerificationError, check_cfr_pts, check_video, parse_fps, remux,
)


class NvencMuxPureTests(unittest.TestCase):
    def test_fps_bounds_and_fraction(self):
        self.assertEqual(parse_fps("30000/1001"), Fraction(30000, 1001))
        self.assertEqual(parse_fps("60"), Fraction(60, 1))
        for invalid in ("0", "241", "-1", "oops", "60/0"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(VerificationError):
                    parse_fps(invalid)

    def test_pts_discontinuity_and_duplicate_detected(self):
        check_cfr_pts([0, 1 / 60, 2 / 60], Fraction(60), 3)
        for invalid in ([0, 0, 2 / 60], [0, 1 / 60, 3 / 60], []):
            with self.subTest(invalid=invalid):
                with self.assertRaises(VerificationError):
                    check_cfr_pts(invalid, Fraction(60), 3)

    def test_rejects_incorrect_color_metadata(self):
        source = {
            "codec_name": "h264", "width": 128, "height": 72,
            "nb_read_frames": "6", "color_range": "tv",
            "color_space": "bt709", "color_transfer": "bt709",
            "color_primaries": "bt709",
        }
        with self.assertRaisesRegex(VerificationError, "color_range"):
            check_video(source, frames=6, width=128, height=72)


class NvencMuxRealFFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "")
        cls.ffmpeg = configured or shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("Requires installed FFmpeg for real MP4 integration")
        cls.ffprobe = str(Path(cls.ffmpeg).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        ))
        if not Path(cls.ffprobe).is_file():
            cls.ffprobe = shutil.which("ffprobe") or ""
        if not cls.ffprobe:
            raise unittest.SkipTest("Requires ffprobe for real MP4 integration")

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "input.h264"
        result = subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-nostdin", "-y", "-f", "lavfi",
            "-i", "color=c=red:s=128x72:r=60",
            "-frames:v", "6", "-c:v", "libx264",
            "-preset", "ultrafast", "-x264-params", "bframes=0",
            "-pix_fmt", "yuv420p",
            "-color_range", "pc",
            "-colorspace", "bt709",
            "-color_trc", "bt709",
            "-color_primaries", "bt709",
            "-f", "h264", str(self.source),
        ], capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace")[-1500:])

    def test_remux_preserves_h264_frame_count_fps_and_color(self):
        output = self.root / "out.mp4"
        result = remux(
            self.source, output, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
            fps=Fraction(60), expected_frames=6,
        )
        self.assertTrue(output.is_file())
        self.assertEqual(result["frames_decoded"], 6)
        self.assertEqual(result["fps"], "60")
        self.assertFalse(result["video_reencoded"])
        self.assertAlmostEqual(result["duration_seconds"], 0.1, places=2)
        self.assertEqual(result["color"]["color_range"], "pc")
        self.assertEqual(result["color"]["color_space"], "bt709")
        self.assertIsNone(result["audio_codec"])

    def test_wrong_count_and_existing_output_fail_without_overwriting(self):
        output = self.root / "out.mp4"
        output.write_bytes(b"do-not-replace")
        with self.assertRaisesRegex(VerificationError, "Refusing to overwrite"):
            remux(self.source, output, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        self.assertEqual(output.read_bytes(), b"do-not-replace")
        output.unlink()
        with self.assertRaisesRegex(VerificationError, "Expected 9 source frames"):
            remux(self.source, output, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
                  expected_frames=9)
        self.assertFalse(output.exists())

    def test_optional_audio_is_aac_and_keeps_all_video_frames(self):
        soundtrack = self.root / "tone.wav"
        result = subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error",
            "-nostdin", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=48000:duration=0.12",
            "-c:a", "pcm_s16le", str(soundtrack),
        ], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace")[-1500:])
        output = self.root / "with-audio.mp4"
        report = remux(
            self.source, output, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
            fps=Fraction(60), expected_frames=6, audio=soundtrack,
        )
        self.assertEqual(report["frames_decoded"], 6)
        self.assertEqual(report["audio_codec"], "aac")
        self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
