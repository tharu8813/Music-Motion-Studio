"""First-run guidance: empty-state recovery, save-state honesty, undo hints, localized Qt text."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

from app.dialogs.new_project_dialog import NewProjectDialog
from app.dialogs.startup_dialog import StartupDialog
from app.dialogs.export_settings_dialog import ExportSettingsDialog
from PySide6.QtWidgets import QDialog
from app.models.playlist import PlaylistTrack
from app.utils.i18n import Language
from tests.main_window_base import MainWindowTestCase


class UxGuidanceTests(MainWindowTestCase):
    def _track(self, title: str = "Song", *, enabled: bool = True) -> PlaylistTrack:
        return PlaylistTrack(f"C:/music/{title}.mp3", title, duration_seconds=120.0, enabled=enabled)

    # -- empty Playlist: Preview/Export explain and offer the fix ------------

    def test_empty_preview_offers_to_add_music_and_then_opens_preview(self) -> None:
        self.window.bottom_tabs.setCurrentIndex(0)

        def add_music() -> None:
            self.window.playlist_service.add_tracks([self._track()])

        with (
            patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes) as warning,
            patch.object(self.window, "_choose_audio_files", side_effect=add_music) as choose,
            patch.object(self.window.preview_controller, "show_export_preview") as show_preview,
        ):
            self.window.bottom_tabs.setCurrentIndex(2)

        warning.assert_called_once()
        self.assertIn("곡이 없습니다", warning.call_args.args[2])
        choose.assert_called_once_with()
        show_preview.assert_called_once()

    def test_excluded_tracks_explain_how_to_include_them_instead_of_adding_music(self) -> None:
        self.window.playlist_service.add_tracks([self._track(enabled=False)])
        with (
            patch.object(QMessageBox, "warning") as warning,
            patch.object(self.window, "_choose_audio_files") as choose,
        ):
            self.window.bottom_tabs.setCurrentIndex(2)

        warning.assert_called_once()
        self.assertIn("제외", warning.call_args.args[2])
        choose.assert_not_called()
        self.assertEqual(self.window.bottom_tabs.currentIndex(), 0)
        self.assertIsNone(self.window._inline_preview)

    def test_export_without_tracks_is_localized_and_stops_when_declined(self) -> None:
        with (
            patch("app.controllers.export_controller.FFmpegRenderer"),
            patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No) as warning,
            patch("app.controllers.export_controller.ExportSettingsDialog") as settings_dialog,
        ):
            self.window._export_video()

        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "영상 내보내기")
        settings_dialog.assert_not_called()

    def test_empty_export_offers_music_before_ffmpeg_setup(self) -> None:
        from app.renderer.ffmpeg_renderer import FFmpegNotFoundError

        with (
            patch(
                "app.controllers.export_controller.FFmpegRenderer",
                side_effect=FFmpegNotFoundError("missing"),
            ) as renderer,
            patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No) as warning,
        ):
            self.window._export_video()

        warning.assert_called_once()
        self.assertIn("곡이 없습니다", warning.call_args.args[2])
        renderer.assert_not_called()

    def test_ffmpeg_prompt_cancel_or_close_does_not_open_settings(self) -> None:
        from app.renderer.ffmpeg_renderer import FFmpegNotFoundError

        self.window.playlist_service.add_tracks([self._track()])
        # NoButton is what closing the message box with X returns.
        for answer in (QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.NoButton):
            with (
                patch.object(ExportSettingsDialog, "exec", return_value=QDialog.DialogCode.Accepted),
                patch(
                    "app.controllers.export_controller.FFmpegRenderer",
                    side_effect=FFmpegNotFoundError("missing"),
                ),
                patch.object(QMessageBox, "warning", return_value=answer),
                patch.object(self.window, "_show_settings") as show_settings,
            ):
                self.window._export_video()
            show_settings.assert_not_called()

    def test_mixed_audio_and_playlist_import_reports_every_added_song(self) -> None:
        with (
            patch.object(self.window, "_import_audio_files", return_value=(2, [], [])),
            patch.object(self.window, "_import_m3u_playlists", return_value=(3, [], [])),
        ):
            self.window._add_music_paths([Path("C:/music/a.mp3"), Path("C:/music/list.m3u8")])

        self.assertIn("5곡", self.window.statusBar().currentMessage())

    # -- save state, window title, feedback ------------------------------

    def test_never_saved_project_is_not_reported_as_saved(self) -> None:
        self.window.current_project_path = None
        self.window.project_session.dirty = False
        self.window.project_controller.update_status()
        self.assertIn("저장 전", self.window.project_status_label.text())
        self.assertIn("새 프로젝트", self.window.windowTitle())
        self.assertFalse(self.window.isWindowModified())

        self.window.project_session.dirty = True
        self.window.project_controller.update_status()
        self.assertIn("저장 필요", self.window.project_status_label.text())
        self.assertTrue(self.window.isWindowModified())

        with TemporaryDirectory() as folder:
            self.window.current_project_path = Path(folder) / "mix.pvsproj"
            self.window.project_session.dirty = False
            self.window.project_controller.update_status()
            self.assertIn("저장됨", self.window.project_status_label.text())
        self.window.current_project_path = None

    def test_removing_tracks_names_undo_in_the_status_bar(self) -> None:
        self.window.playlist_service.add_tracks([self._track("A"), self._track("B")])
        self.application.processEvents()
        self.window.playlist_editor.list_widget.selectAll()
        self.window.playlist_editor.remove_selected()

        self.assertEqual(self.window.playlist_service.tracks, [])
        message = self.window.statusBar().currentMessage()
        self.assertIn("2곡", message)
        self.assertIn("Ctrl+Z", message)

    def test_preview_and_export_have_shortcuts(self) -> None:
        self.assertEqual(self.window.preview_action.shortcut().toString(), "Ctrl+Alt+3")
        self.assertEqual(self.window.export_action.shortcut().toString(), "Ctrl+E")
        self.assertIn("Ctrl+E", self.window.export_action.toolTip())

    # -- startup and new-project dialogs --------------------------------

    def test_startup_clear_list_asks_first(self) -> None:
        dialog = StartupDialog(self.window.translator, self.window.recent_projects, self.window)
        self.addCleanup(dialog.deleteLater)
        with (
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No),
            patch.object(self.window.recent_projects, "clear") as clear,
        ):
            dialog._clear_recent()  # the button itself is disabled while the list is empty
        clear.assert_not_called()
        with (
            patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes),
            patch.object(self.window.recent_projects, "clear") as clear,
        ):
            dialog._clear_recent()  # the button itself is disabled while the list is empty
        clear.assert_called_once_with()

    def test_startup_opens_a_dropped_project_file(self) -> None:
        dialog = StartupDialog(self.window.translator, self.window.recent_projects, self.window)
        self.addCleanup(dialog.deleteLater)
        with TemporaryDirectory() as folder:
            project = Path(folder) / "mix.pvsproj"
            project.write_bytes(b"")
            data = QMimeData()
            data.setUrls([QUrl.fromLocalFile(str(project))])
            event = QDropEvent(
                QPointF(10, 10), Qt.DropAction.CopyAction, data,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            dialog.dropEvent(event)
            self.assertEqual(dialog.action, StartupDialog.OPEN_PROJECT)
            self.assertEqual(dialog.project_path, project)
            self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)

    def test_new_project_accepts_dropped_music_and_marks_create_as_primary(self) -> None:
        dialog = NewProjectDialog(self.window.translator, self.window)
        self.addCleanup(dialog.deleteLater)
        create = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.assertTrue(create.property("primary"))
        with TemporaryDirectory() as folder:
            song = Path(folder) / "song.mp3"
            notes = Path(folder) / "notes.txt"
            song.write_bytes(b"")
            notes.write_bytes(b"")
            data = QMimeData()
            data.setUrls([QUrl.fromLocalFile(str(song)), QUrl.fromLocalFile(str(notes))])
            for _ in range(2):  # a second drop of the same file is not duplicated
                dialog.dropEvent(QDropEvent(
                    QPointF(10, 10), Qt.DropAction.CopyAction, data,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                ))
        self.assertEqual(dialog.music_paths, [song])

    # -- Qt's own strings follow the UI language ---------------------------

    def test_standard_buttons_follow_the_korean_ui(self) -> None:
        directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        if not Path(directory, "qtbase_ko.qm").is_file():
            self.skipTest("Qt Korean catalog is not installed")
        self.assertIs(self.window.translator.language, Language.KOREAN)
        self.assertNotEqual(QCoreApplication.translate("QPlatformTheme", "Cancel"), "Cancel")
        self.window.translator.set_language(Language.ENGLISH)
        self.assertEqual(QCoreApplication.translate("QPlatformTheme", "Cancel"), "Cancel")
        self.window.translator.set_language(Language.KOREAN)
        self.assertNotEqual(QCoreApplication.translate("QPlatformTheme", "Cancel"), "Cancel")


if __name__ == "__main__":
    unittest.main()
