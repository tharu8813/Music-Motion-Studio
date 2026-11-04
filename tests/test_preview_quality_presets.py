"""Preview quality policy, runtime switching and context menu regression tests."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPoint
from PySide6.QtGui import QContextMenuEvent

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
