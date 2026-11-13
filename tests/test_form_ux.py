"""Focused UI regression tests for compact dialogs and quick discovery."""

import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFormLayout, QLabel, QLineEdit

from app.dialogs.playlist_export_dialog import PlaylistExportDialog
from app.dialogs.settings_dialog import SettingsDialog
from app.dialogs.shortcuts_dialog import ShortcutsDialog
from app.services.app_settings_service import AppSettings
from app.services.playlist_export_service import TimestampFormat
from app.services.theme_service import Theme
from app.ui.form_layout import style_form, style_dialog_actions
from app.utils.i18n import Translator


class FormUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_shared_form_changes_only_layout_not_form_values(self):
        form = QFormLayout()
        field = QLineEdit("original")
        form.addRow(QLabel("Name"), field)
        style_form(form)
        self.assertEqual(form.horizontalSpacing(), 18)
        self.assertEqual(form.verticalSpacing(), 10)
        self.assertEqual(field.text(), "original")
        self.assertEqual(
            form.rowWrapPolicy(), QFormLayout.RowWrapPolicy.WrapLongRows,
        )
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        style_dialog_actions(box)
        self.assertEqual(box.objectName(), "dialogActions")
        self.assertIsNotNone(box.button(QDialogButtonBox.StandardButton.Cancel))

    def test_settings_search_indexes_form_labels_and_navigates_to_field(self):
        translator = Translator()
        dialog = SettingsDialog(AppSettings(), translator.language, Theme.DARK, translator)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)

        label = dialog.resolution_label.text()
        key = next(
            title for title in dialog._settings_search_targets
            if title.endswith(" · " + label) and title.startswith(dialog.tabs.tabText(1))
        )
        index, widget = dialog._settings_search_targets[key]
        self.assertEqual(index, 1)
        self.assertIs(widget, dialog.resolution_combo)
        dialog.tabs.setCurrentIndex(0)
        dialog.settings_search_edit.setText(label)
        dialog._search_first_setting()
        self.assertEqual(dialog.tabs.currentIndex(), 1)
        dialog.retranslate()
        self.assertIn(key, dialog._settings_search_targets)

    def test_shortcut_filter_preserves_reference_and_section_structure(self):
        dialog = ShortcutsDialog(Translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)
        rows = dialog.table.rowCount()
        self.assertGreater(rows, 10)
        dialog.search_edit.setText("Ctrl+E")
        visible = [i for i in range(rows) if not dialog.table.isRowHidden(i)]
        self.assertEqual(len(visible), 2)  # File section and export action.
        dialog.search_edit.setText("no-such-shortcut-445")
        self.assertTrue(all(dialog.table.isRowHidden(i) for i in range(rows)))
        self.assertFalse(dialog.empty_results.isHidden())
        dialog.search_edit.clear()
        self.assertEqual(dialog.table.rowCount(), rows)
        self.assertTrue(all(not dialog.table.isRowHidden(i) for i in range(rows)))

    def test_companion_export_keeps_existing_output_decisions(self):
        dialog = PlaylistExportDialog(Path.home(), Translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.reject)
        self.assertEqual(dialog.output_directory, Path.home())
        self.assertEqual(dialog.timestamp_format, TimestampFormat.STANDARD)
        dialog.bracketed_radio.setChecked(True)
        self.assertEqual(dialog.timestamp_format, TimestampFormat.BRACKETED)
        self.assertTrue(dialog.copy_description)
        self.assertEqual(dialog.button_box.objectName(), "dialogActions")


if __name__ == "__main__":
    unittest.main()
