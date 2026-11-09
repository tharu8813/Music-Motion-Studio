"""Real PlaylistTrack lyric timing -> Qt BGRA -> H264 parity regression tests."""
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

from app.models.playlist import PlaylistTrack
from app.preview.frame_state import resolve_lyrics_cue_state
from tools.native_canvas_real_lyrics_probe import (
    capture_real_lyrics, demo_track, load_track,
)
from tools.native_canvas_timeline_probe import (
    TimelineValidationError, load_timeline, verify_video_parity,
)


class RealTrackLyricPacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_ttml_overlap_resolver_and_state_per_frame(self):
        track = demo_track()
        expected = [[0], [0], [0, 1], [0, 1], [0, 2], [2]]
        observed = []
        for i in range(6):
            state = resolve_lyrics_cue_state(
                track, i / 60, 0.0, 0.0, "none", 0.36,
                whole_lines=True,
            )
            observed.append(list(state.active_cue_indices))
        self.assertEqual(observed, expected)

    def test_real_lyric_canvas_z_layers_keep_timecoded_cues(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp) / "lyrics"
            manifest = capture_real_lyrics(directory, frames=6, fps=60, scale=0.25)
            data, pngs = load_timeline(manifest)
            self.assertEqual(data["kind"], "track_lyrics")
            self.assertTrue(data["uses_actual_lyrics_cue_resolver"])
            self.assertEqual([f["pts"] for f in data["frames"]], list(range(6)))
            self.assertEqual(
                [f["active_cue_indices"] for f in data["frames"]],
                [[0], [0], [0, 1], [0, 1], [0, 2], [2]],
            )
            self.assertEqual(len({f["reference_sha256"] for f in data["frames"]}), 6)
            self.assertEqual(len(pngs), 6)
            self.assertEqual((data["width"], data["height"]), (320, 180))
            with self.assertRaisesRegex(TimelineValidationError, "Refusing nonempty"):
                capture_real_lyrics(directory, frames=6, scale=0.25)

    def test_track_json_and_fail_closed_before_gpu(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "track.json"
            track = demo_track()
            path.write_text(json.dumps(track.to_dict()), encoding="utf-8")
            self.assertEqual(len(load_track(path).lyrics), 3)
            timeline = capture_real_lyrics(
                Path(temp) / "captured", track_json=path,
                frames=6, scale=0.25,
            )
            data, _ = load_timeline(timeline)
            mutated = dict(data)
            mutated["frames"] = [dict(f) for f in data["frames"]]
            mutated["frames"][2]["track_elapsed_seconds"] += 1
            timeline.write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaisesRegex(TimelineValidationError, "cue state or clock"):
                load_timeline(timeline)
            with self.assertRaisesRegex(TimelineValidationError, "extends beyond"):
                capture_real_lyrics(
                    Path(temp) / "invalid", track_json=path, start_seconds=0.49,
                )


class RealTrackLyricSoftwareParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg unavailable")
        ffprobe_path = Path(cls.ffmpeg).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        )
        cls.ffprobe = str(ffprobe_path) if ffprobe_path.is_file() else shutil.which("ffprobe")
        if not cls.ffprobe:
            raise unittest.SkipTest("FFprobe unavailable")

    def test_real_qt_lyric_timing_survives_h264_encode_decode(self):
        with TemporaryDirectory() as temp:
            directory = Path(temp) / "lyrics"
            timeline = capture_real_lyrics(directory, frames=6, scale=0.25)
            target = directory / "software.h264"
            command = [
                self.ffmpeg, "-v", "error", "-nostdin", "-y",
                "-framerate", "60",
                "-i", str(directory / "reference-%04d.png"),
                "-frames:v", "6", "-c:v", "libx264",
                "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-x264-params",
                "bframes=0:colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=on",
                "-color_range", "pc", "-colorspace", "bt709",
                "-color_trc", "bt709", "-color_primaries", "bt709",
                "-f", "h264", str(target),
            ]
            result = subprocess.run(command, capture_output=True, timeout=60)
            self.assertEqual(
                result.returncode, 0,
                result.stderr.decode("utf-8", "replace")[-1500:],
            )
            report = verify_video_parity(
                timeline, target, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
            )
            self.assertEqual(report["frames_compared"], 6)
            self.assertTrue(report["decoded_frame_changes_verified"])
            self.assertTrue(report["full_frame_lossy_parity_verified"])


if __name__ == "__main__":
    unittest.main()
