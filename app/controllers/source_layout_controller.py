"""Source layout operations depend on model updates and scene bounds, not a window."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRectF

from app.models.source import Source
from app.services.source_store import SourceStore


class SourceLayoutController:
    def __init__(self, store: SourceStore, content_bounds: Callable[[str], QRectF | None]) -> None:
        self.store = store
        self.content_bounds = content_bounds

    def _bounds(self, sources: list[Source]) -> list[tuple[Source, QRectF]]:
        return [
            (source, bounds) for source in sources
            if not source.locked and (bounds := self.content_bounds(source.id)) is not None
        ]

    def nudge(self, sources: list[Source], x_delta: float, y_delta: float) -> None:
        for source in sources:
            if not source.locked:
                self.store.update(source.id, x=source.x + x_delta, y=source.y + y_delta)

    def center(self, sources: list[Source], artboard: QRectF, horizontal: bool) -> None:
        entries = self._bounds(sources)
        if not entries:
            return
        bounds = QRectF(entries[0][1])
        for _source, rect in entries[1:]:
            bounds = bounds.united(rect)
        delta = (artboard.center().x() - bounds.center().x() if horizontal
                 else artboard.center().y() - bounds.center().y())
        for source, _rect in entries:
            changes = {"x": source.x + delta} if horizontal else {"y": source.y + delta}
            self.store.update(source.id, **changes)

    def align(self, sources: list[Source], mode: str) -> None:
        entries = self._bounds(sources)
        if len(entries) < 2:
            return
        left = min(rect.left() for _source, rect in entries)
        right = max(rect.right() for _source, rect in entries)
        top = min(rect.top() for _source, rect in entries)
        bottom = max(rect.bottom() for _source, rect in entries)
        for source, rect in entries:
            if mode == "left":
                self.store.update(source.id, x=source.x + left - rect.left())
            elif mode == "hcenter":
                self.store.update(source.id, x=source.x + (left + right) / 2 - rect.center().x())
            elif mode == "right":
                self.store.update(source.id, x=source.x + right - rect.right())
            elif mode == "top":
                self.store.update(source.id, y=source.y + top - rect.top())
            elif mode == "vcenter":
                self.store.update(source.id, y=source.y + (top + bottom) / 2 - rect.center().y())
            elif mode == "bottom":
                self.store.update(source.id, y=source.y + bottom - rect.bottom())

    def distribute(self, sources: list[Source], axis: str) -> None:
        """Keep the two outside sources fixed and equalize gaps between transformed edges."""
        entries = self._bounds(sources)
        if len(entries) < 3:
            return
        horizontal = axis == "horizontal"
        entries.sort(key=lambda entry: entry[1].left() if horizontal else entry[1].top())
        extents = [rect.width() if horizontal else rect.height() for _source, rect in entries]
        start = entries[0][1].left() if horizontal else entries[0][1].top()
        end = entries[-1][1].right() if horizontal else entries[-1][1].bottom()
        gap = (end - start - sum(extents)) / (len(entries) - 1)
        cursor = start
        for (source, rect), extent in zip(entries, extents):
            changes = {"x": source.x + cursor - rect.left()} if horizontal else {
                "y": source.y + cursor - rect.top(),
            }
            self.store.update(source.id, **changes)
            cursor += extent + gap
