"""Copyable crash report with a repeat-failure emergency-exit menu."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class CrashReportDialog(QDialog):
    """Display full diagnostics; never exit the application on dialog close."""

    def __init__(
        self,
        exception_type: str,
        exception_message: str,
        traceback_text: str,
        log_path: str | None,
        korean: bool,
        parent: QWidget | None = None,
        *,
        report_text: str | None = None,
        report_path: str | None = None,
        repeat_count: int = 1,
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumSize(720, 500)
        self.resize(820, 590)
        self.setWindowTitle("크래시 보고서" if korean else "Crash report")
        self.exit_choice: str | None = None
        self._korean = korean

        heading = QLabel("프로그램 오류가 발생했습니다." if korean else
                         "The application encountered an error.")
        heading.setObjectName("panelTitle")
        description = QLabel(
            "진단 보고서를 별도 파일에 저장했습니다. 계속 오류가 발생하면 "
            "종료 옵션을 사용해 프로젝트를 안전하게 저장할 수 있습니다."
            if korean else
            "A diagnostic report was saved separately. If errors repeat, "
            "use Exit options to protect your project."
        )
        description.setWordWrap(True)

        form = QFormLayout()

        def add_row(ko: str, en: str, value: str) -> QLabel:
            label = QLabel(value or "-")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(ko if korean else en, label)
            return label

        add_row("오류 유형", "Exception", exception_type)
        add_row("오류 메시지", "Message", exception_message or
                ("메시지 없음" if korean else "No message"))
        add_row("애플리케이션 로그", "Application log", log_path or
                ("사용할 수 없음" if korean else "Unavailable"))
        add_row("진단 보고서", "Saved report", report_path or
                ("파일 저장에 실패함" if korean else "File could not be saved"))
        self.repeat_label = add_row("연속 오류", "Recent failures", "")
        self._repeat_notice = QLabel(
            "짧은 시간에 오류가 반복됐습니다. 저장 후 종료하는 것을 권장합니다."
            if korean else
            "Errors are repeating. Consider saving your project before exiting."
        )
        self._repeat_notice.setWordWrap(True)

        self.details = QPlainTextEdit(report_text if report_text is not None else traceback_text)
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.details.setObjectName("crashReportDetails")

        privacy = QLabel(
            "주의: 보고서에 파일 경로, 프로젝트 이름, 최근 로그가 포함될 수 있습니다. "
            "외부에 전달하기 전에 내용을 확인하세요."
            if korean else
            "Privacy: The report may include paths, project names, and recent "
            "logs. Review it before sharing."
        )
        privacy.setWordWrap(True)

        self.copy_button = QPushButton("전체 보고서 복사" if korean else "Copy full report")
        self.copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.details.toPlainText())
        )
        self.folder_button = QPushButton("보고서 폴더 열기" if korean else "Open report folder")
        self.folder_button.setEnabled(bool(report_path or log_path))
        self.folder_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(
                Path(report_path or log_path).expanduser().parent
            )))
        )
        self.exit_button = QPushButton("종료 옵션" if korean else "Exit options")
        self.exit_button.setObjectName("crashExitOptions")
        exit_menu = QMenu(self.exit_button)
        for code, ko, en in (
            ("save", "프로젝트를 저장하고 종료", "Save project and exit"),
            ("save_as", "프로젝트를 다른 이름으로 저장하고 종료",
             "Save project as and exit"),
            ("discard", "프로젝트를 저장하지 않고 종료", "Exit without saving"),
            ("force", "강제 종료", "Force quit"),
        ):
            action = exit_menu.addAction(ko if korean else en)
            action.triggered.connect(lambda _checked=False, choice=code: self._choose_exit(choice))
        self.exit_button.setMenu(exit_menu)
        self.set_repeat_count(repeat_count)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(
            "닫기" if korean else "Close"
        )
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        from PySide6.QtWidgets import QHBoxLayout

        actions = QHBoxLayout()
        actions.addWidget(self.copy_button)
        actions.addWidget(self.folder_button)
        actions.addStretch(1)
        actions.addWidget(self.exit_button)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(description)
        layout.addLayout(form)
        layout.addWidget(self._repeat_notice)
        layout.addWidget(self.details, 1)
        layout.addWidget(privacy)
        layout.addLayout(actions)
        layout.addWidget(buttons)

    def set_repeat_count(self, count: int) -> None:
        """Also callable while a modal dialog handles nested Qt events."""
        count = max(1, count)
        self.repeat_label.setText(
            f"최근 90초간 {count}회" if self._korean else
            f"{count} report(s) in the last 90 seconds"
        )
        self._repeat_notice.setVisible(count >= 2)
        self.exit_button.setVisible(count >= 2)

    def _choose_exit(self, choice: str) -> None:
        self.exit_choice = choice
        self.accept()
