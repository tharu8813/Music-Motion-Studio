"""Coalesced undo/redo orchestration with an explicit editing-session policy.

The window provides existing services and UI adapters; ProjectSession owns
change tracking, and this controller owns restore reentrancy.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from app.models.project import ProjectDocument
from app.services.project_session import ProjectSession
from app.utils.logging_setup import report_unexpected_error

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

LOGGER = logging.getLogger(__name__)


class HistoryController:
    """Own the undo/redo commit cycle on behalf of a MainWindow."""

    def __init__(self, window: "MainWindow", session: ProjectSession) -> None:
        self.window = window
        self.session = session
        self.applying = False

    def schedule(self) -> None:
        """Coalesce rapid property edits such as dragging into a single undo entry."""
        window = self.window
        if self.session.tracking_changes:
            self.session.mark_changed()
            window._update_project_status()
            window._history_timer.start()
            window._autosave_debounce_timer.start()

    def commit(self) -> None:
        window = self.window
        if self.session.tracking_changes:
            window.history.commit(window._project_document().to_dict())

    def update_actions(self, can_undo: bool, can_redo: bool) -> None:
        window = self.window
        window.undo_action.setEnabled(can_undo)
        window.redo_action.setEnabled(can_redo)

    def undo(self) -> None:
        window = self.window
        if self.applying:
            return
        self.flush_pending()
        selected_source_ids = window.store.selected_ids
        active_source_id = window.store.selected.id if window.store.selected else None
        snapshot = window.history.undo()
        if snapshot is not None:
            self.restore_snapshot(snapshot, selected_source_ids, active_source_id)

    def redo(self) -> None:
        window = self.window
        if self.applying:
            return
        self.flush_pending()
        selected_source_ids = window.store.selected_ids
        active_source_id = window.store.selected.id if window.store.selected else None
        snapshot = window.history.redo()
        if snapshot is not None:
            self.restore_snapshot(snapshot, selected_source_ids, active_source_id)

    def flush_pending(self) -> None:
        """Commit a just-made edit before Undo can navigate past it."""
        window = self.window
        if window._history_timer.isActive():
            window._history_timer.stop()
            self.commit()

    def restore_snapshot(
        self, snapshot: dict, selected_source_ids: object = (),
        active_source_id: str | None = None,
    ) -> None:
        """Restore history and preserve the shared Canvas/Layer selection."""
        window = self.window
        if self.applying:
            return
        self.applying = True
        with self.session.suspend_changes():
            window.setUpdatesEnabled(False)
            try:
                window._apply_project(ProjectDocument.from_dict(snapshot))
                valid_ids = [
                    source_id for source_id in selected_source_ids
                    if window.store.get(source_id) is not None
                ] if isinstance(selected_source_ids, (tuple, list)) else []
                if active_source_id not in valid_ids:
                    active_source_id = valid_ids[-1] if valid_ids else None
                window.store.select_many(valid_ids, active_source_id)
                self.session.mark_changed()
                window._autosave_debounce_timer.start()
                window._update_project_status()
            except Exception as error:
                LOGGER.exception("Undo/redo restore failed")
                report_unexpected_error("Undo/redo restore", error)
                QMessageBox.critical(
                    window,
                    "실행 취소 오류" if window.translator.is_korean else "Undo/redo error",
                    str(error),
                )
            finally:
                window.setUpdatesEnabled(True)
                self.applying = False
                window.canvas.viewport().update()
