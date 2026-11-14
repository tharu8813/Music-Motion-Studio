"""Preview layout checks independent of the main window's menus."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage
from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.dialogs.export_preview_dialog import CpuPreviewSurface
from app.widgets.cpu_preview_surface import CpuPreviewSurface as IsolatedCpuPreviewSurface
from app.models.playlist import PlaylistTrack
from app.utils.i18n import Translator


class PreviewLayoutTests(unittest.TestCase):
    def test_cpu_surface_keeps_legacy_identity_and_letterbox_paint(self):
        app = QApplication.instance() or QApplication([])
        self.assertIs(CpuPreviewSurface, IsolatedCpuPreviewSurface)
        self.assertEqual(CpuPreviewSurface.__module__, "app.widgets.cpu_preview_surface")

        surface = CpuPreviewSurface()
        presented = []
        surface.frame_presented.connect(lambda: presented.append(True))
        try:
            surface.resize(200, 200)
            frame = QImage(80, 40, QImage.Format.Format_RGB32)
            frame.fill(0xff304050)
            surface.set_image(frame)
            surface.show()
            self.assertFalse(surface.grab().isNull())
            self.assertEqual(surface._target_rect, QRect(0, 50, 200, 100))
            self.assertTrue(presented)

            surface.resize(160, 80)
            self.assertFalse(surface.grab().isNull())
            self.assertEqual(surface._target_rect, QRect(0, 0, 160, 80))
            self.assertIs(surface.image, frame)
        finally:
            surface.close()
            surface.deleteLater()
            app.processEvents()

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
