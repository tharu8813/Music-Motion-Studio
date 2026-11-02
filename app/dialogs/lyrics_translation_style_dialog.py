"""Independent translation typography, applied to every translated cue row."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFontComboBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)
from app.dialogs.color_editor_dialog import ColorEditorDialog


class LyricsTranslationStyleDialog(QDialog):
    def __init__(self, source, translator, parent=None):
        super().__init__(parent)
        self.translator = translator
        self.source = source
        self._style = dict(source.subtitle_translation_style)
        tr = lambda ko, en: ko if translator.is_korean else en
        style = source.subtitle_translation_style
        self.color = str(style.get("color", "#B8C4D4"))
        self.setWindowTitle(tr("번역 가사 스타일", "Translation lyric style"))
        self.setMinimumWidth(460)
        self.setObjectName("lyricsTranslationStyleDialog")
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(14)
        title = QLabel(tr("번역 가사 스타일", "Translation lyric style"))
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        label = QLabel(tr("번역 가사에만 적용합니다. 원문 줄별 스타일과 별도로 저장됩니다.",
                          "Applies only to translations, separately from original per-line styles."))
        label.setWordWrap(True)
        label.setObjectName("mutedLabel")
        root.addWidget(label)
        preview_frame = QFrame()
        preview_frame.setObjectName("lyricStylePreview")
        preview_layout = QVBoxLayout(preview_frame)
        preview_layout.setContentsMargins(16, 14, 16, 14)
        caption = QLabel(tr("글꼴 미리보기", "Typography preview"))
        caption.setObjectName("mutedLabel")
        preview_layout.addWidget(caption)
        self.original_preview = QLabel("The moon keeps your name")
        self.original_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.original_preview.setWordWrap(True)
        preview_layout.addWidget(self.original_preview)
        self.preview = QLabel(tr("달은 네 이름을 간직한다", "The moon keeps your name"))
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setWordWrap(True)
        self.preview.setMinimumHeight(64)
        preview_layout.addWidget(self.preview)
        self.preview_detail = QLabel()
        self.preview_detail.setObjectName("mutedLabel")
        self.preview_detail.setWordWrap(True)
        preview_layout.addWidget(self.preview_detail)
        root.addWidget(preview_frame)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.enabled = QCheckBox(tr("번역문 표시", "Show translations"))
        self.enabled.setChecked(source.subtitle_translation_enabled)
        self.family = QFontComboBox()
        self.family.setCurrentFont(QFont(str(style.get("font_family") or source.font_family)))
        self.size_mode = QComboBox()
        self.size_mode.addItem(tr("기본 크기에서 조절", "Offset from base size"), "relative")
        self.size_mode.addItem(tr("고정 크기", "Fixed size"), "absolute")
        self.size = QDoubleSpinBox()
        self.size.setRange(-100, 100)
        self.size.setValue(float(style.get("font_size_offset", -4)))
        self.size.setSuffix(" px")
        self.size.setDecimals(0)
        self._relative_size = float(style.get("font_size_offset", -4))
        self._absolute_size = float(style.get("font_size", max(8, min(120, source.font_size - 4))))
        self.size_mode.currentIndexChanged.connect(self._size_mode_changed)
        if "font_size" in style:
            self.size_mode.setCurrentIndex(1)
            self.size.setValue(float(style["font_size"]))
        self.weight = QComboBox()
        for value in (300, 400, 500, 600, 700, 800, 900):
            names = {300: ("얇게", "Light"), 400: ("보통", "Regular"), 500: ("중간", "Medium"),
                600: ("세미 볼드", "Semi bold"), 700: ("굵게", "Bold"),
                800: ("매우 굵게", "Extra bold"), 900: ("블랙", "Black")}
            self.weight.addItem(f"{tr(*names[value])} · {value}", value)
        self.weight.setCurrentIndex(max(0, self.weight.findData(style.get("font_weight", 400))))
        self.italic = QCheckBox(tr("기울임꼴", "Italic"))
        self.italic.setChecked(bool(style.get("italic", False)))
        self.color_button = QPushButton(self.color)
        self.color_button.clicked.connect(self._choose_color)
        form.addRow(self.enabled)
        for ko, en, widget in (("글꼴", "Font", self.family), ("크기 방식", "Size mode", self.size_mode),
                               ("크기", "Size", self.size), ("굵기", "Weight", self.weight),
                               ("색상", "Color", self.color_button)):
            form.addRow(tr(ko, en), widget)
        form.addRow(self.italic)
        root.addLayout(form)
        self.family.currentFontChanged.connect(self._update_preview)
        self.size.valueChanged.connect(self._update_preview)
        self.weight.currentIndexChanged.connect(self._update_preview)
        self.italic.toggled.connect(self._update_preview)
        self.enabled.toggled.connect(self._update_preview)
        footer = QHBoxLayout()
        reset = QPushButton(tr("기본 스타일로", "Reset style"))
        reset.setFlat(True)
        reset.clicked.connect(self._reset)
        footer.addWidget(reset)
        footer.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("적용", "Apply"))
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("취소", "Cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        root.addLayout(footer)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._update_preview()

    def _size_mode_changed(self):
        absolute = self.size_mode.currentData() == "absolute"
        if absolute:
            self._relative_size = self.size.value()
        else:
            self._absolute_size = self.size.value()
        self.size.setRange(8, 120) if absolute else self.size.setRange(-100, 100)
        self.size.setValue(self._absolute_size if absolute else self._relative_size)
        if hasattr(self, "weight"):
            self._update_preview()

    def _choose_color(self):
        dialog = ColorEditorDialog(QColor(self.color), self.source, self.translator,
            "번역 가사 색상" if self.translator.is_korean else "Translation color", self,
            show_personal_color=False)
        if dialog.exec():
            self.color = ColorEditorDialog._serialized(dialog.selected_color)
            self.color_button.setText(self.color)
            self._update_preview()

    def _update_preview(self, *_args):
        size = self.size.value() if self.size_mode.currentData() == "absolute" else self.source.font_size + self.size.value()
        size = max(8, min(120, size))
        font = self.family.currentFont()
        font.setPixelSize(max(8, round(size)))
        font.setWeight(QFont.Weight(int(self.weight.currentData())))
        font.setItalic(self.italic.isChecked())
        self.preview.setFont(font)
        family = font.family().replace("\\", "\\\\").replace('"', '\\"')
        # Explicit widget rules override the studio's global QWidget font.
        self.preview.setStyleSheet(
            f'color:{self.color}; font-family:"{family}"; font-size:{round(size)}px; '
            f'font-weight:{int(self.weight.currentData())}; font-style:{"italic" if font.italic() else "normal"};')
        original = QFont(self.source.font_family)
        original.setPixelSize(max(8, round(self.source.font_size)))
        original.setWeight(QFont.Weight(self.source.font_weight))
        self.original_preview.setFont(original)
        original_family = self.source.font_family.replace("\\", "\\\\").replace('"', '\\"')
        self.original_preview.setStyleSheet(
            f'font-family:"{original_family}"; font-size:{max(8, round(self.source.font_size))}px; '
            f'font-weight:{self.source.font_weight};')
        self.preview.setVisible(self.enabled.isChecked())
        self.preview_detail.setText((f"번역문 {max(8, round(size))} px · 실제 줄바꿈과 배치는 캔버스에서 확인하세요." if self.translator.is_korean else
            f"Translation {max(8, round(size))} px · Check wrapping and placement on the canvas.") if self.enabled.isChecked() else
            ("번역문을 숨깁니다. 저장된 번역은 유지됩니다." if self.translator.is_korean else "Translations are hidden. Saved translations are kept."))
        swatch = QPixmap(18, 18)
        swatch.fill(QColor(self.color))
        self.color_button.setIcon(QIcon(swatch))

    def _reset(self):
        self._style = {}
        self.color = "#B8C4D4"
        self.color_button.setText(self.color)
        self.family.setCurrentFont(QFont(self.source.font_family))
        self.size_mode.setCurrentIndex(0)
        self.size.setValue(-4)
        self.weight.setCurrentIndex(self.weight.findData(400))
        self.italic.setChecked(False)
        self._update_preview()

    def settings(self):
        style = {key: value for key, value in self._style.items()
            if key not in {"font_size", "font_size_offset"}}
        return {"subtitle_translation_enabled": self.enabled.isChecked(),
                "subtitle_translation_style": {**style, "color": self.color, "font_family": self.family.currentFont().family(),
                    "font_size" if self.size_mode.currentData() == "absolute" else "font_size_offset": self.size.value(),
                    "font_weight": self.weight.currentData(), "italic": self.italic.isChecked()}}
