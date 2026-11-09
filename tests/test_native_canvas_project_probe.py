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
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.animation.keyframes import Keyframe, evaluate as evaluate_keyframes
from app.models.playlist import PlaylistTrack
from app.models.project import CanvasSettings, ProjectDocument, ProjectSettings
from app.models.source import Source, SourceType
from tools.native_canvas_project_probe import (
    capture_project, resolve_project_frame, validate_project_for_gpu_capture,
    main as project_probe_main, unsupported_visible_sources,
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

    document = ProjectDocument(
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
    # An actual persisted global transform curve must survive loading and
    # be evaluated from the absolute compiled timeline clock.
    document.sources[1].keyframes.global_curves["x"] = [
        Keyframe(0.0, 140.0, "linear"),
        Keyframe(1.0, 340.0, "linear"),
    ]
    return document


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

    def test_project_keyframe_is_evaluated_on_absolute_audio_clock(self):
        project = project_fixture()
        source = project.sources[1]
        self.assertTrue(source.keyframes.animated)
        earlier = resolve_project_frame(project, 29 / 60)
        later = resolve_project_frame(project, 30 / 60)
        x1 = evaluate_keyframes(
            source, earlier["timeline_seconds"], earlier["track"].id,
            earlier["track_elapsed_seconds"],
        )["x"]
        x2 = evaluate_keyframes(
            source, later["timeline_seconds"], later["track"].id,
            later["track_elapsed_seconds"],
        )["x"]
        self.assertAlmostEqual(x1, 140 + 200 * 29 / 60, places=4)
        self.assertAlmostEqual(x2, 240.0, places=4)
        self.assertGreater(x2, x1)

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

    def test_canvas_only_explicitly_omits_audio_visualizer_and_particles(self):
        with TemporaryDirectory() as tmp:
            project = project_fixture()
            visualizer = Source(
                SourceType.AUDIO_VISUALIZER, "Giggles spectrum",
                z_index=12, width=800, height=130,
            )
            particles = Source(
                SourceType.PARTICLE_OVERLAY, "Giggles particles",
                z_index=22, width=1280, height=720,
            )
            project.sources.extend([visualizer, particles])
            source = Path(tmp) / "real-project.json"
            source.write_text(json.dumps(project.to_dict()), encoding="utf8")
            snapshot = source.read_bytes()
            with self.assertRaisesRegex(
                TimelineValidationError, "audio_visualizer, particle_overlay"
            ):
                capture_project(
                    source, Path(tmp) / "strict", frames=6, scale=0.25,
                )
            self.assertFalse((Path(tmp) / "strict").exists())
            timeline = capture_project(
                source, Path(tmp) / "partial",
                frames=6, scale=0.25, source_policy="canvas-only",
            )
            meta, references = load_timeline(timeline)
            self.assertEqual(meta["capture_scope"], "partial_canvas_only")
            self.assertEqual(meta["excluded_source_count"], 2)
            self.assertEqual(
                {x["source_type"] for x in meta["excluded_sources"]},
                {"audio_visualizer", "particle_overlay"},
            )
            self.assertEqual(meta["source_count"], 4)
            self.assertFalse(meta["full_project_visual_parity_verified"])
            self.assertEqual(len(references), 6)
            self.assertEqual(source.read_bytes(), snapshot)
            frame = json.loads((timeline.parent / "frame-0000.json").read_text("utf8"))
            self.assertEqual(len(frame["layers"]), 2)

    def test_capture_failure_never_launches_native_rust(self):
        with TemporaryDirectory() as tmp:
            project = project_fixture()
            project.sources.append(
                Source(SourceType.AUDIO_VISUALIZER, "Spectrum")
            )
            source = Path(tmp) / "project.json"
            source.write_text(json.dumps(project.to_dict()), encoding="utf8")
            with mock.patch(
                "tools.native_canvas_project_probe.subprocess.run"
            ) as runner:
                rc = project_probe_main([
                    "--project", str(source),
                    "--output-dir", str(Path(tmp) / "out"),
                    "--encode-and-verify",
                ])
                self.assertEqual(rc, 1)
                runner.assert_not_called()
            self.assertFalse((Path(tmp) / "out").exists())

    def test_invalid_exclusion_disclosure_is_rejected(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            project = project_fixture()
            project.sources.append(
                Source(SourceType.PARTICLE_OVERLAY, "Particles")
            )
            path.write_text(json.dumps(project.to_dict()), encoding="utf8")
            timeline = capture_project(
                path, Path(tmp) / "frames",
                frames=6, scale=0.25, source_policy="canvas-only",
            )
            meta = json.loads(timeline.read_text("utf8"))
            meta["excluded_sources"][0]["reason"] = "silently_skipped"
            timeline.write_text(json.dumps(meta), encoding="utf8")
            with self.assertRaisesRegex(
                TimelineValidationError, "Invalid excluded source metadata"
            ):
                load_timeline(timeline)

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
