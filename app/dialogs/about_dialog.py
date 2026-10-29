"""Program information and support diagnostics dialog."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import PySide6
from PySide6.QtCore import Qt, QUrl, qVersion
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.dialogs.help_dialog import install_help_shortcut
from app.utils.i18n import Translator


REPOSITORY_URL = "https://github.com/tharu8813/Music-Motion-Studio"


class AboutDialog(QDialog):
    """Show release identity, runtime details, and support shortcuts."""

    def __init__(self, translator: Translator, ffmpeg_path: Path | None,
                 log_directory: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("other", "troubleshooting"))
        self.translator = translator
        self.ffmpeg_path = ffmpeg_path
        self.log_directory = log_directory
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        self.license_path = resource_root / "LICENSE.txt"
        if not self.license_path.is_file():
            self.license_path = Path(sys.executable).resolve().parent / "LICENSE.txt"
        self.setObjectName("aboutDialog")
        self.setMinimumSize(620, 520)
        self.resize(660, 560)

        header = QFrame()
        header.setObjectName("aboutHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 16, 18, 16)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(72, 72)
        self.icon_label.setPixmap(QApplication.windowIcon().pixmap(64, 64))
        title_layout = QVBoxLayout()
        self.name_label = QLabel("Music Motion Studio")
        self.name_label.setObjectName("aboutProductName")
        self.version_label = QLabel(f"Version {__version__}")
        self.version_label.setObjectName("aboutVersion")
        self.description_label = QLabel()
        self.description_label.setObjectName("mutedLabel")
        self.description_label.setWordWrap(True)
        title_layout.addWidget(self.name_label)
        title_layout.addWidget(self.version_label)
        title_layout.addWidget(self.description_label)
        header_layout.addWidget(self.icon_label)
        header_layout.addLayout(title_layout, 1)

        details = QFrame()
        details.setObjectName("aboutDetailsCard")
        details_form = QFormLayout(details)
        details_form.setContentsMargins(16, 14, 16, 14)
        details_form.setHorizontalSpacing(24)
        self.version_title = QLabel()
        self.runtime_title = QLabel()
        self.qt_title = QLabel()
        self.os_title = QLabel()
        self.ffmpeg_title = QLabel()
        self.log_title = QLabel()
        self.repository_title = QLabel()
        details_form.addRow(self.version_title, QLabel(__version__))
        details_form.addRow(
            self.runtime_title,
            QLabel(f"Python {platform.python_version()} ({platform.machine()})"),
        )
        details_form.addRow(
            self.qt_title, QLabel(f"Qt {qVersion()} / PySide {PySide6.__version__}"),
        )
        details_form.addRow(self.os_title, QLabel(self._windows_label()))
        self.ffmpeg_value = QLabel()
        self.ffmpeg_value.setWordWrap(True)
        self.ffmpeg_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_form.addRow(self.ffmpeg_title, self.ffmpeg_value)
        self.log_value = QLabel(str(log_directory))
        self.log_value.setWordWrap(True)
        self.log_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_form.addRow(self.log_title, self.log_value)
        self.repository_link = QLabel(
            f'<a href="{REPOSITORY_URL}">github.com/tharu8813/Music-Motion-Studio</a>'
        )
        self.repository_link.setOpenExternalLinks(True)
        self.repository_link.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.repository_link.setToolTip(REPOSITORY_URL)
        details_form.addRow(self.repository_title, self.repository_link)

        self.notice_label = QLabel()
        self.notice_label.setObjectName("mutedLabel")
        self.notice_label.setWordWrap(True)

        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self._copy_diagnostics)
        self.open_logs_button = QPushButton()
        self.open_logs_button.clicked.connect(self._open_logs)
        self.open_license_button = QPushButton()
        self.open_license_button.clicked.connect(self._open_license)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        action_row = QHBoxLayout()
        action_row.addWidget(self.copy_button)
        action_row.addWidget(self.open_logs_button)
        action_row.addWidget(self.open_license_button)
        action_row.addStretch()
        action_row.addWidget(self.buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)
        layout.addWidget(header)
        layout.addWidget(details)
        layout.addWidget(self.notice_label)
        layout.addStretch()
        layout.addLayout(action_row)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()

    def diagnostic_text(self) -> str:
        """Return a compact support block safe to paste into an issue report."""
        ffmpeg = str(self.ffmpeg_path) if self.ffmpeg_path else "Not configured"
        return "\n".join((
            "Music Motion Studio diagnostics",
            f"App version: {__version__}",
            f"Python: {platform.python_version()} ({platform.machine()})",
            f"Qt: {qVersion()}",
            f"PySide: {PySide6.__version__}",
            f"OS: {self._windows_label()}",
            f"FFmpeg: {ffmpeg}",
            f"Logs: {self.log_directory}",
            f"Repository: {REPOSITORY_URL}",
            f"Frozen build: {'yes' if getattr(sys, 'frozen', False) else 'no'}",
        ))

    def _copy_diagnostics(self) -> None:
        QApplication.clipboard().setText(self.diagnostic_text())
        korean = self.translator.is_korean
        self.copy_button.setText("복사됨" if korean else "Copied")

    def _open_logs(self) -> None:
        korean = self.translator.is_korean
        try:
            self.log_directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(
                self,
                "로그 폴더 오류" if korean else "Log folder error",
                str(error),
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_directory))):
            QMessageBox.warning(
                self,
                "로그 폴더 오류" if korean else "Log folder error",
                "로그 폴더를 열 수 없습니다." if korean else "Could not open the log folder.",
            )

    def _open_license(self) -> None:
        """Open the bundled application license in the user's default text viewer."""
        korean = self.translator.is_korean
        if not self.license_path.is_file() or not QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.license_path))
        ):
            QMessageBox.warning(
                self,
                "라이선스 오류" if korean else "License error",
                "Music Motion Studio 라이선스 파일을 열 수 없습니다."
                if korean else "Could not open the Music Motion Studio license file.",
            )

    def retranslate(self) -> None:
        korean = self.translator.is_korean
        self.setWindowTitle("프로그램 정보" if korean else "About Music Motion Studio")
        self.description_label.setText(
            "음악 재생목록을 시각적인 영상으로 구성하고 MP4로 내보내는 데스크톱 스튜디오입니다.\n"
            "ChatGPT Codex와 Claude Code를 활용해 제작했습니다."
            if korean else
            "A desktop studio for turning music playlists into visual compositions and MP4 videos.\n"
            "Created with ChatGPT Codex and Claude Code."
        )
        self.version_title.setText("애플리케이션 버전" if korean else "Application version")
        self.runtime_title.setText("Python 런타임" if korean else "Python runtime")
        self.qt_title.setText("UI 프레임워크" if korean else "UI framework")
        self.os_title.setText("운영체제" if korean else "Operating system")
        self.ffmpeg_title.setText("FFmpeg 상태" if korean else "FFmpeg status")
        self.log_title.setText("로그 폴더" if korean else "Log folder")
        self.repository_title.setText("GitHub 저장소" if korean else "GitHub repository")
        self.ffmpeg_value.setText(
            str(self.ffmpeg_path)
            if self.ffmpeg_path else
            ("설정되지 않음" if korean else "Not configured")
        )
        self.notice_label.setText(
            "Music Motion Studio는 비상업적 동일조건 소스 공개 라이선스로 배포됩니다. "
            "프로그램과 수정본의 상업적 수정·배포는 금지되며, 제작물의 상업적 이용은 허용됩니다. "
            "온라인 제작물에 Music Motion Studio와 원본 저장소를 표시하는 것은 선택 사항입니다. "
            "FFmpeg는 앱에 포함되지 않으며, 자동 설치본은 BtbN FFmpeg GPL 배포본을 SHA-256으로 검증합니다."
            if korean else
            "Music Motion Studio uses a noncommercial, share-alike source-available license. Commercial modification or "
            "distribution of the app is prohibited, while commercial use of output is permitted. Crediting Playlist "
            "Canvas and its original repository in public online uploads is optional. "
            "FFmpeg is not bundled; managed installs use the SHA-256-verified BtbN FFmpeg GPL build."
        )
        self.copy_button.setText("진단 정보 복사" if korean else "Copy diagnostics")
        self.open_logs_button.setText("로그 폴더 열기" if korean else "Open log folder")
        self.open_license_button.setText("라이선스 보기" if korean else "View license")
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText(
            "닫기" if korean else "Close"
        )

    @staticmethod
    def _windows_label() -> str:
        return f"{platform.system()} {platform.release()} ({platform.version()})"
