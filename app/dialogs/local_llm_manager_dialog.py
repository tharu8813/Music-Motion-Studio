"""Manage local LLMs without requiring users to work in a terminal."""
import json
from time import monotonic

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget, QScrollArea,
    QGridLayout, QComboBox)

from app.dialogs.help_dialog import install_help_shortcut
from app.services.local_llm_service import LocalLLMService, MODEL_GUIDES, model_guide
from app.services.translation_worker import TranslationWorker


class _InstalledModelTable(QTableWidget):
    """Distinguish deliberate model selection from Qt restoring focus."""

    user_selected = Signal()

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and index.isValid():
            self.user_selected.emit()

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        if event.key() in {
            Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right,
            Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
            Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter,
        }:
            self.user_selected.emit()


class LocalLLMManagerDialog(QDialog):
    busy_changed = Signal(bool)

    def __init__(self, translator, parent=None, *, service=None, preferred_model=""):
        super().__init__(parent)
        self.translator, self.service = translator, service or LocalLLMService()
        self.worker, self._closing, self.selected_model = None, False, ""
        self.models = []
        self.preferred_model = preferred_model
        self._download_target = ""
        self._before_download = set()
        self._before_download_verified = False
        self._deleted_name = ""
        self._download_sample = None
        self._download_speed = None
        self._download_phase = ""
        self._updating_models = False
        # Qt can restore a current table row when switching tabs. Following
        # an unverified download, only a fresh user selection may enable Use.
        self._require_explicit_selection = False
        self._discovered = False
        # Only a successful model-list response can verify this connection.
        # A prior discovery is not evidence after a failed/cancelled refresh.
        self.models_verified_at = None
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
            "설치된 모델을 선택하거나 새 모델을 설치하세요.",
            "Choose an installed model or install a new one."))
        note.setWordWrap(True)
        root.addWidget(note)
        self.engine_state = QLabel(self.tr_text("로컬 AI 연결을 확인하는 중…", "Checking the local AI connection…"))
        self.engine_state.setObjectName("panelTitle")
        root.addWidget(self.engine_state)
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
        self.table = _InstalledModelTable(0, 4)
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
        self.installed_hint = QLabel(self.tr_text(
            "설치된 모델을 선택하면 바로 사용할 수 있습니다. 모델이 없다면 추가하세요.",
            "Choose an installed model to use it, or add your first model."))
        self.installed_hint.setWordWrap(True)
        installed_layout.addWidget(self.installed_hint)
        installed_actions = QHBoxLayout()
        self.use_button = QPushButton(self.tr_text("이 모델 사용", "Use this model"))
        self.use_button.setProperty("primary", True)
        self.add_model_button = QPushButton(self.tr_text("＋ 모델 추가", "＋ Add model"))
        installed_actions.addWidget(self.use_button)
        installed_actions.addWidget(self.add_model_button)
        installed_layout.addLayout(installed_actions)
        advanced_actions = QGridLayout()
        self.load_button = QPushButton(self.tr_text("메모리에 로드", "Load into memory"))
        self.unload_button = QPushButton(self.tr_text("메모리 해제", "Unload from memory"))
        self.details_button = QPushButton(self.tr_text("상세 정보·라이선스", "Details / license"))
        self.alias_button = QPushButton(self.tr_text("별칭 만들기", "Create alias"))
        self.delete_button = QPushButton(self.tr_text("모델 삭제…", "Delete model…"))
        for index, button in enumerate((self.load_button, self.unload_button)):
            advanced_actions.addWidget(button, index // 2, index % 2)
        advanced_layout.addLayout(advanced_actions)
        advanced_layout.addWidget(self.alias_button)
        self.metadata = QTextBrowser()
        self.metadata.setMinimumHeight(180)
        advanced_layout.addWidget(self.metadata, 1)
        self.alias_button.setVisible(self.service.provider == "ollama")
        self.delete_button.setVisible(self.service.provider == "ollama")
        installed_management = QHBoxLayout()
        installed_management.addWidget(self.details_button)
        installed_management.addWidget(self.delete_button)
        installed_management.addStretch()
        installed_layout.addLayout(installed_management)
        download_layout.addWidget(QLabel(self.tr_text(
            "가사 번역에 적합한 다국어 모델을 우선 보여줍니다. 아래에서 선택하고 바로 설치할 수 있습니다. 실제 품질과 성능은 가사·PC 환경에 따라 다릅니다.",
            "Multilingual models suited to lyrics are shown first. Select one to download directly; actual quality and speed depend on your lyrics and hardware.")))
        catalog_filters = QHBoxLayout()
        self.catalog_search = QLineEdit()
        self.catalog_search.setPlaceholderText(self.tr_text("모델 이름·특징 검색", "Search model names or features"))
        self.catalog_category = QComboBox()
        self.catalog_category.addItem(self.tr_text("가사 번역 추천", "Recommended for lyrics"), "lyrics")
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
        # Translation-first ordering is a suitability shortlist, not an
        # unverified quality benchmark. All existing coding models remain in
        # the catalog under their own filter.
        guides = sorted(MODEL_GUIDES, key=lambda g: (g.get("lyric_rank", 999), MODEL_GUIDES.index(g)))
        for guide in guides:
            if self.service.provider == "lmstudio" and not guide.get("lmstudio"):
                continue
            title = guide["ko" if translator.is_korean else "en"][0]
            kind = (self.tr_text("가사 번역", "Lyrics") if guide.get("lyric_rank") else
                    self.tr_text("코딩", "Coding") if guide["focus"] == "coder" else
                    self.tr_text("다국어", "Multilingual"))
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
        self.download_detail = QLabel("")
        self.download_detail.setWordWrap(True)
        self.download_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.download_detail.hide()
        outer.addWidget(self.download_detail)
        self.status = QLabel(self.tr_text("설치된 모델을 조회합니다. 모델이 없으면 다운로드 탭에서 준비하세요.",
            "Checking installed models. Use Download if none are installed."))
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.status)
        footer = QHBoxLayout()
        self.cancel_button = QPushButton(self.tr_text("작업 취소", "Cancel operation"))
        self.cancel_button.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        footer.addWidget(self.cancel_button)
        footer.addStretch()
        footer.addWidget(buttons)
        outer.addLayout(footer)
        self.setup_button.clicked.connect(lambda: self._start("setup", lambda cancel, progress: self.service.ensure_running(install=True, cancel=cancel, progress=progress)))
        self.refresh_button.clicked.connect(self._refresh)
        self.add_model_button.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        self.table.itemSelectionChanged.connect(self._selection)
        self.table.user_selected.connect(self._installed_selection_changed)
        self.table.itemDoubleClicked.connect(lambda *_: self._use())
        self.search.textChanged.connect(self._filter)
        self.recommended.currentRowChanged.connect(self._recommendation)
        self.catalog_search.textChanged.connect(self._filter_catalog)
        self.catalog_category.currentIndexChanged.connect(self._filter_catalog)
        self.catalog_size.currentIndexChanged.connect(self._filter_catalog)
        self.manual_toggle.toggled.connect(self._manual_mode)
        self.download_name.textChanged.connect(lambda *_: self._recommendation() if self.manual_toggle.isChecked() else None)
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
        # Start the worker before returning, not a zero-delay timer. Otherwise
        # an immediate download can mistake an unqueried list for an empty one.
        self._refresh()

    def tr_text(self, ko, en):
        return ko if self.translator.is_korean else en

    @staticmethod
    def _model_key(name):
        return name.casefold().removesuffix(":latest") if isinstance(name, str) else ""

    def _installed_index(self, name):
        key = self._model_key(name)
        return next((i for i, row in enumerate(self.models)
                     if self._model_key(row["id"]) == key), -1) if key else -1

    def _sync_catalog(self):
        for i in range(self.recommended.count()):
            item = self.recommended.item(i)
            guide = item.data(Qt.ItemDataRole.UserRole)
            title = guide["ko" if self.translator.is_korean else "en"][0]
            kind = (self.tr_text("가사 번역", "Lyrics") if guide.get("lyric_rank") else
                    self.tr_text("코딩", "Coding") if guide["focus"] == "coder" else
                    self.tr_text("다국어", "Multilingual"))
            installed = self._installed_index(guide["id"]) >= 0
            suffix = " · " + self.tr_text("설치됨", "Installed") if installed else ""
            item.setText(f"{title} · {guide['size']} · {kind}{suffix}")
        self._recommendation()

    def _selected(self):
        if self._require_explicit_selection:
            return None
        row = self.table.currentRow()
        return (self.models[row] if self.table.selectedItems() and 0 <= row < len(self.models)
                and not self.table.isRowHidden(row) else None)

    def _installed_selection_changed(self):
        if (self._require_explicit_selection and not self._updating_models
                and self.worker is None and self.operation != "download"
                and self.table.selectedItems()
                and 0 <= self.table.currentRow() < len(self.models)
                and not self.table.isRowHidden(self.table.currentRow())):
            # Only a deliberate mouse/keyboard selection (or the explicit
            # catalog action below) can release the guard.
            self._require_explicit_selection = False
        self._selection()

    def _selection(self, *_args):
        if self._updating_models:
            return
        row = self._selected()
        for button in (self.use_button, self.load_button, self.unload_button, self.details_button, self.alias_button, self.delete_button):
            button.setEnabled(self.worker is None and row is not None)
        self.installed_hint.setText(self.tr_text(
            f"선택한 모델: {row['id']} · '이 모델 사용'을 누르면 번역에 적용됩니다." if row
            else "설치된 모델을 선택하세요. 모델이 없다면 '모델 추가'로 시작할 수 있습니다.",
            f"Selected: {row['id']} · click 'Use this model' to apply it to translations." if row
            else "Choose an installed model. Use 'Add model' if none are installed."))
        if row and self.tabs.currentIndex() == 0:
            strength, limitation = model_guide(row["id"], self.translator.is_korean)
            self.description.setPlainText(self.tr_text(
                f"{row['id']}\n장점: {strength}\n주의: {limitation}\n파일 용량과 실제 실행에 필요한 메모리는 다릅니다.",
                f"{row['id']}\nStrengths: {strength}\nLimitations: {limitation}\nFile size is not required RAM."))
            self.source_link.clear()
        elif self.tabs.currentIndex() == 1:
            self._recommendation()
        else:
            # An installed-model search can hide the previously selected row.
            # Clear its old details rather than showing a phantom selection.
            self.description.clear()
            self.source_link.clear()

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
            shown = matches and (category == "all" or category == "lyrics" and bool(guide.get("lyric_rank"))
                or guide["focus"] == category) and (
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
        if self.manual_toggle.isChecked():
            # Manual model IDs are independent of the catalog filters and
            # should never display an unrelated recommendation's tradeoffs.
            self.download_button.setEnabled(
                self.worker is None and bool(self.download_name.text().strip()))
            self.download_button.setText(self.tr_text(
                "입력한 모델 다운로드·설치", "Download entered model"))
            self.description.setPlainText(self.tr_text(
                "직접 입력한 모델입니다. 다운로드 전에 모델 이름과 이용 조건을 확인하세요.",
                "Custom model: verify its name and usage terms before downloading."))
            self.source_link.clear()
            return
        item = self.recommended.currentItem()
        if not item or item.isHidden():
            # A filtered-out recommendation must not leave a stale model name
            # or details behind that look like an actionable selection.
            self.download_name.clear()
            self.download_button.setEnabled(False)
            self.download_button.setText(self.tr_text(
                "모델을 선택하세요", "Choose a model"))
            self.description.setPlainText(self.tr_text(
                "검색 결과가 없습니다. 검색어나 필터를 변경하세요.",
                "No matching models. Change your search or filters."))
            self.source_link.clear()
            return
        guide = item.data(Qt.ItemDataRole.UserRole)
        installed = self._installed_index(guide["id"]) >= 0
        self.download_button.setEnabled(self.worker is None)
        self.download_button.setText(self.tr_text(
            "설치된 모델 바로 사용" if installed else "선택한 모델 다운로드·설치",
            "Use installed model" if installed else "Download and install selected model"))
        title, strength, limitation = guide["ko" if self.translator.is_korean else "en"]
        name = guide["id"] if self.service.provider == "ollama" else guide["lmstudio"]
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
        self._recommendation()

    def _download(self):
        if self.worker:
            return
        if not self.manual_toggle.isChecked():
            selected = self.recommended.currentItem()
            if selected is None or selected.isHidden():
                self.status.setText(self.tr_text(
                    "추천 목록에서 모델을 선택하거나 검색 조건을 변경하세요.",
                    "Choose a visible recommendation or change your search."))
                return
        name = self.download_name.text().strip()
        if not name:
            self.status.setText(self.tr_text("설치할 모델을 선택하거나 이름을 입력하세요.",
                "Choose a model or enter its name before installing."))
            return
        installed_index = self._installed_index(
            name if self.manual_toggle.isChecked() else
            (self.recommended.currentItem().data(Qt.ItemDataRole.UserRole)["id"]
             if self.recommended.currentItem() and not self.recommended.currentItem().isHidden()
             else name))
        if installed_index >= 0:
            self.search.clear()
            self.tabs.setCurrentIndex(0)
            self.table.selectRow(installed_index)
            self._installed_selection_changed()
            self._use()
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
        self._download_target = name
        self._before_download = {self._model_key(row["id"]) for row in self.models}
        self._before_download_verified = self.models_verified_at is not None
        self._start("download", lambda cancel, progress: self.service.download(
            name, cancel, progress, status=lambda event: self._emit_download_status(event)))

    def _delete(self):
        row = self._selected()
        if row and QMessageBox.question(self, self.windowTitle(), self.tr_text(
                f"{row['id']} 모델을 삭제할까요? 같은 Ollama를 사용하는 다른 앱에도 반영됩니다.",
                f"Delete {row['id']}? This also affects other apps using the same Ollama server."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes:
            self._deleted_name = row["id"]
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

    def _emit_download_status(self, event):
        # Called from the worker thread. Only emit a Qt signal here.
        worker = self.worker
        if worker is not None:
            worker.activity.emit(event)

    @staticmethod
    def _bytes(size):
        if size >= 1024 ** 3:
            return f"{size / 1024 ** 3:.1f} GiB"
        return f"{size / 1024 ** 2:.1f} MiB"

    def _download_activity(self, event):
        worker = self.worker
        sender = self.sender()
        if (worker is None or worker.cancel.is_set() or self._closing
                or (sender is not None and sender is not worker)
                or not isinstance(event, dict) or self.operation != "download"):
            return
        label = str(event.get("status", "")).strip().casefold()
        done, total = event.get("completed", 0), event.get("total", 0)
        if not isinstance(done, (int, float)) or not isinstance(total, (int, float)):
            return

        # Ollama and LM Studio expose different status strings. Map only
        # recognized server events to user-facing stages; unknown server text
        # must not become a misleading success message or expose server data.
        if label.startswith("pulling manifest"):
            phase = "preparing"
        elif label.startswith("pulling ") or label in {"downloading", "download"}:
            phase = "downloading"
        elif label.startswith("verifying"):
            phase = "verifying"
        elif label.startswith(("writing", "removing")):
            phase = "installing"
        elif label in {"success", "completed", "already_downloaded"}:
            phase = "checking"
        else:
            phase = "working"

        if phase != self._download_phase:
            self._download_phase = phase
            self._download_sample = None
            self._download_speed = None
            if phase != "downloading":
                # A 100% transfer is not evidence that verification or model
                # publication has finished. Return to indeterminate progress.
                self.progress.setRange(0, 0)
        stages = {
            "preparing": self.tr_text("모델 정보를 확인하는 중…", "Checking model information…"),
            "downloading": self.tr_text("모델 파일 다운로드 중…", "Downloading model files…"),
            "verifying": self.tr_text("다운로드 파일 검증 중…", "Verifying downloaded files…"),
            "installing": self.tr_text("모델 설치 마무리 중…", "Finishing model installation…"),
            "checking": self.tr_text("서버 완료 응답 · 설치된 모델 목록 확인 중…",
                                     "Server reported completion · checking installed models…"),
            "working": self.tr_text("모델 설치 작업 진행 중…", "Model installation in progress…"),
        }
        self.status.setText(stages[phase])
        if phase != "downloading" or total <= 0:
            self.download_detail.clear()
            return

        done, total = max(0, done), max(0, total)
        now = monotonic()
        sample = self._download_sample
        if sample and done > sample[0] and now > sample[1] + 0.25:
            current_speed = (done - sample[0]) / (now - sample[1])
            self._download_speed = (current_speed if self._download_speed is None
                                    else self._download_speed * 0.7 + current_speed * 0.3)
            self._download_sample = (done, now)
        elif sample is None or done < sample[0]:
            self._download_sample = (done, now)
            self._download_speed = None
        detail = f"{self._bytes(done)} / {self._bytes(total)}"
        if self._download_speed and self._download_speed > 0:
            remaining = max(0, int((total - done) / self._download_speed))
            detail += self.tr_text(
                f" · {self._bytes(self._download_speed)}/초 · 약 {remaining // 60}분 {remaining % 60}초 남음",
                f" · {self._bytes(self._download_speed)}/s · ~{remaining // 60}m {remaining % 60}s left")
        self.download_detail.setText(detail)

    def _start(self, operation, work):
        if self.worker or self._closing:
            return
        if operation != "details":
            # Refreshes and mutations can invalidate the earlier model listing.
            self.models_verified_at = None
            self.engine_state.setText(self.tr_text(
                "로컬 AI 연결 및 모델 목록 확인 중…",
                "Checking local AI connection and installed models…"))
        self.operation = operation
        self._download_sample = None
        self._download_speed = None
        self._download_phase = ""
        self.worker = TranslationWorker(work, self)
        self.worker.finished.connect(self._finished)
        self.worker.progress.connect(self._progress)
        self.worker.activity.connect(self._download_activity)
        for widget in (self.tabs, self.setup_button, self.refresh_button, self.use_button):
            widget.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.download_detail.setText("")
        self.download_detail.setVisible(operation == "download")
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.status.setText(self.tr_text(
            "모델 정보를 확인하는 중…" if operation == "download"
            else "연결 및 모델 목록을 확인 중…",
            "Checking model information…" if operation == "download"
            else "Checking connection and installed models…"))
        self.worker.start()
        self.busy_changed.emit(True)

    def _progress(self, value, total):
        worker = self.worker
        sender = self.sender()
        if (worker is None or worker.cancel.is_set() or self._closing
                or (sender is not None and sender is not worker)):
            return
        if self.operation == "download" and self._download_phase not in {"", "downloading"}:
            # Late byte-transfer progress must not show 100% while model
            # integrity verification or manifest installation is still active.
            return
        if total:
            self.progress.setRange(0, 100)
            self.progress.setValue(round(value / total * 100))

    def _finished(self):
        if self.worker is None or (self.sender() is not None and self.sender() is not self.worker):
            return
        worker, self.worker = self.worker, None
        for widget in (self.tabs, self.setup_button, self.refresh_button):
            widget.setEnabled(True)
        self.progress.hide()
        self.cancel_button.hide()
        if worker.error:
            if self.operation in {"refresh", "setup"}:
                self.engine_state.setText(self.tr_text(
                    "로컬 AI 연결 실패 · 실행 환경을 준비하거나 연결 주소를 확인하세요.",
                    "Local AI unavailable · prepare the engine or check the server address."))
            elif self.operation != "details":
                self.engine_state.setText(self.tr_text(
                    "로컬 AI 연결 상태 미확인 · 모델 목록을 다시 확인하세요.",
                    "Local AI connection unverified · refresh the model list."))
            self.status.setText((
                self.tr_text("모델 다운로드·설치 실패 · ", "Model download/install failed · ")
                if self.operation == "download" else "") + worker.error + (self.tr_text(
                " · 같은 모델을 다시 선택해 재시도할 수 있습니다.",
                " · Select the same model to retry.") if self.operation == "download" else ""))
        elif worker.cancel.is_set():
            if self.operation != "details":
                self.engine_state.setText(self.tr_text(
                    "로컬 AI 연결 상태 미확인 · 모델 목록을 다시 확인하세요.",
                    "Local AI connection unverified · refresh the model list."))
            self.status.setText(self.tr_text("취소했습니다.", "Cancelled.") + (self.tr_text(
                " LM Studio 다운로드는 서버에서 계속될 수 있습니다. LM Studio에서 확인하세요.",
                " LM Studio may continue downloading on the server; check LM Studio.") if self.service.provider == "lmstudio" and self.operation == "download" else ""))
        elif self.operation == "details":
            self._selection()
            self.metadata.setPlainText(json.dumps(worker.value, ensure_ascii=False, indent=2))
            self.tabs.setCurrentIndex(2)
            self.status.setText(self.tr_text("서버가 제공한 모델 정보입니다.", "Model information returned by the local server."))
        else:
            # All successful non-details operations return a freshly queried
            # model index. Do not refresh the proof timestamp on dialog close.
            self.models_verified_at = monotonic()
            first_discovery = not self._discovered
            self._discovered = True
            previous = self._selected()
            self._updating_models = True
            self.models = worker.value or []
            self.table.blockSignals(True)
            try:
                self.table.setRowCount(len(self.models))
                for index, row in enumerate(self.models):
                    size = f"{row['size'] / 1024**3:.2f} GiB" if isinstance(row["size"], (int, float)) and row["size"] else "—"
                    for column, value in enumerate((row["id"], size, row["parameters"] or "—", row["quantization"] or "—")):
                        self.table.setItem(index, column, QTableWidgetItem(str(value)))
                for index, row in enumerate(self.models):
                    self.table.setRowHidden(index, self.search.text().casefold() not in row["id"].casefold())
                requested = (self._download_target if self.operation == "download" else
                             self.preferred_model if first_discovery and self.operation in {"refresh", "setup"} and self.preferred_model
                             else (previous or {}).get("id", ""))
                selected = self._installed_index(requested)
                if (self.operation == "download" and selected < 0
                        and self.service.provider == "lmstudio" and self._before_download_verified):
                    # LM Studio may publish a downloaded Hugging Face model
                    # under a different local file ID. Offer a unique new row
                    # for manual inspection, but never call it verified by name.
                    new_rows = [i for i, row in enumerate(self.models)
                                if self._model_key(row["id"]) not in self._before_download]
                    if len(new_rows) == 1:
                        selected = new_rows[0]
                if self.operation == "delete" and self._model_key(requested) == self._model_key(self._deleted_name):
                    selected = -1
                if (selected < 0 and not requested and self.models
                        and not self._require_explicit_selection
                        and self.operation not in {"delete", "download"}):
                    selected = 0
                self.table.clearSelection()
                if selected >= 0 and not self.table.isRowHidden(selected):
                    self.table.selectRow(selected)
            finally:
                self.table.blockSignals(False)
                self._updating_models = False
            self._sync_catalog()
            self.engine_state.setText(self.tr_text(
                f"로컬 AI 연결됨 · 설치된 모델 {len(self.models)}개",
                f"Local AI connected · {len(self.models)} installed models"))
            if self.operation == "download":
                self.tabs.setCurrentIndex(0)
                # Switching to Installed can restore a previously active row
                # without a deliberate user choice. Even if Qt visually
                # restores it, don't consider it a verified downloaded model.
                self._require_explicit_selection = selected < 0 or self.table.isRowHidden(selected)
                # Restore the intended selection after the tab switch, which
                # may otherwise give the table a different current index.
                self.table.clearSelection()
                self.table.setCurrentCell(-1, -1)
                if not self._require_explicit_selection:
                    self.table.selectRow(selected)
                selected_model = self._selected()
                # A different selected or newly discovered model does not
                # prove that the user's requested model was installed.
                target_confirmed = (selected_model is not None
                                    and self._model_key(selected_model["id"])
                                    == self._model_key(self._download_target))
                possible_new_model = (self._before_download_verified and selected_model is not None
                                      and self._model_key(selected_model["id"]) not in self._before_download)
                if target_confirmed:
                    self.status.setText(self.tr_text(
                        "모델 설치 확인 완료 · '이 모델 사용'을 눌러 적용하세요.",
                        "Model installation verified · click 'Use this model' to apply it."))
                elif self.service.provider == "lmstudio" and possible_new_model:
                    self.status.setText(self.tr_text(
                        "새 로컬 모델을 발견했습니다. 요청한 모델인지 확인한 뒤 사용하세요.",
                        "A new local model was found. Confirm it matches your request before using it."))
                else:
                    self.status.setText(self.tr_text(
                        "서버에서 완료 응답을 받았지만 요청한 설치 모델을 목록에서 확인하지 못했습니다. 목록을 새로고침하세요.",
                        "Server reported completion, but the requested model was not found in the installed list. Refresh models."))
            elif self.operation == "delete":
                self.status.setText(self.tr_text(
                    f"{self._deleted_name} 삭제 완료 · 다른 모델을 사용하려면 직접 선택하세요.",
                    f"{self._deleted_name} deleted · choose another model explicitly."))
                self._deleted_name = ""
            elif not self.models:
                self.tabs.setCurrentIndex(1)
                self.status.setText(self.tr_text(
                    "설치된 모델이 없습니다. 추천 목록에서 모델을 받아보세요.",
                    "No models installed. Choose one from the catalog to get started."))
            elif requested and not self._selected():
                self.status.setText(self.tr_text(
                    "전에 선택한 모델이 설치되어 있지 않습니다. 다른 모델을 직접 선택하세요.",
                    "The previously selected model is missing. Choose another model explicitly."))
            else:
                self.status.setText(self.tr_text(
                    f"설치된 모델 {len(self.models)}개 · 모델을 선택한 뒤 '이 모델 사용'을 누르세요.",
                    f"{len(self.models)} installed models · select one and click 'Use this model'."))
        if self.operation != "details":
            self._selection()
            if self.tabs.currentIndex() == 1:
                self._recommendation()
        self.operation = ""
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
