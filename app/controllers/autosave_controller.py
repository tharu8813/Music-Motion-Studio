"""Autosave/recovery sequencing and ownership of the recovery writer.

ProjectSession supplies change state. The window supplies document/UI
adapters, while this controller keeps recovery writes alive until joined.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from app.dialogs.recovery_selection_dialog import RecoverySelectionDialog

from app.services.autosave_service import RecoveryProblem
from app.services.autosave_worker import AutosaveWorker
from app.services.project_service import ProjectError, ProjectLoadCancelled, ProjectService
from app.services.project_persistence_service import is_project_path
from app.services.project_session import ProjectSession
from app.utils.qt_worker_lifecycle import stop_qthread_now, wait_for_qthread

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow


class AutosaveController:
    """Own periodic autosave and startup recovery on behalf of a MainWindow."""

    def __init__(self, window: "MainWindow", session: ProjectSession) -> None:
        self.window = window
        self.session = session
        self.worker: AutosaveWorker | None = None
        self._waiting_for_pending = False

    def autosave(self) -> None:
        """Periodically write a recovery document without changing the active project.

        The live models are snapshotted here on the GUI thread; the JSON encode
        and atomic file write run on an AutosaveWorker so a large project does
        not stutter the editor every few seconds.
        """
        window = self.window
        if (not getattr(window, '_project_open', True)
                or not self.session.tracking_changes or not self.session.dirty
                or window.project_controller.saving
                or window.project_controller.loading
                or self.worker is not None):
            return
        korean = window.translator.is_korean
        try:
            document_data = window._project_document().to_dict()
        except (TypeError, ValueError) as error:
            window.statusBar().showMessage(str(error), 5000)
            return
        window.activity_progress.begin(
            "autosave", "자동 저장" if korean else "Autosaving",
            detail=(window.project_settings.title or "Untitled Project"),
        )
        worker = AutosaveWorker(
            window.autosave, document_data, window.current_project_path,
            base_revision=window.project_controller.loaded_revision,
        )
        worker.succeeded.connect(window._autosave_succeeded)
        worker.failed.connect(window._autosave_failed)
        worker.finished.connect(lambda: self.thread_finished(worker))
        self.worker = worker
        worker.start()

    def wait_for_pending(self) -> None:
        """Finish recovery IO before clear/update handoff while keeping paints responsive."""
        worker = self.worker
        if worker is None:
            return
        if self._waiting_for_pending:
            wait_for_qthread(worker)
            return
        self._waiting_for_pending = True
        try:
            stop_qthread_now(worker)
        finally:
            if self.worker is worker:
                self.worker = None
            self._waiting_for_pending = False
            self.window.activity_progress.finish("autosave")

    def succeeded(self, _path: object) -> None:
        window = self.window
        window._update_project_status()
        message = (
            "자동 저장됨" if window.translator.is_korean else "Autosaved"
        )
        window.statusBar().showMessage(message, 2500)

    def failed(self, message: str) -> None:
        self.window.statusBar().showMessage(message, 5000)

    def thread_finished(self, worker: AutosaveWorker) -> None:
        # A queued finished callback can outlive disconnect() during an explicit drain.
        if self._waiting_for_pending or self.worker is not worker:
            return
        window = self.window
        window.activity_progress.finish("autosave")
        self.worker = None
        worker.deleteLater()

    def show_recovery_problems(self, problems: list[RecoveryProblem]) -> None:
        """Explain unreadable backups without asking the user to delete them."""
        window = self.window
        korean = window.translator.is_korean
        dialog = QMessageBox(window)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("자동 복구 파일 확인" if korean else "Recovery file diagnostics")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(
            f"복구 파일 {len(problems)}개를 읽을 수 없습니다. 원본은 삭제하지 않았습니다."
            if korean else
            f"{len(problems)} recovery file(s) could not be read. The originals have not been deleted."
        )
        dialog.setInformativeText(
            f"정상 복구 파일은 계속 사용할 수 있습니다. 복구 파일 폴더:\n{window.autosave.directory}"
            if korean else
            f"Valid recovery files remain available. Recovery folder:\n{window.autosave.directory}"
        )
        # Bound dialog content even if a directory contains many bad files or
        # an exception includes a large malformed field.
        details = "\n\n".join(f"{problem.path}\n{problem.reason[:500]}" for problem in problems[:20])
        if len(problems) > 20:
            details += (f"\n\n그 외 {len(problems) - 20}개" if korean
                        else f"\n\n{len(problems) - 20} more file(s)")
        dialog.setDetailedText(details)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def offer_recovery(self, *, snapshot_path: Path | None = None, confirm: bool = True) -> bool:
        """Offer recovery of the most recently autosaved workspace on startup."""
        window = self.window
        try:
            # Another editor can save the same file without saving this session's edits.
            if snapshot_path is not None:
                snapshot = window.autosave.load_snapshot(snapshot_path)
            else:
                snapshots, problems = window.autosave.scan_recoveries()
                if problems:
                    self.show_recovery_problems(problems)
                if len(snapshots) > 1:
                    dialog = RecoverySelectionDialog(snapshots, window.translator.is_korean, window)
                    try:
                        if dialog.exec() != QDialog.DialogCode.Accepted:
                            return False
                        snapshot = dialog.selected_snapshot
                    finally:
                        dialog.deleteLater()
                    # Choosing a specific entry is already explicit consent.
                    confirm = False
                else:
                    snapshot = snapshots[0] if snapshots else None
        except ProjectError as error:
            window.statusBar().showMessage(str(error), 5000)
            return False
        if snapshot is None:
            return False
        korean = window.translator.is_korean
        answer = QMessageBox.question(
            window,
            "자동 저장 복구" if korean else "Autosave recovery",
            "저장되지 않은 작업을 복구할까요? 아니요를 선택해도 복구 파일은 유지됩니다." if korean
            else "Restore your autosaved work? Choosing No keeps the recovery file.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) if confirm else QMessageBox.StandardButton.Yes
        if answer == QMessageBox.StandardButton.Yes:
            media_reference = snapshot.project_path or snapshot.path
            project_path = snapshot.project_path
            if project_path is not None and not is_project_path(project_path):
                project_path = None
            try:
                ProjectService.restore_recovery_media(
                    snapshot.document, snapshot.project_path,
                    window.project_controller._load_document_off_gui_thread,
                )
            except ProjectLoadCancelled:
                return False
            except ProjectError as error:
                # Keep the snapshot and let the normal missing-media dialog offer relinking.
                window.statusBar().showMessage(str(error), 5000)
            window.project_controller.media_relinked = False
            if not window._resolve_project_media(snapshot.document, media_reference):
                return False
            with self.session.suspend_changes():
                window._apply_project(snapshot.document)
                window.current_project_path = project_path
                # Carry the revision from the original editing session, never
                # adopt the currently saved file as the base of recovered edits.
                window.project_controller.loaded_revision = (
                    snapshot.base_revision if project_path is not None else None
                )
                window.history.reset(window._project_document().to_dict())
            self.session.dirty = (
                snapshot.dirty or window.project_controller.media_relinked
                or project_path != snapshot.project_path
            )
            window.set_project_open(True)
            # Own the restored backup so saving this window cannot clear another window's work.
            try:
                owned = window.autosave.save(
                    window._project_document(), project_path, dirty=self.session.dirty,
                    base_revision=window.project_controller.loaded_revision,
                )
                if owned.path != snapshot.path:
                    window.autosave.clear_snapshot(snapshot)
            except ProjectError as error:
                window.statusBar().showMessage(str(error), 5000)
            if self.session.dirty:
                window._autosave_debounce_timer.start()
            window._update_project_status()
            if project_path is not None:
                window.recent_projects.add(project_path)
            return True
        # Declining the restore is not consent to permanently discard edits.
        return False

    def clear_recovery(self) -> None:
        window = self.window
        try:
            window.autosave.clear(window.current_project_path)
        except ProjectError as error:
            window.statusBar().showMessage(str(error), 5000)
