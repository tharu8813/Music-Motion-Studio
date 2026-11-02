"""Real FFmpeg comparisons: easing, alpha, geometry and track ownership."""
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless

import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QImage

from app.animation.keyframes import Keyframe, evaluate, evaluate_curve, INTERPOLATIONS
from app.animation.keyframe_render import KeyframeRenderBinding, curve_expression, ffmpeg_transform
from app.models.source import Source, SourceType
from app.models.project import ProjectDocument
from app.models.playlist import PlaylistTrack
from app.services.project_service import ProjectService
from app.timeline.compiler import compile_playlist
from app.timeline.crossfade import compile_crossfade

FFMPEG = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")


class KeyframePersistenceTests(TestCase):
    def test_packaged_project_preserves_all_three_banks_and_track_ids(self):
        source = Source(SourceType.SHAPE, "Shape")
        track = PlaylistTrack("", "Track", duration_seconds=10)
        source.keyframes.global_curves = {"x": [Keyframe(0, 10)]}
        source.keyframes.single_track_curves[track.id] = {"scale": [Keyframe(0, 2)]}
        source.keyframes.per_track_curves[track.id] = {"rotation": [Keyframe(1, 90)]}
        with TemporaryDirectory() as raw:
            path = ProjectService.save(Path(raw) / "keyframes.pvsproj", ProjectDocument(sources=[source], playlist=[track]))
            restored = ProjectService.load(path)
            self.assertEqual(restored.sources[0].keyframes, source.keyframes)
            self.assertEqual(restored.playlist[0].id, track.id)


@skipUnless(FFMPEG, "FFmpeg is unavailable")
class KeyframeFfmpegTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def ffmpeg(self, inputs, graph, *, width, height, fps=10, duration=1, encode=False):
        command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "1"]
        for spec in inputs:
            command += ["-f", "lavfi", "-i", spec]
        command += ["-filter_complex", graph, "-map", "[out]", "-t", str(duration), "-r", str(fps)]
        if encode:
            with TemporaryDirectory() as raw:
                path = str(Path(raw) / "keyframes.mp4")
                encoded = subprocess.run([*command, "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv444p", path],
                                         capture_output=True, timeout=30)
                self.assertEqual(encoded.returncode, 0, encoded.stderr.decode("utf-8", "replace"))
                result = subprocess.run([str(FFMPEG), "-v", "error", "-i", path, "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                        capture_output=True, timeout=30)
        else:
            result = subprocess.run([*command, "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, height, width, 3)

    def test_every_ffmpeg_easing_matches_numeric_evaluator(self):
        for easing in INTERPOLATIONS:
            points = [Keyframe(.2, 0, easing), Keyframe(.8, 1)]
            expression = curve_expression(points, "T", "0")
            frames = self.ffmpeg(["color=white:s=2x2:r=10:d=1"],
                f"[0:v]format=gbrp,geq=r='255*({expression})':g=0:b=0[out]", width=2, height=2)
            for index, frame in enumerate(frames):
                expected = 255 * evaluate_curve(points, index / 10, 0)
                self.assertAlmostEqual(float(frame[0, 0, 0]), expected, delta=2, msg=f"{easing} frame {index}")

    def test_ffmpeg_geometry_and_alpha_match_canvas_keyframe_poses(self):
        self._check_geometry_and_alpha("linear")

    def test_custom_bezier_ffmpeg_geometry_alpha_and_overshoot_match_canvas(self):
        self._check_geometry_and_alpha("bezier", (.2, -.6, .7, 1.7))
        self._check_geometry_and_alpha("bezier", (.2, 3, .8, 3))

    def _check_geometry_and_alpha(self, easing, handles=(.25, .1, .25, 1)):
        source = Source(SourceType.SHAPE, "Square", x=10, y=30, width=16, height=16, fill_color="#FFFFFF")
        source.keyframes.global_curves = {
            "x": [Keyframe(0, 10, "linear"), Keyframe(1, 110)],
            "scale": [Keyframe(0, 1, "linear"), Keyframe(1, 1.5)],
            "rotation": [Keyframe(0, 0, "linear"), Keyframe(1, 90)],
            "opacity": [Keyframe(0, .25, "linear"), Keyframe(1, 1)],
        }
        source.keyframes.global_curves = {name: [replace(points[0], interpolation=easing, bezier=handles), points[1]]
                                         for name, points in source.keyframes.global_curves.items()}
        if easing == "bezier":
            # Keep even extreme positional overshoot inside the test artboard.
            source.keyframes.global_curves["x"][1] = Keyframe(1, 50)
        track = PlaylistTrack("", "Track", duration_seconds=1)
        binding = KeyframeRenderBinding.build(source, compile_playlist([track]))
        graph = []
        layer, x, y = ffmpeg_transform(graph, "[1:v]", binding, 16, 16, "test")
        graph.append(f"[0:v]{layer}overlay=x='{x}':y='{y}':format=rgb[out]")
        frames = self.ffmpeg(["color=black:s=160x90:r=10:d=1", "color=white:s=16x16:r=10:d=1,format=rgba"],
                             ";".join(graph), width=160, height=90, encode=True)
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot
        scene = CanvasScene()
        scene.set_artboard_size(160, 90)
        scene.artboard_color = QColor("#000000")
        scene.artboard_border_color = QColor("#000000")
        scene.addItem(SourceItem(source))
        for index in (0, 3, 5, 9):
            values = binding.values(index / 10)
            mask = frames[index, :, :, 0] > 20
            ys, xs = np.nonzero(mask)
            self.assertAlmostEqual(float(xs.mean()), values["x"] + 8 - .5, delta=1.5)
            self.assertAlmostEqual(float(ys.mean()), source.y + 8 - .5, delta=1.5)
            self.assertAlmostEqual(float(frames[index, :, :, 0].max()), 255 * values["opacity"], delta=4)
            preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                elapsed_seconds=index / 10, timeline_seconds=index / 10).convertToFormat(QImage.Format.Format_RGB888)
            pixels = np.frombuffer(preview.bits(), dtype=np.uint8).reshape(90, preview.bytesPerLine())[:, :480].reshape(90, 160, 3)
            preview_ys, preview_xs = np.nonzero(pixels[:, :, 0] > 20)
            self.assertAlmostEqual(float(xs.mean()), float(preview_xs.mean()), delta=1.5)
            self.assertAlmostEqual(float(ys.mean()), float(preview_ys.mean()), delta=1.5)
            self.assertAlmostEqual(float(frames[index, :, :, 0].max()), float(pixels[:, :, 0].max()), delta=4)

    def test_each_track_ffmpeg_expression_switches_by_id_at_crossfade_handover(self):
        tracks = [PlaylistTrack("same.wav", "Same", duration_seconds=1) for _ in range(2)]
        plan = compile_crossfade(tracks, .4)
        source = Source(SourceType.SHAPE, "Square", opacity=.1)
        source.keyframes.mode = "per_track"
        source.keyframes.per_track_curves = {tracks[0].id: {"opacity": [Keyframe(0, .25)]},
                                            tracks[1].id: {"opacity": [Keyframe(0, .8)]}}
        binding = KeyframeRenderBinding.build(source, plan)
        expression = binding.expression("opacity", "T")
        frames = self.ffmpeg(["color=white:s=2x2:r=10:d=1.6"],
            f"[0:v]format=gbrp,geq=r='255*({expression})':g=0:b=0[out]", width=2, height=2, duration=1.6)
        for index, frame in enumerate(frames):
            self.assertAlmostEqual(float(frame[0, 0, 0]), 255 * binding.values(index / 10).get("opacity", source.opacity), delta=2)

    def test_scheduled_video_filter_uses_global_time_after_its_start_offset(self):
        from app.renderer.ffmpeg_renderer import VideoClipOverlay
        from app.renderer.ffmpeg.filter_graph import layered_filter_graph
        source = Source(SourceType.VIDEO, "Video", x=10, y=30, width=16, height=16)
        source.keyframes.global_curves = {"x": [Keyframe(0, 10, "linear"), Keyframe(1, 110)]}
        track = PlaylistTrack("", "Track", duration_seconds=1)
        clip = VideoClipOverlay(Path("unused.mov"), .2, .8, 0, 10, 30, 16, 16, 0,
            keyframe_binding=KeyframeRenderBinding.build(source, compile_playlist([track])))
        graph = layered_filter_graph([], [], 10, 160, 90, [clip]).replace("[vout]", "[out]")
        frames = self.ffmpeg(["color=black:s=160x90:r=10:d=1", "color=black:s=2x2:r=10:d=1",
                             "color=white:s=16x16:r=10:d=1,format=rgba"], graph, width=160, height=90)
        self.assertLess(frames[0].max(), 10)
        for index in (2, 5, 9):
            _, xs = np.nonzero(frames[index, :, :, 0] > 128)
            self.assertAlmostEqual(xs.mean(), 10 + index * 10 + 8 - .5, delta=1.5)
