"""Coalesced undo/redo history orchestration, extracted from MainWindow.

MainWindow still owns the actual state (history stack, dirty flag, debounce
timers) and UI widgets (undo/redo actions, message boxes); this controller
just centralizes the sequencing logic that used to live inline in
MainWindow so it stops growing there. Behavior is unchanged from before the
extraction — this is a pure move, not a rewrite.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from app.models.project import ProjectDocument
from app.utils.logging_setup import report_unexpected_error

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

LOGGER = logging.getLogger(__name__)


class HistoryController:
    """Own the undo/redo commit cycle on behalf of a MainWindow."""

    def __init__(self, window: "MainWindow") -> None:
        self.window = window

    def schedule(self) -> None:
        """Coalesce rapid property edits such as dragging into a single undo entry."""
        window = self.window
        if window._history_ready and not window._history_restoring:
            window._project_change_serial += 1
            window._project_dirty = True
            window._update_project_status()
            window._history_timer.start()
            window._autosave_debounce_timer.start()

    def commit(self) -> None:
        window = self.window
        if window._history_ready and not window._history_restoring:
            window.history.commit(window._project_document().to_dict())

    def update_actions(self, can_undo: bool, can_redo: bool) -> None:
        window = self.window
        window.undo_action.setEnabled(can_undo)
        window.redo_action.setEnabled(can_redo)

    def undo(self) -> None:
        window = self.window
        if window._history_applying:
            return
        self.flush_pending()
        selected_source_ids = window.store.selected_ids
        active_source_id = window.store.selected.id if window.store.selected else None
        snapshot = window.history.undo()
        if snapshot is not None:
            self.restore_snapshot(snapshot, selected_source_ids, active_source_id)

    def redo(self) -> None:
        window = self.window
        if window._history_applying:
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
        if window._history_applying:
            return
        window._history_applying = True
        window._history_restoring = True
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
            window._project_change_serial += 1
            window._project_dirty = True
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
            window._history_restoring = False
            window._history_applying = False
            window.canvas.viewport().update()
