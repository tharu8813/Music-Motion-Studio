"""Preview quality policy, runtime switching and context menu regression tests."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPoint
from pathlib import Path

from app.preview.adaptive_quality import AdaptivePreviewQuality
from app.video.decoder_backpressure import VideoDecoderBackpressure

from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.preview.quality_presets import (
    DEFAULT_PREVIEW_QUALITY, PREVIEW_QUALITY_PRESETS, load_preview_quality,
)
from app.utils.i18n import Translator


class PreviewQualityPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def preview(self):
        scene = CanvasScene()
        scene.set_artboard_size(1280, 720)
        with patch("app.dialogs.export_preview_dialog.load_preview_quality", return_value="resolution"):
            preview = ExportPreviewDialog(
                scene, [PlaylistTrack("preview.wav", "Preview", duration_seconds=30)],
                Translator(), preferred_backend="cpu",
            )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        return preview

    def test_default_is_resolution_priority(self):
        self.assertEqual(DEFAULT_PREVIEW_QUALITY, "resolution")
        self.assertIn(load_preview_quality(), PREVIEW_QUALITY_PRESETS)
        preview = self.preview()
        self.assertEqual(preview.preview_quality, "resolution")
        self.assertEqual(preview._active_render_scale, 1.0)
        self.assertEqual(preview._new_video_proxy_cache(), None)

    def test_switching_presets_updates_canvas_decoder_and_presentation_without_audio_restart(self):
        preview = self.preview()
        preview._presentation_fps_override = 120
        with patch("app.dialogs.export_preview_dialog.save_preview_quality", side_effect=lambda value: value), \
             patch.object(preview.media_player, "stop") as stop, \
             patch.object(preview.media_player, "setSource") as set_source:
            preview.set_preview_quality("power")
            self.assertEqual(preview.preview_quality, "power")
            self.assertAlmostEqual(preview._active_render_scale, 0.55)
            self.assertEqual(preview.presentation_fps, 30)
            self.assertEqual(preview._decoder_backpressure.fps_limit, 30)
            preview.set_preview_quality("frames")
            self.assertAlmostEqual(preview._active_render_scale, 0.8)
            self.assertEqual(preview.presentation_fps, 120)
            preview.set_preview_quality("quality")
            self.assertAlmostEqual(preview._active_render_scale, 1.0)
            self.assertTrue(preview._decoder_backpressure.keep_resolution)
            self.assertIsNone(preview._preview_proxy_cache)
            stop.assert_not_called()
            set_source.assert_not_called()

    def test_resolution_priority_avoids_aggressive_scale_loss(self):
        adaptive = AdaptivePreviewQuality(
            1.0, downscale_samples=8, recovery_samples=3, minimum_factor=0.85,
        )
        for _ in range(200):
            adaptive.observe(12, 60)
        self.assertAlmostEqual(adaptive.scale, 0.85)
        for _ in range(3):
            state = adaptive.observe(60, 60)
        self.assertEqual(state.scale, 1.0)

    def test_decoder_reduces_fps_before_resolution_for_high_detail_modes(self):
        decoder = VideoDecoderBackpressure(60, prefer_resolution=True)
        for _ in range(3):
            decoder.observe(
                accepted_frames=1, pressure_drops=2,
                pending_decoders=1, active_decoders=1,
            )
        self.assertEqual(decoder.resolution_factor, 1.0)
        self.assertLess(decoder.fps_limit, 60)
        locked = VideoDecoderBackpressure(
            60, prefer_resolution=True, keep_resolution=True,
        )
        for _ in range(40):
            locked.observe(
                accepted_frames=1, pressure_drops=3,
                pending_decoders=1, active_decoders=1,
            )
        self.assertEqual(locked.resolution_factor, 1.0)

    def test_proxy_limits_follow_preset_and_quality_uses_original(self):
        preview = self.preview()
        with patch.object(preview, "_preview_proxy_ffmpeg", Path("ffmpeg")):
            cache = preview._new_video_proxy_cache()
            self.assertEqual((cache.spec.max_width, cache.spec.max_height), (1920, 1080))
            with patch("app.dialogs.export_preview_dialog.save_preview_quality", side_effect=lambda value: value):
                preview.set_preview_quality("frames")
                self.assertEqual(preview._preview_proxy_cache.spec.max_height, 720)
                preview.set_preview_quality("power")
                self.assertEqual(preview._preview_proxy_cache.spec.max_height, 480)
                preview.set_preview_quality("quality")
                self.assertIsNone(preview._preview_proxy_cache)

    def test_quality_context_menu_is_exclusive_and_switches_mode(self):
        preview = self.preview()
        controller = preview.presentation_controller
        inspected = []
        with patch("app.dialogs.export_preview_dialog.save_preview_quality", side_effect=lambda value: value):
            def inspect():
                menu = controller._menu
                submenu = next(action.menu() for action in menu.actions() if action.menu())
                actions = submenu.actions()
                inspected.append((
                    [action.text() for action in actions],
                    [action.text() for action in actions if action.isChecked()],
                ))
                actions[1].trigger()
                menu.close()
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, inspect)
            controller.show_context_menu(QPoint(50, 50))
        self.assertEqual(inspected[0][0], ["해상도 우선", "프레임 우선", "절전", "품질"])
        self.assertEqual(inspected[0][1], ["해상도 우선"])
        self.assertEqual(preview.preview_quality, "frames")


if __name__ == "__main__":
    unittest.main()
