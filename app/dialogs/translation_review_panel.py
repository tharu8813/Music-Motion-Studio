"""Review editor embedded in the translation workspace; it owns no window."""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QWidget, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QPlainTextEdit, QPushButton, QStyledItemDelegate, QDialog, QApplication,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QCheckBox, QMessageBox,
)

from app.ui.design_system import COLORS
from app.services.lyrics_translation_service import TranslationConfig, TranslationError, apply_translations, approved_translation, translation_text
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
        self._diagnostics = []
        self.setObjectName("translationReviewPanel")
        review_layout = QVBoxLayout(self)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(7)
        self.review_title = QLabel(self.tr_text(
            "2. 번역 검토 및 수정", "2. Review and edit translations",
        ))
        self.review_title.setObjectName("panelTitle")
        review_layout.addWidget(self.review_title)
        self.review_summary = QLabel()
        self.review_summary.setObjectName("mutedLabel")
        self.review_summary.setWordWrap(True)
        review_layout.addWidget(self.review_summary)
        row_actions = QHBoxLayout()
        row_actions.setSpacing(7)
        self._row_action_buttons = []
        for ko, en, checked in (("전체 선택", "Select all", True), ("전체 해제", "Clear selection", False)):
            button = QPushButton(self.tr_text(ko, en))
            button.clicked.connect(lambda _clicked=False, value=checked: self._check_all(value))
            row_actions.addWidget(button)
            self._row_action_buttons.append(button)
        self.selection_label = QLabel()
        row_actions.addWidget(self.selection_label, 1)
        self.follow_check = QCheckBox(self.tr_text("진행 행 따라가기", "Follow active rows"))
        self.follow_check.setToolTip(self.tr_text("끄면 번역 중에도 현재 검토 위치를 유지합니다.",
                                                "When off, translation preserves your review position."))
        row_actions.addWidget(self.follow_check)
        self.inspect_button = QPushButton(self.tr_text("LLM 응답 확인…", "Inspect LLM responses…"))
        self.inspect_button.setToolTip(self.tr_text(
            "이 번역 실행에서 받은 로컬 LLM 원문 출력과 오류 원인을 표시합니다. 가사 내용이 포함될 수 있으며 파일로 자동 저장하지 않습니다.",
            "Inspect raw local LLM output and errors from this run. It may contain lyric text; it is never auto-saved."))
        self.inspect_button.setEnabled(False)
        self.inspect_button.clicked.connect(self.show_diagnostics)
        row_actions.addWidget(self.inspect_button)
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
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.setMinimumHeight(220)
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
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, max(48, self.table.rowHeight(row)))
        review_layout.addWidget(self.table, 1)
        selection_note = QLabel(self.tr_text(
            "체크한 줄만 적용하며, 체크하지 않은 줄은 그대로 유지됩니다. 곡 정보 창에서도 적용한 뒤 프로젝트를 저장하세요.",
            "Only checked rows are applied; others stay unchanged. Apply in track settings and save the project."))
        selection_note.setWordWrap(True)
        review_layout.addWidget(selection_note)
        edit_note = QLabel(self.tr_text("번역문 수정: 두 번 클릭  ·  줄바꿈: Enter  ·  완료: Ctrl+Enter",
            "Edit translation: double-click  ·  New line: Enter  ·  Done: Ctrl+Enter"))
        edit_note.setWordWrap(True)
        edit_note.setObjectName("mutedLabel")
        review_layout.addWidget(edit_note)
        self.attribution_label = QLabel('<a href="https://papago.naver.com/">Translated by Papago</a>')
        self.attribution_label.setOpenExternalLinks(True)
        self.attribution_label.setVisible(any(approved_translation(cue) and cue["translation"]["provider"] == "papago" for cue in cues))
        review_layout.addWidget(self.attribution_label)
        filters = QHBoxLayout()
        filters.setSpacing(7)
        filter_label = QLabel(self.tr_text("표시", "Show"))
        filter_label.setBuddy(self.table)
        filters.addWidget(filter_label)
        self.filter_combo = QComboBox()
        for ko, en, value in (("모든 행", "All rows", "all"), ("번역되지 않은 행", "Untranslated", "untranslated"),
                              ("실패·미완료 행", "Failed / incomplete", "failed"), ("수정한 행", "Edited", "edited")):
            self.filter_combo.addItem(self.tr_text(ko, en), value)
        self.filter_combo.setAccessibleName(self.tr_text("검토 행 필터", "Review row filter"))
        self.filter_combo.setMinimumContentsLength(10)
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
        review_layout.insertLayout(2, filters)
        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.discard_button = QPushButton(self.tr_text("초안 삭제…", "Delete draft…"))
        self.discard_button.clicked.connect(self.discard_draft)
        footer.addWidget(self.discard_button)
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

    def set_diagnostics(self, records):
        self._diagnostics = list(records)
        self.inspect_button.setEnabled(bool(self._diagnostics))

    def show_diagnostics(self):
        if not self._diagnostics:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr_text("로컬 LLM 응답 및 오류", "Local LLM responses and errors"))
        dialog.resize(840, 600)
        layout = QVBoxLayout(dialog)
        notice = QLabel(self.tr_text(
            "모델이 실제 반환한 텍스트입니다. 가사 등 개인 정보가 포함될 수 있으므로 공유 전에 확인하세요. 이 정보는 번역 초안이나 프로젝트 파일에 저장되지 않습니다.",
            "Raw text returned by the model. It may contain lyrics or private content: review before sharing. It is not stored in drafts or project files."))
        notice.setWordWrap(True)
        layout.addWidget(notice)
        entries = []
        for i, record in enumerate(self._diagnostics, 1):
            rows = ", ".join(str(row + 1) for row in record.get("rows", ()))
            title = self.tr_text(f"{i}. 모델 {record.get('model', '')} · 가사 {rows}번",
                f"{i}. Model {record.get('model', '')} · lyric rows {rows}")
            reason = self.tr_text("검증 결과: ", "Validation: ") + str(record.get("reason", ""))
            output = record.get("raw", "")
            if not output:
                output = self.tr_text("(응답 텍스트를 받지 못했습니다.)", "(No model response text received.)")
            elif record.get("truncated"):
                output += self.tr_text("\n(응답이 너무 길어 앞부분만 표시합니다.)",
                    "\n(Output exceeded the safety limit; only the beginning is shown.)")
            entries.append(f"{title}\n{reason}\n\n{output}")
        content = ("\n\n" + "=" * 56 + "\n\n").join(entries)
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        viewer.setPlainText(content)
        layout.addWidget(viewer, 1)
        actions = QHBoxLayout()
        copy_button = QPushButton(self.tr_text("진단 내용 복사", "Copy diagnostics"))
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(content))
        actions.addWidget(copy_button)
        actions.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        actions.addWidget(close)
        layout.addLayout(actions)
        dialog.exec()

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
                self.draft.dirty = True
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
            and bool(self.cues[row].get("translation"))
            and not translation_text(self.cues[row].get("text", ""), self.table.item(row, 3).text())
            for row in range(self.table.rowCount()))
        self.selection_label.setText(self.tr_text(f"{selected}줄 선택", f"{selected} rows selected"))
        translated = sum(bool(self.table.item(row, 3).text().strip()) for row in range(self.table.rowCount()))
        self.review_summary.setText(self.tr_text(
            f"원문 {len(self._editable_rows)}줄 · 번역 {translated}줄 · 선택한 번역 삭제 {removals}줄",
            f"{len(self._editable_rows)} original rows · {translated} translated · {removals} selected for removal"))
        self.apply_button.setText(self.tr_text(f"선택한 {selected}줄 적용", f"Apply {selected} selected rows"))
        self.apply_button.setToolTip(self.tr_text("곡 정보 창에 임시 반영합니다. 취소에 대비해 복구 초안은 보관합니다.",
                                                "Stage in track settings. A recovery draft is retained if you cancel."))
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
        removals = [i + 1 for i, (cue, text) in enumerate(zip(self.cues, texts))
                    if cue.get("translation") and text is not None and not translation_text(cue.get("text", ""), text)]
        if removals and QMessageBox.warning(self, self.tr_text("기존 번역 삭제 확인", "Confirm translation deletion"),
                self.tr_text(f"빈 번역문을 적용하면 기존 번역 {len(removals)}개가 삭제됩니다. 행: {', '.join(map(str, removals[:20]))}\n계속할까요? 복구 초안은 보관됩니다.",
                    f"Applying blank translations deletes {len(removals)} existing translations. Rows: {', '.join(map(str, removals[:20]))}\nContinue? A recovery draft will be retained."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        try:
            reviewed = []
            for index, (cue, text) in enumerate(zip(self.cues, texts)):
                metadata = cue.get("translation", {}) if approved_translation(cue) else {}
                config = self.draft.rows[index].config or TranslationConfig(metadata.get("provider", "manual"),
                    target=metadata.get("language", self._language_value(self.target_combo)), model=metadata.get("model", ""))
                reviewed.extend(apply_translations([cue], [text], config))
            self.capture_draft()
            self.draft.record_applied(reviewed)
            self.draft.save()
            self.selected_cues = reviewed
        except TranslationError as error:
            self.workflow.set_status(str(error))
            return
        self.applied.emit()

    def _set_review_busy(self, busy):
        for button in self._row_action_buttons:
            button.setEnabled(not busy)
        self.discard_button.setEnabled(not busy)
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
        scroll = self.table.verticalScrollBar().value()
        self.table.blockSignals(True)
        try:
            if event["stage"] == "working":
                for row in event["rows"]:
                    self.table.item(row, 4).setText(self.tr_text("번역 중…", "Translating…"))
                    for column in range(self.table.columnCount()):
                        self.table.item(row, column).setBackground(QColor(COLORS["info_surface"]))
                if event["rows"]:
                    if self.follow_check.isChecked():
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
            if not self.follow_check.isChecked():
                self.table.verticalScrollBar().setValue(scroll)

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
            if (row.text != self.table.item(index, 3).text()
                    or row.checked != (self.table.item(index, 0).checkState() == Qt.CheckState.Checked)):
                self.draft.dirty = True
            row.text = self.table.item(index, 3).text()
            row.checked = self.table.item(index, 0).checkState() == Qt.CheckState.Checked
            row.detail = self.table.item(index, 4).toolTip()
            if index in self._incomplete_rows:
                row.status = "incomplete"
            elif row.edited and row.status != "failed":
                row.status = "edited"
        self.draft.changed.emit()

    def restore_draft(self):
        scroll = self.table.verticalScrollBar().value()
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
        if not self.follow_check.isChecked():
            self.table.verticalScrollBar().setValue(scroll)

    def save_draft(self):
        self._commit_editor()
        self.capture_draft()
        try:
            saved = self.draft.save()
        except TranslationError as error:
            self.workflow.set_status(str(error))
            return
        if not self.worker:
            self.workflow.set_status(self.tr_text("복구 초안을 저장했습니다. 적용 후에도 명시적으로 삭제할 때까지 이 기기에 보관합니다.",
                "Recovery draft saved on this device until explicitly deleted.") if saved else
                self.tr_text("보관할 번역 변경사항이 없습니다.", "No translation changes to save."))

    def discard_draft(self):
        if self.worker:
            return
        if QMessageBox.warning(self, self.tr_text("번역 초안 삭제", "Delete translation draft"),
                self.tr_text("이 곡의 보관된 복구 초안과 현재 편집을 삭제하고, 창을 열었을 때의 번역으로 돌아갈까요? 저장된 곡과 프로젝트는 바뀌지 않습니다.",
                    "Delete this track's recovery drafts and current edits, returning to translations present when opened? Saved tracks and projects are unchanged."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.draft.discard()
        except TranslationError as error:
            self.workflow.set_status(str(error))
            return
        self.restore_draft()
        self.workflow.set_status(self.tr_text("보관된 초안을 삭제했습니다. 새로 편집하거나 번역하면 다시 자동 저장합니다.",
                                            "Drafts deleted. New edits or translations will be autosaved."))

