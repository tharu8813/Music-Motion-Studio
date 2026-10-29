"""The project chooser keeps empty, recent and keyboard flows usable."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.dialogs.startup_dialog import StartupDialog
from app.models.project import ProjectDocument
from app.services.project_service import ProjectService
from app.services.recent_projects_service import RecentProjectsService
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Translator


class StartupDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def setUp(self):
        self.settings = QSettings()
        previous = self.settings.value("projects/recent", [])
        self.addCleanup(self.settings.setValue, "projects/recent", previous)
        self.settings.remove("projects/recent")
        self.recent = RecentProjectsService()
        self.translator = Translator()
        self.dialog = StartupDialog(self.translator, self.recent)
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.reject)
        self.dialog.show()
        self.app.processEvents()

    def test_empty_chooser_explains_next_step_and_enter_creates_project(self):
        self.assertTrue(self.dialog.recent_empty.isVisible())
        self.assertEqual(self.dialog.recent_list.count(), 0)
        self.assertFalse(self.dialog.remove_button.isVisible())
        self.assertFalse(self.dialog.open_recent_button.isVisible())
        QTest.keyClick(self.dialog, Qt.Key.Key_Return)
        self.assertEqual(self.dialog.action, StartupDialog.NEW_PROJECT)

    def test_recent_selection_and_remove_return_to_empty_without_deleting_file(self):
        with TemporaryDirectory() as raw:
            path = Path(raw) / "Evening.project.json"
            ProjectService.save(path, ProjectDocument())
            self.recent.add(path)
            self.app.processEvents()
            self.assertFalse(self.dialog.recent_empty.isVisible())
            self.assertEqual(self.dialog.recent_list.count(), 1)
            self.assertTrue(self.dialog.open_recent_button.isVisible())
            self.assertTrue(self.dialog.open_recent_button.isEnabled())
            self.assertEqual(self.dialog._selected_recent_path(), path.resolve())
            self.dialog._remove_recent()
            self.app.processEvents()
            self.assertTrue(self.dialog.recent_empty.isVisible())
            self.assertFalse(self.dialog.remove_button.isVisible())
            self.assertTrue(path.is_file())
