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
from app.models.project import CURRENT_VERSION, ProjectSettings
from app import __version__
from app.dialogs.new_project_dialog import CANVAS_PRESETS
from app.utils.i18n import Translator


def _timestamp_text(raw: str) -> str:
    """Format an existing persisted ISO timestamp; unknown remains unknown."""
    if not raw:
        return ""
    try:
        date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if date.tzinfo is not None:
        date = date.astimezone()
    return date.strftime("%Y-%m-%d %H:%M")


def _file_size_text(size: int | None) -> str:
    if size is None:
        return ""
    if size < 1024:
        return f"{size} B"
    for unit, divisor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= divisor:
            return f"{size / divisor:,.1f} {unit}"
    return f"{size} B"


class ProjectSettingsDialog(QDialog):
    """Edit settings that travel with the project instead of the application."""

    def __init__(
        self, settings: ProjectSettings, translator: Translator,
        canvas_thumbnail: QPixmap, parent: QWidget | None = None,
        canvas_size: tuple[int, int] = (1280, 720),
        project_path: Path | None = None,
        project_counts: dict[str, int] | None = None,
    ) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("other", "project_settings"), translator)
        self.setObjectName("projectSettingsDialog")
        self.translator = translator
        self.selected_settings = replace(settings)
        self.canvas_thumbnail = canvas_thumbnail
        self.original_canvas_size = canvas_size
        self.project_path = Path(project_path) if project_path is not None else None
        self.project_counts = dict(project_counts or {})
        self._file_size: int | None = None
        self._file_modified_at = ""
        if self.project_path is not None:
            try:
                stat = self.project_path.stat()
            except OSError:
                pass
            else:
                self._file_size = stat.st_size
                self._file_modified_at = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
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
        outer.addWidget(self.dialog_title_label)
        # A passive thumbnail at the top, independent of the active settings tab.
        self.thumbnail_group = QFrame()
        self.thumbnail_group.setObjectName("projectThumbnailHeader")
        self.thumbnail_group.setFrameShape(QFrame.Shape.StyledPanel)
        thumbnail_layout = QHBoxLayout(self.thumbnail_group)
        thumbnail_layout.setContentsMargins(14, 10, 14, 10)
        thumbnail_layout.setSpacing(16)
        self.thumbnail_preview = QLabel()
        self.thumbnail_preview.setFixedSize(224, 126)
        self.thumbnail_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_preview.setObjectName("thumbnailPreview")
        thumbnail_layout.addWidget(self.thumbnail_preview)
        thumbnail_text = QVBoxLayout()
        thumbnail_text.setSpacing(5)
        self.thumbnail_title_label = QLabel()
        self.thumbnail_title_label.setObjectName("panelTitle")
        self.thumbnail_project_label = QLabel(settings.title)
        self.thumbnail_project_label.setObjectName("projectThumbnailName")
        self.thumbnail_project_label.setWordWrap(True)
        self.thumbnail_hint_label = QLabel()
        self.thumbnail_hint_label.setObjectName("mutedLabel")
        self.thumbnail_hint_label.setWordWrap(True)
        thumbnail_text.addWidget(self.thumbnail_title_label)
        thumbnail_text.addWidget(self.thumbnail_project_label)
        thumbnail_text.addWidget(self.thumbnail_hint_label)
        thumbnail_text.addStretch()
        thumbnail_layout.addLayout(thumbnail_text, 1)
        outer.addWidget(self.thumbnail_group)
        # The active tab already tells the user which project settings are in view.
        self.subtitle_label.hide()
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
        self.metadata_names: dict[str, QLabel] = {}
        self.metadata_values: dict[str, QLabel] = {}
        self._metadata_fields = (
            "created_at", "created_version", "last_modified", "current_version",
            "format_version", "file_type", "file_size", "file_path",
            "tracks", "sources", "groups", "library",
        )
        for key in self._metadata_fields:
            heading = QLabel()
            value = QLabel()
            value.setObjectName("mutedLabel")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            details_form.addRow(heading, value)
            self.metadata_names[key] = heading
            self.metadata_values[key] = value
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
        for settings_form in (form, details_form, canvas_form):
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
        style_form(details_form)
        style_form(canvas_form)
        style_dialog_actions(self.buttons)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryButton")
        outer.addWidget(self.buttons)
        self.title_edit.textChanged.connect(
            lambda value: self.thumbnail_project_label.setText(value.strip() or "Untitled Project")
        )
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
        self.subtitle_label.setText("현재 프로젝트의 화면과 저장 방식을 설정합니다." if korean
                                    else "Configure the canvas and file storage for this project.")
        for index, names in enumerate((("프로젝트 정보", "Project"), ("캔버스", "Canvas"),
                                        ("파일 관리", "Files"))):
            self.tabs.setTabText(index, names[0 if korean else 1])
        self.identity_group.setTitle("프로젝트 정보" if korean else "Project information")
        self.title_label.setText("이름" if korean else "Name")
        self.author_label.setText("작성자" if korean else "Author")
        self.description_label.setText("설명" if korean else "Description")
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
        self.thumbnail_title_label.setText("프로젝트 미리보기" if korean else "Project preview")
        self.thumbnail_preview.setAccessibleName(
            "프로젝트 썸네일" if korean else "Project thumbnail"
        )
        self.thumbnail_hint_label.setText(
            "현재 캔버스를 캡처한 이미지 · 보기 전용" if korean else
            "Captured from the current canvas · Read-only"
        )
        self.details_group.setTitle("프로젝트 세부 정보" if korean else "Project details")
        labels = {
            "created_at": ("생성일", "Created"),
            "created_version": ("생성 프로그램 버전", "Created with"),
            "last_modified": ("파일 최종 수정", "File last modified"),
            "current_version": ("현재 프로그램 버전", "Current app version"),
            "format_version": ("현재 데이터 규격", "Current data schema"),
            "file_type": ("프로젝트 파일 형식", "File format"),
            "file_size": ("파일 크기", "File size"),
            "file_path": ("저장 위치", "Location"),
            "tracks": ("음원 트랙", "Audio tracks"),
            "sources": ("캔버스 요소", "Canvas sources"),
            "groups": ("요소 그룹", "Source groups"),
            "library": ("콘텐츠 항목", "Library items"),
        }
        missing = "기록 없음" if korean else "Not recorded"
        saved = "저장 전" if korean else "Not saved"
        location = str(self.project_path) if self.project_path is not None else ""
        if self.project_path is None:
            file_type = saved
        elif self.project_path.suffix.casefold() == ".pvsproj":
            file_type = "PVS project package (.pvsproj)"
        else:
            file_type = "기존 JSON 프로젝트" if korean else "Legacy JSON project"
        values = {
            "created_at": _timestamp_text(self.selected_settings.created_at),
            "created_version": self.selected_settings.created_app_version,
            "last_modified": self._file_modified_at,
            "current_version": __version__,
            "format_version": f"v{CURRENT_VERSION}",
            "file_type": file_type,
            "file_size": _file_size_text(self._file_size),
            "file_path": location or saved,
            "tracks": str(self.project_counts["tracks"]) if "tracks" in self.project_counts else "",
            "sources": str(self.project_counts["sources"]) if "sources" in self.project_counts else "",
            "groups": str(self.project_counts["groups"]) if "groups" in self.project_counts else "",
            "library": str(self.project_counts["library"]) if "library" in self.project_counts else "",
        }
        for key, (ko, en) in labels.items():
            self.metadata_names[key].setText(ko if korean else en)
            self.metadata_values[key].setText(values[key] or missing)
        self._refresh_thumbnail()
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            "저장" if korean else "Save"
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "취소" if korean else "Cancel"
        )
        self._update_canvas_summary()

    def _refresh_thumbnail(self) -> None:
        """Show an uneditable canvas snapshot, or a neutral missing-preview label."""
        if self.canvas_thumbnail.isNull():
            self.thumbnail_preview.setPixmap(QPixmap())
            self.thumbnail_preview.setText(
                "미리보기 없음" if self.translator.is_korean else "No preview"
            )
            return
        self.thumbnail_preview.setText("")
        self.thumbnail_preview.setPixmap(
            self.canvas_thumbnail.scaled(
                self.thumbnail_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
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
