"""Upgrade coverage for settings and recovery data across all three product names."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app import LEGACY_PRODUCT_NAMES, PRODUCT_NAME
from main import _configure_product_identity


class ProductIdentityTests(unittest.TestCase):
    @staticmethod
    def settings(name: str) -> QSettings:
        return QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, name, name)

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.previous = (
            self.application.applicationName(), self.application.organizationName(),
            self.application.applicationDisplayName(),
        )
        # tests/__init__.py makes these INI stores disposable, never the registry.
        self.assertEqual(QSettings.defaultFormat(), QSettings.Format.IniFormat)
        for name in (PRODUCT_NAME, *LEGACY_PRODUCT_NAMES):
            self.settings(name).clear()
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.paths = patch(
            "main.QStandardPaths.writableLocation",
            side_effect=lambda _: str(self.root / self.application.applicationName()),
        )
        self.paths.start()

    def tearDown(self) -> None:
        self.paths.stop()
        self.directory.cleanup()
        for name in (PRODUCT_NAME, *LEGACY_PRODUCT_NAMES):
            self.settings(name).clear()
        name, organization, display = self.previous
        self.application.setApplicationName(name)
        self.application.setOrganizationName(organization)
        self.application.setApplicationDisplayName(display)

    def test_current_choices_win_then_canvas_then_original_brand(self) -> None:
        oldest = self.settings(LEGACY_PRODUCT_NAMES[1])
        oldest.setValue("theme", "light")
        oldest.setValue("export/ffmpeg_path", "original/ffmpeg.exe")
        canvas = self.settings(LEGACY_PRODUCT_NAMES[0])
        canvas.setValue("theme", "dark")
        canvas.setValue("language", "ko")
        canvas.setValue("recent/projects", ["기존 프로젝트.pvsproj"])
        current = self.settings(PRODUCT_NAME)
        current.setValue("language", "en")
        for settings in (oldest, canvas, current):
            settings.sync()

        _configure_product_identity(self.application)

        current = QSettings()
        self.assertEqual(self.application.applicationName(), PRODUCT_NAME)
        self.assertEqual(self.application.applicationDisplayName(), PRODUCT_NAME)
        self.assertEqual(self.application.organizationName(), PRODUCT_NAME)
        self.assertEqual(current.value("language"), "en")
        self.assertEqual(current.value("theme"), "dark")
        self.assertEqual(current.value("export/ffmpeg_path"), "original/ffmpeg.exe")
        self.assertEqual(current.value("recent/projects"), ["기존 프로젝트.pvsproj"])

    def test_recoveries_are_copied_from_both_brands_without_overwriting(self) -> None:
        for name in (PRODUCT_NAME, *LEGACY_PRODUCT_NAMES):
            folder = self.root / name / "recoveries"
            folder.mkdir(parents=True)
            (folder / "shared.recovery.json").write_text(name, encoding="utf-8")
        for index, name in enumerate(LEGACY_PRODUCT_NAMES):
            (self.root / name / "recoveries" / f"{index}.recovery.json").write_text(
                name, encoding="utf-8",
            )

        _configure_product_identity(self.application)

        current = self.root / PRODUCT_NAME / "recoveries"
        self.assertEqual((current / "shared.recovery.json").read_text(), PRODUCT_NAME)
        for index, name in enumerate(LEGACY_PRODUCT_NAMES):
            self.assertEqual((current / f"{index}.recovery.json").read_text(), name)
            self.assertTrue((self.root / name / "recoveries" / f"{index}.recovery.json").is_file())

    def test_migration_does_not_restore_deleted_settings_or_recoveries_again(self) -> None:
        canvas = self.settings(LEGACY_PRODUCT_NAMES[0])
        canvas.setValue("theme", "dark")
        canvas.sync()
        _configure_product_identity(self.application)
        current = QSettings()
        current.remove("theme")
        current.sync()
        old = self.root / LEGACY_PRODUCT_NAMES[0] / "recoveries"
        old.mkdir(parents=True)
        (old / "stale.recovery.json").write_text("{}")

        _configure_product_identity(self.application)

        self.assertFalse(QSettings().contains("theme"))
        self.assertFalse((self.root / PRODUCT_NAME / "recoveries/stale.recovery.json").exists())

    def test_copy_failure_keeps_legacy_recovery_and_retries_next_launch(self) -> None:
        old = self.root / LEGACY_PRODUCT_NAMES[0] / "recoveries"
        old.mkdir(parents=True)
        snapshot = old / "retry.recovery.json"
        snapshot.write_text("{}")
        with patch("main.shutil.copy2", side_effect=OSError("locked file")):
            _configure_product_identity(self.application)
        self.assertFalse(QSettings().value("migration/music_motion_studio_brand", False, bool))
        self.assertTrue(snapshot.is_file())
        _configure_product_identity(self.application)
        self.assertTrue((self.root / PRODUCT_NAME / "recoveries/retry.recovery.json").is_file())
        self.assertTrue(QSettings().value("migration/music_motion_studio_brand", False, bool))


if __name__ == "__main__":
    unittest.main()
