"""Read-only timed lyrics with optional lossless row selection."""
from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem
from app.services.lyrics_service import LyricsService


class LyricsCueTable(QTableWidget):
    def __init__(self, *, selectable=True):
        super().__init__(0, 4 if selectable else 3)
        self.selectable = selectable
        self.cues = []
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().hide()
        self.setSortingEnabled(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(self.columnCount()-1, QHeaderView.ResizeMode.Stretch)

    def set_cues(self, cues, korean=True):
        self.cues = deepcopy(cues)
        self.blockSignals(True)
        headers = (["적용" if korean else "Use"] if self.selectable else []) + (
            ["시작", "종료", "가사"] if korean else ["Start", "End", "Lyrics"])
        self.setHorizontalHeaderLabels(headers)
        self.setRowCount(len(cues))
        offset = int(self.selectable)
        for row, cue in enumerate(cues):
            if self.selectable:
                check = QTableWidgetItem()
                check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(Qt.CheckState.Checked)
                self.setItem(row, 0, check)
            for column, key in enumerate(("start", "end", "text"), offset):
                value = (LyricsService.decode_line_breaks(cue.get(key, "")) if key == "text" else
                         LyricsService.lrc_timestamp(float(cue[key])) if key in cue else "—")
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.setItem(row, column, item)
        self.resizeRowsToContents()
        self.blockSignals(False)

    def selected_cues(self):
        return deepcopy([cue for row, cue in enumerate(self.cues) if not self.selectable or
                         self.item(row, 0).checkState() == Qt.CheckState.Checked])

    def select_all_rows(self, checked):
        self.blockSignals(True)
        for row in range(self.rowCount()):
            self.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.blockSignals(False)
        if self.rowCount():
            self.itemChanged.emit(self.item(0, 0))

    def toPlainText(self):
        return "\n".join(str(cue.get("text", "")) for cue in self.selected_cues())

    def clear(self):
        self.cues = []
        self.setRowCount(0)
