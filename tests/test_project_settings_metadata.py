"""Read-only provenance and canvas preview in Project Settings."""

from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from app import __version__
from app.dialogs.project_settings_dialog import ProjectSettingsDialog
from app.models.playlist import PlaylistTrack
from app.models.project import CURRENT_VERSION, ProjectDocument, ProjectSettings
from app.models.source import Source, SourceType
from app.utils.i18n import Language, Translator


class ProjectSettingsMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_readonly_thumbnail_is_in_header_and_project_data_stays_editable(self) -> None:
        thumbnail = QPixmap(320, 180)
        thumbnail.fill(QColor("#224466"))
        settings = ProjectSettings(
            title="Demo", author="Creator", created_app_version="1.2.3",
            created_at="2026-09-05T12:34:00+00:00",
            modified_at="2026-09-08T14:00:00+00:00",
        )
        document = ProjectDocument(
            settings=settings,
            playlist=[PlaylistTrack("one.mp3", "One", duration_seconds=12.0)],
            sources=[Source(SourceType.TEXT, "Label")],
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "Demo.pvsproj"
            path.write_bytes(b"project")
            dialog = ProjectSettingsDialog(
                settings, Translator(), thumbnail,
                canvas_size=(1920, 1080), project_document=document,
                project_path=path,
            )
            try:
                self.assertIs(dialog.thumbnail_preview.parentWidget(), dialog.thumbnail_group)
                self.assertFalse(dialog.scroll_area.widget().isAncestorOf(dialog.thumbnail_preview))
                self.assertFalse(dialog.thumbnail_preview.pixmap().isNull())
                self.assertEqual(dialog.tabs.count(), 3)
                self.assertEqual(dialog.details_values["created_app_version"].text(), "1.2.3")
                self.assertEqual(dialog.details_values["current_app_version"].text(), __version__)
                self.assertEqual(dialog.details_values["format_version"].text(), f"v{CURRENT_VERSION}")
                self.assertEqual(dialog.details_values["track_count"].text(), "1")
                self.assertEqual(dialog.details_values["source_count"].text(), "1")
                self.assertEqual(dialog.details_values["canvas_size"].text(), "1920 × 1080")
                self.assertIn(".pvsproj", dialog.details_values["project_format"].text())
                self.assertEqual(dialog.details_values["file_path"].text(), str(path))
                self.assertNotEqual(dialog.details_values["file_size"].text(), "정보 없음")
                expected_mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime("%Y-%m-%d %H:%M")
                self.assertEqual(dialog.details_values["modified_at"].text(), expected_mtime)
                self.assertTrue(bool(dialog.details_values["created_at"].text()))
                self.assertTrue(dialog.details_values["created_at"].text().startswith("2026-09-"))
                self.assertTrue(dialog.details_values["created_app_version"].textInteractionFlags()
                                & Qt.TextInteractionFlag.TextSelectableByMouse)
                dialog.title_edit.setText("Updated")
                dialog._accept()
                self.assertEqual(dialog.selected_settings.title, "Updated")
                self.assertEqual(settings.title, "Demo")
                self.assertEqual(dialog.selected_settings.created_at, settings.created_at)
                self.assertEqual(dialog.selected_settings.created_app_version, "1.2.3")
            finally:
                dialog.close()

    def test_missing_legacy_metadata_is_unknown_not_current_time(self) -> None:
        payload = ProjectDocument().to_dict()
        for key in ("created_at", "modified_at", "created_app_version"):
            payload["settings"].pop(key)
        payload.pop("app_version", None)
        restored = ProjectDocument.from_dict(payload)
        self.assertEqual(restored.settings.created_at, "")
        self.assertEqual(restored.settings.modified_at, "")
        self.assertEqual(restored.settings.created_app_version, "")
        dialog = ProjectSettingsDialog(restored.settings, Translator(), QPixmap())
        try:
            self.assertIn(dialog.details_values["created_at"].text(), ("정보 없음", "Unknown"))
            self.assertIn(dialog.details_values["created_app_version"].text(), ("정보 없음", "Unknown"))
            self.assertIn(dialog.details_values["modified_at"].text(), ("저장 전", "Not saved yet"))
            self.assertTrue(dialog.thumbnail_preview.pixmap().isNull())
            self.assertIn(dialog.thumbnail_preview.text(), ("미리보기 없음", "No preview"))
            self.assertEqual(restored.settings.created_app_version, "")
        finally:
            dialog.close()

    def test_labels_retranslate_without_mutating_metadata(self) -> None:
        translator = Translator()
        original_language = translator.language
        dialog = ProjectSettingsDialog(
            ProjectSettings(created_app_version="1.0.0"), translator, QPixmap()
        )
        try:
            original = dialog.details_values["created_app_version"].text()
            translator.set_language(Language.ENGLISH)
            dialog.retranslate()
            self.assertEqual(dialog.details_labels["created_app_version"].text(), "Created with")
            self.assertEqual(dialog.details_values["created_app_version"].text(), original)
        finally:
            dialog.close()
            translator.set_language(original_language)


if __name__ == "__main__":
    unittest.main()
