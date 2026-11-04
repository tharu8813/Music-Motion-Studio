"""Focused layout and navigation checks for translation and motion editors."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFrame, QSplitter

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.lyrics_translation_dialog import LyricsTranslationDialog
from app.dialogs.custom_motion_dialog import CustomMotionDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.utils.i18n import Translator


class EditorWorkspaceLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_translation_has_sequenced_workflow_and_safe_language_swap(self):
        cues = [{"start": 0.0, "text": "Hello world"}]
        dialog = LyricsTranslationDialog(cues, Translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)
        self.assertEqual(dialog.translation_settings_card.objectName(), "card")
        self.assertTrue(dialog.settings_section_title.text().startswith("1."))
        self.assertTrue(dialog.review_panel.review_title.text())
        from app.dialogs.translation_service_dialog import TranslationServiceDialog
        TranslationServiceDialog._set_language(dialog.source_combo, "auto")
        dialog._refresh_summary()
        self.assertFalse(dialog.swap_languages_button.isEnabled())
        TranslationServiceDialog._set_language(dialog.source_combo, "en")
        TranslationServiceDialog._set_language(dialog.target_combo, "ko")
        dialog._refresh_summary()
        self.assertTrue(dialog.swap_languages_button.isEnabled())
        dialog.swap_languages_button.click()
        self.assertEqual(TranslationServiceDialog._language_value(dialog.source_combo), "ko")
        self.assertEqual(TranslationServiceDialog._language_value(dialog.target_combo), "en")

    def test_motion_editor_has_resizable_preview_and_readable_simple_actions(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        source = Source(SourceType.SHAPE, "Logo", width=120, height=80)
        scene.addItem(SourceItem(source))
        dialog = CustomMotionDialog(
            source, scene, [PlaylistTrack("", "Example", duration_seconds=10)],
            Translator(),
        )
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)
        self.assertIsInstance(dialog.workspace_splitter, QSplitter)
        self.assertEqual(dialog.workspace_splitter.count(), 2)
        self.assertIs(dialog.workspace_splitter.widget(0), dialog.preview_splitter)
        self.assertIs(dialog.workspace_splitter.widget(1), dialog.timeline_host)
        self.assertTrue(dialog.simple_add.text())
        self.assertTrue(dialog.simple_delete.text())
        self.assertFalse(dialog.advanced)
        dialog.mode_combo.setCurrentIndex(1)
        self.assertTrue(dialog.advanced)
        self.assertEqual(dialog.edge_combo.count(), 2)


if __name__ == "__main__":
    unittest.main()
