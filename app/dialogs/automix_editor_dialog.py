"""AutoMix editor: set each transition by hand on a two-track timeline and hear just that window.

Independent of Preview. The editor keeps its own plan: every committed edit
(one undo step) is stored in the project at once (``override_changed``) and
re-planned on the UI thread with ``compile_automix`` -- CPU only, so a new
length or cue moves the following transitions and their tempo-ramp floors
right away -- while ``TransitionAuditionController`` renders only the
selected window in the background. Nothing here renders the playlist; Export
and Preview plan the same overrides with the same planner and DSP.

Layout: the transition picker and mode on top, the tools under it, the
timeline in the middle with the selected target's properties beside it, and
the audition transport at the bottom. What is selected on the timeline
(the transition, a song's cue, the tempo change, a band) decides what the
side panel shows; the workspace (panel width and visibility, band lanes,
snap) is remembered between openings.
"""

from __future__ import annotations

import html
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QSplitter, QToolButton, QVBoxLayout,
)

from app.automix.overrides import SONG_FIELDS, pair_key
from app.automix.planner import compile_automix
from app.automix.renderer import BAND_ENVELOPES, band_windows_of, transition_dsp_style
from app.automix.settings import automix_settings_for
from app.controllers.transition_audition_controller import (
    FAILED, READY, RENDERING, UNAVAILABLE, WAITING, TransitionAuditionController,
)
from app.dialogs.help_dialog import install_help_shortcut, open_help
from app.ui.studio_icons import menu_icon
from app.widgets.activity_progress import activity_for
from app.widgets.audio_sync_warning import AudioSyncWarning
from app.widgets.automix_timeline import AutoMixTimeline
from app.widgets.transition_editor import (
    FIELD_NAMES, EditContext, TransitionPropertiesPanel, applied_notes, bars_text, changed_items,
    clock, drag_override, edited_override, move_window, override_from_junction, recommendation_note, reset_item,
    snap_units, style_key, style_label, with_band,
)
from app.widgets.transition_inspector import INCOMING_COLOR, OUTGOING_COLOR, _clock, plan_junctions

_SETTINGS_KEY = "automix_editor/advanced"
AUDITION_ACTIVITY = "automix_audition"
_COPIED_FIELDS = ("style", "duration", "tempo_match", "vocal_handoff", "eq_bands",
                  "echo_beats", "echo_feedback", "echo_low_cut", "tape_entry", "key_shift", "ramp_seconds",
                  "roll_beats", "filter_cutoff_hz")
"""What paste and presets carry to another transition: how it mixes, never where (cues belong to the songs)."""
_PRESETS_KEY = "automix_editor/presets"
_GEOMETRY_KEY = "automix_editor/geometry"
"""Size, position and maximized state, kept from one opening to the next."""
_SPLITTER_KEY = "automix_editor/splitter"
_PANEL_KEY = "automix_editor/properties_visible"
_LANES_KEY = "automix_editor/lanes_visible"
_SNAP_KEY = "automix_editor/snap"
PANEL_WIDTH = 368
_FIELD_GROUPS = {"style": ("스타일", "style"), "duration": ("길이", "length"), "tempo_match": ("템포", "tempo"),
                 "ramp_seconds": ("템포", "tempo"), "key_shift": ("템포", "tempo"), "eq_bands": ("대역", "bands"),
                 "vocal_handoff": ("대역", "bands"), "echo_beats": ("효과", "effect"),
                 "echo_feedback": ("효과", "effect"), "echo_low_cut": ("효과", "effect"),
                 "tape_entry": ("효과", "effect"), "outgoing_cue": ("A 큐", "A cue"),
                 "incoming_cue": ("B 시작", "B start"), "roll_beats": ("효과", "effect"),
                 "filter_cutoff_hz": ("효과", "effect")}
"""How an applied preset or paste names what it changed."""


class _OverrideCommand(QUndoCommand):
    """One edit of one junction: undo puts the previous override (or automatic) back."""

    def __init__(self, editor: "AutoMixEditorDialog", key: str, before, after, text: str) -> None:
        super().__init__(text)
        self.editor, self.key, self.before, self.after = editor, key, before, after

    def redo(self) -> None:
        self.editor._store(self.key, self.after)

    def undo(self) -> None:
        self.editor._store(self.key, self.before)


def _executable(window) -> Path | None:
    from app.renderer.ffmpeg_renderer import FFmpegNotFoundError, FFmpegRenderer

    settings = getattr(window, "settings_service", None)
    if settings is None:
        return None
    try:
        return FFmpegRenderer(settings.current.ffmpeg_path or None).executable
    except FFmpegNotFoundError:
        return None


class AutoMixEditorDialog(QDialog):
    """See module docstring."""

    override_changed = Signal(str, object)
    """A junction was set by hand (pair key, TransitionOverride) or put back on automatic (key, None)."""

    def __init__(self, window, pair: tuple[str, str] | None = None, executable: Path | None = None) -> None:
        super().__init__(window)
        self.host = window
        self.setObjectName("automixEditor")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        # A long timeline wants the whole screen: maximize (and minimize) like a main window.
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumSize(760, 480)
        self.resize(1320, 820)
        self.korean = bool(getattr(getattr(window, "translator", None), "is_korean", True))
        self._tracks = [track for track in window.playlist_service.tracks if track.enabled]
        self._settings = automix_settings_for(window.project_settings)
        self._overrides = dict(self._settings.overrides)
        self._changed_keys: set[str] = set()
        self._plan = None
        self._junctions = []
        self._index = -1
        self._selection = "transition"
        self._drag_base = None
        self._drag_override = None
        self._copied = None
        self._finished = False
        self._automatic: tuple[object, object, dict] | None = None
        """(cache key, plan, pair key -> junction): the plan with the selected junction on automatic."""
        self._audio: tuple[str, float, float, float] | None = None
        """(file, origin, start, end): see _on_audio_ready."""
        self._playhead: float | None = None
        self._player = None
        self._pending_position: float | None = None
        self._play_when_ready = False
        self._audition_state = ("idle", "")
        self.undo_stack = QUndoStack(self)
        self.audition = TransitionAuditionController(
            executable if executable is not None else _executable(window), self)
        self.audition.state_changed.connect(self._on_audition_state)
        self.audition.audio_ready.connect(self._on_audio_ready)
        self.audition.peaks_ready.connect(self._on_peaks)
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.setInterval(8000)

        self._build()
        self._message_timer.timeout.connect(self.message_label.clear)
        self.override_changed.connect(window._set_automix_override)
        self.undo_stack.indexChanged.connect(self._refresh_actions)
        for owner in (getattr(window, "automix_analysis_controller", None),
                      getattr(window, "track_analysis_controller", None)):
            for name in ("analyses_updated", "structures_updated"):
                signal = getattr(owner, name, None)
                if signal is not None:
                    # A bound slot, not a lambda: Qt drops it when this dialog is deleted.
                    signal.connect(self._queue_analysis_arrived)
        playlist_changed = getattr(getattr(window, "playlist_service", None), "playlist_changed", None)
        if playlist_changed is not None:
            playlist_changed.connect(self._playlist_changed)
        self._set_advanced(bool(QSettings().value(_SETTINGS_KEY, False, bool)), save=False)
        geometry = QSettings().value(_GEOMETRY_KEY)
        if geometry:
            self.restoreGeometry(geometry)
        self._restore_workspace()
        self._replan()
        start = next((index for index, junction in enumerate(self._junctions)
                      if (junction.outgoing.track_id, junction.incoming.track_id) == pair), 0)
        self._select(start, refit=True)
        self._retranslate()

    # -- construction ------------------------------------------------------------------

    def _build(self) -> None:
        def tool(text: str = "", checkable: bool = False, icon: str | None = None) -> QToolButton:
            button = QToolButton()
            button.setText(text)
            button.setCheckable(checkable)
            if icon is not None:
                button.setIcon(menu_icon(icon))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly if icon is None
                                      else Qt.ToolButtonStyle.ToolButtonIconOnly)
            return button

        # -- top: which transition, and how much of it to show --
        self.previous_button = tool("‹")
        self.next_button = tool("›")
        self.transition_combo = QComboBox()
        self.transition_combo.setMinimumWidth(140)
        self.transition_combo.setMinimumContentsLength(12)
        self.transition_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.transition_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.simple_button = QPushButton()
        self.advanced_button = QPushButton()
        self.mode_group = QButtonGroup(self)
        mode_box = QFrame()
        mode_box.setObjectName("transitionModeSwitch")
        mode_layout = QHBoxLayout(mode_box)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(0)
        for button in (self.simple_button, self.advanced_button):
            button.setCheckable(True)
            button.setObjectName("transitionModeButton")
            self.mode_group.addButton(button)
            mode_layout.addWidget(button)
        self.more_button = tool(icon="more")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more_button)
        self.more_button.setMenu(self.more_menu)
        self.help_button = tool("?")

        # -- tools: undo, snap, zoom, what to show --
        self.undo_button = tool(icon="undo")
        self.redo_button = tool(icon="redo")
        self.snap_label = QLabel()
        self.snap_label.setObjectName("mutedLabel")
        self.snap_combo = QComboBox()
        self.snap_combo.setObjectName("snapCombo")
        for unit in ("off", "beat", "bar"):
            self.snap_combo.addItem("", unit)
        self.snap_combo.setCurrentIndex(1)
        self.zoom_out_button = tool(icon="zoom_out")
        self.fit_button = tool()
        self.zoom_in_button = tool(icon="zoom_in")
        self.tempo_button = tool()
        self.tempo_button.clicked.connect(self._show_tempo)
        self.lanes_button = tool("", checkable=True)
        self.lanes_button.setChecked(True)
        self.properties_button = tool("", checkable=True)
        self.properties_button.setChecked(True)

        # -- the menu: what is used now and then --
        menu = self.more_menu
        self.copy_action = menu.addAction(menu_icon("copy"), "")
        self.copy_action.triggered.connect(self._copy)
        self.paste_action = menu.addAction(menu_icon("paste"), "")
        self.paste_action.triggered.connect(self._paste)
        self.paste_cues_action = menu.addAction("")
        self.paste_cues_action.triggered.connect(lambda: self._paste(cues=True))
        menu.addSeparator()
        self.presets_menu = menu.addMenu(menu_icon("save_preset"), "")
        self.presets_menu.aboutToShow.connect(self._fill_presets_menu)
        menu.addSeparator()
        self.recommend_action = menu.addAction("")
        self.recommend_action.triggered.connect(self._recommend)
        self.reset_all_action = menu.addAction(menu_icon("check_updates"), "")
        self.reset_all_action.triggered.connect(self._reset_all)
        menu.addSeparator()
        self.layout_action = menu.addAction(menu_icon("reset_layout"), "")
        self.layout_action.triggered.connect(self._reset_workspace)
        self.shortcuts_action = menu.addAction(menu_icon("shortcuts"), "")
        self.shortcuts_action.triggered.connect(self._show_shortcuts)
        menu.aboutToShow.connect(self._refresh_actions)

        self.previous_button.clicked.connect(lambda: self._user_select(self._index - 1))
        self.next_button.clicked.connect(lambda: self._user_select(self._index + 1))
        self.transition_combo.activated.connect(self._user_select)
        self.simple_button.clicked.connect(lambda: self._set_advanced(False))
        self.advanced_button.clicked.connect(lambda: self._set_advanced(True))
        self.undo_button.clicked.connect(self._undo)
        self.redo_button.clicked.connect(self._redo)
        self.snap_combo.currentIndexChanged.connect(self._snap_changed)
        self.zoom_out_button.clicked.connect(lambda: self.timeline.zoom(1.25))
        self.zoom_in_button.clicked.connect(lambda: self.timeline.zoom(0.8))
        self.fit_button.clicked.connect(self._fit)
        self.lanes_button.toggled.connect(self._lanes_toggled)
        self.properties_button.toggled.connect(self._toggle_properties)
        self.help_button.clicked.connect(self._show_help)
        install_help_shortcut(self, lambda editor: editor._help_context(), getattr(self.host, "translator", None))

        toolbar = QFrame()
        toolbar.setObjectName("automixEditorToolbar")
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(16, 10, 16, 10)
        bar.setSpacing(8)
        self.editor_title = QLabel("AutoMix")
        self.editor_title.setObjectName("automixEditorTitle")
        bar.addWidget(self.editor_title)
        bar.addSpacing(8)
        for widget in (self.previous_button, self.transition_combo, self.next_button):
            bar.addWidget(widget)
        bar.addSpacing(12)
        bar.addWidget(mode_box)
        bar.addWidget(self.more_button)
        bar.addWidget(self.help_button)

        self.tools_bar = QFrame()
        self.tools_bar.setObjectName("automixToolsBar")
        tools_row = QHBoxLayout(self.tools_bar)
        tools_row.setContentsMargins(16, 6, 16, 6)
        tools_row.setSpacing(6)
        for widget in (self.undo_button, self.redo_button):
            tools_row.addWidget(widget)
        tools_row.addSpacing(14)
        tools_row.addWidget(self.snap_label)
        tools_row.addWidget(self.snap_combo)
        tools_row.addSpacing(14)
        for widget in (self.zoom_out_button, self.fit_button, self.zoom_in_button):
            tools_row.addWidget(widget)
        tools_row.addStretch(1)
        tools_row.addWidget(self.tempo_button)
        tools_row.addWidget(self.lanes_button)
        tools_row.addWidget(self.properties_button)

        # -- centre: the timeline, the selected target's properties beside it --
        self.timeline = AutoMixTimeline()
        self.timeline.seek_requested.connect(self._seek)
        self.timeline.drag_started.connect(self._drag_started)
        self.timeline.drag_moved.connect(self._drag_moved)
        self.timeline.drag_finished.connect(self._drag_finished)
        self.timeline.drag_cancelled.connect(self._drag_cancelled)
        self.timeline.bands_changed.connect(self._bands_changed)
        self.timeline.nudge_requested.connect(self._nudge)
        self.timeline.selection_changed.connect(self._selection_changed)
        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setObjectName("automixTimelineArea")
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.timeline_scroll.setWidget(self.timeline)

        self.properties = TransitionPropertiesPanel()
        self.properties.changed.connect(self._properties_changed)
        self.properties.reset_requested.connect(self._reset)
        self.properties.item_reset.connect(self._reset_item)
        self.properties.lock_toggled.connect(self._toggle_lock)
        self.properties.link_toggled.connect(lambda linked: setattr(self.timeline, "band_linked", linked))
        self.properties_scroll = QScrollArea()
        self.properties_scroll.setObjectName("automixPropertiesArea")
        self.properties_scroll.setWidgetResizable(True)
        self.properties_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.properties_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.properties_scroll.setWidget(self.properties)
        self.properties_scroll.setMinimumWidth(280)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.timeline_scroll)
        self.splitter.addWidget(self.properties_scroll)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([980, PANEL_WIDTH])

        # -- bottom: listening --
        self.play_button = QPushButton()
        self.play_button.setObjectName("previewPlayButton")
        self.play_button.setCheckable(True)
        self.play_button.setMinimumWidth(96)
        self.play_button.toggled.connect(self._toggle_play)
        self.stop_button = tool("■")
        self.stop_button.clicked.connect(self._stop)
        self.loop_button = tool("", checkable=True)
        self.loop_button.setChecked(True)
        self.loop_button.toggled.connect(self._loop_toggled)
        self.compare_label = QLabel()
        self.compare_label.setObjectName("mutedLabel")
        self.mine_button = tool("", checkable=True)
        self.mine_button.setObjectName("compareButton")
        self.mine_button.setChecked(True)
        self.mine_button.clicked.connect(self._hear_mine)
        self.compare_button = tool("", checkable=True)
        self.compare_button.setObjectName("compareButton")
        self.compare_button.toggled.connect(self._compare_toggled)
        self.time_label = QLabel()
        self.time_label.setObjectName("previewTimeLabel")
        self.time_label.setMinimumWidth(130)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.setMinimumWidth(80)
        self.position_slider.sliderMoved.connect(lambda value: self._seek_local(value / 1000.0))
        self.state_label = QLabel()
        self.state_label.setObjectName("automixStateChip")
        self.state_label.setWordWrap(True)
        self.retry_button = QPushButton()
        self.retry_button.clicked.connect(self.audition.retry)
        self.retry_button.hide()
        self.message_label = QLabel()
        self.message_label.setObjectName("automixMessage")
        self.message_label.setWordWrap(True)
        self.volume_label = QLabel()
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.valueChanged.connect(self._volume_changed)
        self.save_label = QLabel()
        self.save_label.setObjectName("mutedLabel")
        self.save_label.setWordWrap(True)
        transport = QFrame()
        transport.setObjectName("automixTransport")
        transport_layout = QVBoxLayout(transport)
        transport_layout.setContentsMargins(16, 10, 16, 10)
        transport_layout.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        for widget in (self.play_button, self.stop_button, self.loop_button):
            row.addWidget(widget)
        row.addSpacing(12)
        row.addWidget(self.compare_label)
        compare_box = QFrame()
        compare_box.setObjectName("transitionModeSwitch")
        compare_layout = QHBoxLayout(compare_box)
        compare_layout.setContentsMargins(0, 0, 0, 0)
        compare_layout.setSpacing(0)
        compare_layout.addWidget(self.mine_button)
        compare_layout.addWidget(self.compare_button)
        row.addWidget(compare_box)
        row.addSpacing(12)
        row.addWidget(self.position_slider, 1)
        row.addWidget(self.time_label)
        transport_layout.addLayout(row)
        status = QHBoxLayout()
        status.setSpacing(8)
        status.addWidget(self.state_label, 1)
        status.addWidget(self.retry_button)
        status.addWidget(self.message_label, 1)
        status.addWidget(self.volume_label)
        status.addWidget(self.volume_slider)
        transport_layout.addLayout(status)
        self.audio_sync_warning = AudioSyncWarning(self, korean=self.korean, volume_slider=self.volume_slider)
        transport_layout.addWidget(self.audio_sync_warning)
        transport_layout.addWidget(self.save_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addWidget(self.tools_bar)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(transport)

        for sequence, callback in (
            ("Space", lambda: self.play_button.toggle()),
            ("L", self.loop_button.toggle),
            ("B", lambda: self.compare_button.isEnabled() and self.compare_button.toggle()),
            (QKeySequence.StandardKey.Undo, self._undo),
            (QKeySequence.StandardKey.Redo, self._redo),
            ("Ctrl+Shift+Z", self._redo),
            ("Alt+Left", lambda: self._user_select(self._index - 1)),
            ("Alt+Right", lambda: self._user_select(self._index + 1)),
            ("Ctrl+1", lambda: self._set_advanced(False)),
            ("Ctrl+2", lambda: self._set_advanced(True)),
            ("S", self._toggle_snap),
            (QKeySequence.StandardKey.Copy, self._copy),
            (QKeySequence.StandardKey.Paste, self._paste),
            ("Ctrl+Shift+V", lambda: self._paste(cues=True)),
            ("Ctrl+Backspace", self._reset),
            ("Ctrl+=", lambda: self.timeline.zoom(0.8)),
            ("Ctrl++", lambda: self.timeline.zoom(0.8)),
            ("Ctrl+-", lambda: self.timeline.zoom(1.25)),
            ("Ctrl+0", self._fit),
            ("Home", lambda: self._seek_local(0.0)),
            ("Shift+F1", self._show_shortcuts),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)

    # -- plan and selection ----------------------------------------------------------

    def _context(self) -> EditContext:
        return EditContext({track.id: track for track in self._tracks},
                           dict(self.host.automix_analyses), dict(self.host.automix_structures))

    @property
    def junction(self):
        return self._junctions[self._index] if 0 <= self._index < len(self._junctions) else None

    def _key(self, index: int | None = None) -> str:
        index = self._index if index is None else index
        if not 0 <= index < len(self._junctions):
            return ""
        junction = self._junctions[index]
        return pair_key(junction.outgoing.track_id, junction.incoming.track_id)

    def _replan(self) -> None:
        """Re-plan the whole playlist (CPU only): later transitions follow an edit at once."""
        context = self._context()
        self._plan = compile_automix(
            self._tracks, context.analyses, self._settings.with_overrides(self._overrides),
            context.structures, log_diagnostics=False,
        )
        self._junctions = plan_junctions(self._plan)
        self.timeline.duration = self._plan.duration_seconds
        self._fill_combo()

    def _automatic_plan(self):
        """The plan with the selected junction on automatic (every other one as set), cached per edit state."""
        key = self._key()
        others = tuple(sorted(((name, value) for name, value in self._overrides.items() if name != key),
                              key=lambda item: item[0]))
        if self._automatic is None or self._automatic[0] != (key, others):
            context = self._context()
            plan = compile_automix(self._tracks, context.analyses, self._settings.with_overrides(dict(others)),
                                   context.structures, log_diagnostics=False)
            junctions = {pair_key(junction.outgoing.track_id, junction.incoming.track_id): junction
                         for junction in plan_junctions(plan)}
            self._automatic = ((key, others), plan, junctions)
        return self._automatic[1]

    def _automatic_junction(self):
        if not self._key():
            return None
        self._automatic_plan()
        return self._automatic[2].get(self._key())

    def _fill_combo(self) -> None:
        self.transition_combo.blockSignals(True)
        self.transition_combo.clear()
        titles = {track.id: track.title or track.filename for track in self._tracks}
        for index, junction in enumerate(self._junctions):
            manual = "✎ " if self._key(index) in self._overrides else ""
            text = (f"{manual}{junction.index:02d} → {junction.index + 1:02d}   "
                    f"{titles.get(junction.outgoing.track_id, '')} → {titles.get(junction.incoming.track_id, '')}")
            self.transition_combo.addItem(text)
            self.transition_combo.setItemData(index, text, Qt.ItemDataRole.ToolTipRole)
        self.transition_combo.setCurrentIndex(self._index)
        self.transition_combo.setToolTip(self.transition_combo.currentText())
        self.transition_combo.blockSignals(False)

    def select_pair(self, outgoing_track_id: str, incoming_track_id: str) -> bool:
        """Select the junction between two tracks (e.g. the playlist's transition chip)."""
        for index, junction in enumerate(self._junctions):
            if (junction.outgoing.track_id, junction.incoming.track_id) == (outgoing_track_id, incoming_track_id):
                self._user_select(index)
                return True
        return False

    def _user_select(self, index: int) -> None:
        if not self._junctions:
            return
        index = min(max(index, 0), len(self._junctions) - 1)
        if index != self._index:
            self._select(index, refit=True)

    def _select(self, index: int, *, refit: bool = False) -> None:
        # A number still being typed belongs to the junction it was typed for.
        self.properties.flush()
        self._index = min(max(index, 0), len(self._junctions) - 1) if self._junctions else -1
        self.transition_combo.setCurrentIndex(self._index)
        self.transition_combo.setToolTip(self.transition_combo.currentText())
        if refit:
            self.compare_button.blockSignals(True)
            self.compare_button.setChecked(False)
            self.compare_button.blockSignals(False)
            self.mine_button.setChecked(True)
            self._stop()
            self._audio = None
            self.timeline.audition = None
            self._set_playhead(None)
        self._show(refit=refit)

    def _selection_changed(self, selection: str) -> None:
        """The timeline selected another target: the panel shows its settings (nothing is re-planned)."""
        self._selection = selection
        self.properties.set_selection(selection)

    def _show_tempo(self) -> None:
        """Enable tempo matching if available, or focus the existing tempo settings."""
        self.properties.flush()
        self.timeline.selected_part = "ramp"
        self.timeline.set_selection("tempo")
        self._selection_changed("tempo")
        self.properties_button.setChecked(True)
        if self.properties.tempo_check.isEnabled():
            self.properties.tempo_check.setChecked(True)

    def _show(self, *, refit: bool = False, drawn=None, drawn_override=None) -> None:
        """Draw the selected junction and fill the side panel (``drawn``: a drag's draft and its override)."""
        junction = self.junction
        context = self._context()
        analyses = context.analyses
        pair = (None, None) if junction is None else (analyses.get(junction.outgoing.track_id),
                                                     analyses.get(junction.incoming.track_id))
        self.timeline.korean = self.korean
        self.timeline.titles = {track.id: track.title or track.filename for track in self._tracks}
        self.timeline.analyses = pair
        self.timeline.draft = drawn is not None
        self.timeline.locked = (drawn_override or self._base()).locked if junction is not None else ()
        self.timeline.set_junction(drawn or junction, refit=refit)
        self.previous_button.setEnabled(self._index > 0)
        self.next_button.setEnabled(0 <= self._index < len(self._junctions) - 1)
        self.properties.setVisible(junction is not None)
        self._refresh_snap_choices(pair)
        if junction is None:
            self.properties.set_state(None, manual=False, pair_text="", songs_html="")
            self._set_audition_text()
            return
        if drawn is None:
            self.audition.load_peaks([track for track in self._tracks
                                      if track.id in (junction.outgoing.track_id, junction.incoming.track_id)])
            self._request_audition()
        self._fill_properties(drawn or junction, pair, drawn_override)
        self._refresh_actions()

    def _fill_properties(self, junction, analyses, override=None) -> None:
        key = self._key()
        stored = self._overrides.get(key)
        manual = (stored is not None and not stored.recommend) or override is not None
        recommend = override is None and stored is not None and stored.recommend
        shown = override or self._base()
        titles = {track.id: track.title or track.filename for track in self._tracks}
        durations = tuple(next((t.duration_seconds for t in self._tracks if t.id == clip.track_id), clip.source_out)
                          for clip in (junction.outgoing, junction.incoming))
        transition = junction.transition
        style = transition_dsp_style(transition) if transition is not None else None
        details = dict(transition.details) if transition is not None else {}
        korean = self.korean
        songs = "<br>".join(
            f"<span style='color:{color.name()}'>■</span> <b>{letter}</b>&nbsp; {html.escape(titles.get(clip.track_id, ''))}"
            for color, letter, clip in ((OUTGOING_COLOR, "A", junction.outgoing), (INCOMING_COLOR, "B", junction.incoming)))
        rate = details.get("outgoing_rate")
        tempo = self._text("템포 변화 없음", "No tempo change")
        if analyses[0] is not None and analyses[1] is not None and analyses[0].bpm and analyses[1].bpm:
            tempo = f"A {analyses[0].bpm:.1f} · B {analyses[1].bpm:.1f} BPM"
            if isinstance(rate, (int, float)) and abs(rate - 1.0) > 1e-9:
                ramp = float(details.get("tempo_ramp_seconds") or 0.0)
                tempo += (self._text(f" · A를 {analyses[0].bpm * rate:.1f} BPM으로 {ramp:.1f}초 동안 변경",
                                     f" · A eases to {analyses[0].bpm * rate:.1f} BPM over {ramp:.1f} s")
                          if ramp > 1e-6 else
                          self._text(f" · A를 {analyses[0].bpm * rate:.1f} BPM으로 바로 변경",
                                     f" · A jumps to {analyses[0].bpm * rate:.1f} BPM"))
                tempo += self._text(" (타임라인의 '템포 변경 시작' 핸들로 시작점 조정)",
                                    " (drag 'Tempo change starts' on the timeline to move where it begins)")
            elif shown.tempo_match:
                tempo += self._text(" · 템포가 이미 같아 바꿀 필요가 없음", " · tempos already match")
        length = junction.end - junction.start
        facts = [
            (self._text("믹스 시작", "Mix starts"), clock(junction.start)),
            (self._text("믹스 끝", "Mix ends"), clock(junction.end)),
            (self._text("겹침", "Overlap"), f"{length:.2f} s" + (f" · {b}" if (b := bars_text(length, analyses[1], korean)) else "")
             if length > 0 else self._text("없음 (컷)", "none (cut)")),
            (self._text("A 큐 (원본)", "A cue (source)"), clock(junction.outgoing.source_at(junction.start))),
            (self._text("B 시작 (원본)", "B starts (source)"), clock(junction.incoming.source_in)),
            (self._text("화면 전환", "Canvas switch"), clock(junction.handover)),
        ]
        automatic = self._automatic_junction()
        changed = changed_items(junction, automatic, override or stored) if manual or recommend else set()
        notes = []
        if recommend:
            notes = [note] if (note := recommendation_note(junction, stored, korean)) else []
        elif manual and override is None:
            notes = applied_notes(shown, junction, durations, korean)

        def song(clip, analysis, when: str) -> str:
            facts_ = [titles.get(clip.track_id, "")]
            if analysis is not None and analysis.bpm:
                facts_.append(f"{analysis.bpm:.1f} BPM" + (f" · {analysis.key}" if analysis.key else ""))
            facts_.append(when)
            if analysis is None or not snap_units(analysis):
                facts_.append(self._text("박자 정보가 불확실해 초 단위로만 맞춥니다.",
                                         "Beat analysis is uncertain: seconds only."))
            return "\n".join(facts_)

        self.properties.set_state(
            shown, manual=manual,
            pair_text=self._text(f"전환 {self._index + 1} / {len(self._junctions)}",
                                 f"Transition {self._index + 1} of {len(self._junctions)}"),
            songs_html=songs, analyses=analyses, durations=durations,
            band_style=style in BAND_ENVELOPES, auto_style=style_label(style_key(junction), korean),
            tempo_text=tempo, facts=facts, changed=sorted(changed), notes=notes,
            summary=self._advanced_summary(changed, shown), recommend=recommend,
            bands=band_windows_of(transition) if style in BAND_ENVELOPES else None,
            cue_facts=(song(junction.outgoing, analyses[0],
                            self._text(f"믹스 {clock(junction.start)}에 다음 곡과 섞이기 시작",
                                       f"Starts mixing at {clock(junction.start)} in the mix")),
                       song(junction.incoming, analyses[1],
                            self._text(f"믹스 {clock(junction.incoming.timeline_start)}부터 재생",
                                       f"Plays from {clock(junction.incoming.timeline_start)} in the mix"))),
        )

    def _advanced_summary(self, changed: set[str], override) -> str:
        """Advanced settings in force, in a line simple mode can show without their controls."""
        names = {"outgoing_cue": ("A 큐", "A cue"), "incoming_cue": ("B 시작", "B start"),
                 "tempo": ("템포", "tempo"), "bands": ("대역 타이밍", "band timing"),
                 "effect": ("효과 세부값", "effect details")}
        parts = [names[item][0 if self.korean else 1] for item in names if item in changed]
        kept = [FIELD_NAMES[name][0 if self.korean else 1] for name in override.locked]
        if kept:
            parts.append(self._text(f"{', '.join(kept)} 고정", f"{', '.join(kept)} kept"))
        return self._text("고급 설정 적용 중 · ", "Advanced settings in force · ") + " · ".join(parts) if parts else ""

    # -- editing -------------------------------------------------------------------------

    def _base(self):
        """What the selected junction plays, as an override: the one set by hand, else prefilled from the plan
        (a recommendation keeping values plays what analysis picked around them)."""
        stored = self._overrides.get(self._key())
        if stored is not None and not stored.recommend:
            return stored
        base = override_from_junction(self.junction)
        return replace(base, locked=stored.locked) if stored is not None else base

    def _commit(self, override, text: str) -> None:
        """One undo step to ``override`` (None: automatic again)."""
        key = self._key()
        before = self._overrides.get(key)
        if not key or override == before:
            return
        self.compare_button.setChecked(False)  # an edit is heard as edited
        self.undo_stack.push(_OverrideCommand(self, key, before, override, text))

    def _request_audition(self) -> None:
        """Audition the selected window: as edited, or -- comparing -- as analysis would mix it."""
        comparing = self.compare_button.isChecked() and self._key() in self._overrides
        self.audition.request(self._automatic_plan() if comparing else self._plan, self._index, self._tracks)
        self._set_audition_text()

    def _compare_toggled(self, checked: bool) -> None:
        self.mine_button.setChecked(not checked)
        if self.junction is not None:
            self._request_audition()

    def _hear_mine(self) -> None:
        self.mine_button.setChecked(True)
        self.compare_button.setChecked(False)

    # -- user presets: how a transition mixes, saved by name -------------------------------

    def _load_presets(self) -> dict:
        import json

        from app.automix.overrides import parse_overrides

        try:
            data = json.loads(str(QSettings().value(_PRESETS_KEY, "{}") or "{}"))
        except ValueError:
            return {}
        # Stored as "preset>name" so parse_overrides' key check and validation apply unchanged.
        return {key[len("preset>"):]: value for key, value in parse_overrides(data).items()
                if key.startswith("preset>")}

    def _save_presets(self, presets: dict) -> None:
        import json

        QSettings().setValue(_PRESETS_KEY, json.dumps(
            {f"preset>{name}": override.to_dict() for name, override in presets.items()}, ensure_ascii=False))

    def _fill_presets_menu(self) -> None:
        menu = self.presets_menu
        menu.clear()
        presets = self._load_presets()
        save = menu.addAction(self._text("현재 설정을 프리셋으로 저장…", "Save these settings as a preset…"))
        save.setEnabled(self.junction is not None)
        save.triggered.connect(self._save_preset)
        if not presets:
            menu.addAction(self._text("저장된 프리셋 없음", "No saved presets")).setEnabled(False)
            return
        menu.addSeparator()
        scope = menu.addAction(self._text("적용 범위: 스타일·길이·템포·대역·효과 (곡별 큐와 고정값은 그대로)",
                                          "Applies style, length, tempo, bands, effects (cues and kept values stay)"))
        scope.setEnabled(False)
        for name, preset in sorted(presets.items()):
            action = menu.addAction(f"{name}  ·  {style_label(preset.style, self.korean)} {preset.duration:g}s")
            action.setEnabled(self.junction is not None)
            action.triggered.connect(lambda _checked=False, name=name: self._apply_preset(name))
        remove = menu.addMenu(self._text("프리셋 삭제", "Delete a preset"))
        for name in sorted(presets):
            remove.addAction(name).triggered.connect(lambda _checked=False, name=name: self._delete_preset(name))

    def _save_preset(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, accepted = QInputDialog.getText(self, self._text("프리셋 저장", "Save preset"),
                                              self._text("프리셋 이름", "Preset name"))
        name = name.strip()
        if accepted and name:
            self.properties.flush()
            presets = self._load_presets()
            presets[name] = replace(self._base(), outgoing_cue=0.0, incoming_cue=0.0, locked=(), recommend=False)
            self._save_presets(presets)
            self._say(self._text(f"프리셋 ‘{name}’ 저장: 스타일·길이·템포·대역·효과 (큐 제외)",
                                 f"Saved preset “{name}”: style, length, tempo, bands, effects (no cues)"))

    def _apply_preset(self, name: str) -> None:
        preset = self._load_presets().get(name)
        if preset is not None and self.junction is not None:
            self._apply_settings(preset, self._text(f"프리셋 적용: {name}", f"Apply preset: {name}"))

    def _delete_preset(self, name: str) -> None:
        presets = self._load_presets()
        if presets.pop(name, None) is not None:
            self._save_presets(presets)

    def _store(self, key: str, override) -> None:
        """Apply one undo step: the project, the whole plan, the view (selecting what changed)."""
        if override is None:
            self._overrides.pop(key, None)
        else:
            self._overrides[key] = override
        self._changed_keys.add(key)
        self.override_changed.emit(key, override)
        current = self._key()
        self._replan()
        index = next((i for i in range(len(self._junctions)) if self._key(i) == key), None)
        if index is None:
            index = next((i for i in range(len(self._junctions)) if self._key(i) == current), self._index)
        if self._key(index) != current:  # undo/redo of another transition: go and show it
            self._select(index, refit=True)
            return
        self._index = index
        self.transition_combo.setCurrentIndex(self._index)
        self._show()

    def _edit(self, changes: dict, text: str) -> None:
        junction = self.junction
        if junction is None:
            return
        bands = band_windows_of(junction.transition) if junction.transition is not None else None
        try:
            override = edited_override(self._base(), changes, bands)
        except ValueError:
            self._show()
            return
        self._commit(override, text)

    def _bands_changed(self, bands) -> None:
        self.properties.flush()
        self._edit({"eq_bands": bands}, self._text("대역 타이밍", "Band timing"))

    def _properties_changed(self, changes: dict) -> None:
        names = {"style": ("스타일", "Style"), "duration": ("겹침 길이", "Overlap length"),
                 "outgoing_cue": ("A 큐", "A cue"), "incoming_cue": ("B 시작", "B start"),
                 "tempo_match": ("템포 맞춤", "Match tempo"), "vocal_handoff": ("보컬 교대", "Vocal handoff"),
                 "eq_bands": ("대역 타이밍", "Band timing"), "ramp_seconds": ("템포 변경 구간", "Tempo ramp"),
                 "key_shift": ("키 맞춤", "Key match")}
        name = names.get(next(iter(changes), ""), ("편집", "Edit"))
        self._edit(changes, name[0 if self.korean else 1])
        if changes.get("tempo_match") is False and self._selection == "tempo":
            self.timeline.selected_part = None
            self.timeline.set_selection("transition")
            self._selection_changed("transition")

    def _reset(self) -> None:
        """Automatic again; with kept values, a new recommendation that keeps them instead."""
        self.properties.flush()
        stored = self._overrides.get(self._key())
        if stored is not None and stored.locked:
            self._recommend()
        elif stored is not None:
            self._commit(None, self._text("자동으로 되돌리기", "Back to automatic"))

    def _recommend(self) -> None:
        """Ask analysis again, keeping the locked values (the planner says if nothing can keep them)."""
        self.properties.flush()
        base = self._base()
        if not base.locked or self.junction is None:
            return
        # Every current value is stored: if no recommendation keeps the locked ones, these keep playing.
        self._commit(replace(base, recommend=True), self._text("자동 추천 다시 받기", "Recommend again"))
        stored = self._overrides.get(self._key())
        details = dict(self.junction.transition.details) if self.junction.transition is not None else {}
        if stored is not None and stored.recommend and details.get("recommendation") == "no_fit":
            self._say(recommendation_note(self.junction, stored, self.korean))

    def _reset_all(self) -> None:
        """The whole transition back on automatic, kept values included."""
        self.properties.flush()
        if self._key() in self._overrides:
            self._commit(None, self._text("전환 전체 초기화", "Reset the whole transition"))

    def _reset_item(self, item: str) -> None:
        """One setting back on what the automatic plan does here."""
        self.properties.flush()
        automatic = self._automatic_junction()
        if automatic is None or self.junction is None:
            return
        target = override_from_junction(automatic)
        override = reset_item(self._base(), target, item)
        if not override.locked and replace(override, recommend=False) == target:
            override = None  # nothing left that differs: automatic again
        names = {"style": ("스타일", "Style"), "duration": ("길이", "Length"), "outgoing_cue": ("A 큐", "A cue"),
                 "incoming_cue": ("B 시작", "B start"), "tempo": ("템포", "Tempo"), "bands": ("대역", "Bands"),
                 "effect": ("효과 세부값", "Effect details")}[item]
        self._commit(override, self._text(f"{names[0]} 자동값으로", f"{names[1]} back to automatic"))

    def _toggle_lock(self, field: str, locked: bool) -> None:
        """Keep (or stop keeping) one value through new recommendations: stored with the transition."""
        self.properties.flush()
        if self.junction is None:
            return
        stored = self._overrides.get(self._key())
        base = self._base()
        names = {*base.locked, field} if locked else set(base.locked) - {field}
        names = tuple(name for name in ("outgoing_cue", "incoming_cue", "duration") if name in names)
        if stored is None or stored.recommend:
            # Still automatic: analysis keeps recommending around the kept values.
            override = replace(base, locked=names, recommend=True) if names else None
        else:
            override = replace(stored, locked=names)
        name = FIELD_NAMES[field][0 if self.korean else 1]
        self._commit(override, self._text(f"{name} 고정", f"Keep {name}") if locked
                     else self._text(f"{name} 고정 해제", f"Stop keeping {name}"))

    def _undo(self) -> None:
        self.properties.flush()
        self.undo_stack.undo()

    def _redo(self) -> None:
        self.properties.flush()
        self.undo_stack.redo()

    def _copy(self) -> None:
        if self.junction is not None:
            self.properties.flush()
            self._copied = self._base()
            self._refresh_actions()
            self._say(self._text("전환 설정을 복사했습니다 (스타일·길이·템포·대역·효과, 큐 위치 포함 보관)",
                                 "Copied this transition's settings (style, length, tempo, bands, effects; cues kept aside)"))

    def _paste(self, cues: bool = False) -> None:
        if self._copied is not None and self.junction is not None:
            self._apply_settings(self._copied, self._text("전환 설정 붙여넣기", "Paste transition settings"),
                                 cues=cues)

    def _apply_settings(self, source, text: str, *, cues: bool = False) -> None:
        """How ``source`` mixes (style, length, tempo, handoff, bands) on the selected transition, keeping its
        cues (unless ``cues``: pasting where the same songs meet) and every value the user locked."""
        self.properties.flush()
        base = self._base()
        fields = _COPIED_FIELDS + (SONG_FIELDS if cues else ())
        values = {name: getattr(source, name) for name in fields if name not in base.locked}
        try:
            override = replace(base, **values, recommend=False)
        except ValueError:
            return
        korean = 0 if self.korean else 1
        changed = list(dict.fromkeys(_FIELD_GROUPS[name][korean] for name in fields
                                     if getattr(override, name) != getattr(base, name)))
        kept = [FIELD_NAMES[name][korean] for name in base.locked if name in fields]
        self._commit(override, text)
        message = text + " · " + (self._text("바뀐 항목: ", "changed: ") + ", ".join(changed) if changed
                                  else self._text("바뀐 항목 없음", "nothing changed"))
        if not cues:
            message += self._text(" · 곡별 큐 위치는 그대로", " · this pair's cues stay")
        if kept:
            message += self._text(f" · 고정한 {', '.join(kept)} 유지", f" · kept {', '.join(kept)}")
        self._say(message)

    def _nudge(self, part: str, direction: int, fine: bool) -> None:
        """Move one part by a beat of its song (0.01 s with Shift, or without a trusted beat grid), one undo step."""
        junction = self.junction
        if junction is None:
            return
        self.properties.flush()
        analyses = self._context().analyses
        if part.startswith("band:"):
            bands = band_windows_of(junction.transition) if junction.transition is not None else None
            analysis = analyses.get(junction.incoming.track_id)
            length = junction.end - junction.start
            if bands is None or length <= 0.0:
                return
            step = (0.01 if fine or analysis is None or not analysis.bpm else 60.0 / analysis.bpm) / length
            band = part[5:]
            out_window, in_window = bands[("low", "mid", "high").index(band)]
            moved = with_band(bands, band, move_window(out_window, "move", direction * step),
                              move_window(in_window, "move", direction * step))
            self._edit({"eq_bands": moved}, self._text("대역 미세 이동", "Nudge band"))
            return
        analysis = analyses.get((junction.incoming if part in ("end", "incoming") else junction.outgoing).track_id)
        beat = 60.0 / analysis.bpm if analysis is not None and analysis.bpm and snap_units(analysis) else None
        step = 0.01 if fine or beat is None else beat
        override, hint, _guide = drag_override(
            self._junctions, self._index, junction, self._base(), part, direction * step, self._context(),
            snapping=False, tolerance=0.0, korean=self.korean)
        if override is None:
            self._say(hint)
            return
        self._commit(override, self._text("미세 이동", "Nudge"))

    # -- snapping ------------------------------------------------------------------------

    def _snap_unit(self) -> str:
        return self.snap_combo.currentData() or "off"

    def _snap_unit_for(self, part: str) -> str:
        """The chosen unit, stepped down to what the dragged song's analysis can be trusted for."""
        junction = self.junction
        unit = self._snap_unit()
        if unit == "off" or junction is None:
            return unit
        analyses = self._context().analyses
        clip = junction.incoming if part in ("end", "incoming") else junction.outgoing
        units = snap_units(analyses.get(clip.track_id))
        return unit if unit in units else "beat" if "beat" in units else "off"

    def _refresh_snap_choices(self, pair) -> None:
        """Offer beats and bars only where some song's analysis can be trusted for them."""
        units = {unit for analysis in pair for unit in snap_units(analysis)}
        model = self.snap_combo.model()
        for row in range(self.snap_combo.count()):
            unit = self.snap_combo.itemData(row)
            model.item(row).setEnabled(unit == "off" or unit in units)
        self.timeline.snap_unit = self._snap_unit()

    def _snap_changed(self, _index: int) -> None:
        self.timeline.snap_unit = self._snap_unit()
        if self._snap_unit() != "off":
            self._last_snap = self._snap_unit()

    def _toggle_snap(self) -> None:
        unit = "off" if self._snap_unit() != "off" else getattr(self, "_last_snap", "beat")
        self.snap_combo.setCurrentIndex(self.snap_combo.findData(unit))

    # -- dragging --------------------------------------------------------------------------

    def _drag_started(self, _part: str) -> None:
        self.properties.flush()
        self._drag_base = (self._base(), self.timeline.junction)
        self._drag_override = None

    def _drag_moved(self, part: str, delta: float, invert: bool) -> None:
        if self._drag_base is None:
            return
        base, drawn = self._drag_base
        unit = self._snap_unit_for(part)
        override, hint, guide = drag_override(
            self._junctions, self._index, drawn, base, part, delta, self._context(),
            snapping=(unit != "off") != invert,  # Shift flips snapping while held
            tolerance=12.0 * self.timeline.seconds_per_pixel(), korean=self.korean,
            unit=unit if unit != "off" else "beat")
        self.timeline.drag_hint = hint
        self.timeline.snap_guide = guide
        if override is None or override == self._drag_override:
            self.timeline.update()
            return
        self._drag_override = override
        from app.widgets.transition_editor import draft_junction

        draft = draft_junction(self._junctions, self._index, override, self._context())
        if draft is not None:
            self._show(drawn=draft, drawn_override=override)

    def _drag_finished(self) -> None:
        override, self._drag_override, self._drag_base = self._drag_override, None, None
        if override is None:
            self._show()
            return
        if override == self._overrides.get(self._key()):
            self._show()
        else:
            self._commit(override, self._text("타임라인 편집", "Timeline edit"))

    def _drag_cancelled(self) -> None:
        """Esc mid-drag: nothing was stored; show the committed plan again."""
        self._drag_override = self._drag_base = None
        self._show()
        self._say(self._text("끌기를 취소했습니다", "Drag cancelled"))

    # -- audition --------------------------------------------------------------------------

    def _on_audition_state(self, state: str, detail: str) -> None:
        self._audition_state = (state, detail)
        self._set_audition_text()
        self._sync_activity(state == RENDERING)

    def _sync_activity(self, rendering: bool) -> None:
        """A window render also shows in the main window's status-bar activity line."""
        bar = activity_for(self.host)
        if bar is None:
            return
        if rendering:
            bar.update(AUDITION_ACTIVITY, None,
                       label=self._text("전환 미리듣기 준비", "Preparing transition audition"),
                       detail=self._text("선택한 전환의 앞뒤 구간만 렌더합니다.",
                                         "Rendering just the selected transition's window."))
        else:
            bar.finish(AUDITION_ACTIVITY)

    def _set_audition_text(self) -> None:
        """Whether what is heard is what is drawn: preparing, up to date, out of date, or failed."""
        state, detail = self._audition_state
        heard_old = self._audio is not None and state in (WAITING, RENDERING, FAILED)
        self.timeline.audition_stale = heard_old
        shown = state
        if self.junction is None:
            text, tip = self._text("전환 없음", "No transitions"), ""
        elif state == UNAVAILABLE:
            text = self._text("● 미리듣기 불가 · FFmpeg 없음", "● No audition · FFmpeg not found")
            tip = self._text("설정에서 FFmpeg 경로를 지정하면 전환 구간을 들을 수 있습니다.",
                             "Set the FFmpeg path in Settings to hear transitions.")
        elif state in (WAITING, RENDERING) and heard_old:
            shown = "stale"
            text = self._text("● 갱신 필요 · 지금은 이전 편집이 들림 · ",
                              "● Out of date · you hear the previous edit · ") + (
                self._text("구간 렌더 중…", "rendering the window…") if state == RENDERING
                else self._text("곧 다시 렌더", "re-rendering shortly"))
            tip = self._text("이 전환 구간만 다시 렌더합니다. 준비되면 같은 위치에서 새 편집으로 바뀝니다.",
                             "Only this window re-renders; it swaps in at the same spot when ready.")
        elif state in (WAITING, RENDERING):
            text = self._text("● 준비 중 · 이 전환 구간만 렌더합니다", "● Preparing · rendering just this window")
            tip = self._text("전체 믹스는 다시 만들지 않습니다.", "The whole mix is never re-rendered.")
        elif state == READY and self._audio is not None:
            _path, _origin, start, end = self._audio
            text = self._text(f"● 최신 편집 반영됨 · {_clock(start, precise=True)}–{_clock(end, precise=True)}",
                              f"● Up to date · {_clock(start, precise=True)}–{_clock(end, precise=True)}")
            tip = self._text("최종 내보내기와 같은 계획·DSP로 렌더한 구간입니다.",
                             "Rendered with the same plan and DSP as the final export.")
        elif state == FAILED:
            reason = detail.splitlines()[0] if detail else ""
            reason = reason if len(reason) <= 60 else reason[:59] + "…"
            text = self._text("● 렌더 실패", "● Render failed") + (f" · {reason}" if reason else "") + (
                self._text(" · 이전 편집이 들림", " · you hear the previous edit") if heard_old else "")
            tip = detail
        else:
            text, tip = "", ""
        if text and self.compare_button.isEnabled():
            text = (self._text("듣는 중: 자동 결과 · ", "Hearing: automatic · ") if self.compare_button.isChecked()
                    else self._text("듣는 중: 내 편집 · ", "Hearing: my edit · ")) + text
        self.state_label.setText(text)
        self.state_label.setToolTip(tip)
        self.state_label.setProperty("state", shown)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)
        self.retry_button.setVisible(state == FAILED)
        playable = self._audio is not None or state in (WAITING, RENDERING, READY)
        self.play_button.setEnabled(playable and state != UNAVAILABLE and self.junction is not None)
        self.timeline.update()

    def _on_peaks(self, track_id: str, peaks) -> None:
        self.timeline.peaks[track_id] = peaks
        self.timeline.update()

    def _ensure_player(self):
        if self._player is None:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self._audio_output = QAudioOutput(self)
            self.audio_sync_warning.bind_output(self._audio_output)
            self._audio_output.setVolume(self.volume_slider.value() / 100.0)
            self._player = QMediaPlayer(self)
            self._player.setAudioOutput(self._audio_output)
            self.audio_sync_warning.bind_player(self._player)
            self._player.positionChanged.connect(self._position_changed)
            self._player.mediaStatusChanged.connect(self._media_status)
        return self._player

    def _on_audio_ready(self, path: str, origin: float, start: float, end: float) -> None:
        """Swap in the new window at the same mix position (the old one played until now).

        File second t plays mix second ``origin + t``; ``start..end`` is the window heard and looped.
        """
        playing = self.play_button.isChecked()
        self._audio = (path, origin, start, end)
        self.timeline.audition = (start, end)
        position = self._playhead if self._playhead is not None and start <= self._playhead < end else start
        self._pending_position = position - origin
        self.position_slider.setRange(0, round((end - start) * 1000))
        player = self._ensure_player()
        player.setSource(QUrl.fromLocalFile(path))
        if playing or self._play_when_ready:
            self._play_when_ready = False
            self.play_button.setChecked(True)
            player.play()
        self._set_playhead(position)
        self._set_audition_text()

    def _media_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia) \
                and self._pending_position is not None:
            self._player.setPosition(round(self._pending_position * 1000))
            self._pending_position = None
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.loop_button.isChecked():
                self._seek_local(0.0)
                self._player.play()
            else:
                self.play_button.setChecked(False)
                self._seek_local(0.0)

    def _position_changed(self, milliseconds: int) -> None:
        if self._audio is None:
            return
        _path, origin, start, end = self._audio
        seconds = origin + milliseconds / 1000.0
        if self.loop_button.isChecked() and self.play_button.isChecked() and seconds >= end - 0.03:
            self._seek_local(0.0)
            return
        self._set_playhead(seconds)

    def _set_playhead(self, seconds: float | None) -> None:
        self._playhead = seconds
        self.timeline.playhead = seconds
        self.timeline.update()
        if self._audio is None or seconds is None:
            self.time_label.setText("–:––.– / –:––.–")
            return
        _path, _origin, start, end = self._audio
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(round((seconds - start) * 1000))
        self.time_label.setText(f"{_clock(seconds, precise=True)} / {_clock(end, precise=True)}")

    def _set_play_text(self) -> None:
        self.play_button.setText(("Ⅱ  " + self._text("일시정지", "Pause")) if self.play_button.isChecked()
                                 else ("▶  " + self._text("재생", "Play")))

    def _toggle_play(self, playing: bool) -> None:
        self._set_play_text()
        if self._audio is None:
            if playing:
                self._play_when_ready = True
                self.audition.flush()
            return
        player = self._ensure_player()
        if playing:
            player.play()
        else:
            player.pause()

    def _stop(self) -> None:
        self._play_when_ready = False
        if self.play_button.isChecked():
            self.play_button.setChecked(False)
        if self._player is not None:
            self._player.pause()
        self._seek_local(0.0)

    def _seek(self, seconds: float) -> None:
        """A mix second, held inside the window heard."""
        if self._audio is None:
            return
        _path, _origin, start, end = self._audio
        self._seek_local(min(max(seconds, start), end) - start)

    def _seek_local(self, seconds: float) -> None:
        """Seconds from the window's start."""
        if self._audio is None:
            return
        _path, origin, start, end = self._audio
        seconds = min(max(0.0, seconds), end - start)
        if self._player is not None:
            self._player.setPosition(round((start - origin + seconds) * 1000))
        self._set_playhead(start + seconds)

    def _loop_toggled(self, looping: bool) -> None:
        self.timeline.loop = looping
        self.timeline.update()

    def _volume_changed(self, value: int) -> None:
        if self._player is not None:
            self._audio_output.setVolume(value / 100.0)

    # -- outside changes -------------------------------------------------------------------

    def _queue_analysis_arrived(self, _results=None) -> None:
        # Queued behind MainWindow's own slot, which merges the new results first.
        QTimer.singleShot(0, self, self._analysis_arrived)

    def _analysis_arrived(self) -> None:
        """New analysis re-plans in place: the selected transition, its selected target and the view stay put."""
        if self._finished:
            return
        self.properties.flush()  # a value typed a moment ago is stored before the re-plan redraws the panel
        key = self._key()
        self._automatic = None
        self._replan()
        self._index = next((i for i in range(len(self._junctions)) if self._key(i) == key),
                           min(self._index, len(self._junctions) - 1))
        self.transition_combo.setCurrentIndex(self._index)
        self._show()

    def _playlist_changed(self) -> None:
        if self._finished:
            return
        self._tracks = [track for track in self.host.playlist_service.tracks if track.enabled]
        self._analysis_arrived()

    # -- workspace -----------------------------------------------------------------------------

    def _restore_workspace(self) -> None:
        settings = QSettings()
        state = settings.value(_SPLITTER_KEY)
        if state:
            self.splitter.restoreState(state)
        self.properties_button.setChecked(bool(settings.value(_PANEL_KEY, True, bool)))
        self.lanes_button.setChecked(bool(settings.value(_LANES_KEY, True, bool)))
        index = self.snap_combo.findData(str(settings.value(_SNAP_KEY, "beat") or "beat"))
        self.snap_combo.setCurrentIndex(index if index >= 0 else 1)

    def _save_workspace(self) -> None:
        settings = QSettings()
        settings.setValue(_SPLITTER_KEY, self.splitter.saveState())
        settings.setValue(_PANEL_KEY, self.properties_button.isChecked())
        settings.setValue(_LANES_KEY, self.lanes_button.isChecked())
        settings.setValue(_SNAP_KEY, self._snap_unit())

    def _reset_workspace(self) -> None:
        """The default layout: panel shown at its width, band lanes shown, beat snap, the transition in view."""
        self.properties_button.setChecked(True)
        self.lanes_button.setChecked(True)
        self.snap_combo.setCurrentIndex(self.snap_combo.findData("beat"))
        self.splitter.setSizes([max(200, self.splitter.width() - PANEL_WIDTH), PANEL_WIDTH])
        self._fit()
        self._save_workspace()
        self._say(self._text("작업 공간을 기본 배치로 되돌렸습니다", "Workspace back to the default layout"))

    # -- chrome -------------------------------------------------------------------------------

    def _text(self, korean: str, english: str) -> str:
        return korean if self.korean else english

    def _say(self, text: str) -> None:
        """A short-lived note about what the last action did."""
        self.message_label.setText(text)
        self.message_label.setToolTip(text)
        self._message_timer.start()

    def _set_advanced(self, advanced: bool, *, save: bool = True) -> None:
        """Simple and advanced show the same edit; switching never changes a value."""
        self.properties.flush()
        self.advanced = advanced
        (self.advanced_button if advanced else self.simple_button).setChecked(True)
        self.properties.set_advanced(advanced)
        self.timeline.set_advanced(advanced)
        self.tempo_button.setVisible(advanced)
        self.lanes_button.setVisible(advanced)
        if advanced:  # the target selected before switching to simple comes back
            self.timeline.set_selection(self._selection)
            self.properties.set_selection(self._selection)
        if save:
            QSettings().setValue(_SETTINGS_KEY, advanced)

    def _toggle_properties(self, visible: bool) -> None:
        self.properties_scroll.setVisible(visible)

    def _lanes_toggled(self, shown: bool) -> None:
        self.timeline.set_show_lanes(shown)

    def _fit(self) -> None:
        self.timeline.fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # A narrow window keeps every control: the title goes first.
        self.editor_title.setVisible(self.width() >= 980)

    def _refresh_actions(self, _index: int = 0) -> None:
        self.undo_button.setEnabled(self.undo_stack.canUndo())
        self.redo_button.setEnabled(self.undo_stack.canRedo())
        self.undo_button.setToolTip(self._text("실행 취소", "Undo") + (
            f": {self.undo_stack.undoText()}" if self.undo_stack.canUndo() else "") + " (Ctrl+Z)")
        self.redo_button.setToolTip(self._text("다시 실행", "Redo") + (
            f": {self.undo_stack.redoText()}" if self.undo_stack.canRedo() else "") + " (Ctrl+Shift+Z)")
        has = self.junction is not None
        self.tempo_button.setEnabled(has)
        self.copy_action.setEnabled(has)
        self.paste_action.setEnabled(self._copied is not None and has)
        self.paste_cues_action.setEnabled(self._copied is not None and has)
        stored = self._overrides.get(self._key())
        self.recommend_action.setEnabled(stored is not None and bool(stored.locked))
        self.reset_all_action.setEnabled(stored is not None)
        manual = stored is not None
        self.compare_button.setEnabled(manual and self.audition.available)
        self.mine_button.setEnabled(manual and self.audition.available)
        if not manual and self.compare_button.isChecked():
            self.compare_button.blockSignals(True)
            self.compare_button.setChecked(False)
            self.compare_button.blockSignals(False)
            self.mine_button.setChecked(True)

    def _retranslate(self) -> None:
        korean = self.korean
        self.setWindowTitle(self._text("AutoMix 편집기", "AutoMix editor"))
        self.simple_button.setText(self._text("기본", "Basic"))
        self.advanced_button.setText(self._text("고급", "Advanced"))
        self.simple_button.setToolTip(self._text("스타일과 길이만 · 고른 뒤 반복해서 들어보기 (Ctrl+1)",
                                                 "Style and length, then listen on a loop (Ctrl+1)"))
        self.advanced_button.setToolTip(self._text("큐·템포·대역 레인과 효과 세부값까지 (Ctrl+2)",
                                                   "Cues, tempo, band lanes and effect details too (Ctrl+2)"))
        self.previous_button.setToolTip(self._text("이전 전환 (Alt+←)", "Previous transition (Alt+Left)"))
        self.next_button.setToolTip(self._text("다음 전환 (Alt+→)", "Next transition (Alt+Right)"))
        self.more_button.setToolTip(self._text("복사·붙여넣기, 프리셋, 초기화, 배치", "Copy/paste, presets, reset, layout"))
        self.more_button.setAccessibleName(self._text("더 보기", "More"))
        self.undo_button.setAccessibleName(self._text("실행 취소", "Undo"))
        self.redo_button.setAccessibleName(self._text("다시 실행", "Redo"))
        self.snap_label.setText(self._text("스냅", "Snap"))
        self.tempo_button.setText(self._text("템포 맞춤", "Match tempo"))
        self.tempo_button.setToolTip(self._text("템포 맞춤 켜기 · 이미 켜져 있으면 설정 선택",
                                              "Enable tempo matching, or select its settings if already enabled"))
        for row, (ko, en) in enumerate((("끄기", "Off"), ("박자", "Beats"), ("마디", "Bars"))):
            self.snap_combo.setItemText(row, ko if korean else en)
        self.snap_combo.setToolTip(self._text(
            "끌 때 붙을 격자 (S로 켜기/끄기). Shift를 누르고 있는 동안은 반대로 동작합니다: 켜져 있으면 자유롭게, "
            "꺼져 있으면 박자에 붙습니다. 박자·마디는 분석을 믿을 수 있을 때만 고를 수 있습니다.",
            "What drags click onto (S turns it on/off). Holding Shift inverts it: free while snapping is on, "
            "onto beats while it is off. Beats and bars are offered only when the analysis can be trusted."))
        self.fit_button.setText(self._text("맞춤", "Fit"))
        self.fit_button.setToolTip(self._text("전환 전체가 보이게 (Ctrl+0)", "Show the whole transition (Ctrl+0)"))
        self.zoom_in_button.setToolTip(self._text("확대 (Ctrl+= · Ctrl+휠)", "Zoom in (Ctrl+= · Ctrl+wheel)"))
        self.zoom_out_button.setToolTip(self._text("축소 (Ctrl+-)", "Zoom out (Ctrl+-)"))
        self.zoom_in_button.setAccessibleName(self._text("확대", "Zoom in"))
        self.zoom_out_button.setAccessibleName(self._text("축소", "Zoom out"))
        self.lanes_button.setText(self._text("대역 레인", "Band lanes"))
        self.lanes_button.setToolTip(self._text("타임라인 아래 대역·효과 레인 보이기/숨기기",
                                                "Show or hide the band and effect lanes"))
        self.copy_action.setText(self._text("전환 설정 복사\tCtrl+C", "Copy transition settings\tCtrl+C"))
        self.paste_action.setText(self._text("붙여넣기 · 곡별 큐 제외\tCtrl+V", "Paste · without cues\tCtrl+V"))
        self.paste_cues_action.setText(self._text("큐 위치까지 붙여넣기\tCtrl+Shift+V",
                                                  "Paste including cues\tCtrl+Shift+V"))
        self.presets_menu.setTitle(self._text("프리셋", "Presets"))
        self.recommend_action.setText(self._text("자동 추천 다시 받기 (고정값 유지)\tCtrl+Backspace",
                                                 "Recommend again (keeping kept values)\tCtrl+Backspace"))
        self.reset_all_action.setText(self._text("전환 전체 초기화 (고정 해제 포함)", "Reset the whole transition (unkeeps too)"))
        self.layout_action.setText(self._text("작업 공간 기본 배치로", "Default workspace layout"))
        self.shortcuts_action.setText(self._text("단축키\tShift+F1", "Shortcuts\tShift+F1"))
        self.compare_label.setText(self._text("A/B 비교", "A/B"))
        self.mine_button.setText(self._text("내 편집", "My edit"))
        self.compare_button.setText(self._text("자동 결과", "Automatic"))
        self.mine_button.setToolTip(self._text("A: 지금 편집한 전환을 듣습니다 (B 키로 전환)",
                                               "A: hear the transition as you edited it (B switches)"))
        self.compare_button.setToolTip(self._text(
            "B: 같은 위치에서 분석이 정했을 자동 전환을 듣습니다. 편집하면 내 편집으로 돌아옵니다 (B)",
            "B: hear what analysis would do, from the same spot; any edit switches back to yours (B)"))
        self.properties_button.setText(self._text("속성", "Properties"))
        self.properties_button.setToolTip(self._text("속성 패널 보이기/숨기기", "Show or hide the properties panel"))
        self.help_button.setToolTip(self._text("도움말 (F1) · 단축키는 Shift+F1", "Help (F1) · shortcuts: Shift+F1"))
        self.stop_button.setToolTip(self._text("정지 · 구간 처음으로", "Stop · back to the window start"))
        self.loop_button.setText(self._text("반복", "Loop"))
        self.loop_button.setToolTip(self._text("전환 앞뒤 구간을 반복 재생 (L)", "Repeat the transition window (L)"))
        self.play_button.setToolTip(self._text("선택한 전환 구간 재생 / 일시정지 (Space)",
                                               "Play / pause the selected transition window (Space)"))
        self.retry_button.setText(self._text("다시 시도", "Retry"))
        self.volume_label.setText(self._text("음량", "Volume"))
        self.save_label.setText(self._text("편집은 바로 프로젝트에 저장되며 미리보기와 내보내기에 같은 설정이 쓰입니다",
                                           "Edits save to the project at once; Preview and Export use the same settings"))
        self.previous_button.setAccessibleName(self._text("이전 전환", "Previous transition"))
        self.next_button.setAccessibleName(self._text("다음 전환", "Next transition"))
        self.help_button.setAccessibleName(self._text("AutoMix 편집기 도움말", "AutoMix editor help"))
        self.volume_slider.setAccessibleName(self._text("미리듣기 음량", "Audition volume"))
        self.position_slider.setAccessibleName(self._text("미리듣기 위치", "Audition position"))
        self.volume_slider.setToolTip(self._text("미리듣기 볼륨", "Audition volume"))
        self.position_slider.setToolTip(self._text("구간 안에서 이동", "Seek within the window"))
        self._set_play_text()
        self.properties.retranslate(korean)
        self.timeline.korean = korean
        self._refresh_actions()
        self._set_audition_text()
        self._set_playhead(self._playhead)

    def _help_context(self) -> tuple[str, str]:
        """F1: the editor's help, on the topic of the part that has focus."""
        focus = QApplication.focusWidget()
        for widget, topic in ((self.properties_scroll, "automix_properties"),
                              (self.timeline_scroll, "automix_timeline"),
                              (self.play_button.parentWidget(), "automix_listen")):
            if focus is not None and (focus is widget or widget.isAncestorOf(focus)):
                return "automix_editor", topic
        return "automix_editor", "automix_editor"

    def _show_help(self) -> None:
        tab, topic = self._help_context()
        open_help(self, getattr(self.host, "translator", None), tab, topic)

    def _show_shortcuts(self) -> None:
        rows = (
            ("Space", "재생 / 일시정지", "Play / pause"), ("L", "구간 반복", "Loop the window"),
            ("B", "A/B 비교: 내 편집 ↔ 자동 결과", "A/B: my edit ↔ automatic"),
            ("Home", "구간 처음으로", "Window start"),
            ("Ctrl+Z · Ctrl+Shift+Z", "실행 취소 · 다시 실행 (끌기 한 번 = 한 단계)", "Undo · redo (one drag = one step)"),
            ("Alt+← / →", "이전 / 다음 전환", "Previous / next transition"),
            ("Ctrl+1 / Ctrl+2", "기본 / 고급", "Basic / advanced"),
            ("S · Shift+끌기", "스냅 켜기/끄기 · 누르는 동안 반대로", "Snap on/off · inverted while held"),
            ("Esc", "끌기 취소 (아무것도 바뀌지 않음)", "Cancel a drag (nothing changes)"),
            ("↑ / ↓", "타임라인에서 편집 대상 선택", "Select what to edit on the timeline"),
            ("← / →", "선택한 대상 한 박자 이동 (Shift: 0.01초)", "Nudge the selection a beat (Shift: 0.01 s)"),
            ("Ctrl+휠 · Ctrl+= / - · Ctrl+0", "확대 · 축소 · 맞춤", "Zoom · fit"),
            ("휠 · 가운데 버튼 끌기", "타임라인 좌우 이동", "Scroll the timeline"),
            ("Ctrl+C / Ctrl+V", "전환 설정 복사 / 붙여넣기 (큐 제외)", "Copy / paste settings (no cues)"),
            ("Ctrl+Shift+V", "큐 위치까지 붙여넣기", "Paste including cues"),
            ("Ctrl+Backspace", "자동으로 되돌리기 · 고정값이 있으면 다시 추천", "Back to automatic · recommend again if values are kept"),
            ("F1 · Shift+F1", "도움말 · 이 단축키 요약", "Help · this shortcut summary"),
        )
        text = "<table cellspacing='6'>" + "".join(
            f"<tr><td><b>{html.escape(keys)}</b></td><td>{html.escape(korean if self.korean else english)}</td></tr>"
            for keys, korean, english in rows) + "</table>" + self._text(
            "<p>숫자 입력칸에 커서가 있으면 입력이 먼저입니다.</p>",
            "<p>While a number field has the cursor, typing goes to it.</p>")
        box = QMessageBox(self)
        box.setWindowTitle(self._text("AutoMix 편집기 단축키", "AutoMix editor shortcuts"))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(text)
        box.open()

    # -- closing ------------------------------------------------------------------------------

    def _finish(self) -> None:
        """Commit a pending typed value, stop audio work, and bring an open Preview up to date once."""
        if self._finished:
            return
        self.timeline.cancel_drag()
        self.properties.flush()
        self._finished = True
        QSettings().setValue(_GEOMETRY_KEY, self.saveGeometry())
        self._save_workspace()
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        self.audition.shutdown()
        self._sync_activity(False)
        preview = getattr(self.host, "_inline_preview", None)
        if self._changed_keys and preview is not None and getattr(preview, "_transition_mode", "") == "automix":
            # Preview plays what it rendered before this editor opened: re-mix it once, not per edit.
            preview._automix_settings = automix_settings_for(self.host.project_settings)
            preview.remix_automix()

    def done(self, result: int) -> None:
        self._finish()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._finish()
        super().closeEvent(event)
