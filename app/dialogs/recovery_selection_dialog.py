"""Choose one pending recovery without discarding the others."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout,
)

from app.services.autosave_service import RecoverySnapshot


class RecoverySelectionDialog(QDialog):
    """Display each session separately, even when project paths are identical."""

    def __init__(self, snapshots: list[RecoverySnapshot], korean: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("자동 저장 복구" if korean else "Autosave recovery")
        self.resize(650, 420)
        self._snapshots = tuple(snapshots)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "복구할 작업을 선택하세요. 선택하지 않은 파일은 삭제되지 않습니다."
            if korean else "Choose a workspace to recover. Other recovery files are kept."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.entries = QListWidget()
        self.entries.setWordWrap(True)
        self.entries.setTextElideMode(Qt.TextElideMode.ElideNone)
        for index, snapshot in enumerate(self._snapshots):
            title = snapshot.document.settings.title or (
                snapshot.project_path.stem if snapshot.project_path else
                ("제목 없는 프로젝트" if korean else "Untitled project")
            )
            location = str(snapshot.project_path) if snapshot.project_path else (
                "저장되지 않은 프로젝트" if korean else "Unsaved project"
            )
            try:
                timestamp = snapshot.saved_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            except (OverflowError, OSError, ValueError):
                # A representable ISO timestamp may overflow the local timezone.
                timestamp = snapshot.saved_at.isoformat()
            state = ("편집 내용 있음" if korean else "Unsaved edits") if snapshot.dirty else (
                "저장 상태" if korean else "Saved workspace"
            )
            item = QListWidgetItem(f"{title} · {state}\n{timestamp}\n{location}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(f"{location}\n{snapshot.path}")
            self.entries.addItem(item)
        layout.addWidget(self.entries, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.restore_button = self.buttons.addButton(
            "선택한 작업 복구" if korean else "Restore selected",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.restore_button.setEnabled(False)
        self.buttons.rejected.connect(self.reject)
        self.restore_button.clicked.connect(self.accept)
        self.entries.currentItemChanged.connect(
            lambda current, _previous: self.restore_button.setEnabled(current is not None)
        )
        layout.addWidget(self.buttons)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)

    @property
    def selected_snapshot(self) -> RecoverySnapshot | None:
        item = self.entries.currentItem()
        if item is None:
            return None
        index = item.data(Qt.ItemDataRole.UserRole)
        return self._snapshots[index]

    def accept(self) -> None:
        if self.selected_snapshot is not None:
            super().accept()
