"""Preview layout checks independent of the main window's menus."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.models.playlist import PlaylistTrack
from app.utils.i18n import Translator


class PreviewLayoutTests(unittest.TestCase):
    def test_optional_panels_and_embedded_controls_preserve_playback_state(self):
        app = QApplication.instance() or QApplication([])
        scene = CanvasScene()
        scene.setSceneRect(0, 0, 1920, 1080)
        for embedded in (False, True):
            with self.subTest(embedded=embedded):
                preview = ExportPreviewDialog(
                    scene, [PlaylistTrack("missing.wav", "Preview", duration_seconds=30)],
                    Translator(), embedded=embedded, preferred_backend="cpu",
                )
                controls = None
                try:
                    self.assertTrue(preview.performance_monitor.isHidden())
                    preview.performance_action.trigger()
                    self.assertFalse(preview.performance_monitor.isHidden())
                    self.assertFalse(hasattr(preview, "tracks_toggle"))
                    self.assertFalse(preview.track_list_panel.isHidden())
                    self.assertEqual(preview.timeline.value(), 0)
                    self.assertFalse(preview._playing)
                    if embedded:
                        controls = preview.build_embedded_controls_page()
                        self.assertIs(preview.transport_card.parentWidget(), controls)
                        self.assertIs(preview.preview_close_button.parentWidget(), preview.transport_card)
                        self.assertIs(preview.build_embedded_controls_page(), controls)
                finally:
                    preview._stop_preview()
                    if controls is not None:
                        controls.deleteLater()
                    preview.deleteLater()
                    app.processEvents()


if __name__ == "__main__":
    unittest.main()
