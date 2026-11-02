"""Translate into a detached draft, then review, edit and apply selected rows."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from PySide6.QtCore import QRect, QSize, QSettings, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
    QFileDialog, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSplitter, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from app.services.chatgpt_auth_service import ChatGPTAuthService
from app.dialogs.help_dialog import install_help_shortcut
from app.services.translation_worker import TranslationWorker
from app.services.local_translation_service import LocalTranslationModels
from app.services.local_llm_service import LocalLLMService, LOCAL_LLM_PROVIDERS, CLOUD_LLM_PROVIDERS
from app.ui.design_system import COLORS
from app.services.lyrics_translation_service import (
    LLM_PROVIDERS, PROVIDERS, LyricsTranslationService,
    TranslationConfig, TranslationError, apply_translations,
    approved_translation,
)
from app.services.translation_credentials import TranslationCredentials
from app.services.lyrics_translation_prompt import DEFAULT_LYRICS_TRANSLATION_PROMPT, lyric_text


class _TranslationDelegate(QStyledItemDelegate):
    """Keep multi-line cue translations editable without flattening line breaks."""
    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        table = self.parent()
        width = max(20, table.columnWidth(index.column()) - 24)
        bounds = option.fontMetrics.boundingRect(QRect(0, 0, width, 100000),
            Qt.TextFlag.TextWordWrap, index.data(Qt.ItemDataRole.DisplayRole) or "")
        return QSize(hint.width(), max(hint.height(), bounds.height() + 14))

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        editor.setMinimumHeight(72)
        return editor

    def setEditorData(self, editor, index):
        editor.setPlainText(index.data(Qt.ItemDataRole.EditRole) or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText(), Qt.ItemDataRole.EditRole)

    def eventFilter(self, editor, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.KeyPress and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
                return True
            return False
        return super().eventFilter(editor, event)


class LyricsTranslationDialog(QDialog):
    def __init__(self, cues, translator, parent=None, *, service=None, auth=None, settings=None, local_llm_factory=None):
        super().__init__(parent)
        self.translator = translator
        install_help_shortcut(self, ("lyrics_editor", "lyrics_translation"), translator=translator)
        self.cues = deepcopy(cues)
        self.selected_cues = deepcopy(cues)
        self.local_models = getattr(service, "local_models", None) or LocalTranslationModels()
        self.service = service or LyricsTranslationService(local_models=self.local_models)
        self.local_llm_factory = local_llm_factory or LocalLLMService
        self._installed_llms = {}
        self._changing_provider = False
        self.settings = settings if settings is not None else QSettings()
        self.credentials = TranslationCredentials(self.settings)
        self.auth = auth or ChatGPTAuthService(self.credentials, self.settings)
        self.worker = None
        self._closing = False
        self._provider = ""
        self._draft_config = None
        self._review_ready = False
        self._new_draft = False
        self._incomplete_rows = set()
        self._editable_rows = {row for row, cue in enumerate(self.cues) if lyric_text(cue)}
        self.setObjectName("lyricsTranslationDialog")
        self.setWindowTitle(self.tr_text("가사 번역 · 검토 후 적용", "Translate lyrics · Review before applying"))
        self.setMinimumSize(760, 530)
        self.resize(1080, 720)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)
        title = QLabel(self.tr_text("가사 번역", "Translate lyrics"))
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        note = QLabel(self.tr_text(
            "클라우드 LLM은 서비스로 가사를 전송합니다. 로컬 LLM과 로컬 번역 모델은 이 컴퓨터에서 실행합니다. 결과를 검토한 뒤 적용하세요.",
            "Cloud LLMs send lyrics to their service. Local LLMs and translation models run on this computer. Review before applying."))
        note.setWordWrap(True)
        note.setObjectName("mutedLabel")
        root.addWidget(note)
        splitter = QSplitter()
        panel = QWidget()
        form = QFormLayout(panel)
        form.setVerticalSpacing(10)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        settings_title = QLabel(self.tr_text("1. 번역 설정", "1. Translation settings"))
        settings_title.setObjectName("lyricLineTitle")
        form.addRow(settings_title)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(self.tr_text("클라우드 LLM", "Cloud LLM"), "cloud_llm")
        self.mode_combo.addItem(self.tr_text("로컬 LLM", "Local LLM"), "local_llm")
        self.mode_combo.addItem(self.tr_text("로컬 번역 모델", "Local translation model"), "local")
        self.provider_combo = QComboBox()
        for key, name in PROVIDERS.items():
            if key in CLOUD_LLM_PROVIDERS:
                self.provider_combo.addItem(name, key)
        self.target_combo = QComboBox()
        self.source_combo = QComboBox()
        for combo in (self.source_combo, self.target_combo):
            combo.setEditable(True)
            for code, ko, en in (("ko", "한국어", "Korean"), ("en", "영어", "English"),
                    ("ja", "일본어", "Japanese"), ("zh", "중국어", "Chinese"),
                    ("zh-Hant", "중국어 번체", "Traditional Chinese"),
                    ("fr", "프랑스어", "French"), ("de", "독일어", "German"),
                    ("es", "스페인어", "Spanish"), ("ru", "러시아어", "Russian"),
                    ("pt", "포르투갈어", "Portuguese"), ("it", "이탈리아어", "Italian")):
                combo.addItem(f"{self.tr_text(ko, en)} ({code})", code)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.setToolTip(self.tr_text("목록에서 선택하거나 언어 코드를 직접 입력하세요. 예: en-GB, zh-Hant",
                "Choose a language or enter a code, e.g. en-GB, zh-Hant."))
        self.source_combo.insertItem(0, self.tr_text("자동 감지", "Detect automatically"), "auto")
        self.source_combo.setCurrentIndex(0)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumContentsLength(14)
        self.model_combo.lineEdit().setPlaceholderText(self.tr_text("모델 ID 입력", "Enter model ID"))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.endpoint_edit = QLineEdit()
        self.instructions_edit = QPlainTextEdit()
        self.instructions_edit.setMaximumHeight(85)
        self.instructions_edit.setPlaceholderText(self.tr_text(
            "예: 존댓말 사용, 고유명사는 원문 유지", "e.g. Formal register; preserve original proper names"))
        self.prompt_note = QLabel(self.tr_text(
            "기본은 원문에 충실한 직역입니다. 문체나 용어에 관한 요청을 추가할 수 있습니다.",
            "Default: faithful, literal translation. Add preferences for register or terminology."))
        self.prompt_note.setWordWrap(True)
        self.prompt_button = QPushButton(self.tr_text("기본 프롬프트 보기…", "View default prompt…"))
        self.prompt_button.clicked.connect(self._show_default_prompt)
        self.remember_check = QCheckBox(self.tr_text("이 기기에 인증 정보 저장", "Remember credentials on this device"))
        self.remember_check.setToolTip(self.tr_text("Windows 계정으로 암호화하며 프로젝트에는 저장하지 않습니다.", "Encrypted for your Windows account; never stored in projects."))
        self.account_button = QPushButton(self.tr_text("OpenAI 계정 관리…", "Manage OpenAI account…"))
        self.local_models_button = QPushButton(self.tr_text("이 언어 모델 자동 설치", "Install model for these languages"))
        self.import_local_button = QPushButton(self.tr_text("모델 파일 직접 가져오기…", "Import a model file…"))
        self.llm_manager_button = QPushButton(self.tr_text("로컬 LLM 관리자…", "Local LLM manager…"))
        self.local_pair_note = QLabel()
        self.local_pair_note.setWordWrap(True)
        self.local_pair_note.setTextFormat(Qt.TextFormat.PlainText)
        self.local_accuracy_warning = QLabel(self.tr_text(
            "로컬 번역 모델은 LLM AI 모델이 아닙니다. 가사·비유·문맥에서는 번역 정확도가 크게 떨어질 수 있으므로 가능하면 클라우드 LLM 또는 로컬 LLM 번역을 권장합니다.",
            "Local translation models are not LLM AI models. Accuracy can be much lower for lyrics, metaphors and context. Prefer a cloud LLM or local LLM when possible."))
        self.local_accuracy_warning.setWordWrap(True)
        self.local_accuracy_warning.setStyleSheet(f"color: {COLORS['warning']}; background: {COLORS['warning_surface']}; padding: 10px; border-radius: 6px;")
        self.models_button = QPushButton(self.tr_text("모델 불러오기", "Load models"))
        self.account_label = QLabel()
        self.account_label.setTextFormat(Qt.TextFormat.PlainText)
        self.account_label.setWordWrap(True)
        self.provider_note = QLabel()
        self.provider_note.setWordWrap(True)
        self.provider_note.setOpenExternalLinks(True)
        self._rows = {}
        for key, ko, en, widget in (
            ("mode", "번역 방식", "Translation mode", self.mode_combo),
            ("provider", "서비스 / 실행 방식", "Provider / engine", self.provider_combo),
            ("source", "원문 언어", "Source language", self.source_combo),
            ("target", "번역 언어", "Target language", self.target_combo),
            ("model", "모델", "Model", self.model_combo),
            ("key", "API 키", "API key", self.key_edit),
            ("endpoint", "서버 주소", "Server URL", self.endpoint_edit),
            ("instructions", "번역 요청 사항", "Translation instructions", self.instructions_edit),
        ):
            label = QLabel(self.tr_text(ko, en))
            label.setBuddy(widget)
            widget.setAccessibleName(label.text())
            form.addRow(label, widget)
            self._rows[key] = (label, widget)
            if key == "mode":
                form.addRow(self.local_accuracy_warning)
            elif key == "provider":
                form.addRow(self.account_label)
                form.addRow(self.account_button)
            elif key == "model":
                form.addRow(self.models_button)
                form.addRow(self.llm_manager_button)
                form.addRow(self.local_pair_note)
                form.addRow(self.local_models_button)
                form.addRow(self.import_local_button)
            elif key == "endpoint":
                form.addRow(self.remember_check)
        for widget in (self.prompt_note, self.prompt_button, self.provider_note):
            form.addRow(widget)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(260)
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        splitter.addWidget(scroll)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(12, 0, 0, 0)
        review_title = QLabel(self.tr_text("2. 원문과 비교 · 검토 후 적용", "2. Compare, review and apply"))
        review_title.setObjectName("lyricLineTitle")
        review_layout.addWidget(review_title)
        self.review_summary = QLabel()
        self.review_summary.setObjectName("mutedLabel")
        self.review_summary.setWordWrap(True)
        review_layout.addWidget(self.review_summary)
        row_actions = QHBoxLayout()
        self._row_action_buttons = []
        for ko, en, checked in (("전체 선택", "Select all", True), ("전체 해제", "Clear selection", False)):
            button = QPushButton(self.tr_text(ko, en))
            button.clicked.connect(lambda _clicked=False, value=checked: self._check_all(value))
            row_actions.addWidget(button)
            self._row_action_buttons.append(button)
        self.selection_label = QLabel()
        row_actions.addWidget(self.selection_label, 1)
        review_layout.addLayout(row_actions)
        self.table = QTableWidget(len(cues), 5)
        self.table.setItemDelegateForColumn(2, _TranslationDelegate(self.table))
        self.table.setItemDelegateForColumn(3, _TranslationDelegate(self.table))
        self.table.setToolTip(self.tr_text("번역문을 두 번 클릭해 수정합니다. Ctrl+Enter로 편집을 마칩니다.",
            "Double-click a translation to edit. Ctrl+Enter finishes editing."))
        self.table.setHorizontalHeaderLabels([self.tr_text("적용", "Apply"), self.tr_text("시작", "Start"),
            self.tr_text("원문", "Original"), self.tr_text("번역문", "Translation"),
            self.tr_text("상태", "Status")])
        self.table.setWordWrap(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAccessibleName(self.tr_text("원문과 번역 검토 표", "Original and translated lyrics review"))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().hide()
        self._row_resize_timer = QTimer(self)
        self._row_resize_timer.setSingleShot(True)
        self._row_resize_timer.setInterval(75)
        self._row_resize_timer.timeout.connect(self.table.resizeRowsToContents)
        self.table.horizontalHeader().sectionResized.connect(lambda *_args: self._row_resize_timer.start())
        for row, cue in enumerate(self.cues):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if row in self._editable_rows else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check)
            seconds = max(0, float(cue.get('start', 0)))
            for column, text in ((1, f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"),
                                 (2, str(cue.get("text", "")).replace("\\n", "\n")),
                                 (3, approved_translation(cue)),
                                 (4, self.tr_text("저장됨", "Saved") if approved_translation(cue) else self.tr_text("대기", "Ready"))):
                item = QTableWidgetItem(text)
                if column != 3:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                self.table.setItem(row, column, item)
            if row not in self._editable_rows:
                check.setFlags(Qt.ItemFlag.NoItemFlags)
                self.table.item(row, 3).setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.item(row, 4).setText(self.tr_text("가사 없음", "No lyrics"))
        self.table.resizeRowsToContents()
        review_layout.addWidget(self.table, 1)
        selection_note = QLabel(self.tr_text(
            "체크하지 않은 줄은 기존 번역을 유지합니다. 빈 번역문을 체크해 적용하면 해당 번역을 삭제합니다.",
            "Unchecked rows keep existing translations. Applying a checked empty translation removes it."))
        selection_note.setWordWrap(True)
        review_layout.addWidget(selection_note)
        edit_note = QLabel(self.tr_text("번역문을 두 번 클릭해 수정 · 줄바꿈은 Enter · 편집 완료는 Ctrl+Enter",
            "Double-click to edit · Enter for a line break · Ctrl+Enter to finish"))
        edit_note.setWordWrap(True)
        edit_note.setObjectName("mutedLabel")
        review_layout.addWidget(edit_note)
        self.attribution_label = QLabel('<a href="https://papago.naver.com/">Translated by Papago</a>')
        self.attribution_label.setOpenExternalLinks(True)
        self.attribution_label.setVisible(any(approved_translation(cue) and cue["translation"]["provider"] == "papago" for cue in cues))
        review_layout.addWidget(self.attribution_label)
        splitter.addWidget(review)
        splitter.setSizes([300, 700])
        root.addWidget(splitter, 1)
        self.progress = QProgressBar()
        self.progress.hide()
        root.addWidget(self.progress)
        self.status_label = QLabel(self.tr_text("번역문을 별도로 저장하며 원문 가사와 시간 정보는 유지합니다.",
            "Translations are saved separately. Original lyrics and timing are preserved."))
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status_label)
        footer = QHBoxLayout()
        self.translate_button = QPushButton(self.tr_text("번역 시작", "Translate"))
        self.translate_button.setAutoDefault(False)
        self.cancel_request_button = QPushButton(self.tr_text("요청 취소", "Cancel request"))
        self.cancel_request_button.hide()
        footer.addWidget(self.translate_button)
        footer.addWidget(self.cancel_request_button)
        footer.addStretch()
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText(self.tr_text("검토한 줄 적용", "Apply reviewed rows"))
        self.apply_button.setEnabled(False)
        self.apply_button.setProperty("primary", True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self.tr_text("닫기", "Close"))
        self.buttons.rejected.connect(self.reject)
        self.apply_button.clicked.connect(self._apply)
        footer.addWidget(self.buttons)
        root.addLayout(footer)
        # Enter in a language/model field must not start a billable request.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._settings_panel = panel
        self._review_panel = review
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.table.itemChanged.connect(self._update_selection)
        self.translate_button.clicked.connect(self._translate)
        self.cancel_request_button.clicked.connect(self._cancel_request)
        self.account_button.clicked.connect(self._open_account)
        self.local_models_button.clicked.connect(self._install_local_pair)
        self.import_local_button.clicked.connect(self._import_local_model)
        self.llm_manager_button.clicked.connect(self._open_llm_manager)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.source_combo.currentTextChanged.connect(self._pair_changed)
        self.target_combo.currentTextChanged.connect(self._pair_changed)
        self.models_button.clicked.connect(self._load_models)
        saved = str(self.settings.value("translation/provider", "chatgpt"))
        mode = "cloud_llm" if saved in CLOUD_LLM_PROVIDERS else "local_llm" if saved in LOCAL_LLM_PROVIDERS else "local"
        self.settings.setValue("translation/last_" + mode, saved)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode))
        self.mode_combo.blockSignals(False)
        self._mode_changed()
        self._restore_review()
        self._update_selection()
        if not self._editable_rows:
            self.translate_button.setEnabled(False)
            self.status_label.setText(self.tr_text("번역할 가사가 없습니다. 곡 설정에서 원문 가사를 먼저 추가하세요.",
                "No lyrics to translate. Add original lyrics in track settings first."))

    def tr_text(self, korean, english):
        return korean if self.translator.is_korean else english

    @staticmethod
    def _language_value(combo):
        index = combo.currentIndex()
        if index >= 0 and combo.currentText() == combo.itemText(index):
            return combo.itemData(index)
        return combo.currentText().strip()

    @staticmethod
    def _set_language(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setEditText(value)

    def _save_settings(self):
        if not self._provider:
            return
        base = "translation/config/" + self._provider + "/"
        for name, value in (("model", self._model_value()),
            ("endpoint", self.endpoint_edit.text()),
            ("instructions", self.instructions_edit.toPlainText()), ("target", self._language_value(self.target_combo)),
            ("source", self._language_value(self.source_combo))):
            if name != "model" or self._provider not in LOCAL_LLM_PROVIDERS or value:
                self.settings.setValue(base + name, value)
        if self._provider not in {"chatgpt", "local_nmt"}:
            self.credentials.save(self._provider, {"key": self.key_edit.text()},
                remember=self.remember_check.isChecked())

    def _provider_changed(self, *_args):
        provider = self.provider_combo.currentData()
        if not provider or self._provider == provider:
            return
        if self._provider:
            try:
                self._save_settings()
            except TranslationError as error:
                self.status_label.setText(str(error))
        self._provider = provider
        self._changing_provider = True
        self.settings.setValue("translation/provider", provider)
        base = "translation/config/" + provider + "/"
        self.model_combo.setEditable(provider in CLOUD_LLM_PROVIDERS)
        self.model_combo.clear()
        if provider in CLOUD_LLM_PROVIDERS:
            self.model_combo.lineEdit().setPlaceholderText(self.tr_text("모델 ID 입력", "Enter model ID"))
            self.model_combo.setEditText(str(self.settings.value(base + "model", "")))
        defaults = {"ollama": "http://localhost:11434/v1", "lmstudio": "http://localhost:1234/v1"}
        self.endpoint_edit.setText(str(self.settings.value(base + "endpoint", defaults.get(provider, ""))))
        self.instructions_edit.setPlainText(str(self.settings.value(base + "instructions", "")))
        self._set_language(self.target_combo, str(self.settings.value(base + "target", "ko")))
        self._set_language(self.source_combo, str(self.settings.value(base + "source", "en" if provider == "local_nmt" else "auto")))
        credentials = self.credentials.load(provider)
        self.key_edit.setText(credentials.get("key", ""))
        self.remember_check.setChecked(bool(self.settings.value("translation/secrets/" + provider, "")))
        for key, visible in {"model": True, "provider": provider != "local_nmt", "key": provider not in {"chatgpt", "local_nmt", "ollama"},
                             "endpoint": provider in {"ollama", "lmstudio"},
                             "instructions": provider in LLM_PROVIDERS}.items():
            for widget in self._rows[key]:
                widget.setVisible(visible)
        for widget in (self.account_button, self.account_label):
            widget.setVisible(provider == "chatgpt")
        self.models_button.setVisible(provider == "chatgpt" or provider in LOCAL_LLM_PROVIDERS)
        self.models_button.setText(self.tr_text("설치된 모델 새로고침", "Refresh installed models") if provider in LOCAL_LLM_PROVIDERS else self.tr_text("모델 불러오기", "Load models"))
        self.llm_manager_button.setVisible(provider in LOCAL_LLM_PROVIDERS)
        for widget in (self.local_models_button, self.import_local_button, self.local_pair_note, self.local_accuracy_warning):
            widget.setVisible(provider == "local_nmt")
        self.remember_check.setVisible(provider not in {"chatgpt", "local_nmt", "ollama"})
        for widget in (self.prompt_note, self.prompt_button):
            widget.setVisible(provider in LLM_PROVIDERS)
        self.source_combo.setEnabled(True)
        self.target_combo.setEnabled(True)
        notes = {
            "chatgpt": self.tr_text("계정 관리에서 로그인하면 앱 종료 후에도 연결을 유지합니다. 계정·구독 한도를 따릅니다.", "Manage sign-in separately; your session is kept after restart. Account and plan limits apply."),
            "claude": self.tr_text("Claude 구독 로그인 대신 Console API 키를 사용합니다.", "Uses a Console API key, separate from a Claude subscription."),
            "local_nmt": self.tr_text("별도 프로그램을 설치할 필요가 없습니다. 실행 구성요소는 앱에 포함되어 있으며 선택한 언어 모델만 자동으로 내려받아 설치합니다.", "No separate program is required. The runtime is included; install only the model for your selected languages."),
            "ollama": self.tr_text("설치된 LLM을 선택하세요. 처음 사용하는 경우 로컬 LLM 관리자에서 실행 구성요소와 모델을 준비할 수 있습니다.", "Choose an installed LLM. The local LLM manager can set up the runtime and download models."),
            "lmstudio": self.tr_text("LM Studio의 Developer 서버에서 LLM을 로드하세요.", "Load an LLM in LM Studio's Developer server."),
        }
        self.provider_note.setText(notes.get(provider, self.tr_text("서비스의 사용량·지원 언어 조건을 따릅니다.", "Provider usage limits and supported languages apply.")))
        self._changing_provider = False
        if provider == "local_nmt":
            self._refresh_local_models(str(self.settings.value(base + "model", "")))
        elif provider in LOCAL_LLM_PROVIDERS:
            self._populate_llm_models(self._installed_llms.get(provider, []), str(self.settings.value(base + "model", "")))
            QTimer.singleShot(0, lambda expected=provider: self._load_models() if self._provider == expected and self.worker is None else None)
        self._refresh_account()

    def _mode_changed(self, *_args):
        self._save_settings()
        old = self._provider
        if old:
            old_mode = "cloud_llm" if old in CLOUD_LLM_PROVIDERS else "local_llm" if old in LOCAL_LLM_PROVIDERS else "local"
            self.settings.setValue("translation/last_" + old_mode, old)
        self._provider = ""
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        mode = self.mode_combo.currentData()
        providers = CLOUD_LLM_PROVIDERS if mode == "cloud_llm" else LOCAL_LLM_PROVIDERS if mode == "local_llm" else {"local_nmt"}
        for key, name in PROVIDERS.items():
            if key in providers:
                self.provider_combo.addItem(name, key)
        previous = str(self.settings.value("translation/last_" + mode, ""))
        index = self.provider_combo.findData(previous)
        self.provider_combo.setCurrentIndex(max(0, index))
        self.provider_combo.blockSignals(False)
        self._provider_changed()

    def _refresh_local_models(self, selected=""):
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        source, target = self._language_value(self.source_combo), self._language_value(self.target_combo)
        matching = self.local_models.for_pair(source, target)
        for model in matching:
            self.model_combo.addItem(model.name, model.id)
        index = self.model_combo.findData(selected)
        self.model_combo.setCurrentIndex(max(0, index))
        if not matching:
            self.model_combo.setPlaceholderText(self.tr_text("이 언어 방향의 모델이 없습니다", "No model for these languages"))
        self.model_combo.blockSignals(False)
        same = source.casefold() == target.casefold()
        self.local_models_button.setEnabled(not matching and not same and source != "auto")
        if same:
            message = self.tr_text("같은 언어입니다. 모델 설치 없이 번역문을 빈칸으로 처리합니다.", "Identical languages: no model needed; translations stay blank.")
        elif source == "auto":
            message = self.tr_text("로컬 번역 모델은 원문 언어를 직접 선택해야 합니다.", "Choose a source language for local translation.")
        elif matching:
            message = self.tr_text(f"{source} → {target} 모델 설치됨 · 오프라인 번역 준비 완료", f"{source} → {target} installed · ready offline")
        else:
            message = self.tr_text(f"{source} → {target} 모델 설치가 필요합니다. 아래 버튼으로 자동 설치하세요.", f"Install a {source} → {target} model using the button below.")
        self.local_pair_note.setText(message)

    def _pair_changed(self, *_args):
        if self._provider == "local_nmt" and not self._changing_provider:
            self._refresh_local_models(self._model_value())

    def _install_local_pair(self):
        if self._provider == "local_nmt":
            source, target = self._language_value(self.source_combo), self._language_value(self.target_combo)
            self._start("install_local", lambda cancel, progress: self.local_models.download_pair(source, target, cancel, progress))

    def _import_local_model(self):
        filename, _ = QFileDialog.getOpenFileName(self, self.tr_text("모델 파일 가져오기", "Import model file"), "", "Argos models (*.argosmodel)")
        if filename:
            self._start("import_local", lambda cancel, progress: self.local_models.install(filename, cancel, progress))

    def _llm_service(self):
        return self.local_llm_factory(self._provider, self.endpoint_edit.text().strip(), self.key_edit.text().strip())

    def _populate_llm_models(self, models, selected=""):
        self.model_combo.clear()
        for model in models:
            self.model_combo.addItem(model["id"], model["id"])
        index = self.model_combo.findData(selected)
        self.model_combo.setCurrentIndex(max(0, index))
        self.model_combo.setPlaceholderText(self.tr_text("관리자에서 모델을 설치하세요", "Install a model in the manager"))

    def _load_models(self):
        if self.worker:
            return
        if self._provider in LOCAL_LLM_PROVIDERS:
            try:
                service = self._llm_service()
            except TranslationError as error:
                self.status_label.setText(str(error))
                return
            self._start("local_llm_models", lambda cancel, _progress: service.discover(cancel))
        elif self._provider == "chatgpt":
            self._start("models", lambda cancel, _progress: self.auth.models(cancel))

    def _open_llm_manager(self):
        from app.dialogs.local_llm_manager_dialog import LocalLLMManagerDialog
        try:
            service = self._llm_service()
        except TranslationError as error:
            self.status_label.setText(str(error))
            return
        dialog = LocalLLMManagerDialog(self.translator, self, service=service)
        dialog.exec()
        if dialog.models:
            self._installed_llms[self._provider] = dialog.models
        self._populate_llm_models(dialog.models, dialog.selected_model or self._model_value())
        dialog.deleteLater()

    def _open_account(self):
        from app.dialogs.openai_account_dialog import OpenAIAccountDialog
        dialog = OpenAIAccountDialog(self.translator, self, auth=self.auth)
        dialog.exec()
        self._refresh_account()
        dialog.deleteLater()

    def _show_default_prompt(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr_text("가사 번역 기본 프롬프트", "Default lyric translation prompt"))
        dialog.resize(760, 600)
        layout = QVBoxLayout(dialog)
        note = QLabel(self.tr_text(
            "모든 LLM에 같은 직역 기준을 적용합니다. 선택한 언어·추가 요청·원문과 참고 문맥은 번역 요청 시 별도로 전달합니다.",
            "All LLMs share these literal translation rules. Selected languages, preferences, lyrics and reference context are supplied separately."))
        note.setWordWrap(True)
        layout.addWidget(note)
        editor = QPlainTextEdit(DEFAULT_LYRICS_TRANSLATION_PROMPT)
        editor.setReadOnly(True)
        layout.addWidget(editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _refresh_account(self):
        try:
            profile = self.auth.profile()
        except TranslationError as error:
            profile = self.credentials.load("chatgpt")
            self.status_label.setText(str(error))
        self.account_label.setText(profile.get("email") or self.tr_text("연결된 ChatGPT 계정 없음", "No ChatGPT account connected"))
        self.models_button.setEnabled(bool(profile) if self._provider == "chatgpt" else True)

    def _restore_review(self):
        """Existing translations can be reviewed/removed without paying for a new request."""
        existing = next((cue["translation"] for cue in self.cues if approved_translation(cue)), None)
        if existing:
            self._review_ready = True
            self._draft_config = TranslationConfig(provider=existing["provider"],
                target=existing["language"], model=existing["model"])
            self.status_label.setText(self.tr_text("저장된 번역을 검토·수정할 수 있습니다.", "Review or edit saved translations."))

    def _model_value(self):
        if self._provider == "local_nmt" or self._provider in LOCAL_LLM_PROVIDERS:
            return self.model_combo.currentData() or ""
        index = self.model_combo.currentIndex()
        if index >= 0 and self.model_combo.currentText() == self.model_combo.itemText(index):
            return self.model_combo.itemData(index) or self.model_combo.currentText()
        return self.model_combo.currentText()

    def _config(self):
        return TranslationConfig(self._provider, target=self._language_value(self.target_combo),
            source=self._language_value(self.source_combo),
            model=self._model_value().strip(),
            key=self.key_edit.text().strip(), endpoint=self.endpoint_edit.text().strip(),
            instructions=self.instructions_edit.toPlainText().strip())

    def _translate(self):
        if self.worker is not None:
            return
        self._commit_editor()
        config = self._config()
        try:
            self.service.prepare(self.cues)
            if config.provider == "local_nmt" and not config.same_language and not config.model:
                raise TranslationError(self.local_pair_note.text())
            if config.provider == "chatgpt":
                config = replace(config, key="pending")
            config.validate()
        except TranslationError as error:
            self.status_label.setText(str(error))
            return
        if self._review_ready:
            answer = QMessageBox.question(self, self.windowTitle(), self.tr_text(
                "다시 번역하면 현재 검토 중인 번역 초안이 바뀝니다. 계속할까요?",
                "Translating again will replace the draft being reviewed. Continue?"))
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            self._save_settings()
        except TranslationError as error:
            self.status_label.setText(str(error))
            return
        def work(cancel, progress):
            connected = replace(config, key=self.auth.access_token(cancel)) if config.provider == "chatgpt" and not config.same_language else config
            return (config, self.service.translate(self.cues, connected, cancel, progress, activity=self.worker.activity.emit))
        self._start("translate", work)

    def _start(self, operation, work):
        if self.worker is not None:
            return
        self.worker = TranslationWorker(work, self)
        self._operation = operation
        self._settings_panel.setEnabled(False)
        self._live_snapshot = [(self.table.item(row, 3).text(), self.table.item(row, 0).checkState(),
            self.table.item(row, 4).text(), self.table.item(row, 4).toolTip())
            for row in range(self.table.rowCount())] if operation == "translate" else None
        self._incomplete_snapshot = self._incomplete_rows.copy()
        if operation == "translate":
            self._incomplete_rows.clear()
            for row in range(self.table.rowCount()):
                self.table.item(row, 4).setToolTip("")
        self._set_review_busy(True)
        self.translate_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.cancel_request_button.show()
        self.cancel_request_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.show()
        messages = {
            "translate": ("번역 중… 완료 후 원문과 비교해 주세요.", "Translating… review the result when ready."),
            "signin": ("브라우저에서 ChatGPT 로그인을 완료하세요. 이 창에서 취소할 수 있습니다.", "Complete ChatGPT sign-in in your browser. You can cancel here."),
            "models": ("사용 가능한 모델을 불러오는 중…", "Loading available models…"),
            "signout": ("ChatGPT 연결을 해제하는 중…", "Disconnecting ChatGPT…"),
            "install_local": ("선택한 언어 모델을 찾고 자동으로 내려받아 설치하는 중…", "Finding, downloading and installing the selected language model…"),
            "import_local": ("모델 파일을 확인하고 설치하는 중…", "Checking and installing the model file…"),
            "local_llm_models": ("설치된 로컬 LLM을 조회하는 중…", "Checking installed local LLMs…"),
        }
        self.status_label.setText(self.tr_text(*messages.get(operation, ("처리 중…", "Working…"))))
        self.worker.progress.connect(self._progress)
        self.worker.activity.connect(self._activity)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _progress(self, value, total):
        self.progress.setRange(0, total)
        self.progress.setValue(value)
        self.progress.setFormat(self.tr_text(f"{value} / {total}줄 완료", f"{value} / {total} rows complete"))

    def _set_review_busy(self, busy):
        for button in self._row_action_buttons:
            button.setEnabled(not busy)
        if busy:
            self._edit_triggers = self.table.editTriggers()
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        else:
            self.table.setEditTriggers(self._edit_triggers)
        self.table.blockSignals(True)
        for row in self._editable_rows:
            item = self.table.item(row, 0)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable if busy else item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        self.table.blockSignals(False)

    def _activity(self, event):
        if self.worker is None or self._operation != "translate":
            return
        self.table.blockSignals(True)
        try:
            if event["stage"] == "working":
                for row in event["rows"]:
                    self.table.item(row, 4).setText(self.tr_text("번역 중…", "Translating…"))
                    for column in range(self.table.columnCount()):
                        self.table.item(row, column).setBackground(QColor(COLORS["info_surface"]))
                if event["rows"]:
                    self.table.scrollToItem(self.table.item(event["rows"][0], 2))
                    self.status_label.setText(self.tr_text(
                        f"{event['rows'][0] + 1}~{event['rows'][-1] + 1}번째 가사를 번역하고 있습니다. 완료된 번역은 표에 바로 표시됩니다.",
                        f"Translating rows {event['rows'][0] + 1}–{event['rows'][-1] + 1}. Completed results appear in the table."))
            elif event["stage"] == "complete":
                row, text = event["row"], event["text"]
                self.table.item(row, 3).setText(text)
                if event.get("missing_lines"):
                    self._incomplete_rows.add(row)
                    self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
                    self.table.item(row, 4).setText(self.tr_text("번역 미완료", "Incomplete"))
                    lines = ", ".join(map(str, event["missing_lines"]))
                    self.table.item(row, 4).setToolTip(self.tr_text(
                        f"묶음 안 {lines}번째 줄에서 모델이 빈 결과를 반환했습니다. 모델의 언어 방향을 확인하거나 번역문을 직접 입력하세요. 이 행은 자동 선택하지 않습니다.",
                        f"The model returned no translation for line(s) {lines} in this cue. Check the model language pair or enter a translation. This row is not selected automatically."))
                else:
                    self.table.item(row, 4).setText(self.tr_text("완료", "Complete") if text else self.tr_text("번역 불필요", "Not needed"))
                for column in range(self.table.columnCount()):
                    self.table.item(row, column).setData(Qt.ItemDataRole.BackgroundRole, None)
                self.table.resizeRowToContents(row)
        finally:
            self.table.blockSignals(False)

    def _finished(self):
        worker, self.worker = self.worker, None
        self._settings_panel.setEnabled(True)
        self._set_review_busy(False)
        self.translate_button.setEnabled(bool(self._editable_rows))
        self.cancel_request_button.hide()
        self.progress.hide()
        if self._live_snapshot is not None and (worker.error or worker.cancel.is_set()):
            self._incomplete_rows = self._incomplete_snapshot
            self.table.blockSignals(True)
            for row, (text, checked, status, tooltip) in enumerate(self._live_snapshot):
                self.table.item(row, 3).setText(text)
                self.table.item(row, 0).setCheckState(checked)
                self.table.item(row, 4).setText(status)
                self.table.item(row, 4).setToolTip(tooltip)
                for column in range(self.table.columnCount()):
                    self.table.item(row, column).setData(Qt.ItemDataRole.BackgroundRole, None)
            self.table.blockSignals(False)
            self.table.resizeRowsToContents()
        if worker.error:
            self.status_label.setText(worker.error)
        elif worker.cancel.is_set():
            self.status_label.setText(self.tr_text("취소했습니다. 번역은 적용되지 않았습니다.", "Cancelled. No translations were applied."))
        elif self._operation == "translate" and worker.value is not None:
            self._draft_config, texts = worker.value
            self._new_draft = True
            self.attribution_label.setVisible(self._draft_config.provider == "papago" or any(
                approved_translation(cue) and cue["translation"]["provider"] == "papago" for cue in self.cues))
            self.table.blockSignals(True)
            for row, text in enumerate(texts):
                self.table.item(row, 3).setText(text)
                self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if row in self._editable_rows
                    and row not in self._incomplete_rows else Qt.CheckState.Unchecked)
                if row in self._editable_rows and row not in self._incomplete_rows:
                    self.table.item(row, 4).setText(self.tr_text("완료", "Complete") if text else self.tr_text("번역 불필요", "Not needed"))
            self.table.blockSignals(False)
            self.table.resizeRowsToContents()
            self._review_ready = True
            translated_count = sum(bool(text.strip()) for row, text in enumerate(texts) if row not in self._incomplete_rows)
            skipped_count = len(self._editable_rows) - translated_count - len(self._incomplete_rows)
            self.status_label.setText(self.tr_text(
                f"번역 {translated_count}줄 · 번역 불필요 {skipped_count}줄(빈칸). 원문과 비교한 뒤 적용하세요.",
                f"{translated_count} translated · {skipped_count} need no translation (blank). Review before applying."))
            if self._incomplete_rows:
                self.status_label.setText(self.status_label.text() + self.tr_text(
                    f" 번역 미완료 {len(self._incomplete_rows)}줄은 선택에서 제외했습니다. 상태 열에서 원인을 확인하세요.",
                    f" {len(self._incomplete_rows)} incomplete rows were left unselected. Check the Status column for details."))
        elif self._operation in {"signin", "models"} and worker.value is not None:
            previous = self._model_value()
            self.model_combo.clear()
            for slug, name in worker.value:
                self.model_combo.addItem(name, slug)
            index = self.model_combo.findData(previous)
            if index >= 0:
                self.model_combo.setCurrentIndex(index)
            if previous and index < 0:
                self.model_combo.setEditText(previous)
            self.status_label.setText(self.tr_text("사용 가능한 모델을 불러왔습니다.", "Available models loaded.") if worker.value else
                self.tr_text("선택 가능한 모델이 없습니다. 계정 권한을 확인하거나 사용 가능한 모델 ID를 입력하세요.",
                    "No models listed. Check account access or enter an available model ID."))
        elif self._operation == "local_llm_models" and worker.value is not None:
            self._installed_llms[self._provider] = worker.value
            selected = self._model_value() or str(self.settings.value("translation/config/" + self._provider + "/model", ""))
            self._populate_llm_models(worker.value, selected)
            self.status_label.setText(self.tr_text(
                f"설치된 로컬 LLM {len(worker.value)}개를 불러왔습니다.", f"Loaded {len(worker.value)} installed local LLMs.") if worker.value else self.tr_text(
                "설치된 로컬 LLM이 없습니다. 로컬 LLM 관리자에서 모델을 다운로드하세요.",
                "No local LLMs installed. Download one in the local LLM manager."))
        elif self._operation in {"install_local", "import_local"} and worker.value is not None:
            self._refresh_local_models(worker.value.id)
            if self.model_combo.findData(worker.value.id) >= 0:
                self.status_label.setText(self.tr_text("이 언어 방향의 모델을 설치했습니다. 번역을 시작할 수 있습니다.",
                    "Model installed for these languages. You can start translating."))
            else:
                self.status_label.setText(self.tr_text(
                    f"{worker.value.source} → {worker.value.target} 모델을 설치했습니다. 해당 언어를 선택하면 사용할 수 있습니다.",
                    f"Installed {worker.value.source} → {worker.value.target}. Select those languages to use it."))
        else:
            self.status_label.setText(self.tr_text("처리했습니다.", "Done."))
        self._refresh_account()
        self._update_selection()
        worker.deleteLater()
        if self._closing:
            self.reject()

    def _check_all(self, checked):
        self._commit_editor()
        self.table.blockSignals(True)
        for row in self._editable_rows:
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_selection()

    def _update_selection(self, *_args):
        if _args and isinstance(_args[0], QTableWidgetItem) and _args[0].column() == 3:
            row = _args[0].row()
            if row in self._editable_rows:
                if row in self._incomplete_rows and _args[0].text().strip():
                    self._incomplete_rows.discard(row)
                    self.table.item(row, 4).setText(self.tr_text("수정됨", "Edited"))
                    self.table.item(row, 4).setToolTip("")
                self._review_ready = True
                if self._draft_config is None:
                    self._draft_config = TranslationConfig("manual", target=self._language_value(self.target_combo))
                self.table.blockSignals(True)
                self.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
                self.table.blockSignals(False)
            self.table.resizeRowToContents(row)
        selected = sum(self.table.item(row, 0).checkState() == Qt.CheckState.Checked for row in range(self.table.rowCount()))
        removals = sum(self.table.item(row, 0).checkState() == Qt.CheckState.Checked
            and bool(self.cues[row].get("translation")) and not self.table.item(row, 3).text().strip()
            for row in range(self.table.rowCount()))
        self.selection_label.setText(self.tr_text(f"{selected}줄 선택", f"{selected} rows selected"))
        translated = sum(bool(self.table.item(row, 3).text().strip()) for row in range(self.table.rowCount()))
        self.review_summary.setText(self.tr_text(
            f"원문 {len(self._editable_rows)}줄 · 번역 {translated}줄 · 선택한 번역 삭제 {removals}줄",
            f"{len(self._editable_rows)} original rows · {translated} translated · {removals} selected for removal"))
        self.apply_button.setText(self.tr_text(f"선택한 {selected}줄 적용", f"Apply {selected} selected rows"))
        self.apply_button.setEnabled(self.worker is None and self._review_ready and selected > 0)

    def _commit_editor(self):
        for editor in self.table.findChildren(QPlainTextEdit):
            if editor.isVisible():
                delegate = self.table.itemDelegateForColumn(3)
                delegate.commitData.emit(editor)
                delegate.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)

    def _apply(self):
        self._commit_editor()
        if not self._review_ready or self.worker is not None or self._draft_config is None:
            return
        if not any(self.table.item(row, 0).checkState() == Qt.CheckState.Checked for row in self._editable_rows):
            return
        texts = [self.table.item(row, 3).text() if self.table.item(row, 0).checkState() == Qt.CheckState.Checked else None
                 for row in range(self.table.rowCount())]
        try:
            if self._new_draft:
                self.selected_cues = apply_translations(self.cues, texts, self._draft_config)
            else:
                # Selective translations can have different languages/providers.
                # Editing a saved row must retain that row's own provenance.
                reviewed = []
                for cue, text in zip(self.cues, texts):
                    metadata = cue.get("translation", {}) if approved_translation(cue) else {}
                    config = TranslationConfig(metadata.get("provider", "manual"),
                        target=metadata.get("language", self._language_value(self.target_combo)), model=metadata.get("model", ""))
                    reviewed.extend(apply_translations([cue], [text], config))
                self.selected_cues = reviewed
        except TranslationError as error:
            self.status_label.setText(str(error))
            return
        self.accept()

    def _cancel_request(self):
        if self.worker:
            self.worker.cancel.set()
            self.cancel_request_button.setEnabled(False)
            self.status_label.setText(self.tr_text("취소 중… 현재 요청이 끝나면 중단합니다.", "Cancelling… waiting for the current request to finish."))

    def done(self, result):
        if self.worker is not None:
            self._closing = True
            self._cancel_request()
            return
        self._commit_editor()
        dirty = self._new_draft or any(self.table.item(row, 3).text() != approved_translation(cue)
            for row, cue in enumerate(self.cues))
        if result != QDialog.DialogCode.Accepted and dirty and not self._confirm_discard():
            self._closing = False
            return
        try:
            self._save_settings()
        except TranslationError:
            pass
        super().done(result)

    def _confirm_discard(self):
        message = QMessageBox(self)
        message.setWindowTitle(self.tr_text("변경사항이 있습니다", "Unsaved changes"))
        message.setIcon(QMessageBox.Icon.Warning)
        message.setText(self.tr_text("변경사항이 있습니다. 적용하지 않고 닫으면 번역 및 수정 내용을 잃게 됩니다.",
            "You have unapplied changes. Closing will discard translated or edited lyrics."))
        discard = message.addButton(self.tr_text("변경사항 버리고 닫기", "Discard and close"), QMessageBox.ButtonRole.DestructiveRole)
        keep = message.addButton(self.tr_text("계속 검토", "Keep reviewing"), QMessageBox.ButtonRole.RejectRole)
        message.setDefaultButton(keep)
        message.exec()
        return message.clickedButton() == discard
