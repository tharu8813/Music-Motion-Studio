"""Optional translation preferences, separate from service setup and review."""
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel,
    QLineEdit, QPlainTextEdit, QScrollArea, QTabWidget, QVBoxLayout, QWidget)
from app.services.lyrics_translation_prompt import DEFAULT_LYRICS_TRANSLATION_PROMPT
from app.services.lyrics_translation_service import LLM_PROVIDERS, LOCAL_LLM_PROVIDERS, TranslationError
from app.dialogs.help_dialog import install_help_shortcut


class TranslationAdvancedDialog(QDialog):
    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services = services
        install_help_shortcut(self, ("lyrics_editor", "lyrics_translation"), translator=services.translator)
        tr = services.tr_text
        self.setObjectName("translationAdvancedDialog")
        self.setWindowTitle(tr("고급 번역 설정", "Advanced translation settings"))
        self.resize(720, 620)
        self.setMinimumSize(440, 360)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        self.caption = QLabel()
        self.caption.setObjectName("dialogTitle")
        self.caption.setWordWrap(True)
        root.addWidget(self.caption)
        tabs = QTabWidget()
        preferences = QWidget()
        form = QFormLayout(preferences)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.model_edit = QLineEdit()
        self.model_label = QLabel(tr("모델 ID 직접 입력", "Manual model ID"))
        self.model_label.setBuddy(self.model_edit)
        self.model_edit.setAccessibleName(self.model_label.text())
        form.addRow(self.model_label, self.model_edit)
        self.endpoint_edit = services.endpoint_edit
        self.endpoint_label = QLabel(tr("로컬 서버 주소", "Local server URL"))
        self.endpoint_label.setBuddy(self.endpoint_edit)
        form.addRow(self.endpoint_label, self.endpoint_edit)
        note = QLabel(tr("문체·존댓말·용어 등의 요청을 적으세요. 기본 번역 기준은 원문에 충실한 직역입니다.",
                        "Add preferences for tone, register or terminology. The default is faithful, literal translation."))
        note.setWordWrap(True)
        form.addRow(note)
        self.instructions_edit = services.instructions_edit
        self.instructions_edit.setMaximumHeight(16777215)
        self.instructions_edit.setMinimumHeight(180)
        label = QLabel(tr("사용자 추가 번역 지침", "Additional translation instructions"))
        label.setBuddy(self.instructions_edit)
        form.addRow(label, self.instructions_edit)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(preferences)
        tabs.addTab(scroll, tr("문체 및 연결", "Style and connection"))
        self.prompt = QPlainTextEdit(DEFAULT_LYRICS_TRANSLATION_PROMPT)
        self.prompt.setReadOnly(True)
        self.prompt.setAccessibleName(tr("가사 번역 기본 프롬프트", "Default lyric translation prompt"))
        tabs.addTab(self.prompt, tr("기본 프롬프트", "Default prompt"))
        root.addWidget(tabs, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        root.addWidget(buttons)
        self.model_edit.textEdited.connect(self._model_changed)
        services.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        s = self.services
        self.caption.setText(s.provider_combo.currentText())
        if self.model_edit.text() != s._model_value():
            self.model_edit.setText(s._model_value())
        cloud = s._provider in LLM_PROVIDERS and s._provider not in LOCAL_LLM_PROVIDERS
        self.model_label.setVisible(cloud)
        self.model_edit.setVisible(cloud)
        for widget in (self.endpoint_label, self.endpoint_edit):
            widget.setVisible(s._provider in LOCAL_LLM_PROVIDERS)
        self.instructions_edit.setEnabled(s._provider in LLM_PROVIDERS)
        self.instructions_edit.setVisible(True)

    def _model_changed(self, text):
        if self.services.model_combo.isEditable():
            self.services.model_combo.setEditText(text)

    def done(self, result):
        try:
            self.services._save_settings()
        except TranslationError as error:
            self.status_label.setText(str(error))
            return
        super().done(result)
