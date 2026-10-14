"""Main window project lifecycle: new/open/save/load, recovery, updates and presets."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QGraphicsView, QMessageBox
from app import __version__
from app.models.project import ProjectDocument, ProjectSettings
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.dialogs.preset_dialog import DesignPresetDialog
from app.dialogs.audio_metadata_dialog import AudioMetadataDialog
from app.dialogs.new_project_dialog import NewProjectDialog
from app.dialogs.startup_dialog import StartupDialog
from app.dialogs.project_settings_dialog import ProjectSettingsDialog
from app.dialogs.project_crash_report_dialog import ProjectCrashReportDialog
from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.services.autosave_service import AutosaveService, RecoverySnapshot
from app.services.project_service import ProjectError, ProjectService
from app.services.update_service import ReleaseInfo
from app.services.playlist_service import AudioImportCandidate, PlaylistService
from app.services.lrc_draft_service import LrcDraftService
from app.utils.i18n import Language
from app.presets.preset_service import PresetService
from tests.main_window_base import MainWindowTestCase


class MainWindowProjectTests(MainWindowTestCase):
    def test_new_project_cancel_preserves_unsaved_workspace(self) -> None:
        marker = Source(SourceType.TEXT, "UNSAVED_TEST_MARKER")
        self.window.store.add(marker)
        self.window._project_dirty = True
        with patch.object(
            QMessageBox, "warning", return_value=QMessageBox.StandardButton.Cancel,
        ):
            self.window._new_project()
        self.assertIsNotNone(self.window.store.get(marker.id))

    def test_new_action_reuses_startup_project_chooser(self) -> None:
        startup_dialog = MagicMock()
        startup_dialog.action = StartupDialog.NEW_PROJECT
        startup_dialog.project_path = None
        startup_dialog.DialogCode = QDialog.DialogCode
        startup_dialog.exec.side_effect = (
            QDialog.DialogCode.Accepted,
            QDialog.DialogCode.Rejected,
        )
        with (
            patch(
                "app.controllers.project_controller.StartupDialog", return_value=startup_dialog,
            ) as startup_class,
            patch.object(
                NewProjectDialog, "exec", return_value=QDialog.DialogCode.Rejected,
            ) as creation_exec,
        ):
            startup_class.NEW_PROJECT = StartupDialog.NEW_PROJECT
            self.window.new_action.trigger()

        self.assertEqual(startup_class.call_count, 2)
        creation_exec.assert_called_once()

    def test_damaged_project_shows_detailed_report_and_preserves_workspace(self) -> None:
        marker = Source(SourceType.TEXT, "KEEP_CURRENT_PROJECT")
        self.window.store.add(marker)
        self.window.store.select(marker.id)
        self.window._project_dirty = True
        original_path = Path("current-project.pvsproj").resolve()
        self.window.current_project_path = original_path
        before = self.window._project_document().to_dict()

        with TemporaryDirectory() as directory:
            damaged = Path(directory) / "damaged.project.json"
            damaged.write_text('{"version": 2, "sources": [', encoding="utf-8")
            with patch.object(
                ProjectCrashReportDialog, "exec",
                return_value=QDialog.DialogCode.Rejected,
            ) as report:
                loaded = self.window._load_project_path(damaged)

        self.assertFalse(loaded)
        report.assert_called_once()
        self.assertEqual(self.window._project_document().to_dict(), before)
        self.assertEqual(self.window.current_project_path, original_path)
        self.assertTrue(self.window._project_dirty)
        self.assertEqual(self.window.store.selected_ids, (marker.id,))
        self.assertTrue(self.window.isEnabled())
        self.assertNotIn("project_load", self.window.activity_progress.active_keys)

    def test_project_apply_failure_rolls_back_partial_workspace_changes(self) -> None:
        marker = Source(SourceType.TEXT, "ROLLBACK_MARKER")
        self.window.store.add(marker)
        self.window.store.select(marker.id)
        self.window._project_dirty = True
        before = self.window._project_document().to_dict()
        incoming = ProjectDocument.from_dict(before)
        incoming.settings.title = "Incoming project"
        incoming.sources = [Source(SourceType.TEXT, "INCOMING_SOURCE")]

        original_replace = self.window.playlist_service.replace
        calls = 0

        def fail_once(tracks: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated apply failure")
            original_replace(tracks)  # type: ignore[arg-type]

        with (
            patch.object(ProjectService, "load", return_value=incoming),
            patch.object(self.window, "_resolve_project_media", return_value=True),
            patch.object(self.window.playlist_service, "replace", side_effect=fail_once),
            patch.object(
                ProjectCrashReportDialog, "exec",
                return_value=QDialog.DialogCode.Rejected,
            ) as report,
        ):
            loaded = self.window._load_project_path(Path("broken-apply.pvsproj"))

        self.assertFalse(loaded)
        report.assert_called_once()
        self.assertEqual(self.window._project_document().to_dict(), before)
        self.assertTrue(self.window._project_dirty)
        self.assertEqual(self.window.store.selected_ids, (marker.id,))
        self.assertNotIn("project_load", self.window.activity_progress.active_keys)

    def test_loading_project_from_different_app_version_warns_and_continues(self) -> None:
        incoming = ProjectDocument(app_version="9.8.7")
        with (
            patch.object(ProjectService, "load", return_value=incoming),
            patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok) as warning,
        ):
            loaded = self.window._load_project_path(Path("different-version.pvsproj"))

        self.assertTrue(loaded)
        warning.assert_called_once()
        message = warning.call_args.args[2]
        self.assertIn("9.8.7", message)
        self.assertIn(__version__, message)

    def test_loading_legacy_or_same_version_project_does_not_warn(self) -> None:
        for app_version in ("", __version__):
            with self.subTest(app_version=app_version), patch.object(
                ProjectService, "load",
                return_value=ProjectDocument(app_version=app_version),
            ), patch.object(QMessageBox, "warning") as warning:
                self.assertTrue(
                    self.window._load_project_path(Path("compatible-version.pvsproj"))
                )
                warning.assert_not_called()

    def test_project_file_is_read_off_the_gui_thread(self) -> None:
        import threading

        load_threads: list[threading.Thread] = []

        def load(_path: object, **_options: object) -> ProjectDocument:
            load_threads.append(threading.current_thread())
            return ProjectDocument(app_version=__version__)

        with patch.object(ProjectService, "load", side_effect=load):
            self.assertTrue(self.window._load_project_path(Path("large.pvsproj")))

        self.assertEqual(len(load_threads), 1)
        self.assertIsNot(load_threads[0], threading.main_thread())

    def test_slow_project_load_shows_cancellable_progress_and_keeps_workspace(self) -> None:
        from PySide6.QtWidgets import QProgressDialog, QPushButton
        from app.services.project_service import ProjectLoadCancelled

        before = self.window._project_document().to_dict()
        seen: dict[str, object] = {}

        def slow_load(_path: object, *, progress, cancel_event) -> ProjectDocument:
            progress(512, 1024)
            if not cancel_event.wait(10):
                return ProjectDocument(app_version=__version__)
            raise ProjectLoadCancelled("Project loading was cancelled.")

        def cancel_dialog() -> None:
            dialog = self.window.findChild(QProgressDialog)
            seen["dialog"] = dialog
            if dialog is not None:
                seen["value"] = dialog.value()
                dialog.findChild(QPushButton).click()

        QTimer.singleShot(900, cancel_dialog)
        with patch.object(ProjectService, "load", side_effect=slow_load):
            loaded = self.window._load_project_path(Path("huge.pvsproj"))

        self.assertIsNotNone(seen.get("dialog"))
        self.assertEqual(seen["value"], 500)
        self.assertFalse(loaded)
        self.assertEqual(self.window._project_document().to_dict(), before)
        self.assertNotIn("project_load", self.window.activity_progress.active_keys)

    def test_startup_offers_recovery_before_project_choice(self) -> None:
        with patch.object(self.window, "_offer_recovery", return_value=True) as offer:
            with patch("app.controllers.project_controller.StartupDialog") as startup_dialog:
                self.assertTrue(self.window.show_startup_dialog())
        offer.assert_called_once_with()
        startup_dialog.assert_not_called()

    def test_update_backs_up_and_resumes_workspace_without_confirmation(self) -> None:
        from main import open_initial_workspace

        with TemporaryDirectory(prefix="pvs-unattended-update-") as directory:
            root = Path(directory)
            installer = root / "Playlist Canvas setup.exe"
            installer.write_bytes(b"verified by download worker")
            self.window.autosave = AutosaveService(root)
            self.window.store.replace([Source(SourceType.TEXT, "Unsaved update work")])
            self.window.playlist_service.replace([])
            for dirty, project_path in ((True, None), (False, root / "saved.pvsproj")):
                with self.subTest(dirty=dirty):
                    if project_path is not None:
                        project_path.write_bytes(b"original project is never overwritten")
                    self.window.current_project_path = project_path
                    self.window._project_dirty = dirty
                    expected = self.window._project_document().to_dict()
                    with patch("app.ui.main_window.QProcess.startDetached", return_value=(True, 1234)) as launch, \
                         patch.object(self.window, "close") as close, \
                         patch.object(QMessageBox, "question", side_effect=AssertionError("Extra confirmation")), \
                         patch.object(QMessageBox, "warning", side_effect=AssertionError("Extra save prompt")):
                        self.window._launch_update_setup(installer)
                        arguments = launch.call_args.args[1]
                        recovery_path = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("/UPDATERECOVERY=")))
                        snapshot = self.window.autosave.load_snapshot(recovery_path)
                        self.assertEqual(snapshot.document.to_dict(), expected)
                        self.assertEqual(snapshot.project_path, project_path)
                        self.assertEqual(snapshot.dirty, dirty)
                        self.assertTrue(self.window._update_install_authorized)
                        close.assert_called_once()
                        self.window.store.replace([])
                        self.assertTrue(open_initial_workspace(self.window, None, update_recovery=recovery_path))
                        self.assertEqual(self.window._project_document().to_dict(), expected)
                        self.assertEqual(self.window.current_project_path, project_path)
                        self.assertEqual(self.window._project_dirty, dirty)
                    if project_path is not None:
                        self.assertEqual(project_path.read_bytes(), b"original project is never overwritten")
            self.window._update_install_authorized = False

    def test_update_save_failure_and_setup_start_failure_keep_editor_open(self) -> None:
        with TemporaryDirectory(prefix="pvs-update-errors-") as directory:
            installer = Path(directory) / "setup.exe"
            installer.write_bytes(b"verified")
            self.window._project_dirty = True
            with patch.object(self.window.autosave, "save", side_effect=ProjectError("disk full")), \
                 patch.object(self.window, "_show_project_error") as error, \
                 patch("app.ui.main_window.QProcess.startDetached") as launch, \
                 patch.object(self.window, "close") as close:
                self.window._launch_update_setup(installer)
                error.assert_called_once()
                launch.assert_not_called()
                close.assert_not_called()
            self.window.autosave = AutosaveService(Path(directory))
            with patch("app.ui.main_window.QProcess.startDetached", return_value=(False, -1)), \
                 patch.object(QMessageBox, "critical") as error, patch.object(self.window, "close") as close:
                self.window._launch_update_setup(installer)
                error.assert_called_once()
                close.assert_not_called()
                self.assertFalse(self.window._update_install_authorized)
                self.assertIsNotNone(self.window.autosave.latest_recovery())

    def test_update_waits_for_busy_work_instead_of_canceling_it(self) -> None:
        render = MagicMock()
        render.isRunning.return_value = True
        with patch.object(self.window, "_render_worker", render), \
             patch("app.ui.main_window.QTimer.singleShot") as retry, \
             patch("app.ui.main_window.QProcess.startDetached") as launch:
            self.window._launch_update_setup(Path("setup.exe"))
            self.assertEqual(retry.call_args.args[0], 500)
            render.cancel.assert_not_called()
            launch.assert_not_called()
        self.window._pending_update_installer = None

    def test_update_shutdown_preserves_the_recovery_and_skips_save_prompt(self) -> None:
        self.window._project_dirty = True
        self.window._update_install_authorized = True
        with patch.object(self.window, "_clear_recovery") as clear, \
             patch.object(QMessageBox, "warning", side_effect=AssertionError("Unexpected save prompt")):
            event = QCloseEvent()
            self.window.closeEvent(event)
            self.assertTrue(event.isAccepted())
            clear.assert_not_called()
        self.window._update_install_authorized = False

    def test_help_menu_exposes_localized_manual_update_check(self) -> None:
        original_language = self.window.translator.language
        try:
            self.assertIn(self.window.check_updates_action, self.window.help_menu.actions())
            self.window.translator.set_language(Language.KOREAN)
            self.assertEqual(self.window.check_updates_action.text(), "업데이트 확인")
            self.window.translator.set_language(Language.ENGLISH)
            self.assertEqual(self.window.check_updates_action.text(), "Check for updates")
        finally:
            self.window.translator.set_language(original_language)

    def test_dismissed_release_is_skipped_only_during_automatic_checks(self) -> None:
        release = ReleaseInfo(
            version="9.0.0",
            tag_name="v9.0.0",
            name="Playlist Canvas 9.0.0",
            body="Release notes",
            published_at="2026-08-12T00:00:00Z",
            html_url="https://github.com/tharu8813/Playlist-Canvas/releases/tag/v9.0.0",
        )
        self.window._update_check_manual = False
        with patch("app.ui.main_window.QSettings") as settings_type:
            settings_type.return_value.value.return_value = release.tag_name
            with patch("app.ui.main_window.UpdateAvailableDialog") as dialog:
                self.window._update_release_found(release)
            dialog.assert_not_called()

    def test_newer_installed_version_shows_warning_without_update(self) -> None:
        release = ReleaseInfo(
            version="1.0.0",
            tag_name="v1.0.0",
            name="Playlist Canvas 1.0.0",
            body="Older public release",
            published_at="2026-08-12T00:00:00Z",
            html_url="https://github.com/tharu8813/Playlist-Canvas/releases/tag/v1.0.0",
        )
        self.window._update_check_manual = False
        with patch.object(QMessageBox, "warning") as warning, patch(
            "app.ui.main_window.UpdateAvailableDialog"
        ) as dialog:
            self.window._update_release_found(release)
        warning.assert_called_once()
        self.assertIn(__version__, warning.call_args.args[2])
        dialog.assert_not_called()

    def test_manual_check_on_latest_shows_current_version_release_notes(self) -> None:
        release = ReleaseInfo(
            version=__version__,
            tag_name=__version__,
            name=f"Playlist Canvas {__version__}",
            body="## 이번 버전\n\n- 변경 사항 A\n",
            published_at="2026-09-04T00:00:00Z",
            html_url=(
                "https://github.com/tharu8813/Playlist-Canvas/releases/tag/"
                + __version__
            ),
        )
        self.window._update_check_manual = True
        with patch(
            "app.ui.main_window.UpdateAvailableDialog"
        ) as dialog:
            self.window._update_release_found(release)
        dialog.assert_called_once()
        self.assertTrue(dialog.call_args.kwargs.get("up_to_date"))

    def test_up_to_date_release_dialog_shows_rendered_notes_without_update_button(
        self,
    ) -> None:
        from app.dialogs.update_dialogs import UpdateAvailableDialog

        release = ReleaseInfo(
            version=__version__, tag_name=__version__,
            name=f"Playlist Canvas {__version__}",
            body="> [!NOTE]\n> 임시 공간이 필요합니다.\n\n## 변경\n\n- 항목 하나\n",
            published_at="2026-09-04T00:00:00Z",
            html_url=(
                "https://github.com/tharu8813/Playlist-Canvas/releases/tag/"
                + __version__
            ),
        )
        dialog = UpdateAvailableDialog(
            release, __version__, True, False, self.window, up_to_date=True,
        )
        try:
            self.assertNotIn(
                dialog.update_button,
                dialog.buttons.buttons(),
            )
            html = dialog.notes.toHtml()
            self.assertNotIn("[!NOTE]", html)
            self.assertIn("항목 하나", html)
        finally:
            dialog.close()

    def test_stale_recovery_is_removed_instead_of_replacing_newer_project(self) -> None:
        with TemporaryDirectory(prefix="pvs-stale-recovery-") as raw_directory:
            project_path = Path(raw_directory) / "newer.pvsproj"
            project_path.write_bytes(b"newer saved project")
            snapshot = RecoverySnapshot(
                path=Path(raw_directory) / "stale.recovery.json",
                document=self.window._project_document(),
                project_path=project_path,
                saved_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            with patch.object(
                self.window.autosave, "recoveries", return_value=[snapshot]
            ), patch.object(
                self.window.autosave, "clear_snapshot"
            ) as clear_snapshot, patch.object(
                QMessageBox, "question"
            ) as recovery_question:
                self.assertFalse(self.window._offer_recovery())
            clear_snapshot.assert_called_once_with(snapshot)
            recovery_question.assert_not_called()

    def test_autosave_writes_recovery_on_a_background_worker(self) -> None:
        from app.services.autosave_service import AutosaveService

        original = self.window.autosave
        with TemporaryDirectory(prefix="pvs-autosave-worker-") as directory:
            self.window.autosave = AutosaveService(Path(directory))
            try:
                self.window.store.add(Source(SourceType.TEXT, "AUTOSAVE_MARKER"))
                self.window._history_ready = True
                self.window._project_dirty = True
                self.window._autosave_project()
                worker = self.window._autosave_worker
                self.assertIsNotNone(worker)
                self.assertIn("autosave", self.window.activity_progress.active_keys)
                self.assertTrue(worker.wait(5000))
                self.application.processEvents()
                self.assertIsNone(self.window._autosave_worker)
                self.assertNotIn(
                    "autosave", self.window.activity_progress.active_keys
                )
                recovery = self.window.autosave.latest_recovery()
                self.assertIsNotNone(recovery)
                self.assertIn(
                    "AUTOSAVE_MARKER",
                    [source.name for source in recovery.document.sources],
                )
            finally:
                if self.window._autosave_worker is not None:
                    self.window._autosave_worker.wait(5000)
                self.window.autosave = original

    def test_close_cancel_keeps_unsaved_window_and_workspace_open(self) -> None:
        marker = Source(SourceType.TEXT, "CLOSE_CANCEL_MARKER")
        self.window.store.add(marker)
        self.window._project_dirty = True
        event = QCloseEvent()
        with patch.object(
            QMessageBox, "warning", return_value=QMessageBox.StandardButton.Cancel,
        ):
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window._project_dirty)
        self.assertIsNotNone(self.window.store.get(marker.id))
        self.assertTrue(self.window.smooth_scroll._installed)

    def test_close_waits_for_a_running_update_check_then_closes(self) -> None:
        # Destroying a still-running QThread at exit aborts the process.
        worker = MagicMock()
        worker.isRunning.return_value = True
        self.window._update_check_worker = worker
        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window.smooth_scroll._installed)

        with patch("app.ui.main_window.normalized_version") as compare_versions:
            self.window._update_release_found(MagicMock(version="999.0.0"))
        compare_versions.assert_not_called()  # returned before any release UI
        with patch.object(QTimer, "singleShot") as single_shot:
            self.window._update_check_finished()
        single_shot.assert_called_once_with(0, self.window.close)
        worker.deleteLater.assert_called_once()
        self.assertFalse(self.window._close_after_update_check)
        self.assertIsNone(self.window._update_check_worker)

    def test_project_save_runs_in_background_and_preserves_newer_edits(self) -> None:
        with TemporaryDirectory(prefix="pvs-background-save-") as raw_directory:
            target = Path(raw_directory) / "many-tracks.pvsproj"
            self.window.current_project_path = target
            self.window._project_dirty = True
            started = threading.Event()
            release = threading.Event()

            def delayed_save(path, _document, _thumbnail):
                started.set()
                release.wait(3)
                return Path(path).resolve()

            with patch.object(ProjectService, "save", side_effect=delayed_save):
                self.assertTrue(self.window._save_project())
                self.assertTrue(started.wait(1))
                worker = self.window.project_controller.save_worker
                self.assertIsNotNone(worker)
                self.assertTrue(worker.isRunning())
                self.assertFalse(self.window.save_action.isEnabled())
                self.assertIn("project_save", self.window.activity_progress.active_keys)

                event_loop_responsive: list[bool] = []
                QTimer.singleShot(0, lambda: event_loop_responsive.append(True))
                self.application.processEvents()
                self.assertEqual(event_loop_responsive, [True])

                # This edit was not part of the frozen save snapshot and must not
                # be incorrectly marked as saved when the worker completes.
                self.window._schedule_history()
                release.set()
                self.window._wait_for_project_save(worker)

            self.assertIsNone(self.window.project_controller.save_worker)
            self.assertNotIn("project_save", self.window.activity_progress.active_keys)
            self.assertTrue(self.window._project_dirty)
            self.assertTrue(self.window.save_action.isEnabled())
            save_message = self.window.statusBar().currentMessage()
            self.assertTrue(
                "저장되지" in save_message or "unsaved" in save_message.lower()
            )

    def test_required_background_save_can_wait_without_losing_success_state(self) -> None:
        with TemporaryDirectory(prefix="pvs-required-save-") as raw_directory:
            target = Path(raw_directory) / "close-save.pvsproj"
            self.window.current_project_path = target
            self.window._project_dirty = True
            with patch.object(
                ProjectService, "save", return_value=target.resolve(),
            ):
                saved = self.window._save_project(wait_for_completion=True)

            self.assertTrue(saved)
            self.assertFalse(self.window._project_dirty)
            self.assertIsNone(self.window.project_controller.save_worker)
            self.assertEqual(self.window.current_project_path, target.resolve())

    def test_background_save_failure_keeps_project_dirty_and_reenables_save(self) -> None:
        with TemporaryDirectory(prefix="pvs-failed-save-") as raw_directory:
            self.window.current_project_path = Path(raw_directory) / "failed.pvsproj"
            self.window._project_dirty = True
            with (
                patch.object(
                    ProjectService, "save",
                    side_effect=ProjectError("simulated save failure"),
                ),
                patch.object(self.window, "_show_project_error") as show_error,
            ):
                saved = self.window._save_project(wait_for_completion=True)

            self.assertFalse(saved)
            self.assertTrue(self.window._project_dirty)
            self.assertIsNone(self.window.project_controller.save_worker)
            self.assertTrue(self.window.save_action.isEnabled())
            self.assertTrue(self.window._autosave_debounce_timer.isActive())
            show_error.assert_called_once()

    def test_design_presets_remain_available_without_ai_builder(self) -> None:
        preset_dialog = DesignPresetDialog(self.window.translator, self.window)
        try:
            self.assertTrue(hasattr(preset_dialog, "selected_preset"))
            self.assertFalse(hasattr(preset_dialog, "prompt_preview"))
            self.assertIn(self.window.presets_action, self.window.project_menu.actions())
            for language in (Language.KOREAN, Language.ENGLISH):
                self.window.translator.set_language(language)
                labels = {action.text() for action in self.window.project_menu.actions() + self.window.toolbar.actions()}
                self.assertFalse(labels & {"AI 프로젝트 빌더", "AI Project Builder"})
        finally:
            preset_dialog.close()

    def test_file_recent_projects_menu_opens_the_selected_entry(self) -> None:
        with TemporaryDirectory(prefix="recent-project-menu-") as directory:
            project_path = Path(directory) / "Recent playlist.pvsproj"
            project_path.write_bytes(b"recent project placeholder")
            with patch.object(
                self.window.recent_projects, "projects",
                return_value=[project_path.resolve()],
            ):
                self.window._rebuild_recent_projects_menu()

            actions = [
                action for action in self.window.recent_projects_menu.actions()
                if action.isEnabled() and not action.isSeparator()
            ]
            self.assertGreaterEqual(len(actions), 2)
            self.assertIn(project_path.name, actions[0].text())
            self.assertEqual(actions[0].toolTip(), str(project_path.resolve()))
            with patch.object(self.window, "_open_recent_project") as open_recent:
                actions[0].trigger()
            open_recent.assert_called_once_with(project_path.resolve())

    def test_recent_project_open_uses_unsaved_change_guard(self) -> None:
        with TemporaryDirectory(prefix="recent-project-open-") as directory:
            project_path = Path(directory) / "Guarded.pvsproj"
            project_path.write_bytes(b"project placeholder")
            with (
                patch.object(
                    self.window, "_confirm_unsaved_changes", return_value=False,
                ) as confirm,
                patch.object(self.window, "_load_project_path") as load,
            ):
                self.assertFalse(self.window._open_recent_project(project_path))
            confirm.assert_called_once()
            load.assert_not_called()

            with (
                patch.object(
                    self.window, "_confirm_unsaved_changes", return_value=True,
                ),
                patch.object(
                    self.window, "_load_project_path", return_value=True,
                ) as load,
            ):
                self.assertTrue(self.window._open_recent_project(project_path))
            load.assert_called_once_with(project_path)

    def test_clear_recent_projects_requires_confirmation(self) -> None:
        with (
            patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.StandardButton.No,
            ) as question,
            patch.object(self.window.recent_projects, "clear") as clear,
        ):
            self.assertFalse(self.window._confirm_clear_recent_projects())
        question.assert_called_once()
        clear.assert_not_called()

        with (
            patch.object(
                QMessageBox, "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(self.window.recent_projects, "clear") as clear,
        ):
            self.assertTrue(self.window._confirm_clear_recent_projects())
        clear.assert_called_once()

    def test_lrc_generator_registers_saved_files_as_project_content(self) -> None:
        saved = Path("generated-test-lyrics.lrc").resolve()
        with (
            patch("app.ui.main_window.LrcGeneratorDialog") as dialog_type,
            patch.object(
                self.window.project_content_service, "add_paths", return_value=1,
            ) as add_paths,
        ):
            dialog = dialog_type.return_value
            dialog.saved_paths = [saved]
            dialog.add_saved_files_to_project = True
            self.window._show_lrc_generator()
        dialog.exec.assert_called_once_with()
        add_paths.assert_called_once_with([saved])
        self.assertEqual(
            dialog_type.call_args.kwargs["playlist_tracks"],
            self.window.playlist_service.tracks,
        )

    def test_lrc_generator_finish_applies_result_to_selected_track(self) -> None:
        track = PlaylistTrack("selected.wav", "Selected track")
        self.window.playlist_service.replace([track])
        item = self.window.playlist_editor.list_widget.item(0)
        item.setSelected(True)
        cues = [{"start": 1.0, "end": 9.0, "text": "Applied"}]

        with patch("app.ui.main_window.LrcGeneratorDialog") as dialog_type:
            dialog = dialog_type.return_value
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.result_track_id = track.id
            dialog.timed_cues.return_value = cues
            dialog.saved_paths = []
            dialog.add_saved_files_to_project = False
            self.window._show_lrc_generator()

        self.assertEqual(track.lyrics, cues)
        self.assertEqual(track.lyrics_path, "")
        kwargs = dialog_type.call_args.kwargs
        self.assertEqual(kwargs["target_track_id"], track.id)
        self.assertEqual(kwargs["initial_audio_path"], track.file_path)

    def test_lrc_generator_finish_without_file_or_track_stays_open(self) -> None:
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            dialog.audio_path = "unmatched.wav"
            dialog.lines = ["Unsaved"]
            dialog.timestamps = [1.0]
            with patch.object(QDialog, "accept") as accept:
                dialog.accept()
            accept.assert_not_called()
            self.assertTrue(dialog.status_label.property("error"))
        finally:
            dialog.done(QDialog.DialogCode.Rejected)

    def test_lrc_generator_finish_can_clear_selected_track_lyrics(self) -> None:
        track = PlaylistTrack(
            "selected.wav",
            "Selected track",
            lyrics_path="selected.lrc",
            lyrics=[{"start": 1.0, "end": 2.0, "text": "Remove me"}],
        )
        self.window.playlist_service.replace([track])

        with patch("app.ui.main_window.LrcGeneratorDialog") as dialog_type:
            dialog = dialog_type.return_value
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.result_track_id = track.id
            dialog.timed_cues.return_value = []
            dialog.saved_paths = []
            dialog.add_saved_files_to_project = False
            self.window._show_lrc_generator()

        self.assertEqual(track.lyrics, [])
        self.assertEqual(track.lyrics_path, "")

    def test_missing_audio_metadata_can_be_edited_before_project_import(self) -> None:
        track = PlaylistTrack(
            str(Path("untagged-song.mp3").resolve()),
            "untagged-song",
            artist="Unknown Artist",
            album="Unknown Album",
            duration_seconds=90.0,
        )
        candidate = AudioImportCandidate(track, ("title", "artist", "album"))
        dialog = AudioMetadataDialog([candidate], self.window.translator, self.window)
        try:
            self.assertEqual(dialog.table.item(0, 1).text(), "untagged-song")
            self.assertEqual(dialog.table.item(0, 2).text(), "")
            self.assertEqual(dialog.table.item(0, 3).text(), "")
            dialog.table.item(0, 1).setText("Project title")
            dialog.table.item(0, 2).setText("Project artist")
            dialog.table.item(0, 3).setText("Project album")
            edited = dialog.selected_tracks[0]
            self.assertEqual(edited.title, "Project title")
            self.assertEqual(edited.artist, "Project artist")
            self.assertEqual(edited.album, "Project album")
            self.assertEqual(track.title, "untagged-song")
        finally:
            dialog.close()

    def test_app_theme_and_language_do_not_dirty_or_follow_project_metadata(self) -> None:
        original_language = self.window.translator.language
        original_theme = self.window.theme_service.preference
        self.window._project_dirty = False

        # Saving Settings with the already-selected theme republishes styling,
        # but it is not a project edit.
        self.window.theme_service.set_preference(original_theme)
        self.application.processEvents()
        self.assertFalse(self.window._project_dirty)

        document = self.window._project_document()
        document.language = "en" if original_language.value == "ko" else "ko"
        document.theme = "light" if original_theme.value != "light" else "dark"
        self.window._history_restoring = True
        try:
            self.window._apply_project(document)
        finally:
            self.window._history_restoring = False

        self.assertIs(self.window.translator.language, original_language)
        self.assertIs(self.window.theme_service.preference, original_theme)
        round_trip = self.window._project_document()
        self.assertEqual(round_trip.language, document.language)
        self.assertEqual(round_trip.theme, document.theme)

    def test_lyrics_use_the_shared_text_color_control_without_style_presets(self) -> None:
        self.window.translator.set_language(Language.KOREAN)
        source = Source(
            SourceType.LYRICS, "Editable lyrics color",
            outline_color="#A1B2C3", outline_width=0.0,
        )
        self.window.store.replace([source])
        self.window.store.select(source.id)
        self.window.show()
        self.application.processEvents()

        inspector = self.window.inspector
        self.assertNotIn("subtitle_style", inspector._field_widgets)
        self.assertTrue(inspector._field_visibility["text_color"])
        self.assertFalse(inspector._field_visibility["outline_color"])
        self.assertEqual(inspector._form_labels["text_color"].text(), "텍스트 색상")
        self.assertEqual(inspector.text_color_button.text(), "#A1B2C3")
        self.assertEqual(inspector._field_categories["font_weight"], "text")
        inspector.font_weight_combo.setCurrentIndex(
            inspector.font_weight_combo.findData(700)
        )
        self.assertEqual(source.font_weight, 700)
        special = inspector._tab_indices["special"]
        self.assertEqual(inspector.property_tabs.tabText(special), "자막/가사")
        self.assertTrue(inspector.property_tabs.isTabVisible(special))

    def test_canvas_drag_defers_model_notifications_until_release(self) -> None:
        source = self.window.store.sources()[0]
        item = self.window.canvas._items[source.id]
        scene = self.window.canvas.scene_model
        scene.snap_enabled = False
        item.setSelected(True)
        notifications: list[tuple[float, float]] = []
        self.window.store.source_changed.connect(
            lambda changed: notifications.append((changed.x, changed.y))
        )

        scene.begin_item_interaction(item, include_selection=True)
        for offset in range(1, 31):
            item.setPos(source.x + offset, source.y + offset * 2)
        self.assertEqual(notifications, [])
        self.assertEqual(source.x, item.pos().x())
        self.assertEqual(source.y, item.pos().y())

        scene.finish_item_interaction()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0], (item.pos().x(), item.pos().y()))
        self.assertEqual(
            self.window.canvas.viewportUpdateMode(),
            QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate,
        )

    def test_canvas_model_update_does_not_echo_a_second_store_signal(self) -> None:
        source = self.window.store.sources()[0]
        notifications: list[float] = []
        self.window.store.source_changed.connect(
            lambda changed: notifications.append(changed.x)
        )
        self.window.store.update(source.id, x=source.x + 50)
        self.assertEqual(len(notifications), 1)

    def test_save_reapply_and_delete_user_preset(self) -> None:
        from app.presets.user_preset_service import UserPresetService, all_presets

        self.window._add_source(SourceType.TEXT)
        self.window._add_source(SourceType.SHAPE)
        original = sorted(s.source_type.value for s in self.window.store.sources())

        with patch(
            "app.ui.main_window.QInputDialog.getText",
            return_value=("My Layout", True),
        ):
            self.window._save_current_as_preset()

        saved = UserPresetService.all()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].name("en"), "My Layout")
        self.assertTrue(saved[0].editable)
        self.assertIn(
            saved[0].identifier, {p.identifier for p in all_presets()}
        )

        self.window.store.replace([])
        self.window._apply_preset(saved[0])
        self.assertEqual(
            sorted(s.source_type.value for s in self.window.store.sources()),
            original,
        )
        # Applied sources get fresh ids so repeated applies never collide.
        self.window._apply_preset(saved[0])
        ids = [s.id for s in self.window.store.sources()]
        self.assertEqual(len(ids), len(set(ids)))

        self.assertTrue(UserPresetService.delete(saved[0].identifier))
        self.assertEqual(UserPresetService.all(), [])

    def test_new_project_requires_explicit_discard(self) -> None:
        marker = Source(SourceType.TEXT, "UNSAVED_TEST_MARKER")
        self.window.store.add(marker)
        self.window._project_dirty = True
        with (
            patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.Discard,
            ),
            patch.object(
                NewProjectDialog, "exec", return_value=QDialog.DialogCode.Accepted,
            ),
        ):
            self.window._new_project()
        self.assertIsNone(self.window.store.get(marker.id))

    def test_new_project_applies_creation_only_aspect_ratio(self) -> None:
        with patch.object(
            NewProjectDialog, "exec", return_value=QDialog.DialogCode.Accepted,
        ), patch.object(
            NewProjectDialog, "canvas_size",
            new_callable=lambda: property(lambda _dialog: (720, 1280)),
        ):
            created = self.window._new_project()

        self.assertTrue(created)
        artboard = self.window.canvas.scene_model.artboard_rect
        self.assertEqual((artboard.width(), artboard.height()), (720, 1280))
        self.assertEqual(
            (self.window._project_document().canvas.width,
             self.window._project_document().canvas.height),
            (720, 1280),
        )
        background = next(
            source for source in self.window.store.sources()
            if source.source_type is SourceType.BACKGROUND
        )
        self.assertEqual((background.width, background.height), (720, 1280))
        for source in self.window.store.sources():
            self.assertLessEqual(source.x + source.width, 720)
            self.assertLessEqual(source.y + source.height, 1280)

    def test_preset_change_warns_before_replacing_canvas(self) -> None:
        original_ids = [source.id for source in self.window.store.sources()]
        preset = PresetService.all()[0]
        with (
            patch.object(
                DesignPresetDialog, "exec",
                return_value=QDialog.DialogCode.Accepted,
            ),
            patch.object(
                DesignPresetDialog, "selected_preset",
                new_callable=lambda: property(lambda _dialog: preset),
            ),
            patch.object(
                QMessageBox, "warning",
                return_value=QMessageBox.StandardButton.Cancel,
            ) as warning,
        ):
            self.window._choose_preset()
        warning.assert_called_once()
        self.assertEqual(
            [source.id for source in self.window.store.sources()], original_ids,
        )

        with (
            patch.object(
                DesignPresetDialog, "exec",
                return_value=QDialog.DialogCode.Accepted,
            ),
            patch.object(
                DesignPresetDialog, "selected_preset",
                new_callable=lambda: property(lambda _dialog: preset),
            ),
            patch.object(
                QMessageBox, "warning",
                return_value=QMessageBox.StandardButton.Yes,
            ),
        ):
            self.window._choose_preset()
        self.assertNotEqual(
            [source.id for source in self.window.store.sources()], original_ids,
        )

    def test_preset_preserves_custom_portrait_ratio_and_adapts_layout(self) -> None:
        self.window.canvas.scene_model.set_artboard_size(800, 1900)
        preset = PresetService.all()[0]
        self.window._apply_preset(preset)

        artboard = self.window.canvas.scene_model.artboard_rect
        self.assertEqual((artboard.width(), artboard.height()), (800, 1900))
        sources = self.window.store.sources()
        background = next(
            source for source in sources
            if source.source_type is SourceType.BACKGROUND
        )
        self.assertEqual(
            (background.x, background.y, background.width, background.height),
            (0.0, 0.0, 800, 1900),
        )
        non_background = [
            source for source in sources
            if source.source_type is not SourceType.BACKGROUND
        ]
        self.assertGreater(
            max(source.y for source in non_background)
            - min(source.y for source in non_background),
            900,
        )
        for source in non_background:
            self.assertGreaterEqual(source.x, 0)
            self.assertGreaterEqual(source.y, 0)
            self.assertLessEqual(source.x + source.width, 800.01)
            self.assertLessEqual(source.y + source.height, 1900.01)

    def test_project_settings_can_change_ratio_and_scale_existing_sources(self) -> None:
        self.window.canvas.scene_model.set_artboard_size(800, 1900)
        self.window.store.replace([
            Source(SourceType.BACKGROUND, "Background", width=800, height=1900),
            Source(SourceType.TEXT, "Marker", x=80, y=190, width=240, height=100),
        ])
        marker = next(
            source for source in self.window.store.sources() if source.name == "Marker"
        )
        self.window.store.select(marker.id)

        self.window._resize_project_canvas((800, 1900), (1900, 800), True)

        artboard = self.window.canvas.scene_model.artboard_rect
        self.assertEqual((artboard.width(), artboard.height()), (1900, 800))
        background = next(
            source for source in self.window.store.sources()
            if source.source_type is SourceType.BACKGROUND
        )
        self.assertEqual((background.width, background.height), (1900, 800))
        resized_marker = self.window.store.get(marker.id)
        self.assertIsNotNone(resized_marker)
        assert resized_marker is not None
        self.assertAlmostEqual(resized_marker.width, 240 * (800 / 1900))
        self.assertAlmostEqual(
            resized_marker.x + resized_marker.width / 2,
            (80 + 120) * (1900 / 800),
        )
        self.assertEqual(self.window.store.selected_ids, (marker.id,))

    def test_project_settings_dialog_supports_custom_canvas_ratio(self) -> None:
        dialog = ProjectSettingsDialog(
            self.window.project_settings,
            self.window.translator,
            QPixmap(),
            canvas_size=(800, 1900),
        )
        try:
            self.assertEqual(dialog.selected_canvas_size, (800, 1900))
            dialog.canvas_preset_combo.setCurrentIndex(
                dialog.canvas_preset_combo.count() - 1
            )
            dialog.canvas_width_spin.setValue(840)
            dialog.canvas_height_spin.setValue(1995)
            self.assertEqual(dialog.selected_canvas_size, (840, 1995))
            self.assertIn("8:19", dialog.canvas_summary.text())
            self.assertTrue(dialog.scale_canvas_content)
        finally:
            dialog.close()

    def test_transition_mode_is_a_per_project_setting(self) -> None:
        self.assertEqual(self.window.project_settings.transition_mode, "none")
        dialog = ProjectSettingsDialog(
            self.window.project_settings, self.window.translator, QPixmap(),
        )
        try:
            self.assertTrue(dialog.transition_none_radio.isChecked())
            self.assertFalse(dialog.crossfade_seconds_spin.isEnabled())
            dialog.transition_crossfade_radio.setChecked(True)
            self.assertTrue(dialog.crossfade_seconds_spin.isEnabled())
            dialog.crossfade_seconds_spin.setValue(5.0)
            dialog._accept()
            self.assertEqual(dialog.selected_settings.transition_mode, "crossfade")
            self.assertEqual(dialog.selected_settings.crossfade_seconds, 5.0)

            dialog2 = ProjectSettingsDialog(
                dialog.selected_settings, self.window.translator, QPixmap(),
            )
            try:
                self.assertFalse(hasattr(dialog2, "automix_preset_combo"))  # the style is always automatic
                dialog2.transition_automix_radio.setChecked(True)
                dialog2._accept()
                self.assertEqual(dialog2.selected_settings.transition_mode, "automix")
            finally:
                dialog2.close()
        finally:
            dialog.close()

    def test_new_project_can_start_from_scaled_design_preset(self) -> None:
        preset = PresetService.all()[0]
        with (
            patch.object(
                NewProjectDialog, "exec",
                return_value=QDialog.DialogCode.Accepted,
            ),
            patch.object(
                NewProjectDialog, "canvas_size",
                new_callable=lambda: property(lambda _dialog: (720, 1280)),
            ),
            patch.object(
                NewProjectDialog, "selected_design_preset",
                new_callable=lambda: property(lambda _dialog: preset),
            ),
        ):
            self.assertTrue(self.window._new_project())

        sources = self.window.store.sources()
        self.assertEqual(len(sources), len(preset.builder()))
        background = next(
            source for source in sources
            if source.source_type is SourceType.BACKGROUND
        )
        self.assertEqual(
            (background.x, background.y, background.width, background.height),
            (0.0, 0.0, 720, 1280),
        )
        for source in sources:
            self.assertGreaterEqual(source.x, 0.0)
            self.assertGreaterEqual(source.y, 0.0)
            self.assertLessEqual(source.x + source.width, 720.001)
            self.assertLessEqual(source.y + source.height, 1280.001)

    def test_new_project_dialog_supports_presets_and_custom_size(self) -> None:
        dialog = NewProjectDialog(self.window.translator, self.window)
        try:
            self.assertEqual(dialog.canvas_size, (1280, 720))
            self.assertEqual(dialog.preset_combo.currentText(), "16:9")
            self.assertNotIn("1280", dialog.preset_combo.currentText())
            self.assertTrue(dialog.width_spin.isHidden())
            dialog.preset_combo.setCurrentIndex(1)
            self.assertEqual(dialog.canvas_size, (720, 1280))
            self.assertFalse(dialog.width_spin.isEnabled())
            dialog.preset_combo.setCurrentIndex(dialog.preset_combo.count() - 1)
            self.assertFalse(dialog.width_spin.isHidden())
            dialog.width_spin.setValue(1000)
            dialog.height_spin.setValue(1250)
            self.assertTrue(dialog.width_spin.isEnabled())
            self.assertEqual(dialog.canvas_size, (1000, 1250))
            self.assertIn("4:5", dialog.summary.text())
            self.assertEqual(dialog.selected_design_preset, None)
            dialog.design_preset_combo.setCurrentIndex(1)
            self.assertEqual(
                dialog.selected_design_preset.identifier,
                PresetService.all()[0].identifier,
            )
            self.assertTrue(dialog.design_description.text())
        finally:
            dialog.close()

    def test_project_settings_dialog_scrolls_its_settings_and_keeps_the_buttons_visible(self) -> None:
        dialog = ProjectSettingsDialog(
            ProjectSettings(), self.window.translator, QPixmap(16, 9), self.window, canvas_size=(720, 1280),
        )
        try:
            self.assertLessEqual(dialog.height(), 640)
            content = dialog.scroll_area.widget()
            self.assertEqual(dialog.tabs.count(), 4)
            for group in (dialog.identity_group, dialog.thumbnail_group):
                self.assertTrue(content.isAncestorOf(group))
            self.assertTrue(dialog.tabs.widget(2).widget().isAncestorOf(dialog.transition_group))
            self.assertFalse(content.isAncestorOf(dialog.buttons))  # Save/Cancel never scroll away
            # A preset-sized canvas is shown as that preset, not as "Custom".
            self.assertEqual(dialog.canvas_preset_combo.currentText(), "9:16")
        finally:
            dialog.close()

    def test_new_project_dialog_collects_identity_playback_storage_and_music(self) -> None:
        dialog = NewProjectDialog(
            self.window.translator, self.window,
            preview_sources=self.window._preset_sources_for_canvas,
        )
        try:
            # Defaults match a fresh ProjectSettings.
            defaults = dialog.project_settings
            self.assertEqual(
                (defaults.title, defaults.transition_mode, defaults.content_mode),
                ("Untitled Project", "none", "embed"),
            )
            self.assertTrue(dialog.crossfade_spin.isHidden())
            dialog.title_edit.setText("  Night drive  ")
            dialog.author_edit.setText("DJ")
            dialog.transition_combo.setCurrentIndex(dialog.transition_combo.findData("crossfade"))
            self.assertFalse(dialog.crossfade_spin.isHidden())
            dialog.crossfade_spin.setValue(6.5)
            dialog.storage_combo.setCurrentIndex(dialog.storage_combo.findData("reference"))
            self.assertIn("다시 연결", dialog.storage_help.text())
            settings = dialog.project_settings
            self.assertEqual(
                (settings.title, settings.author, settings.transition_mode,
                 settings.crossfade_seconds, settings.content_mode),
                ("Night drive", "DJ", "crossfade", 6.5, "reference"),
            )

            # The preview letterboxes the chosen design on the chosen canvas.
            self.assertFalse(dialog.preview_label.pixmap().isNull())
            dialog.design_preset_combo.setCurrentIndex(1)
            dialog.preset_combo.setCurrentIndex(1)  # 9:16
            self.assertIn(
                (dialog.selected_design_preset.identifier, 720, 1280), dialog._preview_cache,
            )

            with patch.object(
                QFileDialog, "getOpenFileNames",
                return_value=(["C:/music/a.mp3", "C:/music/mix.m3u8", "C:/music/a.mp3", "C:/music/cover.jpg"], ""),
            ):
                dialog._choose_music()
            self.assertEqual(dialog.music_paths, [Path("C:/music/a.mp3"), Path("C:/music/mix.m3u8")])
            self.assertEqual(dialog.music_count.text(), "파일 2개 (플레이리스트 1개)")
            dialog.music_list.item(0).setSelected(True)
            dialog._remove_music()
            self.assertEqual(dialog.music_paths, [Path("C:/music/mix.m3u8")])
            self.assertEqual(dialog.music_list.count(), 1)
        finally:
            dialog.close()

    def test_new_project_applies_the_chosen_settings_and_adds_the_starting_music(self) -> None:
        chosen = ProjectSettings(title="Night drive", transition_mode="automix", content_mode="reference")
        songs = [Path("C:/music/a.mp3"), Path("C:/music/mix.m3u8")]

        def choose_and_accept(dialog: NewProjectDialog) -> int:
            dialog.music_paths = list(songs)
            return QDialog.DialogCode.Accepted

        with (
            patch.object(NewProjectDialog, "exec", choose_and_accept),
            patch.object(NewProjectDialog, "project_settings",
                         new_callable=lambda: property(lambda _dialog: chosen)),
            patch.object(self.window, "_add_music_paths") as add_music,
        ):
            self.assertTrue(self.window._new_project(confirm_unsaved=False))
        self.assertIs(self.window.project_settings, chosen)
        self.assertEqual(self.window._project_document().settings.title, "Night drive")
        add_music.assert_called_once_with(songs)

    def test_track_details_dialog_saves_edited_project_metadata_only_on_accept(self) -> None:
        track = PlaylistTrack(
            "track.wav", "Original title", artist="Original artist",
            album="Original album", duration_seconds=20.0,
        )
        dialog = TrackDetailsDialog(track, self.window.translator, self.window)
        try:
            dialog.title_edit.setText("  Edited title  ")
            dialog.artist_edit.setText("Edited artist")
            dialog.album_edit.setText("Edited album")

            self.assertEqual(track.title, "Original title")
            dialog._accept()

            self.assertEqual(dialog.selected_title, "Edited title")
            self.assertEqual(dialog.selected_artist, "Edited artist")
            self.assertEqual(dialog.selected_album, "Edited album")
            self.assertEqual(track.title, "Original title")
        finally:
            dialog.close()

    def test_track_settings_updates_playlist_metadata_after_save(self) -> None:
        track = PlaylistTrack(
            "track.wav", "Before", artist="Before artist", album="Before album",
        )
        self.window.playlist_service.replace([track])
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.selected_title = "After"
        dialog.selected_artist = "After artist"
        dialog.selected_album = "After album"
        dialog.selected_cover_path = ""
        dialog.selected_lyrics_path = ""
        dialog.selected_lyrics = []
        dialog.selected_timing_offset = 0.0
        dialog.selected_video_paths = []
        dialog.selected_volume_db = 0.0
        dialog.selected_eq_db = []
        dialog.selected_karaoke_settings = None

        with patch("app.ui.main_window.TrackDetailsDialog", return_value=dialog):
            self.window._show_track_details(track.id)

        updated = self.window.playlist_service.tracks[0]
        self.assertEqual(
            (updated.title, updated.artist, updated.album),
            ("After", "After artist", "After album"),
        )

    def test_automatic_karaoke_shortcut_saves_lyrics_colors_and_enable_together(self):
        from app.services.lyrics_alignment_service import AlignmentResult
        source = Source(SourceType.LYRICS, "Lyrics")
        self.window.store.replace([source])
        self.window.store.select(source.id)
        track = PlaylistTrack("track.wav", "Track", duration_seconds=5,
                              lyrics=[{"start": 0, "end": 5, "text": "hello world"}])
        self.window.playlist_service.replace([track])
        aligned = [{"start": 1, "end": 3, "text": "hello world", "words": [
            {"offset": 0, "text": "hello", "start": 1, "end": 2},
            {"offset": 6, "text": "world", "start": 2, "end": 3}]}]
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Rejected
        dialog.error = ""
        dialog.result_data = AlignmentResult(aligned, 1, 1)
        dialog.karaoke_settings.return_value = {"subtitle_karaoke_enabled": True, "subtitle_accent_color": "#22AAFF"}
        with patch("app.dialogs.lyrics_alignment_dialog.LyricsAlignmentDialog", return_value=dialog):
            self.window._configure_karaoke()
            self.assertFalse(source.subtitle_karaoke_enabled)
            self.assertNotIn("words", self.window.playlist_service.tracks[0].lyrics[0])
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            self.window.inspector.lyrics_karaoke_button.click()
        self.assertEqual(self.window.playlist_service.tracks[0].lyrics, aligned)
        self.assertTrue(source.subtitle_karaoke_enabled)
        self.assertEqual(source.subtitle_accent_color, "#22AAFF")

    def test_lrc_generator_track_edit_mode_skips_audio_and_file_save_controls(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "track-edit.wav"
            audio_path.write_bytes(b"test")
            candidate = MagicMock()
            candidate.track.title = "Track edit"
            candidate.track.artist = "Artist"
            with patch.object(PlaylistService, "inspect_files", return_value=[candidate]):
                dialog = LrcGeneratorDialog(
                    [], self.window.translator, self.window,
                    track_edit_mode=True,
                    initial_audio_path=str(audio_path),
                    initial_cues=[
                        {"start": 1.0, "end": 3.0, "text": "First"},
                        {"start": 4.0, "end": 7.0, "text": "Second"},
                    ],
                )
            try:
                self.assertEqual(dialog.pages.currentIndex(), 1)
                self.assertEqual(dialog.step_label.text(), "1 / 3 단계")
                self.assertTrue(dialog.back_button.isHidden())

                dialog._next_step()
                self.assertEqual(dialog.pages.currentIndex(), 2)
                dialog._next_step()
                self.assertEqual(dialog.pages.currentIndex(), 3)
                self.assertTrue(dialog.save_button.isHidden())
                self.assertTrue(dialog.add_to_project_check.isHidden())
                self.assertFalse(dialog.finish_button.isHidden())
                self.assertEqual(dialog.step_title.text(), "확인 및 적용")
                self.assertIn("현재 곡", dialog.review_help.text())
            finally:
                dialog.done(QDialog.DialogCode.Accepted)
                self.application.processEvents()

    def test_lrc_generator_autosaves_and_recovers_lyrics_timing_by_audio(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "autosave-song.wav"
            audio_path.write_bytes(b"test")
            candidate = MagicMock()
            candidate.track.title = "Recovered title"
            candidate.track.artist = "Recovered artist"

            first = LrcGeneratorDialog([], self.window.translator, self.window)
            first._draft_service = LrcDraftService(root)
            try:
                with patch.object(PlaylistService, "inspect_files", return_value=[candidate]):
                    first._set_audio(str(audio_path))
                first._mark_draft_dirty()
                first._save_draft_now()
                self.assertIsNone(first._draft_service.load(str(audio_path)))
                first.lyrics_editor.setPlainText("First\nSecond")
                self.assertTrue(first._prepare_lines())
                first.pages.setCurrentIndex(2)
                # Timing-step edits intentionally leave the raw input stale;
                # recovery must rebuild it from the authoritative line list.
                first.lines[0] = "Edited first"
                first.timestamps = [1.25, 4.5]
                first.current_index = 2
                first._autosave_after_change()
                draft = first._draft_service.load(str(audio_path))
                self.assertIsNotNone(draft)
            finally:
                # Simulate a crash: bypass the normal LRC dialog cleanup so
                # the on-disk recovery draft survives for the next launch.
                first._draft_timer.stop()
                first._draft_periodic_timer.stop()
                QDialog.done(first, QDialog.DialogCode.Rejected)
                self.application.processEvents()

            second = LrcGeneratorDialog([], self.window.translator, self.window)
            second._draft_service = LrcDraftService(root)
            try:
                with (
                    patch.object(PlaylistService, "inspect_files", return_value=[candidate]),
                    patch.object(
                        QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes,
                    ) as recovery_question,
                ):
                    second._set_audio(str(audio_path))
                recovery_question.assert_called_once()
                self.assertEqual(second.pages.currentIndex(), 2)
                self.assertEqual(second.lines, ["Edited first", "Second"])
                self.assertEqual(second.timestamps, [1.25, 4.5])
                self.assertEqual(second.current_index, 2)
                self.assertEqual(second.title_edit.text(), "Recovered title")
                recovery_status = second.autosave_label.text()
                self.assertTrue(
                    "복구" in recovery_status or "restored" in recovery_status.lower()
                )
                second._previous_step()
                self.assertEqual(second.pages.currentIndex(), 1)
                self.assertEqual(
                    second.lyrics_editor.toPlainText(), "Edited first\nSecond",
                )
                output = root / "recovered.lrc"
                with patch.object(
                    QFileDialog, "getSaveFileName",
                    return_value=(str(output), "LRC lyrics (*.lrc)"),
                ):
                    second._save_lrc()
                self.assertTrue(output.is_file())
                self.assertIsNone(second._draft_service.load(str(audio_path)))
            finally:
                second.done(QDialog.DialogCode.Rejected)
                self.application.processEvents()

    def test_lrc_generator_normal_close_warns_and_deletes_recovery_draft(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "normal-close.wav"
            audio_path.write_bytes(b"test")
            candidate = MagicMock()
            candidate.track.title = "Normal close"
            candidate.track.artist = "Artist"
            dialog = LrcGeneratorDialog([], self.window.translator, self.window)
            dialog._draft_service = LrcDraftService(root)
            with patch.object(PlaylistService, "inspect_files", return_value=[candidate]):
                dialog._set_audio(str(audio_path))
            dialog.pages.setCurrentIndex(1)
            dialog.lyrics_editor.setPlainText("Unsaved lyric")
            dialog._save_draft_now()
            self.assertIsNotNone(dialog._draft_service.load(str(audio_path)))

            with (
                patch.object(
                    QMessageBox, "warning",
                    return_value=QMessageBox.StandardButton.Cancel,
                ) as warning,
                patch.object(dialog, "done") as done,
            ):
                dialog.reject()
            warning.assert_called_once()
            done.assert_not_called()
            self.assertIsNotNone(dialog._draft_service.load(str(audio_path)))

            with patch.object(
                QMessageBox, "warning",
                return_value=QMessageBox.StandardButton.Yes.value,
            ):
                dialog.reject()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertIsNone(dialog._draft_service.load(str(audio_path)))

    def test_lrc_generator_declining_crash_recovery_deletes_the_draft(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "decline-recovery.wav"
            audio_path.write_bytes(b"test")
            service = LrcDraftService(root)
            service.save(str(audio_path), {
                "lyrics_text": "Recovered lyric",
                "lines": ["Recovered lyric"],
                "timestamps": [None],
                "current_index": 0,
            })
            candidate = MagicMock()
            candidate.track.title = "Recovery"
            candidate.track.artist = "Artist"
            dialog = LrcGeneratorDialog([], self.window.translator, self.window)
            dialog._draft_service = service
            try:
                with (
                    patch.object(PlaylistService, "inspect_files", return_value=[candidate]),
                    patch.object(
                        QMessageBox, "question",
                        return_value=QMessageBox.StandardButton.No,
                    ),
                ):
                    dialog._set_audio(str(audio_path))
                self.assertIsNone(service.load(str(audio_path)))
                deletion_status = dialog.autosave_label.text()
                self.assertTrue(
                    "삭제" in deletion_status or "deleted" in deletion_status.lower()
                )
            finally:
                dialog.done(QDialog.DialogCode.Rejected)

    def test_lrc_generator_does_not_autosave_empty_lyrics_and_clears_stale_draft(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "empty-lyrics.wav"
            audio_path.write_bytes(b"test")
            candidate = MagicMock()
            candidate.track.title = "Empty lyrics"
            candidate.track.artist = "Artist"
            dialog = LrcGeneratorDialog([], self.window.translator, self.window)
            dialog._draft_service = LrcDraftService(root)
            try:
                with patch.object(PlaylistService, "inspect_files", return_value=[candidate]):
                    dialog._set_audio(str(audio_path))
                dialog.pages.setCurrentIndex(1)
                dialog.lyrics_editor.setPlainText("Temporary lyric")
                dialog._save_draft_now()
                self.assertIsNotNone(dialog._draft_service.load(str(audio_path)))

                dialog.lines = ["Stale prepared lyric"]
                dialog.lyrics_editor.clear()
                dialog._save_draft_now()
                self.assertIsNone(dialog._draft_service.load(str(audio_path)))
                empty_status = dialog.autosave_label.text()
                self.assertTrue(
                    "가사 없음" in empty_status or "no lyrics" in empty_status.lower()
                )
            finally:
                dialog.done(QDialog.DialogCode.Rejected)
                self.application.processEvents()

    def test_default_project_title_and_source_kind_read_in_the_ui_language(self) -> None:
        self.window.project_settings = replace(self.window.project_settings, title="Untitled Project")
        self.window.project_controller.update_status()
        self.assertTrue(self.window.project_status_label.text().startswith("새 프로젝트"))
        source = next(iter(self.window.store.sources()))
        self.window.store.select(source.id)
        QApplication.processEvents()
        self.assertEqual(self.window.inspector.subtitle.text(), self.window._source_type_label(source.source_type))
        self.assertNotIn("_", self.window.inspector.subtitle.text())

    def test_project_content_marks_items_already_used_in_the_project(self) -> None:
        panel = self.window.content_library_panel
        with TemporaryDirectory(prefix="pvs-content-added-") as raw_directory:
            song = Path(raw_directory) / "track one.mp3"
            song.write_bytes(b"audio placeholder")
            cover = Path(raw_directory) / "art.png"
            cover.write_bytes(b"image placeholder")
            self.window.project_content_service.add_paths([song, cover])

            def row_for(name: str):
                for index in range(panel.list.count()):
                    item = panel.list.item(index)
                    if name in item.text():
                        return item
                raise AssertionError(f"no content row for {name}")

            self.assertFalse(row_for("track one").data(Qt.ItemDataRole.UserRole + 3))
            self.assertNotIn("추가됨", row_for("art").text())

            self.window.playlist_service.add_tracks(
                [PlaylistTrack(str(song.resolve()), "Track One")]
            )
            panel._used_refresh_timer.stop()
            panel.refresh()
            self.assertTrue(row_for("track one").data(Qt.ItemDataRole.UserRole + 3))
            self.assertIn("추가됨", row_for("track one").text())
            self.assertNotIn("추가됨", row_for("art").text())

            source = Source(
                SourceType.IMAGE, "Art", content_path=str(cover.resolve()),
            )
            self.window.store.add(source)
            panel._used_refresh_timer.stop()
            panel.refresh()
            self.assertIn("추가됨", row_for("art").text())


if __name__ == "__main__":
    unittest.main()
