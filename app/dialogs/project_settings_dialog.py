"""Project identity, packaging, and thumbnail settings."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from math import gcd
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QMessageBox, QRadioButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.ui.form_layout import style_form, style_dialog_actions
from app import __version__
from app.models.project import CURRENT_VERSION, ProjectDocument, ProjectSettings
from app.dialogs.new_project_dialog import CANVAS_PRESETS
from app.utils.i18n import Translator


class ProjectSettingsDialog(QDialog):
    """Edit settings that travel with the project instead of the application."""

    def __init__(
        self, settings: ProjectSettings, translator: Translator,
        canvas_thumbnail: QPixmap, parent: QWidget | None = None,
        canvas_size: tuple[int, int] = (1280, 720),
        project_document: ProjectDocument | None = None,
        project_path: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("other", "project_settings"), translator)
        self.setObjectName("projectSettingsDialog")
        self.translator = translator
        self.selected_settings = replace(settings)
        self.canvas_thumbnail = canvas_thumbnail
        self.original_canvas_size = canvas_size
        self.project_document = project_document
        self.project_path = Path(project_path) if project_path else None
        self.setMinimumSize(620, 420)
        self.resize(820, 620)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)
        self.dialog_title_label = QLabel()
        self.dialog_title_label.setObjectName("dialogTitle")
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("mutedLabel")
        self.subtitle_label.setWordWrap(True)
        # Keep the live canvas capture prominent and view-only, outside the tabs.
        header = QHBoxLayout()
        header.setSpacing(20)
        title_stack = QVBoxLayout()
        title_stack.setSpacing(4)
        title_stack.addWidget(self.dialog_title_label)
        title_stack.addWidget(self.subtitle_label)
        title_stack.addStretch()
        header.addLayout(title_stack, 1)
        self.thumbnail_group = QWidget()
        preview_layout = QVBoxLayout(self.thumbnail_group)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(5)
        self.thumbnail_caption = QLabel()
        self.thumbnail_caption.setObjectName("mutedLabel")
        self.thumbnail_preview = QLabel()
        self.thumbnail_preview.setFixedSize(208, 117)
        self.thumbnail_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_preview.setObjectName("thumbnailPreview")
        self.thumbnail_preview.setAccessibleName("Project canvas preview")
        preview_layout.addWidget(self.thumbnail_caption)
        preview_layout.addWidget(self.thumbnail_preview)
        header.addWidget(self.thumbnail_group, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")
        self.tabs.setDocumentMode(True)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.scroll_area.setWidget(body)
        self.tabs.addTab(self.scroll_area, "")
        outer.addWidget(self.tabs, 1)
        root = QVBoxLayout(body)
        root.setContentsMargins(14, 16, 14, 14)
        root.setSpacing(24)

        identity_group = QGroupBox()
        self.identity_group = identity_group
        form = QFormLayout(identity_group)
        self.title_edit = QLineEdit(settings.title)
        self.author_edit = QLineEdit(settings.author)
        self.title_edit.setClearButtonEnabled(True)
        self.author_edit.setClearButtonEnabled(True)
        self.description_edit = QPlainTextEdit(settings.description)
        self.description_edit.setMaximumHeight(84)
        self.description_edit.setTabChangesFocus(True)
        self.title_label = QLabel()
        self.author_label = QLabel()
        self.description_label = QLabel()
        form.addRow(self.title_label, self.title_edit)
        form.addRow(self.author_label, self.author_edit)
        form.addRow(self.description_label, self.description_edit)
        root.addWidget(identity_group)

        self.details_group = QGroupBox()
        details_form = QFormLayout(self.details_group)
        details_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        details_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        details_form.setHorizontalSpacing(24)
        details_form.setVerticalSpacing(10)
        self.details_labels: dict[str, QLabel] = {}
        self.details_values: dict[str, QLabel] = {}
        for key in (
            "created_at", "modified_at", "created_app_version", "current_app_version",
            "project_format", "format_version", "file_path", "file_size",
            "canvas_size", "track_count", "source_count", "group_count", "content_count",
        ):
            label, value = QLabel(), QLabel()
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            value.setObjectName("projectDetailValue")
            details_form.addRow(label, value)
            self.details_labels[key] = label
            self.details_values[key] = value
        self._populate_project_details()
        root.addWidget(self.details_group)

        self.canvas_group = QGroupBox()
        canvas_form = QFormLayout(self.canvas_group)
        self.canvas_preset_label = QLabel()
        self.canvas_preset_combo = QComboBox()
        for ratio, width, height in CANVAS_PRESETS:
            self.canvas_preset_combo.addItem(ratio, (width, height))
        self.canvas_preset_combo.addItem("", None)
        self.canvas_width_label = QLabel()
        self.canvas_width_spin = QSpinBox()
        self.canvas_width_spin.setRange(64, 16_384)
        self.canvas_width_spin.setSingleStep(2)
        self.canvas_width_spin.setValue(canvas_size[0])
        self.canvas_height_label = QLabel()
        self.canvas_height_spin = QSpinBox()
        self.canvas_height_spin.setRange(64, 16_384)
        self.canvas_height_spin.setSingleStep(2)
        self.canvas_height_spin.setValue(canvas_size[1])
        self.canvas_summary = QLabel()
        self.canvas_summary.setObjectName("mutedLabel")
        self.canvas_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scale_content_radio = QRadioButton()
        self.keep_content_radio = QRadioButton()
        resize_buttons = QButtonGroup(self)
        resize_buttons.addButton(self.scale_content_radio)
        resize_buttons.addButton(self.keep_content_radio)
        self.scale_content_radio.setChecked(True)
        canvas_form.addRow(self.canvas_preset_label, self.canvas_preset_combo)
        canvas_form.addRow(self.canvas_width_label, self.canvas_width_spin)
        canvas_form.addRow(self.canvas_height_label, self.canvas_height_spin)
        canvas_form.addRow("", self.canvas_summary)
        canvas_form.addRow("", self.scale_content_radio)
        canvas_form.addRow("", self.keep_content_radio)
        root.addWidget(self.canvas_group)

        self.content_group = QGroupBox()
        content_layout = QVBoxLayout(self.content_group)
        self.embed_radio = QRadioButton()
        self.reference_radio = QRadioButton()
        content_buttons = QButtonGroup(self)
        content_buttons.addButton(self.embed_radio)
        content_buttons.addButton(self.reference_radio)
        self.embed_help = QLabel()
        self.reference_help = QLabel()
        for label in (self.embed_help, self.reference_help):
            label.setObjectName("mutedLabel")
            label.setWordWrap(True)
            label.setContentsMargins(24, 0, 0, 4)
        content_layout.addWidget(self.embed_radio)
        content_layout.addWidget(self.embed_help)
        content_layout.addWidget(self.reference_radio)
        content_layout.addWidget(self.reference_help)
        (self.embed_radio if settings.content_mode == "embed" else self.reference_radio).setChecked(True)
        root.addWidget(self.content_group)

        root.addStretch(1)
        for group in (self.canvas_group, self.content_group):
            root.removeWidget(group)
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QFrame.Shape.NoFrame)
            page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            content = QWidget()
            page_layout = QVBoxLayout(content)
            page_layout.setContentsMargins(14, 16, 14, 14)
            page_layout.addWidget(group)
            page_layout.addStretch(1)
            page.setWidget(content)
            self.tabs.addTab(page, "")
        for settings_form in (form, canvas_form):
            settings_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            settings_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            settings_form.setHorizontalSpacing(24)
            settings_form.setVerticalSpacing(14)
        for label, field in ((self.title_label, self.title_edit), (self.author_label, self.author_edit),
                             (self.description_label, self.description_edit),
                             (self.canvas_preset_label, self.canvas_preset_combo),
                             (self.canvas_width_label, self.canvas_width_spin),
                             (self.canvas_height_label, self.canvas_height_spin)):
            label.setBuddy(field)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        style_form(form)
        style_form(canvas_form)
        style_dialog_actions(self.buttons)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryButton")
        outer.addWidget(self.buttons)
        self.canvas_preset_combo.currentIndexChanged.connect(
            self._canvas_preset_changed
        )
        self.canvas_width_spin.valueChanged.connect(self._update_canvas_summary)
        self.canvas_height_spin.valueChanged.connect(self._update_canvas_summary)
        translator.language_changed.connect(self.retranslate)
        # By value: findData() compared the stored Python tuples by identity, so a
        # preset-sized canvas always showed up as "Custom".
        matching = next(
            (index for index in range(self.canvas_preset_combo.count())
             if self.canvas_preset_combo.itemData(index) == tuple(canvas_size)),
            self.canvas_preset_combo.count() - 1,
        )
        self.canvas_preset_combo.setCurrentIndex(matching)
        self.retranslate()
        self._canvas_preset_changed(self.canvas_preset_combo.currentIndex())
        self._refresh_thumbnail()

    @staticmethod
    def _display_timestamp(value: str, korean: bool) -> str:
        if not value:
            return "정보 없음" if korean else "Unknown"
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is not None:
                date = date.astimezone()
            return date.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return "정보 없음" if korean else "Unknown"

    def _populate_project_details(self) -> None:
        korean = self.translator.is_korean
        unknown = "정보 없음" if korean else "Unknown"
        not_saved = "저장 전" if korean else "Not saved yet"
        settings = self.selected_settings
        values = self.details_values
        values["created_at"].setText(self._display_timestamp(settings.created_at, korean))
        values["modified_at"].setText(
            self._display_timestamp(settings.modified_at, korean) if self.project_path else not_saved
        )
        values["created_app_version"].setText(settings.created_app_version or unknown)
        values["current_app_version"].setText(__version__)
        values["format_version"].setText(f"v{CURRENT_VERSION}")
        path = self.project_path
        exists = bool(path and path.is_file())
        if not path:
            values["project_format"].setText(not_saved)
        elif path.suffix.lower() == ".pvsproj":
            values["project_format"].setText("Music Motion Studio (.pvsproj)")
        else:
            values["project_format"].setText("Legacy JSON (.json)")
        values["file_path"].setText(str(path) if path else not_saved)
        if exists:
            try:
                byte_size = path.stat().st_size
                values["file_size"].setText(
                    f"{byte_size / 1024 / 1024:.2f} MB" if byte_size >= 1024 * 1024
                    else f"{byte_size / 1024:.1f} KB" if byte_size >= 1024
                    else f"{byte_size} B"
                )
            except OSError:
                values["file_size"].setText(unknown)
        else:
            values["file_size"].setText(unknown if path else not_saved)
        width, height = self.original_canvas_size
        values["canvas_size"].setText(f"{width} × {height}")
        document = self.project_document
        for key, collection in (
            ("track_count", "playlist"),
            ("source_count", "sources"),
            ("group_count", "groups"),
            ("content_count", "content_library"),
        ):
            values[key].setText(str(len(getattr(document, collection))) if document is not None else unknown)

    @property
    def selected_canvas_size(self) -> tuple[int, int]:
        return self.canvas_width_spin.value(), self.canvas_height_spin.value()

    @property
    def scale_canvas_content(self) -> bool:
        return self.scale_content_radio.isChecked()

    def _canvas_preset_changed(self, index: int) -> None:
        size = self.canvas_preset_combo.itemData(index)
        custom = size is None
        self.canvas_width_spin.setEnabled(custom)
        self.canvas_height_spin.setEnabled(custom)
        if size is not None:
            self.canvas_width_spin.setValue(size[0])
            self.canvas_height_spin.setValue(size[1])
        self._update_canvas_summary()

    def _update_canvas_summary(self) -> None:
        width, height = self.selected_canvas_size
        divisor = gcd(width, height)
        ratio = f"{width // divisor}:{height // divisor}"
        self.canvas_summary.setText(
            f"캔버스 {width} × {height} · 화면 비율 {ratio}"
            if self.translator.is_korean else
            f"Canvas {width} × {height} · Aspect ratio {ratio}"
        )

    def retranslate(self) -> None:
        korean = self.translator.is_korean
        self.setWindowTitle("프로젝트 설정" if korean else "Project settings")
        self.dialog_title_label.setText("프로젝트 설정" if korean else "Project settings")
        self.subtitle_label.setText("현재 캔버스와 프로젝트의 설정 및 기록" if korean
                                    else "Canvas, settings and project details")
        for index, names in enumerate((("프로젝트 정보", "Project"), ("캔버스", "Canvas"),
                                        ("파일 관리", "Files"))):
            self.tabs.setTabText(index, names[0 if korean else 1])
        self.identity_group.setTitle("프로젝트 정보" if korean else "Project information")
        self.title_label.setText("이름" if korean else "Name")
        self.author_label.setText("작성자" if korean else "Author")
        self.description_label.setText("설명" if korean else "Description")
        self.thumbnail_caption.setText("현재 캔버스 미리보기" if korean else "Current canvas preview")
        self.thumbnail_preview.setAccessibleName("현재 캔버스 미리보기" if korean else "Current canvas preview")
        self.details_group.setTitle("프로젝트 상세 정보" if korean else "Project details")
        detail_names = {
            "created_at": ("생성일", "Created"),
            "modified_at": ("기록된 수정일", "Recorded modification"),
            "created_app_version": ("생성 당시 프로그램 버전", "Created with"),
            "current_app_version": ("현재 프로그램 버전", "Current app version"),
            "project_format": ("저장 형식", "File format"),
            "format_version": ("데이터 형식 버전", "Data format version"),
            "file_path": ("프로젝트 파일", "Project file"),
            "file_size": ("파일 크기", "File size"),
            "canvas_size": ("캔버스 크기", "Canvas size"),
            "track_count": ("음악 트랙", "Audio tracks"),
            "source_count": ("캔버스 요소", "Canvas sources"),
            "group_count": ("레이어 그룹", "Layer groups"),
            "content_count": ("콘텐츠 항목", "Content items"),
        }
        for key, (ko, en) in detail_names.items():
            self.details_labels[key].setText(ko if korean else en)
        self._populate_project_details()
        self.canvas_group.setTitle("화면 비율 및 캔버스" if korean else "Aspect ratio and canvas")
        self.canvas_preset_label.setText("화면 비율" if korean else "Aspect ratio")
        self.canvas_width_label.setText("너비" if korean else "Width")
        self.canvas_height_label.setText("높이" if korean else "Height")
        self.canvas_preset_combo.setItemText(
            self.canvas_preset_combo.count() - 1,
            "사용자 지정" if korean else "Custom",
        )
        self.scale_content_radio.setText(
            "요소 위치와 크기도 함께 조정 (권장)"
            if korean else "Reposition and scale sources (recommended)"
        )
        self.keep_content_radio.setText(
            "캔버스만 변경하고 요소 위치와 크기 유지"
            if korean else "Change only the canvas and keep source geometry"
        )
        self.content_group.setTitle("콘텐츠 저장 방식" if korean else "Content storage")
        self.embed_radio.setText("추가한 콘텐츠를 프로젝트에 포함" if korean else "Add imported content to the project")
        self.embed_help.setText(
            "이미지, 음원, 폰트와 가사를 패키지 안에 복사합니다. 파일은 커지지만 다른 PC에서도 안전하게 열립니다."
            if korean else "Copies images, audio, fonts, and lyrics into the package. The file is larger but portable."
        )
        self.reference_radio.setText("외부 콘텐츠를 프로젝트에서 참조" if korean else "Reference content from its external location")
        self.reference_help.setText(
            "원본 파일 경로를 사용합니다. 프로젝트는 작지만 원본을 이동하면 다시 연결해야 합니다."
            if korean else "Keeps original file paths. The project stays small, but moved files must be relinked."
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "저장" if korean else "Save"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "취소" if korean else "Cancel"
        )
        self._update_canvas_summary()

    def _refresh_thumbnail(self) -> None:
        if self.canvas_thumbnail.isNull():
            self.thumbnail_preview.setPixmap(QPixmap())
            self.thumbnail_preview.setText("미리보기 없음" if self.translator.is_korean else "No preview")
            return
        self.thumbnail_preview.setPixmap(
            self.canvas_thumbnail.scaled(
                self.thumbnail_preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _accept(self) -> None:
        if self.selected_canvas_size != self.original_canvas_size:
            korean = self.translator.is_korean
            answer = QMessageBox.warning(
                self,
                "캔버스 크기 변경" if korean else "Change canvas size",
                (
                    f"캔버스를 {self.original_canvas_size[0]} × {self.original_canvas_size[1]}에서 "
                    f"{self.selected_canvas_size[0]} × {self.selected_canvas_size[1]}(으)로 변경합니다.\n\n"
                    "요소 배치가 달라질 수 있으며 변경 후 Ctrl+Z로 되돌릴 수 있습니다. 계속할까요?"
                    if korean else
                    f"Change the canvas from {self.original_canvas_size[0]} × {self.original_canvas_size[1]} "
                    f"to {self.selected_canvas_size[0]} × {self.selected_canvas_size[1]}?\n\n"
                    "Source layout may change. You can undo afterward with Ctrl+Z."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.selected_settings.title = self.title_edit.text().strip() or "Untitled Project"
        self.selected_settings.author = self.author_edit.text().strip()
        self.selected_settings.description = self.description_edit.toPlainText().strip()
        self.selected_settings.content_mode = "embed" if self.embed_radio.isChecked() else "reference"
        self.selected_settings.thumbnail_mode = "canvas"
        self.selected_settings.thumbnail_path = ""
        self.accept()
