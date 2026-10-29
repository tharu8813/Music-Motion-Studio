"""Search recordings, inspect their lyrics, then download or attach a selection."""

from __future__ import annotations

from copy import deepcopy
from threading import Event
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QRadioButton, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.widgets.lyrics_cue_table import LyricsCueTable
from dataclasses import replace
from app.models.playlist import PlaylistTrack
from app.ui.studio_icons import menu_icon
from app.services.lyrics_search_service import (
    FoundLyrics, LyricsSearchBatch, LyricsSearchCancelled, LyricsSearchError,
    LyricsSearchQuery, LyricsSearchService, PROVIDERS, SEARCH_PROVIDERS,
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
        self._has_searched = False
        self.document: FoundLyrics | None = None
        self._documents: dict[tuple[str, str], FoundLyrics] = {}
        self.saved_paths = []
        self.attached_track_ids = []
        install_help_shortcut(self, ("lyrics_editor", "lyrics_files"), translator)
        self.setObjectName("lyricsDiscoveryDialog")
        self.setWindowIcon(menu_icon("lyrics_search"))
        self.resize(960, 680)
        self.setMinimumSize(760, 560)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        # A compact workflow header, shared visually with the lyrics editor.
        self.heading = QLabel()
        self.heading.setObjectName("dialogTitle")
        root.addWidget(self.heading)
        self.intro = QLabel()
        self.intro.setObjectName("mutedLabel")
        self.intro.setWordWrap(True)
        root.addWidget(self.intro)
        self.flow_label = QLabel()
        self.flow_label.setObjectName("lyricsDiscoveryFlow")
        root.addWidget(self.flow_label)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)

        self.form_group = QGroupBox()
        grid = QGridLayout(self.form_group)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        self.playlist_radio = QRadioButton()
        self.manual_radio = QRadioButton()
        mode_row = QHBoxLayout()
        mode_row.setSpacing(24)
        mode_row.addWidget(self.playlist_radio)
        mode_row.addWidget(self.manual_radio)
        mode_row.addStretch(1)
        grid.addLayout(mode_row, 0, 0, 1, 3)
        self.playlist_combo = QComboBox()
        for track in self.tracks:
            if searchable_track(track):
                self.playlist_combo.addItem(f"{track.title} — {track.artist}", track.id)
        self.playlist_label = QLabel()
        self.playlist_label.setBuddy(self.playlist_combo)
        playlist_row = QHBoxLayout()
        playlist_row.addWidget(self.playlist_label)
        playlist_row.addWidget(self.playlist_combo, 1)
        grid.addLayout(playlist_row, 1, 0, 1, 3)
        self.playlist_radio.setChecked(self.playlist_combo.count() > 0)
        self.manual_radio.setChecked(self.playlist_combo.count() == 0)
        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("mutedLabel")
        self.mode_hint.setWordWrap(True)
        grid.addWidget(self.mode_hint, 2, 0, 1, 3)
        self.title_edit, self.artist_edit, self.album_edit = QLineEdit(), QLineEdit(), QLineEdit()
        for edit in (self.title_edit, self.artist_edit, self.album_edit):
            edit.setClearButtonEnabled(True)
        self.provider_combo = QComboBox()
        for key in ("all", *SEARCH_PROVIDERS):
            self.provider_combo.addItem("", key)
        self.labels = {}
        for key, widget, row, column in (
            ("title", self.title_edit, 3, 0),
            ("artist", self.artist_edit, 3, 1),
            ("album", self.album_edit, 3, 2),
            ("provider", self.provider_combo, 5, 0),
        ):
            label = QLabel()
            label.setBuddy(widget)
            self.labels[key] = label
            grid.addWidget(label, row, column)
            grid.addWidget(widget, row + 1, column, 1, 2 if key == "provider" else 1)
        for column in range(3):
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
        grid.addLayout(search_row, 6, 2)
        grid.setRowStretch(7, 1)
        self.pages.addWidget(self.form_group)

        self.warning = QLabel()
        self.warning.setObjectName("warningLabel")
        self.warning.setWordWrap(True)
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        self.warning.hide()
        root.addWidget(self.warning)

        results_pane = QWidget()
        results_layout = QVBoxLayout(results_pane)
        results_layout.setContentsMargins(0, 0, 6, 0)
        results_layout.setSpacing(8)
        results_heading_row = QHBoxLayout()
        self.results_heading = QLabel()
        self.results_heading.setObjectName("panelTitle")
        self.result_count = QLabel()
        self.result_count.setObjectName("lyricsDiscoveryCount")
        results_heading_row.addWidget(self.results_heading)
        results_heading_row.addWidget(self.result_count)
        results_heading_row.addStretch(1)
        results_layout.addLayout(results_heading_row)
        self.result_filter = QLineEdit()
        self.result_filter.setObjectName("lyricsDiscoveryFilter")
        self.result_filter.setClearButtonEnabled(True)
        self.result_filter.setEnabled(False)
        results_layout.addWidget(self.result_filter)
        self.table = QTableWidget(0, 6)
        self.table.setObjectName("lyricsDiscoveryTable")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in ((0, 190), (1, 130), (2, 140), (3, 70), (4, 120), (5, 144)):
            self.table.setColumnWidth(column, width)
        for column in (0, 2):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        results_layout.addWidget(self.table, 1)
        self.pages.addWidget(results_pane)

        preview_box = QWidget()
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(10, 0, 0, 0)
        preview_layout.setSpacing(8)
        self.preview_heading = QLabel()
        self.preview_heading.setObjectName("panelTitle")
        preview_layout.addWidget(self.preview_heading)
        self.detail_label = QLabel()
        self.detail_label.setObjectName("mutedLabel")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        preview_layout.addWidget(self.detail_label)
        self.preview_stack = QStackedWidget()
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(16, 16, 16, 16)
        empty_layout.addStretch(1)
        self.empty_heading = QLabel()
        self.empty_heading.setObjectName("lyricsDiscoveryEmptyHeading")
        self.empty_heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_heading.setWordWrap(True)
        self.empty_hint = QLabel()
        self.empty_hint.setObjectName("mutedLabel")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        empty_layout.addWidget(self.empty_heading)
        empty_layout.addWidget(self.empty_hint)
        empty_layout.addStretch(1)
        self.preview_stack.addWidget(empty_page)
        self.preview = LyricsCueTable()
        self.preview.setObjectName("lyricsDiscoveryPreview")
        selection_row = QHBoxLayout()
        self.select_all_button = QPushButton()
        self.select_none_button = QPushButton()
        self.selection_count = QLabel()
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.select_none_button)
        selection_row.addWidget(self.selection_count, 1)
        preview_layout.addLayout(selection_row)
        self.select_all_button.clicked.connect(lambda: self.preview.select_all_rows(True))
        self.select_none_button.clicked.connect(lambda: self.preview.select_all_rows(False))
        self.preview.itemChanged.connect(self._sync_actions)
        self.preview_stack.addWidget(self.preview)
        preview_layout.addWidget(self.preview_stack, 1)
        self.preview_info = QLabel()
        self.preview_info.setObjectName("mutedLabel")
        self.preview_info.setWordWrap(True)
        preview_layout.addWidget(self.preview_info)
        self.pages.addWidget(preview_box)
        self.table.horizontalHeader().setStretchLastSection(False)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumHeight(5)
        self.progress.setTextVisible(False)
        self.progress.hide()
        root.addWidget(self.progress)
        self.status = QLabel()
        self.status.setObjectName("lyricsDiscoveryStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("lyricsDiscoverySeparator")
        root.addWidget(separator)
        self.target_panel = QWidget()
        target_row = QHBoxLayout(self.target_panel)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(8)
        self.target_label = QLabel()
        self.target_combo = QComboBox()
        self.target_combo.setMinimumWidth(175)
        self.target_combo.addItem("", "")
        for track in self.tracks:
            self.target_combo.addItem(f"{track.title} — {track.artist}", track.id)
        target_row.addWidget(self.target_label)
        target_row.addWidget(self.target_combo, 1)
        self.copy_button = QPushButton()
        self.download_button, self.attach_button, self.close_button = QPushButton(), QPushButton(), QPushButton()
        self.attach_button.setProperty("primary", True)
        target_row.addWidget(self.copy_button)
        target_row.addWidget(self.download_button)
        target_row.addWidget(self.attach_button)
        root.addWidget(self.target_panel)
        navigation = QHBoxLayout()
        self.back_button, self.next_button = QPushButton(), QPushButton()
        self.next_button.setProperty("primary", True)
        navigation.addWidget(self.back_button)
        navigation.addStretch(1)
        navigation.addWidget(self.cancel_button)
        navigation.addWidget(self.close_button)
        navigation.addWidget(self.search_button)
        navigation.addWidget(self.next_button)
        root.addLayout(navigation)
        footer_row = QHBoxLayout()
        self.note = QLabel()
        self.note.setObjectName("mutedLabel")
        self.note.setWordWrap(True)
        footer_row.addWidget(self.note, 1)
        self.credit = QLabel('<a href="https://www.kugou.com">KuGou</a> · '
                            '<a href="https://github.com/amll-dev/amll-ttml-db">AMLL TTML DB</a> · '
                            '<a href="https://lrclib.net">LRCLIB</a>')
        self.credit.setOpenExternalLinks(True)
        self.credit.setObjectName("mutedLabel")
        footer_row.addWidget(self.credit, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(footer_row)

        self.playlist_combo.currentIndexChanged.connect(self._use_playlist_track)
        self.playlist_radio.toggled.connect(self._change_input_mode)
        for edit in (self.title_edit, self.artist_edit, self.album_edit):
            edit.textEdited.connect(self._use_manual_input)
        self.title_edit.textChanged.connect(self._sync_actions)
        self.target_combo.currentIndexChanged.connect(self._sync_actions)
        self.search_button.clicked.connect(self._search)
        self.cancel_button.clicked.connect(self._cancel_request)
        self.table.currentCellChanged.connect(self._select_result)
        self.table.itemDoubleClicked.connect(self._select_result)
        self.result_filter.textChanged.connect(self._filter_results)
        self.copy_button.clicked.connect(self._copy_lyrics)
        self.download_button.clicked.connect(self._download)
        self.attach_button.clicked.connect(self._attach)
        self.close_button.clicked.connect(self.reject)
        self.back_button.clicked.connect(lambda: self._set_step(self.pages.currentIndex() - 1))
        self.next_button.clicked.connect(lambda: self._set_step(2))
        title_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        title_shortcut.activated.connect(self._focus_search)
        results_shortcut = QShortcut(QKeySequence("Ctrl+Shift+F"), self)
        results_shortcut.activated.connect(self._focus_results)
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
        if self.playlist_radio.isChecked():
            self._use_playlist_track()
        self._sync_actions()

    def _tr(self, ko, en):
        return ko if self.translator.is_korean else en

    def _focus_search(self):
        if self.worker is None:
            self._set_step(0)
            self.title_edit.setFocus()

    def _focus_results(self):
        if self.worker is None and self._has_searched:
            self._set_step(1)
            self.result_filter.setFocus()

    def _set_step(self, step):
        if self.worker is not None or not 0 <= step <= 2:
            return
        if step == 2 and self.document is None:
            return
        self.pages.setCurrentIndex(step)
        if step == 2:
            self.status.setText(self._tr(
                "가사를 확인한 뒤 파일로 저장하거나 선택한 곡에 적용하세요.",
                "Review the lyrics, then save a file or attach them to the selected track."))
        self._sync_actions()
        (self.title_edit, self.result_filter, self.preview)[step].setFocus()

    def _update_navigation(self):
        step = self.pages.currentIndex()
        busy = self.worker is not None
        names = [self._tr("곡 정보", "Song details"), self._tr("결과 선택", "Choose lyrics"),
                 self._tr("확인 및 적용", "Review & apply")]
        self.flow_label.setText(" &nbsp; › &nbsp; ".join(
            f"<b>{index + 1:02}  {name}</b>" if index == step else f"{index + 1:02}  {name}"
            for index, name in enumerate(names)))
        self.intro.setText(self._tr(*(
            ("KuGou·AMLL·LRCLIB에서 가사를 찾습니다. 결과를 선택하면 시간 정보를 확인합니다.",
             "Search KuGou, AMLL and LRCLIB. Select a result to check its timing."),
            ("곡과 길이를 비교해 결과를 선택한 뒤 ‘다음’을 누르세요. 이전으로 돌아가 검색 조건을 바꿀 수 있습니다.",
             "Compare recordings and durations, select a result, then click Next. Go Back to change your search."),
            ("가사와 동기화 방식을 확인하고 저장하거나 연결할 곡에 적용하세요.",
             "Review the lyrics and timing, then save a file or attach to a track."),
        )[step]))
        self.back_button.setText(self._tr("이전", "Back"))
        self.next_button.setText(self._tr("다음 · 가사 확인", "Next · Review lyrics"))
        self.back_button.setVisible(step > 0)
        self.back_button.setEnabled(not busy)
        self.next_button.setVisible(step == 1)
        self.next_button.setEnabled(not busy and self.document is not None)
        self.search_button.setVisible(step == 0)
        self.target_panel.setVisible(step == 2)
        self.note.setVisible(step == 2)
        self.search_button.setDefault(step == 0)
        self.next_button.setDefault(step == 1)
        self.attach_button.setDefault(step == 2 and self.attach_button.isEnabled())

    def retranslate(self):
        self.setWindowTitle(self._tr("가사 검색 및 가져오기", "Search & Import Lyrics"))
        self.heading.setText(self._tr("가사 검색 및 가져오기", "Search & Import Lyrics"))
        self.form_group.setTitle(self._tr("01  곡 정보", "01  Song details"))
        self.playlist_radio.setText(self._tr("플레이리스트에서 찾기", "Find from playlist"))
        self.manual_radio.setText(self._tr("직접 입력", "Enter manually"))
        self.playlist_label.setText(self._tr("음악 선택", "Choose music"))
        for key, labels in {
            "title": ("제목 *", "Title *"), "artist": ("아티스트", "Artist"), "album": ("앨범", "Album"),
            "provider": ("제공자", "Provider"),
        }.items():
            self.labels[key].setText(self._tr(*labels))
        self.title_edit.setPlaceholderText(self._tr("검색할 곡의 제목", "Song title to search"))
        self.artist_edit.setPlaceholderText(self._tr("정확도를 높이려면 입력", "Optional, improves matching"))
        self.album_edit.setPlaceholderText(self._tr("앨범명 (선택)", "Album name (optional)"))
        for widget in (self.title_edit, self.artist_edit, self.album_edit):
            widget.setToolTip(self._tr(
                "제목·아티스트로 검색합니다. 앨범과 길이는 일치하는 결과의 우선순위를 높입니다.",
                "Search by title and artist. Album and duration help rank matching recordings."))
        for index, key in enumerate(("all", *SEARCH_PROVIDERS)):
            self.provider_combo.setItemText(index, self._tr("모든 공급원 검색 (권장)", "Search all providers (recommended)")
                if key == "all" else {"kugou": "KuGou · KRC", "amll": "AMLL TTML DB · TTML",
                                      "lrclib": "LRCLIB · LRC / TXT"}[key])
        self.provider_combo.setToolTip(self._tr(
            "KuGou KRC·AMLL TTML·LRCLIB LRC/TXT를 직접 조회합니다. 단어 시간은 원문에 있는 경우에만 사용합니다.",
            "Connect directly to KuGou KRC, AMLL TTML and LRCLIB LRC/TXT. Word timing is used only when present in the source."))
        self.synced_only.setText(self._tr("시간 정보가 있는 가사만", "Timed lyrics only"))
        self.synced_only.setToolTip(self._tr(
            "시간 정보가 없는 가사를 검색 결과에서 제외합니다.",
            "Hide untimed lyrics from search results."))
        self.search_button.setText(self._tr("다음 · 가사 검색", "Next · Search lyrics"))
        self.cancel_button.setText(self._tr("요청 취소", "Cancel request"))
        self.results_heading.setText(self._tr("02  검색 결과", "02  Search results"))
        self.result_filter.setPlaceholderText(self._tr(
            "결과 내 검색 (곡·아티스트·제공자)", "Filter results by song, artist, or provider"))
        self.result_filter.setToolTip(self._tr(
            "이미 검색된 결과만 필터링합니다. Ctrl+Shift+F",
            "Filter the current results without another request. Ctrl+Shift+F"))
        self._update_result_count()
        self.table.setHorizontalHeaderLabels([self._tr(*pair) for pair in (
            ("제목", "Title"), ("아티스트", "Artist"), ("앨범", "Album"),
            ("길이", "Length"), ("제공자", "Provider"), ("가사 타입", "Lyrics type"))])
        header = self.table.horizontalHeaderItem(5)
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        for row, result in enumerate(self.results):
            item = self.table.item(row, 5)
            if item is not None:
                timing = self._timing_text(result.timing)
                item.setText(timing)
                item.setToolTip(timing)
        self._filter_results()
        if self.document is not None:
            self._update_preview_details(self.document)
        self.preview_heading.setText(self._tr("03  가사 미리보기", "03  Lyrics preview"))
        self.empty_heading.setText(self._tr(
            "여기에 가사가 표시됩니다", "Lyrics preview appears here"))
        self.empty_hint.setText(self._tr(
            "이전 단계에서 검색 결과를 선택하세요.",
            "Select a result in the previous step."))
        self.target_label.setText(self._tr("연결할 곡", "Attach to"))
        self.target_combo.setItemText(0, self._tr("곡 선택", "Choose a track"))
        self.preview.setToolTip(self._tr(
            "결과를 선택하면 가사가 표시됩니다.", "Select a result to preview lyrics."))
        self.table.setToolTip(self._tr(
            "한 번 클릭해 미리보기 · 불러오기 실패 시 두 번 클릭해 재시도",
            "Click to preview · double-click to retry a failed request"))
        self.copy_button.setText(self._tr("가사 텍스트 복사", "Copy lyrics text"))
        self.copy_button.setToolTip(self._tr(
            "미리보기에 표시된 가사 텍스트를 시간 정보 없이 복사합니다.",
            "Copy the text displayed in the preview, without timestamps."))
        self.download_button.setText(self._tr("파일로 저장…", "Save file…"))
        self.attach_button.setText(self._tr("곡에 적용", "Attach to track"))
        self.close_button.setText(self._tr("닫기", "Close"))
        self.note.setText(self._tr(
            "시간 정보가 없는 가사는 복사·저장만 가능합니다.",
            "Untimed lyrics can be copied or saved, but not attached to a track.") +
            (self._tr(" 곡 설정에서 적용을 눌러야 변경사항이 유지됩니다.",
                      " Click Apply in track settings to keep changes.") if self.staged else ""))
        if not self.status.text():
            self.status.setText(self._tr(
                "곡 제목을 입력하고 가사를 검색하세요.", "Enter a song title to search for lyrics."))
        self._sync_actions()

    def _timing_text(self, timing: str) -> str:
        labels = {
            "line": ("줄 동기화", "Line timing"),
            "plain": ("일반 텍스트", "Plain text"),
            "word": ("단어 동기화", "Word timing"),
            "syllable": ("음절 동기화", "Syllable timing"),
            "unknown": ("선택 후 확인", "Check on select"),
        }
        return self._tr(*labels.get(timing, labels["unknown"]))

    def _update_result_count(self):
        visible = sum(not self.table.isRowHidden(row) for row in range(self.table.rowCount()))
        total = len(self.results)
        self.result_count.setText(
            self._tr(f"{visible} / {total}개", f"{visible} / {total}")
            if self._has_searched else self._tr("검색 전", "Not searched"))

    def _filter_results(self, *_args):
        terms = self.result_filter.text().casefold().split()
        selected_row = self.table.currentRow()
        for row in range(self.table.rowCount()):
            # Search the visible translated labels, not internal timing codes.
            content = " ".join(
                self.table.item(row, column).text()
                for column in range(self.table.columnCount())
                if self.table.item(row, column) is not None
            ).casefold()
            self.table.setRowHidden(row, not all(term in content for term in terms))
        if selected_row >= 0 and self.table.isRowHidden(selected_row):
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            self.document = None
            self.preview.clear()
            self.preview_stack.setCurrentIndex(0)
            self.detail_label.clear()
            self.preview_info.clear()
            self._sync_actions()
        self._update_result_count()

    def _use_playlist_track(self, *_args):
        if not self.playlist_radio.isChecked():
            return
        track_id = self.playlist_combo.currentData()
        track = next((track for track in self.tracks if track.id == track_id), None)
        if track is None:
            return
        self.title_edit.setText(track.title)
        self.artist_edit.setText(track.artist)
        self.album_edit.setText("" if track.album.casefold() in {"unknown album", "알 수 없는 앨범"} else track.album)
        self.target_combo.setCurrentIndex(self.target_combo.findData(track.id))

    def _change_input_mode(self, playlist_mode):
        if playlist_mode:
            self._use_playlist_track()
        self._sync_actions()

    def _use_manual_input(self, *_args):
        self.manual_radio.setChecked(True)

    def _sync_actions(self, *_args):
        selected_count = len(self.preview.selected_cues())
        self.selection_count.setText(self._tr(f"{selected_count} / {self.preview.rowCount()}행 선택 · 선택한 행만 적용", f"{selected_count} / {self.preview.rowCount()} rows selected · only checked rows are applied"))
        self.select_all_button.setText(self._tr("전체 선택", "Select all"))
        self.select_none_button.setText(self._tr("전체 해제", "Clear selection"))
        busy = self.worker is not None
        playlist_mode = self.playlist_radio.isChecked()
        self.playlist_radio.setEnabled(not busy and self.playlist_combo.count() > 0)
        self.manual_radio.setEnabled(not busy)
        self.playlist_combo.setEnabled(not busy and playlist_mode)
        self.playlist_label.setEnabled(not busy and playlist_mode)
        self.mode_hint.setText(self._tr(
            "선택한 음악의 정보를 사용합니다. 아래 내용을 수정하면 직접 입력으로 전환됩니다.",
            "Use the selected music's details. Editing a field switches to manual input.") if playlist_mode else self._tr(
            "검색할 곡 정보를 직접 입력하세요. 제목은 필수이며 나머지는 선택입니다.",
            "Enter song details. Title is required; other fields are optional."))
        self.provider_combo.setEnabled(not busy)
        self.search_button.setEnabled(not busy and bool(self.title_edit.text().strip()))
        self.download_button.setEnabled(not busy and self.document is not None)
        self.copy_button.setEnabled(not busy and self.document is not None)
        self.result_filter.setEnabled(not busy and bool(self.results))
        self.attach_button.setEnabled(not busy and self.document is not None and bool(self.document.cues) and selected_count > 0
                                      and bool(self.target_combo.currentData()) and self.on_attach is not None)
        self.target_combo.setEnabled(not busy and bool(self.tracks))
        self._update_navigation()

    def _start(self, kind, operation):
        self.worker = _LookupWorker(operation, self)
        self.worker.waiting.connect(self._waiting_for_provider)
        self.service.on_wait = self.worker.waiting.emit
        self.worker.finished.connect(self._request_finished)
        self.table.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        for widget in (self.playlist_combo, self.title_edit, self.artist_edit, self.album_edit,
                       self.provider_combo, self.synced_only):
            widget.setEnabled(False)
        self.progress.show()
        self.status.setText(self._tr("검색 중…", "Searching…") if kind == "search" else self._tr("가사 불러오는 중…", "Loading lyrics…"))
        self._sync_actions()
        self.worker.start()

    def _search(self):
        if self.worker is not None:
            return
        self._set_step(0)
        track = next((track for track in self.tracks if self.playlist_radio.isChecked()
                      and track.id == self.playlist_combo.currentData()
                      and track.title.strip() == self.title_edit.text().strip()
                      and track.artist.strip() == self.artist_edit.text().strip()), None)
        query = LyricsSearchQuery(self.title_edit.text().strip(), self.artist_edit.text().strip(),
            self.album_edit.text().strip(), track.duration_seconds if track else 0, self.provider_combo.currentData(),
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
        self._has_searched = False
        self.table.setRowCount(0)
        self.preview.clear()
        self.preview_stack.setCurrentIndex(0)
        self.detail_label.clear()
        self.preview_info.clear()
        self.result_filter.clear()
        self._update_result_count()
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
                       self.provider_combo, self.synced_only):
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
        self._has_searched = True
        self.warning.setText("\n".join(batch.warnings))
        self.warning.setVisible(bool(batch.warnings))
        self.table.setRowCount(len(self.results))
        for row, result in enumerate(self.results):
            timing = self._timing_text(result.timing)
            values = (result.title, result.artist, result.album,
                      format_clock(round(result.duration), hours=False) if result.duration else "—",
                      PROVIDERS[result.provider], timing)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column in (0, 5):
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if column in (3, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
        self._filter_results()
        self._set_step(1)
        summary = " · ".join(f"{PROVIDERS[key]} {count}" for key, count in batch.provider_counts.items())
        message = self._tr(f"검색 결과 {len(self.results)}개 · 가사를 확인할 곡을 선택하세요.",
                          f"{len(self.results)} results · select a recording to inspect its lyrics.") if self.results else self._tr(
                "등록된 가사를 찾지 못했습니다. ‘이전’에서 제목만 검색하거나 아티스트의 다른 표기를 사용해 보세요.",
                "No registered lyrics found. Go Back to search by title alone or try another artist spelling.")
        self.status.setText((summary + "\n" if summary else "") + message)

    def _select_result(self, *_args):
        if self.worker is not None:
            return
        self.document = None
        self.preview.clear()
        self.preview_stack.setCurrentIndex(0)
        self.preview_info.clear()
        self._sync_actions()
        row = self.table.currentRow()
        if not 0 <= row < len(self.results) or self.table.isRowHidden(row):
            self.detail_label.clear()
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
        for row, result in enumerate(self.results):
            if result.key == document.result.key:
                self.results[row] = document.result
                self.table.item(row, 5).setText(self._timing_text(document.result.timing))
                self.table.item(row, 5).setToolTip(self._timing_text(document.result.timing))
                break
        self.detail_label.setText(f"{document.result.title}\n{document.result.artist} · {document.result.album}")
        self.preview_stack.setCurrentIndex(1)
        self.preview.set_cues(document.cues or [{"text": line} for line in document.content.splitlines()], self.translator.is_korean)
        self._update_preview_details(document)
        self.status.setText(self._tr("선택한 가사를 불러왔습니다. ‘다음’에서 내용을 확인하세요.",
                                     "Lyrics loaded. Click Next to review them."))
        self._sync_actions()

    def _update_preview_details(self, document: FoundLyrics):
        word_timed = any(cue.get("words") for cue in document.cues)
        timing = self._tr("단어·음절 동기화", "Word / syllable timing") if word_timed else (
            self._tr("줄 동기화", "Line timing") if document.cues else
            self._tr("시간 정보 없음 · 다운로드만 가능", "Untimed · download only"))
        format_name = "KRC → TTML" if document.result.provider == "kugou" else document.format.upper()
        self.preview_info.setText(f"{PROVIDERS[document.result.provider]}  ·  {format_name}  ·  {timing}\n" + self._tr("체크한 행만 곡에 적용합니다. 다운로드는 원본 파일을 저장합니다.", "Checked rows are applied to the track. Download saves the original file."))

    def _copy_lyrics(self):
        if self.document is None or self.worker is not None:
            return
        QApplication.clipboard().setText(self.preview.toPlainText())
        self.status.setText(self._tr("가사 텍스트를 복사했습니다.", "Lyrics text copied."))

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
        cues = self.preview.selected_cues()
        if not cues:
            return
        selected = replace(self.document, cues=cues)
        if self.on_attach(selected, track_id):
            self.attached_track_ids.append(track_id)
            self.status.setText(self._tr("가사를 연결했습니다. 곡 설정에서 적용을 눌러 반영하세요.",
                                        "Lyrics staged. Click Apply in track settings to keep them.") if self.staged else
                                self._tr("선택한 곡에 가사를 연결했습니다. 프로젝트를 저장하면 함께 보관됩니다.",
                                         "Lyrics attached to the selected track. Save the project to keep them."))
            QMessageBox.information(self, self._tr("가사 선택 완료" if self.staged else "가사 적용 완료", "Lyrics selected" if self.staged else "Lyrics applied"),
                self._tr(f"선택한 가사 {len(cues)}행을 연결했습니다. 곡 설정에서 적용을 눌러 확정하세요." if self.staged else f"선택한 가사 {len(cues)}행을 적용했습니다.",
                         f"Attached {len(cues)} selected rows. Click Apply in track settings to confirm." if self.staged else f"Applied {len(cues)} selected rows."))
            self.accept()

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
