"""Dedicated property lanes, leaving the existing exposure timeline uncluttered."""
from dataclasses import replace
from math import ceil, log10

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QBrush, QPen, QPolygonF, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGraphicsItem,
    QGraphicsPolygonItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from app.animation.keyframes import INTERPOLATIONS, Keyframe, PROPERTIES
from app.ui.design_system import COLORS


class Diamond(QGraphicsPolygonItem):
    def __init__(self, editor, name, point, row):
        super().__init__(QPolygonF([QPointF(0, -6), QPointF(6, 0), QPointF(0, 6), QPointF(-6, 0)]))
        self.editor, self.name, self.point, self.row = editor, name, point, row
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setBrush(QBrush(QColor(COLORS["warning"])))
        self.setPen(QPen(QColor(COLORS["warning"])))
        self.setPos(editor.time_x(point.time), editor.row_y(row))
        self.setToolTip(f"{PROPERTIES[name].labels[0 if editor.korean else 1]} · {point.time:.3f}s · {point.value:g} · {point.interpolation}")

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.scene():
            return QPointF(max(self.editor.LEFT, value.x()), self.editor.row_y(self.row))
        return super().itemChange(change, value)

    def paint(self, painter, option, widget=None):
        painter.setBrush(QColor(COLORS["text"]) if self.isSelected() else QColor(COLORS["warning"]))
        painter.setPen(QPen(QColor(COLORS["warning"]), 2 if self.isSelected() else 1))
        painter.drawPolygon(self.polygon())

    def mousePressEvent(self, event):
        self.editor.dragging = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        editor = self.editor
        selected, replacements = [], []
        for item in self.scene().selectedItems():
            if not isinstance(item, Diamond):
                continue
            seconds = editor.x_time(item.pos().x())
            if editor.snap.isChecked():
                candidates = [round(seconds * editor.fps) / editor.fps]
                candidates += [p.time for points in editor.controller.curves().values() for p in points
                               if abs(p.time - item.point.time) > 1e-6]
                nearest = min(candidates, key=lambda t: abs(t - seconds))
                if abs(nearest - seconds) * editor.zoom <= 8:
                    seconds = nearest
            selected.append((item.name, item.point.time))
            replacements.append((item.name, replace(item.point, time=round(seconds, 6))))
        editor.dragging = False
        if any(abs(point.time - old[1]) > 1e-6 for old, (_, point) in zip(selected, replacements)):
            editor.selected = {(name, point.time) for name, point in replacements}
            editor.controller.replace_points(selected, replacements)
        else:
            editor.refresh()


class LaneView(QGraphicsView):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not isinstance(self.itemAt(event.position().toPoint()), Diamond):
            editor = self.parentWidget()
            position = self.mapToScene(event.position().toPoint())
            if position.x() >= editor.LEFT:
                editor.controller.seek(editor.x_time(position.x()))
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        editor = self.parentWidget()
        if event.matches(QKeySequence.StandardKey.SelectAll):
            for item in self.scene().items():
                if isinstance(item, Diamond):
                    item.setSelected(True)
        elif event.matches(QKeySequence.StandardKey.Copy):
            editor.controller.copy(editor.selection() or None)
        elif event.matches(QKeySequence.StandardKey.Paste):
            editor.controller.paste()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            editor.controller.replace_points(editor.selection())
        elif event.key() == Qt.Key.Key_Escape:
            self.scene().clearSelection()
            editor.refresh()
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            direction = 1 if event.key() == Qt.Key.Key_Right else -1
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                editor.controller.navigate(direction)
            elif editor.selection():
                delta = direction / editor.fps
                replacements = [(item.name, replace(item.point, time=max(0, item.point.time + delta)))
                                for item in self.scene().selectedItems() if isinstance(item, Diamond)]
                selected = editor.selection()
                editor.selected = {(name, point.time) for name, point in replacements}
                editor.controller.replace_points(selected, replacements)
            else:
                editor.controller.seek(editor.controller.time + direction / editor.fps)
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.parentWidget().change_zoom(1.25 if event.angleDelta().y() > 0 else .8)
            event.accept()
        else:
            super().wheelEvent(event)


class KeyframeEditor(QWidget):
    LEFT, ROW = 150, 32

    def __init__(self, controller, translator, parent=None):
        super().__init__(parent)
        self.controller, self.translator = controller, translator
        self.zoom, self.fps = 24.0, 30.0
        self.selected = set()
        self.dragging = self._refreshing = False
        self._context = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source_combo, self.mode, self.track = QComboBox(), QComboBox(), QComboBox()
        from app.animation.keyframes import MODES
        for mode in MODES:
            self.mode.addItem(mode, mode)
        self.source_combo.setMinimumWidth(110)
        self.track.setMinimumWidth(110)
        self.previous_track, self.next_track = QPushButton("‹"), QPushButton("›")
        self.previous_track.setFixedWidth(28)
        self.next_track.setFixedWidth(28)
        self.expanded = QCheckBox()
        self.expanded.setChecked(True)
        self.enabled = QCheckBox()
        self.snap = QCheckBox()
        self.snap.setChecked(True)
        self.frame_rate = QComboBox()
        for fps in (24, 25, 30, 50, 60, 120, 240):
            self.frame_rate.addItem(f"{fps} fps", fps)
        self.frame_rate.setCurrentIndex(2)
        self.frame_rate.setMinimumWidth(55)
        self.frame_rate.setMaximumWidth(80)
        self.frame_rate.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.frame_rate.setMinimumContentsLength(3)
        self.frame_rate.setAccessibleName("프레임 스냅 기준 / Frame snapping rate")
        self.frame_rate.setToolTip("프레임 스냅 기준 / Frame snapping rate")
        self.frame_rate.currentIndexChanged.connect(lambda _: setattr(self, "fps", float(self.frame_rate.currentData())))
        self.fit, self.minus, self.plus = QPushButton(), QPushButton("−"), QPushButton("+")
        self.fit.setFixedWidth(54)
        self.minus.setFixedWidth(30)
        self.plus.setFixedWidth(30)
        toolbar = QHBoxLayout()
        for widget in (self.expanded, self.source_combo, self.mode, self.previous_track, self.track, self.next_track,
                       self.enabled, self.snap, self.fit, self.minus, self.plus):
            toolbar.addWidget(widget)
        layout.addLayout(toolbar)
        self.scene = QGraphicsScene(self)
        self.view = LaneView(self.scene, self)
        self.view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.view.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.view.setMinimumHeight(110)
        self.view.setBackgroundBrush(QColor(COLORS["field"]))
        self.view.setAccessibleName("키프레임 타임라인 / Keyframe timeline")
        layout.addWidget(self.view, 1)
        tools = QHBoxLayout()
        self.property = QComboBox()
        for name in PROPERTIES:
            self.property.addItem(name, name)
        self.time, self.value = QDoubleSpinBox(), QDoubleSpinBox()
        self.time.setRange(0, 8640000)
        self.time.setDecimals(3)
        self.time.setSuffix(" s")
        self.value.setDecimals(3)
        self.interpolation = QComboBox()
        for easing in INTERPOLATIONS:
            self.interpolation.addItem(easing, easing)
        self.interpolation.setCurrentIndex(INTERPOLATIONS.index("ease_in_out"))
        self.add, self.apply, self.delete = QPushButton(), QPushButton(), QPushButton()
        self.copy, self.paste, self.clear = QPushButton(), QPushButton(), QPushButton()
        self.prev, self.next = QPushButton("‹ ◆"), QPushButton("◆ ›")
        for widget in (self.property, self.time, self.value, self.interpolation, self.add, self.apply,
                       self.delete, self.copy, self.paste, self.clear, self.prev, self.next):
            tools.addWidget(widget)
        layout.addLayout(tools)
        # Compact rows stay usable beside a narrow Inspector and at high DPI.
        utility = QHBoxLayout()
        for widget in (self.expanded, self.snap, self.fit, self.minus, self.plus):
            toolbar.removeWidget(widget)
            utility.addWidget(widget)
        utility.insertWidget(2, self.frame_rate)
        utility.addStretch()
        for widget in (self.copy, self.paste, self.clear, self.prev, self.next):
            tools.removeWidget(widget)
            utility.addWidget(widget)
        layout.addLayout(utility)
        for combo in (self.source_combo, self.mode, self.track, self.property, self.interpolation):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(4)
            combo.setMinimumWidth(70)
            combo.setMaximumWidth(180)
        for spin in (self.time, self.value):
            spin.setMinimumWidth(70)
            spin.setMaximumWidth(100)
            spin.setKeyboardTracking(False)
        for button in (self.previous_track, self.next_track, self.fit, self.minus, self.plus, self.prev, self.next):
            button.setObjectName("timelineToolButton")
            button.setStyleSheet("padding: 3px; min-width: 0px;")
        self.prev.setFixedWidth(38)
        self.next.setFixedWidth(38)
        self.hint = QLabel()
        self.hint.setObjectName("mutedLabel")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.source_combo.currentIndexChanged.connect(lambda _: controller.store.select(self.source_combo.currentData()) if not self._refreshing else None)
        self.mode.currentIndexChanged.connect(lambda _: controller.configure(mode=self.mode.currentData()) if not self._refreshing else None)
        self.track.currentIndexChanged.connect(lambda _: controller.choose_track(self.track.currentData() or "") if not self._refreshing else None)
        self.previous_track.clicked.connect(lambda: self.track.setCurrentIndex(max(1, self.track.currentIndex() - 1)))
        self.next_track.clicked.connect(lambda: self.track.setCurrentIndex(min(self.track.count() - 1, self.track.currentIndex() + 1)))
        self.enabled.toggled.connect(lambda enabled: controller.configure(enabled=enabled) if not self._refreshing else None)
        self.expanded.toggled.connect(self.refresh)
        self.fit.clicked.connect(self.fit_all)
        self.minus.clicked.connect(lambda: self.change_zoom(.8))
        self.plus.clicked.connect(lambda: self.change_zoom(1.25))
        self.add.clicked.connect(lambda: controller.replace_points((), [(self.property.currentData(), Keyframe(controller.time, self.value.value(), self.interpolation.currentData()))]))
        self.delete.clicked.connect(lambda: controller.replace_points(self.selection()))
        self.copy.clicked.connect(lambda: controller.copy(self.selection() or None))
        self.paste.clicked.connect(controller.paste)
        self.clear.clicked.connect(self.confirm_clear)
        self.apply.clicked.connect(self.apply_selected)
        self.prev.clicked.connect(lambda: controller.navigate(-1))
        self.next.clicked.connect(lambda: controller.navigate(1))
        self.property.currentIndexChanged.connect(self.fill_value)
        self.time.editingFinished.connect(lambda: controller.seek(self.time.value()) if not self.selection() else None)
        self.scene.selectionChanged.connect(self.selection_changed)
        controller.changed.connect(self.refresh)
        controller.position_changed.connect(self.refresh_position)
        controller.edit_rejected.connect(lambda: self.hint.setText(
            "같은 속성·시간의 키프레임이 있어 이동하거나 붙여넣을 수 없습니다." if self.korean else
            "A keyframe already exists at this property and time. Move or delete it before pasting."))
        controller.store.source_added.connect(lambda *_: self.refresh())
        controller.store.source_removed.connect(lambda *_: self.refresh())
        translator.language_changed.connect(self.refresh)
        self.refresh()

    @property
    def korean(self):
        return self.translator.is_korean

    def time_x(self, seconds):
        return self.LEFT + seconds * self.zoom

    def x_time(self, x):
        return max(0, (x - self.LEFT) / self.zoom)

    def row_y(self, row):
        return 48 + row * self.ROW

    def change_zoom(self, ratio):
        self.zoom = min(3000, max(.01, self.zoom * ratio))
        self.refresh()

    def fit_all(self):
        last = max((p.time for points in self.controller.curves().values() for p in points), default=0)
        self.zoom = max(.01, (self.view.viewport().width() - self.LEFT - 30) / max(1, self.controller.duration, last))
        self.refresh()

    def selection(self):
        return {(item.name, item.point.time) for item in self.scene.selectedItems() if isinstance(item, Diamond)}

    def selection_changed(self):
        if self._refreshing or self.dragging:
            return
        self.selected = self.selection()
        items = [item for item in self.scene.selectedItems() if isinstance(item, Diamond)]
        if items:
            item = items[0]
            self.property.setCurrentIndex(self.property.findData(item.name))
            self.time.setValue(item.point.time)
            self.value.setValue(item.point.value)
            self.interpolation.setCurrentIndex(INTERPOLATIONS.index(item.point.interpolation))
        self.apply.setEnabled(len(items) == 1 and self.controller.editable)
        self.delete.setEnabled(bool(items) and self.controller.editable)

    def fill_value(self):
        name = self.property.currentData()
        spec = PROPERTIES[name]
        self.value.setRange(spec.minimum, spec.maximum)
        self.value.setSingleStep(spec.step)
        if self.controller.source:
            self.value.setValue(self.controller.evaluated().get(name, getattr(self.controller.source, name)))

    def apply_selected(self):
        if len(self.selection()) == 1:
            name, _ = next(iter(self.selection()))
            self.controller.replace_points(self.selection(), [(name, Keyframe(self.time.value(), self.value.value(), self.interpolation.currentData()))])

    def confirm_clear(self):
        if not self.controller.editable:
            return
        if QMessageBox.question(self, "키프레임 / Keyframes", "현재 시간 기준의 선택한 곡 키프레임을 모두 삭제할까요?" if self.korean else
                                "Delete all keyframes in the current time basis and selected track?") == QMessageBox.StandardButton.Yes:
            self.controller.clear_track()

    def refresh(self, *_):
        if self.dragging:
            return
        self._refreshing = True
        controller, source, ko = self.controller, self.controller.source, self.korean
        context = (source.id, source.keyframes.mode, controller.track_id) if source else None
        if context != self._context:
            self.selected.clear()
            self._context = context
        self.playhead_line = None
        self.scene.clear()
        self.source_combo.clear()
        self.source_combo.addItem("요소 선택…" if ko else "Choose source…", "")
        for item in controller.store.sources():
            if not item.locked:
                self.source_combo.addItem(item.name, item.id)
        self.source_combo.setCurrentIndex(max(0, self.source_combo.findData(source.id if source else "")))
        for index, label in enumerate(("전체 영상", "특정 곡", "모든 곡 개별") if ko else
                                      ("Whole video", "Single track", "Each track independently")):
            self.mode.setItemText(index, label)
        self.track.clear()
        self.track.addItem("곡 선택…" if ko else "Choose track…", "")
        for track, _, _ in controller.windows:
            mapping = source.keyframes.single_track_curves if source and source.keyframes.mode == "single_track" else source.keyframes.per_track_curves if source else {}
            self.track.addItem(("◆ " if any(mapping.get(track.id, {}).values()) else "") + track.title, track.id)
        self.track.setCurrentIndex(max(0, self.track.findData(controller.track_id)))
        if source:
            self.mode.setCurrentIndex(self.mode.findData(source.keyframes.mode))
            self.enabled.setChecked(source.keyframes.enabled)
        local = bool(source and source.keyframes.mode != "global")
        for widget in (self.track, self.previous_track, self.next_track):
            widget.setVisible(local)
        for widget in (self.mode, self.enabled):
            widget.setEnabled(bool(source and not source.locked and len(controller.store.selected_ids) == 1))
        self.enabled.setText("활성" if ko else "Enabled")
        self.expanded.setText("속성 펼치기" if ko else "Expand properties")
        self.snap.setText("스냅" if ko else "Snap")
        for widget, labels in ((self.fit, ("맞춤", "Fit")), (self.add, ("추가", "Add")), (self.apply, ("적용", "Apply")),
                               (self.delete, ("삭제", "Delete")), (self.copy, ("복사", "Copy")),
                               (self.paste, ("붙여넣기", "Paste")), (self.clear, ("초기화", "Clear"))):
            widget.setText(labels[0 if ko else 1])
        for index, spec in enumerate(PROPERTIES.values()):
            self.property.setItemText(index, spec.labels[0 if ko else 1])
        for index, label in enumerate(("유지", "선형", "서서히 시작", "서서히 종료", "부드럽게") if ko else
                                      ("Hold", "Linear", "Ease in", "Ease out", "Ease in out")):
            self.interpolation.setItemText(index, label)
        self.hint.setText("드래그로 시간 이동 · Ctrl/드래그로 다중 선택 · Ctrl+C/V · Delete · ←/→ 프레임 이동 · Alt+←/→ 키프레임 이동 · 자동 OFF는 기본값 수정"
                          if ko else "Drag time · Ctrl/rubber band selects · Ctrl+C/V · Delete · Arrows nudge frames · Alt+arrows navigate · Auto OFF edits base values")
        for widget, label in ((self.time, "시간 / Time"), (self.value, "값 / Value"), (self.property, "속성 / Property"),
                              (self.interpolation, "구간 보간 / Segment interpolation"), (self.mode, "적용 시간 기준 / Time basis"),
                              (self.track, "편집할 곡 / Track"), (self.source_combo, "편집할 요소 / Source")):
            widget.setAccessibleName(label)
            widget.setToolTip(label)
        for widget in (self.add, self.paste, self.clear):
            widget.setEnabled(controller.editable)
        self.fill_value()
        self.time.setValue(controller.time)
        last = max((p.time for points in controller.curves().values() for p in points), default=0)
        width = max(self.view.viewport().width() - 4, self.time_x(max(controller.duration, last)) + 40)
        height = self.ROW * (len(PROPERTIES) + 2) if self.expanded.isChecked() else 60
        self.scene.setSceneRect(0, 0, width, height)
        step = max(.01, 10 ** ceil(log10(max(.001, 65 / self.zoom))))
        for i in range(min(2000, int((width - self.LEFT) / self.zoom / step) + 1)):
            seconds = i * step
            x = self.time_x(seconds)
            self.scene.addLine(x, 24, x, height, QPen(QColor(COLORS["border"])))
            label = self.scene.addText(f"{int(seconds // 60):02d}:{seconds % 60:05.2f}")
            label.setDefaultTextColor(QColor(COLORS["muted"]))
            label.setPos(x, 0)
        if self.expanded.isChecked():
            for row, (name, spec) in enumerate(PROPERTIES.items()):
                label = self.scene.addText(spec.labels[0 if ko else 1])
                label.setDefaultTextColor(QColor(COLORS["text"]))
                label.setPos(0, self.row_y(row) - 12)
                for point in controller.curves().get(name, []):
                    diamond = Diamond(self, name, point, row)
                    if controller.editable:
                        self.scene.addItem(diamond)
                        diamond.setSelected((name, point.time) in self.selected)
                    else:
                        self.scene.addItem(diamond)
                        diamond.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.playhead_line = self.scene.addLine(0, 24, 0, height, QPen(QColor(COLORS["accent"]), 1.5))
        self.playhead_line.setPos(self.time_x(controller.time), 0)
        self._refreshing = False
        self.selection_changed()

    def refresh_position(self, *_):
        if self._refreshing or self.dragging:
            return
        if self.playhead_line is not None:
            self.playhead_line.setPos(self.time_x(self.controller.time), 0)
        if not self.selection():
            self.time.setValue(self.controller.time)
            self.fill_value()
