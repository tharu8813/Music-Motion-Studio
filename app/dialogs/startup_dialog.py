"""Modal project chooser displayed before the editor becomes accessible."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.services.recent_projects_service import RecentProjectsService
from app.services.project_service import ProjectError, ProjectService, ProjectSummary
from app.utils.i18n import Translator
from app.widgets.activity_progress import ElidedLabel


class _RecentProjectRow(QWidget):
    """Thumbnail and project metadata displayed in one recent-project row."""

    def __init__(self, path: Path, summary: ProjectSummary, modified: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(12)
        thumbnail = QLabel()
        thumbnail.setObjectName("recentProjectThumbnail")
        thumbnail.setFixedSize(128, 72)
        thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap()
        if summary.thumbnail:
            pixmap.loadFromData(summary.thumbnail)
        if not pixmap.isNull():
            thumbnail.setPixmap(pixmap.scaled(
                thumbnail.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            thumbnail.setText("MMS")
        layout.addWidget(thumbnail)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        title = ElidedLabel(summary.title)
        title.setObjectName("panelTitle")
        description = ElidedLabel(summary.description or str(path.parent))
        description.setObjectName("mutedLabel")
        description.setWordWrap(False)
        meta_parts = [part for part in (summary.author, modified) if part]
        metadata = ElidedLabel("  ·  ".join(meta_parts))
        metadata.setObjectName("mutedLabel")
        text_layout.addWidget(title)
        text_layout.addWidget(description)
        text_layout.addWidget(metadata)
        layout.addLayout(text_layout, 1)


class StartupDialog(QDialog):
    """Choose how the initial editing session should begin."""

    NEW_PROJECT = "new"
    OPEN_PROJECT = "open"

    def __init__(self, translator: Translator, recent: RecentProjectsService,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("canvas", "start"), translator)
        self.translator = translator
        self.recent = recent
        self.action = ""
        self.project_path: Path | None = None
        self.setObjectName("startupDialog")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumSize(820, 540)
        self.resize(960, 600)
        # A project file dragged from Explorer opens directly.
        self.setAcceptDrops(True)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        actions_card = QFrame()
        actions_card.setObjectName("startupRail")
        actions_card.setFixedWidth(292)
        actions_layout = QVBoxLayout(actions_card)
        actions_layout.setContentsMargins(28, 32, 28, 24)
        actions_layout.setSpacing(12)
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        brand_icon = QLabel()
        brand_icon.setPixmap(QIcon(str(resource_root / "app/resources/app_icon.ico")).pixmap(44, 44))
        brand_icon.setFixedSize(48, 48)
        actions_layout.addWidget(brand_icon)
        actions_layout.addSpacing(8)
        self.title = QLabel()
        self.title.setObjectName("startupTitle")
        self.title.setWordWrap(True)
        self.description = QLabel()
        self.description.setObjectName("mutedLabel")
        self.description.setWordWrap(True)
        actions_layout.addWidget(self.title)
        actions_layout.addWidget(self.description)
        actions_layout.addSpacing(24)
        self.actions_title = QLabel()
        self.actions_title.setObjectName("panelTitle")
        self.new_button = QPushButton()
        self.new_button.setMinimumHeight(30)
        self.new_button.setProperty("primary", True)
        self.new_button.setIcon(QIcon())
        self.new_button.setIconSize(QSize(24, 24))
        self.open_button = QPushButton()
        self.open_button.setMinimumHeight(30)
        self.open_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        self.open_button.setIconSize(QSize(18, 18))
        actions_layout.addWidget(self.actions_title)
        actions_layout.addWidget(self.new_button)
        actions_layout.addWidget(self.open_button)
        actions_layout.addStretch()
        self.exit_hint = QLabel()
        self.exit_hint.setObjectName("mutedLabel")
        self.exit_hint.setWordWrap(True)
        actions_layout.addWidget(self.exit_hint)
        root.addWidget(actions_card)

        recent_card = QFrame()
        recent_card.setObjectName("startupRecentPane")
        recent_layout = QVBoxLayout(recent_card)
        recent_layout.setContentsMargins(28, 28, 28, 24)
        recent_layout.setSpacing(16)
        recent_header = QHBoxLayout()
        self.recent_title = QLabel()
        self.recent_title.setObjectName("panelTitle")
        self.clear_button = QPushButton()
        self.clear_button.setFlat(True)
        recent_header.addWidget(self.recent_title)
        recent_header.addStretch()
        recent_header.addWidget(self.clear_button)
        recent_layout.addLayout(recent_header)
        self.recent_list = QListWidget()
        self.recent_list.setObjectName("recentProjectList")
        self.recent_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.recent_list.setAlternatingRowColors(False)
        self.recent_empty = QLabel()
        self.recent_empty.setObjectName("startupEmptyState")
        self.recent_empty.setWordWrap(True)
        self.recent_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout = QVBoxLayout(self.recent_list)
        empty_layout.setContentsMargins(32, 32, 32, 32)
        empty_layout.addWidget(self.recent_empty)
        self.recent_empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        recent_layout.addWidget(self.recent_list, 1)
        recent_buttons = QHBoxLayout()
        self.remove_button = QPushButton()
        self.open_recent_button = QPushButton()
        self.open_recent_button.setDefault(True)
        recent_buttons.addWidget(self.remove_button)
        recent_buttons.addStretch()
        recent_buttons.addWidget(self.open_recent_button)
        recent_layout.addLayout(recent_buttons)
        root.addWidget(recent_card, 1)

        self.new_button.clicked.connect(self._choose_new)
        self.open_button.clicked.connect(self._browse_project)
        self.open_recent_button.clicked.connect(self._open_recent)
        self.remove_button.clicked.connect(self._remove_recent)
        self.clear_button.clicked.connect(self._clear_recent)
        self.recent_list.itemDoubleClicked.connect(lambda _item: self._open_recent())
        self.recent_list.itemSelectionChanged.connect(self._update_recent_buttons)
        remove_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.recent_list)
        remove_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        remove_shortcut.activated.connect(self._remove_recent)
        translator.language_changed.connect(self.retranslate)
        recent.changed.connect(self.refresh_recent)
        self.retranslate()
        self.refresh_recent()

    def retranslate(self) -> None:
        """Refresh all visible startup text."""
        korean = self.translator.is_korean
        self.setWindowTitle("프로젝트 시작" if korean else "Start a project")
        self.title.setText("Music Motion\nStudio")
        self.description.setText(
            "음악을 담고, 장면을 만드세요."
            if korean else
            "Your music. Your moving canvas."
        )
        self.recent_empty.setText(
            "아직 연 프로젝트가 없습니다.\n\n새 프로젝트를 만들거나\n프로젝트 파일을 이곳으로 끌어오세요."
            if korean else
            "No projects opened yet.\n\nCreate a project or drop\na project file here to begin."
        )
        self.actions_title.setText("시작" if korean else "Get started")
        self.new_button.setText("새 프로젝트 만들기" if korean else "Create new project")
        self.open_button.setText("프로젝트 파일 열기" if korean else "Open project file")
        self.recent_title.setText("최근 프로젝트" if korean else "Recent projects")
        self.clear_button.setText("목록 지우기" if korean else "Clear list")
        self.remove_button.setText("목록에서 제거" if korean else "Remove from list")
        self.remove_button.setToolTip(
            "선택한 항목을 최근 목록에서만 뺍니다. 프로젝트 파일은 삭제되지 않습니다. (Delete)"
            if korean else
            "Removes the entry from this list only; the project file is kept. (Delete)"
        )
        self.open_recent_button.setText("선택한 프로젝트 열기" if korean else "Open selected")
        self.exit_hint.setText(
            "이 창을 닫아도 프로그램은 유지됩니다. 프로젝트를 열기 전에는 설정과 도움말 등 일부 기능만 사용할 수 있습니다."
            if korean else "Closing this window keeps the application open. Only settings, help and other basic features are available until you open a project."
        )
        self.refresh_recent()

    def refresh_recent(self) -> None:
        """Rebuild the list with current file names, folders, and timestamps."""
        selected_path = self._selected_recent_path()
        self.recent_list.clear()
        projects = self.recent.projects()
        korean = self.translator.is_korean
        self.recent_empty.setVisible(not projects)
        self.remove_button.setVisible(bool(projects))
        self.open_recent_button.setVisible(bool(projects))
        self.clear_button.setVisible(bool(projects))
        self.new_button.setDefault(not projects)
        self.open_recent_button.setDefault(bool(projects))
        if projects:
            for path in projects:
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    modified = ""
                try:
                    summary = ProjectService.inspect(path)
                except ProjectError:
                    summary = ProjectSummary(
                        path.stem.removesuffix(".project"), "", "", ""
                    )
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, str(path))
                item.setToolTip(str(path))
                item.setSizeHint(QSize(0, 88))
                self.recent_list.addItem(item)
                self.recent_list.setItemWidget(
                    item, _RecentProjectRow(path, summary, modified, self.recent_list)
                )
                if selected_path == path:
                    self.recent_list.setCurrentItem(item)
            if self.recent_list.currentItem() is None:
                self.recent_list.setCurrentRow(0)
        self.clear_button.setEnabled(bool(projects))
        self._update_recent_buttons()

    def _selected_recent_path(self) -> Path | None:
        item = self.recent_list.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return Path(str(value)) if value else None

    def _update_recent_buttons(self) -> None:
        enabled = self._selected_recent_path() is not None
        self.open_recent_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)

    def _choose_new(self) -> None:
        self.action = self.NEW_PROJECT
        self.project_path = None
        self.accept()

    def _browse_project(self) -> None:
        korean = self.translator.is_korean
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "프로젝트 열기" if korean else "Open project",
            "",
            "Music Motion Studio Project (*.pvsproj *.project.json *.json)",
        )
        if selected:
            self.action = self.OPEN_PROJECT
            self.project_path = Path(selected)
            self.accept()

    def _open_recent(self) -> None:
        path = self._selected_recent_path()
        if path is None:
            return
        self.action = self.OPEN_PROJECT
        self.project_path = path
        self.accept()

    def _remove_recent(self) -> None:
        path = self._selected_recent_path()
        if path is not None:
            self.recent.remove(path)

    def _clear_recent(self) -> None:
        korean = self.translator.is_korean
        answer = QMessageBox.question(
            self,
            "최근 프로젝트 목록 지우기" if korean else "Clear recent projects",
            "최근에 연 프로젝트 목록을 모두 지울까요?\n\n프로젝트 파일 자체는 삭제되지 않습니다."
            if korean else
            "Clear the entire recent projects list?\n\nThe project files themselves will not be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.recent.clear()

    @staticmethod
    def _dropped_project(event: QDragEnterEvent | QDropEvent) -> Path | None:
        for url in event.mimeData().urls() if event.mimeData().hasUrls() else ():
            path = Path(url.toLocalFile())
            name = path.name.lower()
            if path.suffix.lower() in {".pvsproj", ".json"} or name.endswith(".project.json"):
                return path
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt API name
        if self._dropped_project(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt API name
        path = self._dropped_project(event)
        if path is None or not path.is_file():
            event.ignore()
            return
        event.acceptProposedAction()
        self.action = self.OPEN_PROJECT
        self.project_path = path
        self.accept()
