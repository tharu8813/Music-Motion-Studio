"""Centered Canvas splitter: fixed edge panels and resizable center pane.

Pure QWidget layout behavior. The main window owns workspace persistence and
re-exports this type to preserve the historical import path.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QWidget


class CanvasCenteredSplitter(QSplitter):
    """Keep edge panels fixed while the Canvas-side pane absorbs window resize."""

    def __init__(
        self, orientation: Qt.Orientation, center_index: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self._center_index = center_index
        self._edge_sizes: dict[int, int] = {}
        self._restoring_edges = False
        self.splitterMoved.connect(self._remember_edge_sizes)

    def lock_edge_sizes(self, sizes: dict[int, int] | None = None) -> None:
        """Use the current user-visible edge sizes for future window resizes."""
        if sizes is not None:
            self._edge_sizes = {
                int(index): max(0, int(size))
                for index, size in sizes.items()
                if int(index) != self._center_index
            }
            return
        current = self.sizes()
        self._edge_sizes = {
            index: size for index, size in enumerate(current)
            if index != self._center_index
        }

    def setSizes(self, sizes: list[int]) -> None:  # noqa: N802 - Qt API name
        super().setSizes(sizes)
        if not self._restoring_edges:
            self.lock_edge_sizes()

    def _remember_edge_sizes(self, _position: int, _index: int) -> None:
        if not self._restoring_edges:
            self.lock_edge_sizes()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        before_resize = self.sizes()
        old_size = event.oldSize()
        old_extent = (
            old_size.width()
            if self.orientation() == Qt.Orientation.Horizontal
            else old_size.height()
        )
        remembered = dict(self._edge_sizes)
        if not remembered and old_extent > 0 and sum(before_resize) > 0:
            remembered = {
                index: size for index, size in enumerate(before_resize)
                if index != self._center_index
            }
        super().resizeEvent(event)
        if not remembered or self.count() <= self._center_index:
            return
        current = self.sizes()
        if len(current) != self.count():
            return
        total = sum(current)
        target = list(current)
        fixed_total = 0
        for index, size in remembered.items():
            widget = self.widget(index)
            if widget is None:
                continue
            if widget.isHidden() or (
                self.orientation() == Qt.Orientation.Horizontal
                and widget.maximumWidth() == 0
            ):
                preserved = 0
            else:
                preserved = max(0, size)
            target[index] = preserved
            fixed_total += preserved
        target[self._center_index] = max(0, total - fixed_total)
        self._restoring_edges = True
        try:
            super().setSizes(target)
        finally:
            self._restoring_edges = False
        # A temporarily small window may force Qt to compress edge panels.
        # Keep the user's remembered sizes so expanding the window restores
        # them instead of treating the temporary compression as a new choice.
        self.lock_edge_sizes(remembered)
