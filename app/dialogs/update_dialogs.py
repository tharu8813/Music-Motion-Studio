"""Dialogs for release notes and application-modal Setup download progress."""

from __future__ import annotations

from time import monotonic

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.services.update_service import ReleaseInfo
from app.utils.release_notes_html import render_release_notes_html


class UpdateAvailableDialog(QDialog):
    """Show the complete release description before the user chooses to update.

    With ``up_to_date`` the same view doubles as a "what changed in this
    version" screen for the manual update check when no newer release exists.
    """

    def __init__(
        self,
        release: ReleaseInfo,
        current_version: str,
        korean: bool,
        automatic: bool,
        parent: QWidget | None = None,
        *,
        up_to_date: bool = False,
    ) -> None:
        super().__init__(parent)
        self.release = release
        self.up_to_date = up_to_date
        self.setWindowTitle("Playlist Canvas 업데이트" if korean else "Playlist Canvas update")
        self.setMinimumSize(620, 460)
        self.resize(680, 520)

        if up_to_date:
            title = QLabel(
                f"이미 최신 버전({current_version})을 사용하고 있습니다."
                if korean else
                f"Playlist Canvas {current_version} is already up to date."
            )
        else:
            title = QLabel(
                f"새 버전 {release.version}을 사용할 수 있습니다."
                if korean else f"Playlist Canvas {release.version} is available."
            )
        title.setObjectName("panelTitle")
        title.setWordWrap(True)
        versions = QLabel(
            (
                f"현재 버전 {current_version}"
                if korean else f"Current version {current_version}"
            )
            if up_to_date else
            (
                f"현재 버전 {current_version}  →  최신 버전 {release.version}"
                if korean else f"Current {current_version}  →  Latest {release.version}"
            )
        )
        versions.setObjectName("mutedLabel")
        heading = QLabel(
            (
                "이번 버전의 변경 사항" if korean else "What changed in this version"
            )
            if up_to_date else
            ("최신 릴리즈 내용" if korean else "Latest release notes")
        )
        heading.setObjectName("panelTitle")
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        dark = self.notes.palette().base().color().lightness() < 128
        self.notes.setHtml(render_release_notes_html(release.body, korean, dark))

        self.buttons = QDialogButtonBox()
        self.update_button = QPushButton(
            "다운로드 및 업데이트" if korean else "Download and update"
        )
        self.update_button.setObjectName("primaryButton")
        self.update_button.setToolTip(
            "작업을 자동으로 보관한 뒤 설치와 재실행까지 진행합니다. 추가 확인 버튼을 누를 필요가 없습니다."
            if korean else "Backs up your workspace, installs and restarts automatically without further confirmation."
        )
        self.update_button.setEnabled(release.can_install and not up_to_date)
        self.update_button.clicked.connect(self.accept)
        dismiss_text = (
            "이 버전은 다시 알리지 않기" if korean else "Do not remind me about this version"
        ) if automatic and not up_to_date else ("닫기" if korean else "Close")
        self.dismiss_button = QPushButton(dismiss_text)
        self.dismiss_button.clicked.connect(self.reject)
        self.buttons.addButton(self.dismiss_button, QDialogButtonBox.ButtonRole.RejectRole)
        if not up_to_date:
            self.buttons.addButton(
                self.update_button, QDialogButtonBox.ButtonRole.AcceptRole,
            )
        else:
            self.update_button.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(versions)
        layout.addSpacing(4)
        layout.addWidget(heading)
        layout.addWidget(self.notes, 1)
        if not release.can_install and not up_to_date:
            warning = QLabel(
                "이 릴리즈에는 SHA-256으로 검증 가능한 Playlist Canvas Setup 파일이 없습니다."
                if korean else
                "This release has no SHA-256-verifiable Playlist Canvas Setup asset."
            )
            warning.setObjectName("warningLabel")
            warning.setWordWrap(True)
            layout.addWidget(warning)
        layout.addWidget(self.buttons)


class UpdateDownloadDialog(QDialog):
    """Lock the application while the selected Setup asset is downloaded and checked."""

    cancel_requested = Signal()

    def __init__(self, release: ReleaseInfo, korean: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._korean = korean
        self._allow_close = False
        self._cancelling = False
        self._started_at = monotonic()
        self.setWindowTitle("업데이트 다운로드" if korean else "Downloading update")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(540, 230)

        self.title = QLabel(
            f"Playlist Canvas {release.version} Setup 다운로드 중"
            if korean else f"Downloading Playlist Canvas {release.version} Setup"
        )
        self.title.setObjectName("panelTitle")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.percent = QLabel("0%")
        self.percent.setObjectName("panelTitle")
        self.detail = QLabel(
            "설치 파일을 다운로드하고 검증합니다. 완료되면 작업을 보관하고 자동으로 설치·재실행합니다."
            if korean else "Downloading and verifying Setup. Your workspace will be backed up before automatic installation and restart."
        )
        self.detail.setWordWrap(True)
        self.detail.setObjectName("mutedLabel")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText("취소" if korean else "Cancel")
        self.cancel_button.clicked.connect(self.request_cancel)

        row = QHBoxLayout()
        row.addWidget(self.progress_bar, 1)
        row.addWidget(self.percent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addWidget(self.title)
        layout.addLayout(row)
        layout.addWidget(self.detail)
        layout.addStretch(1)
        layout.addWidget(self.buttons)

    def update_progress(self, fraction: float, message: str) -> None:
        percent = round(max(0.0, min(1.0, fraction)) * 100)
        self.progress_bar.setValue(percent)
        self.percent.setText(f"{percent}%")
        translated = message
        if self._korean and message.startswith("Downloaded "):
            translated = "다운로드됨 · " + message.removeprefix("Downloaded ")
        elif self._korean and message == "Setup download and SHA-256 verification complete":
            translated = "Setup 다운로드와 SHA-256 검증을 완료했습니다."
        self.detail.setText(translated)

    def request_cancel(self) -> bool:
        if self._cancelling:
            return False
        response = QMessageBox.question(
            self,
            "업데이트 취소" if self._korean else "Cancel update",
            "업데이트 다운로드를 취소할까요? 받은 임시 파일은 삭제됩니다."
            if self._korean else
            "Cancel the update download? The partial file will be removed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return False
        self._cancelling = True
        self.cancel_button.setEnabled(False)
        self.detail.setText(
            "다운로드를 취소하고 임시 파일을 정리하는 중입니다."
            if self._korean else "Cancelling the download and removing temporary data."
        )
        self.cancel_requested.emit()
        return True

    def complete(self, accepted: bool) -> None:
        self._allow_close = True
        self.accept() if accepted else self.reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        if not self._cancelling:
            self.request_cancel()
        event.ignore()
