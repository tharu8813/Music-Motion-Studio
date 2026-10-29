"""Settings → Maintenance: the integrity check, reset, and the once-only guides."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hashlib  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.dialogs.welcome_dialog import AUTOMIX_GUIDE, AUTOMIX_GUIDE_TITLE, MAIN_GUIDE, MAIN_GUIDE_TITLE, GuideDialog, first_time  # noqa: E402
from app.services.maintenance_service import MANIFEST_NAME, reset_program_data, verify_installation  # noqa: E402


class MaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # Always a test store: the reset test clears it, and must never touch the user's settings.
        cls._organization = QApplication.organizationName()
        QApplication.setOrganizationName("Music Motion Studio Maintenance Tests")

    @classmethod
    def tearDownClass(cls):
        QSettings().clear()
        QApplication.setOrganizationName(cls._organization)

    def test_integrity_finds_missing_and_damaged_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "sub").mkdir()
            files = {"a.dll": b"alpha", "sub/b.pyd": b"beta", "c.txt": b"gamma"}
            for name, data in files.items():
                (root / name).write_bytes(data)
            (root / MANIFEST_NAME).write_text(json.dumps({"files": {
                name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}), encoding="utf-8")
            report = verify_installation(root)
            self.assertTrue(report.ok)
            self.assertEqual(report.checked, 3)
            (root / "sub/b.pyd").write_bytes(b"tampered")
            (root / "c.txt").unlink()
            report = verify_installation(root)
            self.assertEqual((report.missing, report.damaged), (["c.txt"], ["sub/b.pyd"]))
            self.assertIsNone(verify_installation(root, lambda _fraction: False))  # cancelled

    def test_reset_clears_settings_but_keeps_ffmpeg_and_migration(self):
        settings = QSettings()
        settings.setValue("export/ffmpeg_path", "C:/ffmpeg.exe")
        settings.setValue("migration/playlist_canvas_brand", True)
        settings.setValue("migration/music_motion_studio_brand", True)
        settings.setValue("language", "en")
        settings.setValue("guides/welcome_seen", True)
        with patch("app.automix.cache.clear_caches") as clear_caches, \
                patch("app.services.maintenance_service.QStandardPaths.writableLocation", return_value=""):
            reset_program_data()
        clear_caches.assert_called_once()
        settings = QSettings()
        self.assertEqual(settings.value("export/ffmpeg_path"), "C:/ffmpeg.exe")
        self.assertTrue(settings.contains("migration/playlist_canvas_brand"))
        self.assertTrue(settings.value("migration/music_motion_studio_brand", False, bool))
        self.assertFalse(settings.contains("language"))
        self.assertFalse(settings.contains("guides/welcome_seen"))
        settings.clear()

    def test_guides_show_once_and_page_through(self):
        QSettings().remove("guides/test")
        self.assertTrue(first_time("guides/test"))
        self.assertFalse(first_time("guides/test"))
        QSettings().remove("guides/test")
        for pages, title in ((MAIN_GUIDE, MAIN_GUIDE_TITLE), (AUTOMIX_GUIDE, AUTOMIX_GUIDE_TITLE)):
            dialog = GuideDialog(pages, title, korean=False)
            self.addCleanup(dialog.deleteLater)
            for _page in pages:
                dialog.art.grab()  # paints the illustration
                dialog.next_button.click()
            self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)


if __name__ == "__main__":
    unittest.main()
