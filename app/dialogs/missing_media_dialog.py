"""Dialog that lets users relink missing project media before loading."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.services.project_media_service import MissingMedia
from app.utils.i18n import Translator


class MissingMediaDialog(QDialog):
    """Displays every unresolved project asset and allows each path to be replaced."""

    def __init__(self, media: list[MissingMedia], translator: Translator,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("other", "projects"), translator)
        self.media = media
        self.translator = translator
        grouped: dict[str, list[MissingMedia]] = {}
        for entry in media:
            key = entry.original_path.replace("\\", "/").casefold()
            grouped.setdefault(key or f"{entry.kind}:{entry.identifier}", []).append(entry)
        self._groups = list(grouped.values())
        self.setMinimumSize(760, 360)
        self.heading = QLabel()
        self.heading.setObjectName("panelTitle")
        self.description = QLabel()
        self.description.setObjectName("mutedLabel")
        self.description.setWordWrap(True)
        self.folder_button = QPushButton()
        self.folder_button.clicked.connect(self._choose_folder)
        self.summary = QLabel()
        self.summary.setObjectName("mutedLabel")
        self.summary.setWordWrap(True)
        self.table = QTableWidget(len(self._groups), 4)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(50)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(3, 110)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        for row, entries in enumerate(self._groups):
            self._populate_row(row, entries[0])
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.description)
        tools = QHBoxLayout()
        tools.addWidget(self.folder_button)
        tools.addWidget(self.summary, 1)
        layout.addLayout(tools)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.button_box)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()

    def _populate_row(self, row: int, entry: MissingMedia) -> None:
        kind = QTableWidgetItem()
        name = QTableWidgetItem(entry.display_name)
        name.setToolTip("\n".join(usage.display_name for usage in self._groups[row]))
        path = QTableWidgetItem(entry.original_path)
        path.setToolTip(entry.original_path)
        self.table.setItem(row, 0, kind)
        self.table.setItem(row, 1, name)
        self.table.setItem(row, 2, path)
        browse_button = QPushButton()
        browse_button.clicked.connect(lambda _checked=False, index=row: self._choose_file(index))
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(browse_button)
        self.table.setCellWidget(row, 3, container)

    def _choose_file(self, row: int) -> None:
        entry = self._groups[row][0]
        filter_text = (
            "Video (*.mp4 *.mov *.mkv *.webm *.avi *.m4v)" if entry.is_video else
            "Audio (*.mp3 *.wav *.flac *.aac *.m4a *.ogg)" if entry.is_audio else
            "Fonts (*.ttf *.otf)" if entry.is_font else
            "Lyrics (*.lrc *.srt *.vtt *.ttml)" if entry.is_lyrics else
            "Supported content (*.*)" if entry.library_type else
            "Images (*.jpg *.jpeg *.png *.webp *.svg)"
        )
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "영상 찾기" if entry.is_video and self._korean else
            "음원 찾기" if entry.is_audio and self._korean else
            "글꼴 찾기" if entry.is_font and self._korean else
            "가사 찾기" if entry.is_lyrics and self._korean else
            "앨범 커버 찾기" if entry.kind == "cover" and self._korean else
            "콘텐츠 찾기" if entry.library_type and self._korean else
            "이미지 찾기" if self._korean else
            "Locate video" if entry.is_video else
            "Locate audio" if entry.is_audio else "Locate font" if entry.is_font else
            "Locate lyrics" if entry.is_lyrics else
            "Locate album artwork" if entry.kind == "cover" else
            "Locate content" if entry.library_type else
            "Locate image",
            str(Path(entry.original_path).parent),
            filter_text,
        )
        if selected:
            self._set_replacement(row, selected)
            self._update_summary()

    def _choose_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "누락 파일이 있는 폴더" if self._korean else "Folder containing missing files",
        )
        if not selected:
            return
        directory = Path(selected)
        filenames = Counter(
            Path(entries[0].original_path).name.casefold()
            for entries in self._groups
        )
        matched = 0
        ambiguous = False
        for row, entries in enumerate(self._groups):
            entry = entries[0]
            if entry.replacement_path:
                continue
            candidate = directory / Path(entry.original_path).name
            if candidate.is_file():
                if filenames[candidate.name.casefold()] > 1:
                    ambiguous = True
                    continue
                self._set_replacement(row, str(candidate))
                matched += 1
        self._update_summary()
        if ambiguous:
            notice = ("이름이 겹치는 파일은 개별 지정해 주세요." if self._korean
                      else "Choose files with duplicate names individually.")
            self.summary.setText(f"{self.summary.text()} · {notice}")
        elif not matched:
            notice = ("이 폴더에서 일치하는 파일을 찾지 못했습니다." if self._korean
                      else "No matching files in this folder.")
            self.summary.setText(f"{self.summary.text()} · {notice}")

    def _set_replacement(self, row: int, path: str) -> None:
        entries = self._groups[row]
        for entry in entries:
            entry.replacement_path = path
        item = self.table.item(row, 2)
        item.setText(path)
        item.setToolTip(f"{entries[0].original_path}\n→ {path}")

    def _update_summary(self) -> None:
        linked = sum(bool(entries[0].replacement_path) for entries in self._groups)
        remaining = len(self._groups) - linked
        self.summary.setText(
            f"연결 {linked}개 · 남음 {remaining}개" if self._korean else
            f"{linked} linked · {remaining} remaining"
        )
        button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        if remaining:
            button.setText("누락 파일 제외하고 열기" if self._korean else "Open without missing files")
        else:
            button.setText("프로젝트 열기" if self._korean else "Open project")

    @property
    def _korean(self) -> bool:
        return self.translator.is_korean

    def retranslate(self) -> None:
        """Refresh all labels without losing paths selected by the user."""
        korean = self._korean
        self.setWindowTitle("누락된 미디어" if korean else "Missing media")
        self.heading.setText("프로젝트에서 찾을 수 없는 파일" if korean else "Files missing from this project")
        self.description.setText(
            "파일을 하나씩 지정하거나 같은 이름의 파일이 모인 폴더를 선택하세요. 연결하지 않은 상태로 열면 누락된 음원은 비활성화되고 나머지 누락 파일은 연결이 해제됩니다. 프로젝트에 저장된 가사는 유지됩니다."
            if korean else
            "Choose files individually or select a folder containing files with the same names. Opening without files disables missing audio tracks and detaches the other missing files. Lyrics stored in the project are kept."
        )
        self.table.setHorizontalHeaderLabels(
            ["종류", "사용 위치", "파일 경로", "동작"]
            if korean else ["Type", "Used by", "File path", "Action"]
        )
        self.folder_button.setText("폴더에서 찾기…" if korean else "Find in folder…")
        for row, entries in enumerate(self._groups):
            entry = entries[0]
            label = (
                "영상" if korean and entry.is_video else
                "음원" if korean and entry.is_audio else
                "글꼴" if korean and entry.is_font else
                "가사" if korean and entry.is_lyrics else
                "앨범 커버" if korean and entry.kind == "cover" else
                "콘텐츠" if korean and entry.library_type else
                "이미지" if korean else
                "Video" if entry.is_video else
                "Audio" if entry.is_audio else "Font" if entry.is_font else
                "Lyrics" if entry.is_lyrics else
                "Album artwork" if entry.kind == "cover" else
                "Content" if entry.library_type else "Image"
            )
            self.table.item(row, 0).setText(label)
            if len(entries) > 1:
                name = (f"{entry.display_name} 외 {len(entries) - 1}곳" if korean else
                        f"{entry.display_name} and {len(entries) - 1} more")
                self.table.item(row, 1).setText(name)
            button = self.table.cellWidget(row, 3).findChild(QPushButton)
            button.setText("찾아보기" if korean else "Browse")
        self._update_summary()
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "취소" if korean else "Cancel"
        )
