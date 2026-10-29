"""Closing the startup chooser leaves safe app-level navigation available."""
from unittest.mock import MagicMock, patch
from pathlib import Path

from PySide6.QtWidgets import QDialog

from tests.main_window_base import MainWindowTestCase


class ProjectlessWorkspaceTests(MainWindowTestCase):
    def test_cancel_startup_keeps_window_open_and_locks_editor(self):
        w = self.window
        w.show()
        dialog = MagicMock()
        dialog.DialogCode = QDialog.DialogCode
        dialog.exec.return_value = QDialog.DialogCode.Rejected
        with patch.object(w, '_offer_recovery', return_value=False), patch(
            'app.controllers.project_controller.StartupDialog', return_value=dialog,
        ):
            self.assertFalse(w.show_startup_dialog())
        self.assertTrue(w.isVisible())
        self.assertFalse(w.centralWidget().isEnabled())
        self.assertFalse(w.project_session.dirty)
        for action in (w.new_action, w.open_action, w.settings_action,
                       w.help_action, w.about_action, w.check_updates_action, w.exit_action):
            self.assertTrue(action.isEnabled(), action.text())
        for action in (w.save_action, w.export_action, w.preview_action,
                       w.delete_action, w.project_settings_action, w.import_m3u_action):
            self.assertFalse(action.isEnabled(), action.text())
            action.setEnabled(True)
            self.assertFalse(action.isEnabled())
        self.assertEqual(w.project_status_label.text(), '열린 프로젝트 없음')
        with patch.object(w.autosave, 'save') as save:
            w._autosave_project()
            save.assert_not_called()

    def test_successful_new_project_unlocks_editor(self):
        w = self.window
        w.set_project_open(False)
        dialog = MagicMock()
        dialog.DialogCode = QDialog.DialogCode
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.canvas_size = (1280, 720)
        dialog.selected_design_preset = None
        dialog.project_settings = w.project_settings
        dialog.music_paths = []
        with patch('app.controllers.project_controller.NewProjectDialog', return_value=dialog):
            self.assertTrue(w._new_project(confirm_unsaved=False))
        self.assertTrue(w.centralWidget().isEnabled())
        self.assertTrue(w.save_action.isEnabled())
        self.assertTrue(w.export_action.isEnabled())
        self.assertFalse(w.undo_action.isEnabled())

    def test_cancel_new_chooser_preserves_existing_project(self):
        w = self.window
        sources = [source.id for source in w.store.sources()]
        dialog = MagicMock()
        dialog.DialogCode = QDialog.DialogCode
        dialog.exec.return_value = QDialog.DialogCode.Rejected
        with patch.object(w, '_confirm_unsaved_changes', return_value=True), patch(
            'app.controllers.project_controller.StartupDialog', return_value=dialog,
        ):
            self.assertFalse(w._show_project_start_dialog())
        self.assertTrue(w.centralWidget().isEnabled())
        self.assertEqual([source.id for source in w.store.sources()], sources)

    def test_load_project_unlocks_project_tools(self):
        w = self.window
        document = w._project_document()
        w.set_project_open(False)
        with patch.object(w.project_controller, '_load_document_off_gui_thread', return_value=document), patch.object(
            w, '_resolve_project_media', return_value=True,
        ):
            self.assertTrue(w._load_project_path(Path('example.pvsproj')))
        self.assertTrue(w.centralWidget().isEnabled())
        self.assertTrue(w.save_action.isEnabled())

    def test_update_check_can_run_without_a_project(self):
        w = self.window
        w.set_project_open(False)
        with patch('app.ui.main_window.QTimer.singleShot') as schedule, patch.object(w, '_check_for_updates') as check:
            w.schedule_automatic_update_check()
            callback = schedule.call_args.args[-1]
            callback()
            check.assert_called_once_with(manual=False)
