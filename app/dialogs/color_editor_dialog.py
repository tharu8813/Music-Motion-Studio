"""Music Motion Studio color editor with localized personal-color controls."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.models.playlist import PlaylistTrack
from app.models.source import Shadow, Source
from app.preview.album_art import adjust_personal_color, extract_track_personal_color
from app.utils.i18n import Translator


PRESET_COLORS = (
    "#FFFFFF", "#D7DEE8", "#8795A8", "#263042", "#090D14", "#000000",
    "#FF5C5C", "#FF9F43", "#FFD93D", "#55D187", "#36CFC9", "#4DA3FF",
    "#6C7CFF", "#A66CFF", "#E76BC4", "#F5A9B8", "#C99868", "#7C3AED",
)


class ColorPreview(QWidget):
    """Paint original/current swatches over a transparency checkerboard."""

    def __init__(self, original: QColor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.original = QColor(original)
        self.current = QColor(original)
        self.setMinimumHeight(92)
        self.setObjectName("colorPreview")

    def set_current(self, color: QColor) -> None:
        self.current = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        painter.setPen(Qt.PenStyle.NoPen)
        cell = 10
        for y in range(0, self.height(), cell):
            for x in range(0, self.width(), cell):
                painter.setBrush(QColor("#D7DCE3") if (x // cell + y // cell) % 2 else QColor("#F5F6F8"))
                painter.drawRect(x, y, cell, cell)
        half = rect.width() / 2.0
        painter.setBrush(self.original)
        painter.drawRoundedRect(QRectF(rect.x(), rect.y(), half + 5.0, rect.height()), 10.0, 10.0)
        painter.setBrush(self.current)
        painter.drawRoundedRect(QRectF(rect.x() + half - 5.0, rect.y(), half + 5.0, rect.height()), 10.0, 10.0)
        border = self.palette().color(self.foregroundRole())
        border.setAlpha(75)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(border)
        painter.drawRoundedRect(rect, 10.0, 10.0)
        painter.end()


class ColorEditorDialog(QDialog):
    """Edit one source color and its track-aware personal-color policy."""

    def __init__(
        self,
        initial_color: QColor,
        source: Source | Shadow,
        translator: Translator,
        title: str,
        parent: QWidget | None = None,
        tracks: list[PlaylistTrack] | None = None,
        show_personal_color: bool = True,
    ) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("canvas", "properties"), translator)
        self.translator = translator
        self._korean = translator.is_korean
        self._tracks = list(tracks or [])
        self._show_personal_color = show_personal_color
        self._initial_color = QColor(initial_color)
        if not self._initial_color.isValid():
            self._initial_color = QColor("#FFFFFF")
        self._color = QColor(self._initial_color)
        self._syncing = False
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(620, 570)
        self.resize(680, 650)
        self.setObjectName("colorEditorDialog")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)
        heading = QLabel(title)
        heading.setObjectName("dialogTitle")
        heading.setStyleSheet("font-size: 21px; font-weight: 600;")
        root.addWidget(heading)
        description = QLabel(
            ("색상을 직접 조절하거나 현재 곡의 앨범 커버 색상과 연결할 수 있습니다."
             if show_personal_color else "이 항목의 색상과 투명도를 조절합니다.")
            if self._korean else
            ("Choose a color directly or link it to the current track artwork."
             if show_personal_color else "Adjust this color and its opacity.")
        )
        description.setObjectName("mutedLabel")
        description.setWordWrap(True)
        root.addWidget(description)

        tabs = QTabWidget()
        color_page = QWidget()
        self.color_page = color_page
        color_page_layout = QVBoxLayout(color_page)
        color_page_layout.setContentsMargins(10, 12, 10, 10)
        color_page_layout.setSpacing(12)
        personal_page = QWidget()
        personal_page_layout = QVBoxLayout(personal_page)
        personal_page_layout.setContentsMargins(10, 12, 10, 10)
        tabs.addTab(color_page, "색상" if self._korean else "Color")
        if show_personal_color:
            tabs.setTabToolTip(0, "퍼스널 컬러를 끄면 직접 색상을 조절할 수 있습니다." if self._korean else
                               "Turn off personal color to edit the color manually.")
        tabs.addTab(personal_page, "퍼스널 컬러" if self._korean else "Personal color")
        tabs.setTabVisible(1, show_personal_color)
        tabs.tabBar().setVisible(show_personal_color)
        root.addWidget(tabs, 1)

        preview_card = QFrame()
        preview_card.setObjectName("settingsCard")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(14, 14, 14, 14)
        preview_labels = QHBoxLayout()
        preview_labels.addWidget(QLabel("기존 색상" if self._korean else "Original"))
        preview_labels.addStretch(1)
        preview_labels.addWidget(QLabel("새 색상" if self._korean else "New color"))
        preview_layout.addLayout(preview_labels)
        self.preview = ColorPreview(self._initial_color)
        preview_layout.addWidget(self.preview)
        color_page_layout.addWidget(preview_card)

        palette_group = QGroupBox("빠른 색상" if self._korean else "Quick colors")
        palette_grid = QGridLayout(palette_group)
        palette_grid.setContentsMargins(12, 12, 12, 12)
        palette_grid.setHorizontalSpacing(8)
        palette_grid.setVerticalSpacing(8)
        for index, value in enumerate(PRESET_COLORS):
            button = QPushButton()
            button.setFixedSize(32, 32)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(value)
            button.setAccessibleName(
                f"색상 {value}" if self._korean else f"Color {value}"
            )
            button.setStyleSheet(
                f"background:{value}; border:1px solid rgba(120,130,145,0.7); "
                "border-radius:8px;"
            )
            button.clicked.connect(
                lambda _checked=False, color=value: self._use_preset(color)
            )
            palette_grid.addWidget(button, index // 9, index % 9)
        color_page_layout.addWidget(palette_group)

        controls = QGroupBox("세부 조정" if self._korean else "Fine adjustments")
        form = QFormLayout(controls)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.hex_edit = QLineEdit()
        self.hex_edit.setPlaceholderText("#RRGGBB / #AARRGGBB")
        self.hex_edit.setMaxLength(9)
        self.hex_edit.editingFinished.connect(self._apply_hex)
        form.addRow("HEX", self.hex_edit)
        self.hue_slider, self.hue_spin = self._slider_row(0, 359, "°")
        self.saturation_slider, self.saturation_spin = self._slider_row(0, 100, "%")
        self.value_slider, self.value_spin = self._slider_row(0, 100, "%")
        self.alpha_slider, self.alpha_spin = self._slider_row(0, 100, "%")
        form.addRow("색조" if self._korean else "Hue", self._row(self.hue_slider, self.hue_spin))
        form.addRow("채도" if self._korean else "Saturation", self._row(self.saturation_slider, self.saturation_spin))
        form.addRow("밝기" if self._korean else "Brightness", self._row(self.value_slider, self.value_spin))
        form.addRow("투명도" if self._korean else "Opacity", self._row(self.alpha_slider, self.alpha_spin))
        color_page_layout.addWidget(controls)
        color_page_layout.addStretch(1)

        personal_group = QGroupBox("현재 트랙 퍼스널 컬러" if self._korean else "Current-track personal color")
        personal_group_layout = QVBoxLayout(personal_group)
        self.personal_check = QCheckBox(
            "현재 트랙에 퍼스널 컬러 사용하기"
            if self._korean else "Use current track personal color"
        )
        self.personal_check.setChecked(source.personal_color_enabled)
        personal_group_layout.addWidget(self.personal_check)
        personal_hint = QLabel(
            "미리보기와 내보내기에서 앨범 커버의 대표 강조색을 사용합니다. 커버가 없으면 위에서 지정한 색상을 유지합니다."
            if self._korean else
            "Preview and export use a dominant artwork accent. The color above remains the fallback when artwork is unavailable."
        )
        personal_hint.setObjectName("mutedLabel")
        personal_hint.setWordWrap(True)
        if isinstance(source, Shadow):
            personal_hint.setText(("그림자 전용 퍼스널 컬러 설정입니다.\n" if self._korean else
                                   "These personal-color settings apply to the shadow.\n") + personal_hint.text())
        personal_group_layout.addWidget(personal_hint)
        personal_form = QFormLayout()
        self.personal_brightness = self._spin(-100, 100, "%", source.personal_color_brightness)
        self.personal_saturation = self._spin(-100, 100, "%", source.personal_color_saturation)
        self.personal_hue = self._spin(-180, 180, "°", source.personal_color_hue_shift)
        self.personal_strength = self._spin(0, 100, "%", source.personal_color_strength * 100.0)
        personal_form.addRow("밝기 보정" if self._korean else "Brightness adjustment", self.personal_brightness)
        personal_form.addRow("채도 보정" if self._korean else "Saturation adjustment", self.personal_saturation)
        personal_form.addRow("색조 이동" if self._korean else "Hue shift", self.personal_hue)
        personal_form.addRow("적용 강도" if self._korean else "Strength", self.personal_strength)
        personal_group_layout.addLayout(personal_form)
        personal_page_layout.addWidget(personal_group)

        self.personal_preview_group = QGroupBox(
            "곡별 미리보기" if self._korean else "Per-track preview"
        )
        preview_layout = QFormLayout(self.personal_preview_group)
        self.personal_track_combo = QComboBox()
        for track in self._tracks:
            self.personal_track_combo.addItem(track.title or track.filename)
        preview_layout.addRow(
            "미리볼 곡" if self._korean else "Preview track",
            self.personal_track_combo,
        )
        self.personal_preview_swatch = QFrame()
        self.personal_preview_swatch.setObjectName("colorPreview")
        self.personal_preview_swatch.setMinimumHeight(48)
        preview_layout.addRow(
            "결과 색상" if self._korean else "Result color",
            self.personal_preview_swatch,
        )
        self.personal_preview_note = QLabel()
        self.personal_preview_note.setObjectName("mutedLabel")
        self.personal_preview_note.setWordWrap(True)
        preview_layout.addRow("", self.personal_preview_note)
        if not self._tracks:
            self.personal_preview_group.setVisible(False)
        personal_page_layout.addWidget(self.personal_preview_group)
        personal_page_layout.addStretch(1)

        footer = QHBoxLayout()
        self.reset_button = QPushButton("처음 색상" if self._korean else "Reset color")
        self.reset_button.clicked.connect(self._reset_color)
        footer.addWidget(self.reset_button)
        footer.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("확인" if self._korean else "Apply")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소" if self._korean else "Cancel")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        root.addLayout(footer)

        self.personal_check.toggled.connect(self._sync_personal_controls)
        for spin in (
            self.personal_brightness, self.personal_saturation,
            self.personal_hue, self.personal_strength,
        ):
            spin.valueChanged.connect(self._refresh_personal_preview)
        self.personal_track_combo.currentIndexChanged.connect(
            self._refresh_personal_preview
        )
        self.personal_check.toggled.connect(self._refresh_personal_preview)
        self._sync_personal_controls()
        if show_personal_color and self.personal_check.isChecked():
            tabs.setCurrentWidget(personal_page)
        self._set_color(self._initial_color)
        self._refresh_personal_preview()

    @staticmethod
    def _row(slider: QSlider, spin: QSpinBox) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(slider, 1)
        layout.addWidget(spin)
        return row

    def _slider_row(self, minimum: int, maximum: int, suffix: str) -> tuple[QSlider, QSpinBox]:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(minimum, maximum)
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSuffix(suffix)
        spin.setMinimumWidth(82)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        slider.valueChanged.connect(self._sliders_changed)
        return slider, spin

    @staticmethod
    def _spin(minimum: int, maximum: int, suffix: str, value: float) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSuffix(suffix)
        spin.setValue(round(value))
        return spin

    @property
    def selected_color(self) -> QColor:
        return QColor(self._color)

    def personal_settings(self) -> dict[str, object]:
        if not self._show_personal_color:
            return {}
        return {
            "personal_color_enabled": self.personal_check.isChecked(),
            "personal_color_brightness": float(self.personal_brightness.value()),
            "personal_color_saturation": float(self.personal_saturation.value()),
            "personal_color_hue_shift": float(self.personal_hue.value()),
            "personal_color_strength": self.personal_strength.value() / 100.0,
        }

    def _set_color(self, color: QColor) -> None:
        if not color.isValid():
            return
        self._syncing = True
        try:
            self._color = QColor(color)
            hue = color.hsvHue()
            self.hue_slider.setValue(0 if hue < 0 else hue)
            self.saturation_slider.setValue(round(color.hsvSaturationF() * 100.0))
            self.value_slider.setValue(round(color.valueF() * 100.0))
            self.alpha_slider.setValue(round(color.alphaF() * 100.0))
            self.hex_edit.setText(self._serialized(color))
            self.hex_edit.setStyleSheet("")
            self.hex_edit.setToolTip("")
            self.preview.set_current(color)
        finally:
            self._syncing = False
        self._refresh_personal_preview()

    def _sliders_changed(self, _value: int) -> None:
        if self._syncing:
            return
        color = QColor(self._color)
        sender = self.sender()
        if sender is self.alpha_slider:
            color.setAlphaF(self.alpha_slider.value() / 100.0)
        else:
            hue, saturation, value, alpha = color.getHsvF()
            color.setHsvF(
                self.hue_slider.value() / 360.0 if sender is self.hue_slider else max(0.0, hue),
                self.saturation_slider.value() / 100.0 if sender is self.saturation_slider else saturation,
                self.value_slider.value() / 100.0 if sender is self.value_slider else value,
                alpha,
            )
        self._set_color(color)

    def accept(self) -> None:
        if self.color_page.isEnabled() and not self._apply_hex():
            self.hex_edit.setFocus()
            return
        super().accept()

    def _apply_hex(self) -> bool:
        value = self.hex_edit.text().strip()
        if value and not value.startswith("#"):
            value = f"#{value}"
        color = QColor(value)
        if color.isValid() and len(value) in {4, 7, 9}:
            self._set_color(color)
            return True
        self.hex_edit.setStyleSheet("border-color:#E05252;")
        self.hex_edit.setToolTip(
            "#RRGGBB 또는 #AARRGGBB 형식으로 입력하세요."
            if self._korean else "Enter #RRGGBB or #AARRGGBB."
        )
        return False

    def _use_preset(self, value: str) -> None:
        color = QColor(value)
        color.setAlpha(self._color.alpha())
        self._set_color(color)

    def _reset_color(self) -> None:
        self._set_color(self._initial_color)

    def _sync_personal_controls(self) -> None:
        enabled = self._show_personal_color and self.personal_check.isChecked()
        self.color_page.setEnabled(not enabled)
        self.reset_button.setEnabled(not enabled)
        for widget in (
            self.personal_brightness, self.personal_saturation,
            self.personal_hue, self.personal_strength,
        ):
            widget.setEnabled(enabled)

    def _refresh_personal_preview(self, *_args: object) -> None:
        """Recompute the selected track's personal color with current adjustments."""
        if not getattr(self, "_tracks", None):
            return
        index = self.personal_track_combo.currentIndex()
        if not 0 <= index < len(self._tracks):
            return
        track = self._tracks[index]
        personal = extract_track_personal_color(track.file_path, track.cover_path)
        result_hex = adjust_personal_color(
            personal,
            self._serialized(self._color),
            brightness=float(self.personal_brightness.value()),
            saturation=float(self.personal_saturation.value()),
            hue_shift=float(self.personal_hue.value()),
            strength=self.personal_strength.value() / 100.0,
        )
        self.personal_preview_swatch.setStyleSheet(
            f"background:{result_hex}; border:1px solid rgba(120,130,145,0.7); "
            "border-radius:10px;"
        )
        if not personal.isValid():
            self.personal_preview_note.setText(
                "이 곡은 앨범 아트가 없어 위에서 지정한 기준 색상을 사용합니다."
                if self._korean else
                "This track has no artwork, so the base color above is used."
            )
        elif not self.personal_check.isChecked():
            self.personal_preview_note.setText(
                "퍼스널 컬러가 꺼져 있어 실제로는 적용되지 않습니다. (미리보기만 표시)"
                if self._korean else
                "Personal color is off, so this is a preview only."
            )
        else:
            self.personal_preview_note.setText(
                f"미리보기 결과: {result_hex}" if self._korean
                else f"Preview result: {result_hex}"
            )

    @staticmethod
    def _serialized(color: QColor) -> str:
        mode = (
            QColor.NameFormat.HexRgb
            if color.alpha() == 255 else QColor.NameFormat.HexArgb
        )
        return color.name(mode).upper()
