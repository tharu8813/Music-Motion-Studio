"""Preview quality policy, legacy migration and context menu regression tests."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.preview.adaptive_quality import AdaptivePreviewQuality
from app.preview.quality_presets import (
    DEFAULT_PREVIEW_QUALITY, PREVIEW_QUALITY_KEY, PREVIEW_QUALITY_PRESETS,
    load_preview_quality, normalize_preview_quality, save_preview_quality,
)
from app.utils.i18n import Translator
from app.video.decoder_backpressure import VideoDecoderBackpressure


class PreviewQualityPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def preview(self):
        scene = CanvasScene()
        scene.set_artboard_size(1280, 720)
        with patch("app.dialogs.export_preview_dialog.load_preview_quality", return_value="balanced"):
            preview = ExportPreviewDialog(
                scene, [PlaylistTrack("preview.wav", "Preview", duration_seconds=30)],
                Translator(), preferred_backend="cpu",
            )
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        return preview

    def test_three_modes_and_default_balanced_match_old_power_policy(self):
        self.assertEqual(DEFAULT_PREVIEW_QUALITY, "balanced")
        self.assertEqual(list(PREVIEW_QUALITY_PRESETS), ["eco", "balanced", "quality"])
        self.assertEqual(
            [preset.name_ko for preset in PREVIEW_QUALITY_PRESETS.values()],
            ["절전", "균형", "최상"],
        )
        balanced = PREVIEW_QUALITY_PRESETS["balanced"]
        self.assertEqual(
            (balanced.canvas_scale, balanced.proxy_size, balanced.fps_cap,
             balanced.downscale_samples, balanced.recovery_samples, balanced.minimum_factor),
            (0.55, (854, 480), 30, 2, 6, 0.35),
        )
        eco = PREVIEW_QUALITY_PRESETS["eco"]
        self.assertLess(eco.canvas_scale, balanced.canvas_scale)
        self.assertLess(eco.proxy_size[0], balanced.proxy_size[0])
        self.assertLess(eco.fps_cap, balanced.fps_cap)
        self.assertIn(load_preview_quality(), PREVIEW_QUALITY_PRESETS)
        preview = self.preview()
        self.assertEqual(preview.preview_quality, "balanced")
        self.assertAlmostEqual(preview._active_render_scale, 0.55)

    def test_legacy_settings_migrate_once_without_unexpected_load_increase(self):
        for stored, expected in (
            ("power", "balanced"), ("frames", "balanced"),
            ("resolution", "balanced"), ("quality", "quality"),
            ("eco", "eco"), ("balanced", "balanced"),
            ("unsupported", "balanced"),
        ):
            with self.subTest(stored=stored), patch(
                "app.preview.quality_presets.QSettings"
            ) as settings_class:
                settings = settings_class.return_value
                settings.value.return_value = stored
                self.assertEqual(normalize_preview_quality(stored), expected)
                self.assertEqual(load_preview_quality(), expected)
                settings.value.assert_called_once_with(PREVIEW_QUALITY_KEY, "balanced")
                if stored != expected:
                    settings.setValue.assert_called_once_with(PREVIEW_QUALITY_KEY, expected)
                else:
                    settings.setValue.assert_not_called()

    def test_save_only_persists_three_modes_and_maps_legacy_key(self):
        with patch("app.preview.quality_presets.QSettings") as settings_class:
            settings = settings_class.return_value
            self.assertEqual(save_preview_quality("eco"), "eco")
            settings.setValue.assert_called_with(PREVIEW_QUALITY_KEY, "eco")
            self.assertEqual(save_preview_quality("power"), "balanced")
            settings.setValue.assert_called_with(PREVIEW_QUALITY_KEY, "balanced")

    def test_switching_presets_updates_canvas_decoder_and_presentation_without_audio_restart(self):
        preview = self.preview()
        preview._presentation_fps_override = 120
        with patch("app.dialogs.export_preview_dialog.save_preview_quality", side_effect=lambda value: value), \
             patch.object(preview.media_player, "stop") as stop, \
             patch.object(preview.media_player, "setSource") as set_source:
            preview.set_preview_quality("eco")
            self.assertEqual(preview.preview_quality, "eco")
            self.assertAlmostEqual(preview._active_render_scale, 0.4)
            self.assertEqual(preview.presentation_fps, 24)
            self.assertEqual(preview._decoder_backpressure.fps_limit, 24)
            preview.set_preview_quality("balanced")
            self.assertAlmostEqual(preview._active_render_scale, 0.55)
            self.assertEqual(preview.presentation_fps, 30)
            preview.set_preview_quality("quality")
            self.assertAlmostEqual(preview._active_render_scale, 1.0)
            self.assertTrue(preview._decoder_backpressure.keep_resolution)
            self.assertIsNone(preview._preview_proxy_cache)
            stop.assert_not_called()
            set_source.assert_not_called()

    def test_maximum_quality_keeps_full_canvas_resolution_under_load(self):
        preset = PREVIEW_QUALITY_PRESETS["quality"]
        adaptive = AdaptivePreviewQuality(
            preset.canvas_scale,
            downscale_samples=preset.downscale_samples,
            recovery_samples=preset.recovery_samples,
            minimum_factor=preset.minimum_factor,
        )
        for _ in range(200):
            state = adaptive.observe(12, 60)
            self.assertEqual(state.scale, 1.0)
        self.assertEqual(adaptive.level, 0)

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
            self.assertEqual((cache.spec.max_width, cache.spec.max_height), (854, 480))
            with patch("app.dialogs.export_preview_dialog.save_preview_quality", side_effect=lambda value: value):
                preview.set_preview_quality("eco")
                self.assertEqual(self._proxy_size(preview), (640, 360))
                preview.set_preview_quality("balanced")
                self.assertEqual(self._proxy_size(preview), (854, 480))
                preview.set_preview_quality("quality")
                self.assertIsNone(preview._preview_proxy_cache)

    @staticmethod
    def _proxy_size(preview):
        spec = preview._preview_proxy_cache.spec
        return spec.max_width, spec.max_height

    def test_quality_context_menu_is_exclusive_and_switches_mode(self):
        preview = self.preview()
        controller = preview.presentation_controller
        from app.utils.i18n import Language
        preview.translator.set_language(Language.KOREAN)
        with patch("app.dialogs.export_preview_dialog.save_preview_quality", side_effect=lambda value: value):
            # Avoid a native popup event loop so this test remains deterministic.
            menu = controller._build_context_menu()
            submenu = next(action.menu() for action in menu.actions() if action.menu())
            actions = submenu.actions()
            self.assertEqual(
                [action.text() for action in actions],
                ["절전", "균형", "최상"],
            )
            self.assertEqual(
                [action.text() for action in actions if action.isChecked()],
                ["균형"],
            )
            actions[0].trigger()
            self.assertEqual(preview.preview_quality, "eco")
            self.assertEqual(
                [action.text() for action in actions if action.isChecked()],
                ["절전"],
            )
            menu.deleteLater()


if __name__ == "__main__":
    unittest.main()
