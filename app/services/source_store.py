"""Observable source collection used by presentation widgets."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QObject, Signal

from app.models.source import Source, SourceType
from app.models.source_registry import source_registry
from app.models.layer import LayerGroup


class SourceStore(QObject):
    """Single source of truth for the Phase 1A canvas sources."""

    source_added = Signal(Source)
    source_removed = Signal(str)
    source_changed = Signal(Source)
    sources_replaced = Signal()
    selection_changed = Signal(object)
    selection_set_changed = Signal(object, object)
    groups_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sources: dict[str, Source] = {}
        self._groups: dict[str, LayerGroup] = {}
        self._selected_id: str | None = None
        self._selected_ids: tuple[str, ...] = ()

    def add(self, source: Source) -> None:
        """Add a source and make it the active selection."""
        self._sources[source.id] = source
        self.source_added.emit(source)
        self.select(source.id)

    def remove(self, source_id: str) -> None:
        """Remove one source if it exists."""
        if source_id not in self._sources:
            return
        del self._sources[source_id]
        self.source_removed.emit(source_id)
        if source_id in self._selected_ids:
            remaining = [entry for entry in self._selected_ids if entry != source_id]
            active = self._selected_id if self._selected_id != source_id else None
            self.select_many(remaining, active)

    def get(self, source_id: str | None) -> Source | None:
        """Return a source by identifier."""
        return self._sources.get(source_id) if source_id else None

    def sources(self) -> list[Source]:
        """Return sources ordered by stacking index."""
        return sorted(self._sources.values(), key=lambda source: source.z_index)

    def groups(self) -> list[LayerGroup]:
        """Return groups in their creation order."""
        return list(self._groups.values())

    def add_group(self, name: str, source_ids: Iterable[str] = ()) -> LayerGroup:
        """Create a group and optionally add existing sources to it."""
        group = LayerGroup(name=name.strip() or "Group")
        self._groups[group.id] = group
        self.assign_group(source_ids, group.id)
        self.groups_changed.emit()
        return group

    def remove_group(self, group_id: str) -> None:
        """Ungroup sources then remove the specified group."""
        if group_id not in self._groups:
            return
        for source in self._sources.values():
            if source.group_id == group_id:
                source.group_id = None
                self.source_changed.emit(source)
        del self._groups[group_id]
        self.groups_changed.emit()

    def assign_group(self, source_ids: Iterable[str], group_id: str | None) -> None:
        """Assign sources to a group, or clear their group membership."""
        if group_id is not None and group_id not in self._groups:
            return
        changed = False
        for source_id in source_ids:
            source = self._sources.get(source_id)
            if source and source.group_id != group_id:
                source.group_id = group_id
                self.source_changed.emit(source)
                changed = True
        if changed:
            self.groups_changed.emit()

    def move_layer(self, source_id: str, direction: int) -> None:
        """Move one source one drawing level up or down."""
        self.move_layers([source_id], direction)

    def move_layers(self, source_ids: Iterable[str], direction: int) -> None:
        """Move a selected layer block one step while preserving internal order."""
        selected = {source_id for source_id in source_ids if source_id in self._sources}
        if not selected or direction == 0:
            return
        ordered = self.sources()
        indices = (
            range(len(ordered) - 2, -1, -1)
            if direction > 0 else range(1, len(ordered))
        )
        for index in indices:
            neighbor = index + 1 if direction > 0 else index - 1
            if ordered[index].id in selected and ordered[neighbor].id not in selected:
                ordered[index], ordered[neighbor] = ordered[neighbor], ordered[index]
        self._apply_back_to_front_order(ordered)

    def move_layers_to_edge(self, source_ids: Iterable[str], front: bool) -> None:
        """Move selected layers to the front or back as one stable block."""
        selected = {source_id for source_id in source_ids if source_id in self._sources}
        if not selected:
            return
        ordered = self.sources()
        moving = [source for source in ordered if source.id in selected]
        remaining = [source for source in ordered if source.id not in selected]
        self._apply_back_to_front_order(
            [*remaining, *moving] if front else [*moving, *remaining]
        )

    def reorder_layers(
        self, source_ids_front_to_back: Iterable[str],
        group_assignments: dict[str, str | None] | None = None,
    ) -> None:
        """Commit the visual layer-tree order and optional group destinations."""
        front_to_back: list[str] = []
        seen: set[str] = set()
        for source_id in source_ids_front_to_back:
            if source_id in self._sources and source_id not in seen:
                front_to_back.append(source_id)
                seen.add(source_id)
        # Retain any source omitted by a temporary tree state at the back.
        front_to_back.extend(
            source.id for source in reversed(self.sources()) if source.id not in seen
        )
        group_changed = False
        if group_assignments:
            for source_id, group_id in group_assignments.items():
                if group_id is not None and group_id not in self._groups:
                    continue
                source = self._sources.get(source_id)
                if source is not None and source.group_id != group_id:
                    source.group_id = group_id
                    group_changed = True
        self._apply_back_to_front_order(
            [self._sources[source_id] for source_id in reversed(front_to_back)]
        )
        if group_changed:
            self.groups_changed.emit()

    def rename_group(self, group_id: str, name: str) -> None:
        """Rename a layer group without changing its members."""
        group = self._groups.get(group_id)
        cleaned = name.strip()
        if group is not None and cleaned and group.name != cleaned:
            group.name = cleaned
            self.groups_changed.emit()

    def _apply_back_to_front_order(self, ordered: list[Source]) -> None:
        for z_index, source in enumerate(ordered):
            if source.z_index != z_index:
                source.z_index = z_index
                self.source_changed.emit(source)

    def select(self, source_id: str | None) -> None:
        """Select one existing source or clear the complete selection."""
        self.select_many([source_id] if source_id else [], source_id)

    def select_many(
        self, source_ids: Iterable[str], active_id: str | None = None,
    ) -> None:
        """Publish a shared multi-selection plus one Inspector-active source."""
        valid: list[str] = []
        seen: set[str] = set()
        for source_id in source_ids:
            if source_id in self._sources and source_id not in seen:
                valid.append(source_id)
                seen.add(source_id)
        if active_id not in seen:
            active_id = valid[-1] if valid else None
        selected_ids = tuple(valid)
        if selected_ids == self._selected_ids and active_id == self._selected_id:
            return
        self._selected_ids = selected_ids
        self._selected_id = active_id
        active = self.get(active_id)
        self.selection_set_changed.emit(selected_ids, active)
        self.selection_changed.emit(active)

    @property
    def selected(self) -> Source | None:
        """Return the currently selected source."""
        return self.get(self._selected_id)

    @property
    def selected_ids(self) -> tuple[str, ...]:
        """Return the ordered selection shared by Canvas and Layers."""
        return self._selected_ids

    def update_component(self, source_id: str, **changes: object) -> None:
        """Validate component field edits atomically, then publish the legacy update."""
        source = self._sources.get(source_id)
        if source is None:
            return
        updates = source_registry.component_changes(source, **changes)
        if updates:
            self.update(source_id, **updates)

    def update(self, source_id: str, **changes: object) -> None:
        """Apply model changes and notify all dependent widgets."""
        source = self._sources.get(source_id)
        if source is None:
            return
        if "name" in changes and (not isinstance(changes["name"], str) or not changes["name"].strip()):
            changes["name"] = source.name
        if source.source_type is SourceType.ALBUM_COVER:
            # Album artwork is a square source everywhere, including Inspector
            # edits and multi-selection updates. Canvas resize already supplies
            # equal values, while a single-field edit mirrors its new value.
            if "width" in changes:
                changes["height"] = changes["width"]
            elif "height" in changes:
                changes["width"] = changes["height"]
        for name, value in changes.items():
            if hasattr(source, name):
                setattr(source, name, value)
        self.source_changed.emit(source)
        if changes.get("locked") is True and source_id in self._selected_ids:
            # A locked source cannot be selected anywhere, so drop it from the
            # shared Canvas/Layer/Inspector selection the moment it is locked.
            self.select_many(
                [entry for entry in self._selected_ids if entry != source_id],
                self._selected_id if self._selected_id != source_id else None,
            )

    def unlock_all(self) -> int:
        """Clear the lock flag on every locked source; return how many changed."""
        locked_ids = [
            source_id for source_id, source in self._sources.items() if source.locked
        ]
        for source_id in locked_ids:
            self.update(source_id, locked=False)
        return len(locked_ids)

    def replace(self, sources: Iterable[Source], groups: Iterable[LayerGroup] = ()) -> None:
        """Replace the collection, primarily for future project loading."""
        self._sources = {source.id: source for source in sources}
        self._groups = {group.id: group for group in groups}
        self._selected_id = None
        self._selected_ids = ()
        self.sources_replaced.emit()
        self.groups_changed.emit()
        self.selection_set_changed.emit((), None)
        self.selection_changed.emit(None)
