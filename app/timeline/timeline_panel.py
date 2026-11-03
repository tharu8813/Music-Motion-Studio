"""Visual timeline editing with optional precise timing drafts."""
from __future__ import annotations
from PySide6.QtCore import QSignalBlocker, QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QStyle, QVBoxLayout
from app.controllers.keyframe_controller import KeyframeController
from app.models.source import Source
from app.services.playlist_service import PlaylistService
from app.services.source_store import SourceStore
from app.timeline.visual_timeline import VisualTimeline, clock
from app.timeline.precision_time_dialog import PrecisionTimeDialog
from app.utils.i18n import Translator
from app.utils.time_format import format_clock


class TimelinePanel(QFrame):
    preview_requested = Signal(float)
    edit_started = Signal()
    edit_finished = Signal()

    def __init__(self, playlist: PlaylistService, sources: SourceStore, translator: Translator, parent=None):
        super().__init__(parent)
        self.setObjectName("timelineStrip")
        self.playlist, self.sources, self.translator = playlist, sources, translator
        self._refresh_pending = False
        self._shown = None
        self.setMinimumHeight(250)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(8)
        header = QHBoxLayout()
        self.title, self.summary = QLabel(), QLabel()
        self.title.setObjectName("panelTitle")
        self.summary.setObjectName("mutedLabel")
        self.position_label = QLabel()
        self.position_label.setObjectName("motionClock")
        self.position_label.setMinimumWidth(92)
        self.position_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview_button = QPushButton()
        self.up_button, self.down_button = QPushButton("↑"), QPushButton("↓")
        header.addWidget(self.title)
        header.addWidget(self.summary)
        header.addStretch()
        for widget in (self.up_button, self.down_button, self.position_label, self.preview_button):
            header.addWidget(widget)
        layout.addLayout(header)
        self.visual = VisualTimeline()
        self.keyframe_controller = KeyframeController(sources, playlist.timeline_tracks, self)
        self.visual.position_changed.connect(self.keyframe_controller.seek_global)
        self.keyframe_controller.position_changed.connect(self._follow_keyframe_position)
        playlist.playlist_changed.connect(self.keyframe_controller.changed.emit)
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.snap_button, self.fit_button = QPushButton(), QPushButton()
        self.snap_button.setCheckable(True)
        self.snap_button.setChecked(True)
        self.zoom_out, self.zoom_in = QPushButton("−"), QPushButton("+")
        self.zoom_label = QLabel("100%")
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setObjectName("mutedLabel")
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.focus_button, self.start_button, self.end_button, self.reset_button = (QPushButton() for _ in range(4))
        self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipBackward))
        self.end_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipForward))
        for widget in (self.snap_button, self.fit_button, self.zoom_out, self.zoom_label, self.zoom_in,
                       self.focus_button, self.start_button, self.end_button, self.reset_button):
            tools.addWidget(widget)
        tools.addStretch()
        for button in (self.snap_button, self.fit_button, self.zoom_out, self.zoom_in, self.focus_button,
                       self.start_button, self.end_button, self.reset_button, self.up_button, self.down_button, self.preview_button):
            button.setObjectName("timelineToolButton")
        layout.addLayout(tools)
        layout.addWidget(self.visual, 1)
        self.visual_hint = QLabel()
        self.visual_hint.setObjectName("timelineHint")
        self.visual_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.visual_hint)
        self.snap_button.toggled.connect(lambda enabled: setattr(self.visual, "snapping", enabled))
        self.fit_button.clicked.connect(self.visual.fit_all)
        self.focus_button.clicked.connect(self.visual.focus_selected)
        self.start_button.clicked.connect(lambda: self.visual.seek_selected(False))
        self.end_button.clicked.connect(lambda: self.visual.seek_selected(True))
        self.reset_button.clicked.connect(self._reset_selected)
        self.zoom_out.clicked.connect(lambda: self.visual.zoom(.8))
        self.zoom_in.clicked.connect(lambda: self.visual.zoom(1.25))
        self.visual.zoom_changed.connect(lambda percent: self.zoom_label.setText(f"{percent}%"))
        self.visual.position_changed.connect(self._show_position)
        self.visual.preview_requested.connect(lambda: self.preview_button.click())
        self.preview_button.clicked.connect(lambda: self.preview_requested.emit(self.visual.playhead))
        self.visual.hint_changed.connect(self.visual_hint.setText)
        self.visual.selection_changed.connect(self._select_visual_block)
        self.visual.timing_committed.connect(self._commit_visual_timing)
        self.visual.precision_requested.connect(self._edit_precise_timing)
        self.visual.reset_requested.connect(self._reset_visual_timing)
        self.visual.reorder_requested.connect(self.playlist.move_track)
        self.up_button.clicked.connect(lambda: self._move_selected_track(-1))
        self.down_button.clicked.connect(lambda: self._move_selected_track(1))
        playlist.playlist_changed.connect(self.schedule_refresh)
        sources.source_added.connect(lambda _source: self.schedule_refresh())
        sources.source_removed.connect(lambda _source_id: self.schedule_refresh())
        sources.source_changed.connect(lambda _source: self.schedule_refresh())
        sources.sources_replaced.connect(self.schedule_refresh)
        sources.selection_changed.connect(self._follow_source_selection)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()
        self._follow_source_selection(sources.selected)

    def retranslate(self):
        korean = self.translator.is_korean
        self.title.setText("타임라인" if korean else "Timeline")
        self.visual.korean = korean
        self.snap_button.setText("스냅" if korean else "Snap")
        self.snap_button.setToolTip("시작·끝·재생 위치에 맞춥니다. Shift로 스냅을 임시 반전합니다." if korean else "Snap to clip edges and playhead. Hold Shift to invert snapping.")
        self.fit_button.setText("전체 보기" if korean else "Fit all")
        self.fit_button.setToolTip("전체 곡과 요소 보기 (F)" if korean else "Fit all tracks and sources (F)")
        self.focus_button.setText("선택 확대" if korean else "Focus selection")
        self.focus_button.setToolTip("선택 구간을 크게 보기 · 막대 두 번 클릭" if korean else "Fit the selected range · double-click a block")
        self.zoom_out.setToolTip("축소 · Ctrl+휠 / −" if korean else "Zoom out · Ctrl+wheel / −")
        self.zoom_in.setToolTip("확대 · Ctrl+휠 / +" if korean else "Zoom in · Ctrl+wheel / +")
        self.start_button.setToolTip("선택 시작으로 이동 ([)" if korean else "Seek selection start ([)")
        self.end_button.setToolTip("선택 끝으로 이동 (])" if korean else "Seek selection end (])")
        self.preview_button.setText("이 위치 미리보기" if korean else "Preview here")
        self.preview_button.setToolTip("현재 위치에서 재생 미리보기 (Enter)" if korean else "Open Preview at the playhead (Enter)")
        self.position_label.setAccessibleName("재생 위치 (읽기 전용)" if korean else "Playhead position (read only)")
        self.up_button.setToolTip("이전 순서로 · Ctrl+↑" if korean else "Move track earlier · Ctrl+Up")
        self.down_button.setToolTip("다음 순서로 · Ctrl+↓" if korean else "Move track later · Ctrl+Down")
        self.visual.setAccessibleName("시각 타임라인 편집기" if korean else "Visual timeline editor")
        self.visual.setAccessibleDescription("막대 이동 · 양끝 손잡이로 길이 조절 · 레이어 이름으로 선택 · 두 번 클릭해 확대" if korean else "Drag to move · Edge handles resize · Click lane labels to select · Double-click to focus")
        self.visual_hint.setText("막대 이동 · 양끝 길이 조절 · F2/우클릭: 시간 정밀 편집 · 두 번 클릭: 확대 · Esc 취소" if korean else "Drag to move · Resize edges · F2/right-click: precise timing · Double-click: focus · Esc: cancel")
        for button in (self.start_button, self.end_button, self.up_button, self.down_button):
            button.setAccessibleName(button.toolTip())
        self._shown = None
        self.refresh()

    def refresh(self):
        tracks, sources = self.playlist.timeline_tracks(), self.sources.sources()
        shown = (self.translator.is_korean,
            tuple((track.id, track.title, track.artist, start, end) for track, start, end in tracks),
            tuple((s.id, s.name, s.timeline_start, s.timeline_duration, s.locked, s.visible, s.source_type, s.z_index) for s in sources))
        if shown != self._shown:
            self._shown = shown
            self.visual.set_content(tracks, sources)
            total = max((end for _track, _start, end in tracks), default=0)
            self.summary.setText(f"{len(tracks)}곡 · 총 {format_clock(round(total))}" if self.translator.is_korean else f"{len(tracks)} tracks · {format_clock(round(total))}")
        self.preview_button.setEnabled(bool(tracks))
        self._show_position(self.visual.playhead)
        self._update_selection_tools()

    def schedule_refresh(self):
        if self._refresh_pending:
            return
        self._refresh_pending = True
        def perform():
            self._refresh_pending = False
            self.refresh()
        QTimer.singleShot(0, self, perform)

    def _show_position(self, value):
        self.position_label.setText(clock(value))

    def _follow_keyframe_position(self, value):
        with QSignalBlocker(self.visual):
            self.visual.set_playhead(value)
        self._show_position(value)

    def _update_selection_tools(self):
        block = self.visual.selected_block()
        for button in (self.focus_button, self.start_button, self.end_button):
            button.setEnabled(block is not None)
        self.reset_button.setEnabled(block is not None and not block.locked)
        track = block is not None and block.kind == "track"
        self.reset_button.setText(("자동 이어붙이기" if track else "곡 끝까지 표시") if self.translator.is_korean else ("Sequence automatically" if track else "Show to playlist end"))
        ids = [item.id for item in self.playlist.tracks]
        index = ids.index(block.identifier) if track and block.identifier in ids else -1
        self.up_button.setVisible(track)
        self.down_button.setVisible(track)
        self.up_button.setEnabled(index > 0)
        self.down_button.setEnabled(0 <= index < len(ids)-1)

    def _select_visual_block(self, kind, identifier):
        self.visual.set_selected(kind, identifier)
        if kind == "source":
            self.sources.select(identifier)
        self._update_selection_tools()

    def _follow_source_selection(self, source: Source | None):
        if source is not None:
            self.visual.set_selected("source", source.id)
        self._update_selection_tools()

    def _move_selected_track(self, direction):
        block = self.visual.selected_block()
        if block is not None and block.kind == "track":
            self.playlist.move_track(block.identifier, direction)

    def _reset_selected(self):
        block = self.visual.selected_block()
        if block is not None:
            self._reset_visual_timing(block.kind, block.identifier)

    def _commit_visual_timing(self, kind: str, identifier: str, start: float, duration: float) -> None:
        # Drafts live only in the view: one release produces one undoable model edit.
        if kind == "track":
            current = next((s for t, s, _e in self.playlist.timeline_tracks() if t.id == identifier), None)
            if current is None or current == start:
                return
            self.edit_started.emit()
            self.playlist.set_start_time(identifier, start)
        else:
            source = self.sources.get(identifier)
            if source is None or source.locked or (source.timeline_start, source.timeline_duration) == (start, duration):
                return
            self.edit_started.emit()
            self.sources.update(identifier, timeline_start=start, timeline_duration=duration)
        self.edit_finished.emit()
        self.refresh()

    def _edit_precise_timing(self, kind, identifier):
        self.refresh()
        block = next((b for b in self.visual.blocks if (b.kind, b.identifier) == (kind, identifier)), None)
        if block is None or block.locked:
            return
        source = self.sources.get(identifier) if kind == "source" else None
        track = next((t for t, _s, _e in self.playlist.timeline_tracks() if t.id == identifier), None)
        dialog = PrecisionTimeDialog(block.name, block.start,
            source.timeline_duration if source is not None else track.duration_seconds,
            track=kind == "track", full=block.full,
            minimum=self.playlist.minimum_start_time(identifier) if kind == "track" else 0,
            playlist_end=max((end for _t, _s, end in self.playlist.timeline_tracks()), default=0),
            korean=self.translator.is_korean, parent=self)
        dialog.adjustSize()
        rect, viewport = self.visual.block_rect(block), self.visual.viewport()
        anchor = viewport.mapToGlobal(rect.bottomLeft().toPoint())
        screen = self.screen().availableGeometry()
        anchor.setX(max(screen.left(), min(anchor.x(), screen.right()-dialog.width()+1)))
        anchor.setY(max(screen.top(), min(anchor.y(), screen.bottom()-dialog.height()+1)))
        dialog.move(anchor)
        try:
            # The screen-clamped popup owns its draft and closes on Escape or
            # outside click. Focus changes never touch project history.
            if dialog.exec() == dialog.DialogCode.Accepted:
                start, duration = dialog.timing
                horizontal = self.visual.horizontalScrollBar().value()
                vertical = self.visual.verticalScrollBar().value()
                self._commit_visual_timing(kind, identifier, start, duration)
                self.visual.horizontalScrollBar().setValue(horizontal)
                self.visual.verticalScrollBar().setValue(vertical)
        finally:
            dialog.deleteLater()
            self.visual.setFocus()

    def _reset_visual_timing(self, kind: str, identifier: str) -> None:
        if kind == "track":
            self.playlist.set_start_time(identifier, None)
        else:
            source = self.sources.get(identifier)
            if source is not None and not source.locked:
                self.sources.update(identifier, timeline_duration=0.0)
