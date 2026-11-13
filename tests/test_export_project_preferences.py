"""Project-specific export persistence and package thumbnail regression tests."""

from __future__ import annotations

from dataclasses import asdict
from unittest import TestCase
from unittest.mock import MagicMock, patch

from PySide6.QtGui import QImage

from app.controllers.project_controller import ProjectController
from app.models.project import ProjectDocument, ProjectSettings
from app.services.app_settings_service import AppSettings
from app.services.project_export_preferences import (
    load_export_preferences, remember_export_preferences, settings_for_project,
)


class _MemorySettings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def value(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def setValue(self, key: str, value: str) -> None:  # noqa: N802
        self.values[key] = value


class ProjectExportPreferencesTests(TestCase):
    def test_first_export_uses_app_defaults_and_later_export_is_project_scoped(self) -> None:
        first = ProjectSettings(title="First", created_at="2026-01-01T01:00:00+00:00")
        second = ProjectSettings(title="Second", created_at="2026-01-02T01:00:00+00:00")
        app_defaults = AppSettings(fps=30, crf=18, output_directory="C:/Exports")
        storage = _MemorySettings()
        with patch("app.services.project_export_preferences.QSettings", return_value=storage):
            initial, profile = settings_for_project(app_defaults, first)
            self.assertEqual(initial, app_defaults)
            self.assertEqual(profile, {})
            changed = AppSettings(fps=60, crf=22, output_directory="D:/Videos")
            remember_export_preferences(
                first, changed, engine="ffmpeg", kind="audio", file_format="mp3",
            )
            restored, saved = settings_for_project(app_defaults, first)
            self.assertEqual(restored.fps, 60)
            self.assertEqual(restored.output_directory, "D:/Videos")
            self.assertEqual(saved["engine"], "ffmpeg")
            self.assertEqual(saved["format"], "mp3")
            self.assertEqual(settings_for_project(app_defaults, second)[0], app_defaults)
            self.assertEqual(app_defaults.fps, 30)
            # The values also travel with the project file on its next save.
            loaded = ProjectDocument.from_dict(
                ProjectDocument(settings=first).to_dict()
            )
            self.assertEqual(loaded.settings.export_preferences["fps"], 60)

    def test_creation_version_is_preserved_for_old_projects(self) -> None:
        data = ProjectDocument().to_dict()
        data["app_version"] = "1.1.0"
        data["settings"].pop("created_app_version")
        loaded = ProjectDocument.from_dict(data)
        self.assertEqual(loaded.settings.created_app_version, "1.1.0")
        self.assertEqual(
            ProjectDocument.from_dict(loaded.to_dict()).settings.created_app_version,
            "1.1.0",
        )

    def test_thumbnail_always_captures_canvas_even_with_custom_thumbnail_selected(self) -> None:
        window = MagicMock()
        window.project_settings = ProjectSettings(
            thumbnail_mode="custom", thumbnail_path="C:/external-thumbnail.png",
        )
        source = QImage(640, 360, QImage.Format.Format_ARGB32)
        source.fill(0xFF123456)
        controller = ProjectController(window, MagicMock())
        with patch(
            "app.controllers.project_controller.CanvasSnapshot.capture",
            return_value=source,
        ) as capture:
            thumbnail = controller.thumbnail_image()
        capture.assert_called_once_with(
            window.canvas.scene_model, output_scale=0.5,
        )
        self.assertFalse(thumbnail.isNull())
        self.assertEqual(thumbnail.size().width(), 480)
        self.assertEqual(thumbnail.size().height(), 270)
