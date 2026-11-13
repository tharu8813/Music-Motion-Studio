"""Export-local text tile reuse, including hardware-only raster comparison."""
from dataclasses import replace
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PySide6.QtWidgets import QApplication

from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.native_canvas_layer_packets import (
    NativeCanvasBgraLayer, NativeCanvasFrame, NativeCanvasTextLayer, save_native_canvas_frame,
)
from app.renderer.native_frame_transport import FrameReuseState, frame_chunks, HEADER
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations
from app.renderer.native_project_frames import produce_complete_project_frames
from tests.test_native_canvas_dynamic_overlays import sample_wav
from tests.test_native_canvas_project_probe import project_fixture


PROBE = os.environ.get("PLAYLIST_CANVAS_AFFINE_PROBE")


class NativeTextLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def frames(self, audio, enabled, counters, *, scale=1, layer_limit=8):
        project = project_fixture()
        project.canvas.width, project.canvas.height = 512, 256
        project.playlist = [
            PlaylistTrack(str(audio), "처음 First", duration_seconds=1.0),
            PlaylistTrack(str(audio), "다음 Second", duration_seconds=1.0),
        ]
        for index, track in enumerate(project.playlist):
            track.lyrics = [
                {"start": 0.0, "end": 0.85, "text": "한글 Hello" if index == 0 else "다음 Again",
                 "words": [{"start": 0.0, "end": 0.40, "text": "한글" if index == 0 else "다음", "offset": 0},
                           {"start": 0.4, "end": 0.82, "text": "Hello" if index == 0 else "Again", "offset": 3}]},
                {"start": 0.48, "end": 0.95, "text": "겹침 overlap"},
            ]
        title = Source(SourceType.TEXT, "Title", x=40, y=24, width=380, height=50, z_index=1,
                       fill_color="#00000000", outline_color="#e9f3ff", text="%title%",
                       font_size=28, text_stroke_width=2, text_stroke_color="#10003a",
                       animation_in="rise", animation_in_duration=0.35)
        title.shadow.enabled = title.text_shadow_glyph = True
        title.shadow.color = "#6399ff"
        lyrics = Source(SourceType.LYRICS, "Lyrics", x=40, y=82, width=430, height=160, z_index=2,
                        fill_color="#00000000", outline_color="#fff9fa", font_size=29,
                        subtitle_karaoke_enabled=True, subtitle_accent_color="#e47035",
                        subtitle_animation="cascade", subtitle_animation_duration=0.28,
                        subtitle_intro_enabled=False, subtitle_intro_midtrack=False,
                        subtitle_context_lines=1, subtitle_next_lines=1)
        project.sources = [title, lyrics]
        options = replace(NativeGpuOptimizations.for_profile("automatic"),
                          gpu_text=enabled, canvas_layer_limit=layer_limit)
        iterator = produce_complete_project_frames(
            project, fps=12, scale=scale, prepared_audio=audio, ffmpeg=None,
            raster_counters=counters, optimizations=options,
        )
        return [p.resolve() if hasattr(p, "resolve") else p for p in iterator]

    def test_text_atlas_reused_and_highlight_advances_across_tracks(self):
        with TemporaryDirectory() as directory:
            audio = Path(directory) / "tone.wav"
            sample_wav(audio)
            counters = {}
            frames = self.frames(audio, True, counters)
            one_stream = self.frames(audio, True, {}, layer_limit=1)
            baseline = self.frames(audio, False, {})
        self.assertEqual(len(frames), 24)
        self.assertGreater(counters["gpu_text_frames"], 20)
        self.assertLess(counters["gpu_text_atlases_built"], counters["gpu_text_frames"])
        self.assertTrue(all(isinstance(frames[i].layers[1], NativeCanvasTextLayer) for i in (2, 6, 14, 18)))
        self.assertGreater(len({frames[i].layers[1].quads for i in (2, 3, 4, 5)}), 1)
        self.assertNotEqual(frames[3].layers[1].pixels, frames[15].layers[1].pixels)
        transport = FrameReuseState()
        for i, frame in enumerate(frames):
            chunks = list(frame_chunks(frame, i, reuse_state=transport))
            self.assertLessEqual(HEADER.unpack(chunks[0])[3], 65536)
        self.assertGreater(transport.reused_planes, len(frames))
        self.assertEqual(one_stream, baseline)

    @unittest.skipUnless(PROBE, "Set PLAYLIST_CANVAS_AFFINE_PROBE for real D3D11 readback")
    def test_real_gpu_text_atlas_matches_qt_around_karaoke_overlap_and_track_handoff(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "tone.wav"
            sample_wav(audio)
            reference = self.frames(audio, False, {}, scale=3)
            candidate = self.frames(audio, True, {}, scale=3)
            for i in (2, 4, 6, 11, 12, 14, 18):
                with self.subTest(frame=i):
                    a, b = reference[i], candidate[i]
                    self.assertEqual((a.width, a.height), (b.width, b.height))
                    for mode, frame in (("qt", a), ("gpu", b)):
                        stem = f"{mode}-{i}"
                        manifest = save_native_canvas_frame(frame, root, stem=stem)
                        result = subprocess.run(
                            [PROBE, str(manifest), "0", str(root / f"{stem}.nv12"), "--nv12"],
                            capture_output=True, text=True, timeout=30,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                    width, height = a.width, a.height
                    left, right = [
                        np.frombuffer((root / f"{mode}-{i}.nv12").read_bytes(), dtype=np.uint8)
                        [:width * height].reshape(height, width).astype(np.float32)
                        for mode in ("qt", "gpu")
                    ]
                    for section in (slice(30, 246), slice(246, 726)):
                        expected, actual = left[section, 108:1420], right[section, 108:1420]
                        self.assertLess(np.abs(actual - expected).mean(), 2.8)
                        if expected.max() > 50:
                            self.assertGreater(actual.max(), 50)
                        expected_energy = np.abs(expected - expected[0, 0]).sum()
                        actual_energy = np.abs(actual - actual[0, 0]).sum()
                        if expected_energy > 10000:
                            self.assertGreater(actual_energy / expected_energy, 0.90)
                            self.assertLess(actual_energy / expected_energy, 1.10)
