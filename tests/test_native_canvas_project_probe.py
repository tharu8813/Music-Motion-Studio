"""Isolated genuine-project Qt->NVENC packet timeline validation.

All tests work without NVIDIA hardware. The FFmpeg regression exercises
the same lossless reference PNG and H264 parity checker as the RTX test.
"""
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
from app.models.project import CanvasSettings, ProjectDocument, ProjectSettings
from app.models.source import Source, SourceType
from tools.native_canvas_project_probe import (
    capture_project, resolve_project_frame, validate_project_for_gpu_capture,
)
from tools.native_canvas_timeline_probe import (
    TimelineValidationError, load_timeline, verify_video_parity,
)


def project_fixture() -> ProjectDocument:
    def words(label: str, start: float, end: float) -> dict:
        return {
            "start": start, "end": end, "text": label, "format": "ttml",
            "words": [{"start": start, "end": end, "offset": 0, "text": label}],
        }

    return ProjectDocument(
        canvas=CanvasSettings(width=1280, height=720),
        settings=ProjectSettings(title="Actual project video path"),
        sources=[
            Source(SourceType.SHAPE, "Opaque background",
                   x=0, y=0, width=1280, height=720, z_index=0,
                   fill_color="#1C2C47"),
            Source(SourceType.PROGRESS_BAR, "Progress from actual track clock",
                   x=140, y=560, width=1020, height=42, z_index=3,
                   progress_mode="track"),
            Source(SourceType.TEXT, "Actual playlist track title",
                   x=130, y=140, width=1020, height=110, z_index=4,
                   text="%title%", font_size=54, fill_color="#FFFFFF"),
            Source(SourceType.LYRICS, "Actual TTML lyric source",
                   x=130, y=260, width=1020, height=230, z_index=5,
                   subtitle_animation="none", subtitle_intro_enabled=False,
                   subtitle_intro_midtrack=False,
                   subtitle_karaoke_enabled=True, subtitle_context_lines=0,
                   subtitle_next_lines=0, font_size=34,
                   fill_color="#FFFFFF"),
        ],
        playlist=[
            PlaylistTrack("", "First", duration_seconds=0.5, lyrics=[
                words("Old", 0.0, 0.49),
            ]),
            PlaylistTrack("", "Second", duration_seconds=0.5, lyrics=[
                words("Overlap long", 0.0, 0.3),
                words("Overlap short", 1/60, 0.12),
            ]),
        ],
    )


class RealProjectCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_frame_ownership_advances_at_track_boundary(self):
        project = project_fixture()
        plan = None
        first = resolve_project_frame(project, 29 / 60, plan=plan)
        second = resolve_project_frame(project, 30 / 60, plan=plan)
        self.assertEqual(first["track"].title, "First")
        self.assertEqual(second["track"].title, "Second")
        self.assertEqual(first["track_number"], 1)
        self.assertEqual(second["track_number"], 2)
        self.assertAlmostEqual(second["track_elapsed_seconds"], 0)
        with self.assertRaisesRegex(TimelineValidationError, "playlist presentation gap"):
            resolve_project_frame(project, 1.01)

    def test_real_project_source_and_lyric_clock_capture(self):
        with TemporaryDirectory() as tmp:
            project = project_fixture()
            path = Path(tmp) / "project.json"
            path.write_text(json.dumps(project.to_dict()), encoding="utf8")
            output = Path(tmp) / "frames"
            source = path.read_bytes()
            timeline = capture_project(
                path, output, start_seconds=28 / 60,
                frames=6, fps=60, scale=0.25,
            )
            doc, refs = load_timeline(timeline)
            self.assertEqual(doc["kind"], "project_timeline")
            self.assertEqual((doc["width"], doc["height"]), (320, 180))
            self.assertEqual(len(refs), 6)
            self.assertEqual([f["track_number"] for f in doc["frames"]],
                             [1, 1, 2, 2, 2, 2])
            self.assertEqual(
                doc["frames"][3]["lyric_states"][0]["active_cue_indices"],
                [0, 1],
            )
            self.assertEqual(len({
                f["reference_sha256"] for f in doc["frames"]
            }), 6)
            self.assertEqual(source, path.read_bytes())
            with self.assertRaisesRegex(TimelineValidationError, "Refusing nonempty"):
                capture_project(path, output, start_seconds=28 / 60,
                                frames=6, scale=0.25)

    def test_reject_unsupported_dynamic_sources_and_automix(self):
        project = project_fixture()
        project.sources.append(Source(SourceType.VIDEO, "Video source"))
        with self.assertRaisesRegex(TimelineValidationError, "dynamic sources"):
            validate_project_for_gpu_capture(project, scale=0.25)
        project.sources.pop()
        project.settings.transition_mode = "automix"
        with self.assertRaisesRegex(TimelineValidationError, "transition_mode=none"):
            validate_project_for_gpu_capture(project, scale=0.25)

    def test_tampered_project_clock_rejected(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.json"
            path.write_text(json.dumps(project_fixture().to_dict()), encoding="utf8")
            timeline = capture_project(
                path, Path(tmp) / "frames",
                start_seconds=28 / 60, frames=4, scale=0.25,
            )
            original = json.loads(timeline.read_text("utf8"))
            tampered = dict(original)
            tampered["frames"] = [dict(f) for f in original["frames"]]
            tampered["frames"][2]["track_elapsed_seconds"] = 15
            timeline.write_text(json.dumps(tampered), encoding="utf8")
            with self.assertRaisesRegex(TimelineValidationError, "Compiled project timeline"):
                load_timeline(timeline)


class ProjectSoftwareH264ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("Requires FFmpeg")
        adjacent = Path(cls.ffmpeg).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe",
        )
        cls.ffprobe = str(adjacent) if adjacent.is_file() else shutil.which("ffprobe")
        if not cls.ffprobe:
            raise unittest.SkipTest("Requires ffprobe")

    def test_project_source_title_lyrics_and_clock_survive_h264(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            path.write_text(json.dumps(project_fixture().to_dict()), encoding="utf8")
            directory = Path(tmp) / "frames"
            timeline = capture_project(
                path, directory, start_seconds=28 / 60,
                frames=6, fps=60, scale=0.25,
            )
            video = directory / "software.h264"
            result = subprocess.run([
                self.ffmpeg, "-v", "error", "-nostdin", "-y",
                "-framerate", "60", "-i", str(directory / "reference-%04d.png"),
                "-frames:v", "6", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-x264-params",
                "bframes=0:colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=on",
                "-color_range", "pc", "-colorspace", "bt709",
                "-color_trc", "bt709", "-color_primaries", "bt709",
                "-f", "h264", str(video),
            ], capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0,
                             result.stderr.decode("utf8", "replace")[-1500:])
            report = verify_video_parity(
                timeline, video, ffmpeg=self.ffmpeg, ffprobe=self.ffprobe,
            )
            self.assertEqual(report["frames_compared"], 6)
            self.assertTrue(report["decoded_frame_changes_verified"])
            self.assertTrue(report["full_frame_lossy_parity_verified"])


if __name__ == "__main__":
    unittest.main()
