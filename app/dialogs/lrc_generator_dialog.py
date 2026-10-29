"""Interactive, audio-assisted LRC lyric timing generator."""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path
from copy import deepcopy
import re

from PySide6.QtCore import QSize, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QKeyEvent, QKeySequence, QPalette, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.models.project import ProjectContent
from app.models.playlist import PlaylistTrack
from app.dialogs.help_dialog import install_help_shortcut
from app.dialogs.lrc_shortcuts_dialog import LrcShortcutsDialog
from app.dialogs.ttml_edit_dialog import TtmlRowsDialog, choose_ttml_edit_mode, line_cues, needs_ttml_protection
from app.services.lyrics_service import LyricsError, LyricsService
from app.services.lrc_draft_service import LrcDraft, LrcDraftError, LrcDraftService
from app.services.preview_audio_settings import preview_volume, save_preview_volume
from app.services.playlist_service import PlaylistService
from app.utils.i18n import Translator
from app.widgets.audio_sync_warning import AudioSyncWarning


_STEP_HELP_TOPICS = {0: "lrc_audio", 1: "lrc_input", 2: "lrc_timing", 3: "lrc_review"}
"""F1 opens the lyrics editor's help on the topic of the step being shown."""


class LrcGeneratorDialog(QDialog):
    """Record one timestamp per lyric line while listening to an audio file."""

    AUDIO_FILTER = "Audio files (*.mp3 *.wav *.flac *.aac *.m4a *.ogg)"

    def __init__(
        self, project_content: list[ProjectContent], translator: Translator,
        parent: QWidget | None = None,
        *,
        playlist_tracks: list[PlaylistTrack] | None = None,
        target_track_id: str = "",
        track_edit_mode: bool = False,
        initial_audio_path: str = "",
        initial_cues: list[dict[str, object]] | None = None,
        initial_title: str = "",
        initial_artist: str = "",
    ) -> None:
        super().__init__(parent)
        self.translator = translator
        self.track_edit_mode = track_edit_mode
        self._preserved_cues = None
        self.audio_path = ""
        self.lines: list[str] = []
        self.timestamps: list[float | None] = []
        self.current_index = 0
        self._undo_stack: list[tuple[int, float | None, float | None]] = []
        self._redo_stack: list[tuple[int, float | None, float | None]] = []
        self._shortcuts_dialog: LrcShortcutsDialog | None = None
        self._playback_highlight_row = -1
        self._playback_timestamps: list[tuple[float, int]] = []
        self._timeline_marker_rows: set[int] = set()
        self._last_playback_position = 0
        self._suspend_playback_autoscroll = False
        self._playback_scroll_resume_timer = QTimer(self)
        self._playback_scroll_resume_timer.setSingleShot(True)
        self._playback_scroll_resume_timer.setInterval(900)
        self._playback_scroll_resume_timer.timeout.connect(
            self._resume_playback_autoscroll
        )
        self.saved_paths: list[Path] = []
        self._initial_audio_path = initial_audio_path
        self._initial_cues_supplied = bool(initial_cues)
        self._initial_title = initial_title
        self._initial_artist = initial_artist
        self._playlist_tracks = list(playlist_tracks or [])
        self._target_track_id = target_track_id
        self._applied_lyrics_track: PlaylistTrack | None = None
        self._lyrics_choice_audio = ""
        self._timing_baseline_lines: list[str] = []
        self._timing_baseline_timestamps: list[float | None] = []
        self._last_step_index = 0
        self._last_double_clicked_row = -1
        self._metadata_reader = PlaylistService(self)
        data_root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        self._draft_service = LrcDraftService(Path(data_root or Path.cwd() / ".app-data"))
        self._draft_dirty = False
        self._draft_had_content_this_session = False
        self._restoring_draft = False
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(1_500)
        self._draft_timer.timeout.connect(self._save_draft_now)
        self._draft_periodic_timer = QTimer(self)
        self._draft_periodic_timer.setInterval(15_000)
        self._draft_periodic_timer.timeout.connect(self._save_draft_now)
        self._draft_periodic_timer.start()

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(preview_volume() / 100.0)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)

        self.setMinimumSize(820, 600)
        self.resize(980, 700)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        header_text = QVBoxLayout()
        self.step_label = QLabel()
        self.step_label.setObjectName("mutedLabel")
        self.step_title = QLabel()
        self.step_title.setObjectName("panelTitle")
        self.step_description = QLabel()
        self.step_description.setObjectName("mutedLabel")
        self.step_description.setWordWrap(True)
        header_text.addWidget(self.step_label)
        header_text.addWidget(self.step_title)
        header_text.addWidget(self.step_description)
        header.addLayout(header_text, 1)
        self.step_dots = QLabel()
        self.step_dots.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.step_dots)
        root.addLayout(header)

        self.audio_group = QGroupBox()
        audio_layout = QVBoxLayout(self.audio_group)
        self.project_audio_combo = QComboBox()
        for content in project_content:
            if content.media_type == "audio" and Path(content.path).is_file():
                self.project_audio_combo.addItem(content.name, content.path)
        self.project_audio_combo.addItem("", "__local__")
        audio_layout.addWidget(self.project_audio_combo)
        self.local_audio_row = QWidget()
        local_audio_layout = QHBoxLayout(self.local_audio_row)
        local_audio_layout.setContentsMargins(0, 0, 0, 0)
        self.local_audio_path_edit = QLineEdit()
        self.local_audio_path_edit.setReadOnly(True)
        self.browse_audio_button = QPushButton()
        local_audio_layout.addWidget(self.local_audio_path_edit, 1)
        local_audio_layout.addWidget(self.browse_audio_button)
        audio_layout.addWidget(self.local_audio_row)
        self.audio_path_label = QLabel()
        self.audio_path_label.setObjectName("mutedLabel")
        self.audio_path_label.setWordWrap(True)
        audio_layout.addWidget(self.audio_path_label)
        input_host = QWidget()
        input_layout = QVBoxLayout(input_host)
        input_layout.setContentsMargins(0, 0, 5, 0)
        self.input_help = QLabel()
        self.input_help.setWordWrap(True)
        self.input_help.setObjectName("mutedLabel")
        input_mode_row = QHBoxLayout()
        self.input_mode_label = QLabel()
        self.input_mode_combo = QComboBox()
        self.input_mode_combo.addItem("", "single")
        self.input_mode_combo.addItem("", "multiline")
        input_mode_row.addWidget(self.input_mode_label)
        input_mode_row.addWidget(self.input_mode_combo, 1)
        self.lyrics_editor = QPlainTextEdit()
        self.lyrics_editor.setPlaceholderText("")
        input_layout.addWidget(self.input_help)
        input_layout.addLayout(input_mode_row)
        input_layout.addWidget(self.lyrics_editor, 1)
        self.filter_group = QGroupBox()
        filter_layout = QVBoxLayout(self.filter_group)
        filter_layout.setContentsMargins(10, 8, 10, 8)
        self.ignore_line_breaks_check = QCheckBox()
        self.ignore_bracketed_lines_check = QCheckBox()
        self.advanced_filter_check = QCheckBox()
        filter_layout.addWidget(self.ignore_line_breaks_check)
        filter_layout.addWidget(self.ignore_bracketed_lines_check)
        filter_layout.addWidget(self.advanced_filter_check)
        self.regex_filter_row = QWidget()
        regex_filter_layout = QHBoxLayout(self.regex_filter_row)
        regex_filter_layout.setContentsMargins(18, 0, 0, 0)
        self.regex_filter_label = QLabel()
        self.regex_filter_edit = QLineEdit()
        regex_filter_layout.addWidget(self.regex_filter_label)
        regex_filter_layout.addWidget(self.regex_filter_edit, 1)
        filter_layout.addWidget(self.regex_filter_row)
        self.regex_filter_row.setVisible(False)
        input_layout.addWidget(self.filter_group)
        metadata = QFormLayout()
        self.title_edit = QLineEdit()
        self.artist_edit = QLineEdit()
        self.title_edit.setReadOnly(True)
        self.artist_edit.setReadOnly(True)
        self.title_label = QLabel()
        self.artist_label = QLabel()
        metadata.addRow(self.title_label, self.title_edit)
        metadata.addRow(self.artist_label, self.artist_edit)
        input_layout.addLayout(metadata)
        # Retained as a hidden compatibility alias; Next performs preparation.
        self.prepare_button = QPushButton()
        self.prepare_button.hide()

        timing_host = QWidget()
        timing_layout = QVBoxLayout(timing_host)
        timing_layout.setContentsMargins(5, 0, 0, 0)
        self.timeline_table = QTableWidget(0, 3)
        self.timeline_table.setObjectName("lrcTimelineTable")
        self.timeline_table.setStyleSheet(
            "QTableWidget#lrcTimelineTable::item:selected {"
            "background-color: transparent; color: palette(text); border: none;"
            "}"
        )
        self.timeline_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.timeline_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.timeline_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.timeline_table.verticalHeader().setVisible(False)
        self.timeline_table.horizontalHeader().setStretchLastSection(True)
        self.timeline_table.setColumnWidth(0, 68)
        self.timeline_table.setColumnWidth(1, 105)
        self.timeline_table.itemSelectionChanged.connect(self._timeline_selection_changed)
        self.timeline_table.cellDoubleClicked.connect(self._seek_to_row)
        timing_layout.addWidget(self.timeline_table, 1)

        record_row = QHBoxLayout()
        record_row.setSpacing(8)
        self.record_button = QPushButton()
        self.record_button.setObjectName("primaryButton")
        self.record_button.setMinimumWidth(210)
        self.use_selected_button = QToolButton()
        self.use_selected_button.setMinimumWidth(180)
        record_row.addWidget(self.record_button, 1)
        record_row.addWidget(self.use_selected_button)
        timing_layout.addLayout(record_row)

        tool_groups_row = QHBoxLayout()
        tool_groups_row.setSpacing(8)
        self.history_tools_group = QGroupBox()
        history_tools_layout = QHBoxLayout(self.history_tools_group)
        history_tools_layout.setContentsMargins(8, 6, 8, 8)
        history_tools_layout.setSpacing(6)
        self.undo_button = QToolButton()
        self.redo_button = QToolButton()
        history_tools_layout.addWidget(self.undo_button)
        history_tools_layout.addWidget(self.redo_button)

        self.timing_tools_group = QGroupBox()
        timing_tools_layout = QGridLayout(self.timing_tools_group)
        timing_tools_layout.setContentsMargins(8, 6, 8, 8)
        timing_tools_layout.setHorizontalSpacing(6)
        timing_tools_layout.setVerticalSpacing(6)
        self.nudge_back_button = QToolButton()
        self.nudge_forward_button = QToolButton()
        self.clear_time_button = QToolButton()
        self.reset_all_button = QToolButton()
        timing_tools_layout.addWidget(self.nudge_back_button, 0, 0)
        timing_tools_layout.addWidget(self.nudge_forward_button, 0, 1)
        timing_tools_layout.addWidget(self.clear_time_button, 1, 0)
        timing_tools_layout.addWidget(self.reset_all_button, 1, 1)

        self.lyric_tools_group = QGroupBox()
        lyric_tools_layout = QGridLayout(self.lyric_tools_group)
        lyric_tools_layout.setContentsMargins(8, 6, 8, 8)
        lyric_tools_layout.setHorizontalSpacing(6)
        lyric_tools_layout.setVerticalSpacing(6)
        self.add_line_button = QToolButton()
        self.edit_line_button = QToolButton()
        self.delete_line_button = QToolButton()
        lyric_tools_layout.addWidget(self.add_line_button, 0, 0)
        lyric_tools_layout.addWidget(self.edit_line_button, 0, 1)
        lyric_tools_layout.addWidget(self.delete_line_button, 1, 0, 1, 2)
        tool_groups_row.addWidget(self.history_tools_group, 2)
        tool_groups_row.addWidget(self.timing_tools_group, 3)
        tool_groups_row.addWidget(self.lyric_tools_group, 3)
        timing_layout.addLayout(tool_groups_row)

        help_row = QHBoxLayout()
        self.timing_cursor_help = QLabel()
        self.timing_cursor_help.setObjectName("mutedLabel")
        help_row.addStretch(1)
        help_row.addWidget(self.timing_cursor_help)
        timing_layout.addLayout(help_row)

        tool_buttons = (
            self.undo_button, self.redo_button, self.use_selected_button,
            self.nudge_back_button, self.nudge_forward_button,
            self.clear_time_button, self.reset_all_button,
            self.add_line_button, self.edit_line_button, self.delete_line_button,
        )
        for button in tool_buttons:
            button.setMinimumHeight(36)
            button.setIconSize(QSize(18, 18))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        standard_icon = self.style().standardIcon
        self.record_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.undo_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_ArrowBack))
        self.redo_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_ArrowForward))
        self.use_selected_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_ArrowDown))
        self.nudge_back_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_MediaSeekBackward))
        self.nudge_forward_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_MediaSeekForward))
        self.clear_time_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_DialogResetButton))
        self.reset_all_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_BrowserReload))
        self.add_line_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        self.edit_line_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.delete_line_button.setIcon(standard_icon(QStyle.StandardPixmap.SP_TrashIcon))
        self.delete_line_button.setProperty("danger", True)

        calibration_row = QHBoxLayout()
        self.calibration_label = QLabel()
        self.calibration_spin = QSpinBox()
        self.calibration_spin.setRange(-1000, 1000)
        self.calibration_spin.setSingleStep(10)
        self.calibration_spin.setSuffix(" ms")
        self.calibration_help = QLabel()
        self.calibration_help.setObjectName("mutedLabel")
        self.calibration_help.setWordWrap(True)
        calibration_row.addWidget(self.calibration_label)
        calibration_row.addWidget(self.calibration_spin)
        calibration_row.addWidget(self.calibration_help, 1)
        timing_layout.addLayout(calibration_row)

        self.preview_group = QGroupBox()
        preview_layout = QVBoxLayout(self.preview_group)
        preview_layout.setContentsMargins(12, 8, 12, 10)
        preview_layout.setSpacing(7)
        progress_row = QHBoxLayout()
        self.position_label = QLabel("00:00.00")
        self.position_label.setMinimumWidth(72)
        self.position_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 1)
        self.position_slider.setTracking(True)
        self.position_slider.setMinimumHeight(28)
        self.duration_label = QLabel("00:00.00")
        self.duration_label.setMinimumWidth(72)
        progress_row.addWidget(self.position_label)
        progress_row.addWidget(self.position_slider, 1)
        progress_row.addWidget(self.duration_label)
        preview_layout.addLayout(progress_row)

        transport = QHBoxLayout()
        transport.setSpacing(8)
        self.play_button = QPushButton()
        self.rewind_button = QPushButton()
        self.forward_button = QPushButton()
        self.stop_button = QPushButton()
        self.play_button.setObjectName("transportPlayButton")
        self.play_button.setMinimumSize(132, 38)
        for button in (self.rewind_button, self.forward_button, self.stop_button):
            button.setMinimumSize(86, 34)
        transport.addStretch(1)
        transport.addWidget(self.rewind_button)
        transport.addWidget(self.play_button)
        transport.addWidget(self.forward_button)
        transport.addWidget(self.stop_button)
        transport.addStretch(1)
        preview_layout.addLayout(transport)

        options_row = QHBoxLayout()
        self.preview_mode_check = QCheckBox()
        self.shortcuts_button = QPushButton()
        self.volume_label = QLabel()
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(preview_volume())
        self.volume_slider.setMinimumWidth(140)
        self.volume_slider.setMaximumWidth(220)
        self.volume_value = QLabel(f"{preview_volume()}%")
        self.volume_value.setMinimumWidth(38)
        options_row.addWidget(self.preview_mode_check)
        options_row.addWidget(self.shortcuts_button)
        options_row.addStretch(1)
        options_row.addWidget(self.volume_label)
        options_row.addWidget(self.volume_slider)
        options_row.addWidget(self.volume_value)
        preview_layout.addLayout(options_row)

        media_icon = self.style().standardIcon
        self.rewind_button.setIcon(media_icon(QStyle.StandardPixmap.SP_MediaSeekBackward))
        self.forward_button.setIcon(media_icon(QStyle.StandardPixmap.SP_MediaSeekForward))
        self.stop_button.setIcon(media_icon(QStyle.StandardPixmap.SP_MediaStop))
        self.pages = QStackedWidget()
        audio_page = QWidget()
        audio_page_layout = QVBoxLayout(audio_page)
        audio_page_layout.setContentsMargins(4, 8, 4, 4)
        audio_page_layout.addWidget(self.audio_group)
        audio_page_layout.addStretch(1)
        lyrics_page = QWidget()
        lyrics_page_layout = QVBoxLayout(lyrics_page)
        lyrics_page_layout.setContentsMargins(4, 8, 4, 4)
        lyrics_page_layout.addWidget(input_host, 1)
        timing_page = QWidget()
        timing_page_layout = QVBoxLayout(timing_page)
        timing_page_layout.setContentsMargins(4, 8, 4, 4)
        timing_page_layout.setSpacing(10)
        timing_page_layout.addWidget(timing_host, 1)
        timing_page_layout.addWidget(self.preview_group)
        review_page = QWidget()
        review_page_layout = QVBoxLayout(review_page)
        review_page_layout.setContentsMargins(4, 8, 4, 4)
        self.review_summary = QLabel()
        self.review_summary.setObjectName("panelTitle")
        self.review_summary.setWordWrap(True)
        self.review_text = QPlainTextEdit()
        self.review_text.setReadOnly(True)
        review_page_layout.addWidget(self.review_summary)
        review_page_layout.addWidget(self.review_text, 1)
        self.review_help = QLabel()
        self.review_help.setObjectName("mutedLabel")
        self.review_help.setWordWrap(True)
        review_page_layout.addWidget(self.review_help)
        for page in (audio_page, lyrics_page, timing_page, review_page):
            self.pages.addWidget(page)
        root.addWidget(self.pages, 1)
        self.audio_sync_warning = AudioSyncWarning(self, audio_output=self.audio_output,
                                                  media_player=self.media_player, translator=translator)
        root.addWidget(self.audio_sync_warning)

        footer = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setObjectName("mutedLabel")
        self.autosave_label = QLabel()
        self.autosave_label.setObjectName("mutedLabel")
        self.add_to_project_check = QCheckBox()
        self.add_to_project_check.setChecked(True)
        self.back_button = QPushButton()
        self.next_button = QPushButton()
        self.save_button = QPushButton()
        self.finish_button = QPushButton()
        self.close_button = QPushButton()
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.autosave_label)
        footer.addWidget(self.add_to_project_check)
        footer.addWidget(self.back_button)
        footer.addWidget(self.next_button)
        footer.addWidget(self.save_button)
        footer.addWidget(self.finish_button)
        footer.addWidget(self.close_button)
        root.addLayout(footer)

        self.project_audio_combo.currentIndexChanged.connect(self._audio_selection_changed)
        self.project_audio_combo.activated.connect(self._audio_selection_activated)
        self.browse_audio_button.clicked.connect(self._browse_audio)
        self.prepare_button.clicked.connect(self._prepare_lines)
        self.input_mode_combo.currentIndexChanged.connect(self._update_input_help)
        self.advanced_filter_check.toggled.connect(self.regex_filter_row.setVisible)
        self.record_button.clicked.connect(self._record_timestamp)
        self.undo_button.clicked.connect(self._undo_record)
        self.redo_button.clicked.connect(self._redo_record)
        self.use_selected_button.clicked.connect(self._use_selected_row)
        self.nudge_back_button.clicked.connect(lambda: self._nudge_selected(-0.1))
        self.nudge_forward_button.clicked.connect(lambda: self._nudge_selected(0.1))
        self.clear_time_button.clicked.connect(self._clear_selected_time)
        self.reset_all_button.clicked.connect(self._reset_all)
        self.add_line_button.clicked.connect(self._add_lyric_line)
        self.edit_line_button.clicked.connect(self._edit_selected_line)
        self.delete_line_button.clicked.connect(self._delete_selected_line)
        self.play_button.clicked.connect(self._toggle_playback)
        self.rewind_button.clicked.connect(lambda: self._seek_relative(-3_000))
        self.forward_button.clicked.connect(lambda: self._seek_relative(3_000))
        self.stop_button.clicked.connect(self._stop_playback)
        self.preview_mode_check.toggled.connect(self._preview_mode_changed)
        self.shortcuts_button.clicked.connect(self._open_shortcuts)
        self.position_slider.sliderReleased.connect(self._seek_slider)
        self.volume_slider.valueChanged.connect(self._set_volume)
        self.save_button.clicked.connect(self._save_lrc)
        self.back_button.clicked.connect(self._previous_step)
        self.next_button.clicked.connect(self._next_step)
        self.finish_button.clicked.connect(self.accept)
        self.close_button.clicked.connect(self.reject)
        self.pages.currentChanged.connect(self._step_changed)
        self.media_player.positionChanged.connect(self._position_changed)
        self.media_player.durationChanged.connect(self._duration_changed)
        self.media_player.playbackStateChanged.connect(self._playback_state_changed)
        self.media_player.errorOccurred.connect(self._playback_error)
        self.record_shortcut = QShortcut(QKeySequence("Space"), self)
        self.record_shortcut.activated.connect(self._record_timestamp)
        self.undo_shortcut = QShortcut(QKeySequence.StandardKey.Undo, self)
        self.undo_shortcut.activated.connect(self._undo_record)
        self.redo_shortcut = QShortcut(QKeySequence.StandardKey.Redo, self)
        self.redo_shortcut.activated.connect(self._redo_record)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.activated.connect(self._save_lrc)
        self.help_shortcut = install_help_shortcut(
            self, lambda dialog: ("lyrics_editor", _STEP_HELP_TOPICS.get(dialog.pages.currentIndex())),
            translator,
        )
        self.shortcuts_shortcut = QShortcut(QKeySequence("Shift+F1"), self)
        self.shortcuts_shortcut.activated.connect(self._open_shortcuts)
        self.play_pause_shortcut = QShortcut(QKeySequence("Ctrl+Space"), self)
        self.play_pause_shortcut.activated.connect(self._toggle_playback)
        self.seek_back_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        self.seek_back_shortcut.activated.connect(lambda: self._seek_relative(-1_000))
        self.seek_forward_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        self.seek_forward_shortcut.activated.connect(lambda: self._seek_relative(1_000))
        self.edit_line_shortcut = QShortcut(QKeySequence("F2"), self)
        self.edit_line_shortcut.activated.connect(self._edit_selected_line)
        self.delete_line_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self)
        self.delete_line_shortcut.activated.connect(self._delete_selected_line)
        application = QApplication.instance()
        if application is not None:
            application.focusChanged.connect(self._sync_shortcuts_for_focus)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()
        self.title_edit.setText(initial_title)
        self.artist_edit.setText(initial_artist)
        self._load_initial_cues(initial_cues or [])
        if initial_audio_path:
            self._set_audio(initial_audio_path)
            project_index = self.project_audio_combo.findData(str(Path(initial_audio_path).resolve()))
            if project_index >= 0:
                self.project_audio_combo.setCurrentIndex(project_index)
            else:
                self.project_audio_combo.setCurrentIndex(
                    self.project_audio_combo.findData("__local__")
                )
                self.local_audio_path_edit.setText(self.audio_path)
        elif self.project_audio_combo.count() > 1:
            self.project_audio_combo.setCurrentIndex(0)
            self._audio_selection_changed(0)
        else:
            self._audio_selection_changed(self.project_audio_combo.currentIndex())
        if self.track_edit_mode and self.pages.currentIndex() == 0:
            self.pages.setCurrentIndex(1)
        self._step_changed(self.pages.currentIndex())
        self.lyrics_editor.textChanged.connect(self._mark_draft_dirty)
        self.title_edit.textChanged.connect(self._mark_draft_dirty)
        self.artist_edit.textChanged.connect(self._mark_draft_dirty)
        self.input_mode_combo.currentIndexChanged.connect(self._mark_draft_dirty)
        self.ignore_line_breaks_check.toggled.connect(self._mark_draft_dirty)
        self.ignore_bracketed_lines_check.toggled.connect(self._mark_draft_dirty)
        self.advanced_filter_check.toggled.connect(self._mark_draft_dirty)
        self.regex_filter_edit.textChanged.connect(self._mark_draft_dirty)

    def _load_initial_cues(self, cues: list[dict[str, object]]) -> None:
        """Populate the editor from an existing timed-lyrics collection."""
        if needs_ttml_protection(cues):
            raise LyricsError("TTML requires an explicit conversion or deletion-only choice before line editing.")
        prepared: list[tuple[float, str]] = []
        for cue in cues:
            text = LyricsService.decode_line_breaks(cue.get("text", "")).strip()
            if not text:
                continue
            try:
                start = max(0.0, float(cue.get("start", 0.0)))
            except (TypeError, ValueError):
                continue
            prepared.append((start, text))
        prepared.sort(key=lambda item: item[0])
        self.lines = [text for _start, text in prepared]
        self.timestamps = [start for start, _text in prepared]
        self.current_index = len(self.lines)
        self._undo_stack.clear()
        self._redo_stack.clear()
        multiline = any("\n" in text for text in self.lines)
        mode_index = self.input_mode_combo.findData("multiline" if multiline else "single")
        if mode_index >= 0:
            self.input_mode_combo.setCurrentIndex(mode_index)
        separator = "\n\n" if multiline else "\n"
        self.lyrics_editor.setPlainText(separator.join(self.lines))
        self._refresh_table()

    @property
    def add_saved_files_to_project(self) -> bool:
        return self.add_to_project_check.isChecked()

    @property
    def result_track_id(self) -> str:
        """Return the unambiguous playlist track matching the edited audio."""
        if not self.audio_path:
            return ""
        try:
            audio = Path(self.audio_path).expanduser().resolve()
        except OSError:
            return ""
        matches: list[PlaylistTrack] = []
        for track in self._playlist_tracks:
            try:
                if Path(track.file_path).expanduser().resolve() == audio:
                    matches.append(track)
            except OSError:
                continue
        preferred = next(
            (track for track in matches if track.id == self._target_track_id), None
        )
        if preferred is not None:
            return preferred.id
        return matches[0].id if len(matches) == 1 else ""

    def accept(self) -> None:
        """Close only when the result has a durable file or an application target."""
        if (
            not self.track_edit_mode
            and not self.saved_paths
            and not self.result_track_id
        ):
            self._set_status(
                "적용할 플레이리스트 곡이 없습니다. 곡을 선택해 다시 열거나 LRC 파일을 먼저 저장하세요."
                if self._korean else
                "There is no playlist track to apply to. Select a track and reopen, or save an LRC file first.",
                True,
            )
            return
        super().accept()

    def _use_project_audio(self) -> None:
        """Compatibility wrapper for selecting the current project-audio item."""
        path = str(self.project_audio_combo.currentData() or "")
        if path and path != "__local__":
            self._set_audio(path)

    def _audio_selection_changed(self, _index: int) -> None:
        value = str(self.project_audio_combo.currentData() or "")
        local = value == "__local__"
        self.local_audio_row.setVisible(local)
        if value and not local:
            self._set_audio(value)

    def _audio_selection_activated(self, index: int) -> None:
        if str(self.project_audio_combo.itemData(index) or "") == "__local__":
            self.media_player.stop()
            self.media_player.setSource(QUrl())
            self.audio_path = ""
            self.local_audio_path_edit.clear()
            self.audio_path_label.clear()
            self._browse_audio()

    def _browse_audio(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, self.browse_audio_button.text(), self.audio_path, self.AUDIO_FILTER,
        )
        if selected:
            self._set_audio(selected)

    def _set_audio(self, path: str) -> None:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            self._set_status("오디오 파일을 찾을 수 없습니다." if self._korean else "Audio file not found.", True)
            return
        previous_audio = self.audio_path
        if previous_audio and previous_audio != str(candidate.resolve()):
            self._save_draft_now()
            self._initial_cues_supplied = False
            self._preserved_cues = None
        self.media_player.stop()
        self.audio_path = str(candidate.resolve())
        if self.audio_path != previous_audio:
            self._draft_had_content_this_session = False
            self._lyrics_choice_audio = ""
        self._applied_lyrics_track = self._track_with_applied_lyrics(self.audio_path)
        self.media_player.setSource(QUrl.fromLocalFile(self.audio_path))
        self.audio_path_label.setText(self.audio_path)
        if str(self.project_audio_combo.currentData() or "") == "__local__":
            self.local_audio_path_edit.setText(self.audio_path)
        was_restoring = self._restoring_draft
        self._restoring_draft = True
        metadata = self._metadata_reader.inspect_files([candidate])
        if metadata:
            track = metadata[0].track
            title = track.title
            artist = track.artist
            try:
                same_initial_audio = (
                    bool(self._initial_audio_path)
                    and Path(self._initial_audio_path).resolve() == candidate.resolve()
                )
            except OSError:
                same_initial_audio = False
            if same_initial_audio:
                if title == candidate.stem and self._initial_title.strip():
                    title = self._initial_title.strip()
                if artist == "Unknown Artist" and self._initial_artist.strip():
                    artist = self._initial_artist.strip()
            self.title_edit.setText(title)
            self.artist_edit.setText(artist)
        else:
            self.title_edit.setText(self._initial_title.strip() or candidate.stem)
            self.artist_edit.setText(self._initial_artist.strip() or "Unknown Artist")
        self._restoring_draft = was_restoring
        self._set_status("오디오를 불러왔습니다." if self._korean else "Audio loaded.")
        if self.audio_path != previous_audio and not self._initial_cues_supplied:
            self._offer_draft_recovery()
        self._update_enabled()

    def _track_with_applied_lyrics(self, audio_path: str) -> PlaylistTrack | None:
        try:
            target = Path(audio_path).expanduser().resolve()
        except OSError:
            return None
        for track in self._playlist_tracks:
            try:
                same_audio = Path(track.file_path).expanduser().resolve() == target
            except OSError:
                same_audio = False
            if same_audio and (track.lyrics or track.lyrics_path.strip()):
                return track
        return None

    def _existing_lyrics_choice(self) -> str:
        track = self._applied_lyrics_track
        if track is None:
            return "new"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(
            "기존 가사 확인" if self._korean else "Existing lyrics"
        )
        box.setText(
            "해당 곡은 이미 가사가 적용되어 있는 오디오입니다.\n"
            "새로 작성하시겠습니까, 또는 적용되어 있는 가사를 불러오시겠습니까?"
            if self._korean else
            "This audio already has lyrics applied.\n"
            "Would you like to start over or load the applied lyrics?"
        )
        new_button = box.addButton(
            "새로 작성" if self._korean else "Start over",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        load_button = box.addButton(
            "적용된 가사 불러오기" if self._korean else "Load applied lyrics",
            QMessageBox.ButtonRole.AcceptRole,
        )
        cancel_button = box.addButton(
            "취소" if self._korean else "Cancel",
            QMessageBox.ButtonRole.RejectRole,
        )
        box.setDefaultButton(load_button)
        box.exec()
        if box.clickedButton() is load_button:
            return "load"
        if box.clickedButton() is new_button:
            return "new"
        if box.clickedButton() is cancel_button:
            return "cancel"
        return "cancel"

    def _load_applied_track_lyrics(self) -> bool:
        track = self._applied_lyrics_track
        if track is None:
            return False
        cues = [cue.copy() for cue in track.lyrics]
        if not cues and track.lyrics_path.strip():
            try:
                cues = [dict(cue) for cue in LyricsService.load(track.lyrics_path)]
            except LyricsError as error:
                QMessageBox.warning(
                    self,
                    "가사 불러오기 실패" if self._korean else "Could not load lyrics",
                    str(error),
                )
                return False
        if not cues:
            return False
        mode = choose_ttml_edit_mode(cues, track.lyrics_path, self.translator, self)
        if mode == "cancel":
            return False
        if mode == "delete":
            if not self.result_track_id:
                QMessageBox.warning(self, "가사/자막 편집기" if self._korean else "Lyrics/Subtitle Editor",
                    "적용할 곡이 모호합니다. 플레이리스트에서 곡을 선택하거나 곡 정보/설정에서 편집하세요."
                    if self._korean else "The target track is ambiguous. Select a playlist track or edit from Track Settings.")
                return False
            dialog = TtmlRowsDialog(cues, self.translator, self, title=track.title, target_track_id=track.id)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            if accepted:
                self._preserved_cues = dialog.timed_cues()
                self.accept()
            dialog.deleteLater()
            return False
        if mode == "convert":
            cues = line_cues(cues)
        self._restoring_draft = True
        try:
            self.title_edit.setText(track.title)
            self.artist_edit.setText(track.artist)
            self._load_initial_cues(cues)
            self._timing_baseline_lines = list(self.lines)
            self._timing_baseline_timestamps = list(self.timestamps)
        finally:
            self._restoring_draft = False
        self._autosave_after_change()
        return True

    def _clear_lyrics_for_new_draft(self) -> None:
        self._restoring_draft = True
        try:
            self.lyrics_editor.clear()
            self.lines = []
            self.timestamps = []
            self.current_index = 0
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._refresh_table()
        finally:
            self._restoring_draft = False

    def _mark_draft_dirty(self, *_args: object) -> None:
        if self._restoring_draft:
            return
        self._draft_dirty = True
        if self.audio_path:
            self.autosave_label.setText(
                "자동 저장 대기 중…" if self._korean else "Autosave pending…"
            )
            self._draft_timer.start()

    def _autosave_after_change(self) -> None:
        self._mark_draft_dirty()
        self._draft_timer.stop()
        self._save_draft_now()

    def _draft_payload(self) -> dict[str, object]:
        return {
            "step_index": self.pages.currentIndex(),
            "lyrics_text": self.lyrics_editor.toPlainText(),
            "lines": list(self.lines),
            "timestamps": list(self.timestamps),
            "current_index": self.current_index,
            "title": self.title_edit.text(),
            "artist": self.artist_edit.text(),
            "input_mode": str(self.input_mode_combo.currentData() or "single"),
            "ignore_line_breaks": self.ignore_line_breaks_check.isChecked(),
            "ignore_bracketed_lines": self.ignore_bracketed_lines_check.isChecked(),
            "advanced_filter": self.advanced_filter_check.isChecked(),
            "regex_filter": self.regex_filter_edit.text(),
        }

    def _save_draft_now(self) -> None:
        if not self._draft_dirty or not self.audio_path:
            return
        has_lyrics = (
            bool(self.lyrics_editor.toPlainText().strip())
            if self.pages.currentIndex() <= 1
            else any(line.strip() for line in self.lines)
        )
        if not has_lyrics:
            if self._draft_had_content_this_session:
                try:
                    self._draft_service.clear(self.audio_path)
                except LrcDraftError as error:
                    self.autosave_label.setText(
                        "복구 초안 정리 실패"
                        if self._korean else "Could not clear recovery draft"
                    )
                    self.autosave_label.setToolTip(str(error))
                    return
                self._draft_had_content_this_session = False
            self._draft_dirty = False
            self._draft_timer.stop()
            self.autosave_label.setText(
                "가사 없음 · 자동 저장 안 함"
                if self._korean else "No lyrics · autosave skipped"
            )
            return
        try:
            draft = self._draft_service.save(self.audio_path, self._draft_payload())
        except LrcDraftError:
            self.autosave_label.setText(
                "자동 저장 실패" if self._korean else "Autosave failed"
            )
            return
        self._draft_dirty = False
        self._draft_had_content_this_session = True
        saved_time = draft.saved_at.astimezone().strftime("%H:%M:%S")
        self.autosave_label.setText(
            f"자동 저장됨 {saved_time}" if self._korean else f"Autosaved {saved_time}"
        )

    def _offer_draft_recovery(self) -> None:
        try:
            draft = self._draft_service.load(self.audio_path)
        except LrcDraftError as error:
            self.autosave_label.setText(
                "복구 초안 읽기 실패" if self._korean else "Could not read recovery draft"
            )
            self.autosave_label.setToolTip(str(error))
            return
        if draft is None:
            return
        saved_time = draft.saved_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        answer = QMessageBox.question(
            self,
            "가사 자동 저장 복구" if self._korean else "Recover autosaved lyrics",
            (
                f"이 오디오의 저장되지 않은 가사 작업이 있습니다.\n"
                f"저장 시각: {saved_time}\n\n가사와 타이밍을 복구할까요?"
                if self._korean else
                f"Unsaved lyric work exists for this audio.\n"
                f"Saved: {saved_time}\n\nRestore its lyrics and timing?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._restore_draft(draft)
        else:
            if self._clear_recovery_draft():
                self.autosave_label.setText(
                    "복구 초안 삭제됨" if self._korean else "Recovery draft deleted"
                )

    def _restore_draft(self, draft: LrcDraft) -> None:
        data = draft.data
        raw_lines = data.get("lines", [])
        raw_timestamps = data.get("timestamps", [])
        if not isinstance(raw_lines, list) or not isinstance(raw_timestamps, list):
            return
        lines = [LyricsService.decode_line_breaks(value).strip() for value in raw_lines]
        lines = [value for value in lines if value]
        timestamps: list[float | None] = []
        for value in raw_timestamps[:len(lines)]:
            try:
                timestamps.append(None if value is None else max(0.0, float(value)))
            except (TypeError, ValueError):
                timestamps.append(None)
        timestamps.extend([None] * (len(lines) - len(timestamps)))
        saved_step_value = data.get("step_index")
        if saved_step_value is None:
            # Drafts written before step restoration was added did not contain
            # a page index. Recover them at the safest useful stage.
            saved_step = (
                2 if lines and any(value is not None for value in timestamps)
                else 1 if lines or str(data.get("lyrics_text", "")).strip()
                else 0
            )
        else:
            try:
                saved_step = int(saved_step_value)
            except (TypeError, ValueError):
                saved_step = 0
        minimum_step = 1 if self.track_edit_mode else 0
        saved_step = max(minimum_step, min(saved_step, self.pages.count() - 1))
        self._restoring_draft = True
        try:
            mode_index = self.input_mode_combo.findData(str(data.get("input_mode", "single")))
            if mode_index >= 0:
                self.input_mode_combo.setCurrentIndex(mode_index)
            self.ignore_line_breaks_check.setChecked(bool(data.get("ignore_line_breaks", False)))
            self.ignore_bracketed_lines_check.setChecked(
                bool(data.get("ignore_bracketed_lines", False))
            )
            self.advanced_filter_check.setChecked(bool(data.get("advanced_filter", False)))
            self.regex_filter_edit.setText(str(data.get("regex_filter", "")))
            self.title_edit.setText(str(data.get("title", self.title_edit.text())))
            self.artist_edit.setText(str(data.get("artist", self.artist_edit.text())))
            self.lines = lines
            self.timestamps = timestamps
            if saved_step >= 2 and self.lines:
                multiline = any("\n" in line for line in self.lines)
                if multiline:
                    multiline_index = self.input_mode_combo.findData("multiline")
                    if multiline_index >= 0:
                        self.input_mode_combo.setCurrentIndex(multiline_index)
                separator = (
                    "\n\n" if self.input_mode_combo.currentData() == "multiline" else "\n"
                )
                self.lyrics_editor.setPlainText(separator.join(self.lines))
            else:
                self.lyrics_editor.setPlainText(str(data.get("lyrics_text", "")))
            try:
                saved_index = int(data.get("current_index", 0))
            except (TypeError, ValueError):
                saved_index = 0
            self.current_index = max(0, min(saved_index, len(self.lines)))
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._refresh_table()
            self._timing_baseline_lines = list(self.lines)
            self._timing_baseline_timestamps = list(self.timestamps)
            self.pages.setCurrentIndex(saved_step)
        finally:
            self._restoring_draft = False
        self._draft_timer.stop()
        self._draft_dirty = False
        self._draft_had_content_this_session = True
        saved_time = draft.saved_at.astimezone().strftime("%H:%M:%S")
        self.autosave_label.setText(
            f"자동 저장 복구됨 {saved_time}" if self._korean else f"Autosave restored {saved_time}"
        )
        self._set_status(
            "저장되지 않았던 가사와 타이밍을 복구했습니다."
            if self._korean else "Restored unsaved lyrics and timing."
        )

    def _prepare_lines(self) -> bool:
        raw_text = self.lyrics_editor.toPlainText().strip()
        regex_filter: re.Pattern[str] | None = None
        if self.advanced_filter_check.isChecked() and self.regex_filter_edit.text().strip():
            try:
                regex_filter = re.compile(self.regex_filter_edit.text())
            except re.error as error:
                self._set_status(
                    f"정규식 오류: {error}" if self._korean else f"Regular expression error: {error}",
                    True,
                )
                self.regex_filter_edit.setFocus()
                return False

        physical_lines: list[str] = []
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if (self.ignore_bracketed_lines_check.isChecked()
                    and re.fullmatch(r"\[[^\]]*\]", line)):
                continue
            if regex_filter is not None:
                line = regex_filter.sub("", line).strip()
                if not line:
                    continue
            physical_lines.append(line)
        filtered_text = "\n".join(physical_lines)

        if self.ignore_line_breaks_check.isChecked():
            new_lines = [line for line in physical_lines if line]
        elif self.input_mode_combo.currentData() == "multiline":
            blocks = [
                block for block in re.split(r"\r?\n\s*\r?\n", filtered_text)
                if block.strip()
            ]
            new_lines = [
                "\n".join(line.strip() for line in block.splitlines() if line.strip())
                for block in blocks
            ]
        else:
            new_lines = [line.strip() for line in physical_lines if line.strip()]
        new_lines = [LyricsService.decode_line_breaks(line) for line in new_lines]
        if not new_lines:
            self._set_status("한 줄 이상의 가사를 입력하세요." if self._korean else "Enter at least one lyric line.", True)
            return False
        preserve_timestamps = (
            len(new_lines) == len(self.lines)
            and bool(self.lines)
            and any(value is not None for value in self.timestamps)
        )
        if any(value is not None for value in self.timestamps) and not preserve_timestamps:
            answer = QMessageBox.warning(
                self,
                "타이밍 다시 준비" if self._korean else "Prepare timing again",
                "기존에 기록한 시간이 초기화됩니다. 계속할까요?"
                if self._korean else "Existing recorded timestamps will be cleared. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        self.lines = new_lines
        if preserve_timestamps:
            self.current_index = self._next_unrecorded()
        else:
            self.timestamps = [None] * len(new_lines)
            self.current_index = 0
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._refresh_table()
        if preserve_timestamps:
            self._set_status(
                "가사 문구를 수정하고 기존 타이밍을 유지했습니다."
                if self._korean else "Lyric text updated while keeping the existing timing."
            )
        else:
            self._set_status(
                "재생 후 가사 타이밍에 맞춰 스페이스바를 누르세요."
                if self._korean else "Start playback, then press Space on each lyric timing."
            )
        self._autosave_after_change()
        return True

    def _record_timestamp(self) -> None:
        if not self.lines:
            self._prepare_lines()
            if not self.lines:
                return
        if not self.audio_path:
            self._set_status("먼저 오디오를 선택하세요." if self._korean else "Select an audio file first.", True)
            return
        if self.current_index >= len(self.lines):
            self._set_status("모든 가사의 시간이 기록되었습니다." if self._korean else "All lyric lines are timed.")
            return
        recorded = max(0.0, (self.media_player.position() + self.calibration_spin.value()) / 1000.0)
        index = self.current_index
        old = self.timestamps[index]
        self.timestamps[index] = recorded
        self._undo_stack.append((index, old, recorded))
        self._redo_stack.clear()
        self.current_index = min(len(self.lines), index + 1)
        self._suspend_playback_autoscroll = True
        self._playback_scroll_resume_timer.start()
        self._refresh_table(center_timing_cursor=True)
        self._set_status(
            f"{index + 1}번 줄을 {LyricsService.lrc_timestamp(recorded)}에 기록했습니다."
            if self._korean else
            f"Line {index + 1} recorded at {LyricsService.lrc_timestamp(recorded)}."
        )
        self._autosave_after_change()

    def _next_unrecorded(self, start: int = 0) -> int:
        for index in range(max(0, start), len(self.timestamps)):
            if self.timestamps[index] is None:
                return index
        return len(self.timestamps)

    def _undo_record(self) -> None:
        if not self._undo_stack:
            return
        index, old, new = self._undo_stack.pop()
        self.timestamps[index] = old
        self._redo_stack.append((index, old, new))
        self.current_index = index
        self._refresh_table()
        self._autosave_after_change()

    def _redo_record(self) -> None:
        if not self._redo_stack:
            return
        index, old, new = self._redo_stack.pop()
        self.timestamps[index] = new
        self._undo_stack.append((index, old, new))
        self.current_index = min(len(self.lines), index + 1)
        self._refresh_table()
        self._autosave_after_change()

    def _selected_row(self) -> int:
        indexes = self.timeline_table.selectionModel().selectedRows()
        return indexes[0].row() if indexes else -1

    def _use_selected_row(self) -> None:
        row = self._selected_row()
        if 0 <= row < len(self.lines):
            self._last_double_clicked_row = -1
            self.current_index = row
            self._refresh_table()
            self._autosave_after_change()

    def _seek_to_row(self, row: int, _column: int = 0) -> None:
        """Seek and toggle the timing cursor between after/current on repeated double-click."""
        if self.preview_mode_check.isChecked():
            return
        if 0 <= row < len(self.timestamps) and self.timestamps[row] is not None:
            self.media_player.setPosition(round(float(self.timestamps[row]) * 1000))
        advanced_index = min(len(self.lines), row + 1)
        if self._last_double_clicked_row == row and self.current_index == advanced_index:
            self.current_index = row
        else:
            self.current_index = advanced_index
        self._last_double_clicked_row = row
        self._refresh_table(row)
        self._autosave_after_change()

    def _add_lyric_line(self) -> None:
        text, accepted = QInputDialog.getMultiLineText(
            self,
            "가사 추가" if self._korean else "Add lyric",
            "추가할 가사" if self._korean else "Lyric text",
        )
        text = LyricsService.decode_line_breaks(text).strip()
        if not accepted or not text:
            return
        selected = self._selected_row()
        index = selected + 1 if selected >= 0 else min(self.current_index, len(self.lines))
        self.lines.insert(index, text)
        self.timestamps.insert(index, None)
        self.current_index = index
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._refresh_table(index)
        self._autosave_after_change()

    def _edit_selected_line(self) -> None:
        row = self._selected_row()
        if row < 0 or row >= len(self.lines):
            return
        text, accepted = QInputDialog.getMultiLineText(
            self,
            "가사 편집" if self._korean else "Edit lyric",
            "가사 내용" if self._korean else "Lyric text",
            self.lines[row],
        )
        text = LyricsService.decode_line_breaks(text).strip()
        if not accepted or not text or text == self.lines[row]:
            return
        self.lines[row] = text
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._refresh_table(row)
        self._autosave_after_change()

    def _delete_selected_line(self) -> None:
        row = self._selected_row()
        if row < 0 or row >= len(self.lines):
            return
        answer = QMessageBox.warning(
            self,
            "가사 삭제" if self._korean else "Delete lyric",
            f"선택한 {row + 1}번 가사와 기록된 타이밍을 삭제할까요?"
            if self._korean else
            f"Delete lyric {row + 1} and its recorded timing?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.lines.pop(row)
        self.timestamps.pop(row)
        if self.current_index > row:
            self.current_index -= 1
        self.current_index = min(self.current_index, len(self.lines))
        self._undo_stack.clear()
        self._redo_stack.clear()
        next_selection = min(row, len(self.lines) - 1)
        self._refresh_table(next_selection if next_selection >= 0 else None)
        self._autosave_after_change()

    def _nudge_selected(self, delta: float) -> None:
        row = self._selected_row()
        if row < 0 or self.timestamps[row] is None:
            return
        old = self.timestamps[row]
        new = max(0.0, float(old) + delta)
        self.timestamps[row] = new
        self._undo_stack.append((row, old, new))
        self._redo_stack.clear()
        self._refresh_table(row)
        self._autosave_after_change()

    def _clear_selected_time(self) -> None:
        row = self._selected_row()
        if row < 0 or self.timestamps[row] is None:
            return
        old = self.timestamps[row]
        self.timestamps[row] = None
        self._undo_stack.append((row, old, None))
        self._redo_stack.clear()
        self.current_index = row
        self._refresh_table(row)
        self._autosave_after_change()

    def _reset_all(self) -> None:
        if not any(value is not None for value in self.timestamps):
            return
        answer = QMessageBox.warning(
            self,
            "모든 타이밍 초기화" if self._korean else "Reset all timing",
            "기록한 모든 시간을 지울까요?" if self._korean else "Clear every recorded timestamp?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.timestamps = [None] * len(self.lines)
            self.current_index = 0
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._refresh_table()
            self._autosave_after_change()

    def timed_cues(self) -> list[dict[str, object]]:
        if self._preserved_cues is not None:
            return deepcopy(self._preserved_cues)
        cues = [
            {"start": float(timestamp), "end": 0.0, "text": self.lines[index]}
            for index, timestamp in enumerate(self.timestamps)
            if timestamp is not None
        ]
        cues.sort(key=lambda cue: float(cue["start"]))
        for index, cue in enumerate(cues):
            cue["end"] = (
                float(cues[index + 1]["start"])
                if index + 1 < len(cues) else float(cue["start"]) + 8.0
            )
        return cues

    def _open_shortcuts(self) -> None:
        if self._shortcuts_dialog is None:
            self._shortcuts_dialog = LrcShortcutsDialog(self.translator, self)
        self._shortcuts_dialog.show()
        self._shortcuts_dialog.raise_()
        self._shortcuts_dialog.activateWindow()

    def _preview_mode_changed(self, enabled: bool) -> None:
        for widget in (self.record_button, self.use_selected_button,
                       self.history_tools_group, self.timing_tools_group, self.lyric_tools_group,
                       self.timing_cursor_help, self.calibration_label, self.calibration_spin,
                       self.calibration_help):
            widget.setVisible(not enabled)
        self.timeline_table.clearSelection()
        self.timeline_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
            if enabled else QAbstractItemView.SelectionMode.SingleSelection
        )
        self._suspend_playback_autoscroll = False
        self._playback_scroll_resume_timer.stop()
        self._apply_timeline_visuals(set())
        self._update_enabled()
        self._sync_shortcuts_for_focus(None, self.focusWidget())
        if enabled:
            self._update_playback_highlight(
                self._last_playback_position, force_center=True,
            )
            self._set_status(
                "미리보기 모드: 현재 가사를 따라갑니다. 체크를 해제하면 편집할 수 있습니다."
                if self._korean else
                "Preview mode: following the current lyric. Uncheck to edit."
            )

    def _resume_playback_autoscroll(self) -> None:
        self._suspend_playback_autoscroll = False

    def _toggle_playback(self) -> None:
        if not self.audio_path:
            return
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
        else:
            self.media_player.play()

    def _stop_playback(self) -> None:
        self.media_player.stop()
        self.media_player.setPosition(0)

    def _seek_relative(self, milliseconds: int) -> None:
        self.media_player.setPosition(max(0, self.media_player.position() + milliseconds))

    def _seek_slider(self) -> None:
        self.media_player.setPosition(self.position_slider.value())

    def _position_changed(self, position: int) -> None:
        self._last_playback_position = max(0, position)
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(max(0, position))
        self.position_label.setText(self._clock(position))
        self.duration_label.setText(self._clock(self.position_slider.maximum()))
        self._update_playback_highlight(position)

    def _duration_changed(self, duration: int) -> None:
        self.position_slider.setRange(0, max(1, duration))
        self._position_changed(self.media_player.position())

    def _timeline_selection_changed(self) -> None:
        if self._selected_row() != self._last_double_clicked_row:
            self._last_double_clicked_row = -1
        self._apply_timeline_visuals(set())
        self._update_enabled()

    def _playback_row_at(self, position: int) -> int:
        seconds = max(0.0, position / 1000.0)
        index = bisect_right(self._playback_timestamps, (seconds + 0.0005, len(self.lines))) - 1
        return self._playback_timestamps[index][1] if index >= 0 else -1

    def _update_playback_highlight(
        self, position: int, *, force_center: bool = False,
    ) -> None:
        row = self._playback_row_at(position)
        changed = row != self._playback_highlight_row
        self._playback_highlight_row = row
        if changed:
            self._apply_timeline_visuals(set())
        if (row >= 0 and not self._suspend_playback_autoscroll
                and (force_center or (changed and self.preview_mode_check.isChecked()))):
            item = self.timeline_table.item(row, 0)
            if item is not None:
                self.timeline_table.scrollToItem(
                    item, QAbstractItemView.ScrollHint.PositionAtCenter,
                )

    def _apply_timeline_visuals(self, rows: set[int] | None = None) -> None:
        selected = self._selected_row()
        marker_rows = {selected, self.current_index, self._playback_highlight_row}
        affected = range(self.timeline_table.rowCount()) if rows is None else rows | marker_rows | self._timeline_marker_rows
        self._timeline_marker_rows = marker_rows
        for row in affected:
            if not 0 <= row < self.timeline_table.rowCount():
                continue
            playback_row = row == self._playback_highlight_row
            timing_cursor = (
                row == self.current_index
                and not self.preview_mode_check.isChecked()
            )
            for column in range(self.timeline_table.columnCount()):
                cell = self.timeline_table.item(row, column)
                if cell is None:
                    continue
                if playback_row:
                    cell.setBackground(QColor("#FFD54F"))
                    cell.setForeground(QColor("#202020"))
                elif timing_cursor:
                    cell.setBackground(QColor(255, 213, 79, 26))
                    cell.setForeground(
                        self.timeline_table.palette().brush(QPalette.ColorRole.Text)
                    )
                else:
                    cell.setBackground(QColor(0, 0, 0, 0))
                    cell.setForeground(
                        self.timeline_table.palette().brush(QPalette.ColorRole.Text)
                    )
                font = cell.font()
                font.setBold(playback_row)
                cell.setFont(font)
            item = self.timeline_table.item(row, 0)
            if item is None:
                continue
            markers: list[str] = []
            if playback_row:
                markers.append("♪")
            if timing_cursor:
                markers.append("▶")
            if row == selected:
                markers.append("●")
            prefix = " ".join(markers)
            item.setText(f"{prefix} {row + 1}" if prefix else str(row + 1))
            if playback_row:
                item.setToolTip(
                    "현재 재생 시간에 표시되는 가사입니다."
                    if self._korean else "Lyric displayed at the current playback time."
                )
            elif row == selected and timing_cursor:
                item.setToolTip(
                    "선택한 가사이며 다음 타이밍 기록 위치입니다."
                    if self._korean else
                    "Selected lyric and next timing-record position."
                )
            elif row == selected:
                item.setToolTip(
                    "현재 선택한 편집 대상 가사입니다."
                    if self._korean else "Currently selected lyric for editing."
                )
            elif timing_cursor:
                item.setToolTip(
                    "다음 타이밍을 기록할 가사" if self._korean else "Next lyric to receive timing"
                )
            else:
                item.setToolTip("")

    def _refresh_table(
        self,
        selected_row: int | None = None,
        *,
        center_timing_cursor: bool = False,
    ) -> None:
        previous_selection = self._selected_row()
        self._playback_timestamps = sorted(
            (float(timestamp), row) for row, timestamp in enumerate(self.timestamps)
            if timestamp is not None
        )
        self.timeline_table.blockSignals(True)
        self.timeline_table.setUpdatesEnabled(False)
        self.timeline_table.setRowCount(len(self.lines))
        changed_rows: set[int] = set()
        for row, line in enumerate(self.lines):
            timestamp = self.timestamps[row]
            values = (
                str(row + 1),
                LyricsService.lrc_timestamp(timestamp) if timestamp is not None else "--:--.--",
                line,
            )
            for column, value in enumerate(values):
                item = self.timeline_table.item(row, column)
                if item is None:
                    item = QTableWidgetItem(value)
                    self.timeline_table.setItem(row, column, item)
                    changed_rows.add(row)
                elif item.text() != value and column != 0:
                    item.setText(value)
                    changed_rows.add(row)
            line_count = max(1, len([part for part in line.splitlines() if part.strip()]))
            height = max(30, 22 * line_count + 8)
            if self.timeline_table.rowHeight(row) != height:
                self.timeline_table.setRowHeight(row, height)
        target = selected_row if selected_row is not None else previous_selection
        if 0 <= target < len(self.lines):
            self.timeline_table.selectRow(target)
            if not center_timing_cursor:
                self.timeline_table.scrollToItem(self.timeline_table.item(target, 0))
        elif 0 <= self.current_index < len(self.lines):
            self.timeline_table.scrollToItem(self.timeline_table.item(self.current_index, 0))
        if center_timing_cursor and 0 <= self.current_index < len(self.lines):
            self.timeline_table.scrollToItem(
                self.timeline_table.item(self.current_index, 0),
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )
        self.timeline_table.blockSignals(False)
        self._playback_highlight_row = self._playback_row_at(
            self._last_playback_position
        )
        self._apply_timeline_visuals(changed_rows)
        self.timeline_table.setUpdatesEnabled(True)
        self._update_enabled()

    def _save_lrc(self) -> None:
        cues = self.timed_cues()
        if not cues:
            self._set_status("저장할 타이밍이 없습니다." if self._korean else "There are no timestamps to save.", True)
            return
        missing = len(self.lines) - len(cues)
        if missing:
            answer = QMessageBox.warning(
                self,
                "미기록 가사" if self._korean else "Untimed lyrics",
                f"시간이 없는 {missing}개 줄을 제외하고 저장할까요?"
                if self._korean else f"Save while omitting {missing} untimed lines?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        default = (
            str(Path(self.audio_path).with_suffix(".lrc")) if self.audio_path
            else str(Path.cwd() / "lyrics.lrc")
        )
        selected, _ = QFileDialog.getSaveFileName(
            self, "LRC 파일 저장" if self._korean else "Save LRC file",
            default, "LRC lyrics (*.lrc)",
        )
        if not selected:
            return
        try:
            saved = LyricsService.save_lrc(
                selected, cues, title=self.title_edit.text(), artist=self.artist_edit.text(),
            )
        except LyricsError as error:
            self._set_status(str(error), True)
            return
        if saved not in self.saved_paths:
            self.saved_paths.append(saved)
        try:
            self._draft_service.clear(self.audio_path)
            self._draft_dirty = False
            self._draft_had_content_this_session = False
            self._draft_timer.stop()
            self.autosave_label.setText(
                "LRC 저장 완료 · 복구 초안 정리됨"
                if self._korean else "LRC saved · recovery draft cleared"
            )
        except LrcDraftError as error:
            self.autosave_label.setText(
                "복구 초안 정리 실패" if self._korean else "Could not clear recovery draft"
            )
            self.autosave_label.setToolTip(str(error))
        self._set_status(
            f"LRC 파일을 저장했습니다: {saved}" if self._korean else f"LRC file saved: {saved}"
        )

    def _set_volume(self, value: int) -> None:
        value = save_preview_volume(value)
        self.audio_output.setVolume(value / 100.0)
        self.volume_value.setText(f"{value}%")

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setText(
            ("일시정지" if self._korean else "Pause")
            if playing else ("재생" if self._korean else "Play")
        )
        self.play_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPause
            if playing else QStyle.StandardPixmap.SP_MediaPlay
        ))

    def _playback_error(self, _error: QMediaPlayer.Error, message: str = "") -> None:
        if message:
            self._set_status(
                f"오디오 재생 오류: {message}" if self._korean else f"Audio playback error: {message}", True
            )

    def _previous_step(self) -> None:
        current = self.pages.currentIndex()
        if current == 2 and self._timing_changes_present():
            action = self._confirm_timing_changes_before_back()
            if action == "cancel":
                return
            if action == "discard":
                self.lines = list(self._timing_baseline_lines)
                self.timestamps = list(self._timing_baseline_timestamps)
                self.current_index = self._next_unrecorded()
                self._undo_stack.clear()
                self._redo_stack.clear()
                self._refresh_table()
            self._sync_editor_from_lines()
            self._autosave_after_change()
        minimum = 1 if self.track_edit_mode else 0
        self.pages.setCurrentIndex(max(minimum, current - 1))

    def _timing_changes_present(self) -> bool:
        return (
            self.lines != self._timing_baseline_lines
            or self.timestamps != self._timing_baseline_timestamps
        )

    def _confirm_timing_changes_before_back(self) -> str:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(
            "타이밍 단계 변경사항" if self._korean else "Timing step changes"
        )
        box.setText(
            "타이밍 또는 가사가 변경되었습니다. 이전 단계로 돌아갈 때 변경한 가사를 "
            "입력란에 반영하거나, 이 단계의 변경사항을 모두 무시할 수 있습니다."
            if self._korean else
            "Timing or lyrics changed. You can carry the edited lyrics back to the input "
            "step, or discard every change made in this timing step."
        )
        keep_button = box.addButton(
            "변경 유지" if self._korean else "Keep changes",
            QMessageBox.ButtonRole.AcceptRole,
        )
        discard_button = box.addButton(
            "변경 무시" if self._korean else "Discard changes",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel_button = box.addButton(
            "취소" if self._korean else "Cancel",
            QMessageBox.ButtonRole.RejectRole,
        )
        box.setDefaultButton(keep_button)
        box.exec()
        if box.clickedButton() is keep_button:
            return "keep"
        if box.clickedButton() is discard_button:
            return "discard"
        if box.clickedButton() is cancel_button:
            return "cancel"
        return "cancel"

    def _sync_editor_from_lines(self) -> None:
        multiline = any("\n" in line for line in self.lines)
        if multiline:
            index = self.input_mode_combo.findData("multiline")
            if index >= 0:
                self.input_mode_combo.setCurrentIndex(index)
        separator = "\n\n" if self.input_mode_combo.currentData() == "multiline" else "\n"
        self.lyrics_editor.setPlainText(separator.join(self.lines))

    def _next_step(self) -> None:
        current = self.pages.currentIndex()
        if current == 0:
            if not self.audio_path and not self.lines:
                self._set_status(
                    "오디오 파일을 선택하세요." if self._korean else "Select an audio file to continue.",
                    True,
                )
                return
            if (self._applied_lyrics_track is not None
                    and not self._initial_cues_supplied
                    and self._lyrics_choice_audio != self.audio_path):
                track = self._applied_lyrics_track
                if needs_ttml_protection(track.lyrics, track.lyrics_path):
                    if self._load_applied_track_lyrics():
                        self._lyrics_choice_audio = self.audio_path
                        self.pages.setCurrentIndex(2)
                    return
                choice = self._existing_lyrics_choice()
                if choice == "cancel":
                    return
                self._lyrics_choice_audio = self.audio_path
                if choice == "load":
                    if not self._load_applied_track_lyrics():
                        self._lyrics_choice_audio = ""
                        return
                    self.pages.setCurrentIndex(2)
                    return
                self._clear_lyrics_for_new_draft()
        elif current == 1:
            if not self._prepare_lines():
                return
        elif current == 2:
            cues = self.timed_cues()
            if not cues:
                self._set_status(
                    "한 줄 이상의 타이밍을 기록하세요."
                    if self._korean else "Record timing for at least one lyric line.",
                    True,
                )
                return
            missing = len(self.lines) - len(cues)
            if missing:
                answer = QMessageBox.warning(
                    self,
                    "미기록 가사" if self._korean else "Untimed lyrics",
                    f"시간이 없는 가사 {missing}개는 결과에서 제외됩니다. 계속할까요?"
                    if self._korean else
                    f"{missing} untimed lyric line(s) will be omitted. Continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._refresh_review()
        self.pages.setCurrentIndex(min(self.pages.count() - 1, current + 1))

    def _step_changed(self, index: int) -> None:
        previous = self._last_step_index
        if index == 2 and previous == 1:
            self._timing_baseline_lines = list(self.lines)
            self._timing_baseline_timestamps = list(self.timestamps)
        review = index == self.pages.count() - 1
        self.back_button.setVisible(index > (0 if not self.track_edit_mode else 1))
        self.next_button.setVisible(not review)
        self.save_button.setVisible(review and not self.track_edit_mode)
        self.finish_button.setVisible(review)
        self.add_to_project_check.setVisible(review and not self.track_edit_mode)
        if review:
            self._refresh_review()
        self._refresh_step_header()
        self._sync_shortcuts_for_focus(None, self.focusWidget())
        self._update_enabled()
        self._last_step_index = index
        self._mark_draft_dirty()

    def _refresh_step_header(self) -> None:
        korean = self._korean
        index = self.pages.currentIndex()
        titles = (
            ("오디오 선택", "가사 입력", "타이밍 기록", "확인 및 저장")
            if korean else
            ("Select audio", "Enter lyrics", "Record timing", "Review and save")
        )
        descriptions = (
            (
                "프로젝트 콘텐츠 또는 컴퓨터에서 작업할 오디오를 선택하세요.",
                "가사 단위와 곡 정보를 입력한 뒤 다음 단계로 이동하세요.",
                "음악을 재생하면서 각 가사에 맞춰 Space 키를 누르세요.",
                "기록 결과를 확인하고 LRC로 저장하거나 현재 곡에 적용하세요.",
            )
            if korean else
            (
                "Choose an audio file from project content or this computer.",
                "Enter lyric units and song information, then continue.",
                "Play the audio and press Space at the timing of each lyric.",
                "Review the result, save an LRC file, or apply it to the current track.",
            )
        )
        workflow = (1, 2, 3) if self.track_edit_mode else (0, 1, 2, 3)
        display_index = workflow.index(index) if index in workflow else 0
        if self.track_edit_mode and index == 3:
            title = "확인 및 적용" if korean else "Review and apply"
            description = (
                "편집 결과를 확인하고 완료를 눌러 현재 곡에 적용하세요."
                if korean else
                "Review the result and choose Finish to apply it to the current track."
            )
        else:
            title = titles[index]
            description = descriptions[index]
        self.step_label.setText(
            f"{display_index + 1} / {len(workflow)} 단계"
            if korean else f"Step {display_index + 1} of {len(workflow)}"
        )
        self.step_title.setText(title)
        self.step_description.setText(description)
        self.step_dots.setText("  ".join(
            "●" if step == display_index else "○" for step in range(len(workflow))
        ))

    def _refresh_review(self) -> None:
        cues = self.timed_cues()
        missing = max(0, len(self.lines) - len(cues))
        duration = float(cues[-1]["start"]) if cues else 0.0
        self.review_summary.setText(
            f"기록된 가사 {len(cues)}개 · 미기록 {missing}개 · 마지막 타이밍 {self._clock(round(duration * 1000))}"
            if self._korean else
            f"{len(cues)} timed · {missing} untimed · last timing {self._clock(round(duration * 1000))}"
        )
        self.review_text.setPlainText(LyricsService.format_lrc(
            cues,
            title=self.title_edit.text(),
            artist=self.artist_edit.text(),
        ))

    def _update_enabled(self) -> None:
        has_audio = bool(self.audio_path)
        has_lines = bool(self.lines)
        has_timing = bool(self.timed_cues())
        selected = self._selected_row()
        selected_timed = 0 <= selected < len(self.timestamps) and self.timestamps[selected] is not None
        preview_mode = self.preview_mode_check.isChecked()
        for widget in (
            self.play_button, self.rewind_button, self.forward_button,
            self.stop_button, self.position_slider,
        ):
            widget.setEnabled(has_audio)
        timing_step = self.pages.currentIndex() == 2
        self.record_button.setEnabled(
            timing_step and not preview_mode and has_audio and has_lines
            and self.current_index < len(self.lines)
        )
        self.undo_button.setEnabled(not preview_mode and bool(self._undo_stack))
        self.redo_button.setEnabled(not preview_mode and bool(self._redo_stack))
        self.preview_mode_check.setEnabled(timing_step and has_audio and has_timing)
        self.save_button.setEnabled(not preview_mode and has_timing)
        self.finish_button.setEnabled(not preview_mode and has_timing)
        self.use_selected_button.setEnabled(not preview_mode and selected >= 0)
        self.clear_time_button.setEnabled(not preview_mode and selected_timed)
        self.nudge_back_button.setEnabled(not preview_mode and selected_timed)
        self.nudge_forward_button.setEnabled(not preview_mode and selected_timed)
        self.add_line_button.setEnabled(timing_step and not preview_mode)
        self.edit_line_button.setEnabled(
            timing_step and not preview_mode and selected >= 0
        )
        self.delete_line_button.setEnabled(
            timing_step and not preview_mode and selected >= 0
        )
        for widget in (
            self.reset_all_button, self.calibration_spin, self.shortcuts_button,
            self.back_button, self.next_button, self.add_to_project_check,
        ):
            widget.setEnabled(not preview_mode)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _update_input_help(self) -> None:
        multiline = self.input_mode_combo.currentData() == "multiline"
        if multiline:
            self.input_help.setText(
                "한 가사 안에서는 줄바꿈을 사용하고, 다음 가사는 빈 줄로 구분하세요."
                if self._korean else
                "Use line breaks inside one lyric and a blank line between lyric units."
            )
        else:
            self.input_help.setText(
                "가사를 한 줄에 하나씩 입력하세요."
                if self._korean else "Enter one lyric unit per line."
            )

    @property
    def _korean(self) -> bool:
        return self.translator.is_korean

    @staticmethod
    def _clock(milliseconds: int) -> str:
        hundredths = max(0, milliseconds) // 10
        minutes, remainder = divmod(hundredths, 6_000)
        seconds, fraction = divmod(remainder, 100)
        return f"{minutes:02d}:{seconds:02d}.{fraction:02d}"

    def keyPressEvent(self, event: QKeyEvent) -> None:
        focus = self.focusWidget()
        editing_text = focus in {
            self.lyrics_editor, self.title_edit, self.artist_edit,
            self.regex_filter_edit, self.local_audio_path_edit,
        }
        if (event.key() == Qt.Key.Key_Space and not editing_text
                and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and self.pages.currentIndex() == 2
                and not self.preview_mode_check.isChecked()):
            self._record_timestamp()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Undo) and not editing_text:
            self._undo_record()
            event.accept()
            return
        super().keyPressEvent(event)

    def _sync_shortcuts_for_focus(
        self, _old: QWidget | None, focus: QWidget | None,
    ) -> None:
        editing_text = (
            focus in {
                self.lyrics_editor, self.title_edit, self.artist_edit,
                self.regex_filter_edit, self.local_audio_path_edit,
            }
            or (focus is not None and self.lyrics_editor.isAncestorOf(focus))
        )
        timing_step = self.pages.currentIndex() == 2
        preview_mode = self.preview_mode_check.isChecked()
        self.record_shortcut.setEnabled(timing_step and not editing_text and not preview_mode)
        self.undo_shortcut.setEnabled(timing_step and not editing_text and not preview_mode)
        self.redo_shortcut.setEnabled(timing_step and not editing_text and not preview_mode)
        self.play_pause_shortcut.setEnabled(timing_step and not editing_text)
        self.seek_back_shortcut.setEnabled(timing_step and not editing_text)
        self.seek_forward_shortcut.setEnabled(timing_step and not editing_text)
        self.edit_line_shortcut.setEnabled(timing_step and not editing_text and not preview_mode)
        self.delete_line_shortcut.setEnabled(timing_step and not editing_text and not preview_mode)

    def _clear_recovery_draft(self) -> bool:
        self._draft_timer.stop()
        self._draft_dirty = False
        self._draft_had_content_this_session = False
        if not self.audio_path:
            return True
        try:
            self._draft_service.clear(self.audio_path)
        except LrcDraftError as error:
            self.autosave_label.setText(
                "복구 초안 정리 실패"
                if self._korean else "Could not clear recovery draft"
            )
            self.autosave_label.setToolTip(str(error))
            return False
        return True

    def reject(self) -> None:
        if self.pages.currentIndex() >= (1 if not self.track_edit_mode else 1):
            answer = QMessageBox.warning(
                self,
                "가사/자막 편집기 닫기" if self._korean else "Close Lyrics/Subtitle Editor",
                (
                    "현재 가사/자막 편집 작업을 닫을까요?\n\n"
                    "정상적으로 닫으면 자동 저장된 복구 내역도 삭제됩니다. "
                    "자동 복구는 프로그램이 비정상적으로 종료된 경우에만 제공됩니다."
                    if self._korean else
                    "Close the current lyrics/subtitle editing work?\n\n"
                    "Closing normally also deletes its autosaved recovery data. "
                    "Automatic recovery is kept only after an abnormal program termination."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.done(QDialog.DialogCode.Rejected)

    def done(self, result: int) -> None:
        self._draft_timer.stop()
        self._draft_periodic_timer.stop()
        self._playback_scroll_resume_timer.stop()
        # Reaching done() means the dialog/app shut down normally. Recovery
        # drafts deliberately survive only crashes or forced termination where
        # this normal cleanup path never runs.
        self._clear_recovery_draft()
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        if self._shortcuts_dialog is not None:
            self._shortcuts_dialog.close()
        super().done(result)

    def retranslate(self) -> None:
        korean = self._korean
        self.setWindowTitle("가사/자막 편집기" if korean else "Lyrics/Subtitle Editor")
        self.audio_group.setTitle("오디오 파일" if korean else "Audio file")
        local_index = self.project_audio_combo.findData("__local__")
        if local_index >= 0:
            self.project_audio_combo.setItemText(
                local_index,
                "로컬에서 선택하기…" if korean else "Choose from this computer…",
            )
        self.local_audio_path_edit.setPlaceholderText(
            "오디오 파일을 선택하세요." if korean else "Choose an audio file."
        )
        self.browse_audio_button.setText("찾아보기…" if korean else "Browse…")
        self.audio_path_label.setText(
            self.audio_path or ("선택된 오디오가 없습니다." if korean else "No audio selected.")
        )
        self.input_mode_label.setText("가사 단위" if korean else "Lyric unit mode")
        self.input_mode_combo.setItemText(
            0, "한 줄마다 한 가사" if korean else "One line per lyric"
        )
        self.input_mode_combo.setItemText(
            1, "빈 줄마다 한 가사 (여러 줄 지원)"
            if korean else "One lyric per blank-line block (multi-line)"
        )
        self._update_input_help()
        self.lyrics_editor.setPlaceholderText(
            "첫 번째 가사\n두 번째 가사\n세 번째 가사\n\n※ 여러 줄 모드에서는 빈 줄로 다음 가사를 구분하세요."
            if korean else
            "First lyric\nSecond lyric\nThird lyric\n\nIn multi-line mode, a blank line starts the next lyric."
        )
        self.title_label.setText("곡 제목" if korean else "Title")
        self.artist_label.setText("아티스트" if korean else "Artist")
        metadata_help = (
            "선택한 오디오의 메타데이터에서 자동으로 가져옵니다."
            if korean else "Automatically read from the selected audio metadata."
        )
        self.title_edit.setToolTip(metadata_help)
        self.artist_edit.setToolTip(metadata_help)
        self.filter_group.setTitle("입력 전처리 옵션" if korean else "Input preprocessing")
        self.ignore_line_breaks_check.setText(
            "빈 줄 구분 무시 (모든 줄을 개별 가사로 처리)"
            if korean else "Ignore blank-line grouping (treat every line as one lyric)"
        )
        self.ignore_bracketed_lines_check.setText(
            "[ ]로 묶인 줄 무시" if korean else "Ignore lines enclosed in [ ]"
        )
        self.advanced_filter_check.setText(
            "고급 설정: 정규식으로 각 줄의 내용 제거"
            if korean else "Advanced: remove matching text from each line with a regex"
        )
        self.regex_filter_label.setText("정규식" if korean else "Regular expression")
        self.regex_filter_edit.setPlaceholderText(
            r"예: ^\d+\.\s* 또는 \(.*?\)" if korean else r"Example: ^\d+\.\s* or \(.*?\)"
        )
        headers = ["#", "시간", "가사"] if korean else ["#", "Time", "Lyric"]
        self.timeline_table.setHorizontalHeaderLabels(headers)
        self.record_button.setText("현재 줄 기록  [Space]" if korean else "Record current line  [Space]")
        self.history_tools_group.setTitle("실행 이력" if korean else "History")
        self.timing_tools_group.setTitle("선택 시간 조정" if korean else "Selected timing")
        self.lyric_tools_group.setTitle("가사 편집" if korean else "Lyric editing")
        tool_texts = (
            (self.undo_button, "실행 취소", "Undo", "기록 취소 (Ctrl+Z)", "Undo timing (Ctrl+Z)"),
            (self.redo_button, "다시 실행", "Redo", "다시 실행 (Ctrl+Y)", "Redo timing (Ctrl+Y)"),
            (self.use_selected_button, "선택 줄을 기록 위치로", "Use selected as cursor", "선택한 줄을 다음 타이밍 기록 위치로 지정", "Use selected row as the next timestamp position"),
            (self.nudge_back_button, "−0.1초", "−0.1s", "선택 시간을 0.10초 앞으로", "Move selected time 0.10s earlier"),
            (self.nudge_forward_button, "+0.1초", "+0.1s", "선택 시간을 0.10초 뒤로", "Move selected time 0.10s later"),
            (self.clear_time_button, "시간 삭제", "Clear time", "선택한 줄의 기록 시간 삭제", "Clear selected timestamp"),
            (self.reset_all_button, "전체 초기화", "Reset all", "모든 타이밍 초기화", "Reset all timestamps"),
            (self.add_line_button, "가사 추가", "Add lyric", "선택한 줄 다음에 새 가사 추가", "Add a lyric after the selected row"),
            (self.edit_line_button, "가사 편집", "Edit lyric", "가사 편집 (F2)", "Edit lyric (F2)"),
            (self.delete_line_button, "가사 삭제", "Delete lyric", "가사 삭제 (Delete)", "Delete lyric (Delete)"),
        )
        for button, ko_label, en_label, ko_tip, en_tip in tool_texts:
            label = ko_label if korean else en_label
            tip = ko_tip if korean else en_tip
            button.setText(label)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
        self.timing_cursor_help.setText(
            "▶ 희미한 행: 다음 기록 · ♪ 노란 행: 현재 가사 · ● 원: 선택"
            if korean else
            "▶ Faint row: next timestamp · ♪ Yellow row: current lyric · ● Circle: selection"
        )
        self.calibration_label.setText("입력 지연 보정" if korean else "Input latency offset")
        self.calibration_help.setText(
            "버튼을 늦게 누르는 편이면 음수 값을 사용하세요."
            if korean else "Use a negative value if you tend to press the button late."
        )
        self.preview_group.setTitle(
            "음악 재생" if korean else "Music playback"
        )
        self.rewind_button.setText("−3초" if korean else "−3s")
        self.rewind_button.setToolTip(
            "3초 뒤로 이동 (← 키는 1초)" if korean else
            "Seek back 3 seconds (Left seeks 1 second)"
        )
        self.forward_button.setText("+3초" if korean else "+3s")
        self.forward_button.setToolTip(
            "3초 앞으로 이동 (→ 키는 1초)" if korean else
            "Seek forward 3 seconds (Right seeks 1 second)"
        )
        self.stop_button.setText("정지" if korean else "Stop")
        self.play_button.setToolTip(
            "재생 또는 일시정지 (Ctrl+Space)" if korean else
            "Play or pause (Ctrl+Space)"
        )
        self.preview_mode_check.setText(
            "미리보기 모드" if korean else "Preview mode"
        )
        self.preview_mode_check.setToolTip(
            "편집을 잠그고 현재 재생 가사를 표 중앙에서 따라갑니다."
            if korean else
            "Lock editing and follow the current playback lyric at the table center."
        )
        self.shortcuts_button.setText("단축키 안내" if korean else "Shortcuts")
        self.volume_label.setText("볼륨" if korean else "Volume")
        self.add_to_project_check.setText(
            "저장한 LRC를 프로젝트 콘텐츠에 추가" if korean else "Add saved LRC to project content"
        )
        self.review_help.setText(
            (
                "완료를 누르면 편집한 가사와 타이밍이 현재 곡에 바로 적용됩니다."
                if korean else
                "Choose Finish to apply the edited lyrics and timing directly to the current track."
            )
            if self.track_edit_mode else
            (
                "LRC 저장은 파일을 생성하며, 완료는 현재 편집 결과를 곡/가사 설정으로 전달합니다."
                if korean else
                "Save LRC creates a file; Finish sends the current result back to track/lyrics settings."
            )
        )
        self.back_button.setText("< 이전" if korean else "< Back")
        self.next_button.setText("다음 >" if korean else "Next >")
        self.save_button.setText("LRC 저장…" if korean else "Save LRC…")
        self.finish_button.setText("완료" if korean else "Finish")
        self.close_button.setText("취소" if korean else "Cancel")
        self.autosave_label.setToolTip(
            "가사 입력과 타이밍 변경은 오디오별 복구 초안으로 자동 저장됩니다."
            if korean else
            "Lyric input and timing changes are autosaved as an audio-specific recovery draft."
        )
        self._playback_state_changed(self.media_player.playbackState())
        self._refresh_step_header()
        if self.pages.currentIndex() == self.pages.count() - 1:
            self._refresh_review()
        self._refresh_table()
