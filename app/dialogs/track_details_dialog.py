"""Track metadata, lyrics attachment, and per-track synchronization editor."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import subprocess
import sys
import threading

from mutagen import File as MutagenFile
from PySide6.QtCore import QProcess, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.automix.analysis.key import key_to_camelot
from app.automix.models import TrackAnalysis
from app.automix.structure.models import TrackStructureAnalysis
from app.dialogs.help_dialog import install_help_shortcut
from app.models.playlist import (
    EQ_BANDS_HZ, EQ_LIMIT_DB, VOLUME_RANGE_DB, PlaylistTrack, track_audio_filter,
)
from app.widgets.activity_progress import activity_for
from app.widgets.audio_sync_warning import AudioSyncWarning
from app.widgets.track_analysis_panel import TrackAnalysisPanel
from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog
from app.services.lyrics_service import LyricsError, LyricsService
from app.services.preview_audio_settings import preview_volume, save_preview_volume
from app.preview.album_art import extract_track_cover
from app.utils.i18n import Translator
from app.utils.subprocess_utils import hidden_process_kwargs
from app.utils.time_format import format_clock

EQ_PREVIEW_LIMITER = "alimiter=limit=0.95:level=0"
"""A boost past full scale would clip in the preview file; the export has float headroom instead."""
EQ_ACTIVITY = "track_eq_preview"


def audio_file_facts(path: str) -> dict[str, object]:
    """Container/stream facts for the file tab; empty when the file cannot be read."""
    facts: dict[str, object] = {}
    source = Path(path)
    try:
        facts["size"] = source.stat().st_size
    except OSError:
        return facts
    facts["format"] = source.suffix.lstrip(".").upper()
    try:
        info = getattr(MutagenFile(source), "info", None)
    except Exception:  # noqa: BLE001 - mutagen raises format-specific errors; the tab just shows less
        info = None
    for name in ("bitrate", "sample_rate", "channels", "bits_per_sample"):
        value = getattr(info, name, None)
        if isinstance(value, (int, float)) and value > 0:
            facts[name] = value
    return facts


_TAB_HELP_TOPICS = ("track_info", "track_analysis", "track_lyrics", "track_videos", "track_audio")
"""F1 opens the help topic of the tab being shown (tabs in this order)."""


class TrackDetailsDialog(QDialog):
    """Edit timed lyrics and synchronization without mutating the track on Cancel."""

    analysis_requested = Signal()
    """The user asked to analyze this track (MainWindow runs it and reports back)."""
    eq_rendered = Signal(int, str, str, str)
    """generation, filter, rendered file ("" on failure), error -- from the EQ preview thread."""

    def __init__(
        self, track: PlaylistTrack, translator: Translator,
        parent: QWidget | None = None, *,
        content_lyrics: list[tuple[str, str]] | None = None,
        analysis: TrackAnalysis | None = None,
        structure: TrackStructureAnalysis | None = None,
        ffmpeg_executable: Path | None = None,
    ) -> None:
        super().__init__(parent)
        install_help_shortcut(
            self, lambda dialog: ("track", _TAB_HELP_TOPICS[max(0, dialog.tabs.currentIndex())]), translator,
        )
        self.track = track
        # EQ preview: the player swaps to a copy of the track rendered with the
        # current volume/EQ (Qt Multimedia cannot filter audio itself).
        self.ffmpeg_executable = ffmpeg_executable
        self._eq_dir: TemporaryDirectory | None = None
        self._eq_generation = 0
        self._eq_playing_filter = ""
        self._eq_rendering_filter = ""
        self._eq_state = "original"
        self._eq_error = ""
        self._pending_resume: tuple[int, bool] | None = None
        self._eq_timer = QTimer(self)
        self._eq_timer.setSingleShot(True)
        self._eq_timer.setInterval(400)
        self._eq_timer.timeout.connect(self._update_audio_source)
        self.eq_rendered.connect(self._eq_render_finished)
        self.translator = translator
        self.analysis = analysis
        self._analysis_error = ""
        # (display name, resolved path) for every lyrics/subtitle file already
        # in the project content library.
        self._content_lyrics = list(content_lyrics or [])
        self.selected_lyrics_path = track.lyrics_path
        self.selected_lyrics = [cue.copy() for cue in track.lyrics]
        self.selected_timing_offset = float(track.lyrics_timing_offset_seconds)
        self.selected_title = track.title
        self.selected_artist = track.artist
        self.selected_album = track.album
        self.selected_cover_path = track.cover_path
        self.selected_video_paths = list(track.video_paths)
        self.selected_volume_db = float(track.volume_db)
        self.selected_eq_db = [float(gain) for gain in track.eq_db]
        saved_volume = preview_volume()
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(saved_volume / 100.0)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self._audio_available = Path(track.file_path).is_file()
        if self._audio_available:
            self.media_player.setSource(QUrl.fromLocalFile(str(Path(track.file_path).resolve())))
        self._file_facts = audio_file_facts(track.file_path)
        self.setMinimumSize(780, 540)
        self.resize(880, 640)  # fits small laptop screens; the Analysis tab scrolls

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        root.addWidget(self._build_header())
        self.audio_sync_warning = AudioSyncWarning(self, audio_output=self.audio_output,
                                                  media_player=self.media_player, translator=translator)
        root.addWidget(self.audio_sync_warning)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("trackDetailsTabs")
        self.info_tab = QWidget()
        self.lyrics_tab = QWidget()
        self.video_tab = QWidget()
        info_tab_layout = QHBoxLayout(self.info_tab)
        info_tab_layout.setContentsMargins(12, 12, 12, 12)
        info_tab_layout.setSpacing(14)
        info_column = QVBoxLayout()
        info_column.setSpacing(10)

        self.info_group = QGroupBox()
        info_form = QFormLayout(self.info_group)
        info_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        info_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.info_name_labels: list[QLabel] = []
        self.title_edit = QLineEdit(track.title)
        self.artist_edit = QLineEdit(track.artist)
        self.album_edit = QLineEdit(track.album)
        self.metadata_edits = (self.title_edit, self.artist_edit, self.album_edit)
        self.info_labels = list(self.metadata_edits)
        for field in self.metadata_edits:
            field.setObjectName("trackMetadataEdit")
            field.setClearButtonEnabled(True)
            field.setPlaceholderText("—")
            field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            field.textChanged.connect(self._refresh_header)
            name_label = QLabel()
            name_label.setObjectName("trackInfoName")
            name_label.setMinimumWidth(78)
            self.info_name_labels.append(name_label)
            info_form.addRow(name_label, field)
        info_column.addWidget(self.info_group)

        self.file_group = QGroupBox()
        file_form = QFormLayout(self.file_group)
        file_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        file_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.file_label = QLabel(track.file_path.strip() or "—")
        self.file_label.setToolTip(track.file_path)
        self.reveal_file_button = QPushButton()
        self.reveal_file_button.setEnabled(self._audio_available)
        self.reveal_file_button.clicked.connect(self._reveal_audio_file)
        path_widget = QWidget()
        path_row = QHBoxLayout(path_widget)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self.file_label, 1)
        path_row.addWidget(self.reveal_file_button, 0, Qt.AlignmentFlag.AlignTop)
        self.duration_label = QLabel(track.duration_label)
        self.format_label = QLabel()
        self.quality_label = QLabel()
        self.size_label = QLabel()
        self.file_name_labels: list[QLabel] = []
        for field in (path_widget, self.duration_label, self.format_label, self.quality_label, self.size_label):
            name_label = QLabel()
            name_label.setObjectName("trackInfoName")
            name_label.setMinimumWidth(78)
            self.file_name_labels.append(name_label)
            file_form.addRow(name_label, field)
        for field in (self.file_label, self.duration_label, self.format_label, self.quality_label, self.size_label):
            field.setObjectName("trackInfoValue")
            field.setWordWrap(True)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info_column.addWidget(self.file_group)
        info_column.addStretch(1)
        info_tab_layout.addLayout(info_column, 3)

        self.cover_group = QGroupBox()
        cover_layout = QVBoxLayout(self.cover_group)
        cover_layout.setContentsMargins(12, 14, 12, 12)
        cover_layout.setSpacing(8)
        self.cover_preview = QLabel()
        self.cover_preview.setObjectName("trackCoverPreview")
        self.cover_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_preview.setFixedSize(220, 220)
        self.cover_preview.setFrameShape(QFrame.Shape.StyledPanel)
        self.cover_preview.setScaledContents(False)
        self.cover_source_label = QLabel()
        self.cover_source_label.setObjectName("mutedLabel")
        self.cover_source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_source_label.setWordWrap(True)
        cover_actions = QHBoxLayout()
        self.change_cover_button = QPushButton()
        self.reset_cover_button = QPushButton()
        cover_actions.addWidget(self.change_cover_button)
        cover_actions.addWidget(self.reset_cover_button)
        cover_layout.addWidget(self.cover_preview, 0, Qt.AlignmentFlag.AlignHCenter)
        cover_layout.addWidget(self.cover_source_label)
        cover_layout.addLayout(cover_actions)
        cover_layout.addStretch(1)
        info_tab_layout.addWidget(self.cover_group, 2)

        self.lyrics_group = QGroupBox()
        lyrics_layout = QVBoxLayout(self.lyrics_group)
        path_row = QHBoxLayout()
        self.lyrics_path = QLabel()
        self.lyrics_path.setObjectName("mutedLabel")
        self.lyrics_path.setWordWrap(True)
        self.load_button = QPushButton()
        self.search_lyrics_button = QPushButton()
        self.content_lyrics_button = QPushButton()
        self.content_lyrics_button.setVisible(bool(self._content_lyrics))
        self.edit_lrc_button = QPushButton()
        self.export_lrc_button = QPushButton()
        self.clear_button = QPushButton()
        path_row.addWidget(self.lyrics_path, 1)
        path_row.addWidget(self.content_lyrics_button)
        path_row.addWidget(self.load_button)
        path_row.addWidget(self.clear_button)
        lyrics_layout.addLayout(path_row)
        lyrics_action_row = QHBoxLayout()
        lyrics_action_row.addWidget(self.search_lyrics_button)
        lyrics_action_row.addStretch(1)
        lyrics_action_row.addWidget(self.edit_lrc_button)
        lyrics_action_row.addWidget(self.export_lrc_button)
        lyrics_layout.addLayout(lyrics_action_row)

        timing_form = QFormLayout()
        self.timing_label = QLabel()
        self.timing_offset_spin = QDoubleSpinBox()
        self.timing_offset_spin.setRange(-30.0, 30.0)
        self.timing_offset_spin.setDecimals(2)
        self.timing_offset_spin.setSingleStep(0.05)
        self.timing_offset_spin.setSuffix(" s")
        self.timing_offset_spin.setKeyboardTracking(False)
        self.timing_offset_spin.setValue(self.selected_timing_offset)
        timing_controls = QWidget()
        timing_controls_layout = QHBoxLayout(timing_controls)
        timing_controls_layout.setContentsMargins(0, 0, 0, 0)
        timing_controls_layout.setSpacing(5)
        self.earlier_button = QPushButton("−0.10")
        self.later_button = QPushButton("+0.10")
        self.reset_button = QPushButton()
        timing_controls_layout.addWidget(self.timing_offset_spin, 1)
        timing_controls_layout.addWidget(self.earlier_button)
        timing_controls_layout.addWidget(self.later_button)
        timing_controls_layout.addWidget(self.reset_button)
        timing_form.addRow(self.timing_label, timing_controls)
        lyrics_layout.addLayout(timing_form)

        self.timing_help = QLabel()
        self.timing_help.setObjectName("mutedLabel")
        self.timing_help.setWordWrap(True)
        lyrics_layout.addWidget(self.timing_help)

        self.playback_group = QGroupBox()
        playback_layout = QVBoxLayout(self.playback_group)
        playback_layout.setSpacing(7)
        self.playback_status = QLabel()
        self.playback_status.setObjectName("mutedLabel")
        self.playback_status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lyrics_preview_host = QWidget()
        self.lyrics_preview_layout = QVBoxLayout(self.lyrics_preview_host)
        self.lyrics_preview_layout.setContentsMargins(4, 0, 4, 0)
        self.lyrics_preview_layout.setSpacing(3)
        self.previous_lyric = QLabel()
        self.previous_lyric.setObjectName("mutedLabel")
        self.previous_lyric.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.previous_lyric.setWordWrap(True)
        self.previous_lyric.setMaximumHeight(54)
        self.current_lyric = QLabel()
        self.current_lyric.setObjectName("panelTitle")
        self.current_lyric.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.current_lyric.setWordWrap(True)
        self.current_lyric.setMinimumHeight(42)
        self.current_lyric.setMaximumHeight(84)
        self.next_lyric = QLabel()
        self.next_lyric.setObjectName("mutedLabel")
        self.next_lyric.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_lyric.setWordWrap(True)
        self.next_lyric.setMaximumHeight(54)
        self.lyrics_preview_layout.addWidget(self.previous_lyric)
        self.lyrics_preview_layout.addWidget(self.current_lyric)
        self.lyrics_preview_layout.addWidget(self.next_lyric)
        playback_layout.addWidget(self.playback_status)
        playback_layout.addStretch(1)
        playback_layout.addWidget(self.lyrics_preview_host)
        playback_layout.addStretch(1)

        transport_row = QHBoxLayout()
        self.play_button = QPushButton()
        self.stop_button = QPushButton()
        self.playback_slider = QSlider(Qt.Orientation.Horizontal)
        self.playback_slider.setObjectName("trackPlaybackSlider")
        self.playback_slider.setRange(0, max(1, round(track.duration_seconds * 1000)))
        self.playback_slider.setSingleStep(100)
        self.playback_slider.setPageStep(5_000)
        self.playback_slider.setTracking(True)
        self.playback_slider.setMinimumWidth(220)
        self.playback_slider.setMinimumHeight(30)
        self.playback_slider.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.playback_time = QLabel()
        self.playback_time.setObjectName("mutedLabel")
        self.playback_time.setMinimumWidth(78)
        self.playback_time.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        transport_row.addWidget(self.play_button)
        transport_row.addWidget(self.stop_button)
        transport_row.addStretch(1)
        transport_row.addWidget(self.playback_time)
        playback_layout.addLayout(transport_row)
        playback_layout.addWidget(self.playback_slider)

        volume_row = QHBoxLayout()
        self.volume_label = QLabel()
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(saved_volume)
        self.volume_value = QLabel(f"{saved_volume}%")
        self.volume_value.setObjectName("mutedLabel")
        self.volume_value.setMinimumWidth(42)
        self.volume_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        volume_row.addWidget(self.volume_label)
        volume_row.addWidget(self.volume_slider, 1)
        volume_row.addWidget(self.volume_value)
        playback_layout.addLayout(volume_row)

        self.cue_summary = QLabel()
        self.cue_summary.setObjectName("mutedLabel")
        lyrics_layout.addWidget(self.cue_summary)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        lyrics_layout.addWidget(self.preview, 1)
        content_row = QHBoxLayout()
        content_row.setSpacing(10)
        content_row.addWidget(self.lyrics_group, 5)
        content_row.addWidget(self.playback_group, 4)
        lyrics_tab_layout = QVBoxLayout(self.lyrics_tab)
        lyrics_tab_layout.setContentsMargins(10, 10, 10, 10)
        lyrics_tab_layout.addLayout(content_row, 1)
        self.tabs.addTab(self.info_tab, "")
        self.analysis_panel = TrackAnalysisPanel()
        self.analysis_panel.set_results(analysis, structure)
        self.analysis_panel.analyze_requested.connect(self.analysis_requested)
        self.analysis_tab = QScrollArea()
        self.analysis_tab.setWidgetResizable(True)
        self.analysis_tab.setFrameShape(QFrame.Shape.NoFrame)
        self.analysis_tab.setWidget(self.analysis_panel)
        self.tabs.addTab(self.analysis_tab, "")
        self.tabs.addTab(self.lyrics_tab, "")
        video_layout = QVBoxLayout(self.video_tab)
        video_layout.setContentsMargins(12, 12, 12, 12)
        video_layout.setSpacing(10)
        self.video_scope_badge = QLabel()
        self.video_scope_badge.setObjectName("trackVideoScopeBadge")
        self.video_scope_badge.setWordWrap(True)
        self.video_scope_badge.setStyleSheet(
            "background: rgba(121, 199, 180, 0.12); "
            "border: 1px solid rgba(121, 199, 180, 0.35); "
            "border-radius: 7px; padding: 8px 10px; font-weight: 700;"
        )
        self.video_help = QLabel()
        self.video_help.setObjectName("mutedLabel")
        self.video_help.setWordWrap(True)
        self.video_list_group = QGroupBox()
        video_list_layout = QVBoxLayout(self.video_list_group)
        video_list_header = QHBoxLayout()
        self.video_count_label = QLabel()
        self.video_count_label.setObjectName("mutedLabel")
        video_list_header.addStretch(1)
        video_list_header.addWidget(self.video_count_label)
        video_list_layout.addLayout(video_list_header)
        self.video_list = QListWidget()
        self.video_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for video_path in self.selected_video_paths:
            self.video_list.addItem(video_path)
        self.video_empty_label = QLabel()
        self.video_empty_label.setObjectName("mutedLabel")
        self.video_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_empty_label.setWordWrap(True)
        video_actions = QHBoxLayout()
        self.add_video_button = QPushButton()
        self.remove_video_button = QPushButton()
        self.video_up_button = QPushButton()
        self.video_down_button = QPushButton()
        video_actions.addWidget(self.add_video_button)
        video_actions.addWidget(self.remove_video_button)
        video_actions.addWidget(self.video_up_button)
        video_actions.addWidget(self.video_down_button)
        video_actions.addStretch(1)
        video_list_layout.addWidget(self.video_empty_label)
        video_list_layout.addWidget(self.video_list, 1)
        video_list_layout.addLayout(video_actions)
        video_layout.addWidget(self.video_scope_badge)
        video_layout.addWidget(self.video_help)
        video_layout.addWidget(self.video_list_group, 1)
        self.tabs.addTab(self.video_tab, "")
        self.tabs.addTab(self._build_audio_tab(), "")
        root.addWidget(self.tabs, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self.load_button.clicked.connect(self._load_lyrics)
        self.content_lyrics_button.clicked.connect(self._show_content_lyrics_menu)
        self.search_lyrics_button.clicked.connect(self._find_lyrics)
        self.edit_lrc_button.clicked.connect(self._edit_in_lrc_generator)
        self.export_lrc_button.clicked.connect(self._export_current_lyrics_as_lrc)
        self.clear_button.clicked.connect(self._clear_lyrics)
        self.change_cover_button.clicked.connect(self._choose_cover)
        self.reset_cover_button.clicked.connect(self._reset_cover)
        self.add_video_button.clicked.connect(self._add_track_videos)
        self.remove_video_button.clicked.connect(self._remove_track_videos)
        self.video_up_button.clicked.connect(lambda: self._move_track_video(-1))
        self.video_down_button.clicked.connect(lambda: self._move_track_video(1))
        self.video_list.itemSelectionChanged.connect(self._update_track_video_ui)
        self.video_list.currentRowChanged.connect(self._update_track_video_ui)
        self.earlier_button.clicked.connect(lambda: self._nudge_timing(-0.1))
        self.later_button.clicked.connect(lambda: self._nudge_timing(0.1))
        self.reset_button.clicked.connect(lambda: self.timing_offset_spin.setValue(0.0))
        self.timing_offset_spin.valueChanged.connect(self._refresh_preview)
        self.play_button.clicked.connect(self._toggle_playback)
        self.stop_button.clicked.connect(self._stop_playback)
        self.volume_slider.valueChanged.connect(self._set_volume)
        self.playback_slider.sliderMoved.connect(self._playback_position_changed)
        self.playback_slider.sliderReleased.connect(self._seek_playback)
        self.media_player.positionChanged.connect(self._playback_position_changed)
        self.media_player.durationChanged.connect(self._playback_duration_changed)
        self.media_player.playbackStateChanged.connect(self._playback_state_changed)
        self.media_player.errorOccurred.connect(self._playback_error)
        self.media_player.mediaStatusChanged.connect(self._media_status_changed)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()
        self._refresh_cover()
        self._refresh_preview()
        self._playback_duration_changed(round(track.duration_seconds * 1000))
        self._update_track_video_ui()
        self._eq_timer.stop()
        self._update_audio_source()  # a saved EQ plays from the first Play

    # -- header ---------------------------------------------------------------

    def _build_header(self) -> QFrame:
        """Cover, title, artist · album and one line of at-a-glance facts, above every tab."""
        header = QFrame()
        header.setObjectName("trackDetailsHeader")
        header.setStyleSheet(
            "#trackDetailsHeader { border: 1px solid rgba(128, 128, 128, 0.3);"
            " border-radius: 10px; background: rgba(128, 128, 128, 0.07); }"
        )
        layout = QHBoxLayout(header)
        layout.setContentsMargins(10, 10, 14, 10)
        layout.setSpacing(12)
        self.header_cover = QLabel()
        self.header_cover.setFixedSize(68, 68)
        self.header_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.header_title = QLabel()
        self.header_title.setObjectName("panelTitle")
        self.header_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.header_subtitle = QLabel()
        self.header_facts = QLabel()
        self.header_facts.setObjectName("mutedLabel")
        self.header_facts.setWordWrap(True)
        for label in (self.header_title, self.header_subtitle, self.header_facts):
            text.addWidget(label)
        text.addStretch(1)
        layout.addWidget(self.header_cover, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text, 1)
        return header

    # -- audio: volume and EQ -----------------------------------------------------

    def _db_slider(self, orientation: Qt.Orientation, limits: tuple[float, float], value: float) -> QSlider:
        """A slider in 0.5 dB steps."""
        slider = QSlider(orientation)
        slider.setRange(round(limits[0] * 2), round(limits[1] * 2))
        slider.setPageStep(2)
        slider.setValue(round(value * 2))
        slider.valueChanged.connect(self._refresh_audio_labels)
        return slider

    def _build_audio_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.track_volume_group = QGroupBox()
        volume_row = QHBoxLayout(self.track_volume_group)
        self.track_volume_slider = self._db_slider(
            Qt.Orientation.Horizontal, VOLUME_RANGE_DB, self.selected_volume_db,
        )
        self.track_volume_value = QLabel()
        self.track_volume_value.setMinimumWidth(64)
        self.track_volume_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        volume_row.addWidget(self.track_volume_slider, 1)
        volume_row.addWidget(self.track_volume_value)

        self.eq_group = QGroupBox()
        eq_row = QHBoxLayout(self.eq_group)
        eq_row.setSpacing(18)
        gains = self.selected_eq_db + [0.0] * (len(EQ_BANDS_HZ) - len(self.selected_eq_db))
        self.eq_sliders: list[QSlider] = []
        self.eq_values: list[QLabel] = []
        for frequency, gain in zip(EQ_BANDS_HZ, gains):
            column = QVBoxLayout()
            value = QLabel()
            value.setObjectName("mutedLabel")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slider = self._db_slider(Qt.Orientation.Vertical, (-EQ_LIMIT_DB, EQ_LIMIT_DB), gain)
            slider.setMinimumHeight(160)
            name = QLabel(f"{frequency // 1000}k" if frequency >= 1000 else str(frequency))
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(value)
            column.addWidget(slider, 1, Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(name)
            eq_row.addLayout(column)
            self.eq_sliders.append(slider)
            self.eq_values.append(value)

        actions = QHBoxLayout()
        self.eq_bypass_check = QCheckBox()
        self.eq_bypass_check.toggled.connect(self._update_audio_source)
        actions.addWidget(self.eq_bypass_check)
        actions.addStretch(1)
        self.reset_audio_button = QPushButton()
        self.reset_audio_button.clicked.connect(self._reset_audio)
        actions.addWidget(self.reset_audio_button)

        # The same player as the Lyrics tab, with its own transport here.
        self.audition_group = QGroupBox()
        audition = QVBoxLayout(self.audition_group)
        transport = QHBoxLayout()
        self.audio_play_button = QPushButton()
        self.audio_stop_button = QPushButton()
        self.audio_play_button.clicked.connect(self._toggle_playback)
        self.audio_stop_button.clicked.connect(self._stop_playback)
        self.audio_time = QLabel()
        self.audio_time.setObjectName("mutedLabel")
        self.eq_status = QLabel()
        self.eq_status.setObjectName("mutedLabel")
        transport.addWidget(self.audio_play_button)
        transport.addWidget(self.audio_stop_button)
        transport.addWidget(self.eq_status, 1)
        transport.addWidget(self.audio_time)
        self.audio_position_slider = QSlider(Qt.Orientation.Horizontal)
        self.audio_position_slider.setRange(0, self.playback_slider.maximum())
        self.audio_position_slider.setPageStep(5_000)
        self.audio_position_slider.sliderMoved.connect(self._playback_position_changed)
        self.audio_position_slider.sliderReleased.connect(self._seek_playback)
        audition.addLayout(transport)
        audition.addWidget(self.audio_position_slider)

        self.audio_help = QLabel()
        self.audio_help.setObjectName("mutedLabel")
        self.audio_help.setWordWrap(True)
        layout.addWidget(self.track_volume_group)
        layout.addWidget(self.eq_group, 1)
        layout.addLayout(actions)
        layout.addWidget(self.audition_group)
        layout.addWidget(self.audio_help)
        self._refresh_audio_labels()
        return tab

    def _refresh_audio_labels(self, *_args: object) -> None:
        if not hasattr(self, "reset_audio_button"):
            return  # still building the tab
        self.track_volume_value.setText(f"{self.track_volume_slider.value() / 2:+.1f} dB")
        for slider, label in zip(self.eq_sliders, self.eq_values):
            label.setText(f"{slider.value() / 2:+.1f}")
        self.reset_audio_button.setEnabled(
            any(slider.value() for slider in (self.track_volume_slider, *self.eq_sliders))
        )
        self._eq_timer.start()

    def _reset_audio(self) -> None:
        for slider in (self.track_volume_slider, *self.eq_sliders):
            slider.setValue(0)

    def _wanted_filter(self) -> str:
        if self.eq_bypass_check.isChecked():
            return ""
        return track_audio_filter(
            self.track_volume_slider.value() / 2, [slider.value() / 2 for slider in self.eq_sliders],
        )

    def _update_audio_source(self, *_args: object) -> None:
        """Point the player at the original or at a copy rendered with the current volume/EQ."""
        if not self._audio_available:
            return
        wanted = self._wanted_filter()
        if self._eq_state == "rendering" and wanted == self._eq_rendering_filter:
            return
        self._eq_generation += 1  # whatever is still rendering is stale now
        self._eq_rendering_filter = wanted
        if wanted == self._eq_playing_filter:
            self._set_eq_state("eq" if wanted else "original")
        elif not wanted:
            self._swap_source(Path(self.track.file_path), "")
            self._set_eq_state("original")
        elif self.ffmpeg_executable is None:
            self._set_eq_state("no_ffmpeg")
        else:
            if self._eq_dir is None:
                self._eq_dir = TemporaryDirectory(prefix="track-eq-", ignore_cleanup_errors=True)
            output = Path(self._eq_dir.name) / f"eq_{self._eq_generation}.flac"
            self._set_eq_state("rendering")
            # ponytail: a superseded render still runs to its end (~1-2 s per track); kill it if that ever shows.
            threading.Thread(
                target=self._render_eq, args=(self._eq_generation, wanted, output), daemon=True,
            ).start()

    def _render_eq(self, generation: int, audio_filter: str, output: Path) -> None:
        """Worker thread: the whole track through ``audio_filter`` into ``output``."""
        command = [
            str(self.ffmpeg_executable), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", self.track.file_path, "-vn", "-map", "0:a:0",
            "-af", f"{audio_filter},{EQ_PREVIEW_LIMITER}",
            "-c:a", "flac", "-compression_level", "0", "-y", str(output),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, errors="replace", check=False,
                **hidden_process_kwargs(),
            )
            error = "" if result.returncode == 0 else (
                result.stderr.strip().splitlines() or [f"FFmpeg exited with {result.returncode}"])[-1]
        except OSError as exc:
            error = str(exc)
        try:
            self.eq_rendered.emit(generation, audio_filter, "" if error else str(output), error)
        except RuntimeError:
            pass  # the dialog closed meanwhile

    def _eq_render_finished(self, generation: int, audio_filter: str, path: str, error: str) -> None:
        if generation != self._eq_generation:
            if path:
                Path(path).unlink(missing_ok=True)
            return
        if error:
            self._eq_error = error
            self._set_eq_state("failed")
            return
        self._swap_source(Path(path), audio_filter)
        self._set_eq_state("eq")

    def _swap_source(self, path: Path, audio_filter: str) -> None:
        """Change what the player plays, resuming at the same position."""
        playing = self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self._pending_resume = (self.media_player.position(), playing)
        previous = self.media_player.source().toLocalFile()
        self._eq_playing_filter = audio_filter
        self.media_player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        if self._eq_dir is not None and previous.startswith(Path(self._eq_dir.name).as_posix()):
            try:
                Path(previous).unlink(missing_ok=True)
            except OSError:
                pass  # still held by the player; the folder goes when the dialog closes

    def _media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if self._pending_resume is None or status not in (
            QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            return
        position, playing = self._pending_resume
        self._pending_resume = None
        self.media_player.setPosition(position)
        if playing:
            self.media_player.play()

    def _set_eq_state(self, state: str) -> None:
        self._eq_state = state
        korean = self.translator.is_korean
        bar = activity_for(self)
        if bar is not None and state == "rendering":
            bar.update(EQ_ACTIVITY, None, label="음량·EQ 미리듣기 준비" if korean else "Preparing volume/EQ preview",
                       detail=self.track.title or Path(self.track.file_path).name)
        elif bar is not None:
            bar.finish(EQ_ACTIVITY)
        self.eq_status.setText({
            "original": "원본 소리" if korean else "Original sound",
            "rendering": "EQ 적용 중…" if korean else "Applying EQ…",
            "eq": "볼륨/EQ 적용된 소리" if korean else "With volume/EQ",
            "failed": (f"EQ 미리듣기 실패: {self._eq_error}" if korean
                       else f"EQ preview failed: {self._eq_error}"),
            "no_ffmpeg": ("FFmpeg가 없어 원본으로 재생합니다" if korean
                          else "No FFmpeg: playing the original"),
        }[state])

    def _refresh_header(self, *_args: object) -> None:
        if not hasattr(self, "video_list"):
            return  # still building the tabs the header summarizes
        korean = self.translator.is_korean
        self.header_title.setText(self.title_edit.text().strip() or Path(self.track.file_path).stem)
        self.header_subtitle.setText(" · ".join(
            value for value in (self.artist_edit.text().strip(), self.album_edit.text().strip()) if value
        ) or ("아티스트·앨범 정보 없음" if korean else "No artist or album"))
        facts = [self.track.duration_label]
        if self._file_facts.get("format"):
            facts.append(str(self._file_facts["format"]))
        if self._file_facts.get("sample_rate"):
            facts.append(f"{float(self._file_facts['sample_rate']) / 1000:g} kHz")
        count = len(self.selected_lyrics)
        facts.append(
            (f"가사 {count}줄" if count else "가사 없음") if korean
            else (f"{count} lyric lines" if count else "No lyrics")
        )
        videos = self.video_list.count()
        if videos:
            facts.append(f"영상 {videos}개" if korean else f"{videos} video(s)")
        analysis = self.analysis
        if analysis is not None and analysis.bpm is not None:
            camelot = key_to_camelot(analysis.key) if analysis.key else None
            facts.append(f"{analysis.bpm:.0f} BPM" + (f" · {camelot}" if camelot else ""))
        elif analysis is None:
            facts.append("분석 전" if korean else "Not analyzed")
        if not self._audio_available:
            facts.append("⚠ 음원 파일 없음" if korean else "⚠ Audio file missing")
        self.header_facts.setText("  ·  ".join(facts))
        pixmap = extract_track_cover(self.track.file_path, self.selected_cover_path)
        if pixmap.isNull():
            self.header_cover.setPixmap(QPixmap())
            self.header_cover.setText("♪")
        else:
            self.header_cover.setPixmap(pixmap.scaled(
                self.header_cover.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            ))

    def _refresh_file_facts(self, korean: bool) -> None:
        facts = self._file_facts
        unknown = "알 수 없음" if korean else "Unknown"
        if not facts:
            for label in (self.format_label, self.quality_label, self.size_label):
                label.setText("파일을 찾을 수 없음" if korean else "File not found")
            return
        channels = facts.get("channels")
        channel_text = (
            {1: "모노" if korean else "Mono", 2: "스테레오" if korean else "Stereo"}.get(
                int(channels), f"{int(channels)}ch") if channels else ""
        )
        self.format_label.setText(" · ".join(
            part for part in (str(facts.get("format") or ""), channel_text) if part
        ) or unknown)
        quality = []
        if facts.get("bitrate"):
            quality.append(f"{round(float(facts['bitrate']) / 1000)} kbps")
        if facts.get("sample_rate"):
            quality.append(f"{float(facts['sample_rate']) / 1000:g} kHz")
        if facts.get("bits_per_sample"):
            quality.append(f"{facts['bits_per_sample']}-bit")
        self.quality_label.setText(" · ".join(quality) or unknown)
        size = float(facts["size"])
        self.size_label.setText(
            f"{size / 1024 ** 2:.1f} MB" if size >= 1024 ** 2 else f"{size / 1024:.0f} KB"
        )

    def _reveal_audio_file(self) -> None:
        """Show the audio file in Explorer (its folder elsewhere)."""
        path = Path(self.track.file_path)
        if sys.platform == "win32":
            QProcess.startDetached("explorer", ["/select,", str(path)])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    # -- analysis, run by MainWindow ---------------------------------------------

    def begin_analysis(self) -> None:
        self._analysis_error = ""
        self.analysis_panel.begin_analysis()

    def analysis_step(self, track_id: str, step: str, fraction: float) -> None:
        if track_id == self.track.id:
            self.analysis_panel.set_step(step, fraction)

    def analyses_received(self, analyses: dict) -> None:
        analysis = analyses.get(self.track.id)
        if analysis is not None:
            self.analysis = analysis
            self.analysis_panel.set_results(analysis)
            self._refresh_header()

    def structures_received(self, structures: dict) -> None:
        structure = structures.get(self.track.id)
        if structure is not None:
            self.analysis_panel.set_structure(structure)

    def analyses_failed(self, failures: dict) -> None:
        self._analysis_error = str(failures.get(self.track.id, "") or self._analysis_error)

    def analysis_running_changed(self, running: bool) -> None:
        if not running:
            self.analysis_panel.finish_analysis(self._analysis_error)

    def _nudge_timing(self, delta: float) -> None:
        self.timing_offset_spin.setValue(self.timing_offset_spin.value() + delta)

    def _add_track_videos(self) -> None:
        korean = self.translator.is_korean
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "곡 영상 추가" if korean else "Add track videos",
            "",
            "Video files (*.mp4 *.mov *.mkv *.webm *.avi *.m4v)",
        )
        existing = {str(Path(path).resolve()).casefold() for path in self.selected_video_paths}
        for raw_path in paths:
            resolved = str(Path(raw_path).resolve())
            if resolved.casefold() not in existing:
                self.selected_video_paths.append(resolved)
                self.video_list.addItem(resolved)
                existing.add(resolved.casefold())
        self._update_track_video_ui()

    def _remove_track_videos(self) -> None:
        for item in self.video_list.selectedItems():
            self.video_list.takeItem(self.video_list.row(item))
        self.selected_video_paths = [
            self.video_list.item(index).text()
            for index in range(self.video_list.count())
        ]
        self._update_track_video_ui()

    def _move_track_video(self, delta: int) -> None:
        """Reorder this track's sequence without changing any video settings."""
        row = self.video_list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.video_list.count():
            return
        item = self.video_list.takeItem(row)
        self.video_list.insertItem(target, item)
        self.video_list.setCurrentRow(target)
        self.selected_video_paths = [
            self.video_list.item(index).text()
            for index in range(self.video_list.count())
        ]

    def _update_track_video_ui(self, *_args: object) -> None:
        count = self.video_list.count()
        korean = self.translator.is_korean
        self.video_count_label.setText(
            f"{count}개 영상" if korean else f"{count} video{'s' if count != 1 else ''}"
        )
        self.video_empty_label.setText(
            "아직 이 곡에 등록된 영상이 없습니다.\n‘영상 추가…’를 눌러 선택하세요."
            if korean else
            "No videos are assigned to this track yet.\nChoose ‘Add videos…’ to begin."
        )
        self.video_empty_label.setVisible(count == 0)
        self.video_list.setVisible(count > 0)
        selected = bool(self.video_list.selectedItems())
        row = self.video_list.currentRow()
        self.remove_video_button.setEnabled(selected)
        self.video_up_button.setEnabled(row > 0)
        self.video_down_button.setEnabled(0 <= row < count - 1)
        self._refresh_header()

    def _load_lyrics(self) -> None:
        korean = self.translator.is_korean
        path, _ = QFileDialog.getOpenFileName(
            self,
            "가사/자막 불러오기" if korean else "Load lyrics/subtitles",
            self.selected_lyrics_path,
            "Lyrics / subtitles (*.lrc *.srt *.vtt *.ttml)",
        )
        if path:
            self._apply_lyrics_from_path(path)

    def _show_content_lyrics_menu(self) -> None:
        """Pick a lyrics/subtitle file already imported into project content."""
        if not self._content_lyrics:
            return
        menu = QMenu(self)
        for name, path in self._content_lyrics:
            action = menu.addAction(name)
            action.setToolTip(path)
            action.triggered.connect(
                lambda _checked=False, target=path: self._apply_lyrics_from_path(target)
            )
        menu.exec(
            self.content_lyrics_button.mapToGlobal(
                self.content_lyrics_button.rect().bottomLeft()
            )
        )

    def _apply_lyrics_from_path(self, path: str) -> bool:
        """Load cues from ``path`` into the pending selection, or report an error."""
        try:
            cues = LyricsService.load(path)
        except LyricsError as error:
            self.preview.setPlainText(str(error))
            return False
        if not cues:
            self.preview.setPlainText(
                "인식할 수 있는 시간 정보가 없습니다. LRC/SRT/VTT/TTML 타임코드를 확인하세요. 기존 가사는 유지됩니다."
                if self.translator.is_korean else
                "No timed lyrics found. Check the LRC/SRT/VTT/TTML timestamps. Existing lyrics are retained."
            )
            return False
        self.selected_lyrics_path = str(Path(path).resolve())
        self.selected_lyrics = [cue.copy() for cue in cues]
        self._refresh_preview()
        return True

    def _find_lyrics(self) -> None:
        from copy import deepcopy
        from app.dialogs.lyrics_compare_dialog import LyricsCompareDialog
        from app.dialogs.lyrics_search_dialog import LyricsSearchDialog

        self.media_player.pause()
        snapshot = deepcopy(self.track)
        snapshot.title = self.title_edit.text().strip()
        snapshot.artist = self.artist_edit.text().strip()
        snapshot.album = self.album_edit.text().strip()

        def attach(document, _track_id):
            if self.selected_lyrics or self.selected_lyrics_path:
                comparison = LyricsCompareDialog(self.selected_lyrics, document.cues, self.selected_lyrics_path,
                                                 document.filename, self.translator, self)
                accepted = comparison.exec() == QDialog.DialogCode.Accepted and comparison.replace_requested
                comparison.deleteLater()
                if not accepted:
                    return False
            self.selected_lyrics = deepcopy(document.cues)
            self.selected_lyrics_path = ""
            self._refresh_preview()
            return True

        dialog = LyricsSearchDialog([snapshot], self.translator, self, initial_track_id=snapshot.id,
                                    on_attach=attach, staged=True)
        dialog.exec()
        dialog.deleteLater()

    def _clear_lyrics(self) -> None:
        self.selected_lyrics_path = ""
        self.selected_lyrics = []
        self._refresh_preview()

    def _choose_cover(self) -> None:
        korean = self.translator.is_korean
        path, _ = QFileDialog.getOpenFileName(
            self,
            "앨범 커버 선택" if korean else "Choose album cover",
            self.selected_cover_path,
            "Images (*.jpg *.jpeg *.png *.webp *.svg)",
        )
        if not path:
            return
        pixmap = extract_track_cover("", path)
        has_cover = not pixmap.isNull()
        if not has_cover:
            QMessageBox.warning(
                self,
                "이미지 불러오기 실패" if korean else "Could not load image",
                "선택한 파일을 앨범 커버로 사용할 수 없습니다."
                if korean else "The selected file cannot be used as album artwork.",
            )
            return
        self.selected_cover_path = str(Path(path).resolve())
        self._refresh_cover()

    def _reset_cover(self) -> None:
        self.selected_cover_path = ""
        self._refresh_cover()

    def _refresh_cover(self) -> None:
        korean = self.translator.is_korean
        pixmap = extract_track_cover(self.track.file_path, self.selected_cover_path)
        has_cover = not pixmap.isNull()
        if not has_cover:
            self.cover_preview.setPixmap(QPixmap())
            self.cover_preview.setText("앨범 커버 없음" if korean else "No album artwork")
        else:
            self.cover_preview.clear()
            self.cover_preview.setPixmap(pixmap.scaled(
                self.cover_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        if self.selected_cover_path:
            path = Path(self.selected_cover_path)
            valid_override = path.is_file() and not extract_track_cover("", path).isNull()
            self.cover_source_label.setText(
                path.name if valid_override else
                (
                    "선택한 커버 없음 · 내장 커버 사용 중"
                    if korean else "Selected artwork missing · using embedded artwork"
                )
            )
            self.cover_source_label.setToolTip(str(path))
        else:
            self.cover_source_label.setText(
                (
                    "음원에 내장된 커버" if has_cover else "음원에 내장된 커버 없음"
                ) if korean else (
                    "Artwork embedded in the audio" if has_cover
                    else "No artwork embedded in the audio"
                )
            )
            self.cover_source_label.setToolTip(self.track.file_path)
        self.reset_cover_button.setEnabled(bool(self.selected_cover_path))
        self._refresh_header()

    def _edit_in_lrc_generator(self) -> None:
        """Round-trip this track's timed lyrics through the LRC generator."""
        self.media_player.pause()
        dialog = LrcGeneratorDialog(
            [],
            self.translator,
            self,
            track_edit_mode=True,
            initial_audio_path=self.track.file_path,
            initial_cues=self.selected_lyrics,
            initial_title=self.title_edit.text().strip(),
            initial_artist=self.artist_edit.text().strip(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        edited_cues = dialog.timed_cues()
        if edited_cues:
            self.selected_lyrics = [cue.copy() for cue in edited_cues]
        if dialog.saved_paths:
            self.selected_lyrics_path = str(dialog.saved_paths[-1].resolve())
        self._refresh_preview()
        self._accept()

    def _export_current_lyrics_as_lrc(self) -> None:
        """Convert the attached/embedded cues to LRC with this track's offset applied."""
        korean = self.translator.is_korean
        if not self.selected_lyrics:
            QMessageBox.warning(
                self,
                "내보낼 가사 없음" if korean else "No lyrics to export",
                "먼저 시간 정보가 있는 가사를 등록하세요."
                if korean else "Attach timed lyrics before exporting an LRC file.",
            )
            return
        source = Path(self.selected_lyrics_path) if self.selected_lyrics_path else Path(self.track.file_path)
        default_name = source.with_suffix(".lrc")
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "현재 가사를 LRC로 내보내기" if korean else "Export current lyrics as LRC",
            str(default_name),
            "LRC lyrics (*.lrc)",
        )
        if not selected:
            return
        offset = self.timing_offset_spin.value()
        adjusted_cues: list[dict[str, object]] = []
        for cue in self.selected_lyrics:
            start = max(0.0, float(cue.get("start", 0.0)) - offset)
            end = max(start, float(cue.get("end", start + 8.0)) - offset)
            adjusted_cues.append({
                "start": start,
                "end": end,
                "text": LyricsService.decode_line_breaks(cue.get("text", "")),
            })
            if cue.get("words"):
                adjusted_cues[-1]["words"] = [word | {
                    "start": max(0.0, float(word["start"]) - offset),
                    "end": max(0.0, float(word["end"]) - offset),
                } for word in cue["words"]]
        try:
            saved = LyricsService.save_lrc(
                selected,
                adjusted_cues,
                title=self.title_edit.text().strip(),
                artist=self.artist_edit.text().strip(),
            )
        except LyricsError as error:
            QMessageBox.critical(
                self,
                "LRC 내보내기 오류" if korean else "LRC export error",
                str(error),
            )
            return
        QMessageBox.information(
            self,
            "LRC 내보내기 완료" if korean else "LRC export complete",
            f"현재 가사를 LRC 파일로 저장했습니다.\n{saved}"
            if korean else f"The current lyrics were saved as an LRC file.\n{saved}",
        )

    def _toggle_playback(self) -> None:
        """Play or pause the selected track without leaving the settings form."""
        if not self._audio_available:
            return
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
        else:
            self.media_player.play()

    def _set_volume(self, value: int) -> None:
        """Apply and persist the volume shared with the full-video preview."""
        value = save_preview_volume(value)
        self.audio_output.setVolume(value / 100.0)
        self.volume_value.setText(f"{value}%")

    def _stop_playback(self) -> None:
        """Stop audio and return the synchronized preview to the track start."""
        self.media_player.stop()
        self.media_player.setPosition(0)
        self._playback_position_changed(0)

    def _position_sliders(self) -> tuple[QSlider, ...]:
        """The Lyrics tab's and the Audio tab's position sliders (one player)."""
        audio = getattr(self, "audio_position_slider", None)
        return (self.playback_slider,) if audio is None else (self.playback_slider, audio)

    def _seek_playback(self) -> None:
        """Seek audio to the position chosen on whichever preview slider moved."""
        sender = self.sender()
        slider = sender if isinstance(sender, QSlider) else self.playback_slider
        if self._audio_available:
            self.media_player.setPosition(slider.value())
        self._playback_position_changed(slider.value())

    def _playback_position_changed(self, position_ms: int) -> None:
        for slider in self._position_sliders():
            if not slider.isSliderDown():
                slider.setValue(max(0, position_ms))
        total_ms = max(self.playback_slider.maximum(), round(self.track.duration_seconds * 1000))
        text = f"{self._clock(position_ms)} / {self._clock(total_ms)}"
        self.playback_time.setText(text)
        if hasattr(self, "audio_time"):
            self.audio_time.setText(text)
        self._update_live_lyrics(position_ms)

    def _playback_duration_changed(self, duration_ms: int) -> None:
        duration = max(1, duration_ms, round(self.track.duration_seconds * 1000))
        for slider in self._position_sliders():
            slider.setRange(0, duration)
        self._playback_position_changed(self.media_player.position())

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        korean = self.translator.is_korean
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        text = ("일시정지" if korean else "Pause") if playing else ("재생" if korean else "Play")
        self.play_button.setText(text)
        self.audio_play_button.setText(text)

    def _playback_error(
        self, _error: QMediaPlayer.Error, message: str = "",
    ) -> None:
        if not message:
            return
        korean = self.translator.is_korean
        self.playback_status.setText(
            f"오디오 재생 오류: {message}" if korean else f"Audio playback error: {message}"
        )

    def _update_live_lyrics(self, position_ms: int | None = None) -> None:
        """Display the cue synchronized to audio and the unsaved timing offset."""
        korean = self.translator.is_korean
        if not self.selected_lyrics:
            self.previous_lyric.clear()
            self.next_lyric.clear()
            self.current_lyric.setText(
                "시간 정보가 있는 가사를 불러오세요."
                if korean else "Load timed lyrics to preview them here."
            )
            return
        position = self.media_player.position() if position_ms is None else position_ms
        lyric_seconds = max(
            0.0, position / 1000.0 + self.timing_offset_spin.value()
        )
        cue_index = LyricsService.display_cue_index(self.selected_lyrics, lyric_seconds)
        if cue_index is None:
            return
        latest_started = cue_index
        active = ()
        if self.selected_lyrics[cue_index].get("format") == "ttml":
            active = LyricsService.active_cue_indices(self.selected_lyrics, lyric_seconds)
            if not active and lyric_seconds >= float(self.selected_lyrics[cue_index]["end"]):
                active = LyricsService.last_ending_cue_indices(self.selected_lyrics, lyric_seconds)
                cue_index = active[0]

        def cue_text(index: int) -> str:
            return LyricsService.decode_line_breaks(
                self.selected_lyrics[index].get("text", "")
            ).strip()

        current_indices = active or (cue_index,)
        previous = [index for index in range(latest_started + 1) if index not in current_indices]
        self.previous_lyric.setText(cue_text(previous[-1]) if previous else "")
        self.current_lyric.setText("\n".join(cue_text(index) for index in current_indices))
        next_index = latest_started + 1
        self.next_lyric.setText(
            cue_text(next_index) if next_index < len(self.selected_lyrics) else ""
        )

    @staticmethod
    def _clock(milliseconds: int) -> str:
        return format_clock(milliseconds // 1000)

    @staticmethod
    def _timestamp(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        minutes, remainder = divmod(milliseconds, 60_000)
        whole_seconds, fraction = divmod(remainder, 1000)
        return f"{minutes:02d}:{whole_seconds:02d}.{fraction:03d}"

    def _refresh_preview(self, _value: float = 0.0) -> None:
        korean = self.translator.is_korean
        offset = self.timing_offset_spin.value()
        self.lyrics_path.setText(
            self.selected_lyrics_path
            or (("프로젝트 내장 가사" if korean else "Lyrics embedded in project") if self.selected_lyrics else
                ("연결된 가사 파일이 없습니다." if korean else "No lyric file attached."))
        )
        self.clear_button.setEnabled(bool(self.selected_lyrics_path or self.selected_lyrics))
        self.edit_lrc_button.setEnabled(self._audio_available or bool(self.selected_lyrics))
        self.export_lrc_button.setEnabled(bool(self.selected_lyrics))
        count = len(self.selected_lyrics)
        self.cue_summary.setText(
            f"{count}개 타임코드 · 보정 적용 미리보기"
            if korean else f"{count} timed cues · adjusted preview"
        )
        self._update_live_lyrics()
        self._refresh_header()
        if not self.selected_lyrics:
            self.preview.setPlainText(
                "시간 정보가 있는 가사를 불러오세요."
                if korean else "Load lyrics containing timing information."
            )
            return
        lines: list[str] = []
        for cue in self.selected_lyrics[:120]:
            # A positive offset advances lyrics, so their display timestamp is
            # cue time minus the offset.
            start = float(cue.get("start", 0.0)) - offset
            text = LyricsService.decode_line_breaks(
                cue.get("text", "")
            ).replace("\n", " / ").strip()
            lines.append(f"[{self._timestamp(start)}]  {text}")
        if count > 120:
            lines.append(f"… +{count - 120}")
        self.preview.setPlainText("\n".join(lines))

    def _accept(self) -> None:
        korean = self.translator.is_korean
        if (self.selected_cover_path
                and extract_track_cover("", self.selected_cover_path).isNull()):
            QMessageBox.warning(
                self,
                "앨범 커버 확인 필요" if korean else "Check album artwork",
                "선택한 앨범 커버 파일을 찾거나 읽을 수 없습니다."
                if korean else "The selected album artwork is missing or unreadable.",
            )
            self.tabs.setCurrentIndex(0)
            return
        title = self.title_edit.text().strip()
        if not title:
            QMessageBox.warning(
                self,
                "곡 제목 필요" if korean else "Track title required",
                "곡 제목을 입력하세요." if korean else "Enter a title for this track.",
            )
            self.title_edit.setFocus()
            return
        self.selected_title = title
        self.selected_artist = self.artist_edit.text().strip()
        self.selected_album = self.album_edit.text().strip()
        self.selected_timing_offset = self.timing_offset_spin.value()
        self.selected_video_paths = [
            self.video_list.item(index).text()
            for index in range(self.video_list.count())
        ]
        self.selected_volume_db = self.track_volume_slider.value() / 2
        eq = [slider.value() / 2 for slider in self.eq_sliders]
        self.selected_eq_db = eq if any(eq) else []
        self.accept()

    def done(self, result: int) -> None:
        """Never leave preview audio playing after the form closes."""
        self.media_player.stop()
        self._eq_timer.stop()
        self._eq_generation += 1  # drop any render still on its way
        bar = activity_for(self)
        if bar is not None:
            bar.finish(EQ_ACTIVITY)
        if self._eq_dir is not None:
            self.media_player.setSource(QUrl())  # release the rendered file first
            self._eq_dir.cleanup()
            self._eq_dir = None
        super().done(result)

    def retranslate(self) -> None:
        korean = self.translator.is_korean
        self.setWindowTitle("곡 정보/설정" if korean else "Track information/settings")
        self.tabs.setTabText(0, "곡 정보" if korean else "Track information")
        self.tabs.setTabText(1, "분석" if korean else "Analysis")
        self.tabs.setTabText(2, "가사 설정" if korean else "Lyrics settings")
        self.tabs.setTabText(3, "이 곡의 영상" if korean else "Videos for this track")
        self.tabs.setTabText(4, "오디오" if korean else "Audio")
        self.track_volume_group.setTitle("곡 볼륨" if korean else "Track volume")
        self.eq_group.setTitle("이퀄라이저 (dB)" if korean else "Equalizer (dB)")
        self.reset_audio_button.setText("초기화" if korean else "Reset")
        self.eq_bypass_check.setText("원본과 비교 (볼륨/EQ 끄기)" if korean else "Compare with original (bypass)")
        self.audition_group.setTitle("미리듣기" if korean else "Listen")
        self.audio_stop_button.setText("정지" if korean else "Stop")
        for button in (self.audio_play_button, self.audio_stop_button, self.eq_bypass_check):
            button.setEnabled(self._audio_available)
        self.audio_position_slider.setEnabled(self._audio_available)
        self._set_eq_state(self._eq_state)
        self.audio_help.setText(
            "이 곡에만 적용되며 미리보기와 내보내기에 반영됩니다. 전체 믹스는 여전히 -14 LUFS로 "
            "맞춰지므로, 볼륨은 다른 곡과 비교한 상대적인 크기를 조절합니다. "
            "값을 바꾸면 잠시 뒤 재생 중인 소리에 반영됩니다."
            if korean else
            "Applies to this track only, in Preview and Export. The whole mix is still normalized "
            "to -14 LUFS, so volume sets this track's level relative to the others. "
            "Changes reach the playing sound after a moment."
        )
        self.analysis_panel.retranslate(korean)
        self.video_scope_badge.setText(
            "적용 범위 · 이 곡이 재생되는 동안만"
            if korean else "Scope · Only while this track is playing"
        )
        self.video_help.setText(
            "캔버스의 영상 요소에서 ‘곡마다 다른 영상 사용’을 선택하면 이 목록을 사용합니다. "
            "영상 요소의 크기·반복·속도·효과 설정은 그대로 적용됩니다."
            if korean else
            "Canvas video sources set to ‘Use different videos for each track’ use this list. "
            "The source's size, repeat, speed, and effects still apply."
        )
        self.video_list_group.setTitle(
            "재생 순서" if korean else "Playback order"
        )
        self.add_video_button.setText("영상 추가…" if korean else "Add videos…")
        self.remove_video_button.setText("선택 제거" if korean else "Remove selected")
        self.video_up_button.setText("위로" if korean else "Up")
        self.video_down_button.setText("아래로" if korean else "Down")
        self._update_track_video_ui()
        self.info_group.setTitle("기본 정보" if korean else "Basic information")
        self.cover_group.setTitle("앨범 커버" if korean else "Album artwork")
        self.change_cover_button.setText("이미지 변경…" if korean else "Change image…")
        self.reset_cover_button.setText("내장 커버 사용" if korean else "Use embedded artwork")
        self.cover_group.setToolTip(
            "프로젝트에서 사용할 곡별 커버입니다. 원본 음원의 태그는 변경하지 않습니다."
            if korean else
            "Per-track artwork stored by the project. Source audio tags are not changed."
        )
        info_names = ("제목", "아티스트", "앨범") if korean else ("Title", "Artist", "Album")
        for label, name in zip(self.info_name_labels, info_names):
            label.setText(name)
        self.file_group.setTitle("파일" if korean else "File")
        file_names = (
            ("위치", "재생 시간", "형식", "음질", "크기")
            if korean else ("Location", "Duration", "Format", "Quality", "Size")
        )
        for label, name in zip(self.file_name_labels, file_names):
            label.setText(name)
        self.reveal_file_button.setText("폴더에서 보기" if korean else "Show in folder")
        self.reveal_file_button.setToolTip(
            "탐색기에서 이 음원 파일을 선택해 보여 줍니다." if korean
            else "Show this audio file in the file manager."
        )
        self._refresh_file_facts(korean)
        metadata_tip = (
            "프로젝트에 저장할 곡 정보를 직접 수정할 수 있습니다. 원본 오디오 파일의 태그는 변경되지 않습니다."
            if korean else
            "Edit the track information stored in this project. The source audio file tags are not changed."
        )
        for field in self.metadata_edits:
            field.setToolTip(metadata_tip)
        self.lyrics_group.setTitle("가사 / 자막" if korean else "Lyrics / subtitles")
        self.playback_group.setTitle("노래와 가사 미리보기" if korean else "Audio and lyrics preview")
        self.load_button.setText("파일 불러오기…" if korean else "Load file…")
        self.content_lyrics_button.setText(
            "프로젝트 콘텐츠…" if korean else "From project content…"
        )
        self.content_lyrics_button.setToolTip(
            "프로젝트 콘텐츠에 추가한 가사·자막 파일을 이 곡에 연결합니다."
            if korean else
            "Attach a lyrics/subtitle file already in Project Content to this track."
        )
        self.edit_lrc_button.setText(
            "LRC 생성기로 편집…" if korean else "Edit in LRC Generator…"
        )
        self.edit_lrc_button.setToolTip(
            "현재 곡의 오디오와 등록된 가사·타이밍을 LRC 생성기에서 편집합니다."
            if korean else
            "Edit this track's audio, lyrics, and timing in the LRC generator."
        )
        self.export_lrc_button.setText(
            "LRC로 내보내기…" if korean else "Export as LRC…"
        )
        self.search_lyrics_button.setText("가사 찾아보기…" if korean else "Find lyrics…")
        self.export_lrc_button.setToolTip(
            "현재 곡의 타이밍 보정을 적용하고 여러 줄 가사는 문자 \\n으로 저장합니다."
            if korean else
            "Applies this track's timing offset and stores multi-line lyrics using the literal \\n characters."
        )
        self.clear_button.setText("연결 해제" if korean else "Detach")
        self.timing_label.setText("곡별 타이밍 보정" if korean else "Per-track timing offset")
        self.reset_button.setText("초기화" if korean else "Reset")
        self.play_button.setText("재생" if korean else "Play")
        self.stop_button.setText("정지" if korean else "Stop")
        self.volume_label.setText("볼륨" if korean else "Volume")
        self.play_button.setEnabled(self._audio_available)
        self.stop_button.setEnabled(self._audio_available)
        self.playback_slider.setEnabled(self._audio_available)
        if self._audio_available:
            self.playback_status.setText(
                "재생 위치에 맞춰 가사를 표시합니다."
                if korean else "Lyrics follow the current playback position."
            )
        else:
            self.playback_status.setText(
                "음원 파일을 찾을 수 없어 재생할 수 없습니다."
                if korean else "The audio file is missing and cannot be played."
            )
        self._playback_state_changed(self.media_player.playbackState())
        self.earlier_button.setToolTip(
            "가사를 0.1초 늦게 표시" if korean else "Show lyrics 0.1 seconds later"
        )
        self.later_button.setToolTip(
            "가사를 0.1초 빠르게 표시" if korean else "Show lyrics 0.1 seconds earlier"
        )
        self.timing_help.setText(
            "양수 값은 가사를 더 빠르게, 음수 값은 더 늦게 표시합니다. 이 값은 현재 곡에만 적용되며 가사 요소의 공통 보정값과 합산됩니다."
            if korean else
            "Positive values show lyrics earlier; negative values show them later. This applies only to the current track and is added to the Lyrics source's global offset."
        )
        save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setText("적용" if korean else "Apply")
        cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText("취소" if korean else "Cancel")
        self._refresh_cover()
        self._refresh_preview()
