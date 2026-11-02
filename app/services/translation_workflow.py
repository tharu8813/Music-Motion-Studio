"""One cancellable workspace job shared by the translation windows."""
from copy import deepcopy
from dataclasses import replace

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from app.services.lyrics_translation_service import TranslationError
from app.services.translation_worker import TranslationWorker


class TranslationWorkflow(QObject):
    busy_changed = Signal(bool)
    completed = Signal()
    status_changed = Signal(str)
    progress_changed = Signal(int, int)

    def __init__(self, service, auth, draft, services, review, parent=None):
        super().__init__(parent)
        self.service, self.auth, self.draft = service, auth, draft
        self.services, self.review = services, review
        self.worker, self.operation = None, ""
        self._rows, self._snapshot = [], None
        self.status = ""

    def tr_text(self, ko, en):
        return self.services.tr_text(ko, en)

    def set_status(self, message):
        self.status = message
        self.status_changed.emit(message)

    def translate(self, rows=None):
        if self.worker:
            return
        manager = getattr(self.services, "manager", None)
        if manager is not None and manager.isVisible():
            manager.raise_()
            self.set_status(self.tr_text("모델 관리를 마친 뒤 창을 닫고 번역을 시작하세요.",
                                        "Close model management before translating."))
            return
        review = self.review
        review._commit_editor()
        review.capture_draft()
        config = self.services._config()
        selected = sorted(set(rows) & review._editable_rows) if rows is not None else sorted(review._editable_rows)
        if not selected:
            self.set_status(self.tr_text("재번역할 행을 먼저 선택하세요.", "Select rows to retranslate first."))
            return
        try:
            self.service.prepare(self.draft.cues)
            if config.provider == "local_nmt" and not config.same_language and not config.model:
                raise TranslationError(self.services.local_pair_note.text())
            check = replace(config, key="pending") if config.provider == "chatgpt" else config
            check.validate()
            self.services._save_settings()
        except TranslationError as error:
            self.set_status(str(error))
            self.services.show()
            self.services.raise_()
            return
        if self.draft.has_result:
            message = self.tr_text(
                f"{len(selected)}개 행의 번역 초안을 바꿉니다. 직접 수정한 내용도 교체됩니다. 계속할까요?",
                f"Replace the draft for {len(selected)} rows, including manual edits. Continue?")
            if QMessageBox.question(self.parent(), self.tr_text("가사 재번역", "Retranslate lyrics"), message,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                return
        self._rows = selected
        # Keep global cue IDs and surrounding context for selective retranslations.
        def work(cancel, progress):
            connected = replace(config, key=self.auth.access_token(cancel)) if config.provider == "chatgpt" and not config.same_language else config
            return (config, self.service.translate(self.draft.cues, connected, cancel, progress,
                activity=self.worker.activity.emit, **({"selected_rows": selected} if rows is not None else {})))
        self.start("translate", work)

    def start(self, operation, work):
        if self.worker:
            return
        self.review._commit_editor()
        self.review.capture_draft()
        self.operation = operation
        self._snapshot = deepcopy(self.draft.rows) if operation == "translate" else None
        if operation == "translate" and not self._rows:
            self._rows = sorted(self.review._editable_rows)
        if operation == "translate":
            self.review._incomplete_rows.difference_update(self._rows)
            for row in self._rows:
                self.review.table.item(row, 4).setToolTip("")
        self._previous_result = self.draft.has_result
        self.worker = TranslationWorker(work, self)
        self.worker.finished.connect(self._finished)
        self.worker.progress.connect(self.progress_changed)
        self.worker.activity.connect(self.review._activity)
        self.review._set_review_busy(True)
        self.review.apply_button.setEnabled(False)
        self.services._settings_panel.setEnabled(False)
        self.busy_changed.emit(True)
        messages = {
            "translate": ("번역 중… 아래 표에 완료된 결과가 실시간으로 표시됩니다.", "Translating… completed results appear in the table below."),
            "models": ("사용 가능한 모델을 불러오는 중…", "Loading available models…"),
            "connection": ("인증 정보와 연결을 확인하는 중…", "Checking credentials and connection…"),
            "local_llm_models": ("설치된 로컬 LLM을 조회하는 중…", "Checking installed local LLMs…"),
            "install_local": ("선택한 언어 모델을 내려받아 설치하는 중…", "Downloading and installing the language model…"),
            "import_local": ("모델 파일을 확인하고 설치하는 중…", "Checking and installing the model file…"),
        }
        self.set_status(self.tr_text(*messages.get(operation, ("처리 중…", "Working…"))))
        self.worker.start()

    def cancel(self):
        if self.worker:
            self.worker.cancel.set()
            self.set_status(self.tr_text("취소 중… 현재 요청이 끝나면 중단합니다.",
                                        "Cancelling… waiting for the current request to finish."))

    def _finished(self):
        worker, self.worker = self.worker, None
        review, services = self.review, self.services
        review._set_review_busy(False)
        services._settings_panel.setEnabled(True)
        if worker.error or worker.cancel.is_set():
            if self._snapshot is not None:
                self.draft.rows = self._snapshot
                self.draft.has_result = self._previous_result
                # Retain earlier edited/saved translations; failed rows remain retryable.
                if worker.error:
                    for row in self._rows:
                        self.draft.rows[row].status = "failed"
                        self.draft.rows[row].detail = worker.error
                        self.draft.rows[row].checked = False
                review.restore_draft()
                self.draft.save()
            self.set_status(worker.error or self.tr_text("취소했습니다. 번역은 적용되지 않았습니다.",
                                                        "Cancelled. No translations were applied."))
        elif self.operation == "translate" and worker.value is not None:
            config, texts = worker.value
            # Activity can report incomplete physical lines before final results arrive.
            incomplete = review._incomplete_rows.copy()
            for index in self._rows:
                row = self.draft.rows[index]
                row.text, row.edited = texts[index], False
                row.config = self.draft.safe_config(config)
                row.checked = index not in incomplete
                row.status = "incomplete" if index in incomplete else "complete" if row.text else "unneeded"
                row.detail = review.table.item(index, 4).toolTip() if index in incomplete else ""
            self.draft.has_result = True
            review.restore_draft()
            count = sum(bool(self.draft.rows[i].text.strip()) for i in self._rows if i not in incomplete)
            missing = len(set(self._rows) & incomplete)
            self.set_status(self.tr_text(f"번역 {count}줄 · 번역 미완료 {missing}줄. 결과를 검토한 뒤 적용하세요.",
                f"{count} translated · {missing} incomplete. Review before applying."))
            self.draft.save()
        elif self.operation in {"models", "connection"} and worker.value is not None:
            previous = services._model_value()
            services.model_combo.clear()
            for slug, name in worker.value:
                services.model_combo.addItem(name, slug)
            index = services.model_combo.findData(previous)
            if index >= 0:
                services.model_combo.setCurrentIndex(index)
            elif previous:
                services.model_combo.setEditText(previous)
            self.set_status(self.tr_text("연결됨 · 사용 가능한 모델을 불러왔습니다.", "Connected · available models loaded.") if worker.value else
                self.tr_text("연결됨 · 모델 목록이 비었습니다. 고급 설정에서 사용 가능한 모델 ID를 입력하세요.",
                             "Connected · no models listed. Enter an available model ID in Advanced settings."))
        elif self.operation == "local_llm_models" and worker.value is not None:
            services._installed_llms[services._provider] = worker.value
            previous = services._model_value() or str(services.settings.value("translation/config/" + services._provider + "/model", ""))
            services._populate_llm_models(worker.value, previous)
            self.set_status(self.tr_text(f"설치된 로컬 LLM {len(worker.value)}개를 불러왔습니다.",
                f"Loaded {len(worker.value)} installed local LLMs.") if worker.value else self.tr_text(
                "설치된 로컬 LLM이 없습니다. 로컬 LLM 관리자에서 모델을 다운로드하세요.",
                "No local LLMs installed. Download one in the local LLM manager."))
        elif self.operation in {"install_local", "import_local"} and worker.value is not None:
            services._refresh_local_models(worker.value.id)
            self.set_status(self.tr_text("언어 모델을 설치했습니다. 번역을 시작할 수 있습니다.",
                                        "Language model installed. You can start translating."))
        self._rows = []
        self._snapshot = None
        services._refresh_account()
        services.changed.emit()
        review._update_selection()
        worker.deleteLater()
        self.busy_changed.emit(False)
        if self.operation == "translate" and not worker.error and not worker.cancel.is_set() and worker.value is not None:
            self.completed.emit()
