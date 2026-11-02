"""Unified translation and review workspace with separate technical settings."""
from copy import deepcopy
from PySide6.QtCore import QSettings, QTimer, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QGridLayout, QHBoxLayout,
    QLabel, QProgressBar, QPushButton, QVBoxLayout, QSizePolicy)
from app.dialogs.help_dialog import install_help_shortcut
from app.dialogs.translation_service_dialog import TranslationServiceDialog
from app.dialogs.translation_advanced_dialog import TranslationAdvancedDialog
from app.dialogs.translation_review_panel import TranslationReviewPanel
from app.services.chatgpt_auth_service import ChatGPTAuthService
from app.services.local_llm_service import LocalLLMService
from app.services.lyrics_translation_service import LyricsTranslationService, TranslationError, CLOUD_LLM_PROVIDERS, LOCAL_LLM_PROVIDERS
from app.services.translation_credentials import TranslationCredentials
from app.services.translation_draft import TranslationDraft
from app.services.translation_workflow import TranslationWorkflow


class LyricsTranslationDialog(QDialog):
    style_requested = Signal()

    def __init__(self, cues, translator, parent=None, *, service=None, auth=None,
                 settings=None, local_llm_factory=None, draft_key="", style_callback=None):
        super().__init__(parent)
        self.translator, self.cues = translator, deepcopy(cues)
        self.selected_cues = deepcopy(cues)
        self.settings = settings if settings is not None else QSettings()
        self.credentials = TranslationCredentials(self.settings)
        self.auth = auth or ChatGPTAuthService(self.credentials, self.settings)
        self.service = service or LyricsTranslationService()
        self._closing = False
        self.setObjectName("lyricsTranslationDialog")
        self.setWindowTitle(self.tr_text("가사 번역 · 검토 및 편집", "Translate lyrics · Review and edit"))
        self.resize(1180, 780)
        self.setMinimumSize(800, 580)
        install_help_shortcut(self, ("lyrics_editor", "lyrics_translation"), translator=translator)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(10)
        title = QLabel(self.tr_text("가사 번역", "Translate lyrics"))
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        controls = QGridLayout()
        controls.setHorizontalSpacing(16)
        self.mode_combo = QComboBox()
        for ko, en, value in (("클라우드 LLM", "Cloud LLM", "cloud_llm"), ("로컬 LLM", "Local LLM", "local_llm"),
                              ("로컬 번역 모델", "Local translation model", "local")):
            self.mode_combo.addItem(self.tr_text(ko, en), value)
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


        for column, (ko, en, widget) in enumerate((("원문 언어", "Source language", self.source_combo),
                                                  ("번역 언어", "Target language", self.target_combo),
                                                  ("번역 방식", "Translation mode", self.mode_combo))):
            label = QLabel(self.tr_text(ko, en))
            label.setBuddy(widget)
            widget.setAccessibleName(label.text())
            widget.setMinimumContentsLength(8)
            widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            controls.addWidget(label, 0, column)
            controls.addWidget(widget, 1, column)
            controls.setColumnStretch(column, 1)
        root.addLayout(controls)
        actions = QHBoxLayout()
        self.selection_summary = QLabel()
        self.selection_summary.setWordWrap(True)
        self.selection_summary.setTextFormat(Qt.TextFormat.PlainText)
        actions.addWidget(self.selection_summary, 1)
        self.setup_button = QPushButton(self.tr_text("서비스 및 모델 설정…", "Service and model settings…"))
        self.translate_button = QPushButton(self.tr_text("번역 시작", "Translate"))
        self.translate_button.setProperty("primary", True)
        self.cancel_request_button = QPushButton(self.tr_text("요청 취소", "Cancel request"))
        self.cancel_request_button.hide()
        actions.addWidget(self.setup_button)
        actions.addWidget(self.translate_button)
        actions.addWidget(self.cancel_request_button)
        root.addLayout(actions)
        self.readiness_label = QLabel()
        self.readiness_label.setWordWrap(True)
        self.readiness_label.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(self.readiness_label)
        self.privacy_note = QLabel()
        self.privacy_note.setWordWrap(True)
        self.privacy_note.setObjectName("mutedLabel")
        root.addWidget(self.privacy_note)
        self.progress = QProgressBar()
        self.progress.hide()
        root.addWidget(self.progress)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label.hide()
        root.addWidget(self.status_label)
        self.draft = TranslationDraft(cues, self.settings, identity=draft_key, parent=self)
        self.service_dialog = TranslationServiceDialog(translator, self, service=self.service,
            auth=self.auth, settings=self.settings, local_llm_factory=local_llm_factory or LocalLLMService,
            source_combo=self.source_combo, target_combo=self.target_combo, mode_combo=self.mode_combo)
        self.review_panel = TranslationReviewPanel(cues, translator, self.draft, self)
        root.addWidget(self.review_panel, 1)
        self.review_panel.target_combo = self.target_combo
        self.workflow = TranslationWorkflow(self.service, self.auth, self.draft,
            self.service_dialog, self.review_panel, self)
        self.service_dialog.workflow = self.review_panel.workflow = self.workflow
        self.advanced_dialog = TranslationAdvancedDialog(self.service_dialog, self)
        self.setup_button.clicked.connect(lambda: self._show_window(self.service_dialog))
        self.translate_button.clicked.connect(lambda: self.workflow.translate())
        self.cancel_request_button.clicked.connect(self.workflow.cancel)
        self.service_dialog.advanced_requested.connect(lambda: self._show_window(self.advanced_dialog))
        self.review_panel.retry_requested.connect(self.workflow.translate)
        self.review_panel.applied.connect(self._applied)
        self.review_panel.close_requested.connect(self.reject)
        self.review_panel.style_requested.connect(self.style_requested)
        if style_callback:
            self.style_requested.connect(lambda: style_callback(self))
        else:
            self.review_panel.style_button.setEnabled(False)
            self.review_panel.style_button.setToolTip(self.tr_text("캔버스의 가사 소스에서 번역문 스타일을 설정할 수 있습니다.",
                "Set translation typography on the canvas Lyrics source."))
        self.workflow.busy_changed.connect(self._busy_changed)
        self.workflow.status_changed.connect(self._status_changed)
        self.workflow.progress_changed.connect(self._progress)
        self.workflow.completed.connect(self._translation_completed)
        self.service_dialog.changed.connect(self._refresh_summary)
        self.source_combo.currentTextChanged.connect(self._refresh_summary)
        self.target_combo.currentTextChanged.connect(self._refresh_summary)
        self.draft.changed.connect(lambda: self._draft_timer.start())
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        self._draft_timer.timeout.connect(self.draft.save)
        saved = str(self.settings.value("translation/provider", "chatgpt"))
        mode = "cloud_llm" if saved in CLOUD_LLM_PROVIDERS else "local_llm" if saved in LOCAL_LLM_PROVIDERS else "local"
        self.settings.setValue("translation/last_" + mode, saved)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode))
        self.mode_combo.blockSignals(False)
        self.service_dialog._mode_changed()
        self._refresh_summary()
        self.review_panel._update_selection()
        if self.draft.restored:
            self._status_changed(self.tr_text("이전에 저장한 번역 초안을 복원했습니다. 아래 표에서 이어서 편집하세요.",
                                              "Saved draft restored. Continue editing in the table below."))
        # Keep existing integration accessors while the editor and settings own their controls.
        for name in ("provider_combo", "model_combo", "key_edit", "endpoint_edit", "instructions_edit",
                     "remember_check", "local_models_button", "local_pair_note", "local_accuracy_warning"):
            setattr(self, name, getattr(self.service_dialog, name))
        for name in ("table", "apply_button", "selection_label", "review_summary"):
            setattr(self, name, getattr(self.review_panel, name))
        for name in ("_model_value", "_config", "_save_settings", "_install_local_pair", "_load_models"):
            setattr(self, name, getattr(self.service_dialog, name))
        for name in ("_apply", "_check_all"):
            setattr(self, name, getattr(self.review_panel, name))
        self._translate, self._start, self._cancel_request = self.workflow.translate, self.workflow.start, self.workflow.cancel
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    def _translation_completed(self):
        if not self._closing:
            self.review_panel.table.setFocus(Qt.FocusReason.OtherFocusReason)

    def tr_text(self, ko, en):
        return ko if self.translator.is_korean else en

    _language_value = staticmethod(TranslationServiceDialog._language_value)

    @property
    def worker(self):
        return self.workflow.worker

    @property
    def _incomplete_rows(self):
        return self.review_panel._incomplete_rows

    def _show_window(self, window):
        window.show()
        window.raise_()
        window.activateWindow()

    def _refresh_summary(self, *_args):
        s = self.service_dialog
        if not s._provider:
            return
        config = s._config()
        self.selection_summary.setText(s.provider_combo.currentText() + " · " + (s.model_combo.currentText() or self.tr_text("모델 선택 필요", "Choose a model")))
        try:
            if config.provider == "chatgpt" and not config.same_language:
                profile = self.auth.profile()
                if not profile:
                    raise TranslationError(self.tr_text("ChatGPT 로그인이 필요합니다. 서비스 설정을 열어 연결하세요.",
                                                       "Sign in to ChatGPT in service settings."))
                from dataclasses import replace
                config = replace(config, key="pending")
            config.validate()
            message = self.tr_text("번역 준비 완료", "Ready to translate")
        except TranslationError as error:
            message = str(error)
        self.readiness_label.setText(message)
        local = s._provider not in CLOUD_LLM_PROVIDERS
        self.privacy_note.setText(self.tr_text("이 컴퓨터에서 번역합니다.", "Translation runs on this computer.") if local else
            self.tr_text("번역을 시작하면 선택한 클라우드 서비스로 가사를 전송합니다.", "Starting translation sends lyrics to the selected cloud service."))
        if s._provider == "local_nmt":
            self.privacy_note.setText(self.tr_text("비LLM 번역은 이 컴퓨터에서 실행합니다. 가사·비유·문맥 번역이 부정확할 수 있으므로 결과를 꼭 검토하세요.",
                "Non-LLM translation runs locally. Review carefully: lyrics, metaphors and context may be inaccurate."))
        manager = getattr(s, "manager", None)
        managing = manager is not None and manager.isVisible()
        self.translate_button.setEnabled(bool(self.review_panel._editable_rows) and self.worker is None and not managing)
        if managing:
            self.readiness_label.setText(self.tr_text("모델 관리를 마친 뒤 창을 닫고 번역을 시작하세요.",
                                                    "Close model management before translating."))
        if not self.review_panel._editable_rows:
            self.readiness_label.setText(self.tr_text("번역할 가사가 없습니다. 원문 가사를 먼저 추가하세요.", "Add original lyrics before translating."))

    def _status_changed(self, text):
        for window in (self, self.service_dialog):
            window.status_label.setText(text)
            window.status_label.setVisible(bool(text))

    def _progress(self, value, total):
        for window in (self, self.service_dialog):
            window.progress.setRange(0, total)
            window.progress.setValue(value)
            window.progress.setFormat(self.tr_text(f"{value} / {total}줄 완료", f"{value} / {total} rows complete"))

    def _busy_changed(self, busy):
        for window in (self, self.service_dialog):
            window.progress.setVisible(busy)
            window.progress.setRange(0, 0)
        self.mode_combo.setEnabled(not busy)
        self.source_combo.setEnabled(not busy)
        self.target_combo.setEnabled(not busy)
        self.advanced_dialog.setEnabled(not busy)
        self.service_dialog.test_button.setEnabled(not busy)
        self.service_dialog.advanced_button.setEnabled(not busy)
        self.service_dialog.cancel_button.setVisible(busy)
        self.cancel_request_button.setVisible(busy)
        self.review_panel.retry_selected.setEnabled(not busy)
        self.review_panel.retry_failed.setEnabled(not busy)
        self.review_panel._update_retry_selection()
        self.translate_button.setEnabled(not busy and bool(self.review_panel._editable_rows))
        if not busy and self._closing:
            self.reject()

    def _applied(self):
        self._draft_timer.stop()
        self.selected_cues = deepcopy(self.review_panel.selected_cues)
        self.accept()

    def done(self, result):
        manager = getattr(self.service_dialog, "manager", None)
        if manager and manager.worker:
            self._closing = True
            manager.reject()
            QTimer.singleShot(50, lambda: self.done(result))
            return
        if self.worker:
            self._closing = True
            self.workflow.cancel()
            return
        self._draft_timer.stop()
        self.review_panel._commit_editor()
        self.review_panel.capture_draft()
        self._draft_timer.stop()
        if result != QDialog.DialogCode.Accepted:
            self.draft.save()
        else:
            self.draft.clear()
        try:
            self.service_dialog._save_settings()
        except TranslationError:
            pass
        for window in (self.service_dialog, self.advanced_dialog):
            window.hide()
        if manager:
            manager.hide()
        super().done(result)
