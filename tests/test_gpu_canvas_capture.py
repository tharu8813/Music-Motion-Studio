"""Safe experimental OpenGL Canvas export and CPU fallback contracts."""

from __future__ import annotations

import os
from unittest import TestCase, skipUnless
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QGraphicsScene

import app.preview.gpu_canvas_capture as gpu
from app.canvas.live_canvas import CanvasScene
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.canvas.source_item import SourceItem
from app.preview.canvas_snapshot import CanvasSnapshot


def scene_fixture() -> QGraphicsScene:
    scene = QGraphicsScene(0, 0, 128, 80)
    scene.addRect(QRectF(2, 2, 18, 15), brush=QColor("#EE1100"))
    scene.addRect(QRectF(3, 64, 19, 14), brush=QColor("#00CC44"))
    return scene


def raster_reference(scene, transparent=False):
    image = QImage(128, 80,
                   QImage.Format.Format_ARGB32_Premultiplied if transparent
                   else QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0, 0) if transparent else QColor(0, 0, 0))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scene.render(painter, QRectF(0, 0, 128, 80),
                     QRectF(0, 0, 128, 80))
    finally:
        painter.end()
    return image


class ExperimentalGpuCanvasTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        gpu.reset_gpu_capture_for_tests()

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {gpu.ENABLE_ENV: "0"}), patch.object(
            gpu, "_OpenGLCanvasRaster", side_effect=AssertionError("unexpected GPU context")
        ):
            self.assertIsNone(gpu.capture_canvas_opengl(
                scene_fixture(), QRectF(0, 0, 128, 80),
                QRectF(0, 0, 128, 80), False,
            ))

    def test_gpu_initialization_failure_switches_to_cpu_for_entire_worker(self):
        gpu.reset_gpu_capture_for_tests()
        with patch.dict(os.environ, {gpu.ENABLE_ENV: "1"}), patch.object(
            gpu, "_OpenGLCanvasRaster", side_effect=RuntimeError("OpenGL unavailable")
        ) as factory:
            for _ in range(3):
                self.assertIsNone(gpu.capture_canvas_opengl(
                    scene_fixture(), QRectF(0, 0, 128, 80),
                    QRectF(0, 0, 128, 80), False,
                ))
        self.assertEqual(factory.call_count, 1)

    def test_canvas_snapshot_cpu_fallback_keeps_frame_format_and_pixels(self):
        scene = CanvasScene()
        scene.addItem(SourceItem(Source(
            SourceType.SHAPE, "Test", x=25, y=30,
            width=65, height=40, fill_color="#A23564",
        )))
        try:
            with patch.dict(os.environ, {gpu.ENABLE_ENV: "0"}):
                cpu = CanvasSnapshot.capture(scene, output_scale=0.5)
            with patch.dict(os.environ, {gpu.ENABLE_ENV: "1"}), patch.object(
                gpu, "_OpenGLCanvasRaster", side_effect=RuntimeError("GPU unavailable")
            ):
                gpu.reset_gpu_capture_for_tests()
                fallback = CanvasSnapshot.capture(scene, output_scale=0.5)
            self.assertEqual(cpu.format(), fallback.format())
            self.assertEqual(cpu.size(), fallback.size())
            self.assertEqual(
                memoryview(cpu.constBits()).tobytes(),
                memoryview(fallback.constBits()).tobytes(),
            )
        finally:
            scene.clear()

    def test_transparent_offscreen_gpu_band_preserves_alpha_if_available(self):
        gpu.reset_gpu_capture_for_tests()
        scene = QGraphicsScene(0, 0, 64, 64)
        scene.addRect(
            QRectF(12, 12, 24, 24),
            brush=QBrush(QColor(255, 0, 0, 128)),
        )
        target = QRectF(0, 0, 64, 64)
        with patch.dict(os.environ, {gpu.ENABLE_ENV: "1"}):
            image = gpu.capture_canvas_opengl(scene, target, target, True)
        if image is None:
            self.skipTest("A hardware OpenGL context is not available in CI")
        self.assertEqual(image.format(), QImage.Format.Format_ARGB32_Premultiplied)
        self.assertEqual(image.pixelColor(4, 4).alpha(), 0)
        self.assertGreater(image.pixelColor(20, 20).alpha(), 80)
        self.assertLess(image.pixelColor(20, 20).alpha(), 200)

    def test_gpu_output_orientation_and_format_if_available(self):
        gpu.reset_gpu_capture_for_tests()
        scene = scene_fixture()
        target = QRectF(0, 0, 128, 80)
        with patch.dict(os.environ, {gpu.ENABLE_ENV: "1"}):
            frame = gpu.capture_canvas_opengl(scene, target, target, False)
        if frame is None:
            self.skipTest("No functional OpenGL context in this Windows/Qt environment")
        self.assertEqual(frame.format(), QImage.Format.Format_RGB32)
        self.assertEqual((frame.width(), frame.height()), (128, 80))
        # Strong orientation guard. Antialiasing may differ by GPU driver.
        self.assertGreater(frame.pixelColor(10, 10).red(), 180)
        self.assertLess(frame.pixelColor(10, 10).blue(), 70)
        self.assertGreater(frame.pixelColor(12, 70).green(), 130)
        self.assertLess(frame.pixelColor(12, 70).blue(), 100)


if __name__ == "__main__":
    import unittest
    unittest.main()
