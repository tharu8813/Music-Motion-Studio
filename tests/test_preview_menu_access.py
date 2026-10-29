"""Preview menu access, exit confirmation, and editor lock regressions."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QFrame, QMessageBox, QWidget

from app.models.playlist import PlaylistTrack
from app.models.source import SourceType
from tests.main_window_base import MainWindowTestCase


class StubPreview(QDialog):
    def __init__(self, *_args, **kwargs):
        super().__init__(kwargs.get("parent"))
        self.setWindowFlags(Qt.WindowType.Widget)
        self.controls_page = QWidget()
        self.track_list_panel = QFrame()
        self.stopped = False

    def build_embedded_controls_page(self):
        return self.controls_page

    def _stop_preview(self):
        self.stopped = True


class PreviewMenuAccessTests(MainWindowTestCase):
    def setUp(self):
        super().setUp()
        self.window.playlist_service.add_tracks([
            PlaylistTrack("preview.wav", "Preview", duration_seconds=10),
        ])
        with patch("app.controllers.preview_controller.ExportPreviewDialog", StubPreview):
            self.window._show_bottom_panel(2)
        self.preview = self.window._inline_preview
        self.assertIsNotNone(self.preview)

    @staticmethod
    def _answer_warning(box, *, proceed=False, escape=False):
        # Exercise the real modal dialog, including its default and Escape behavior.
        if escape:
            QTimer.singleShot(0, lambda: QTest.keyClick(box, Qt.Key.Key_Escape))
        else:
            role = QMessageBox.ButtonRole.AcceptRole if proceed else QMessageBox.ButtonRole.RejectRole
            button = next(b for b in box.buttons() if box.buttonRole(b) == role)
            QTimer.singleShot(0, button.click)
        return _MESSAGE_BOX_EXEC(box)

    def test_all_top_level_menus_and_safe_actions_remain_available(self):
        for action in self.window.menuBar().actions():
            with self.subTest(menu=action.text()):
                self.assertTrue(action.isEnabled())
        for action in (
            self.window.save_action, self.window.save_as_action,
            self.window.export_m3u_action, self.window.save_preset_action,
            self.window.welcome_guide_action, self.window.help_action,
            self.window.shortcuts_action, self.window.about_action,
            self.window.check_updates_action, self.window.preview_action,
        ):
            with self.subTest(action=action.text()):
                self.assertTrue(action.isEnabled())
        with patch("app.ui.main_window.HelpDialog") as help_dialog, \
             patch.object(self.window.project_controller, "save") as save, \
             patch("app.ui.main_window.QFileDialog.getSaveFileName", return_value=("", "")) as choose:
            self.window.help_action.trigger()
            self.window.save_action.trigger()
            self.window.export_m3u_action.trigger()
        help_dialog.return_value.exec.assert_called_once()
        save.assert_called_once()
        choose.assert_called_once()
        self.assertIs(self.window._inline_preview, self.preview)
        self.assertFalse(self.preview.stopped)

    def test_guarded_actions_use_the_gate_including_all_insert_items(self):
        actions = (
            self.window.new_action, self.window.open_action,
            self.window.export_action, self.window.playlist_files_action,
            self.window.project_settings_action, self.window.presets_action,
            self.window.import_m3u_action, self.window.settings_action,
            self.window.lyrics_search_action, self.window.lrc_generator_action,
            self.window.mp3_metadata_action, self.window.reset_layout_action,
            self.window.show_playlist_action, self.window.show_timeline_action,
            self.window.exit_action, *self.window.source_insert_actions.values(),
        )
        for action in actions:
            with self.subTest(action=action.text()), \
                 patch.object(self.window.preview_controller, "confirm_exit_for_action", return_value=False) as gate:
                self.assertTrue(action.isEnabled())
                action.trigger()
                gate.assert_called_once_with(action)
        self.assertIs(self.window._inline_preview, self.preview)

    def test_close_and_escape_keep_preview_and_sources_unchanged(self):
        before = [source.to_dict() for source in self.window.store.sources()]
        for escape in (False, True):
            boxes = []

            def answer(box):
                boxes.append(box)
                return self._answer_warning(box, escape=escape)

            with self.subTest(escape=escape), patch.object(QMessageBox, "exec", answer):
                self.window.source_insert_actions[SourceType.TEXT].trigger()
                self.assertEqual({b.text() for b in boxes[0].buttons()}, {
                    "닫기", "미리보기 모드 해제 및 진행",
                })
                self.assertEqual(boxes[0].defaultButton().text(), "닫기")
                self.assertIs(self.window._inline_preview, self.preview)
                self.assertFalse(self.preview.stopped)
                self.assertEqual(before, [source.to_dict() for source in self.window.store.sources()])

    def test_continue_stops_preview_and_restores_editor_before_running_action_once(self):
        observed = []

        def add_source(kind):
            observed.append((kind, self.window._inline_preview,
                             self.window._preview_ui_lock_state,
                             self.window.canvas.isEnabled(), self.preview.stopped))

        with patch.object(QMessageBox, "exec", lambda box: self._answer_warning(box, proceed=True)), \
             patch.object(self.window, "_add_source", side_effect=add_source):
            self.window.source_insert_actions[SourceType.TEXT].trigger()
        self.assertEqual(observed, [(SourceType.TEXT, None, None, True, True)])
        self.assertNotEqual(self.window.bottom_tabs.currentIndex(), 2)
        self.assertTrue(self.window.left_workspace.isEnabled())
        self.assertTrue(self.window.acceptDrops())

    def test_rebuilt_recent_project_menu_also_requires_preview_exit(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "recent.pvsproj"
            target.touch()
            with patch.object(self.window.recent_projects, "projects", return_value=[target]):
                self.window._rebuild_recent_projects_menu()
            action = self.window.recent_projects_menu.actions()[0]
            with patch.object(QMessageBox, "exec", self._answer_warning), \
                 patch.object(self.window, "_open_project_with_confirmation") as open_project:
                action.trigger()
            open_project.assert_not_called()
            self.assertIs(self.window._inline_preview, self.preview)
            with patch.object(QMessageBox, "exec", lambda box: self._answer_warning(box, proceed=True)), \
                 patch.object(self.window, "_open_project_with_confirmation") as open_project:
                action.trigger()
            open_project.assert_called_once_with(target)
            self.assertIsNone(self.window._inline_preview)

    def test_ctrl_o_uses_the_same_confirmation(self):
        self.window.show()
        self.window.activateWindow()
        self.preview.setFocus()
        QTest.qWait(250)  # allow the preview-loading popup to release modality
        self.application.processEvents()
        with patch.object(self.window.preview_controller, "confirm_exit_for_action", return_value=False) as gate:
            QTest.keyClick(self.window, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
        gate.assert_called_once_with(self.window.open_action)
        self.assertIs(self.window._inline_preview, self.preview)

    def test_focus_changes_and_clipboard_refresh_cannot_unlock_canvas_edits(self):
        self.window._sync_canvas_shortcut_actions(None, self.window.canvas)
        self.window._update_alignment_toolbar_actions()
        for action in (
            self.window.cut_action, self.window.copy_action,
            self.window.paste_action, self.window.duplicate_action,
            self.window.delete_action, self.window.select_all_action,
            self.window.clear_selection_action, self.window.grid_action,
            self.window.panels_action, self.window.inspector_panel_action,
            self.window.bottom_panel_action, self.window.center_horizontal_action,
            *self.window._canvas_shortcut_actions,
        ):
            with self.subTest(action=action.text()):
                self.assertFalse(action.isEnabled())
        self.assertFalse(self.window.left_workspace.isEnabled())

    def test_unlock_preserves_busy_state_of_safe_actions(self):
        # Save and update workers may change these flags while playback is open.
        self.window.save_action.setEnabled(False)
        self.window.check_updates_action.setEnabled(False)
        self.window._finish_inline_preview()
        self.assertFalse(self.window.save_action.isEnabled())
        self.assertFalse(self.window.check_updates_action.isEnabled())

    def test_history_refresh_still_routes_undo_and_redo_through_the_gate(self):
        self.window.history_controller.update_actions(True, True)
        with patch.object(self.window.preview_controller, "confirm_exit_for_action", return_value=False) as gate:
            self.window.undo_action.trigger()
            self.window.redo_action.trigger()
        self.assertEqual([call.args[0] for call in gate.call_args_list], [
            self.window.undo_action, self.window.redo_action,
        ])
        self.assertIs(self.window._inline_preview, self.preview)

    def test_nested_action_while_warning_is_open_does_not_run_or_prompt_again(self):
        warning_count = 0

        def answer(box):
            nonlocal warning_count
            warning_count += 1
            self.window.source_insert_actions[SourceType.TEXT].trigger()
            return self._answer_warning(box)

        with patch.object(QMessageBox, "exec", answer), \
             patch.object(self.window, "_add_source") as add:
            self.window.source_insert_actions[SourceType.TEXT].trigger()
        self.assertEqual(warning_count, 1)
        add.assert_not_called()
        self.assertIs(self.window._inline_preview, self.preview)

    def test_action_outside_preview_runs_without_warning(self):
        self.window._finish_inline_preview()
        with patch.object(QMessageBox, "exec") as warning, \
             patch.object(self.window, "_add_source") as add:
            self.window.source_insert_actions[SourceType.TEXT].trigger()
        warning.assert_not_called()
        add.assert_called_once_with(SourceType.TEXT)


_MESSAGE_BOX_EXEC = QMessageBox.exec
