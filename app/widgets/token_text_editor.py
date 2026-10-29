"""IDE-style editors for Music Motion Studio dynamic text tokens."""

from __future__ import annotations

import re
from typing import Protocol

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.preview.text_template import TEXT_TEMPLATE_TOKEN_NAMES
from app.utils.i18n import Translator


def _token_description(translator: Translator, name: str) -> str:
    # Keep literal calls visible to the language-pack generator so community
    # packs can translate the detail panel without changing Python code.
    descriptions = {
        "title": translator.literal("Current track title", "현재 곡의 제목"),
        "artist": translator.literal("Current track artist", "현재 곡의 아티스트"),
        "album": translator.literal("Current track album", "현재 곡의 앨범"),
        "track": translator.literal("Current track number", "현재 곡 번호"),
        "track_total": translator.literal("Total number of tracks", "플레이리스트의 전체 곡 수"),
        "filename": translator.literal("Current audio filename", "현재 오디오 파일 이름"),
        "current_time": translator.literal(
            "Current track time (legacy alias)", "현재 곡의 재생 시간 (이전 이름)",
        ),
        "total_time": translator.literal(
            "Current track duration (legacy alias)", "현재 곡의 전체 길이 (이전 이름)",
        ),
        "track_current_time": translator.literal(
            "Elapsed time in the current track", "현재 곡의 재생 시간",
        ),
        "track_total_time": translator.literal(
            "Duration of the current track", "현재 곡의 전체 길이",
        ),
        "video_current_time": translator.literal(
            "Elapsed time in the complete video", "전체 영상의 현재 재생 시간",
        ),
        "video_total_time": translator.literal(
            "Duration of the complete video", "전체 영상의 길이",
        ),
    }
    return descriptions[name]


class _TokenEditor(Protocol):
    translator: Translator

    def _editor_text(self) -> str: ...
    def _editor_cursor_position(self) -> int: ...
    def _set_editor_cursor_position(self, position: int) -> None: ...
    def _replace_editor_range(self, start: int, end: int, value: str) -> None: ...
    def cursorRect(self): ...


class TokenCompletionPopup(QFrame):
    """Non-focus-stealing completion list with a companion detail panel."""

    token_activated = Signal(str)

    def __init__(self, editor: QWidget, translator: Translator) -> None:
        super().__init__(editor, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.translator = translator
        self.setObjectName("tokenCompletionPopup")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(220)
        self.setMaximumWidth(320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.list = QListWidget()
        self.list.setObjectName("tokenCompletionList")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMouseTracking(True)
        self.list.currentItemChanged.connect(self._show_description)
        self.list.itemClicked.connect(lambda item: self.token_activated.emit(item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(self.list)

        self.description_popup = QFrame(
            editor, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self.description_popup.setObjectName("tokenDescriptionPopup")
        self.description_popup.setFrameShape(QFrame.Shape.StyledPanel)
        detail_layout = QVBoxLayout(self.description_popup)
        detail_layout.setContentsMargins(10, 8, 10, 8)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setMinimumWidth(220)
        self.description.setMaximumWidth(300)
        detail_layout.addWidget(self.description)

    def set_matches(self, names: list[str]) -> None:
        current = self.current_token()
        existing = [
            str(self.list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.list.count())
        ]
        if existing == names:
            if self.list.currentRow() < 0 and self.list.count():
                self.list.setCurrentRow(0)
            else:
                self._show_description(self.list.currentItem(), None)
            return
        self.list.clear()
        for name in names:
            item = QListWidgetItem(f"%{name}%")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list.addItem(item)
        if self.list.count():
            matching_rows = [
                row for row in range(self.list.count())
                if self.list.item(row).data(Qt.ItemDataRole.UserRole) == current
            ]
            self.list.setCurrentRow(matching_rows[0] if matching_rows else 0)
        self.list.setFixedHeight(min(260, max(38, self.list.sizeHintForRow(0) * self.list.count() + 10)))

    def current_token(self) -> str | None:
        item = self.list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None

    def move_selection(self, offset: int) -> None:
        count = self.list.count()
        if not count:
            return
        self.list.setCurrentRow((self.list.currentRow() + offset) % count)

    def show_below(self, editor: QWidget) -> None:
        caret = editor.cursorRect()
        position = editor.mapToGlobal(caret.bottomLeft() + QPoint(0, 4))
        self.adjustSize()
        self.move(position)
        self.show()
        self.raise_()
        self._position_description()

    def hide_popups(self) -> None:
        self.hide()
        self.description_popup.hide()

    def _show_description(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            self.description_popup.hide()
            return
        name = str(current.data(Qt.ItemDataRole.UserRole))
        detail = _token_description(self.translator, name)
        self.description.setText(f"<b>%{name}%</b><br>{detail}")
        if self.isVisible():
            self._position_description()
            self.description_popup.show()
            self.description_popup.raise_()

    def _position_description(self) -> None:
        self.description_popup.adjustSize()
        self.description_popup.move(self.frameGeometry().topRight() + QPoint(6, 0))
        if self.list.currentItem() is not None:
            self.description_popup.show()


class _TokenCompletionMixin:
    """Shared pairing, filtering, navigation, and insertion behavior."""

    _TOKEN_QUERY = re.compile(r"^[a-z_]*$", re.IGNORECASE)

    def _init_token_completion(self, translator: Translator) -> None:
        self.translator = translator
        self.token_popup = TokenCompletionPopup(self, translator)
        self.token_popup.token_activated.connect(self._insert_selected_token)
        self._completion_range: tuple[int, int] | None = None
        translator.language_changed.connect(self._refresh_completion)

    def _handle_token_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        if self.token_popup.isVisible():
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self.token_popup.move_selection(1 if key == Qt.Key.Key_Down else -1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Right, Qt.Key.Key_Tab):
                self._insert_selected_token()
                return True
            if key == Qt.Key.Key_Escape:
                self.token_popup.hide_popups()
                return True
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete) and not self._editor_selected_text():
            text = self._editor_text()
            position = self._editor_cursor_position()
            if 0 < position < len(text) and text[position - 1:position + 1] == "%%":
                self._replace_editor_range(position - 1, position + 1, "")
                self._set_editor_cursor_position(position - 1)
                self.token_popup.hide_popups()
                self._completion_range = None
                return True
        if event.text() == "%" and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            self._insert_percent_pair()
            self._refresh_completion()
            return True
        return False

    def _insert_percent_pair(self) -> None:
        text = self._editor_text()
        position = self._editor_cursor_position()
        selected = self._editor_selected_text()
        if selected:
            start, end = self._editor_selection_range()
            self._replace_editor_range(start, end, f"%{selected}%")
            self._set_editor_cursor_position(end + 2)
        elif position < len(text) and text[position] == "%":
            self._set_editor_cursor_position(position + 1)
        else:
            self._replace_editor_range(position, position, "%%")
            self._set_editor_cursor_position(position + 1)

    def _refresh_completion(self) -> None:
        text = self._editor_text()
        position = self._editor_cursor_position()
        start = text.rfind("%", 0, position)
        if start < 0:
            self.token_popup.hide_popups()
            self._completion_range = None
            return
        query = text[start + 1:position]
        if not self._TOKEN_QUERY.fullmatch(query):
            self.token_popup.hide_popups()
            self._completion_range = None
            return
        # A percent before the candidate opener means this one closes an
        # already complete token instead of starting a new candidate.
        if text[:start].count("%") % 2:
            self.token_popup.hide_popups()
            self._completion_range = None
            return
        end_marker = text.find("%", position)
        end = end_marker + 1 if end_marker >= 0 else position
        matches = [name for name in TEXT_TEMPLATE_TOKEN_NAMES if name.startswith(query.lower())]
        if not matches:
            self.token_popup.hide_popups()
            self._completion_range = None
            return
        self._completion_range = (start, end)
        self.token_popup.set_matches(matches)
        self.token_popup.show_below(self)

    def _insert_selected_token(self, token: str | None = None) -> None:
        name = token or self.token_popup.current_token()
        if not name or self._completion_range is None:
            return
        start, end = self._completion_range
        value = f"%{name}%"
        self._replace_editor_range(start, end, value)
        self._set_editor_cursor_position(start + len(value))
        self.token_popup.hide_popups()
        self._completion_range = None
        self.setFocus()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.token_popup.hide_popups()
        super().focusOutEvent(event)


class TokenLineEdit(_TokenCompletionMixin, QLineEdit):
    """Single-line token editor used by the Inspector."""

    pairedTextEdited = Signal(str)

    def __init__(self, translator: Translator, parent: QWidget | None = None) -> None:
        QLineEdit.__init__(self, parent)
        self._init_token_completion(translator)
        self.textEdited.connect(lambda _text: self._refresh_completion())
        self.cursorPositionChanged.connect(lambda _old, _new: self._refresh_completion())

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._handle_token_key(event):
            return
        super().keyPressEvent(event)
        self._refresh_completion()

    def _editor_text(self) -> str:
        return self.text()

    def _editor_cursor_position(self) -> int:
        return self.cursorPosition()

    def _set_editor_cursor_position(self, position: int) -> None:
        self.setCursorPosition(position)

    def _editor_selected_text(self) -> str:
        return self.selectedText()

    def _editor_selection_range(self) -> tuple[int, int]:
        start = self.selectionStart()
        return start, start + len(self.selectedText())

    def _replace_editor_range(self, start: int, end: int, value: str) -> None:
        self.setSelection(start, end - start)
        self.insert(value)
        self.pairedTextEdited.emit(self.text())


class TokenPlainTextEdit(_TokenCompletionMixin, QPlainTextEdit):
    """Multiline token editor used by the expanded editing dialog."""

    def __init__(self, translator: Translator, parent: QWidget | None = None) -> None:
        QPlainTextEdit.__init__(self, parent)
        self._init_token_completion(translator)
        self.textChanged.connect(self._refresh_completion)
        self.cursorPositionChanged.connect(self._refresh_completion)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._handle_token_key(event):
            return
        super().keyPressEvent(event)
        self._refresh_completion()

    def _editor_text(self) -> str:
        return self.toPlainText()

    def _editor_cursor_position(self) -> int:
        return self.textCursor().position()

    def _set_editor_cursor_position(self, position: int) -> None:
        cursor = self.textCursor()
        cursor.setPosition(position)
        self.setTextCursor(cursor)

    def _editor_selected_text(self) -> str:
        return self.textCursor().selectedText().replace("\u2029", "\n")

    def _editor_selection_range(self) -> tuple[int, int]:
        cursor = self.textCursor()
        return cursor.selectionStart(), cursor.selectionEnd()

    def _replace_editor_range(self, start: int, end: int, value: str) -> None:
        cursor = self.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(value)
        self.setTextCursor(cursor)
