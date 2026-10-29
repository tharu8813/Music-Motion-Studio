"""Side-by-side confirmation before replacing lyrics attached to a track."""

from __future__ import annotations

from app.ui.design_system import COLORS

from pathlib import Path
import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

from app.services.lyrics_service import LyricsService
from app.utils.i18n import Translator
from app.widgets.lyrics_cue_table import LyricsCueTable


class LyricsCompareDialog(QDialog):
    """Let the user compare current and incoming timed lyrics before replacement."""

    def __init__(
        self, current_cues: list[dict[str, Any]], incoming_cues: list[dict[str, Any]],
        current_path: str, incoming_path: str, translator: Translator,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.replace_requested = False
        korean = translator.is_korean
        self.setWindowTitle("가사 비교 및 교체" if korean else "Compare and replace lyrics")
        self.setModal(True)
        self.resize(900, 590)

        root = QVBoxLayout(self)
        title = QLabel(
            "이 곡에는 이미 가사가 적용되어 있습니다. 두 가사를 비교한 뒤 사용할 항목을 선택하세요."
            if korean else
            "This track already has lyrics. Compare both versions before choosing which one to use."
        )
        title.setWordWrap(True)
        root.addWidget(title)
        summary = QLabel(
            f"현재 {len(current_cues)}행 → 새 가사 {len(incoming_cues)}행 · 교체하면 기존 가사 대신 적용됩니다."
            if korean else f"{len(current_cues)} current → {len(incoming_cues)} incoming rows · replacement overwrites the attached lyrics."
        )
        summary.setObjectName("infoCallout")
        summary.setWordWrap(True)
        root.addWidget(summary)

        columns = QHBoxLayout()
        self.previews = []
        columns.setSpacing(12)
        columns.addLayout(self._column(
            "현재 가사" if korean else "Current lyrics", current_path,
            current_cues, korean,
        ), 1)
        columns.addLayout(self._column(
            "추가할 가사" if korean else "Incoming lyrics", incoming_path,
            incoming_cues, korean,
        ), 1)
        root.addLayout(columns, 1)
        old = {json.dumps(cue, sort_keys=True, ensure_ascii=False) for cue in current_cues}
        new = {json.dumps(cue, sort_keys=True, ensure_ascii=False) for cue in incoming_cues}
        for table, cues, other in ((self.previews[0], current_cues, new), (self.previews[1], incoming_cues, old)):
            for row, cue in enumerate(cues):
                if json.dumps(cue, sort_keys=True, ensure_ascii=False) not in other:
                    for column in range(table.columnCount()):
                        table.item(row, column).setBackground(QColor(COLORS["warning_surface"]))
        legend = QLabel("강조된 행은 시간·내용·단어 정보가 다른 구간입니다. 원본 파일은 변경하지 않습니다." if korean else
                        "Highlighted rows have different timing, text or word data. Original files are not modified.")
        legend.setWordWrap(True)
        legend.setObjectName("mutedLabel")
        root.addWidget(legend)

        buttons = QDialogButtonBox()
        keep = buttons.addButton(
            "현재 가사 유지" if korean else "Keep current",
            QDialogButtonBox.ButtonRole.RejectRole,
        )
        replace = buttons.addButton(
            "새 가사 적용" if korean else "Use new lyrics",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        keep.setDefault(True)
        replace.setProperty("primary", True)
        keep.clicked.connect(self.reject)
        replace.clicked.connect(self._accept_replacement)
        root.addWidget(buttons)

    def _column(
        self, heading: str, path: str, cues: list[dict[str, Any]], korean: bool,
    ) -> QVBoxLayout:
        layout = QVBoxLayout()
        label = QLabel(f"<b>{heading}</b> · {len(cues)}" + ("개 구간" if korean else " cues"))
        layout.addWidget(label)
        timing = QLabel(("단어·음절 시간 포함" if korean else "Word / syllable timing included")
                        if any(cue.get("words") for cue in cues) else
                        ("줄 단위 시간 정보" if korean else "Line timing"))
        timing.setObjectName("mutedLabel")
        layout.addWidget(timing)
        path_label = QLabel(Path(path).name if path else ("프로젝트 내장 가사" if korean else "Embedded lyrics"))
        path_label.setObjectName("mutedLabel")
        path_label.setTextFormat(Qt.TextFormat.PlainText)
        path_label.setWordWrap(True)
        path_label.setToolTip(path)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path_label)
        preview = LyricsCueTable(selectable=False)
        preview.set_cues(cues, korean)
        preview.setAccessibleName(heading)
        self.previews.append(preview)
        layout.addWidget(preview, 1)
        return layout

    def _accept_replacement(self) -> None:
        self.replace_requested = True
        self.accept()
