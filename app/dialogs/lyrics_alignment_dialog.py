"""Choose physical lines and review acoustic timings before applying them."""

from copy import deepcopy
import threading

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QColorDialog, QFormLayout, QGroupBox, QPlainTextEdit, QProgressBar, QPushButton,
    QDoubleSpinBox, QSizePolicy, QSpacerItem, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from app.services.lyrics_alignment_service import (
    AlignmentCancelled, AlignmentResult, LyricsAlignmentService, cue_lines, line_words,
)
from app.services.lyrics_service import LyricsService
from app.models.source import Source, SourceType
from app.inspector.editors.lyrics_editor import LyricsSection
from app.canvas.renderers.lyrics_renderer import pending_color


class _AlignmentWorker(QThread):
    progress = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, track, ffmpeg, line_positions, parent, *, use_cache=True):
        super().__init__(parent)
        self.track, self.ffmpeg = deepcopy(track), ffmpeg
        self.line_positions = line_positions
        self.use_cache = use_cache
        self.cancel_event = threading.Event()

    def run(self):
        try:
            result = LyricsAlignmentService().analyze(
                self.track, self.ffmpeg, self.cancel_event, self.progress.emit, self.line_positions,
                use_cache=self.use_cache)
        except AlignmentCancelled:
            return
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.succeeded.emit(result)


class LyricsAlignmentDialog(QDialog):
    def __init__(self, track, ffmpeg, korean, parent=None, *, source=None):
        super().__init__(parent)
        self.korean = korean
        self.track, self.ffmpeg = deepcopy(track), ffmpeg
        self.result_data = None
        self._analysis_result = None
        self.worker = None
        self.review_rows = []
        self.error = ""
        self.display_source = deepcopy(source) if source else Source(SourceType.LYRICS, "Lyrics")
        self.display_source.subtitle_karaoke_enabled = True
        self.setWindowTitle(self._tr("자동 노래방 (베타)", "Automatic karaoke (Beta)") + " · " + track.title)
        self.resize(760, 600)
        layout = QVBoxLayout(self)
        self.label = QLabel(self._tr(
            "노래방 가사는 베타 버전 기능입니다.\n"
            "줄 시간으로 바로 적용하면 다운로드 없이 안정적으로 표시합니다. 오디오 분석은 선택 사항입니다.\n"
            "원문인 1번째 줄을 기본 분석합니다. 번역 줄도 실제로 부를 때만 추가로 선택하세요.",
            "Karaoke lyrics are a beta feature.\n"
            "Apply cue times for stable display without downloads. Audio analysis is optional.\n"
            "Line 1 is selected by default. Add a translation line only if it is also sung."))
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.label)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([self._tr("분석", "Analyze"), self._tr("줄 위치", "Line"), self._tr("가사 예시", "Example lyric")])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        examples = {}
        for cue in self.track.lyrics:
            for position, _offset, text in cue_lines(cue):
                if position < 12:
                    examples.setdefault(position, text)
        for position, text in sorted(examples.items()):
            row = self.table.rowCount()
            self.table.insertRow(row)
            choice = QTableWidgetItem()
            choice.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            choice.setCheckState(Qt.CheckState.Checked if position == 0 else Qt.CheckState.Unchecked)
            choice.setData(Qt.ItemDataRole.UserRole, position)
            self.table.setItem(row, 0, choice)
            self.table.setItem(row, 1, QTableWidgetItem(self._tr(f"{position + 1}번째 줄", f"Line {position + 1}")))
            self.table.setItem(row, 2, QTableWidgetItem(text))
        layout.addWidget(self.table, 1)
        self.table.setMaximumHeight(120)
        self.display_group = QGroupBox(self._tr("노래방 설정 (베타)", "Karaoke settings (Beta)"))
        self.display_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        form = QFormLayout(self.display_group)
        def spin(a, b, step):
            widget = QDoubleSpinBox()
            widget.setRange(a, b)
            widget.setSingleStep(step)
            return widget
        self.colors = LyricsSection(spin, QPushButton)
        self.colors.retranslate(korean)
        self.color_keys = ("subtitle_karaoke_offset",
                           "subtitle_accent_color", "subtitle_karaoke_pending_mode",
                           "subtitle_karaoke_pending_color", "subtitle_karaoke_pending_brightness")
        self.colors.fill(self.display_source, set_color=self._color_button)
        brightness = self.colors.widgets["subtitle_karaoke_pending_brightness"]
        brightness.setRange(0, 100)
        brightness.setDecimals(0)
        brightness.setSingleStep(5)
        brightness.setSuffix("%")
        brightness.setValue(self.display_source.subtitle_karaoke_pending_brightness * 100)
        for key in self.color_keys:
            widget = self.colors.widgets[key]
            widget.setToolTip(self.colors.HELP[key.removeprefix("subtitle_")][0 if korean else 1])
            form.addRow(self.colors.LABELS[key][0 if korean else 1], widget)
            if self.colors.kind(widget) == "color":
                widget.clicked.connect(lambda _checked=False, key=key: self._choose_color(key))
            elif self.colors.kind(widget) == "combo":
                widget.currentIndexChanged.connect(lambda _index, key=key: self._display_changed(key))
            else:
                widget.valueChanged.connect(lambda _value, key=key: self._display_changed(key))
        self.color_preview = QLabel()
        form.addRow(self.color_preview)
        layout.addWidget(self.display_group)
        self._refresh_colors()
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(140)
        self.detail.hide()
        layout.addWidget(self.detail)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.hide()
        layout.addWidget(self.bar)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self._tr("취소", "Cancel"))
        self.buttons.rejected.connect(self.reject)
        self.start_button = self.buttons.addButton(self._tr("분석 시작", "Start analysis"), QDialogButtonBox.ButtonRole.ActionRole)
        self.start_button.setToolTip(self._tr(
            "기존 단어 시간도 다시 분석합니다. 체크한 결과를 적용할 때만 교체하며, 취소하면 기존 시간을 유지합니다.",
            "Reanalyzes existing word times too. Only checked results replace them on Apply; Cancel keeps existing times."))
        self.start_button.clicked.connect(self._start)
        self.simple_button = self.buttons.addButton(self._tr("줄 시간으로 바로 적용", "Apply cue times"), QDialogButtonBox.ButtonRole.ActionRole)
        self.simple_button.setDefault(True)
        self.simple_button.setToolTip(self._tr("오디오 인식 없이 기존 자막 시간과 단어 길이로 추정합니다. 가사 색상과 표시만 빠르게 설정할 때 사용하세요.", "Estimate from cue duration and word length without recognition. Quickly apply colors and karaoke display."))
        self.simple_button.clicked.connect(self._use_cue_times)
        self.apply_button = self.buttons.addButton(self._tr("선택한 결과 적용", "Apply selected results"), QDialogButtonBox.ButtonRole.ActionRole)
        self.apply_button.hide()
        self.apply_button.clicked.connect(self._apply)
        self.listen_button = self.buttons.addButton(self._tr("선택 구간 듣기", "Listen to selected cue"), QDialogButtonBox.ButtonRole.ActionRole)
        self.listen_button.hide()
        self.listen_button.clicked.connect(self._listen)
        self.preflight_space = QSpacerItem(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        layout.addItem(self.preflight_space)
        layout.addWidget(self.buttons)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self._listen_end = 0
        self._listen_start = 0
        self.player.positionChanged.connect(self._position_changed)
        self.player.mediaStatusChanged.connect(self._media_status_changed)
        self.table.itemChanged.connect(self._selection_changed)
        self.table.currentCellChanged.connect(self._show_detail)
        self._selection_changed()

    def _tr(self, korean, english):
        return korean if self.korean else english

    @staticmethod
    def _color_button(button, value):
        button.setText(value)
        button.setStyleSheet(f"border: 2px solid {value};")

    def _choose_color(self, key):
        from PySide6.QtGui import QColor
        color = QColorDialog.getColor(QColor(getattr(self.display_source, key)), self)
        if color.isValid():
            setattr(self.display_source, key, color.name())
            self._color_button(self.colors.widgets[key], color.name())
            self._refresh_colors()

    def _display_changed(self, key):
        widget = self.colors.widgets[key]
        value = widget.currentData() if self.colors.kind(widget) == "combo" else widget.value()
        if key == "subtitle_karaoke_pending_brightness":
            value /= 100
        setattr(self.display_source, key, value)
        self._refresh_colors()

    def _refresh_colors(self):
        form = self.display_group.layout()
        for key in self.color_keys:
            visible = self.colors.hidden_when_off(self.display_source).get(key, True)
            widget = self.colors.widgets[key]
            widget.setVisible(visible)
            form.labelForField(widget).setVisible(visible)
        reached, pending = self.display_source.subtitle_accent_color, pending_color(self.display_source).name()
        self.color_preview.setText(self._tr(
            f'<span style="color:{reached}">부른 가사</span>　<span style="color:{pending}">아직 부르지 않은 가사</span>',
            f'<span style="color:{reached}">Reached words</span>　<span style="color:{pending}">Pending words</span>'))

    def karaoke_settings(self):
        settings = {key: getattr(self.display_source, key) for key in ("subtitle_karaoke_enabled", *self.color_keys)}
        positions = self.line_positions if hasattr(self, "line_positions") else tuple(
            self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked)
        settings["subtitle_karaoke_lines"] = list(positions)
        return settings

    def _use_cue_times(self):
        if self.worker is not None and self.worker.isRunning():
            return
        if not hasattr(self, "line_positions") and not any(
                self.table.item(row, 0).checkState() == Qt.CheckState.Checked
                for row in range(self.table.rowCount())):
            return
        self.result_data = AlignmentResult(deepcopy(self.track.lyrics), 0, 0)
        self.accept()

    def _selection_changed(self, *_args):
        count = sum(self.table.item(row, 0).checkState() == Qt.CheckState.Checked for row in range(self.table.rowCount()))
        self.start_button.setEnabled(count > 0 or (self.worker is not None and not self.worker.isRunning()))
        self.simple_button.setEnabled(count > 0 or self.worker is not None)
        self.apply_button.setEnabled(count > 0)
        self.apply_button.setText(self._tr(f"선택한 {count}줄 적용", f"Apply {count} selected lines"))

    def _start(self):
        self.player.stop()
        if self.worker is None:
            self.line_positions = tuple(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                                        for row in range(self.table.rowCount())
                                        if self.table.item(row, 0).checkState() == Qt.CheckState.Checked)
        if not self.line_positions or (self.worker is not None and self.worker.isRunning()):
            return
        use_cache = self.worker is None and not any(cue.get("words") for cue in self.track.lyrics)
        if self.worker is not None:
            self.worker.deleteLater()
        self.review_rows.clear()
        self._analysis_result = self.result_data = None
        self.error = ""
        self.apply_button.hide()
        self.listen_button.hide()
        self.simple_button.hide()
        self.detail.hide()
        self.display_group.hide()
        self.table.setEnabled(False)
        self.start_button.hide()
        self.bar.show()
        self.worker = _AlignmentWorker(self.track, self.ffmpeg, self.line_positions, self,
                                       use_cache=use_cache)
        self.worker.progress.connect(self._progress)
        self.worker.succeeded.connect(self._succeeded)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._finished)
        self._progress(0, "recognize")
        self.worker.start()

    def _progress(self, value, stage):
        if self.worker.cancel_event.is_set():
            return
        self.bar.setValue(round(value * 100))
        self.label.setText((
            "최초 1회 인식 도구 다운로드 중 · 약 36 MB\n오디오는 외부로 전송하지 않습니다."
            if self.korean else "First-use recognition download · about 36 MB\nAudio stays on this PC."
        ) if stage == "download" else (
            "선택한 줄의 단어 시간을 분석 중입니다. 완료되면 줄별 결과를 확인하고 적용할 수 있습니다."
            if self.korean else "Analyzing selected lines. Review each result before applying it."
        ))

    def _succeeded(self, result):
        self._analysis_result = result

    def _failed(self, error):
        self.error = error

    def _finished(self):
        self.worker.wait()  # finished has already fired; ensure native thread cleanup.
        if self.worker.cancel_event.is_set():
            self.done(QDialog.DialogCode.Rejected)
            return
        if self._analysis_result is None:
            self._analysis_result = AlignmentResult(deepcopy(self.track.lyrics), 0, sum(
                position in self.line_positions for cue in self.track.lyrics
                for position, _offset, _text in cue_lines(cue)))
        self.bar.hide()
        self.preflight_space.changeSize(0, 0, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)
        self.layout().invalidate()
        self.table.setMaximumHeight(16777215)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels([self._tr("적용", "Apply"), self._tr("시간 · 줄 위치", "Time · Line"),
                                              self._tr("가사", "Lyric"), self._tr("분석 결과", "Result")])
        for index, original in enumerate(self.track.lyrics):
            for position, offset, text in cue_lines(original):
                if position not in self.line_positions:
                    continue
                existing = line_words(original, offset, text)
                candidate = line_words(self._analysis_result.cues[index], offset, text)
                if candidate == existing:
                    candidate = []
                self.review_rows.append((index, offset, text, candidate))
                row = self.table.rowCount()
                self.table.insertRow(row)
                choice = QTableWidgetItem()
                choice.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                                | Qt.ItemFlag.ItemIsUserCheckable)
                choice.setCheckState(Qt.CheckState.Checked)
                self.table.setItem(row, 0, choice)
                timestamp = LyricsService.lrc_timestamp(float(original["start"]))
                self.table.setItem(row, 1, QTableWidgetItem(self._tr(f"{timestamp} · {position + 1}번째 줄", f"{timestamp} · Line {position + 1}")))
                self.table.setItem(row, 2, QTableWidgetItem(text))
                status = (self._tr(f"{len(candidate)}/{len(text.split())}단어 맞춤", f"{len(candidate)}/{len(text.split())} words aligned") if candidate else
                          self._tr("기존 단어 시간 유지", "Existing word times retained") if existing else
                          self._tr("줄 시간으로 추정", "Estimated from cue times"))
                status_item = QTableWidgetItem(status)
                status_item.setToolTip(self._tr(
                    "기존 줄 시간은 유지합니다. 다시 분석한 단어 시간은 체크한 줄만 교체합니다. 인식하지 못한 단어는 주변 시간으로 추정합니다. "
                    "24초를 넘거나 오디오 범위 밖인 구간도 줄 시간으로 표시합니다.",
                    "Cue times are preserved. Reanalyzed word times replace checked lines only. Missing words use surrounding times. "
                    "Intervals longer than 24 seconds or outside the audio also use cue times."))
                self.table.setItem(row, 3, status_item)
        self.table.blockSignals(False)
        self.table.setEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.label.setText(self._tr(
            f"{self._analysis_result.total}줄 중 {self._analysis_result.matched}줄 인식. 아직 적용하지 않았습니다.\n"
            "줄을 선택하면 단어 시간을 보고 구간을 들을 수 있습니다. 체크한 결과만 반영합니다. 취소하면 모두 버립니다. "
            "적용하면 색상과 노래방 표시까지 함께 반영합니다.",
            f"Matched {self._analysis_result.matched} of {self._analysis_result.total} lines. Nothing applied yet.\n"
            "Select a row to inspect word times and listen. Only checked results will be used; Cancel discards them. "
            "Applying also sets colors and enables karaoke display."))
        if not self._analysis_result.matched:
            self.label.setText(self._tr(
                "인식 결과가 없어 기존 줄 시간으로 표시합니다. 바로 적용하거나 다시 분석할 수 있습니다.\n"
                "구간 듣기로 줄 시간이 맞는지 확인하세요.",
                "No matching words; display will use existing cue times. Apply now or retry analysis.\n"
                "Listen to check cue timing."))
        self.display_group.show()
        self.detail.show()
        self.apply_button.show()
        self.listen_button.show()
        self.simple_button.show()
        self.start_button.setText(self._tr("다시 분석", "Analyze again"))
        self.start_button.show()
        self.apply_button.setDefault(True)
        self._selection_changed()
        if self.review_rows:
            self.table.selectRow(0)

    def _show_detail(self, *_args):
        row = self.table.currentRow()
        if not self.review_rows or not 0 <= row < len(self.review_rows):
            return
        self.player.stop()
        index, offset, text, candidate = self.review_rows[row]
        words = candidate or line_words(self.track.lyrics[index], offset, text)
        timing = "\n".join(f"{word['start']:.2f}–{word['end']:.2f} s  {word['text']}" for word in words)
        transcript = self._analysis_result.transcript if self._analysis_result else ""
        original = self.track.lyrics[index]
        correction = self._tr("\n줄 시간: ", "\nCue time: ") + f"{original['start']:.2f}–{original['end']:.2f} s"
        self.detail.setPlainText(text + correction + "\n\n" + (timing or self._tr("단어 시간이 없어 기존 줄 타이밍을 유지합니다.", "No word times; existing cue timing is retained."))
                                 + (self._tr("\n\n실제 인식 내용:\n", "\n\nActual transcript:\n") + transcript if transcript else "")
                                 + (self._tr("\n\n분석 오류:\n", "\n\nAnalysis error:\n") + self.error if self.error else ""))

    def _listen(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self.review_rows):
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.stop()
            return
        cue = self._analysis_result.cues[self.review_rows[row][0]]
        self._listen_end = max(0, round((float(cue["end"]) - self.track.lyrics_timing_offset_seconds) * 1000))
        self._listen_start = max(0, round((float(cue["start"]) - self.track.lyrics_timing_offset_seconds) * 1000))
        source = QUrl.fromLocalFile(self.track.file_path)
        if self.player.source() != source:
            self.player.setSource(source)
        self.player.setPosition(self._listen_start)
        self.player.play()

    def _media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.player.setPosition(self._listen_start)

    def _position_changed(self, position):
        if self._listen_end and position >= self._listen_end:
            self.player.stop()

    def _apply(self):
        if self._analysis_result is None:
            return
        cues, count = deepcopy(self.track.lyrics), 0
        selected = False
        for row, (index, _offset, _text, candidate) in enumerate(self.review_rows):
            if self.table.item(row, 0).checkState() != Qt.CheckState.Checked:
                continue
            selected = True
            if candidate:
                existing = line_words(cues[index], _offset, _text)
                cues[index]["words"] = sorted([word for word in cues[index].get("words", []) if word not in existing]
                                              + deepcopy(candidate), key=lambda word: word["offset"])
                count += 1
        if selected:
            self.result_data = AlignmentResult(cues, count, self._analysis_result.total)
            self.accept()

    def done(self, result):
        self.player.stop()
        super().done(result)

    def reject(self):
        if self.worker is not None:
            self.worker.cancel_event.set()
        if self.worker is not None and self.worker.isRunning():
            self.buttons.setEnabled(False)
            self.label.setText("취소 중…" if self.korean else "Cancelling…")
        else:
            super().reject()
