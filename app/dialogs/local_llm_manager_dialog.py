"""Manage local LLMs without requiring users to work in a terminal."""
import json

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget, QScrollArea,
    QGridLayout, QComboBox)

from app.dialogs.help_dialog import install_help_shortcut
from app.services.local_llm_service import LocalLLMService, MODEL_GUIDES, model_guide
from app.services.translation_worker import TranslationWorker


class LocalLLMManagerDialog(QDialog):
    busy_changed = Signal(bool)

    def __init__(self, translator, parent=None, *, service=None):
        super().__init__(parent)
        self.translator, self.service = translator, service or LocalLLMService()
        self.worker, self._closing, self.selected_model = None, False, ""
        self.models = []
        self._discovered = False
        install_help_shortcut(self, ("lyrics_editor", "lyrics_translation"), translator=translator)
        self.setWindowTitle(self.tr_text("로컬 LLM 관리자", "Local LLM manager"))
        self.resize(1000, 760)
        self.setMinimumSize(600, 420)
        self.setObjectName("localLLMManagerDialog")
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        scroll.setWidget(panel)
        outer.addWidget(scroll)
        root = QVBoxLayout(panel)
        root.setContentsMargins(22, 20, 22, 20)
        title = QLabel(self.windowTitle())
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        note = QLabel(self.tr_text(
            "설치된 모델을 선택하거나 모델을 내려받으세요. 모델 크기는 다운로드 용량이며 실제 메모리는 문맥 길이·양자화·장치에 따라 더 필요합니다.",
            "Choose an installed model or download one. Download size is not required RAM; memory also depends on context, quantization and hardware."))
        note.setWordWrap(True)
        root.addWidget(note)
        self.steps_label = QLabel(self.tr_text(
            "① 실행 준비  →  ② 모델 찾기·설치  →  ③ 설치된 모델 선택 후 사용",
            "① Prepare engine  →  ② Find and install  →  ③ Select an installed model"))
        self.steps_label.setObjectName("panelTitle")
        self.steps_label.setWordWrap(True)
        root.addWidget(self.steps_label)
        self.runtime_note = QLabel(self.tr_text(
            "처음이라면 ‘실행 구성요소 자동 설치·시작’을 누르세요. 공식 Ollama Windows x64 파일(수 GB)을 검증해 앱 전용 폴더에 설치합니다. 이미 실행 중인 Ollama는 그대로 사용합니다.",
            "New to local LLMs? Automatic setup verifies and installs the official Ollama Windows x64 runtime (several GB) in this app's folder. An existing Ollama server is reused.") if self.service.provider == "ollama" else self.tr_text(
            "LM Studio 서버를 켜세요. 모델 조회·다운로드·메모리 로드는 최신 REST API를 사용합니다. 파일 삭제와 프로그램 설치는 LM Studio에서 진행합니다.",
            "Start the LM Studio server. Listing, downloads and memory loading use its current REST API. Install LM Studio and delete files in LM Studio."))
        self.runtime_note.setWordWrap(True)
        links = QLabel('<a href="https://docs.ollama.com/windows">Ollama 설치 안내</a> · '
            '<a href="https://lmstudio.ai/docs/app">LM Studio 설치 안내</a>')
        links.setOpenExternalLinks(True)
        actions = QHBoxLayout()
        self.setup_button = QPushButton(self.tr_text("AI 실행 환경 준비하기", "Set up local AI engine"))
        self.setup_button.setToolTip(self.tr_text(
            "Ollama를 자동으로 준비하거나 이미 실행 중인 Ollama에 연결합니다.",
            "Prepare Ollama automatically, or reuse an existing running Ollama server."))
        self.setup_button.setVisible(self.service.provider == "ollama")
        self.refresh_button = QPushButton(self.tr_text("모델 목록 새로고침", "Refresh models"))
        actions.addWidget(self.setup_button)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        root.addLayout(actions)
        self.tabs = QTabWidget()
        installed_page, download_page, advanced_page = QWidget(), QWidget(), QWidget()
        advanced_layout = QVBoxLayout(advanced_page)
        advanced_layout.addWidget(self.runtime_note)
        advanced_layout.addWidget(links)
        server = QLabel(self.tr_text("연결 중인 로컬 서버: ", "Local server: ") + str(getattr(self.service, "base", "")))
        server.setWordWrap(True)
        server.setTextFormat(Qt.TextFormat.PlainText)
        advanced_layout.addWidget(server)
        installed_layout, download_layout = QVBoxLayout(installed_page), QVBoxLayout(download_page)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr_text("설치 모델 검색", "Search installed models"))
        installed_layout.addWidget(self.search)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels([self.tr_text("모델", "Model"), self.tr_text("파일 용량", "File size"),
            self.tr_text("매개변수", "Parameters"), self.tr_text("양자화", "Quantization")])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 4):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(180)
        self.table.setAccessibleName(self.tr_text("설치된 로컬 LLM", "Installed local LLMs"))
        installed_layout.addWidget(self.table)
        advanced_actions = QGridLayout()
        self.load_button = QPushButton(self.tr_text("메모리에 로드", "Load into memory"))
        self.unload_button = QPushButton(self.tr_text("메모리 해제", "Unload from memory"))
        self.details_button = QPushButton(self.tr_text("상세 정보·라이선스", "Details / license"))
        self.alias_button = QPushButton(self.tr_text("별칭 만들기", "Create alias"))
        self.delete_button = QPushButton(self.tr_text("모델 삭제…", "Delete model…"))
        for index, button in enumerate((self.load_button, self.unload_button, self.delete_button)):
            advanced_actions.addWidget(button, index // 2, index % 2)
        advanced_layout.addLayout(advanced_actions)
        advanced_layout.addWidget(self.alias_button)
        self.metadata = QTextBrowser()
        self.metadata.setMinimumHeight(180)
        advanced_layout.addWidget(self.metadata, 1)
        self.alias_button.setVisible(self.service.provider == "ollama")
        self.delete_button.setVisible(self.service.provider == "ollama")
        installed_layout.addWidget(self.details_button)
        download_layout.addWidget(QLabel(self.tr_text(
            "모델 찾기 · 실제 가사 품질 순위가 아니며 코딩 모델도 번역 정확도를 보장하지 않습니다.",
            "Find models · not a lyric-quality ranking; coding models do not guarantee translation accuracy.")))
        catalog_filters = QHBoxLayout()
        self.catalog_search = QLineEdit()
        self.catalog_search.setPlaceholderText(self.tr_text("모델 이름·특징 검색", "Search model names or features"))
        self.catalog_category = QComboBox()
        self.catalog_category.addItem(self.tr_text("모든 종류", "All types"), "all")
        self.catalog_category.addItem(self.tr_text("코딩 · JSON", "Coding · JSON"), "coder")
        self.catalog_category.addItem(self.tr_text("다국어 · 번역", "Multilingual · translation"), "multilingual")
        self.catalog_size = QComboBox()
        self.catalog_size.addItem(self.tr_text("모든 크기", "All sizes"), "all")
        self.catalog_size.addItem(self.tr_text("가벼움", "Lightweight"), "small")
        self.catalog_size.addItem(self.tr_text("중간", "Medium"), "medium")
        self.catalog_size.addItem(self.tr_text("고사양", "High-end"), "large")
        catalog_filters.addWidget(self.catalog_search, 2)
        catalog_filters.addWidget(self.catalog_category, 1)
        catalog_filters.addWidget(self.catalog_size, 1)
        download_layout.addLayout(catalog_filters)
        self.recommended = QListWidget()
        self.recommended.setMinimumHeight(170)
        self.recommended.setAccessibleName(self.tr_text("추천 다운로드 모델 목록", "Recommended model catalog"))
        for guide in MODEL_GUIDES:
            if self.service.provider == "lmstudio" and not guide.get("lmstudio"):
                continue
            title = guide["ko" if translator.is_korean else "en"][0]
            kind = self.tr_text("코딩", "Coding") if guide["focus"] == "coder" else self.tr_text("다국어", "Multilingual")
            item = QListWidgetItem(f"{title} · {guide['size']} · {kind}")
            item.setData(Qt.ItemDataRole.UserRole, guide)
            self.recommended.addItem(item)
        download_layout.addWidget(self.recommended)
        self.catalog_count = QLabel()
        download_layout.addWidget(self.catalog_count)
        self.manual_toggle = QPushButton(self.tr_text("다른 모델 직접 입력 (고급)", "Enter another model (advanced)"))
        self.manual_toggle.setCheckable(True)
        download_layout.addWidget(self.manual_toggle)
        self.manual_label = QLabel(self.tr_text("모델 ID 또는 Hugging Face 주소", "Model ID or Hugging Face URL"))
        self.manual_label.setVisible(False)
        download_layout.addWidget(self.manual_label)
        self.download_name = QLineEdit()
        self.download_name.setPlaceholderText("qwen2.5-coder:7b" if self.service.provider == "ollama" else "https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF")
        self.download_name.setVisible(False)
        download_layout.addWidget(self.download_name)
        self.download_button = QPushButton(self.tr_text("선택한 모델 다운로드·설치", "Download and install selected model"))
        download_layout.addWidget(self.download_button)
        self.tabs.addTab(installed_page, self.tr_text("설치된 모델", "Installed"))
        self.tabs.addTab(download_page, self.tr_text("모델 찾기·설치", "Find and install"))
        self.tabs.addTab(advanced_page, self.tr_text("고급 관리", "Advanced management"))
        root.addWidget(self.tabs, 2)
        self.description = QTextBrowser()
        self.description.setMinimumHeight(100)
        self.description.setMaximumHeight(180)
        root.addWidget(self.description, 1)
        self.source_link = QLabel()
        self.source_link.setOpenExternalLinks(True)
        root.addWidget(self.source_link)
        self.progress = QProgressBar()
        self.progress.hide()
        outer.addWidget(self.progress)
        self.status = QLabel(self.tr_text("설치된 모델을 조회합니다. 모델이 없으면 다운로드 탭에서 준비하세요.",
            "Checking installed models. Use Download if none are installed."))
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.status)
        footer = QHBoxLayout()
        self.cancel_button = QPushButton(self.tr_text("작업 취소", "Cancel operation"))
        self.cancel_button.hide()
        self.use_button = QPushButton(self.tr_text("선택한 모델 사용", "Use selected model"))
        self.use_button.setProperty("primary", True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        footer.addWidget(self.cancel_button)
        footer.addStretch()
        footer.addWidget(self.use_button)
        footer.addWidget(buttons)
        outer.addLayout(footer)
        self.setup_button.clicked.connect(lambda: self._start("setup", lambda cancel, progress: self.service.ensure_running(install=True, cancel=cancel, progress=progress)))
        self.refresh_button.clicked.connect(self._refresh)
        self.table.itemSelectionChanged.connect(self._selection)
        self.table.itemDoubleClicked.connect(lambda *_: self._use())
        self.search.textChanged.connect(self._filter)
        self.recommended.currentRowChanged.connect(self._recommendation)
        self.catalog_search.textChanged.connect(self._filter_catalog)
        self.catalog_category.currentIndexChanged.connect(self._filter_catalog)
        self.catalog_size.currentIndexChanged.connect(self._filter_catalog)
        self.manual_toggle.toggled.connect(self._manual_mode)
        self.tabs.currentChanged.connect(self._selection)
        self.download_button.clicked.connect(self._download)
        self.use_button.clicked.connect(self._use)
        self.cancel_button.clicked.connect(self._cancel)
        self.delete_button.clicked.connect(self._delete)
        self.alias_button.clicked.connect(self._alias)
        self.details_button.clicked.connect(self._details)
        self.load_button.clicked.connect(lambda: self._memory(True))
        self.unload_button.clicked.connect(lambda: self._memory(False))
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._filter_catalog()
        self.recommended.setCurrentRow(0)
        self._selection()
        QTimer.singleShot(0, self._refresh)

    def tr_text(self, ko, en):
        return ko if self.translator.is_korean else en

    def _selected(self):
        row = self.table.currentRow()
        return self.models[row] if 0 <= row < len(self.models) and not self.table.isRowHidden(row) else None

    def _selection(self, *_args):
        row = self._selected()
        self.use_button.setVisible(self.tabs.currentIndex() == 0)
        for button in (self.use_button, self.load_button, self.unload_button, self.details_button, self.alias_button, self.delete_button):
            button.setEnabled(self.worker is None and row is not None)
        if row and self.tabs.currentIndex() == 0:
            strength, limitation = model_guide(row["id"], self.translator.is_korean)
            self.description.setPlainText(self.tr_text(
                f"{row['id']}\n장점: {strength}\n주의: {limitation}\n파일 용량과 실제 실행에 필요한 메모리는 다릅니다.",
                f"{row['id']}\nStrengths: {strength}\nLimitations: {limitation}\nFile size is not required RAM."))
            self.source_link.clear()
        elif self.tabs.currentIndex() == 1:
            self._recommendation()

    def _filter_catalog(self, *_args):
        term = self.catalog_search.text().strip().casefold()
        category = self.catalog_category.currentData()
        size = self.catalog_size.currentData()
        visible = 0
        for i in range(self.recommended.count()):
            item = self.recommended.item(i)
            guide = item.data(Qt.ItemDataRole.UserRole)
            matches = (not term or term in (item.text() + " " + guide["id"] + " "
                + " ".join(guide["ko" if self.translator.is_korean else "en"])).casefold())
            shown = matches and (category == "all" or guide["focus"] == category) and (
                size == "all" or guide["tier"] == size)
            item.setHidden(not shown)
            visible += int(shown)
        self.catalog_count.setText(self.tr_text(
            f"표시된 모델 {visible}개 · 파일 크기는 참고용이며 실제 실행 메모리는 더 필요합니다.",
            f"{visible} models shown · download sizes are estimates, not runtime RAM."))
        if self.recommended.currentItem() is None or self.recommended.currentItem().isHidden():
            first = next((i for i in range(self.recommended.count()) if not self.recommended.item(i).isHidden()), -1)
            self.recommended.setCurrentRow(first)

    def _recommendation(self, *_args):
        item = self.recommended.currentItem()
        if not item or item.isHidden():
            self.download_button.setEnabled(False)
            return
        self.download_button.setEnabled(True)
        guide = item.data(Qt.ItemDataRole.UserRole)
        title, strength, limitation = guide["ko" if self.translator.is_korean else "en"]
        if self.service.provider == "ollama":
            name = guide["id"]
        else:
            name = guide["lmstudio"]
        self.download_name.setText(name)
        self.description.setPlainText(self.tr_text(f"{title}\n장점: {strength}\n주의: {limitation}\n{guide['size']} · 용량은 버전과 양자화에 따라 바뀔 수 있습니다.",
            f"{title}\nStrengths: {strength}\nLimitations: {limitation}\n{guide['size']} · Size varies with version and quantization."))
        self.source_link.setText(f'<a href="{guide["url"]}">' + self.tr_text("모델 공식 정보·이용 조건", "Official model information / terms") + "</a>")

    def _filter(self, text):
        for index, row in enumerate(self.models):
            self.table.setRowHidden(index, text.casefold() not in row["id"].casefold())
        self._selection()

    def _refresh(self):
        self._start("refresh", lambda cancel, _progress: self.service.discover(cancel))

    def _manual_mode(self, manual):
        self.manual_label.setVisible(manual)
        self.download_name.setVisible(manual)
        self.download_button.setText(self.tr_text("입력한 모델 다운로드·설치", "Download entered model")
            if manual else self.tr_text("선택한 모델 다운로드·설치", "Download and install selected model"))
        if not manual:
            self._recommendation()

    def _download(self):
        name = self.download_name.text().strip()
        if not name:
            self.status.setText(self.tr_text("설치할 모델을 선택하거나 이름을 입력하세요.",
                "Choose a model or enter its name before installing."))
            return
        if not self.manual_toggle.isChecked():
            item = self.recommended.currentItem()
            guide = item.data(Qt.ItemDataRole.UserRole) if item and not item.isHidden() else None
            if guide and guide["tier"] == "large":
                if QMessageBox.question(self, self.windowTitle(), self.tr_text(
                        f"{guide['id']} 모델은 다운로드만 {guide['size']} 정도입니다. 실제 실행에는 추가 메모리가 필요합니다. 계속할까요?",
                        f"{guide['id']} requires approximately {guide['size']} to download; additional RAM is needed to run it. Continue?"),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
                    return
        self._start("download", lambda cancel, progress: self.service.download(name, cancel, progress))

    def _delete(self):
        row = self._selected()
        if row and QMessageBox.question(self, self.windowTitle(), self.tr_text(
                f"{row['id']} 모델을 삭제할까요? 같은 Ollama를 사용하는 다른 앱에도 반영됩니다.",
                f"Delete {row['id']}? This also affects other apps using the same Ollama server."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes:
            self._start("delete", lambda cancel, _progress: self.service.delete(row["id"], cancel))

    def _alias(self):
        row = self._selected()
        if row:
            name, ok = QInputDialog.getText(self, self.tr_text("모델 별칭", "Model alias"), self.tr_text("새 모델 이름", "New model name"))
            if ok and name.strip():
                self._start("copy", lambda cancel, _progress: self.service.copy(row["id"], name.strip(), cancel))

    def _memory(self, load):
        row = self._selected()
        if row:
            self._start("memory", lambda cancel, _progress: self.service.memory(row["id"], load, cancel))

    def _details(self):
        row = self._selected()
        if row:
            self._start("details", lambda cancel, _progress: self.service.details(row["id"], cancel))

    def _use(self):
        row = self._selected()
        if self.worker is None and row:
            self.selected_model = row["id"]
            self.accept()

    def _start(self, operation, work):
        if self.worker:
            return
        self.operation = operation
        self.worker = TranslationWorker(work, self)
        self.worker.finished.connect(self._finished)
        self.worker.progress.connect(self._progress)
        for widget in (self.tabs, self.setup_button, self.refresh_button, self.use_button):
            widget.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.status.setText(self.tr_text("처리 중… 처음 다운로드는 오래 걸릴 수 있습니다.", "Working… the first download may take a while."))
        self.worker.start()
        self.busy_changed.emit(True)

    def _progress(self, value, total):
        if total:
            self.progress.setRange(0, 100)
            self.progress.setValue(round(value / total * 100))

    def _finished(self):
        worker, self.worker = self.worker, None
        for widget in (self.tabs, self.setup_button, self.refresh_button):
            widget.setEnabled(True)
        self.progress.hide()
        self.cancel_button.hide()
        if worker.error:
            self.status.setText(worker.error)
        elif worker.cancel.is_set():
            self.status.setText(self.tr_text("취소했습니다.", "Cancelled.") + (self.tr_text(
                " LM Studio 다운로드는 서버에서 계속될 수 있습니다. LM Studio에서 확인하세요.",
                " LM Studio may continue downloading on the server; check LM Studio.") if self.service.provider == "lmstudio" and self.operation == "download" else ""))
        elif self.operation == "details":
            self._selection()
            self.metadata.setPlainText(json.dumps(worker.value, ensure_ascii=False, indent=2))
            self.tabs.setCurrentIndex(2)
            self.status.setText(self.tr_text("서버가 제공한 모델 정보입니다.", "Model information returned by the local server."))
        else:
            self._discovered = True
            previous = self._selected()
            self.models = worker.value or []
            self.table.setRowCount(len(self.models))
            for index, row in enumerate(self.models):
                size = f"{row['size'] / 1024**3:.2f} GiB" if isinstance(row["size"], (int, float)) and row["size"] else "—"
                for column, text in enumerate((row["id"], size, row["parameters"] or "—", row["quantization"] or "—")):
                    self.table.setItem(index, column, QTableWidgetItem(str(text)))
            self._filter(self.search.text())
            if self.models:
                requested = self.download_name.text().strip() if self.operation == "download" else (previous or {}).get("id", "")
                selected = next((i for i, row in enumerate(self.models) if row["id"] == requested), 0)
                self.table.selectRow(selected)
                if self.operation == "download":
                    self.tabs.setCurrentIndex(0)
            elif self.operation in {"setup", "refresh"}:
                self.tabs.setCurrentIndex(1)
            self.status.setText(self.tr_text(f"설치된 모델 {len(self.models)}개 · 사용할 모델을 선택한 뒤 '선택한 모델 사용'을 누르세요.",
                f"{len(self.models)} installed models · select one and click 'Use selected model'.") if self.models else self.tr_text(
                "모델이 아직 없습니다. '모델 찾기·설치'에서 모델을 선택하고 다운로드하세요.",
                "No model installed yet. Choose and download one under 'Find and install'."))
        if self.operation != "details":
            self._selection()
        worker.deleteLater()
        self.busy_changed.emit(False)
        if self._closing:
            super().reject()

    def _cancel(self):
        if self.worker:
            self.worker.cancel.set()
            self.cancel_button.setEnabled(False)
            self.status.setText(self.tr_text("취소 중… 현재 연결이 끝날 때까지 기다려 주세요.", "Cancelling… waiting for the current connection."))

    def done(self, result):
        if self.worker:
            self._closing = True
            self._cancel()
            return
        super().done(result)
