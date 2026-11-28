"""Detailed report shown when a project cannot be loaded safely."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QMenu,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ProjectCrashReportDialog(QDialog):
    """Explain a failed project load while reassuring that the editor survived."""

    def __init__(
        self,
        *,
        project_path: str,
        stage: str,
        exception_type: str,
        exception_message: str,
        cause_type: str,
        cause_message: str,
        guidance: str,
        report_text: str,
        log_path: str,
        korean: bool,
        report_path: str | None = None,
        repeat_count: int = 1,
        workspace_restored: bool = True,
        allow_salvage: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.exit_choice: str | None = None
        self.salvage_requested = False
        self._korean = korean
        self.setMinimumSize(760, 520)
        self.resize(820, 580)
        self.setWindowTitle(
            "프로젝트 크래시 보고서" if korean else "Project crash report"
        )

        heading = QLabel(
            "프로젝트를 불러오지 못했습니다."
            if korean else "The project could not be loaded."
        )
        heading.setObjectName("panelTitle")
        description = QLabel(
            (
                "기존 작업공간 복원에도 실패했습니다. 원본 프로젝트를 덮어쓰지 마세요. "
                "오류 보고서를 보관하고 필요한 경우 다른 이름으로 저장하세요."
                if korean else
                "Restoring the previous workspace also failed. Do not overwrite "
                "the original project. Keep the report and consider Save As."
            ) if not workspace_restored else (
                "문제가 있는 프로젝트는 적용하지 않았으며, 기존 프로젝트와 프로그램은 계속 사용할 수 있습니다."
                if korean else
                "The problematic project was not applied. Your existing project and the application remain available."
            )
        )
        description.setWordWrap(True)

        form = QFormLayout()

        def add_row(korean_label: str, english_label: str, value: str) -> None:
            label = QLabel(value or "-")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(
                label.textInteractionFlags()
                | Qt.TextInteractionFlag.TextSelectableByMouse
            )
            form.addRow(korean_label if korean else english_label, label)

        add_row("프로젝트 파일", "Project file", project_path)
        add_row("실패 단계", "Failed stage", stage)
        add_row("오류 유형", "Exception type", exception_type)
        add_row("오류 메시지", "Exception message", exception_message)
        if cause_type and (cause_type != exception_type or cause_message != exception_message):
            add_row("근본 원인", "Root cause", f"{cause_type}: {cause_message}")
        add_row("권장 조치", "Suggested action", guidance)
        add_row("로그 폴더", "Log folder", log_path)
        add_row("저장된 보고서", "Saved report", report_path or
                ("보고서를 저장하지 못했습니다." if korean else "Report file unavailable."))
        self.repeat_label = QLabel()
        form.addRow("연속 오류" if korean else "Recent failures", self.repeat_label)

        details_label = QLabel("상세 보고서" if korean else "Detailed report")
        details_label.setObjectName("sectionTitle")
        self.details = QPlainTextEdit(report_text)
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self.copy_button = QPushButton(
            "보고서 복사" if korean else "Copy report"
        )
        self.copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.details.toPlainText())
        )
        self.salvage_button = QPushButton("복구 시도" if korean else "Attempt recovery")
        self.salvage_button.setVisible(allow_salvage)
        self.salvage_button.clicked.connect(self._choose_salvage)
        self.exit_button = QPushButton("종료 옵션" if korean else "Exit options")
        self.exit_button.setObjectName("crashExitOptions")
        exit_menu = QMenu(self.exit_button)
        for choice, ko, en in (
            ("save", "프로젝트를 저장하고 종료", "Save project and exit"),
            ("save_as", "프로젝트를 다른 이름으로 저장하고 종료", "Save project as and exit"),
            ("discard", "프로젝트를 저장하지 않고 종료", "Exit without saving"),
            ("force", "강제 종료", "Force quit"),
        ):
            action = exit_menu.addAction(ko if korean else en)
            action.triggered.connect(
                lambda _checked=False, mode=choice: self._choose_exit(mode)
            )
        self.exit_button.setMenu(exit_menu)
        self.set_repeat_count(repeat_count)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(
            "닫기" if korean else "Close"
        )
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(description)
        layout.addLayout(form)
        layout.addWidget(details_label)
        layout.addWidget(self.details, 1)
        actions = QHBoxLayout()
        actions.addWidget(self.copy_button)
        actions.addWidget(self.salvage_button)
        actions.addStretch()
        actions.addWidget(self.exit_button)
        layout.addLayout(actions)
        layout.addWidget(QLabel(
            "보고서에 파일 경로와 최근 로그가 포함될 수 있습니다. 공유 전에 확인하세요."
            if korean else "This report may include local paths and recent logs. Review before sharing."
        ))
        layout.addWidget(buttons)

    def set_repeat_count(self, count: int) -> None:
        count = max(1, count)
        self.repeat_label.setText(
            f"최근 90초간 {count}회" if self._korean else
            f"{count} report(s) in the last 90 seconds"
        )
        self.exit_button.setVisible(count >= 2)

    def _choose_salvage(self) -> None:
        if self.salvage_button.isHidden():
            return
        self.salvage_requested = True
        self.accept()

    def _choose_exit(self, choice: str) -> None:
        self.exit_choice = choice
        self.accept()
