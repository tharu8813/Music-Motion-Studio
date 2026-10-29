"""OBS-style layer panel synchronized with canvas source state."""

from __future__ import annotations

from app.ui.design_system import COLORS

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.models.source import Source
from app.services.source_store import SourceStore
from app.utils.i18n import Translator


class LayerTreeWidget(QTreeWidget):
    """Tree that reports a completed internal move after Qt updates its rows."""

    layers_dropped = Signal()

    def dropEvent(self, event: QDropEvent) -> None:
        super().dropEvent(event)
        if event.isAccepted():
            self.layers_dropped.emit()


class LayerPanel(QFrame):
    """Layer tree providing visibility, locking, grouping, and z-order controls."""

    _kind_role = Qt.ItemDataRole.UserRole + 1
    _group_role = Qt.ItemDataRole.UserRole + 2

    def __init__(self, store: SourceStore, translator: Translator,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("layerPanel")
        self.store = store
        self.translator = translator
        self._refreshing = False
        self._refresh_pending = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(7)
        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("panelTitle")
        header.addWidget(self.title)
        header.addStretch()
        self.summary_label = QLabel()
        self.summary_label.setObjectName("mutedLabel")
        header.addWidget(self.summary_label)
        layout.addLayout(header)
        self.tree = LayerTreeWidget()
        self.tree.setObjectName("layerTree")
        self.tree.setColumnCount(3)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(16)
        self.tree.setMinimumHeight(190)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        layout.addWidget(self.tree, 1)
        controls = QHBoxLayout()
        self.front_button = QPushButton("⇈")
        self.up_button = QPushButton("↑")
        self.down_button = QPushButton("↓")
        self.back_button = QPushButton("⇊")
        self.unlock_all_button = QPushButton()
        self.group_button = QPushButton()
        self.ungroup_button = QPushButton()
        controls.addWidget(self.front_button)
        controls.addWidget(self.up_button)
        controls.addWidget(self.down_button)
        controls.addWidget(self.back_button)
        controls.addStretch()
        controls.addWidget(self.unlock_all_button)
        controls.addWidget(self.group_button)
        controls.addWidget(self.ungroup_button)
        layout.addLayout(controls)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.layers_dropped.connect(self._commit_tree_order)
        self.front_button.clicked.connect(lambda: self._move_to_edge(True))
        self.up_button.clicked.connect(lambda: self._move_selected(1))
        self.down_button.clicked.connect(lambda: self._move_selected(-1))
        self.back_button.clicked.connect(lambda: self._move_to_edge(False))
        self.group_button.clicked.connect(self._create_group)
        self.ungroup_button.clicked.connect(self._ungroup)
        self.unlock_all_button.clicked.connect(lambda: self.store.unlock_all())
        store.source_added.connect(lambda _source: self.schedule_refresh())
        store.source_removed.connect(lambda _source_id: self.schedule_refresh())
        store.source_changed.connect(lambda _source: self.schedule_refresh())
        store.sources_replaced.connect(self.schedule_refresh)
        store.groups_changed.connect(self.schedule_refresh)
        store.selection_set_changed.connect(self._select_sources)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()
        self.refresh()

    def retranslate(self) -> None:
        """Refresh panel text using the active app language."""
        korean = self.translator.is_korean
        self.title.setText("레이어" if korean else "Layers")
        self.tree.setHeaderLabels(
            ["이름" if korean else "Name", "표시" if korean else "Show", "잠금" if korean else "Lock"]
        )
        self.group_button.setText("그룹" if korean else "Group")
        self.ungroup_button.setText("해제" if korean else "Ungroup")
        self.unlock_all_button.setText("잠금 해제" if korean else "Unlock all")
        self.unlock_all_button.setToolTip(
            "잠긴 모든 레이어의 잠금을 해제합니다." if korean else
            "Unlock every locked layer."
        )
        self.front_button.setToolTip("맨 앞으로" if korean else "Bring to front")
        self.up_button.setToolTip("한 단계 앞으로" if korean else "Move forward")
        self.down_button.setToolTip("한 단계 뒤로" if korean else "Move backward")
        self.back_button.setToolTip("맨 뒤로" if korean else "Send to back")
        self.group_button.setToolTip(
            "선택한 요소를 새 그룹으로 묶습니다." if korean else
            "Place selected sources in a new group."
        )
        self.ungroup_button.setToolTip(
            "선택한 요소 또는 그룹을 해제합니다." if korean else
            "Ungroup selected sources or groups."
        )
        self.tree.setToolTip(
            "드래그하여 레이어 순서나 그룹을 변경합니다. 더블클릭 또는 F2로 이름을 바꿉니다."
            if korean else
            "Drag to change layer order or group. Double-click or press F2 to rename."
        )
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the visible tree from the store's group and z-order state."""
        if self._refreshing:
            return
        selected_ids = set(self.store.selected_ids)
        active_id = self.store.selected.id if self.store.selected else None
        active_item: QTreeWidgetItem | None = None
        self._refreshing = True
        try:
            self.tree.clear()
            groups = self.store.groups()
            group_items: dict[str, QTreeWidgetItem] = {}
            for group in groups:
                item = QTreeWidgetItem([group.name, "", ""])
                item.setData(0, self._kind_role, "group")
                item.setData(0, self._group_role, group.id)
                item.setFlags(
                    (item.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDropEnabled)
                    & ~Qt.ItemFlag.ItemIsDragEnabled
                )
                item.setFirstColumnSpanned(False)
                self.tree.addTopLevelItem(item)
                group_items[group.id] = item
            ungrouped_label = "미분류" if self.translator.is_korean else "Ungrouped"
            ungrouped = QTreeWidgetItem([ungrouped_label, "", ""])
            ungrouped.setData(0, self._kind_role, "root")
            ungrouped.setFlags(
                (ungrouped.flags() | Qt.ItemFlag.ItemIsDropEnabled)
                & ~(Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsEditable)
            )
            self.tree.addTopLevelItem(ungrouped)
            for source in reversed(self.store.sources()):
                parent = group_items.get(source.group_id, ungrouped)
                item = self._source_item(source)
                parent.addChild(item)
                if source.id in selected_ids:
                    item.setSelected(True)
                if source.id == active_id:
                    active_item = item
            for group_item in group_items.values():
                group_item.setExpanded(True)
            ungrouped.setExpanded(True)
            if not ungrouped.childCount():
                ungrouped.setHidden(True)
            if active_item is not None:
                self.tree.setCurrentItem(
                    active_item, 0, QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            self._refreshing = False
        self._update_controls()

    def schedule_refresh(self) -> None:
        """Defer a complete tree rebuild until the active Qt item event has ended."""
        if self._refresh_pending:
            return
        self._refresh_pending = True

        def perform_refresh() -> None:
            self._refresh_pending = False
            self.refresh()

        QTimer.singleShot(0, self, perform_refresh)  # dropped if the panel is deleted first

    _LOCKED_FOREGROUND = QBrush(QColor(COLORS["warning"]))

    def _source_item(self, source: Source) -> QTreeWidgetItem:
        item = QTreeWidgetItem([source.name, "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, source.id)
        item.setData(0, self._kind_role, "source")
        flags = (
            item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsEditable
        ) & ~Qt.ItemFlag.ItemIsDropEnabled
        if source.locked:
            # A locked source is not selectable on the Canvas; keep the Layer row
            # consistent while its lock checkbox stays clickable (unlock path).
            flags &= ~Qt.ItemFlag.ItemIsSelectable
            for column in range(3):
                item.setForeground(column, self._LOCKED_FOREGROUND)
        item.setFlags(flags)
        item.setCheckState(1, Qt.CheckState.Checked if source.visible else Qt.CheckState.Unchecked)
        item.setCheckState(2, Qt.CheckState.Checked if source.locked else Qt.CheckState.Unchecked)
        type_name = source.source_type.value.replace("_", " ").title()
        item.setToolTip(0, f"{type_name} · z {source.z_index}")
        item.setData(0, Qt.ItemDataRole.AccessibleDescriptionRole, type_name)
        return item

    def selected_source_ids(self) -> list[str]:
        """Return selected source identifiers, excluding groups and locked rows."""
        ids: list[str] = []
        for item in self.tree.selectedItems():
            if item.data(0, self._kind_role) != "source":
                continue
            source_id = item.data(0, Qt.ItemDataRole.UserRole)
            source = self.store.get(str(source_id))
            if source is not None and source.locked:
                continue
            ids.append(source_id)
        return ids

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._refreshing:
            return
        kind = item.data(0, self._kind_role)
        if kind == "group" and column == 0:
            self.store.rename_group(str(item.data(0, self._group_role)), item.text(0))
            return
        if kind == "source":
            source_id = str(item.data(0, Qt.ItemDataRole.UserRole))
            if column == 0:
                source = self.store.get(source_id)
                name = item.text(0).strip()
                if source is not None and name and name != source.name:
                    self.store.update(source_id, name=name)
                elif not name:
                    self.schedule_refresh()
            elif column == 1:
                self.store.update(source_id, visible=item.checkState(1) == Qt.CheckState.Checked)
            elif column == 2:
                self.store.update(source_id, locked=item.checkState(2) == Qt.CheckState.Checked)

    def _current_changed(self, current: QTreeWidgetItem | None,
                         previous: QTreeWidgetItem | None) -> None:
        del previous
        if not self._refreshing and current is not None:
            self._publish_selection()

    def _selection_changed(self) -> None:
        if not self._refreshing:
            self._publish_selection()
        self._update_controls()

    def _publish_selection(self) -> None:
        source_ids = self.selected_source_ids()
        current = self.tree.currentItem()
        active_id = (
            current.data(0, Qt.ItemDataRole.UserRole)
            if current is not None and current.data(0, self._kind_role) == "source"
            else source_ids[-1] if source_ids else None
        )
        self.store.select_many(source_ids, active_id)

    def _select_sources(self, source_ids: object, active: Source | None) -> None:
        if self._refreshing:
            return
        selected = set(source_ids if isinstance(source_ids, (tuple, list)) else ())
        active_item: QTreeWidgetItem | None = None
        for row in range(self.tree.topLevelItemCount()):
            root = self.tree.topLevelItem(row)
            for child_index in range(root.childCount()):
                child = root.child(child_index)
                source_id = child.data(0, Qt.ItemDataRole.UserRole)
                if active is not None and source_id == active.id:
                    active_item = child
        self._refreshing = True
        try:
            self.tree.clearSelection()
            for row in range(self.tree.topLevelItemCount()):
                root = self.tree.topLevelItem(row)
                for child_index in range(root.childCount()):
                    child = root.child(child_index)
                    child.setSelected(
                        child.data(0, Qt.ItemDataRole.UserRole) in selected
                    )
            if active_item is not None:
                self.tree.setCurrentItem(
                    active_item, 0, QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            self._refreshing = False
        self._update_controls()

    def _move_selected(self, direction: int) -> None:
        selected = self.selected_source_ids()
        if selected:
            self.store.move_layers(selected, direction)

    def _move_to_edge(self, front: bool) -> None:
        selected = self.selected_source_ids()
        if selected:
            self.store.move_layers_to_edge(selected, front)

    def _commit_tree_order(self) -> None:
        """Translate the dropped visual order into group membership and z-index."""
        ungrouped: QTreeWidgetItem | None = None
        top_level_sources: list[QTreeWidgetItem] = []
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item.data(0, self._kind_role) == "root":
                ungrouped = item
            elif item.data(0, self._kind_role) == "source":
                top_level_sources.append(item)
        if ungrouped is not None:
            for item in top_level_sources:
                index = self.tree.indexOfTopLevelItem(item)
                if index >= 0:
                    self.tree.takeTopLevelItem(index)
                    ungrouped.insertChild(0, item)

        front_to_back: list[str] = []
        group_assignments: dict[str, str | None] = {}
        for row in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(row)
            kind = parent.data(0, self._kind_role)
            if kind == "source":
                source_id = str(parent.data(0, Qt.ItemDataRole.UserRole))
                front_to_back.append(source_id)
                group_assignments[source_id] = None
                continue
            group_id = (
                str(parent.data(0, self._group_role)) if kind == "group" else None
            )
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                if child.data(0, self._kind_role) != "source":
                    continue
                source_id = str(child.data(0, Qt.ItemDataRole.UserRole))
                front_to_back.append(source_id)
                group_assignments[source_id] = group_id
        selected = self.selected_source_ids()
        self.store.reorder_layers(front_to_back, group_assignments)
        self.store.select_many(selected, selected[-1] if selected else None)
        self.schedule_refresh()

    def _update_controls(self) -> None:
        selected = set(self.selected_source_ids())
        ordered = self.store.sources()
        selected_indices = [
            index for index, source in enumerate(ordered) if source.id in selected
        ]
        can_forward = any(
            index + 1 < len(ordered) and ordered[index + 1].id not in selected
            for index in selected_indices
        )
        can_backward = any(
            index > 0 and ordered[index - 1].id not in selected
            for index in selected_indices
        )
        self.front_button.setEnabled(can_forward)
        self.up_button.setEnabled(can_forward)
        self.down_button.setEnabled(can_backward)
        self.back_button.setEnabled(can_backward)
        locked_count = sum(source.locked for source in ordered)
        self.unlock_all_button.setEnabled(locked_count > 0)
        self.unlock_all_button.setVisible(locked_count > 0)
        self.group_button.setEnabled(len(selected) >= 2)
        selected_groups = {
            item.data(0, self._group_role)
            for item in self.tree.selectedItems()
            if item.data(0, self._kind_role) == "group"
        }
        self.ungroup_button.setEnabled(
            bool(selected_groups)
            or any(
                source.group_id is not None for source in ordered
                if source.id in selected
            )
        )
        korean = self.translator.is_korean
        self.summary_label.setText(
            f"{len(ordered)}개 · {len(selected)}개 선택"
            if korean else f"{len(ordered)} layers · {len(selected)} selected"
        )

    def _create_group(self) -> None:
        selected = self.selected_source_ids()
        korean = self.translator.is_korean
        label = "그룹 이름" if korean else "Group name"
        title = "새 레이어 그룹" if korean else "New layer group"
        name, accepted = QInputDialog.getText(self, title, label)
        if accepted:
            self.store.add_group(name, selected)

    def _ungroup(self) -> None:
        group_ids = {
            item.data(0, self._group_role)
            for item in self.tree.selectedItems()
            if item.data(0, self._kind_role) == "group"
        }
        for group_id in group_ids:
            self.store.remove_group(group_id)
        source_ids = self.selected_source_ids()
        if source_ids:
            self.store.assign_group(source_ids, None)
