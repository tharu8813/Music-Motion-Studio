"""Provider configuration and preparation, independent of lyric editing."""
from __future__ import annotations


from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QFileDialog, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget

from app.dialogs.help_dialog import install_help_shortcut
from app.services.local_translation_service import LocalTranslationModels
from app.services.local_llm_service import LOCAL_LLM_PROVIDERS, CLOUD_LLM_PROVIDERS
from app.ui.design_system import COLORS
from app.services.lyrics_translation_service import LLM_PROVIDERS, PROVIDERS, TranslationConfig, TranslationError
from app.services.translation_credentials import TranslationCredentials


class TranslationServiceDialog(QDialog):
    changed = Signal()
    advanced_requested = Signal()

    def __init__(self, translator, parent=None, *, service, auth, settings, local_llm_factory,
                 source_combo, target_combo, mode_combo):
        super().__init__(parent)
        install_help_shortcut(self, ("lyrics_editor", "lyrics_translation"), translator=translator)
        self.translator, self.service, self.auth = translator, service, auth
        self.settings, self.local_llm_factory = settings, local_llm_factory
        self.local_models = service.local_models or LocalTranslationModels()
        self.credentials = TranslationCredentials(settings)
        self.source_combo, self.target_combo, self.mode_combo = source_combo, target_combo, mode_combo
        self._provider, self._changing_provider, self._installed_llms = "", False, {}
        self.workflow = None
        self.setObjectName("translationServiceDialog")
        self.setWindowTitle(self.tr_text("번역 서비스 및 모델 설정", "Translation service and models"))
        self.resize(640, 620)
        self.setMinimumSize(460, 360)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        title = QLabel(self.windowTitle())
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        panel = QWidget()
        form = QFormLayout(panel)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(12)
        self.provider_combo = QComboBox()
        for key, name in PROVIDERS.items():
            if key in CLOUD_LLM_PROVIDERS:
                self.provider_combo.addItem(name, key)
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
            ("provider", "서비스 / 실행 방식", "Provider / engine", self.provider_combo),
            ("model", "모델", "Model", self.model_combo),
            ("key", "API 키", "API key", self.key_edit),
        ):
            label = QLabel(self.tr_text(ko, en))
            label.setBuddy(widget)
            widget.setAccessibleName(label.text())
            form.addRow(label, widget)
            self._rows[key] = (label, widget)
        for widget in (self.account_label, self.account_button, self.models_button,
                       self.llm_manager_button, self.local_pair_note, self.local_models_button,
                       self.import_local_button, self.remember_check, self.local_accuracy_warning,
                       self.provider_note):
            form.addRow(widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(panel)
        root.addWidget(scroll, 1)
        self._settings_panel = panel
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.hide()
        root.addWidget(self.progress)
        footer = QHBoxLayout()
        self.test_button = QPushButton(self.tr_text("연결 테스트", "Test connection"))
        self.advanced_button = QPushButton(self.tr_text("고급 설정…", "Advanced settings…"))
        self.cancel_button = QPushButton(self.tr_text("작업 취소", "Cancel operation"))
        self.cancel_button.hide()
        footer.addWidget(self.test_button)
        footer.addWidget(self.advanced_button)
        footer.addWidget(self.cancel_button)
        footer.addStretch()
        close = QPushButton(self.tr_text("완료", "Done"))
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        root.addLayout(footer)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.source_combo.currentTextChanged.connect(self._pair_changed)
        self.target_combo.currentTextChanged.connect(self._pair_changed)
        self.models_button.clicked.connect(self._load_models)
        self.account_button.clicked.connect(self._open_account)
        self.local_models_button.clicked.connect(self._install_local_pair)
        self.import_local_button.clicked.connect(self._import_local_model)
        self.llm_manager_button.clicked.connect(self._open_llm_manager)
        self.test_button.clicked.connect(self._test_connection)
        self.advanced_button.clicked.connect(self.advanced_requested)
        self.cancel_button.clicked.connect(lambda: self.workflow.cancel())
        for widget_signal in (self.model_combo.currentTextChanged, self.key_edit.textChanged,
                              self.endpoint_edit.textChanged, self.instructions_edit.textChanged):
            widget_signal.connect(self.changed)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    @property
    def worker(self):
        return self.workflow.worker if self.workflow else None

    def _start(self, operation, work):
        if self.workflow:
            self.workflow.start(operation, work)

    def done(self, result):
        try:
            self._save_settings()
        except TranslationError as error:
            self.status_label.setText(str(error))
            return
        self.changed.emit()
        super().done(result)

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
            self.model_combo.lineEdit().setReadOnly(True)
            self.model_combo.lineEdit().setPlaceholderText(self.tr_text("모델 목록에서 선택 · 직접 입력은 고급 설정", "Choose a model · manual ID in Advanced settings"))
            self.model_combo.setEditText(str(self.settings.value(base + "model", "")))
        defaults = {"ollama": "http://localhost:11434/v1", "lmstudio": "http://localhost:1234/v1"}
        self.endpoint_edit.setText(str(self.settings.value(base + "endpoint", defaults.get(provider, ""))))
        self.instructions_edit.setPlainText(str(self.settings.value(base + "instructions", "")))
        self._set_language(self.target_combo, str(self.settings.value(base + "target", "ko")))
        self._set_language(self.source_combo, str(self.settings.value(base + "source", "en" if provider == "local_nmt" else "auto")))
        credentials = self.credentials.load(provider)
        self.key_edit.setText(credentials.get("key", ""))
        self.remember_check.setChecked(bool(self.settings.value("translation/secrets/" + provider, "")))
        for key, visible in {"model": True, "provider": provider != "local_nmt", "key": provider not in {"chatgpt", "local_nmt", "ollama"}}.items():
            for widget in self._rows[key]:
                widget.setVisible(visible)
        for widget in (self.account_button, self.account_label):
            widget.setVisible(provider == "chatgpt")
        self.models_button.setVisible(provider in LLM_PROVIDERS)
        self.models_button.setText(self.tr_text("설치된 모델 새로고침", "Refresh installed models") if provider in LOCAL_LLM_PROVIDERS else self.tr_text("모델 불러오기", "Load models"))
        self.llm_manager_button.setVisible(provider in LOCAL_LLM_PROVIDERS)
        for widget in (self.local_models_button, self.import_local_button, self.local_pair_note, self.local_accuracy_warning):
            widget.setVisible(provider == "local_nmt")
        self.remember_check.setVisible(provider not in {"chatgpt", "local_nmt", "ollama"})
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
        self.changed.emit()

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
        elif self._provider in CLOUD_LLM_PROVIDERS:
            self._test_connection()

    def _open_llm_manager(self):
        if self.worker:
            return
        from app.dialogs.local_llm_manager_dialog import LocalLLMManagerDialog
        try:
            service = self._llm_service()
        except TranslationError as error:
            self.status_label.setText(str(error))
            return
        if getattr(self, "manager", None) is not None and self.manager.isVisible():
            self.manager.raise_()
            self.manager.activateWindow()
            return
        previous = getattr(self, "manager", None)
        if previous:
            previous.deleteLater()
        self.manager = LocalLLMManagerDialog(self.translator, self.parentWidget(), service=service)
        dialog = self.manager
        provider = self._provider
        def finished(_result):
            self._installed_llms[provider] = dialog.models
            if self._provider == provider:
                if dialog._discovered:
                    self._populate_llm_models(dialog.models, dialog.selected_model or self._model_value())
                try:
                    self._save_settings()
                except TranslationError as error:
                    self.status_label.setText(str(error))
                self.changed.emit()
        self.manager.finished.connect(finished)
        self.manager.busy_changed.connect(self.changed)
        self.manager.show()
        self.changed.emit()

    def _open_account(self):
        from app.dialogs.openai_account_dialog import OpenAIAccountDialog
        dialog = OpenAIAccountDialog(self.translator, self, auth=self.auth)
        dialog.exec()
        self._refresh_account()
        dialog.deleteLater()

    def _refresh_account(self):
        try:
            profile = self.auth.profile()
        except TranslationError as error:
            profile = self.credentials.load("chatgpt")
            self.status_label.setText(str(error))
        self.account_label.setText(profile.get("email") or self.tr_text("연결된 ChatGPT 계정 없음", "No ChatGPT account connected"))
        self.models_button.setEnabled(bool(profile) if self._provider == "chatgpt" else True)

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

    def _test_connection(self):
        from app.services.translation_connection import available_models
        config = self._config()
        if config.provider in LOCAL_LLM_PROVIDERS or config.provider == "chatgpt":
            self._load_models()
        elif config.provider == "local_nmt":
            self._refresh_local_models(self._model_value())
            self.status_label.setText(self.local_pair_note.text())
        else:
            self._start("connection", lambda cancel, _progress: available_models(self.service.request, config, cancel))

