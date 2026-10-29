"""Explicit lossy-conversion choice and lossless cue deletion for TTML."""

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from app.services.lyrics_service import LyricsService


def needs_ttml_protection(cues, path=""):
    return Path(path).suffix.lower() == ".ttml" or any(
        cue.get("format") == "ttml" or bool(cue.get("words")) for cue in cues)


def line_cues(cues):
    """Explicitly discard word timing and rich metadata for ordinary LRC editing."""
    return [{"start": cue["start"], "end": cue["end"], "text": cue["text"]} for cue in cues]


def choose_ttml_edit_mode(cues, path, translator, parent):
    if not needs_ttml_protection(cues, path):
        return "edit"
    ko = translator.is_korean
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("TTML 가사 편집 방식" if ko else "Choose how to edit TTML lyrics")
    box.setText("이 가사에는 TTML 또는 단어별 시간 정보가 있습니다." if ko else
                "These lyrics contain TTML or word-level timing.")
    box.setInformativeText(
        "일반 편집기는 TTML을 직접 수정할 수 없습니다.\n\n"
        "• LRC로 변환: 단어·음절 시간과 TTML 전용 정보가 제외되고, 줄 종료 시간이 LRC 규칙으로 바뀝니다.\n"
        "• 줄만 수정하기: 선택한 줄을 삭제하는 것만 가능합니다. 남은 줄의 시간과 단어 정보는 유지됩니다.\n\n"
        "원본 파일은 변경하지 않습니다." if ko else
        "The regular editor cannot edit TTML directly.\n\n"
        "• Convert to LRC: removes word/syllable timing and TTML-specific data; line ends follow LRC rules.\n"
        "• Edit lines only: delete selected lines while preserving all remaining timing and word data.\n\n"
        "The original file is not modified.")
    convert = box.addButton("LRC로 변환" if ko else "Convert to LRC", QMessageBox.ButtonRole.AcceptRole)
    rows = box.addButton("줄만 수정하기" if ko else "Edit lines only", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(rows)
    box.exec()
    if box.clickedButton() is convert:
        return "convert"
    if box.clickedButton() is rows:
        return "delete"
    return "cancel"


class TtmlRowsDialog(QDialog):
    """Only deletion and undo are exposed; original cue dictionaries stay intact."""

    def __init__(self, cues, translator, parent=None, *, title="", target_track_id="", staged=False):
        super().__init__(parent)
        self.translator = translator
        self.staged = staged
        self._original = deepcopy(cues)
        self._rows = list(range(len(cues)))
        self._undo = []
        self.saved_paths = []
        self.add_saved_files_to_project = False
        self.result_track_id = target_track_id
        self.resize(880, 620)
        self.setMinimumSize(680, 440)
        layout = QVBoxLayout(self)
        self.heading = QLabel(title)
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setObjectName("panelTitle")
        layout.addWidget(self.heading)
        self.help_label = QLabel()
        self.help_label.setObjectName("infoCallout")
        self.help_label.setWordWrap(True)
        layout.addWidget(self.help_label)
        self.table = QTableWidget(0, 3)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.setAccessibleName("가사 줄 선택" if translator.is_korean else "Select lyric lines")
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.delete_button, self.undo_button = QPushButton(), QPushButton()
        self.select_all_button, self.clear_selection_button = QPushButton(), QPushButton()
        self.select_all_button.clicked.connect(self.table.selectAll)
        self.clear_selection_button.clicked.connect(self._clear_selection)
        self.count_label = QLabel()
        actions.addWidget(self.delete_button)
        actions.addWidget(self.undo_button)
        actions.addWidget(self.select_all_button)
        actions.addWidget(self.clear_selection_button)
        actions.addStretch(1)
        actions.addWidget(self.count_label)
        layout.insertLayout(layout.indexOf(self.table), actions)
        self.status_label = QLabel()
        self.status_label.setObjectName("mutedLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.commit_hint = QLabel()
        self.commit_hint.setObjectName("mutedLabel")
        self.commit_hint.setWordWrap(True)
        layout.addWidget(self.commit_hint)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.delete_button.clicked.connect(self._delete_selected)
        self.undo_button.clicked.connect(self._undo_delete)
        self.table.itemSelectionChanged.connect(self._sync_actions)
        for sequence, action in (("Delete", self._delete_selected), ("Ctrl+Z", self._undo_delete)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(action)
        for button in (self.delete_button, self.undo_button, self.select_all_button,
                       self.clear_selection_button, *self.buttons.buttons()):
            button.setAutoDefault(False)
            button.setDefault(False)
        finish_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        finish_button.setProperty("primary", True)
        finish_button.style().unpolish(finish_button)
        finish_button.style().polish(finish_button)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()
        self._refresh()

    def timed_cues(self):
        return deepcopy([self._original[index] for index in self._rows])

    def _refresh(self):
        scroll = self.table.verticalScrollBar().value()
        self.table.blockSignals(True)
        self._clear_selection()
        self.table.clearContents()
        self.table.setRowCount(len(self._rows))
        self.table.setVerticalHeaderLabels([str(index + 1) for index in self._rows])
        for row, index in enumerate(self._rows):
            cue = self._original[index]
            for column, text in enumerate((LyricsService.lrc_timestamp(float(cue['start'])), LyricsService.lrc_timestamp(float(cue['end'])),
                                           LyricsService.decode_line_breaks(cue.get("text", "")))):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.table.resizeRowsToContents()
        self._clear_selection()
        self.table.verticalScrollBar().setValue(scroll)
        self.table.blockSignals(False)
        self._sync_actions()

    def _clear_selection(self):
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)

    def _sync_actions(self):
        self.delete_button.setEnabled(bool(self.table.selectionModel().selectedRows()))
        self.undo_button.setEnabled(bool(self._undo))
        removed = len(self._original) - len(self._rows)
        self.count_label.setText(f"남은 줄 {len(self._rows)}개 · 삭제 {removed}개" if self.translator.is_korean else
                                 f"{len(self._rows)} remaining · {removed} deleted")
        selected = len(self.table.selectionModel().selectedRows())
        self.clear_selection_button.setEnabled(bool(selected))
        self.select_all_button.setEnabled(bool(self._rows))
        self.delete_button.setText(f"선택한 {selected}줄 삭제" if self.translator.is_korean else f"Delete {selected} selected lines")

    def _delete_selected(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()}, reverse=True)
        if not rows:
            return
        ko = self.translator.is_korean
        if QMessageBox.question(self, "줄 삭제" if ko else "Delete lines",
                f"선택한 {len(rows)}개 줄을 삭제할까요? 원본 파일은 유지됩니다." if ko else
                f"Delete {len(rows)} selected lines? The original file will be retained.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self._undo.append(list(self._rows))
        for row in rows:
            self._rows.pop(row)
        self._refresh()
        self.status_label.setText(
            f"{len(rows)}줄을 삭제했습니다. 다음 줄을 삭제하려면 다시 선택하세요. Ctrl+Z로 되돌릴 수 있습니다."
            if ko else f"Deleted {len(rows)} lines. Select lines again to delete more. Ctrl+Z undoes deletion.")

    def _undo_delete(self):
        if self._undo:
            self._rows = self._undo.pop()
            self._refresh()
            self.status_label.setText("마지막 삭제를 되돌렸습니다. 선택은 초기화되었습니다." if self.translator.is_korean else
                                      "Undid the last deletion. Selection cleared.")

    def retranslate(self):
        ko = self.translator.is_korean
        self.setWindowTitle("가사/자막 편집기 · 줄 삭제 전용" if ko else "Lyrics/Subtitle Editor · Delete lines only")
        self.help_label.setText("왼쪽 번호는 원본 줄 번호입니다. Ctrl·Shift로 여러 줄을 선택해 삭제하세요. 남은 줄의 글자·타이밍·단어 정보는 그대로 유지됩니다."
                               if ko else "Left numbers refer to the original lines. Use Ctrl/Shift to select multiple lines. Remaining text, timing and word data are preserved.")
        self.table.setHorizontalHeaderLabels(["시작", "종료", "가사 / 자막"] if ko else ["Start", "End", "Lyrics / subtitles"])
        self.select_all_button.setText("전체 선택" if ko else "Select all")
        self.clear_selection_button.setText("선택 해제" if ko else "Clear selection")
        self.commit_hint.setText(
            ("완료하면 곡 정보/설정으로 돌아갑니다. 해당 창의 ‘적용’을 눌러야 확정됩니다. 취소·닫기는 이 창의 편집을 버립니다."
             if self.staged else "‘편집 완료’를 누르면 변경을 반영합니다. 취소·닫기는 이 창의 편집을 버립니다.") if ko else
            ("Finish returns to Track settings. Click Apply there to confirm. Cancel or close discards this edit."
             if self.staged else "Finish applies changes. Cancel or close discards this edit."))
        self.delete_button.setText("선택한 줄 삭제" if ko else "Delete selected lines")
        self.undo_button.setText("삭제 취소" if ko else "Undo deletion")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("편집 완료" if ko else "Finish editing")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("변경 취소" if ko else "Discard changes")
        self._sync_actions()
