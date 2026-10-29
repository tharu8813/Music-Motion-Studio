"""Autosave/recovery sequencing and ownership of the recovery writer.

ProjectSession supplies change state. The window supplies document/UI
adapters, while this controller keeps recovery writes alive until joined.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from app.services.autosave_worker import AutosaveWorker
from app.services.project_service import ProjectError, ProjectLoadCancelled, ProjectService
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

    def offer_recovery(self, *, snapshot_path: Path | None = None, confirm: bool = True) -> bool:
        """Offer recovery of the most recently autosaved workspace on startup."""
        window = self.window
        try:
            # Another editor can save the same file without saving this session's edits.
            snapshot = (window.autosave.load_snapshot(snapshot_path) if snapshot_path is not None
                        else window.autosave.latest_recovery())
        except ProjectError as error:
            window.statusBar().showMessage(str(error), 5000)
            return False
        if snapshot is None:
            return False
        korean = window.translator.is_korean
        answer = QMessageBox.question(
            window,
            "자동 저장 복구" if korean else "Autosave recovery",
            "저장되지 않은 작업을 복구할까요?" if korean
            else "Restore your most recently autosaved work?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) if confirm else QMessageBox.StandardButton.Yes
        if answer == QMessageBox.StandardButton.Yes:
            media_reference = snapshot.project_path or snapshot.path
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
                window.current_project_path = snapshot.project_path
                window._legacy_project_path = (
                    snapshot.project_path
                    if snapshot.project_path is not None
                    and snapshot.project_path.suffix.lower() == ".json" else None
                )
                window.upgrade_project_action.setEnabled(window._legacy_project_path is not None)
                window.history.reset(window._project_document().to_dict())
            self.session.dirty = snapshot.dirty or window.project_controller.media_relinked
            window.set_project_open(True)
            # Own the restored backup so saving this window cannot clear another window's work.
            try:
                owned = window.autosave.save(window._project_document(), snapshot.project_path, dirty=self.session.dirty)
                if owned.path != snapshot.path:
                    window.autosave.clear_snapshot(snapshot)
            except ProjectError as error:
                window.statusBar().showMessage(str(error), 5000)
            if self.session.dirty:
                window._autosave_debounce_timer.start()
            window._update_project_status()
            if snapshot.project_path is not None:
                window.recent_projects.add(snapshot.project_path)
            return True
        try:
            window.autosave.clear_snapshot(snapshot)
        except ProjectError as error:
            window.statusBar().showMessage(str(error), 5000)
        return False

    def clear_recovery(self) -> None:
        window = self.window
        try:
            window.autosave.clear(window.current_project_path)
        except ProjectError as error:
            window.statusBar().showMessage(str(error), 5000)
