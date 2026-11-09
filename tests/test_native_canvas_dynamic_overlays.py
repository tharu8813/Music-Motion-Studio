"""Real-audio native packet experiment, without requiring an NVIDIA GPU."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import struct
from tempfile import TemporaryDirectory
import unittest
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.native_canvas_layer_packets import NativeCanvasBgraLayer, NativeCanvasFrame
from app.timeline.compiler import compile_playlist
from tests.test_native_canvas_project_probe import project_fixture
from tools.native_canvas_dynamic_overlays import (
    build_dynamic_stack, interleave_dynamic_layers, real_audio_dynamic_frames,
)
from tools.native_canvas_project_probe import capture_project, validate_project_for_gpu_capture
from tools.native_canvas_timeline_probe import TimelineValidationError, load_timeline


def sample_wav(path: Path) -> None:
    """Short PCM sine; diagnostics analyze actual audio rather than sample UI bars."""
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"".join(
            struct.pack("<h", round(10000 * math.sin(2 * math.pi * 440 * i / 8000)))
            for i in range(8000)
        ))


def dynamic_project(path: Path):
    project = project_fixture()
    project.playlist = [PlaylistTrack(str(path), "Sine", duration_seconds=1.0)]
    project.sources.extend([
        Source(SourceType.AUDIO_VISUALIZER, "Actual spectrum",
               x=30, y=25, width=380, height=170, z_index=2),
        Source(SourceType.PARTICLE_OVERLAY, "Deterministic particles",
               x=40, y=40, width=400, height=260, z_index=6),
    ])
    return project


class NativeDynamicOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_dynamic_stacking_keeps_static_canvas_between_effects(self):
        project = dynamic_project(Path("not-needed.wav"))
        plan = build_dynamic_stack(project, 0.25)
        self.assertEqual(plan.source_ids, (project.sources[-2].id, project.sources[-1].id))
        self.assertEqual(plan.order, (
            ("canvas", 0), ("dynamic", 0), ("canvas", 1), ("dynamic", 1),
        ))
        self.assertEqual(plan.bands, ((None, 1.5), (2.5, 5.5)))
        self.assertEqual(validate_project_for_gpu_capture(
            project, scale=0.25, source_policy="dynamic-audio",
        ), [])
        with self.assertRaisesRegex(TimelineValidationError, "dynamic sources"):
            validate_project_for_gpu_capture(project, scale=0.25)

    def test_equal_z_and_unsupported_effects_fail_closed(self):
        project = dynamic_project(Path("not-needed.wav"))
        project.sources[-2].z_index = 3
        with self.assertRaisesRegex(ValueError, "equal-Z"):
            build_dynamic_stack(project, 0.25)
        project.sources[-2].z_index = 2
        project.sources.append(Source(SourceType.VIDEO, "Unsupported video", z_index=8))
        with self.assertRaisesRegex(TimelineValidationError, "cannot render"):
            validate_project_for_gpu_capture(project, scale=0.25, source_policy="dynamic-audio")
        project.sources.pop()
        project.sources[-2].rotation = 15
        with self.assertRaisesRegex(TimelineValidationError, "rotation"):
            validate_project_for_gpu_capture(project, scale=0.25, source_policy="dynamic-audio")

    def test_bgra_alpha_and_order_are_retained(self):
        project = dynamic_project(Path("not-needed.wav"))
        stack = build_dynamic_stack(project, 0.25)
        width, height = 320, 180
        base = NativeCanvasBgraLayer(
            "base", width, height, bytes((3, 2, 1, 255)) * (width * height),
        )
        foreground = NativeCanvasBgraLayer(
            "layer:0", width, height, bytes((0, 0, 0, 0)) * (width * height),
        )
        static = NativeCanvasFrame(width, height, (base, foreground))
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor(255, 0, 0, 128))
        from types import SimpleNamespace
        overlays = [
            (SimpleNamespace(x=2, y=3), image),
            (SimpleNamespace(x=5, y=6), image),
        ]
        result = interleave_dynamic_layers(static, stack, overlays)
        self.assertEqual(len(result.layers), 4)
        self.assertEqual([x.stream_key for x in result.layers],
                         ["base", "layer:0", "layer:1", "layer:2"])
        self.assertEqual(result.layers[0].pixels[:4], bytes((3, 2, 1, 255)))
        offset = (3 * width + 2) * 4
        self.assertEqual(result.layers[1].pixels[offset:offset + 4],
                         bytes((0, 0, 255, 128)))
        self.assertEqual(result.layers[2].pixels[offset:offset + 4],
                         bytes((0, 0, 0, 0)))

    def test_real_audio_frames_and_gpu_manifests(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("FFmpeg needed for real audio analysis")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            plan = compile_playlist(project.playlist)
            with real_audio_dynamic_frames(
                project, plan, fps=30, scale=0.25, start_seconds=0,
                prepared_audio=audio, ffmpeg=ffmpeg,
            ) as (overlays, streams, origin):
                self.assertEqual(origin, "provided_prepared_audio")
                self.assertEqual([o.kind for o in overlays], ["visualizer", "particles"])
                self.assertTrue(all(not next(stream).isNull() for stream in streams))
            source = root / "project.json"
            source.write_text(json.dumps(project.to_dict()), encoding="utf8")
            snapshot = source.read_bytes()
            output = root / "capture"
            timeline = capture_project(
                source, output, source_policy="dynamic-audio",
                prepared_audio=audio, ffmpeg=ffmpeg, frames=3, fps=30, scale=0.25,
            )
            meta, refs = load_timeline(timeline)
            self.assertEqual(len(refs), 3)
            self.assertEqual(meta["capture_scope"], "dynamic_overlay_audio_diagnostic")
            self.assertEqual(len(meta["dynamic_sources"]), 2)
            self.assertEqual(meta["dynamic_audio_origin"], "provided_prepared_audio")
            self.assertFalse(meta["full_project_visual_parity_verified"])
            self.assertEqual(source.read_bytes(), snapshot)
            packet = json.loads((output / "frame-0000.json").read_text("utf8"))
            self.assertEqual(len(packet["layers"]), 4)
            # Reject a forged diagnostic that fails to disclose rendered sources.
            meta["dynamic_sources"] = []
            timeline.write_text(json.dumps(meta), encoding="utf8")
            with self.assertRaisesRegex(TimelineValidationError, "disclosure"):
                load_timeline(timeline)


if __name__ == "__main__":
    unittest.main()
