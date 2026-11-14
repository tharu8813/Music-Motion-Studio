"""Project settings metadata is read-only, truthful, and localizable."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from app.dialogs.project_settings_dialog import ProjectSettingsDialog, _file_size_text, _timestamp_text
from app.models.project import ProjectSettings
from app.utils.i18n import Language, Translator


class ProjectSettingsMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.translator = Translator()

    def test_existing_project_shows_recorded_metadata_and_read_only_thumbnail(self) -> None:
        with TemporaryDirectory() as directory:
            project = Path(directory) / "demo.pvsproj"
            project.write_bytes(b"saved-payload")
            picture = QPixmap(320, 180)
            picture.fill(QColor("#334455"))
            settings = ProjectSettings(
                title="Night ride", created_at="2026-01-02T11:22:00+00:00",
                created_app_version="1.5.0",
            )
            dialog = ProjectSettingsDialog(
                settings, self.translator, picture, project_path=project,
                project_counts={"tracks": 4, "sources": 13, "groups": 3, "library": 9},
            )
            try:
                dialog.show()
                self.app.processEvents()
                self.assertFalse(dialog.thumbnail_preview.pixmap().isNull())
                self.assertFalse(dialog.scroll_area.widget().isAncestorOf(dialog.thumbnail_group))
                self.assertTrue(dialog.scroll_area.widget().isAncestorOf(dialog.details_group))
                self.assertEqual(dialog.metadata_values["created_version"].text(), "1.5.0")
                self.assertIn("2026", dialog.metadata_values["created_at"].text())
                self.assertEqual(dialog.metadata_values["file_path"].text(), str(project))
                self.assertEqual(dialog.metadata_values["tracks"].text(), "4")
                self.assertEqual(dialog.metadata_values["sources"].text(), "13")
                self.assertEqual(dialog.metadata_values["groups"].text(), "3")
                self.assertEqual(dialog.metadata_values["library"].text(), "9")
                self.assertEqual(dialog.metadata_values["file_size"].text(), "13 B")
                self.assertEqual(dialog.selected_settings, settings)
                dialog.title_edit.setText("After edit")
                self.assertEqual(dialog.thumbnail_project_label.text(), "After edit")
                # Metadata display must not mutate the saved project's identity.
                self.assertEqual(dialog.selected_settings.created_at, settings.created_at)
            finally:
                dialog.close()

    def test_unknown_legacy_fields_do_not_acquire_false_dates_or_versions(self) -> None:
        settings = ProjectSettings(
            title="Old project", created_at="", modified_at="", created_app_version="",
        )
        dialog = ProjectSettingsDialog(settings, self.translator, QPixmap())
        try:
            self.assertEqual(dialog.metadata_values["created_at"].text(), "기록 없음")
            self.assertEqual(dialog.metadata_values["created_version"].text(), "기록 없음")
            self.assertEqual(dialog.metadata_values["last_modified"].text(), "기록 없음")
            self.assertEqual(dialog.metadata_values["file_size"].text(), "기록 없음")
            self.assertEqual(dialog.metadata_values["file_path"].text(), "저장 전")
            self.assertEqual(dialog.thumbnail_preview.text(), "미리보기 없음")
            self.translator.set_language(Language.ENGLISH)
            self.assertEqual(dialog.metadata_values["created_at"].text(), "Not recorded")
            self.assertEqual(dialog.metadata_values["file_path"].text(), "Not saved")
            self.assertEqual(dialog.thumbnail_preview.text(), "No preview")
        finally:
            dialog.close()

    def test_format_helpers_leave_bad_or_missing_dates_blank(self) -> None:
        self.assertEqual(_timestamp_text(""), "")
        self.assertEqual(_timestamp_text("not-a-date"), "")
        self.assertIn("2026-01-02", _timestamp_text("2026-01-02T12:00:00+00:00"))
        self.assertEqual(_file_size_text(None), "")
        self.assertEqual(_file_size_text(1024), "1.0 KB")


if __name__ == "__main__":
    unittest.main()
