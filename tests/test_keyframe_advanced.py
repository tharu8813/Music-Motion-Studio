"""Custom Bezier timing and additional Canvas properties, including restoration."""
import json
import os
from dataclasses import replace
from unittest import TestCase, skipUnless
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.animation.canvas_keyframes import CanvasKeyframeBinding
from app.animation.keyframes import (
    DEFAULT_BEZIER, Keyframe, bezier_progress, cubic_coordinate, curve_maximum,
    evaluate, normalize_curves, properties_for, resolved_source, transform_positions,
)
from app.canvas.live_canvas import CanvasScene, LiveCanvas
from app.canvas.source_item import SourceItem
from app.controllers.keyframe_controller import KeyframeController
from app.models.playlist import PlaylistTrack
from app.models.source import Shadow, Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.services.source_store import SourceStore
from app.timeline.bezier_editor import BezierEditor
from app.utils.i18n import Translator


def pixels(image):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits(), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


class AdvancedKeyframeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.source = Source(SourceType.SHAPE, "Shape", x=30, y=40, width=60, height=30,
                             fill_color="#FFFFFF", shadow=Shadow(enabled=True))
        self.track = PlaylistTrack("", "A", duration_seconds=10)
        self.store = SourceStore()
        self.store.add(self.source)
        self.controller = KeyframeController(self.store, lambda: [(self.track, 0, 10)])
        self.translator = Translator()

    def test_bezier_inverts_time_coordinate_and_handles_flat_tangents(self):
        for handles in ((0, 0, 1, 1), (0, 3, 0, -2), (1, -2, 1, 3), (.8, -1, .1, 2)):
            for t in (.0001, .1, .25, .5, .9, .9999):
                x = cubic_coordinate(t, handles[0], handles[2])
                y = cubic_coordinate(t, handles[1], handles[3])
                self.assertAlmostEqual(bezier_progress(x, handles), y, places=6)
            self.assertEqual(bezier_progress(0, handles), 0)
            self.assertEqual(bezier_progress(1, handles), 1)

    def test_malformed_handles_normalize_and_json_roundtrip_preserves_all_banks(self):
        curves = normalize_curves({"x": [dict(time=0, value=30, interpolation="bezier", bezier=[-1, -9, 9, 9])],
                                   "opacity": [dict(time=0, value=.5, bezier=[False, 1, 2, float("nan")])]})
        self.assertEqual(curves["x"][0].bezier, (0, -2, 1, 3))
        self.assertEqual(curves["opacity"][0].bezier, DEFAULT_BEZIER)
        animation = self.source.keyframes
        animation.global_curves = curves
        animation.single_track_curves = {self.track.id: {"width": [Keyframe(1, 80, "bezier", (.2, -.4, .7, 1.5))]}}
        animation.per_track_curves = {self.track.id: {"shadow.offset_x": [Keyframe(2, 8)]}}
        restored = Source.from_dict(json.loads(json.dumps(self.source.to_dict())))
        self.assertEqual(restored.keyframes, animation)
        transformed = transform_positions(animation, 2, 3, 4, 5)
        self.assertEqual(transformed.global_curves["x"][0].bezier, curves["x"][0].bezier)
        resized = transform_positions(animation, 2, 3, size_scale=2)
        self.assertEqual(resized.single_track_curves[self.track.id]["width"][0].value, 160)
        self.assertEqual(resized.per_track_curves[self.track.id]["shadow.offset_x"][0].value, 16)

    def test_export_canvas_capture_keeps_each_animated_state_and_matches_preview(self):
        from app.preview.export_canvas_capture import ExportCanvasCapturer
        from app.renderer.export_timeline import ExportFrameSample
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        self.source.keyframes.global_curves = {
            "width": [Keyframe(0, 60, "bezier", (.2, -.5, .7, 1.5)), Keyframe(10, 160)],
            "shadow.opacity": [Keyframe(0, .1), Keyframe(10, .7)],
            "outline_width": [Keyframe(0, 0), Keyframe(10, 6)],
        }
        scene.addItem(SourceItem(self.source))
        captured = []
        def stage(image, duration, stream):
            captured.append(image.copy())
            return None
        capturer = ExportCanvasCapturer(scene, [self.track], 10, set(), [(None, None)],
                                        stage, lambda: None, lambda *_: None, output_fps=10)
        samples = [ExportFrameSample(self.track, 1, 0, .1, t, t) for t in (0, 2, 5, 8)]
        self.assertNotIn("base", capturer.invariant_stream_keys)
        self.assertEqual(len(capturer.coalesce_samples(samples, "base")), len(samples))
        for sample in samples:
            capturer.capture(sample)
            preview = CanvasSnapshot.capture_track(scene, self.track, 1, 1, 0,
                elapsed_seconds=sample.elapsed_seconds, timeline_seconds=sample.timeline_seconds)
            np.testing.assert_array_equal(pixels(captured[-1]), pixels(preview))
        self.assertFalse(np.array_equal(pixels(captured[0]), pixels(captured[-1])))

    def test_overshoot_is_bounded_and_video_envelope_includes_interior_peak(self):
        handles = (.2, 3, .8, 3)
        curve = [Keyframe(0, 1, "bezier", handles), Keyframe(1, 2)]
        self.assertGreater(curve_maximum(curve), 3)
        values = [1 + bezier_progress(i / 1000, handles) for i in range(1001)]
        self.assertGreaterEqual(curve_maximum(curve) + 1e-8, max(values))
        self.source.keyframes.global_curves = {"opacity": [replace(p, value=p.value - 1) for p in curve]}
        self.assertEqual(evaluate(self.source, .5)["opacity"], 1)

    def test_supported_properties_follow_source_type_and_nested_values_do_not_alias(self):
        video = Source(SourceType.VIDEO, "Video")
        self.assertEqual(set(properties_for(video)), {"x", "y", "scale", "rotation", "opacity"})
        self.assertNotIn("font_size", properties_for(self.source))
        copy = resolved_source(self.source, {"width": 100, "shadow.offset_x": 9})
        self.assertEqual(copy.width, 100)
        self.assertEqual(copy.shadow.offset_x, 9)
        self.assertEqual(self.source.width, 60)
        self.assertEqual(self.source.shadow.offset_x, 3)
        self.assertIsNot(copy.shadow, self.source.shadow)
        self.controller.toggle("font_size")
        self.assertFalse(self.source.keyframes.animated)

    def test_editor_scrub_capture_and_disable_restore_source_geometry_and_shadow(self):
        self.source.keyframes.global_curves = {
            "width": [Keyframe(0, 60, "linear"), Keyframe(10, 160)],
            "height": [Keyframe(0, 30, "linear"), Keyframe(10, 80)],
            "border_radius": [Keyframe(0, 0, "linear"), Keyframe(10, 20)],
            "shadow.offset_x": [Keyframe(0, 3, "linear"), Keyframe(10, 23)],
        }
        canvas = LiveCanvas(self.store, self.translator)
        canvas.replace_sources()
        canvas.scene_model.set_artboard_size(320, 180)
        binding = CanvasKeyframeBinding(canvas, self.controller)
        self.addCleanup(canvas.deleteLater)
        self.controller.seek_global(8)
        item = canvas._items[self.source.id]
        self.assertEqual(item.source.width, 140)
        saved, display, origin = self.source.to_dict(), item.source, item.transformOriginPoint()
        observed = []
        def capture(*args, **kwargs):
            observed.append((item.source.width, item.source.height, item.source.border_radius, item.source.shadow.offset_x))
            return QImage(320, 180, QImage.Format.Format_RGBA8888)
        with patch.object(CanvasSnapshot, "capture", side_effect=capture):
            for time in (5, 0, 9):
                CanvasSnapshot.capture_track(canvas.scene_model, self.track, 1, 1, 0,
                                             elapsed_seconds=time, timeline_seconds=time)
        self.assertEqual(observed, [(110, 55, 10, 13), (60, 30, 0, 3), (150, 75, 18, 21)])
        self.assertEqual(self.source.to_dict(), saved)
        self.assertIs(item.source, display)
        self.assertEqual(item.transformOriginPoint(), origin)
        self.assertFalse(item._suppress_position_sync)
        self.controller.configure(enabled=False)
        self.assertIs(item.source, self.source)
        self.assertEqual(item.content_rect().width(), 60)
        self.assertEqual(item.transformOriginPoint().x(), 30)

    def test_animated_width_changes_rendered_pixels_and_restores_after_capture_exception(self):
        self.source.shadow.enabled = False
        self.source.keyframes.global_curves = {"width": [Keyframe(0, 60, "linear"), Keyframe(10, 160)]}
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        scene.artboard_color = QColor("#000000")
        scene.artboard_border_color = QColor("#000000")
        item = SourceItem(self.source)
        scene.addItem(item)
        frames = [CanvasSnapshot.capture_track(scene, self.track, 1, 1, 0, elapsed_seconds=t, timeline_seconds=t)
                  for t in (0, 5)]
        self.assertLess(pixels(frames[0])[50, 120, 0], 20)
        self.assertGreater(pixels(frames[1])[50, 120, 0], 240)
        with patch.object(CanvasSnapshot, "capture", side_effect=RuntimeError("capture failed")):
            with self.assertRaises(RuntimeError):
                CanvasSnapshot.capture_track(scene, self.track, 1, 1, 0, elapsed_seconds=5, timeline_seconds=5)
        self.assertIs(item.source, self.source)
        self.assertEqual(self.source.width, 60)
        self.assertEqual(item.content_rect().width(), 60)
        self.assertFalse(item._suppress_position_sync)

    def test_blur_and_font_size_change_actual_pixels_without_writing_base(self):
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        scene.artboard_color = QColor("#000000")
        scene.artboard_border_color = QColor("#000000")
        image_source = Source(SourceType.IMAGE, "Blur", x=20, y=20, width=64, height=64)
        image_source.keyframes.global_curves = {"blur": [Keyframe(0, 0, "linear"), Keyframe(10, 8)]}
        item = SourceItem(image_source)
        raw = QImage(64, 64, QImage.Format.Format_RGB32)
        raw.fill(QColor("#000000"))
        for x in range(32, 64):
            for y in range(64):
                raw.setPixelColor(x, y, QColor("#FFFFFF"))
        item._raw_pixmap = item._pixmap = QPixmap.fromImage(raw)
        scene.addItem(item)
        text = Source(SourceType.TEXT, "Font", x=100, y=20, width=200, height=100,
                      font_size=12, text="Animate", fill_color="#00000000")
        text.keyframes.global_curves = {"font_size": [Keyframe(0, 12, "linear"), Keyframe(10, 60)]}
        scene.addItem(SourceItem(text))
        saved = [source.to_dict() for source in (image_source, text)]
        original_pixmap = item._pixmap.cacheKey()
        before, after = [pixels(CanvasSnapshot.capture_track(scene, self.track, 1, 1, 0,
                           elapsed_seconds=t, timeline_seconds=t)) for t in (0, 9)]
        self.assertGreater(after[50, 49, 0], before[50, 49, 0] + 20)
        self.assertGreater(np.count_nonzero(after[20:120, 100:300, :3]), np.count_nonzero(before[20:120, 100:300, :3]))
        self.assertEqual([source.to_dict() for source in (image_source, text)], saved)
        self.assertEqual(item._pixmap.cacheKey(), original_pixmap)

    def test_capture_another_track_cannot_reuse_editor_animated_blur(self):
        source = Source(SourceType.IMAGE, "Image", width=64, height=64)
        source.keyframes.mode, source.keyframes.target_track_id = "single_track", self.track.id
        source.keyframes.single_track_curves = {self.track.id: {"blur": [Keyframe(0, 8)]}}
        self.store.replace([source])
        self.store.select(source.id)
        canvas = LiveCanvas(self.store, self.translator)
        canvas.replace_sources()
        self.addCleanup(canvas.deleteLater)
        item = canvas._items[source.id]
        pattern = QImage(64, 64, QImage.Format.Format_RGB32)
        pattern.fill(QColor("#000000"))
        for x in range(32, 64):
            for y in range(64):
                pattern.setPixelColor(x, y, QColor("#FFFFFF"))
        item._raw_pixmap = item._pixmap = QPixmap.fromImage(pattern)
        binding = CanvasKeyframeBinding(canvas, self.controller)
        self.controller.seek_global(1)
        displayed = item._pixmap.cacheKey()
        self.assertGreater(item._pixmap.toImage().pixelColor(30, 30).red(), 20)
        other = PlaylistTrack("", "B", duration_seconds=10)
        with patch.object(CanvasSnapshot, "capture", side_effect=lambda *_a, **_kw: item._pixmap.toImage().copy()):
            unkeyed = CanvasSnapshot.capture_track(canvas.scene_model, other, 2, 2, 10,
                elapsed_seconds=1, timeline_seconds=11)
        self.assertEqual(unkeyed.pixelColor(30, 30).red(), 0)
        self.assertEqual(item._pixmap.cacheKey(), displayed)
        self.assertEqual(source.blur, 0)

    def test_graph_handles_mouse_numeric_preset_and_cancel_are_local(self):
        point = Keyframe(0, 0)
        dialog = BezierEditor(point, Keyframe(1, 100), "X")
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        graph = dialog.graph
        start = graph.position(*graph.handles[:2]).toPoint()
        end = graph.position(.4, .6).toPoint()
        QTest.mousePress(graph, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(graph, end)
        QTest.mouseRelease(graph, Qt.MouseButton.LeftButton, pos=end)
        self.assertAlmostEqual(dialog.handles[0], .4, delta=.01)
        self.assertAlmostEqual(dialog.fields[1].value(), .6, delta=.01)
        dialog.fields[0].setValue(.8)
        self.assertAlmostEqual(dialog.handles[0], .8)
        dialog.preset.setCurrentIndex(2)
        self.assertEqual(dialog.handles, (0, 0, 1, 1))
        dialog.reject()
        self.assertEqual(point.bezier, DEFAULT_BEZIER)

@skipUnless(os.environ.get("QT_QPA_PLATFORM") == "windows", "Requires native Windows OpenGL")
class NativeKeyframeGpuTests(TestCase):
    def test_actual_gpu_framebuffer_matches_animated_canvas_frames(self):
        from app.preview.gpu_texture_surface import GpuTexturePreviewSurface
        app = QApplication.instance() or QApplication([])
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        source = Source(SourceType.SHAPE, "GPU parity", x=30, y=40, width=60, height=40,
                        fill_color="#F5A742", shadow=Shadow(enabled=True, color="#FFFFFF"))
        source.keyframes.global_curves = {
            "width": [Keyframe(0, 60, "bezier", (.2, -.3, .7, 1.4)), Keyframe(1, 150)],
            "border_radius": [Keyframe(0, 0), Keyframe(1, 20)],
            "shadow.offset_x": [Keyframe(0, 3), Keyframe(1, 15)],
        }
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("", "GPU", duration_seconds=2)
        surface = GpuTexturePreviewSurface()
        self.addCleanup(surface.deleteLater)
        self.addCleanup(surface.close)
        surface.resize(320, 180)
        surface.show()
        QTest.qWait(250)
        self.assertTrue(surface.ready, "A real GPU context is required")
        print("KEYFRAME_GPU_DEVICE", surface.backend_info, flush=True)
        for time in (0, .25, .5, .9):
            frame = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=time, timeline_seconds=time)
            surface.set_image(frame)
            QTest.qWait(100)
            gpu = surface.grabFramebuffer()
            if gpu.size() != frame.size():
                gpu = gpu.scaled(frame.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            error = np.abs(pixels(gpu).astype(float) - pixels(frame).astype(float))
            self.assertLess(float(error.mean()), .5)
            self.assertLess(float(np.percentile(error, 99)), 3)
        app.processEvents()
