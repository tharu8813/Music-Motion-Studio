"""Autosave and recovery orchestration, extracted from MainWindow.

MainWindow still owns the live state (dirty flag, debounce timer, active
worker reference) and UI widgets/dialogs; this controller centralizes the
periodic-autosave and startup-recovery sequencing that used to live inline
in MainWindow. Pure move, not a rewrite.

Cross-calls into project load/apply/document logic go through the window's
existing `_xxx` wrapper methods (not straight to ProjectController) so that
tests which monkeypatch a specific window method keep intercepting it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from app.services.autosave_worker import AutosaveWorker
from app.services.project_service import ProjectError

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow


class AutosaveController:
    """Own periodic autosave and startup recovery on behalf of a MainWindow."""

    def __init__(self, window: "MainWindow") -> None:
        self.window = window

    def autosave(self) -> None:
        """Periodically write a recovery document without changing the active project.

        The live models are snapshotted here on the GUI thread; the JSON encode
        and atomic file write run on an AutosaveWorker so a large project does
        not stutter the editor every few seconds.
        """
        window = self.window
        if (not window._history_ready or not window._project_dirty
                or window.project_controller.saving
                or window._autosave_worker is not None):
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
        window._autosave_worker = worker
        worker.start()

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
        window = self.window
        window.activity_progress.finish("autosave")
        if window._autosave_worker is worker:
            window._autosave_worker = None
        worker.deleteLater()

    def offer_recovery(self, *, snapshot_path: Path | None = None, confirm: bool = True) -> bool:
        """Offer recovery of the most recently autosaved workspace on startup."""
        window = self.window
        try:
            snapshot = window.autosave.load_snapshot(snapshot_path) if snapshot_path is not None else None
            for candidate in (() if snapshot_path is not None else window.autosave.recoveries()):
                project_path = candidate.project_path
                if project_path is not None and project_path.is_file():
                    try:
                        recovery_is_newer = (
                            candidate.saved_at.timestamp() > project_path.stat().st_mtime
                        )
                    except OSError:
                        recovery_is_newer = True
                    if not recovery_is_newer:
                        # A normal successful save should already remove this file,
                        # but stale recoveries can remain after antivirus/file-lock
                        # interference. Never offer one over a newer project file.
                        try:
                            window.autosave.clear_snapshot(candidate)
                        except ProjectError as error:
                            # A locked stale file should not hide a different,
                            # genuinely recoverable workspace.
                            window.statusBar().showMessage(str(error), 5000)
                        continue
                snapshot = candidate
                break
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
            if not window._resolve_project_media(snapshot.document, media_reference):
                return False
            window._history_restoring = True
            try:
                window._apply_project(snapshot.document)
                window.current_project_path = snapshot.project_path
                window._legacy_project_path = (
                    snapshot.project_path
                    if snapshot.project_path is not None
                    and snapshot.project_path.suffix.lower() == ".json" else None
                )
                window.upgrade_project_action.setEnabled(window._legacy_project_path is not None)
                window.history.reset(window._project_document().to_dict())
            finally:
                window._history_restoring = False
            window._project_dirty = snapshot.dirty
            if snapshot.dirty:
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
