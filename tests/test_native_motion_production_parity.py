"""Compare actual native packet pixels with the existing FFmpeg export transform.

The oracle executes ffmpeg_transform's generated filter graph; it does not
reimplement binding.values or the native painter. FFmpeg and Qt use different
edge resamplers, so compare coverage/centres and interior colour independently.
"""
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless

import numpy as np
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from app.animation.keyframes import Keyframe
from app.animation.custom_motion import CustomMotion, MotionPoint
from app.animation.keyframe_render import ffmpeg_transform
from app.models.source import Source, SourceType
from app.renderer.native_dynamic_motion import dynamic_layer_frame
from app.renderer.native_project_overlays import project_visualizers
from tests.test_native_canvas_project_probe import project_fixture

FFMPEG = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")


@skipUnless(FFMPEG, "Real production transform comparison requires FFmpeg")
class NativeMotionProductionParity(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def compare_production(self, source, output_scale):
        project = project_fixture()
        project.playlist = project.playlist[:1]
        project.playlist[0].duration_seconds = 2
        project.sources.append(source)
        overlay = project_visualizers(project, project.playlist)[0]
        # Production diagnostics downscale the already resolved overlay geometry.
        width, height = round(overlay.width * output_scale), round(overlay.height * output_scale)
        image = QImage(width, height, QImage.Format.Format_RGBA8888)
        image.fill(QColor(220, 140, 80, 128))
        graph = []
        binding = replace(overlay.keyframe_binding, output_scale=output_scale)
        layer, x, y = ffmpeg_transform(graph, "[1:v]", binding, width, height, "parity")
        graph.append("[0:v]format=rgb24,drawbox=c=black:t=fill[base]")
        graph.append(f"[base]{layer}overlay=x='{x}':y='{y}':format=rgb[out]")
        with TemporaryDirectory() as raw:
            path = Path(raw) / "layer.png"
            self.assertTrue(image.save(str(path)))
            result = subprocess.run([
                str(FFMPEG), "-v", "error", "-filter_complex_threads", "1",
                "-f", "lavfi", "-i", "color=black:s=256x192:r=10:d=2",
                "-loop", "1", "-framerate", "10", "-i", str(path),
                "-filter_complex", ";".join(graph), "-map", "[out]",
                "-frames:v", "20", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
            ], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        frames = np.frombuffer(result.stdout, np.uint8).reshape(20, 192, 256, 3)
        for index in (0, 3, 7, 11, 17, 19):
            with self.subTest(scale=output_scale, timestamp=index / 10):
                packet = dynamic_layer_frame(image, overlay, 256, 192,
                                             seconds=index / 10, output_scale=output_scale)
                bgra = np.frombuffer(packet.pixels, np.uint8).reshape(192, 256, 4)
                native = bgra[:, :, 2::-1].astype(float) * bgra[:, :, 3:4] / 255
                reference = frames[index].astype(float)
                native_mask = native[:, :, 0] > max(1, native[:, :, 0].max() * .2)
                reference_mask = reference[:, :, 0] > max(1, reference[:, :, 0].max() * .2)
                if not reference_mask.any():
                    self.assertLessEqual(float(native.max()), 2)
                    continue
                self.assertTrue(native_mask.any())
                # Distinct rasterizers may differ by one pixel on each edge.
                for axis in (0, 1):
                    actual = np.nonzero(native_mask)[axis]
                    expected = np.nonzero(reference_mask)[axis]
                    self.assertAlmostEqual(float(actual.mean()), float(expected.mean()), delta=1.5)
                    # At the almost-transparent outro, 8-bit alpha rounding
                    # changes the thresholded edge. Centre/colour checks still
                    # apply; assert exact coverage bounds on visible frames.
                    if reference[:, :, 0].max() >= 20:
                        self.assertAlmostEqual(float(actual.min()), float(expected.min()), delta=2)
                        self.assertAlmostEqual(float(actual.max()), float(expected.max()), delta=2)
                union = native_mask | reference_mask
                self.assertLess(float(np.abs(native - reference)[union].mean()), 6)
                self.assertAlmostEqual(float(native[:, :, 0].max()),
                                       float(reference[:, :, 0].max()), delta=3)
                # Straight RGB survives partial alpha. Double opacity or a
                # premultiplied packet would darken these interior samples.
                interior = bgra[:, :, 3] == bgra[:, :, 3].max()
                np.testing.assert_allclose(bgra[:, :, :3][interior].mean(axis=0),
                                           [80, 140, 220], atol=3)
        return overlay

    def test_nonunit_scale_all_animated_properties_and_non_square_downscale(self):
        source = Source(SourceType.AUDIO_VISUALIZER, "animated", x=100, y=100,
                        width=64, height=32, scale=1.5, opacity=.65, z_index=9)
        source.keyframes.global_curves = {
            name: [Keyframe(0, start), Keyframe(2, end)]
            for name, start, end in (("x", 100, 150), ("y", 100, 145),
                                     ("scale", 1.5, 2.25), ("rotation", -20, 70),
                                     ("opacity", .25, .85))
        }
        for scale in (1.0, .5):
            with self.subTest(output_scale=scale):
                overlay = self.compare_production(source, scale)
                self.assertEqual(overlay.opacity, 1, "input painter must not multiply source opacity")

    def test_custom_intro_and_outro_nonunit_scale_and_straight_alpha(self):
        source = Source(SourceType.AUDIO_VISUALIZER, "custom", x=100, y=90,
                        width=64, height=32, scale=1.5, opacity=.65, z_index=9,
                        animation_in="custom", animation_out="custom",
                        animation_in_duration=.7, animation_out_duration=.7)
        source.custom_intro_keyframes = CustomMotion((
            MotionPoint(0, dx=-20, dy=10, scale=.6, rotation=-25, opacity=.2),
            MotionPoint(1),
        ))
        source.custom_outro_keyframes = CustomMotion((
            MotionPoint(0),
            MotionPoint(1, dx=20, dy=-10, scale=.7, rotation=35, opacity=.1),
        ))
        for scale in (1.0, .5):
            with self.subTest(output_scale=scale):
                self.compare_production(source, scale)
