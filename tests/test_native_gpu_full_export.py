"""Exact complete-project Qt frame generator checks (GPU mock is explicit)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.renderer.ffmpeg_renderer import RenderResult
from app.timeline.compiler import compile_playlist
from tests.test_native_canvas_dynamic_overlays import dynamic_project, sample_wav
from tools.native_gpu_full_export import (
    export_complete_project, produce_complete_project_frames,
)


class NativeGpuFullProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_complete_project_yields_all_real_qt_and_audio_layers_lazily(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("Verified FFmpeg required for real-audio full project frames")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            project.sources[0].background_bass_reactive = True
            before = project.to_dict()
            frames = produce_complete_project_frames(
                project, fps=30, scale=0.25,
                prepared_audio=audio, ffmpeg=str(ffmpeg),
            )
            self.assertEqual(compile_playlist(project.playlist).duration_seconds, 1.0)
            count = 0
            for frame in frames:
                count += 1
                self.assertEqual((frame.width, frame.height), (320, 180))
                self.assertEqual(len(frame.layers), 4)
                self.assertEqual(
                    [layer.stream_key for layer in frame.layers],
                    ["base", "layer:0", "layer:1", "layer:2"],
                )
            self.assertEqual(count, 30)
            self.assertEqual(project.to_dict(), before)

    def test_full_export_invokes_real_bounded_stream_and_mp4_mux_after_audio_prep(self):
        ffmpeg = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).is_file():
            self.skipTest("Verified FFmpeg required for real-audio export orchestration")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "sine.wav"
            sample_wav(audio)
            project = dynamic_project(audio)
            project.settings.title = "Native audio project"
            source = root / "project.json"
            source.write_text(json.dumps(project.to_dict()), encoding="utf8")
            original_bytes = source.read_bytes()
            target = root / "full.mp4"
            collected = []

            def mock_stream(_renderer, source_frames, **kwargs):
                for frame in source_frames:
                    collected.append((frame.width, frame.height, len(frame.layers)))
                self.assertEqual(kwargs["settings"].fps, 30)
                self.assertEqual(kwargs["settings"].video_codec, "h264_nvenc")
                self.assertEqual(kwargs["metadata"].title, "Native audio project")
                self.assertEqual(kwargs["compiled_plan"].duration_seconds, 1.0)
                return RenderResult(target, 1)

            with patch(
                "tools.native_gpu_full_export.FFmpegRenderer.prepare_playlist_audio",
                return_value=audio,
            ) as prepare, patch(
                "tools.native_gpu_full_export.render_native_gpu_playlist",
                side_effect=mock_stream,
            ) as render:
                result = export_complete_project(
                    source, target, native_exe=root / "native.exe",
                    fps=30, scale=0.25, ffmpeg=str(ffmpeg),
                )
            self.assertEqual(result, target)
            self.assertEqual(len(collected), 30)
            self.assertEqual(collected[0], (320, 180, 4))
            prepare.assert_called_once()
            render.assert_called_once()
            self.assertEqual(source.read_bytes(), original_bytes)

    def test_refuses_to_overwrite_user_video_before_audio_or_gpu_start(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / "full.mp4"
            existing.write_bytes(b"USER-ORIGINAL-VIDEO")
            source = root / "project.json"
            source.write_text(json.dumps(dynamic_project(root / "test.wav").to_dict()),
                              encoding="utf8")
            with self.assertRaisesRegex(ValueError, "will not be overwritten"):
                export_complete_project(
                    source, existing, native_exe=root / "native.exe",
                )
            self.assertEqual(existing.read_bytes(), b"USER-ORIGINAL-VIDEO")


if __name__ == "__main__":
    unittest.main()
