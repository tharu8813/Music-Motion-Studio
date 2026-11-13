"""Dialog for generating YouTube timestamp and playlist companion files."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.ui.form_layout import style_form, style_dialog_actions
from app.services.playlist_export_service import TimestampFormat
from app.utils.i18n import Translator


class PlaylistExportDialog(QDialog):
    """Collect the output folder and timestamp appearance for companion files."""

    def __init__(self, default_directory: Path, translator: Translator,
                 parent: object | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("canvas", "playlist"), translator)
        self.translator = translator
        self.setMinimumWidth(460)
        self.directory_edit = QLineEdit(str(default_directory))
        self.directory_edit.setClearButtonEnabled(True)
        self.browse_button = QPushButton()
        self.standard_radio = QRadioButton()
        self.bracketed_radio = QRadioButton()
        self.standard_radio.setChecked(True)
        self.copy_check = QCheckBox()
        self.copy_check.setChecked(True)
        self.preview_label = QLabel()
        self.preview_label.setObjectName("mutedLabel")
        self.preview_label.setWordWrap(True)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        style_dialog_actions(self.button_box)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.browse_button.clicked.connect(self._choose_directory)
        self.standard_radio.toggled.connect(self._update_preview)

        directory_row = QHBoxLayout()
        directory_row.addWidget(self.directory_edit, 1)
        directory_row.addWidget(self.browse_button)
        form = QFormLayout()
        self.output_label = QLabel()
        self.format_label = QLabel()
        style_form(form)
        form.addRow(self.output_label, directory_row)
        form.addRow(self.format_label, self.standard_radio)
        form.addRow("", self.bracketed_radio)
        form.addRow("", self.copy_check)
        layout = QVBoxLayout(self)
        layout.addWidget(self._heading())
        layout.addWidget(self._description())
        layout.addLayout(form)
        # The two radio choices already show exact output examples.
        self.preview_label.hide()
        layout.addWidget(self.button_box)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()

    @property
    def output_directory(self) -> Path:
        """Return the user-selected target directory."""
        return Path(self.directory_edit.text().strip() or Path.cwd())

    @property
    def timestamp_format(self) -> TimestampFormat:
        """Return the selected timestamp notation."""
        return (
            TimestampFormat.BRACKETED
            if self.bracketed_radio.isChecked()
            else TimestampFormat.STANDARD
        )

    @property
    def copy_description(self) -> bool:
        """Return whether description text should be placed on the clipboard."""
        return self.copy_check.isChecked()

    def _choose_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, self._choose_folder_title(), str(self.output_directory)
        )
        if selected:
            self.directory_edit.setText(selected)

    def _update_preview(self) -> None:
        self.preview_label.setText(
            "[00:00] Artist - Title"
            if self.bracketed_radio.isChecked()
            else "00:00 Artist - Title"
        )

    def retranslate(self) -> None:
        """Refresh static dialog text when the application language changes."""
        korean = self.translator.is_korean
        self.setWindowTitle("타임라인 내보내기" if korean else "Export timeline")
        self._heading_label.setText("타임라인 내보내기" if korean else "Export timeline")
        self._description_label.setText(
            "description.txt와 playlist.csv를 생성합니다."
            if korean else "Creates description.txt and playlist.csv."
        )
        self.browse_button.setText("찾아보기" if korean else "Browse")
        self.output_label.setText("저장 폴더" if korean else "Output folder")
        self.format_label.setText("타임스탬프 형식" if korean else "Timestamp format")
        self.standard_radio.setText("00:00 Artist - Title")
        self.bracketed_radio.setText("[00:00] Artist - Title")
        self.copy_check.setText(
            "설명문을 클립보드에 자동 복사" if korean else "Copy description to clipboard"
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText(
            "타임라인 내보내기" if korean else "Export timeline"
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "취소" if korean else "Cancel"
        )
        self._update_preview()

    def _heading(self) -> QLabel:
        self._heading_label = QLabel()
        self._heading_label.setObjectName("dialogTitle")
        return self._heading_label

    def _description(self) -> QLabel:
        self._description_label = QLabel()
        self._description_label.setObjectName("mutedLabel")
        self._description_label.setWordWrap(True)
        return self._description_label

    def _choose_folder_title(self) -> str:
        return "저장 폴더 선택" if self.translator.is_korean else "Choose output folder"
