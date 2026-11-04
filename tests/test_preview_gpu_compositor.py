"""GPU preview content reuse and keyframe transform parity tests."""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
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

    def test_track_static_policy_excludes_animated_or_timed_sources(self):
        self.assertTrue(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.ALBUM_COVER, "Cover")
        ))
        self.assertTrue(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.BACKGROUND, "Backdrop", background_mode="album_art")
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.LYRICS, "Lyrics")
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.TEXT, "Time", text="%current_time%")
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.TEXT, "Moving", loop_motion="float")
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.BACKGROUND, "Animated", background_mode="album_art",
                   background_ambient=True)
        ))
        self.assertFalse(CanvasSnapshot.source_is_preview_track_static(
            Source(SourceType.ALBUM_COVER, "Temporary", timeline_start=1.0)
        ))

    def test_static_z_band_cache_reuses_images_and_invalidates_on_change(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "Preview", duration_seconds=30)],
            Translator(), preferred_backend="cpu",
        )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        frame = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        frame.fill(0xff304050)
        bands = [(None, 0.0), (1.0, None)]

        def render():
            return preview._gpu_z_band_layers(
                preview.tracks[0], 0, 5.0, 0.0,
                None, 1.0, 0.0, [], (),
            )

        with patch.object(CanvasSnapshot, "z_bands", return_value=bands), \
             patch.object(CanvasSnapshot, "split_mixed_capture_bands", return_value=bands), \
             patch.object(CanvasSnapshot, "invariant_stream_keys", return_value={"base", "layer:0"}), \
             patch.object(CanvasSnapshot, "capture_track", return_value=frame) as capture:
            first = render()
            second = render()
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(len(first), len(second))
            self.assertEqual(preview._static_band_cache_hits, 2)
            preview._invalidate_preview_render_cache()
            render()
            self.assertEqual(capture.call_count, 4)
            preview._frame_junction = object()
            render()
            self.assertEqual(capture.call_count, 6)

    def test_ordered_canvas_band_cache_renders_only_changed_runs(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "Preview", duration_seconds=30)],
            Translator(), preferred_backend="cpu",
        )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        preview._cached_source_items = ()
        image = QImage(640, 360, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0xff444444)
        bands = [(None, 0), (1, 1), (2, None)]
        with patch.object(CanvasSnapshot, "split_mixed_capture_bands", return_value=bands), \
             patch.object(CanvasSnapshot, "invariant_stream_keys", return_value={"base", "layer:1"}), \
             patch.object(CanvasSnapshot, "band_capture_envelope", return_value=None), \
             patch.object(CanvasSnapshot, "capture_track", return_value=image) as capture:
            first = preview._ordered_canvas_gpu_layers(
                preview.tracks[0], 0, 5.0, 0.0, frozenset(),
            )
            second = preview._ordered_canvas_gpu_layers(
                preview.tracks[0], 0, 5.1, 0.0, frozenset(),
            )
            self.assertEqual(len(first), 3)
            self.assertEqual(len(second), 3)
            self.assertEqual(capture.call_count, 4)
            self.assertEqual(preview._ordered_cache_hits, 2)
            preview._invalidate_preview_render_cache()
            preview._ordered_canvas_gpu_layers(
                preview.tracks[0], 0, 5.2, 0.0, frozenset(),
            )
            self.assertEqual(capture.call_count, 7)

    def test_ordered_canvas_uses_crop_coordinates_for_gpu_layers(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(
            scene, [PlaylistTrack("missing.wav", "Preview", duration_seconds=30)],
            Translator(), preferred_backend="cpu",
        )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        preview._cached_source_items = ()
        image = QImage(100, 60, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0xffaabbcc)
        bands = [(None, 0), (1, None)]
        crop = QRectF(40, 30, 100, 60)
        with patch.object(CanvasSnapshot, "split_mixed_capture_bands", return_value=bands), \
             patch.object(CanvasSnapshot, "invariant_stream_keys", return_value=set()), \
             patch.object(CanvasSnapshot, "band_capture_envelope", return_value=crop), \
             patch.object(CanvasSnapshot, "capture_track", return_value=image):
            layers = preview._ordered_canvas_gpu_layers(
                preview.tracks[0], 0, 5.0, 0.0, frozenset(),
            )
        self.assertEqual((layers[1].target.x(), layers[1].target.y()), (40, 30))
        self.assertEqual((layers[1].target.width(), layers[1].target.height()), (100, 60))

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
