"""Snapshot-based undo/redo history for project editing operations."""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, Signal


class HistoryService(QObject):
    """Stores immutable project snapshots and exposes undo/redo navigation."""

    changed = Signal(bool, bool)

    def __init__(self, parent: QObject | None = None, max_entries: int = 100) -> None:
        super().__init__(parent)
        self._snapshots: list[dict[str, Any]] = []
        self._index = -1
        self._max_entries = max(10, max_entries)

    @staticmethod
    def _copy(snapshot: Any) -> Any:
        """Create a JSON-safe deep copy, preventing later mutable-state leaks."""
        return json.loads(json.dumps(snapshot))

    def reset(self, snapshot: dict[str, Any]) -> None:
        """Start a fresh history rooted at the supplied project state."""
        self._snapshots = [self._copy(snapshot)]
        self._index = 0
        self._emit_changed()

    def commit(self, snapshot: dict[str, Any]) -> None:
        """Append a state only when it differs from the active history entry."""
        if self._index >= 0 and self._snapshots[self._index] == snapshot:
            return
        previous = self._snapshots[self._index] if self._index >= 0 else {}
        # Only private snapshots share branches; callers always get detached copies.
        copied = {
            key: previous[key] if key in previous and previous[key] == value else self._copy(value)
            for key, value in snapshot.items()
        }
        if self._index >= 0 and self._snapshots[self._index] == copied:
            return
        del self._snapshots[self._index + 1:]
        self._snapshots.append(copied)
        self._index = len(self._snapshots) - 1
        overflow = len(self._snapshots) - self._max_entries
        if overflow > 0:
            del self._snapshots[:overflow]
            self._index -= overflow
        self._emit_changed()

    def undo(self) -> dict[str, Any] | None:
        """Move to the preceding snapshot, if one exists."""
        if self._index <= 0:
            return None
        self._index -= 1
        self._emit_changed()
        return self._copy(self._snapshots[self._index])

    def redo(self) -> dict[str, Any] | None:
        """Move to the following snapshot, if one exists."""
        if self._index >= len(self._snapshots) - 1:
            return None
        self._index += 1
        self._emit_changed()
        return self._copy(self._snapshots[self._index])

    def retained_source_ids(self) -> set[str]:
        """Return source IDs reachable from any current Undo/redo snapshot."""
        retained: set[str] = set()
        for snapshot in self._snapshots:
            sources = snapshot.get("sources", [])
            if not isinstance(sources, list):
                continue
            retained.update(
                source_id
                for source in sources
                if isinstance(source, dict)
                for source_id in [source.get("id")]
                if isinstance(source_id, str) and source_id
            )
        return retained

    def _emit_changed(self) -> None:
        self.changed.emit(self._index > 0, self._index < len(self._snapshots) - 1)
