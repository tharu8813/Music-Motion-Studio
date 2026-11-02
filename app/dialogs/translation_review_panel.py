"""Review editor embedded in the translation workspace; it owns no window."""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QWidget, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QPlainTextEdit, QPushButton, QStyledItemDelegate,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from app.ui.design_system import COLORS
from app.services.lyrics_translation_service import TranslationConfig, TranslationError, apply_translations, approved_translation
from app.services.lyrics_translation_prompt import lyric_text


class _TranslationDelegate(QStyledItemDelegate):
    """Keep multi-line cue translations editable without flattening line breaks."""
    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        table = self.parent()
        width = max(20, table.columnWidth(index.column()) - 24)
        bounds = option.fontMetrics.boundingRect(QRect(0, 0, width, 100000),
            Qt.TextFlag.TextWordWrap, index.data(Qt.ItemDataRole.DisplayRole) or "")
        return QSize(hint.width(), max(hint.height(), bounds.height() + 14))

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setMinimumHeight(72)
        return editor

    def setEditorData(self, editor, index):
        editor.setPlainText(index.data(Qt.ItemDataRole.EditRole) or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)

    def eventFilter(self, editor, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.KeyPress and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
                return True
            return False
        return super().eventFilter(editor, event)


class TranslationReviewPanel(QWidget):
    close_requested = Signal()
    applied = Signal()
    retry_requested = Signal(object)
    style_requested = Signal()

    def __init__(self, cues, translator, draft, parent=None):
        super().__init__(parent)
        self.translator, self.draft, self.cues = translator, draft, deepcopy(cues)
        self.selected_cues = deepcopy(cues)
        self.workflow = None
        self.target_combo = None
        self._draft_config, self._review_ready = None, draft.has_result
        self._incomplete_rows = {i for i, row in enumerate(draft.rows) if row.status == "incomplete"}
        self._editable_rows = {i for i, cue in enumerate(cues) if lyric_text(cue)}
        self.setObjectName("translationReviewPanel")
        review_layout = QVBoxLayout(self)
        review_layout.setContentsMargins(0, 0, 0, 0)
        self.review_summary = QLabel()
        self.review_summary.setObjectName("mutedLabel")
        self.review_summary.setWordWrap(True)
        review_layout.addWidget(self.review_summary)
        row_actions = QHBoxLayout()
        self._row_action_buttons = []
        for ko, en, checked in (("전체 선택", "Select all", True), ("전체 해제", "Clear selection", False)):
            button = QPushButton(self.tr_text(ko, en))
            button.clicked.connect(lambda _clicked=False, value=checked: self._check_all(value))
            row_actions.addWidget(button)
            self._row_action_buttons.append(button)
        self.selection_label = QLabel()
        row_actions.addWidget(self.selection_label, 1)
        review_layout.addLayout(row_actions)
        self.table = QTableWidget(len(cues), 5)
        self.table.setItemDelegateForColumn(2, _TranslationDelegate(self.table))
        self.table.setItemDelegateForColumn(3, _TranslationDelegate(self.table))
        self.table.setToolTip(self.tr_text("번역문을 두 번 클릭해 수정합니다. Ctrl+Enter로 편집을 마칩니다.",
            "Double-click a translation to edit. Ctrl+Enter finishes editing."))
        self.table.setHorizontalHeaderLabels([self.tr_text("적용", "Apply"), self.tr_text("시작", "Start"),
            self.tr_text("원문", "Original"), self.tr_text("번역문", "Translation"),
            self.tr_text("상태", "Status")])
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAccessibleName(self.tr_text("원문과 번역 검토 표", "Original and translated lyrics review"))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().hide()
        self._row_resize_timer = QTimer(self)
        self._row_resize_timer.setSingleShot(True)
        self._row_resize_timer.setInterval(75)
        self._row_resize_timer.timeout.connect(self.table.resizeRowsToContents)
        self.table.horizontalHeader().sectionResized.connect(lambda *_args: self._row_resize_timer.start())
        for row, cue in enumerate(self.cues):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if row in self._editable_rows else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            seconds = max(0, float(cue.get('start', 0)))
            for column, text in ((1, f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"),
                                 (2, str(cue.get("text", "")).replace("\\n", "\n")),
                                 (3, approved_translation(cue)),
                                 (4, self.tr_text("저장됨", "Saved") if approved_translation(cue) else self.tr_text("대기", "Ready"))):
                item = QTableWidgetItem(text)
                if column != 3:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                self.table.setItem(row, column, item)
            if row not in self._editable_rows:
                check.setFlags(Qt.ItemFlag.NoItemFlags)
                self.table.item(row, 3).setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.item(row, 4).setText(self.tr_text("가사 없음", "No lyrics"))
        self.table.resizeRowsToContents()
        review_layout.addWidget(self.table, 1)
        selection_note = QLabel(self.tr_text(
            "체크하지 않은 줄은 기존 번역을 유지합니다. 빈 번역문을 체크해 적용하면 해당 번역을 삭제합니다.",
            "Unchecked rows keep existing translations. Applying a checked empty translation removes it."))
        selection_note.setWordWrap(True)
        review_layout.addWidget(selection_note)
        edit_note = QLabel(self.tr_text("번역문을 두 번 클릭해 수정 · 줄바꿈은 Enter · 편집 완료는 Ctrl+Enter",
            "Double-click to edit · Enter for a line break · Ctrl+Enter to finish"))
        edit_note.setWordWrap(True)
        edit_note.setObjectName("mutedLabel")
        review_layout.addWidget(edit_note)
        self.attribution_label = QLabel('<a href="https://papago.naver.com/">Translated by Papago</a>')
        self.attribution_label.setOpenExternalLinks(True)
        self.attribution_label.setVisible(any(approved_translation(cue) and cue["translation"]["provider"] == "papago" for cue in cues))
        review_layout.addWidget(self.attribution_label)
        filters = QHBoxLayout()
        self.filter_combo = QComboBox()
        for ko, en, value in (("모든 행", "All rows", "all"), ("번역되지 않은 행", "Untranslated", "untranslated"),
                              ("실패·미완료 행", "Failed / incomplete", "failed"), ("수정한 행", "Edited", "edited")):
            self.filter_combo.addItem(self.tr_text(ko, en), value)
        self.filter_combo.setAccessibleName(self.tr_text("검토 행 필터", "Review row filter"))
        filters.addWidget(self.filter_combo)
        self.retry_selected = QPushButton(self.tr_text("선택한 행 재번역", "Retranslate selected rows"))
        self.retry_selected.setToolTip(self.tr_text(
            "표에서 클릭·Ctrl·Shift로 강조한 행을 재번역합니다. 왼쪽 체크 상자는 적용 대상을 정합니다.",
            "Retranslate highlighted table rows (click, Ctrl or Shift). Checkboxes choose rows to apply."))
        self.retry_failed = QPushButton(self.tr_text("실패한 행 재번역", "Retry failed rows"))
        filters.addWidget(self.retry_selected)
        filters.addWidget(self.retry_failed)
        filters.addStretch()
        self.style_button = QPushButton(self.tr_text("번역문 스타일…", "Translation style…"))
        filters.addWidget(self.style_button)
        review_layout.insertLayout(1, filters)
        footer = QHBoxLayout()
        footer.addStretch()
        self.save_button = QPushButton(self.tr_text("초안 저장", "Save draft"))
        footer.addWidget(self.save_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setProperty("primary", True)
        self.buttons.rejected.connect(self.close_requested)
        self.apply_button.clicked.connect(self._apply)
        footer.addWidget(self.buttons)
        review_layout.addLayout(footer)
        self.table.itemChanged.connect(self._update_selection)
        self.filter_combo.currentIndexChanged.connect(self._filter)
        self.retry_selected.clicked.connect(lambda: self.retry_requested.emit(sorted({i.row() for i in self.table.selectedIndexes()
                                                                                    if not self.table.isRowHidden(i.row())})))
        self.retry_failed.clicked.connect(lambda: self.retry_requested.emit([i for i, row in enumerate(self.draft.rows)
                                                                           if row.status in {"failed", "incomplete"}]))
        self.style_button.clicked.connect(self.style_requested)
        self.save_button.clicked.connect(self.save_draft)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.activated.connect(self.save_draft)
        self.table.itemSelectionChanged.connect(self._update_retry_selection)
        self.restore_draft()
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    @property
    def worker(self):
        return self.workflow.worker if self.workflow else None

    @property
    def _operation(self):
        return self.workflow.operation if self.workflow else ""

    @staticmethod
    def _language_value(combo):
        index = combo.currentIndex()
        return combo.itemData(index) if index >= 0 and combo.currentText() == combo.itemText(index) else combo.currentText().strip()

    def tr_text(self, korean, english):
        return korean if self.translator.is_korean else english

    def _check_all(self, checked):
        self._commit_editor()
        self.table.blockSignals(True)
        for row in self._editable_rows:
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_selection()

    def _update_selection(self, *_args):
        if _args and isinstance(_args[0], QTableWidgetItem) and _args[0].column() == 3:
            row = _args[0].row()
            if row in self._editable_rows:
                if row in self._incomplete_rows and _args[0].text().strip():
                    self._incomplete_rows.discard(row)
                    self.table.item(row, 4).setText(self.tr_text("수정됨", "Edited"))
                    self.table.item(row, 4).setToolTip("")
                self.draft.rows[row].edited = True
                self.draft.rows[row].status = "edited"
                if self.draft.rows[row].config is None:
                    self.draft.rows[row].config = TranslationConfig("manual", target=self._language_value(self.target_combo))
                self.table.blockSignals(True)
                self.table.item(row, 4).setText(self.tr_text("수정됨", "Edited"))
                self.table.blockSignals(False)
                self._review_ready = True
                if self._draft_config is None:
                    self._draft_config = TranslationConfig("manual", target=self._language_value(self.target_combo))
                self.table.blockSignals(True)
                self.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
                self.table.blockSignals(False)
            self.table.resizeRowToContents(row)
        selected = sum(self.table.item(row, 0).checkState() == Qt.CheckState.Checked for row in range(self.table.rowCount()))
        removals = sum(self.table.item(row, 0).checkState() == Qt.CheckState.Checked
            and bool(self.cues[row].get("translation")) and not self.table.item(row, 3).text().strip()
            for row in range(self.table.rowCount()))
        self.selection_label.setText(self.tr_text(f"{selected}줄 선택", f"{selected} rows selected"))
        translated = sum(bool(self.table.item(row, 3).text().strip()) for row in range(self.table.rowCount()))
        self.review_summary.setText(self.tr_text(
            f"원문 {len(self._editable_rows)}줄 · 번역 {translated}줄 · 선택한 번역 삭제 {removals}줄",
            f"{len(self._editable_rows)} original rows · {translated} translated · {removals} selected for removal"))
        self.apply_button.setText(self.tr_text(f"선택한 {selected}줄 적용", f"Apply {selected} selected rows"))
        self.apply_button.setEnabled(self.worker is None and self._review_ready and selected > 0)

        self.capture_draft()
        self._filter()

    def _update_retry_selection(self):
        selected = {item.row() for item in self.table.selectedIndexes() if not self.table.isRowHidden(item.row())}
        self.retry_selected.setEnabled(self.worker is None and bool(selected & self._editable_rows))
        self.retry_failed.setEnabled(self.worker is None and any(row.status in {"failed", "incomplete"} for row in self.draft.rows))

    def _commit_editor(self):
        for editor in self.table.findChildren(QPlainTextEdit):
            if editor.isVisible():
                delegate = self.table.itemDelegateForColumn(3)
                delegate.commitData.emit(editor)
                delegate.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)

    def _apply(self):
        self._commit_editor()
        if not self._review_ready or self.worker is not None or self._draft_config is None:
            return
        if not any(self.table.item(row, 0).checkState() == Qt.CheckState.Checked for row in self._editable_rows):
            return
        texts = [self.table.item(row, 3).text() if self.table.item(row, 0).checkState() == Qt.CheckState.Checked else None
                 for row in range(self.table.rowCount())]
        try:
            reviewed = []
            for index, (cue, text) in enumerate(zip(self.cues, texts)):
                metadata = cue.get("translation", {}) if approved_translation(cue) else {}
                config = self.draft.rows[index].config or TranslationConfig(metadata.get("provider", "manual"),
                    target=metadata.get("language", self._language_value(self.target_combo)), model=metadata.get("model", ""))
                reviewed.extend(apply_translations([cue], [text], config))
            self.selected_cues = reviewed
        except TranslationError as error:
            self.workflow.set_status(str(error))
            return
        self.draft.clear()
        self.applied.emit()

    def _set_review_busy(self, busy):
        for button in self._row_action_buttons:
            button.setEnabled(not busy)
        if busy:
            self._edit_triggers = self.table.editTriggers()
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        else:
            self.table.setEditTriggers(self._edit_triggers)
        self.table.blockSignals(True)
        for row in self._editable_rows:
            item = self.table.item(row, 0)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable if busy else item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        self.table.blockSignals(False)

    def _activity(self, event):
        if self.worker is None or self._operation != "translate":
            return
        self.table.blockSignals(True)
        try:
            if event["stage"] == "working":
                for row in event["rows"]:
                    self.table.item(row, 4).setText(self.tr_text("번역 중…", "Translating…"))
                    for column in range(self.table.columnCount()):
                        self.table.item(row, column).setBackground(QColor(COLORS["info_surface"]))
                if event["rows"]:
                    self.table.scrollToItem(self.table.item(event["rows"][0], 2))
                    self.workflow.set_status(self.tr_text(
                        f"{event['rows'][0] + 1}~{event['rows'][-1] + 1}번째 가사를 번역하고 있습니다. 완료된 번역은 표에 바로 표시됩니다.",
                        f"Translating rows {event['rows'][0] + 1}–{event['rows'][-1] + 1}. Completed results appear in the table."))
            elif event["stage"] == "complete":
                row, text = event["row"], event["text"]
                self.table.item(row, 3).setText(text)
                if event.get("missing_lines"):
                    self._incomplete_rows.add(row)
                    self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
                    self.table.item(row, 4).setText(self.tr_text("번역 미완료", "Incomplete"))
                    lines = ", ".join(map(str, event["missing_lines"]))
                    self.table.item(row, 4).setToolTip(self.tr_text(
                        f"묶음 안 {lines}번째 줄에서 모델이 빈 결과를 반환했습니다. 모델의 언어 방향을 확인하거나 번역문을 직접 입력하세요. 이 행은 자동 선택하지 않습니다.",
                        f"The model returned no translation for line(s) {lines} in this cue. Check the model language pair or enter a translation. This row is not selected automatically."))
                else:
                    self.table.item(row, 4).setText(self.tr_text("완료", "Complete") if text else self.tr_text("번역 불필요", "Not needed"))
                for column in range(self.table.columnCount()):
                    self.table.item(row, column).setData(Qt.ItemDataRole.BackgroundRole, None)
                self.table.resizeRowToContents(row)
        finally:
            self.table.blockSignals(False)

    def _filter(self, *_args):
        mode = self.filter_combo.currentData()
        for index, row in enumerate(self.draft.rows):
            visible = (mode == "all" or mode == "untranslated" and not row.text.strip()
                       or mode == "failed" and row.status in {"failed", "incomplete"}
                       or mode == "edited" and row.edited)
            self.table.setRowHidden(index, not visible)
        self._update_retry_selection()

    def capture_draft(self):
        if self.worker:
            return
        self.draft.has_result = self._review_ready
        for index, row in enumerate(self.draft.rows):
            row.text = self.table.item(index, 3).text()
            row.checked = self.table.item(index, 0).checkState() == Qt.CheckState.Checked
            row.detail = self.table.item(index, 4).toolTip()
            if index in self._incomplete_rows:
                row.status = "incomplete"
            elif row.edited and row.status != "failed":
                row.status = "edited"
        self.draft.changed.emit()

    def restore_draft(self):
        labels = {"ready": ("대기", "Ready"), "saved": ("저장됨", "Saved"), "complete": ("완료", "Complete"),
                  "unneeded": ("번역 불필요", "Not needed"), "incomplete": ("번역 미완료", "Incomplete"),
                  "failed": ("번역 실패", "Failed"), "edited": ("수정됨", "Edited")}
        self.table.blockSignals(True)
        for index, row in enumerate(self.draft.rows):
            self.table.item(index, 3).setText(row.text)
            self.table.item(index, 0).setCheckState(Qt.CheckState.Checked if row.checked and index in self._editable_rows else Qt.CheckState.Unchecked)
            self.table.item(index, 4).setText(self.tr_text(*labels[row.status]))
            self.table.item(index, 4).setToolTip(row.detail)
            for column in range(self.table.columnCount()):
                self.table.item(index, column).setData(Qt.ItemDataRole.BackgroundRole, None)
        self.table.blockSignals(False)
        self._draft_config = next((row.config for row in self.draft.rows if row.config), TranslationConfig("manual"))
        self._review_ready = self.draft.has_result
        self._incomplete_rows = {i for i, row in enumerate(self.draft.rows) if row.status == "incomplete"}
        self._update_selection()
        self.table.resizeRowsToContents()

    def save_draft(self):
        self._commit_editor()
        self.capture_draft()
        self.draft.save()
        if not self.worker:
            self.workflow.set_status(self.tr_text("초안을 저장했습니다. 다시 열면 이어서 검토할 수 있습니다.",
                                                 "Draft saved. Reopen to continue reviewing."))

