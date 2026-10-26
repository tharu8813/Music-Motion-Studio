"""Transient editing state shared by history and persistence, never saved in a project."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class ProjectSession:
    """GUI-thread policy; workers carry a frozen change serial, never this live object."""

    ready: bool = False
    dirty: bool = False
    change_serial: int = 0
    _change_suspensions: int = field(default=0, init=False, repr=False)

    @property
    def tracking_changes(self) -> bool:
        return self.ready and self._change_suspensions == 0

    def mark_changed(self) -> None:
        """Record an edit, including an explicitly completed undo/redo restore."""
        self.change_serial += 1
        self.dirty = True

    def mark_saved(self, snapshot_serial: int) -> bool:
        """Keep edits made after the background save's snapshot marked unsaved."""
        self.dirty = snapshot_serial != self.change_serial
        return not self.dirty

    @contextmanager
    def suspend_changes(self) -> Iterator[None]:
        """Suppress incidental model signals through nested restores and failures."""
        self._change_suspensions += 1
        try:
            yield
        finally:
            self._change_suspensions -= 1
