"""Regression coverage for resize, form reachability and property navigation."""
from dataclasses import replace
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox

from app.dialogs.adaptive_dialog import AdaptiveDialog
from app.dialogs.export_settings_dialog import ExportSettingsDialog
from app.dialogs.new_project_dialog import NewProjectDialog
from app.models.source import Source, SourceType
from app.models.playlist import PlaylistTrack
from app.services.app_settings_service import AppSettings
from app.utils.i18n import Language
from tests.main_window_base import MainWindowTestCase


class ResponsiveWorkspaceTests(MainWindowTestCase):
    def test_fitted_canvas_stays_whole_and_manual_zoom_survives_resize(self):
        window = self.window
        window.resize(1560, 920)
        window.show()
        self.application.processEvents()
        canvas = window.canvas
        canvas.fit_artboard()
        for width, height in ((1100, 680), (960, 520), (1366, 720)):
            window.resize(width, height)
            self.application.processEvents()
            artboard = canvas.mapFromScene(canvas.scene_model.artboard_rect).boundingRect()
            self.assertTrue(canvas.viewport().rect().adjusted(-1, -1, 1, 1).contains(artboard))
            self.assertGreaterEqual(window.main_splitter.height(), 220)
        canvas.zoom_by(1.15)
        zoom = canvas.transform().m11()
        window.resize(1200, 680)
        self.application.processEvents()
        self.assertAlmostEqual(canvas.transform().m11(), zoom)
        canvas.set_zoom(1.0)
        window.resize(1560, 920)
        self.application.processEvents()
        self.assertAlmostEqual(canvas.transform().m11(), 1.0)

    def test_small_screen_keeps_dialog_actions_visible_and_body_scrollable(self):
        screen = Mock()
        screen.availableGeometry.return_value = QRect(0, 0, 1093, 614)
        dialogs = (
            NewProjectDialog(self.window.translator),
            ExportSettingsDialog(AppSettings(), 3, 720, self.window.translator, "audit.mp4"),
        )
        for dialog in dialogs:
            with self.subTest(dialog=type(dialog).__name__), patch.object(AdaptiveDialog, "screen", return_value=screen):
                self.addCleanup(dialog.deleteLater)
                self.addCleanup(dialog.close)
                dialog.resize(900, 900)
                dialog.show()
                self.application.processEvents()
                dialog.resize(dialog.width(), 420)
                self.application.processEvents()
                self.assertLessEqual(dialog.height(), 590)
                box = dialog.buttons if isinstance(dialog, NewProjectDialog) else dialog.button_box
                button = box.button(QDialogButtonBox.StandardButton.Ok)
                bottom = button.mapTo(dialog, QPoint(0, button.height())).y()
                self.assertLessEqual(bottom, dialog.height())
                self.assertGreater(dialog.body_scroll.verticalScrollBar().maximum(), 0)
                dialog.body_scroll.verticalScrollBar().setValue(dialog.body_scroll.verticalScrollBar().maximum())
                self.assertEqual(button.mapTo(dialog, QPoint()).y(), bottom - button.height())

    def test_export_disclosure_preserves_custom_values_and_export_type(self):
        dialog = ExportSettingsDialog(AppSettings(), 1, 60, self.window.translator, "audit.mp4")
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        self.assertTrue(dialog.advanced_group.isHidden())
        self.assertTrue(dialog.advanced_check.text())
        dialog.quality_mode_combo.setCurrentIndex(dialog.quality_mode_combo.findData("custom"))
        self.assertFalse(dialog.advanced_group.isHidden())
        dialog.crf_spin.setValue(21)
        dialog.advanced_check.setChecked(False)
        dialog.advanced_check.setChecked(True)
        self.assertEqual(dialog.app_settings.crf, 21)
        for kind, visible in (("subtitles", False), ("audio", True), ("video", True)):
            index = dialog.export_type_combo.findData(kind)
            self.assertGreaterEqual(index, 0)
            dialog.export_type_combo.setCurrentIndex(index)
            self.assertEqual(not dialog.advanced_group.isHidden(), visible)
        custom = ExportSettingsDialog(replace(AppSettings(), crf=21), 1, 60, self.window.translator, "custom.mp4")
        self.addCleanup(custom.deleteLater)
        self.addCleanup(custom.close)
        self.assertFalse(custom.advanced_group.isHidden())

    def test_narrow_inspector_offers_all_categories_and_keeps_header_fixed(self):
        self.window.resize(1100, 680)
        self.window.show()
        self.window._add_source(SourceType.LYRICS)
        self.application.processEvents()
        inspector = self.window.inspector
        selector = inspector.category_selector
        self.assertTrue(selector.isVisible())
        visible = [i for i in range(inspector.property_tabs.count()) if inspector.property_tabs.isTabVisible(i)]
        self.assertEqual([selector.itemData(i) for i in range(selector.count())], visible)
        animation = inspector._tab_indices["animation"]
        selector.setCurrentIndex(selector.findData(animation))
        self.application.processEvents()
        self.assertEqual(inspector.property_tabs.currentIndex(), animation)
        scroll = inspector._category_scrolls["animation"]
        position = inspector.title.mapTo(inspector, QPoint())
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        self.application.processEvents()
        self.assertEqual(inspector.title.mapTo(inspector, QPoint()), position)
        self.assertEqual(inspector.verticalScrollBar().maximum(), 0)

    def test_visible_form_fields_have_distinct_localized_accessibility_names(self):
        inspector = self.window.inspector
        self.window._add_source(SourceType.TEXT)
        for key in ("name", "x", "y", "width", "height"):
            field = inspector._field_widgets[key]
            self.assertEqual(QAccessible.queryAccessibleInterface(field).text(QAccessible.Text.Name), inspector._form_labels[key].text())
            self.assertTrue(field.accessibleName())
        dialog = NewProjectDialog(self.window.translator)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        self.assertNotEqual(dialog.title_edit.accessibleName(), dialog.author_edit.accessibleName())
        self.window.translator.set_language(Language.ENGLISH)
        QTest.qWait(20)
        self.assertEqual(dialog.title_edit.accessibleName(), "Name")
        self.assertEqual(dialog.author_edit.accessibleName(), "Author")

    def test_lyrics_guidance_stays_out_of_output_and_legacy_text_changes_only_on_edit(self):
        self.window._add_source(SourceType.LYRICS)
        inspector = self.window.inspector
        fresh = self.window.store.get(inspector._source_id)
        self.assertEqual((fresh.text, fresh.subtitle_fallback), ("", ""))
        self.assertIn("음악을 추가", inspector.lyrics_output_note.text())
        self.window.playlist_service.add_tracks([
            PlaylistTrack("missing.mp3", "Missing lyrics", duration_seconds=30)
        ])
        self.assertIn("1곡 / 전체 1곡", inspector.lyrics_output_note.text())
        self.assertTrue(inspector.lyrics_search_button.isEnabled())
        old = Source(SourceType.LYRICS, "Existing project", text="User text", subtitle_fallback="Old fallback")
        self.window.store.add(old)
        self.window.store.select(old.id)
        self.assertEqual((old.text, old.subtitle_fallback), ("User text", "Old fallback"))
        inspector.text_edit.editingFinished.emit()
        self.assertEqual((old.text, old.subtitle_fallback), ("User text", "Old fallback"))
        inspector.text_edit.clear()
        inspector.text_edit.textEdited.emit("")
        inspector.text_edit.editingFinished.emit()
        self.assertEqual((old.text, old.subtitle_fallback), ("", ""))
