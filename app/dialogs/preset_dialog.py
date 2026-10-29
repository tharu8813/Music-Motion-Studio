"""Design preset selection dialog with user-preset management."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.presets.preset_service import PresetDefinition
from app.presets.preset_preview import render_preset_thumbnail
from app.presets.user_preset_service import (
    PRESET_SUFFIX, UserPresetError, UserPresetService, all_presets,
)
from app.utils.i18n import Translator


_PREVIEW_WIDTH = 480
_PREVIEW_HEIGHT = 270
_SECTION_ROLE = Qt.ItemDataRole.UserRole + 1


class DesignPresetDialog(QDialog):
    """Select one design preset, and manage user-saved presets."""

    def __init__(self, translator: Translator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("canvas", "presets"), translator)
        self.translator = translator
        self._preview_cache: dict[str, QPixmap] = {}
        self._presets: list[PresetDefinition] = []
        self.setMinimumSize(760, 480)
        self.resize(940, 580)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        self.heading = QLabel()
        self.heading.setObjectName("panelTitle")
        self.description = QLabel()
        self.description.setObjectName("mutedLabel")
        self.description.setWordWrap(True)
        layout.addWidget(self.heading)
        layout.addWidget(self.description)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("presetList")
        self.list_widget.setFixedWidth(248)
        body.addWidget(self.list_widget)

        self.preview_label = QLabel()
        self.preview_label.setObjectName("presetPreview")
        self.preview_label.setFrameShape(QFrame.Shape.StyledPanel)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(_PREVIEW_WIDTH, _PREVIEW_HEIGHT)
        self.preview_label.setScaledContents(False)
        body.addWidget(self.preview_label, 1)
        layout.addLayout(body, 1)

        manage_row = QHBoxLayout()
        self.import_button = QPushButton()
        self.export_button = QPushButton()
        self.delete_button = QPushButton()
        self.import_button.clicked.connect(self._import_preset)
        self.export_button.clicked.connect(self._export_preset)
        self.delete_button.clicked.connect(self._delete_preset)
        manage_row.addWidget(self.import_button)
        manage_row.addWidget(self.export_button)
        manage_row.addWidget(self.delete_button)
        manage_row.addStretch(1)
        layout.addLayout(manage_row)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self.accept
        )
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.list_widget.currentRowChanged.connect(self._update_description)
        self.list_widget.itemDoubleClicked.connect(self._apply_from_item)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()

    # -- population -----------------------------------------------------------

    def _rebuild_list(self, select_id: str | None) -> None:
        korean = self.translator.is_korean
        self._presets = all_presets()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        seen_user_header = False
        for preset in self._presets:
            if preset.editable and not seen_user_header:
                seen_user_header = True
                header = QListWidgetItem("— " + ("내 프리셋" if korean else "My presets"))
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setData(_SECTION_ROLE, "header")
                self.list_widget.addItem(header)
            item = QListWidgetItem(preset.name(self.translator.language.value))
            item.setData(Qt.ItemDataRole.UserRole, preset.identifier)
            self.list_widget.addItem(item)
            if preset.identifier == select_id:
                self.list_widget.setCurrentItem(item)
        self.list_widget.blockSignals(False)
        if self.list_widget.currentItem() is None:
            for row in range(self.list_widget.count()):
                if self.list_widget.item(row).data(Qt.ItemDataRole.UserRole):
                    self.list_widget.setCurrentRow(row)
                    break
        self._update_description()

    def retranslate(self) -> None:
        """Refresh translated strings while keeping the current selection."""
        korean = self.translator.is_korean
        selected = self.selected_preset.identifier if self.selected_preset else None
        self.setWindowTitle("디자인 프리셋" if korean else "Design Presets")
        self.heading.setText("디자인 프리셋 선택" if korean else "Choose a design preset")
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).setText(
            "적용" if korean else "Apply"
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "취소" if korean else "Cancel"
        )
        self.import_button.setText("가져오기…" if korean else "Import…")
        self.export_button.setText("내보내기…" if korean else "Export…")
        self.delete_button.setText("삭제" if korean else "Delete")
        self._rebuild_list(selected)

    # -- selection ----------------------------------------------------------

    @property
    def selected_preset(self) -> PresetDefinition | None:
        """Return the selected preset definition, or None on a section header."""
        current = self.list_widget.currentItem()
        if current is None:
            return None
        identifier = current.data(Qt.ItemDataRole.UserRole)
        return next(
            (preset for preset in self._presets if preset.identifier == identifier),
            None,
        )

    def _apply_from_item(self, item: QListWidgetItem) -> None:
        if item.data(Qt.ItemDataRole.UserRole):
            self.accept()

    def _preview_pixmap(self, preset: PresetDefinition) -> QPixmap:
        cached = self._preview_cache.get(preset.identifier)
        if cached is None:
            cached = render_preset_thumbnail(
                preset.builder(), _PREVIEW_WIDTH, _PREVIEW_HEIGHT,
            )
            self._preview_cache[preset.identifier] = cached
        return cached

    def _update_description(self) -> None:
        preset = self.selected_preset
        self.description.setText(
            preset.description(self.translator.language.value) if preset else ""
        )
        if preset is None:
            self.preview_label.clear()
        else:
            self.preview_label.setPixmap(
                self._preview_pixmap(preset).scaled(
                    self.preview_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        self.button_box.button(QDialogButtonBox.StandardButton.Apply).setEnabled(
            preset is not None
        )
        self.export_button.setEnabled(preset is not None and preset.editable)
        self.delete_button.setEnabled(preset is not None and preset.editable)

    # -- user preset management -------------------------------------------

    def _import_preset(self) -> None:
        korean = self.translator.is_korean
        path, _ = QFileDialog.getOpenFileName(
            self,
            "프리셋 가져오기" if korean else "Import preset",
            "",
            f"Music Motion Studio preset (*{PRESET_SUFFIX});;JSON (*.json)",
        )
        if not path:
            return
        try:
            preset = UserPresetService.import_from(path)
        except UserPresetError as error:
            QMessageBox.warning(
                self, "가져오기 실패" if korean else "Import failed", str(error),
            )
            return
        self._rebuild_list(preset.identifier)

    def _export_preset(self) -> None:
        preset = self.selected_preset
        if preset is None or not preset.editable:
            return
        korean = self.translator.is_korean
        path, _ = QFileDialog.getSaveFileName(
            self,
            "프리셋 내보내기" if korean else "Export preset",
            f"{preset.name(self.translator.language.value)}{PRESET_SUFFIX}",
            f"Music Motion Studio preset (*{PRESET_SUFFIX})",
        )
        if not path:
            return
        try:
            UserPresetService.export_to(preset.identifier, path)
        except (UserPresetError, OSError) as error:
            QMessageBox.warning(
                self, "내보내기 실패" if korean else "Export failed", str(error),
            )

    def _delete_preset(self) -> None:
        preset = self.selected_preset
        if preset is None or not preset.editable:
            return
        korean = self.translator.is_korean
        confirm = QMessageBox.question(
            self,
            "프리셋 삭제" if korean else "Delete preset",
            (f"'{preset.name('ko')}' 프리셋을 삭제할까요?"
             if korean else
             f"Delete the preset '{preset.name('en')}'?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        UserPresetService.delete(preset.identifier)
        self._preview_cache.pop(preset.identifier, None)
        self._rebuild_list(None)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self.selected_preset is not None:
            self._update_description()


# Keep third-party imports compatible with releases that exposed this name.
PresetDialog = DesignPresetDialog
