"""Readable keyboard shortcut reference dialog."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.utils.i18n import Translator


class ShortcutsDialog(QDialog):
    """Present editor shortcuts in a compact, grouped reference table."""

    def __init__(self, translator: Translator, parent: object | None = None) -> None:
        super().__init__(parent)
        self.translator = translator
        self.setMinimumSize(650, 560)
        self.intro = QLabel()
        self.intro.setObjectName("mutedLabel")
        self.intro.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.table.setObjectName("shortcutTable")
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons = buttons
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(self.intro)
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()

    def retranslate(self) -> None:
        """Populate the reference table in the selected application language."""
        korean = self.translator.is_korean
        self.setWindowTitle("단축키 안내" if korean else "Keyboard shortcuts")
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText(
            "닫기" if korean else "Close"
        )
        self.intro.setText(
            "패널 단축키는 메인 창 전체에서 동작합니다. 캔버스 편집 단축키는 캔버스에 포커스가 있을 때 적용되며, 텍스트 입력 중에는 일반 입력을 우선합니다."
            if korean else
            "Panel shortcuts work throughout the main window. Canvas editing shortcuts apply while the Canvas has focus, and text inputs keep their normal behavior."
        )
        self.table.setHorizontalHeaderLabels(["키" if korean else "Key", "동작" if korean else "Action"])
        groups = [
            ("파일" if korean else "File", [
                ("Ctrl+N", "새 프로젝트 또는 최근 프로젝트 시작" if korean else "Start a new or recent project"),
                ("Ctrl+O", "프로젝트 열기" if korean else "Open a project"),
                ("Ctrl+S / Ctrl+Shift+S", "저장 / 다른 이름으로 저장" if korean else "Save / save as"),
                ("Ctrl+E", "영상 내보내기" if korean else "Export video"),
                ("F1", "도움말" if korean else "Help"),
            ]),
            ("작업 공간" if korean else "Workspace", [
                ("Ctrl+Alt+L", "왼쪽 요소·콘텐츠·레이어 패널 표시 전환" if korean else "Toggle the left Sources, Content, and Layers panel"),
                ("Ctrl+Alt+R", "오른쪽 속성 패널 표시 전환" if korean else "Toggle the right properties panel"),
                ("Ctrl+Alt+B", "하단 작업 패널 표시 전환" if korean else "Toggle the bottom workspace"),
                ("Ctrl+Alt+1 / 2 / 3", "플레이리스트 / 타임라인 / 미리보기 열기" if korean else "Open Playlist / Timeline / Preview"),
            ]),
            ("플레이리스트" if korean else "Playlist", [
                ("Enter", "선택한 곡 정보/설정 열기" if korean else "Open the selected track's information/settings"),
                ("Space", "선택한 곡을 내보내기에 포함 / 제외" if korean else "Include / exclude the selected tracks in export"),
                ("Delete", "선택한 곡 삭제 (Ctrl+Z로 되돌리기)" if korean else "Remove the selected tracks (Ctrl+Z restores them)"),
            ]),
            ("기본 편집" if korean else "Essential editing", [
                ("Ctrl+X / Ctrl+C / Ctrl+V", "잘라내기 / 복사 / 붙여넣기" if korean else "Cut / copy / paste selected sources"),
                ("Ctrl+D", "선택 요소 복제" if korean else "Duplicate selected sources"),
                ("Ctrl+A", "표시 중인 요소 전체 선택" if korean else "Select all visible sources"),
                ("Esc", "선택 해제" if korean else "Clear selection"),
                ("Delete / Backspace", "선택 요소 삭제" if korean else "Delete selected sources"),
                ("F2", "선택 요소 이름 편집" if korean else "Rename the selected source"),
                ("Ctrl+Z / Ctrl+Shift+Z", "실행 취소 / 다시 실행" if korean else "Undo / redo"),
            ]),
            ("이동 및 정렬" if korean else "Move and align", [
                ("방향키" if korean else "Arrow", "1px 미세 이동" if korean else "Nudge 1px"),
                ("Shift+방향키" if korean else "Shift+Arrow", "10px 이동" if korean else "Nudge 10px"),
                ("Ctrl+Shift+H / V", "가로 / 세로 중앙 정렬" if korean else "Center horizontally / vertically"),
                ("Ctrl+] / Ctrl+[", "맨 앞으로 / 맨 뒤로" if korean else "Bring to front / send to back"),
            ]),
            ("캔버스 및 레이어" if korean else "Canvas and layers", [
                ("Space+드래그" if korean else "Space+drag", "캔버스 시점 이동" if korean else "Pan Canvas"),
                ("Ctrl+휠" if korean else "Ctrl+wheel", "커서 기준 확대 / 축소" if korean else "Zoom in / out around the cursor"),
                ("더블클릭" if korean else "Double-click", "텍스트·가사 요소 편집" if korean else "Edit a text or lyrics source"),
                ("Ctrl+드래그" if korean else "Ctrl+drag", "드롭 위치에 복제" if korean else "Duplicate at the drop point"),
                ("Alt+크기 조절" if korean else "Alt+resize", "현재 요소 비율 유지" if korean else "Preserve the current source ratio"),
                ("Alt+Shift+크기 조절" if korean else "Alt+Shift+resize", "1:1 비율로 전환해 크기 조절" if korean else "Switch to and resize at a 1:1 ratio"),
                ("Ctrl+G / Ctrl+Shift+G", "그룹화 / 그룹 해제" if korean else "Group / ungroup"),
                ("Ctrl+L", "잠금 토글" if korean else "Toggle lock"),
                ("Home / F / Ctrl+0", "캔버스 전체 맞춤" if korean else "Fit the complete Canvas"),
                ("Ctrl++ / Ctrl+-", "캔버스 확대 / 축소" if korean else "Zoom Canvas in / out"),
            ]),
            ("사용자 지정 키프레임 편집 창" if korean else "Custom keyframe editor window", [
                ("Space / K / Delete", "재생·일시 정지 / 점 추가 / 점 삭제" if korean else "Play/pause / add key / delete key"),
                ("1–5 / ↑ / ↓", "X·Y·크기·회전·투명도 트랙 선택" if korean else "Select X/Y/scale/rotation/opacity track"),
                ("← / → · Shift+← / →", "0.01초 / 0.1초 탐색" if korean else "Seek 0.01 s / 0.1 s"),
                ("Ctrl+← / →", "선택 트랙의 이전 / 다음 키프레임" if korean else "Previous / next key in selected track"),
                ("B / P / E", "구간 곡선 / 프리셋 / 트랙 켜기·끄기" if korean else "Interval curve / presets / enable track"),
                ("휠 · Space+드래그" if korean else "Wheel · Space+drag", "미리보기 확대·축소 / 화면 이동" if korean else "Zoom preview / pan"),
                ("F1 / Shift+F1", "사용 도움말 / 편집 창의 전체 단축키" if korean else "Usage help / full editor shortcuts"),
            ]),
        ]
        self.table.setRowCount(0)
        row = 0
        for group, entries in groups:
            self.table.insertRow(row)
            heading = QTableWidgetItem(group)
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            heading.setBackground(self.palette().alternateBase())
            self.table.setItem(row, 0, heading)
            self.table.setSpan(row, 0, 1, 2)
            self.table.setRowHeight(row, 28)
            row += 1
            for shortcut, description in entries:
                self.table.insertRow(row)
                key = QTableWidgetItem(shortcut)
                key.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                description_item = QTableWidgetItem(description)
                self.table.setItem(row, 0, key)
                self.table.setItem(row, 1, description_item)
                self.table.setRowHeight(row, 34)
                row += 1
