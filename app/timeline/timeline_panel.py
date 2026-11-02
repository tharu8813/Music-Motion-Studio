"""Timeline editor for playlist tracks and visual source display ranges."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, QTimer, Signal, QSignalBlocker
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.models.source import Source
from app.timeline.visual_timeline import VisualTimeline, clock
from app.controllers.keyframe_controller import KeyframeController
from app.timeline.keyframe_editor import KeyframeEditor
from app.services.playlist_service import PlaylistService
from app.services.source_store import SourceStore
from app.utils.i18n import Translator
from app.utils.time_format import format_clock


class TimelineSpinBox(QDoubleSpinBox):
    """Seconds input that accepts and displays a friendly ``MM:SS`` timecode."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRange(0.0, 86_400.0)
        self.setDecimals(2)
        self.setSingleStep(1.0)
        self.setKeyboardTracking(False)
        self.setMinimumWidth(82)

    @staticmethod
    def format_timecode(value: float) -> str:
        """Format seconds without allocating a temporary QWidget."""
        return format_clock(round(value))

    def textFromValue(self, value: float) -> str:
        return clock(value)

    def valueFromText(self, text: str) -> float:
        """Parse plain seconds, ``MM:SS``, or ``HH:MM:SS`` input."""
        parts = [part.strip() for part in text.split(":")]
        try:
            if len(parts) == 1:
                return float(parts[0])
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return self.value()
        return self.value()

    def validate(self, text: str, position: int) -> tuple[QValidator.State, str, int]:
        """Accept partial numeric timecode while the user is typing."""
        if re.fullmatch(r"[0-9:.]*", text):
            return (QValidator.State.Acceptable, text, position)
        return super().validate(text, position)


class TimelinePanel(QFrame):
    """Visual lanes with the existing numeric timing tables as a precision view."""

    preview_requested = Signal(float)

    def __init__(self, playlist: PlaylistService, sources: SourceStore,
                 translator: Translator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("timelineStrip")
        self.playlist = playlist
        self.sources = sources
        self.translator = translator
        self._refreshing = False
        self._refresh_pending = False
        self._shown: tuple | None = None
        self.setMinimumHeight(250)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("panelTitle")
        self.summary = QLabel()
        self.summary.setObjectName("mutedLabel")
        self.up_button = QPushButton("↑")
        self.down_button = QPushButton("↓")
        self.up_button.setObjectName("timelineMoveButton")
        self.down_button.setObjectName("timelineMoveButton")
        self.view_combo = QComboBox()
        self.view_combo.addItems(["", ""])
        self.view_combo.setObjectName("timelineViewCombo")
        header.addWidget(self.title)
        header.addWidget(self.summary)
        header.addStretch()
        header.addWidget(self.view_combo)
        header.addWidget(self.up_button)
        header.addWidget(self.down_button)
        layout.addLayout(header)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("timelineTabs")
        self.tabs.setDocumentMode(True)
        self.track_table = QTableWidget(0, 5)
        self.track_table.setObjectName("timelineTrackTable")
        self._configure_table(self.track_table)
        self.source_table = QTableWidget(0, 3)
        self.source_table.setObjectName("timelineSourceTable")
        self._configure_table(self.source_table)
        track_header = self.track_table.horizontalHeader()
        track_header.setStretchLastSection(False)
        track_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        track_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4):
            track_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents,
            )
        source_header = self.source_table.horizontalHeader()
        source_header.setStretchLastSection(False)
        source_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        source_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        source_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.track_page = self._table_page(self.track_table)
        self.source_page = self._table_page(self.source_table)
        # No tracks yet: say where they come from instead of showing a bare header row.
        self.track_empty_label = QLabel()
        self.track_empty_label.setObjectName("mutedLabel")
        self.track_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.track_empty_label.setWordWrap(True)
        self.track_page.layout().addWidget(self.track_empty_label, 1)
        self.tabs.addTab(self.track_page, "")
        self.tabs.addTab(self.source_page, "")
        self.visual = VisualTimeline()
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.visual)
        self.view_stack.addWidget(self.tabs)
        self.keyframe_controller = KeyframeController(sources, playlist.timeline_tracks, self)
        self.keyframe_editor = KeyframeEditor(self.keyframe_controller, translator, self)
        self.view_stack.addWidget(self.keyframe_editor)
        self.view_combo.addItem("")
        self.visual.position_changed.connect(self.keyframe_controller.seek_global)
        self.keyframe_controller.position_changed.connect(self._follow_keyframe_position)
        self.keyframe_controller.editor_requested.connect(lambda: self.view_combo.setCurrentIndex(2))
        playlist.playlist_changed.connect(self.keyframe_controller.changed.emit)
        self.visual_toolbar = QWidget()
        tools = QHBoxLayout(self.visual_toolbar)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(6)
        self.snap_button = QPushButton()
        self.snap_button.setCheckable(True)
        self.snap_button.setChecked(True)
        self.fit_button = QPushButton()
        self.zoom_out = QPushButton("−")
        self.zoom_in = QPushButton("+")
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("mutedLabel")
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position_editor = TimelineSpinBox()
        self.preview_button = QPushButton()
        for button in (self.snap_button, self.fit_button, self.zoom_out, self.zoom_in, self.preview_button):
            button.setObjectName("timelineToolButton")
        for widget in (self.snap_button, self.fit_button, self.zoom_out, self.zoom_label, self.zoom_in):
            tools.addWidget(widget)
        tools.addStretch()
        tools.addWidget(self.position_editor)
        tools.addWidget(self.preview_button)
        layout.addWidget(self.visual_toolbar)
        layout.addWidget(self.view_stack, 1)
        self.visual_hint = QLabel()
        self.visual_hint.setObjectName("timelineHint")
        self.visual_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.visual_hint)
        self.view_combo.currentIndexChanged.connect(self._change_view)
        self.snap_button.toggled.connect(lambda enabled: setattr(self.visual, "snapping", enabled))
        self.fit_button.clicked.connect(self.visual.fit_all)
        self.zoom_out.clicked.connect(lambda: self.visual.zoom(.8))
        self.zoom_in.clicked.connect(lambda: self.visual.zoom(1.25))
        self.visual.zoom_changed.connect(lambda percent: self.zoom_label.setText(f"{percent}%"))
        self.visual.position_changed.connect(self._show_position)
        self.position_editor.valueChanged.connect(self.visual.set_playhead)
        self.preview_button.clicked.connect(lambda: self.preview_requested.emit(self.visual.playhead))
        self.visual.hint_changed.connect(self.visual_hint.setText)
        self.visual.selection_changed.connect(self._select_visual_block)
        self.visual.timing_committed.connect(self._commit_visual_timing)
        self.visual.reset_requested.connect(self._reset_visual_timing)
        self.visual.numeric_edit_requested.connect(self._edit_numerically)
        self.up_button.clicked.connect(lambda: self._move_selected_track(-1))
        self.down_button.clicked.connect(lambda: self._move_selected_track(1))
        self.source_table.itemSelectionChanged.connect(self._select_source_on_canvas)
        self.track_table.itemSelectionChanged.connect(self._update_move_buttons)
        self.tabs.currentChanged.connect(lambda _index: self._update_move_buttons())
        playlist.playlist_changed.connect(self.schedule_refresh)
        sources.source_added.connect(lambda _source: self.schedule_refresh())
        sources.source_removed.connect(lambda _source_id: self.schedule_refresh())
        sources.source_changed.connect(lambda _source: self.schedule_refresh())
        sources.sources_replaced.connect(self.schedule_refresh)
        sources.selection_changed.connect(self._follow_source_selection)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()
        self.refresh()
        self._change_view(0)
        self._follow_source_selection(sources.selected)

    @staticmethod
    def _table_page(table: QTableWidget) -> QWidget:
        """Place one timing table inside a clean tab page without extra chrome."""
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 6, 0, 0)
        page_layout.addWidget(table)
        return page

    @staticmethod
    def _configure_table(table: QTableWidget) -> None:
        """Apply consistent compact editor-table behavior to timeline sections."""
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setFrameShape(QFrame.Shape.NoFrame)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(38)
        table.horizontalHeader().setFixedHeight(30)

    def retranslate(self) -> None:
        """Refresh static labels in the current language."""
        korean = self.translator.is_korean
        self.title.setText("타임라인" if korean else "Timeline")
        self.visual.korean = korean
        self.view_combo.setItemText(0, "시각 편집" if korean else "Visual editor")
        self.view_combo.setItemText(1, "숫자 편집" if korean else "Numeric editor")
        self.view_combo.setItemText(2, "키프레임 편집" if korean else "Keyframe editor")
        self.snap_button.setText("스냅" if korean else "Snap")
        self.snap_button.setToolTip("곡·요소의 시작과 끝에 맞춥니다. Shift를 누르면 스냅을 임시로 반전합니다."
                                   if korean else "Snap to clip edges. Hold Shift to temporarily invert snapping.")
        self.fit_button.setText("전체 보기" if korean else "Fit all")
        self.fit_button.setToolTip("전체 곡과 요소를 한 화면에 맞춥니다." if korean else "Fit every track and source into the view.")
        self.zoom_out.setToolTip("축소 · Ctrl+휠 / −" if korean else "Zoom out · Ctrl+wheel / −")
        self.zoom_in.setToolTip("확대 · Ctrl+휠 / +" if korean else "Zoom in · Ctrl+wheel / +")
        self.preview_button.setText("이 위치 미리보기" if korean else "Preview here")
        self.preview_button.setToolTip("선택한 시간에서 메인 재생 미리보기를 엽니다." if korean else "Open Full Preview at this time.")
        self.position_editor.setAccessibleName("재생 위치" if korean else "Playhead position")
        self.position_editor.setToolTip("시간축을 클릭하거나 시간을 입력해 미리보기 위치를 정합니다." if korean else "Click the ruler or enter a time to set the preview position.")
        self.visual.setAccessibleName("시각 타임라인 편집기" if korean else "Visual timeline editor")
        self.visual.setAccessibleDescription("막대를 끌어 이동하고 요소의 양끝을 끌어 길이를 바꿉니다. 위·아래 키로 선택하고 좌·우 키로 이동합니다. Shift로 0.1초 이동, Esc로 끌기를 취소합니다."
                                             if korean else "Drag blocks to move; drag source edges to resize. Up/down selects; left/right nudges; Shift nudges 0.1 seconds; Escape cancels a drag.")
        self._default_visual_hint()
        self.track_table.setHorizontalHeaderLabels(
            ["#", "트랙" if korean else "Track", "시작" if korean else "Start",
             "길이" if korean else "Duration", "종료" if korean else "End"]
        )
        self.source_table.setHorizontalHeaderLabels(
            ["소스" if korean else "Source", "시작" if korean else "Start",
             "지속 시간" if korean else "Duration"]
        )
        self.tabs.setTabText(0, "음악 타임라인" if korean else "Music timeline")
        self.tabs.setTabText(1, "소스 타이밍" if korean else "Source timing")
        self.up_button.setToolTip("위로 이동" if korean else "Move up")
        self.down_button.setToolTip("아래로 이동" if korean else "Move down")
        self.track_empty_label.setText(
            "플레이리스트 탭에서 음악을 추가하면 곡별 시작 시간과 길이가 여기에 표시됩니다."
            if korean else
            "Add music in the Playlist tab to see and edit each track's start time here."
        )
        self.refresh()

    def refresh(self) -> None:
        """Rebuild timeline rows from the playlist and source stores."""
        if self._refreshing:
            return
        timeline_tracks = self.playlist.timeline_tracks()
        source_list = self.sources.sources()
        # Moving or restyling a source changes nothing shown here: keep every editor.
        shown = (
            self.translator.is_korean,
            tuple((track.id, track.title, track.artist, track.duration_label, start, end,
                   self.playlist.minimum_start_time(track.id)) for track, start, end in timeline_tracks),
            tuple((source.id, source.name, source.timeline_start, source.timeline_duration,
                   source.locked, source.visible, source.source_type, source.z_index)
                  for source in source_list),
        )
        if shown == self._shown:
            return
        self._shown = shown
        selected_track_id = self._selected_track_id()
        selected_source_id = self._selected_source_id()
        track_scroll = self.track_table.verticalScrollBar().value()
        source_scroll = self.source_table.verticalScrollBar().value()
        self._refreshing = True
        try:
            self.track_table.setRowCount(len(timeline_tracks))
            for row, (track, start, end) in enumerate(timeline_tracks):
                number = QTableWidgetItem(str(row + 1))
                name = QTableWidgetItem(f"{track.title} — {track.artist}")
                number.setData(Qt.ItemDataRole.UserRole, track.id)
                self.track_table.setItem(row, 0, number)
                self.track_table.setItem(row, 1, name)
                start_editor = TimelineSpinBox()
                minimum_start = self.playlist.minimum_start_time(track.id)
                start_editor.setMinimum(minimum_start)
                start_editor.setValue(max(start, minimum_start))
                start_editor.setToolTip(
                    f"최소 시작 {TimelineSpinBox.format_timecode(minimum_start)}"
                    if self.translator.is_korean else
                    f"Minimum {TimelineSpinBox.format_timecode(minimum_start)}"
                )
                start_editor.valueChanged.connect(
                    lambda value, identifier=track.id: self._on_track_start_changed(identifier, value)
                )
                self.track_table.setCellWidget(row, 2, start_editor)
                duration = QTableWidgetItem(track.duration_label)
                end_item = QTableWidgetItem(TimelineSpinBox.format_timecode(end))
                self.track_table.setItem(row, 3, duration)
                self.track_table.setItem(row, 4, end_item)

            self.source_table.setRowCount(len(source_list))
            for row, source in enumerate(source_list):
                source_item = QTableWidgetItem(source.name)
                source_item.setData(Qt.ItemDataRole.UserRole, source.id)
                self.source_table.setItem(row, 0, source_item)
                start_editor = TimelineSpinBox()
                start_editor.setValue(source.timeline_start)
                start_editor.valueChanged.connect(
                    lambda value, identifier=source.id: self._on_source_timing_changed(
                        identifier, "timeline_start", value
                    )
                )
                duration_editor = TimelineSpinBox()
                duration_editor.setSpecialValueText("곡 끝까지" if self.translator.is_korean else "Playlist end")
                duration_editor.setValue(source.timeline_duration)
                duration_editor.valueChanged.connect(
                    lambda value, identifier=source.id: self._on_source_timing_changed(
                        identifier, "timeline_duration", value
                    )
                )
                self.source_table.setCellWidget(row, 1, start_editor)
                self.source_table.setCellWidget(row, 2, duration_editor)
            total = max((end for _track, _start, end in timeline_tracks), default=0.0)
            total_label = TimelineSpinBox.format_timecode(total)
            self.summary.setText(
                f"{len(timeline_tracks)}곡 · 총 {total_label}"
                if self.translator.is_korean else
                f"{len(timeline_tracks)} tracks · {total_label}"
            )
            self.visual.set_content(timeline_tracks, source_list)
            self.position_editor.setMaximum(self.visual.duration)
            self.preview_button.setEnabled(bool(timeline_tracks))
            self._restore_row_selection(
                self.track_table, 0, selected_track_id,
            )
            self._restore_row_selection(
                self.source_table, 0, selected_source_id,
            )
            self.track_table.setVisible(bool(timeline_tracks))
            self.track_empty_label.setVisible(not timeline_tracks)
            self._update_move_buttons()
            # ``self`` as context: the timer is dropped if the panel is deleted first.
            QTimer.singleShot(
                0, self,
                lambda value=track_scroll:
                    self.track_table.verticalScrollBar().setValue(value),
            )
            QTimer.singleShot(
                0, self,
                lambda value=source_scroll:
                    self.source_table.verticalScrollBar().setValue(value),
            )
        finally:
            self._refreshing = False

    def schedule_refresh(self) -> None:
        """Coalesce store signals so Undo does not rebuild tables repeatedly."""
        if self._refresh_pending:
            return
        self._refresh_pending = True

        def perform_refresh() -> None:
            self._refresh_pending = False
            self.refresh()

        QTimer.singleShot(0, self, perform_refresh)

    def _selected_track_id(self) -> str | None:
        row = self.track_table.currentRow()
        item = self.track_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _selected_source_id(self) -> str | None:
        row = self.source_table.currentRow()
        item = self.source_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    @staticmethod
    def _restore_row_selection(
        table: QTableWidget, id_column: int, identifier: str | None,
    ) -> None:
        if not identifier:
            return
        for row in range(table.rowCount()):
            item = table.item(row, id_column)
            if item is not None and str(
                item.data(Qt.ItemDataRole.UserRole)
            ) == identifier:
                table.selectRow(row)
                table.setCurrentCell(row, id_column)
                return

    def _update_move_buttons(self) -> None:
        """Reordering needs a selected track with room to move, on the music tab."""
        on_tracks = self.tabs.currentIndex() == 0
        row = self.track_table.currentRow() if self.track_table.selectedItems() else -1
        count = self.track_table.rowCount()
        self.up_button.setVisible(on_tracks and self.view_combo.currentIndex() == 1)
        self.down_button.setVisible(on_tracks and self.view_combo.currentIndex() == 1)
        self.up_button.setEnabled(on_tracks and row > 0)
        self.down_button.setEnabled(on_tracks and 0 <= row < count - 1)

    def _move_selected_track(self, direction: int) -> None:
        track_id = self._selected_track_id()
        if track_id:
            self.playlist.move_track(track_id, direction)

    def _on_track_start_changed(self, track_id: str, value: float) -> None:
        """Commit an edited track start without rebuilding table widgets mid-signal."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self.playlist.set_start_time(track_id, value)
        finally:
            self._refreshing = False
        self.schedule_refresh()

    def _on_source_timing_changed(self, source_id: str, field: str, value: float) -> None:
        """Commit one source timing field without destroying the active spin box."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self.sources.update(source_id, **{field: value})
        finally:
            self._refreshing = False
        self.schedule_refresh()

    def _select_source_on_canvas(self) -> None:
        """Select a source in Canvas and Inspector from its timeline row."""
        if self._refreshing:
            return
        row = self.source_table.currentRow()
        item = self.source_table.item(row, 0) if row >= 0 else None
        if item:
            self.sources.select(item.data(Qt.ItemDataRole.UserRole))

    def _change_view(self, index: int) -> None:
        self.visual.cancel_drag()
        self.view_stack.setCurrentIndex(index)
        self.setMinimumHeight(max(340, self.keyframe_editor.minimumSizeHint().height() + 70)
                              if index == 2 else 250)
        self.visual_toolbar.setVisible(index == 0)
        self.visual_hint.setVisible(index == 0)
        self.up_button.setVisible(index == 1 and self.tabs.currentIndex() == 0)
        self.down_button.setVisible(index == 1 and self.tabs.currentIndex() == 0)

    def _default_visual_hint(self) -> None:
        self.visual_hint.setText("막대 이동 · 요소 양끝으로 길이 조절 · Ctrl+휠 확대 · Shift+휠 가로 이동 · Esc 취소"
                                 if self.translator.is_korean else
                                 "Drag to move · Source edges resize · Ctrl+wheel zooms · Shift+wheel pans · Esc cancels")

    def _show_position(self, value: float) -> None:
        self.position_editor.blockSignals(True)
        self.position_editor.setMaximum(max(self.visual.duration, value))
        self.position_editor.setValue(value)
        self.position_editor.blockSignals(False)

    def _follow_keyframe_position(self, value: float) -> None:
        # The legacy exposure view may be shorter than a tempo-adjusted mix.
        # Its visual clamp must not seek the keyframe editor back in time.
        with QSignalBlocker(self.visual):
            self.visual.set_playhead(value)
        self._show_position(value)

    def _select_visual_block(self, kind: str, identifier: str) -> None:
        table = self.track_table if kind == "track" else self.source_table
        self._refreshing = True
        try:
            self._restore_row_selection(table, 0, identifier)
        finally:
            self._refreshing = False
        if kind == "source":
            self.sources.select(identifier)
        self._update_move_buttons()

    def _follow_source_selection(self, source: Source | None) -> None:
        if source is not None:
            self.visual.set_selected("source", source.id)
            self._refreshing = True
            try:
                self._restore_row_selection(self.source_table, 0, source.id)
            finally:
                self._refreshing = False

    def _commit_visual_timing(self, kind: str, identifier: str, start: float, duration: float) -> None:
        # Drafts live only in the view: one release produces one undoable model edit.
        if kind == "track":
            self._on_track_start_changed(identifier, start)
        else:
            source = self.sources.get(identifier)
            if source is not None and not source.locked:
                self.sources.update(identifier, timeline_start=start, timeline_duration=duration)

    def _reset_visual_timing(self, kind: str, identifier: str) -> None:
        if kind == "track":
            self.playlist.set_start_time(identifier, None)
        else:
            source = self.sources.get(identifier)
            if source is not None and not source.locked:
                self.sources.update(identifier, timeline_duration=0.0)

    def _edit_numerically(self, kind: str, identifier: str) -> None:
        self.view_combo.setCurrentIndex(1)
        self.tabs.setCurrentIndex(0 if kind == "track" else 1)
        self._select_visual_block(kind, identifier)
