"""GPU preview content reuse and keyframe transform parity tests."""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.preview.texture_fingerprint import image_content_signature
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.utils.i18n import Translator


@dataclass(frozen=True)
class DummyBinding:
    source: object
    output_scale: float = 1.0
    windows: tuple = ()
    def values(self, seconds):
        return dict(x=50.0, y=60.0, scale=1.5, opacity=0.6, rotation=30.0)


class GpuCompositorOptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_identical_pixels_different_qimage_cache_keys_share_signature(self):
        image = QImage(48, 32, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0xff123456)
        copy = QImage(48, 32, QImage.Format.Format_ARGB32_Premultiplied)
        copy.fill(0xff123456)
        self.assertNotEqual(image.cacheKey(), copy.cacheKey())
        self.assertEqual(image_content_signature(image), image_content_signature(copy))
        copy.setPixel(4, 3, 0xff113355)
        self.assertNotEqual(image_content_signature(image), image_content_signature(copy))
        self.assertIsNone(image_content_signature(image, max_bytes=32))

    def test_gpu_keyframe_uses_unchanged_texture_pixels(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "preview", duration_seconds=30)],
            Translator(), preferred_backend="cpu",
        )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        source = SimpleNamespace(x=0.0, y=0.0, width=100.0, height=50.0,
                                 scale=1.0, opacity=1.0, rotation=0.0)
        overlay = VisualizerOverlay(
            10, 20, 100, 50, "bars", "#ffffff",
            keyframe_binding=DummyBinding(source),
        )
        image = QImage(100, 50, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0xffabcdef)
        layers = preview._gpu_audio_layers([overlay], (image,))
        self.assertEqual(len(layers), 1)
        layer = layers[0]
        self.assertEqual(layer.image.cacheKey(), image.cacheKey())
        self.assertEqual((layer.target.width(), layer.target.height()), (150.0, 75.0))
        self.assertEqual(layer.rotation, 30.0)
        self.assertAlmostEqual(layer.opacity, 0.6)
        # CPU path keeps its pixel-scaling fallback for a GPU context loss.
        _overlays, cpu_images = preview._apply_overlay_keyframes(
            [overlay], (image,), 0.0,
        )
        self.assertEqual((cpu_images[0].width(), cpu_images[0].height()), (150, 75))


if __name__ == "__main__":
    unittest.main()
