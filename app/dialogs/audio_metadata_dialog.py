"""Batch editor for audio files whose common metadata tags are missing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.models.playlist import PlaylistTrack
from app.services.playlist_service import AudioImportCandidate
from app.utils.i18n import Translator


class AudioMetadataDialog(QDialog):
    """Let users complete project metadata without modifying source audio tags."""

    def __init__(
        self,
        candidates: list[AudioImportCandidate],
        translator: Translator,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.candidates = candidates
        self.translator = translator
        self.setMinimumSize(820, 390)
        self.resize(980, min(720, 330 + len(candidates) * 42))

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        root.addWidget(self.intro)

        self.table = QTableWidget(len(candidates), 5)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive,
            )
            self.table.setColumnWidth(column, 170)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table, 1)

        self.note = QLabel()
        self.note.setObjectName("mutedLabel")
        self.note.setWordWrap(True)
        root.addWidget(self.note)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._populate()
        translator.language_changed.connect(self.retranslate)
        self.retranslate()

    def _populate(self) -> None:
        for row, candidate in enumerate(self.candidates):
            track = candidate.track
            file_item = QTableWidgetItem(Path(track.file_path).name)
            file_item.setToolTip(track.file_path)
            file_item.setFlags(file_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, file_item)

            values = {
                "title": track.title if "title" not in candidate.missing_fields else Path(track.file_path).stem,
                "artist": track.artist if "artist" not in candidate.missing_fields else "",
                "album": track.album if "album" not in candidate.missing_fields else "",
            }
            for column, field in enumerate(("title", "artist", "album"), start=1):
                item = QTableWidgetItem(values[field])
                if field in candidate.missing_fields:
                    item.setData(Qt.ItemDataRole.UserRole, True)
                self.table.setItem(row, column, item)
            missing_item = QTableWidgetItem()
            missing_item.setFlags(missing_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 4, missing_item)

    @property
    def selected_tracks(self) -> list[PlaylistTrack]:
        """Return edited project tracks, applying safe fallbacks to blank cells."""
        tracks: list[PlaylistTrack] = []
        for row, candidate in enumerate(self.candidates):
            title = self.table.item(row, 1).text().strip()
            artist = self.table.item(row, 2).text().strip()
            album = self.table.item(row, 3).text().strip()
            tracks.append(replace(
                candidate.track,
                title=title or Path(candidate.track.file_path).stem,
                artist=artist or "Unknown Artist",
                album=album or "Unknown Album",
            ))
        return tracks

    def retranslate(self) -> None:
        korean = self.translator.is_korean
        self.setWindowTitle(
            "누락된 오디오 정보 입력" if korean else "Complete missing audio information"
        )
        self.intro.setText(
            "일부 오디오 파일에 제목, 아티스트 또는 앨범 정보가 없습니다. "
            "프로젝트에서 사용할 정보를 확인하거나 수정한 뒤 추가하세요."
            if korean else
            "Some audio files have no title, artist, or album tag. Review or edit the "
            "information that Music Motion Studio should use before adding them."
        )
        self.table.setHorizontalHeaderLabels(
            ["파일", "제목", "아티스트", "앨범", "누락 항목"]
            if korean else ["File", "Title", "Artist", "Album", "Missing"]
        )
        field_names = {
            "title": "제목" if korean else "Title",
            "artist": "아티스트" if korean else "Artist",
            "album": "앨범" if korean else "Album",
        }
        for row, candidate in enumerate(self.candidates):
            self.table.item(row, 4).setText(
                ", ".join(field_names[field] for field in candidate.missing_fields)
            )
        self.note.setText(
            "입력한 정보는 현재 프로젝트에만 저장되며 원본 오디오 파일의 태그는 변경하지 않습니다. "
            "비워 둔 아티스트와 앨범은 알 수 없음으로 표시됩니다."
            if korean else
            "These values are saved only in this project; the source audio tags are not modified. "
            "Blank artist and album fields use Unknown Artist and Unknown Album."
        )
        save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setText("프로젝트에 추가" if korean else "Add to project")
        cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText("취소" if korean else "Cancel")
