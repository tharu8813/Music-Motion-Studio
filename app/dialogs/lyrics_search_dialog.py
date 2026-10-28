"""Search recordings, inspect their lyrics, then download or attach a selection."""

from __future__ import annotations

from copy import deepcopy
from threading import Event
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.models.playlist import PlaylistTrack
from app.services.lyrics_search_service import (
    FoundLyrics, LyricsSearchBatch, LyricsSearchCancelled, LyricsSearchError,
    LyricsSearchQuery, LyricsSearchService, PROVIDERS,
)
from app.utils.i18n import Translator
from app.utils.qt_worker_lifecycle import stop_qthread_now
from app.utils.time_format import format_clock


def searchable_track(track: PlaylistTrack) -> bool:
    return bool(track.title.strip() and track.artist.strip()
                and track.artist.strip().casefold() not in {"unknown artist", "알 수 없는 아티스트"})


class _LookupWorker(QThread):
    waiting = Signal(float)
    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation
        self.cancel_event = Event()
        self.value = None
        self.error = ""

    def run(self):
        try:
            self.value = self.operation(self.cancel_event)
        except LyricsSearchCancelled:
            self.cancel_event.set()
        except Exception as error:  # A provider failure must leave the dialog usable.
            self.error = str(error)


class LyricsSearchDialog(QDialog):
    def __init__(self, tracks: list[PlaylistTrack], translator: Translator,
                 parent: QWidget | None = None, *, initial_track_id: str = "",
                 on_attach: Callable[[FoundLyrics, str], bool] | None = None,
                 service: LyricsSearchService | None = None, staged: bool = False):
        super().__init__(parent)
        self.tracks = deepcopy(tracks)
        self.translator = translator
        self.service = service or LyricsSearchService()
        self.on_attach = on_attach
        self.staged = staged
        self.worker: _LookupWorker | None = None
        self._close_result: int | None = None
        self.results = []
        self.document: FoundLyrics | None = None
        self._documents: dict[tuple[str, str], FoundLyrics] = {}
        self.saved_paths = []
        self.attached_track_ids = []
        install_help_shortcut(self, ("lyrics_editor", "lyrics_files"), translator)
        self.resize(980, 680)
        self.setMinimumSize(760, 540)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        self.intro = QLabel()
        self.intro.setWordWrap(True)
        root.addWidget(self.intro)
        self.form_group = QGroupBox()
        grid = QGridLayout(self.form_group)
        self.playlist_combo = QComboBox()
        self.playlist_combo.addItem("", "")
        for track in self.tracks:
            if searchable_track(track):
                self.playlist_combo.addItem(f"{track.title} — {track.artist}", track.id)
        self.playlist_label = QLabel()
        grid.addWidget(self.playlist_label, 0, 0)
        grid.addWidget(self.playlist_combo, 0, 1, 1, 5)
        self.title_edit, self.artist_edit, self.album_edit = QLineEdit(), QLineEdit(), QLineEdit()
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0, 86400)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setKeyboardTracking(False)
        self.provider_combo = QComboBox()
        for key in ("all", *PROVIDERS):
            self.provider_combo.addItem("", key)
        self.labels = {}
        fields = (("title", self.title_edit), ("artist", self.artist_edit), ("album", self.album_edit),
                  ("duration", self.duration_spin), ("provider", self.provider_combo))
        for index, (key, widget) in enumerate(fields):
            label = QLabel()
            label.setBuddy(widget)
            self.labels[key] = label
            row, column = 1 + index // 3, (index % 3) * 2
            grid.addWidget(label, row, column)
            grid.addWidget(widget, row, column + 1)
        for column in (1, 3, 5):
            grid.setColumnStretch(column, 1)
        self.synced_only = QCheckBox()
        self.synced_only.setChecked(True)
        self.search_button, self.cancel_button = QPushButton(), QPushButton()
        self.search_button.setDefault(True)
        self.search_button.setProperty("primary", True)
        self.cancel_button.hide()
        search_row = QHBoxLayout()
        search_row.addWidget(self.synced_only)
        search_row.addStretch(1)
        search_row.addWidget(self.cancel_button)
        search_row.addWidget(self.search_button)
        grid.addLayout(search_row, 3, 0, 1, 6)
        root.addWidget(self.form_group)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        self.warning.hide()
        root.addWidget(self.warning)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 6)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in ((0, 180), (1, 110), (2, 120), (3, 58), (4, 90), (5, 100)):
            self.table.setColumnWidth(column, width)
        splitter.addWidget(self.table)
        preview_box = QWidget()
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(6, 0, 0, 0)
        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        preview_layout.addWidget(self.detail_label)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        preview_layout.addWidget(self.preview, 1)
        splitter.addWidget(preview_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([670, 290])
        root.addWidget(splitter, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumHeight(5)
        self.progress.setTextVisible(False)
        self.progress.hide()
        root.addWidget(self.progress)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        target_row = QHBoxLayout()
        self.target_label = QLabel()
        self.target_combo = QComboBox()
        self.target_combo.addItem("", "")
        for track in self.tracks:
            self.target_combo.addItem(f"{track.title} — {track.artist}", track.id)
        target_row.addWidget(self.target_label)
        target_row.addWidget(self.target_combo, 1)
        self.download_button, self.attach_button, self.close_button = QPushButton(), QPushButton(), QPushButton()
        target_row.addWidget(self.download_button)
        target_row.addWidget(self.attach_button)
        target_row.addWidget(self.close_button)
        root.addLayout(target_row)
        self.note = QLabel()
        self.note.setObjectName("mutedLabel")
        self.note.setWordWrap(True)
        root.addWidget(self.note)
        self.credit = QLabel('<a href="https://github.com/AlFarrizi-Studio/LiriQo">LiriQo</a>')
        self.credit.setOpenExternalLinks(True)
        self.credit.setObjectName("mutedLabel")
        root.addWidget(self.credit)

        self.playlist_combo.currentIndexChanged.connect(self._use_playlist_track)
        self.title_edit.textChanged.connect(self._sync_actions)
        self.target_combo.currentIndexChanged.connect(self._sync_actions)
        self.search_button.clicked.connect(self._search)
        self.cancel_button.clicked.connect(self._cancel_request)
        self.table.currentCellChanged.connect(self._select_result)
        self.table.itemDoubleClicked.connect(self._select_result)
        self.download_button.clicked.connect(self._download)
        self.attach_button.clicked.connect(self._attach)
        self.close_button.clicked.connect(self.reject)
        self.translator.language_changed.connect(self.retranslate)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_worker)
        self.retranslate()
        if initial_track_id:
            index = self.playlist_combo.findData(initial_track_id)
            if index >= 0:
                self.playlist_combo.setCurrentIndex(index)
            index = self.target_combo.findData(initial_track_id)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
        self._sync_actions()

    def _tr(self, ko, en):
        return ko if self.translator.is_korean else en

    def retranslate(self):
        self.setWindowTitle(self._tr("가사 찾아보기", "Find lyrics"))
        self.intro.setText(self._tr("곡을 검색하고 가사를 미리 본 뒤, 파일로 내려받거나 플레이리스트의 곡에 연결하세요.",
                                   "Search for a recording, preview its lyrics, then download a file or attach lyrics to a playlist track."))
        self.form_group.setTitle(self._tr("곡 정보", "Song information"))
        self.playlist_label.setText(self._tr("검색에 사용할 곡", "Search using"))
        self.playlist_combo.setItemText(0, self._tr("직접 입력", "Enter manually"))
        for key, labels in {
            "title": ("제목 *", "Title *"), "artist": ("아티스트", "Artist"), "album": ("앨범", "Album"),
            "duration": ("길이 (초)", "Duration (s)"), "provider": ("제공자", "Provider"),
        }.items():
            self.labels[key].setText(self._tr(*labels))
        self.title_edit.setPlaceholderText(self._tr("필수 입력", "Required"))
        self.artist_edit.setPlaceholderText(self._tr("선택 입력", "Optional"))
        self.album_edit.setPlaceholderText(self._tr("선택 입력", "Optional"))
        for widget in (self.title_edit, self.artist_edit, self.album_edit):
            widget.setToolTip(self._tr("LiriQo가 제목과 아티스트로 곡을 찾습니다. 앨범은 결과 필터로 사용합니다.",
                                      "LiriQo resolves a recording by title and artist. Album filters the returned results."))
        self.duration_spin.setSpecialValueText(self._tr("지정 안 함", "Any"))
        self.duration_spin.setToolTip(self._tr("길이가 비슷한 검색 결과를 우선 표시합니다.", "Ranks recordings with a similar duration first."))
        for index, key in enumerate(("all", *PROVIDERS)):
            self.provider_combo.setItemText(index, self._tr("모든 제공자", "All providers") if key == "all" else PROVIDERS[key])
        self.synced_only.setText(self._tr("시간 정보가 있는 가사만", "Timed lyrics only"))
        self.synced_only.setToolTip(self._tr("LiriQo에서 시간 정보가 있는 결과만 표시합니다.", "Shows only timed lyric variants returned by LiriQo."))
        self.search_button.setText(self._tr("검색", "Search"))
        self.cancel_button.setText(self._tr("작업 취소", "Cancel request"))
        self.table.setHorizontalHeaderLabels([self._tr(*pair) for pair in (
            ("제목", "Title"), ("아티스트", "Artist"), ("앨범", "Album"),
            ("길이", "Length"), ("제공자", "Provider"), ("가사", "Lyrics"))])
        self.target_label.setText(self._tr("연결할 곡", "Attach to"))
        self.target_combo.setItemText(0, self._tr("연결할 곡을 선택하세요", "Choose a target track"))
        self.preview.setPlaceholderText(self._tr("검색 결과에서 곡을 선택하면 가사를 표시합니다.", "Select a recording from the search results to preview lyrics."))
        self.table.setToolTip(self._tr("곡을 선택해 가사를 확인하세요. 실패한 결과는 두 번 클릭해 다시 불러올 수 있습니다.", "Select a recording to inspect lyrics. Double-click a failed result to retry."))
        self.download_button.setText(self._tr("로컬 다운로드…", "Download…"))
        self.attach_button.setText(self._tr("플레이리스트에 추가", "Add to playlist"))
        self.close_button.setText(self._tr("닫기", "Close"))
        self.note.setText(self._tr("플레이리스트에 추가하면 선택한 곡에 가사를 연결합니다. 시간 정보 없는 가사는 다운로드만 가능합니다.",
                                  "Add to playlist attaches lyrics to the chosen track. Untimed lyrics can only be downloaded.") +
                          (self._tr(" 곡 설정에서 적용을 눌러야 반영됩니다.", " Click Apply in track settings to keep the attachment.") if self.staged else ""))
        if not self.status.text():
            self.status.setText(self._tr("제목을 입력하고 검색하세요.", "Enter a title to search."))

    def _use_playlist_track(self, *_args):
        track_id = self.playlist_combo.currentData()
        track = next((track for track in self.tracks if track.id == track_id), None)
        if track is None:
            return
        self.title_edit.setText(track.title)
        self.artist_edit.setText(track.artist)
        self.album_edit.setText("" if track.album.casefold() in {"unknown album", "알 수 없는 앨범"} else track.album)
        self.duration_spin.setValue(track.duration_seconds)
        self.target_combo.setCurrentIndex(self.target_combo.findData(track.id))

    def _sync_actions(self, *_args):
        busy = self.worker is not None
        self.search_button.setEnabled(not busy and bool(self.title_edit.text().strip()))
        self.download_button.setEnabled(not busy and self.document is not None)
        self.attach_button.setEnabled(not busy and self.document is not None and bool(self.document.cues)
                                      and bool(self.target_combo.currentData()) and self.on_attach is not None)
        self.target_combo.setEnabled(not busy and bool(self.tracks))

    def _start(self, kind, operation):
        self.worker = _LookupWorker(operation, self)
        self.worker.waiting.connect(self._waiting_for_provider)
        self.service.on_wait = self.worker.waiting.emit
        self.worker.finished.connect(self._request_finished)
        self.table.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        for widget in (self.playlist_combo, self.title_edit, self.artist_edit, self.album_edit,
                       self.duration_spin, self.provider_combo, self.synced_only):
            widget.setEnabled(False)
        self.progress.show()
        self.status.setText(self._tr("검색 중…", "Searching…") if kind == "search" else self._tr("가사 불러오는 중…", "Loading lyrics…"))
        self._sync_actions()
        self.worker.start()

    def _search(self):
        if self.worker is not None:
            return
        query = LyricsSearchQuery(self.title_edit.text().strip(), self.artist_edit.text().strip(),
            self.album_edit.text().strip(), self.duration_spin.value(), self.provider_combo.currentData(),
            synced_only=self.synced_only.isChecked())
        try:
            query.validate()
        except LyricsSearchError as error:
            self.status.setText(str(error))
            self.title_edit.setFocus()
            return
        self.document = None
        self._documents.clear()
        self.results = []
        self.table.setRowCount(0)
        self.preview.clear()
        self.detail_label.clear()
        self.warning.hide()
        self._start("search", lambda cancel: self.service.search(query, cancel))

    def _request_finished(self):
        worker, self.worker = self.worker, None
        if worker is None:
            return
        self.service.on_wait = None
        self.progress.hide()
        self.cancel_button.hide()
        self.form_group.setEnabled(True)
        for widget in (self.playlist_combo, self.title_edit, self.artist_edit, self.album_edit,
                       self.duration_spin, self.provider_combo, self.synced_only):
            widget.setEnabled(True)
        self.table.setEnabled(True)
        worker.deleteLater()
        if self._close_result is not None:
            super().done(self._close_result)
            return
        if worker.cancel_event.is_set():
            self.status.setText(self._tr("작업을 취소했습니다.", "Request cancelled."))
        elif worker.error:
            self.status.setText(worker.error)
        elif isinstance(worker.value, LyricsSearchBatch):
            self._show_results(worker.value)
        elif isinstance(worker.value, FoundLyrics):
            self._documents[worker.value.result.key] = worker.value
            self._show_document(worker.value)
        self._sync_actions()

    def _waiting_for_provider(self, seconds):
        if self.worker is not None and not self.worker.cancel_event.is_set():
            self.status.setText(self._tr(f"요청 간격 제한으로 약 {int(seconds) + 1}초 대기 중… 취소할 수 있습니다.",
                                        f"Waiting about {int(seconds) + 1}s for the provider request limit… You can cancel."))

    def _show_results(self, batch):
        self.results = batch.results
        self.warning.setText("\n".join(batch.warnings))
        self.warning.setVisible(bool(batch.warnings))
        self.table.setRowCount(len(self.results))
        for row, result in enumerate(self.results):
            timing = {"line": self._tr("줄 동기화", "Line timing"), "plain": self._tr("일반 텍스트", "Plain text"),
                      "word": self._tr("단어 동기화", "Word timing"),
                      "syllable": self._tr("음절 동기화", "Syllable timing"),
                      "unknown": self._tr("선택 후 확인", "Check on select")}[result.timing]
            values = (result.title, result.artist, result.album, format_clock(round(result.duration), hours=False), PROVIDERS[result.provider], timing)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.status.setText(self._tr(f"검색 결과 {len(self.results)}개 · 가사를 확인할 곡을 선택하세요.",
                                    f"{len(self.results)} results · select a recording to inspect its lyrics.") if self.results else
                            self._tr("검색 결과가 없습니다. 제목·아티스트·앨범 또는 제공자를 바꿔 보세요.",
                                     "No results. Try changing the title, artist, album, or provider."))

    def _select_result(self, *_args):
        if self.worker is not None:
            return
        self.document = None
        self.preview.clear()
        self._sync_actions()
        row = self.table.currentRow()
        if not 0 <= row < len(self.results):
            return
        result = self.results[row]
        self.detail_label.setText(f"{result.title}\n{result.artist} · {result.album}")
        cached = self._documents.get(result.key)
        if cached:
            self._show_document(cached)
        else:
            self._start("fetch", lambda cancel: self.service.fetch(result, cancel))

    def _show_document(self, document):
        self.document = document
        self.preview.setPlainText("\n".join(str(cue["text"]) for cue in document.cues) if document.cues else document.content)
        word_timed = any(cue.get("words") for cue in document.cues)
        timing = self._tr("단어·음절 동기화", "Word / syllable timing") if word_timed else (
            self._tr("줄 동기화", "Line timing") if document.cues else self._tr("시간 정보 없음 · 다운로드만 가능", "Untimed · download only"))
        self.status.setText(f"{PROVIDERS[document.result.provider]} · {document.format.upper()} · {timing}")
        self._sync_actions()

    def _download(self):
        if self.document is None or self.worker is not None:
            return
        document = self.document
        selected, _ = QFileDialog.getSaveFileName(self, self._tr("가사 다운로드", "Download lyrics"),
            document.filename, f"{document.format.upper()} lyrics (*.{document.format})")
        if not selected:
            return
        try:
            path = self.service.save(document, selected)
        except LyricsSearchError as error:
            QMessageBox.warning(self, self._tr("가사 저장 실패", "Could not save lyrics"), str(error))
            return
        self.saved_paths.append(path)
        self.status.setText(self._tr(f"저장했습니다 · {path}", f"Saved · {path}"))

    def _attach(self):
        if self.document is None or not self.document.cues or self.worker is not None or self.on_attach is None:
            return
        track_id = self.target_combo.currentData()
        if not track_id:
            return
        if self.on_attach(self.document, track_id):
            self.attached_track_ids.append(track_id)
            self.status.setText(self._tr("가사를 연결했습니다. 곡 설정에서 적용을 눌러 반영하세요.",
                                        "Lyrics staged. Click Apply in track settings to keep them.") if self.staged else
                                self._tr("선택한 곡에 가사를 연결했습니다. 프로젝트를 저장하면 함께 보관됩니다.",
                                         "Lyrics attached to the selected track. Save the project to keep them."))

    def _cancel_request(self):
        if self.worker is not None:
            self.worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status.setText(self._tr("취소 중…", "Cancelling…"))

    def _shutdown_worker(self):
        worker, self.worker = self.worker, None
        self.service.on_wait = None
        if worker is not None:
            worker.cancel_event.set()
            stop_qthread_now(worker)

    def done(self, result):
        if self.worker is not None:
            self._close_result = result
            self._cancel_request()
            self.close_button.setEnabled(False)
            return
        super().done(result)
