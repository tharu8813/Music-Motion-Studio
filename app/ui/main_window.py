"""Primary application window and Phase 1A workspace composition."""

from __future__ import annotations

import sys
import os
from collections.abc import Callable
from dataclasses import replace
import json
import logging
from pathlib import Path
import shutil  # re-exported: tests patch app.ui.main_window.shutil.disk_usage
import threading
from tempfile import TemporaryDirectory

from PySide6.QtCore import (QByteArray, QEvent, QEventLoop, QMimeData, QProcess, QSettings,
                            QStandardPaths, Qt, QTimer)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCloseEvent, QDragEnterEvent,
                           QDropEvent, QFontDatabase, QIcon, QImage, QImageReader,
                           QImageWriter, QKeySequence, QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTabBar,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.canvas.live_canvas import LiveCanvas
from app.automix.models import TrackAnalysis
from app.automix.settings import automix_settings_for
from app.automix.structure.models import TrackStructureAnalysis
from app.controllers.automix_analysis_controller import AutoMixAnalysisController
from app.controllers.autosave_controller import AutosaveController
from app.controllers.export_controller import ExportOrchestrator
from app.controllers.history_controller import HistoryController
from app.controllers.preview_controller import PreviewController
from app.controllers.project_controller import ProjectController
from app.controllers.source_layout_controller import SourceLayoutController
from app.animation.motion import MotionController
from app.animation.canvas_preview import CanvasAnimationPreviewController
from app.dialogs.export_progress_dialog import ExportProgressDialog
from app.dialogs.ffmpeg_install_progress_dialog import FFmpegInstallProgressDialog
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.dialogs.missing_media_dialog import MissingMediaDialog
from app.dialogs.new_project_dialog import NewProjectDialog
from app.dialogs.playlist_export_dialog import PlaylistExportDialog
from app.dialogs.preset_dialog import DesignPresetDialog
from app.dialogs.audio_metadata_dialog import AudioMetadataDialog
from app.dialogs.mp3_metadata_editor_dialog import Mp3MetadataEditorDialog
from app.dialogs.m3u_import_dialog import M3uImportDialog
from app.dialogs.project_settings_dialog import ProjectSettingsDialog
from app.dialogs.project_crash_report_dialog import ProjectCrashReportDialog
from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog
from app.dialogs.lyrics_search_dialog import LyricsSearchDialog
from app.dialogs.lyrics_compare_dialog import LyricsCompareDialog
from app.dialogs.settings_dialog import SettingsDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.dialogs.shortcuts_dialog import ShortcutsDialog
from app.dialogs.text_editor_dialog import TextEditorDialog
from app.dialogs.about_dialog import AboutDialog
from app.dialogs.help_dialog import HelpDialog
from app.dialogs.update_dialogs import UpdateAvailableDialog, UpdateDownloadDialog
from app.ffmpeg.install_worker import FFmpegCatalogWorker, FFmpegInstallWorker
from app.ffmpeg.managed_installer import (
    FFmpegInstallError,
    FFmpegReleaseOption,
    ManagedFFmpegInstallation,
    ManagedFFmpegInstaller,
)
from app.inspector.source_inspector import SourceInspector
from app.layers.layer_panel import LayerPanel
from app.models.source import Source, SourceType
from app.models.project import CanvasSettings, ProjectDocument, ProjectSettings
from app.services.project_service import ProjectError, ProjectService
from app.services.project_persistence_service import (
    default_project_path,
    is_legacy_project_path,
)
from app.services.project_save_worker import ProjectSaveWorker
from app.services.project_media_service import ProjectMediaService
from app.services.project_content_service import LYRICS_EXTENSIONS, ProjectContentService
from app.services.recent_projects_service import RecentProjectsService
from app.services.autosave_service import AutosaveService
from app.services.project_session import ProjectSession
from app.services.history_service import HistoryService
from app.services.lyrics_service import (
    LyricsError,
    LyricsService,
    find_sidecar_lyrics,
)
from app.services.theme_service import Theme, ThemeService
from app.services.source_store import SourceStore
from app.models.source_registry import source_registry
from app.services.playlist_service import (
    AUDIO_EXTENSIONS, AudioImportCandidate, AudioImportWorker, PlaylistService,
)
from app.utils.qt_worker_lifecycle import stop_qthread_now
from app.services.playlist_export_service import PlaylistExportError, PlaylistExportService
from app.services.m3u_playlist import (
    PLAYLIST_FILE_EXTENSIONS,
    M3uPlaylistError,
    read_m3u,
    write_m3u8,
)
from app.services.app_settings_service import (
    AppSettings,
    AppSettingsService,
    VIDEO_ENCODERS,
)
from app.services.export_storage_service import ExportStorageMonitor
from app.services.smooth_scroll_service import SmoothScrollService
from app.widgets.source_template_button import SourceTemplateButton
from app.services.update_service import (
    GitHubUpdateService,
    ReleaseInfo,
    automatic_setup_arguments,
    launch_setup,
    normalized_version,
)
from app.services.update_worker import UpdateCheckWorker, UpdateDownloadWorker
from app.presets.preset_service import PresetDefinition
from app.presets.user_preset_service import UserPresetError, UserPresetService
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_session import ExportSession
from app.preview.gpu_texture_surface import (
    GPU_TEXTURE_SURFACE_AVAILABLE, GpuTexturePreviewSurface,
)
from app.renderer.ffmpeg_renderer import (
    FFmpegNotFoundError,
    FFmpegRenderer,
    RenderError,
    RenderFrame,
    RenderSettings,
    RenderResult,
    VisualizerOverlay,
    VideoClipOverlay,
    WORK_MODE_AUTO,
)
from app.renderer.render_worker import RenderWorker
from app.timeline.timeline_panel import TimelinePanel
from app.utils.i18n import Language, Translator
from app.utils.image_loader import load_pixmap
from app.utils.logging_setup import log_directory, report_unexpected_error
from app.widgets.playlist_editor import PlaylistEditor
from app.widgets.content_library_panel import ContentLibraryPanel
from app.widgets.activity_progress import ActivityProgressWidget
from app.ui.studio_icons import menu_icon, source_icon
from app import __version__


LOGGER = logging.getLogger(__name__)

SOURCE_CLIPBOARD_MIME = "application/x-playlist-video-studio-sources+json"


class CanvasCenteredSplitter(QSplitter):
    """Keep edge panels fixed while the Canvas-side pane absorbs window resize."""

    def __init__(
        self, orientation: Qt.Orientation, center_index: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self._center_index = center_index
        self._edge_sizes: dict[int, int] = {}
        self._restoring_edges = False
        self.splitterMoved.connect(self._remember_edge_sizes)

    def lock_edge_sizes(self, sizes: dict[int, int] | None = None) -> None:
        """Use the current user-visible edge sizes for future window resizes."""
        if sizes is not None:
            self._edge_sizes = {
                int(index): max(0, int(size))
                for index, size in sizes.items()
                if int(index) != self._center_index
            }
            return
        current = self.sizes()
        self._edge_sizes = {
            index: size for index, size in enumerate(current)
            if index != self._center_index
        }

    def setSizes(self, sizes: list[int]) -> None:  # noqa: N802 - Qt API name
        super().setSizes(sizes)
        if not self._restoring_edges:
            self.lock_edge_sizes()

    def _remember_edge_sizes(self, _position: int, _index: int) -> None:
        if not self._restoring_edges:
            self.lock_edge_sizes()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        before_resize = self.sizes()
        old_size = event.oldSize()
        old_extent = (
            old_size.width()
            if self.orientation() == Qt.Orientation.Horizontal
            else old_size.height()
        )
        remembered = dict(self._edge_sizes)
        if not remembered and old_extent > 0 and sum(before_resize) > 0:
            remembered = {
                index: size for index, size in enumerate(before_resize)
                if index != self._center_index
            }
        super().resizeEvent(event)
        if not remembered or self.count() <= self._center_index:
            return
        current = self.sizes()
        if len(current) != self.count():
            return
        total = sum(current)
        target = list(current)
        fixed_total = 0
        for index, size in remembered.items():
            widget = self.widget(index)
            if widget is None:
                continue
            if widget.isHidden() or (
                self.orientation() == Qt.Orientation.Horizontal
                and widget.maximumWidth() == 0
            ):
                preserved = 0
            else:
                preserved = max(0, size)
            target[index] = preserved
            fixed_total += preserved
        target[self._center_index] = max(0, total - fixed_total)
        self._restoring_edges = True
        try:
            super().setSizes(target)
        finally:
            self._restoring_edges = False
        # A temporarily small window may force Qt to compress edge panels.
        # Keep the user's remembered sizes so expanding the window restores
        # them instead of treating the temporary compression as a new choice.
        self.lock_edge_sizes(remembered)


class MainWindow(QMainWindow):
    """The runnable Phase 1A desktop workspace."""

    _DEFAULT_LEFT_PANEL_WIDTH = 300
    _DEFAULT_RIGHT_PANEL_WIDTH = 380  # every property tab fits without scroll arrows
    _DEFAULT_BOTTOM_PANEL_HEIGHT = 240

    @property
    def project_settings(self) -> ProjectSettings:
        return self._project_settings

    @project_settings.setter
    def project_settings(self, settings: ProjectSettings) -> None:
        # Every path that changes it (new, open, Project settings, undo) goes
        # through here, so the Inspector's mix-handover rows always match.
        self._project_settings = settings
        inspector = getattr(self, "inspector", None)
        if inspector is not None:
            inspector.set_mix_transitions_active(settings.transition_mode != "none")
        playlist_editor = getattr(self, "playlist_editor", None)
        if playlist_editor is not None:
            playlist_editor.set_transitions(settings.transition_mode == "automix", settings.automix_overrides)
        automix_editor_action = getattr(self, "automix_editor_action", None)
        if automix_editor_action is not None:
            automix_editor_action.setEnabled(settings.transition_mode == "automix")

    def __init__(self) -> None:
        super().__init__()
        # Signal lambdas that capture self keep this wrapper alive forever, so without
        # this PySide destroyed the whole window tree during interpreter shutdown, which
        # could crash at exit (0xC0000409). Deleted on close, it goes while Qt still runs.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        from app.ui.design_system import apply_studio_style
        apply_studio_style(QApplication.instance())
        self.store = SourceStore(self)
        self.playlist_service = PlaylistService(self)
        self.project_content_service = ProjectContentService(self)
        self.project_settings = ProjectSettings()
        self.playlist_export_service = PlaylistExportService()
        self.settings_service = AppSettingsService(self)
        # Renderer selection is process-scoped. Saving a different backend does
        # not mutate an already-created OpenGL/widget hierarchy mid-session.
        self._preview_backend_for_session = (
            self.settings_service.current.preview_backend
        )
        self.smooth_scroll = SmoothScrollService(
            self.settings_service.current.smooth_scrolling,
            self.settings_service.current.smooth_scroll_duration_ms,
            self,
        )
        self.smooth_scroll.install()
        self.settings_service.changed.connect(
            lambda settings: self.smooth_scroll.configure(
                settings.smooth_scrolling, settings.smooth_scroll_duration_ms
            )
        )
        self.settings_service.changed.connect(
            self._sync_export_notification_tray
        )
        self.recent_projects = RecentProjectsService(self)
        self.translator = Translator(self)
        self.theme_service = ThemeService(self)
        self.motion = MotionController(self)
        self.animation_preview_controller = CanvasAnimationPreviewController(self)
        self.animation_preview_controller.finished.connect(
            self._finish_canvas_animation_preview
        )
        self._animation_preview_active = False
        self._animation_preview_cancel_armed = False
        self.store.source_changed.connect(self._cancel_animation_preview)
        self.store.selection_set_changed.connect(self._cancel_animation_preview)
        self._inline_preview: ExportPreviewDialog | None = None
        self._inline_preview_controls: QWidget | None = None
        self._inline_preview_track_panel: QWidget | None = None
        self._preview_ui_lock_state: dict[str, object] | None = None
        self._bottom_tab_change_guard = False
        self._last_edit_bottom_tab = 0
        self.project_session = ProjectSession()
        self.history = HistoryService(self)
        self.history_controller = HistoryController(self, self.project_session)
        self.project_controller = ProjectController(self, self.project_session)
        self.autosave_controller = AutosaveController(self, self.project_session)
        self.export_orchestrator = ExportOrchestrator(self)
        self.preview_controller = PreviewController(self)
        self.automix_analyses: dict[str, TrackAnalysis] = {}
        self.automix_structures: dict[str, TrackStructureAnalysis] = {}
        self.automix_analysis_controller = AutoMixAnalysisController(self)
        self.automix_analysis_controller.analyses_updated.connect(
            self._automix_analyses_received
        )
        self.automix_analysis_controller.structures_updated.connect(
            self._automix_structures_received
        )
        self.automix_analysis_controller.progress_changed.connect(
            lambda _stages: self._update_automix_analysis_activity()
        )
        self.automix_analysis_controller.running_changed.connect(self._automix_analysis_running_changed)
        # "Analyze this track" in Track information: its own pass, whatever the
        # transition mode, so it never cancels (or waits for) the background one.
        self.track_analysis_controller = AutoMixAnalysisController(self)
        self.track_analysis_controller.analyses_updated.connect(self._automix_analyses_received)
        self.track_analysis_controller.structures_updated.connect(self._automix_structures_received)
        self.track_analysis_controller.track_step_changed.connect(self._track_analysis_step)
        self.track_analysis_controller.analyses_failed.connect(self._track_analysis_failed)
        self.track_analysis_controller.running_changed.connect(self._track_analysis_running_changed)
        self._analyzing_track_id = ""
        # A pass over cached files ends within a moment of every playlist edit;
        # only a pass still running after this delay is worth a progress bar.
        self._automix_activity_timer = QTimer(self)
        self._automix_activity_timer.setSingleShot(True)
        self._automix_activity_timer.setInterval(600)
        self._automix_activity_timer.timeout.connect(lambda: self._update_automix_analysis_activity(show=True))
        self._automix_analysis_timer = QTimer(self)
        self._automix_analysis_timer.setSingleShot(True)
        self._automix_analysis_timer.setInterval(500)
        self._automix_analysis_timer.timeout.connect(self._maybe_start_automix_analysis)
        self.playlist_service.playlist_changed.connect(
            self._automix_analysis_timer.start
        )
        recovery_directory = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        self.autosave = AutosaveService(Path(recovery_directory or Path.cwd() / ".app-data"))
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(300)
        self._history_timer.timeout.connect(self._commit_history)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(30_000)
        self._autosave_timer.timeout.connect(self._autosave_project)
        self._autosave_debounce_timer = QTimer(self)
        self._autosave_debounce_timer.setSingleShot(True)
        self._autosave_debounce_timer.setInterval(4_000)
        self._autosave_debounce_timer.timeout.connect(self._autosave_project)
        self.current_project_path: Path | None = None
        self._legacy_project_path: Path | None = None
        self.current_theme = self.theme_service.preference.value
        # Version-2 projects contain legacy UI preference metadata. Keep it for
        # lossless round-trips, but do not let a project overwrite app-wide UI
        # preferences or participate in project dirty/history state.
        self._project_theme_metadata = self.current_theme
        self._project_language_metadata = self.translator.language.value
        self._sidebar_open_width = self._DEFAULT_LEFT_PANEL_WIDTH
        self._inspector_open_width = self._DEFAULT_RIGHT_PANEL_WIDTH
        self._bottom_open_height = self._DEFAULT_BOTTOM_PANEL_HEIGHT
        self._sidebar_transition = False
        self._panel_transition_serial = {"left": 0, "right": 0, "bottom": 0}
        self._render_worker: RenderWorker | None = None
        self._active_export_session: ExportSession | None = None
        self._export_dialog: ExportProgressDialog | None = None
        self._export_storage_monitor: ExportStorageMonitor | None = None
        self._export_ui_lock_state: tuple[bool, bool, bool, bool, bool] | None = None
        self._export_preparation_cancel: threading.Event | None = None
        self._close_after_export_cancel = False
        self._export_restore_pending = False
        self._canvas_fit_pending = False
        self._canvas_fit_timer = QTimer(self)
        self._canvas_fit_timer.setSingleShot(True)
        self._canvas_fit_timer.timeout.connect(self._apply_scheduled_canvas_fit)
        self._notification_tray: QSystemTrayIcon | None = None
        self._export_notified_steps: set[str] = set()
        self._active_export_output_path: Path | None = None
        self._pending_export_result: RenderResult | None = None
        self._clipboard_paste_serial = 0
        self._ffmpeg_install_worker: FFmpegInstallWorker | None = None
        self._ffmpeg_catalog_worker: FFmpegCatalogWorker | None = None
        self._ffmpeg_catalog_cache: tuple[
            list[FFmpegReleaseOption], ManagedFFmpegInstallation | None,
        ] | None = None
        self._close_after_ffmpeg_catalog_cancel = False
        self._ffmpeg_install_dialog: FFmpegInstallProgressDialog | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._ffmpeg_notice: QMessageBox | None = None
        self._update_service = GitHubUpdateService()
        self._update_check_worker: UpdateCheckWorker | None = None
        self._update_check_manual = False
        self._update_check_reinstall = False
        self._reset_after_close = False
        self._close_after_update_check = False
        self._update_download_worker: UpdateDownloadWorker | None = None
        self._update_download_dialog: UpdateDownloadDialog | None = None
        self._downloaded_update_path: Path | None = None
        self._update_install_authorized = False
        self._pending_update_installer: Path | None = None
        self._source_buttons: dict[SourceType, QPushButton] = {}
        self._source_card_groups: dict[SourceType, QWidget] = {}
        self._source_variant_containers: dict[SourceType, QWidget] = {}
        self._source_variant_toggles: dict[SourceType, QToolButton] = {}
        self.setAcceptDrops(True)
        self.resize(1560, 920)
        self.setMinimumSize(1100, 680)
        self._build_toolbar()
        self._build_workspace()
        self.source_layout_controller = SourceLayoutController(self.store, self.canvas.source_content_bounds)
        self._build_canvas_edit_actions()
        self._build_menu_bar()
        self.activity_progress = ActivityProgressWidget(
            self.translator.is_korean, self,
        )
        self.statusBar().addPermanentWidget(self.activity_progress)
        self._build_zoom_controls()
        self._apply_style()
        self._add_welcome_sources()
        self.canvas.scene_model.set_placeholder_language(
            self.translator.is_korean
        )
        self.translator.language_changed.connect(self.retranslate)
        self.translator.language_changed.connect(
            lambda: self.activity_progress.set_korean(
                self.translator.is_korean
            )
        )
        self.translator.language_changed.connect(lambda: self._update_automix_analysis_activity())
        self.translator.language_changed.connect(self._sync_language_actions)
        self.translator.packs_changed.connect(self._rebuild_language_menu)
        self.theme_service.theme_changed.connect(self._on_theme_changed)
        self.retranslate()
        self._connect_history()
        self._autosave_timer.start()
        self._sync_export_notification_tray(self.settings_service.current)
        QTimer.singleShot(0, self._finish_initialization)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main toolbar")
        self.toolbar = toolbar
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)
        self.new_action = QAction(self)
        self.new_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
        self.new_action.triggered.connect(self._show_project_start_dialog)
        toolbar.addAction(self.new_action)
        self.open_action = QAction(self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        )
        self.open_action.triggered.connect(self._open_project)
        toolbar.addAction(self.open_action)
        self.save_action = QAction(self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self.save_action.triggered.connect(self._save_project)
        toolbar.addAction(self.save_action)
        self.save_as_action = QAction(self)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.triggered.connect(lambda: self._save_project(True))
        self.undo_action = QAction(self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack)
        )
        self.undo_action.triggered.connect(self._undo)
        self.undo_action.setEnabled(False)
        toolbar.addAction(self.undo_action)
        self.redo_action = QAction(self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward)
        )
        self.redo_action.triggered.connect(self._redo)
        self.redo_action.setEnabled(False)
        toolbar.addAction(self.redo_action)
        self.center_horizontal_action = QAction(
            self._alignment_toolbar_icon(horizontal=True), "", self
        )
        self.center_horizontal_action.setEnabled(False)
        self.center_horizontal_action.triggered.connect(
            lambda: self._center_selected_sources(horizontal=True)
        )
        self.center_vertical_action = QAction(
            self._alignment_toolbar_icon(horizontal=False), "", self
        )
        self.center_vertical_action.setEnabled(False)
        self.center_vertical_action.triggered.connect(
            lambda: self._center_selected_sources(horizontal=False)
        )
        self.presets_action = QAction(self)
        self.presets_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView)
        )
        self.presets_action.triggered.connect(self._choose_preset)
        toolbar.addAction(self.presets_action)
        toolbar.addSeparator()
        self.fit_action = QAction(self)
        self.fit_action.setShortcut("F")
        self.fit_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DesktopIcon)
        )
        self.fit_action.triggered.connect(self.canvas_fit)
        toolbar.addAction(self.fit_action)
        self.grid_action = QAction(self)
        self.grid_action.setCheckable(True)
        self.grid_action.setChecked(True)
        self.grid_action.toggled.connect(self._toggle_grid)
        toolbar.addAction(self.grid_action)
        # Kept as a non-toolbar action for legacy translated text; snapping is now
        # temporarily disabled with Alt instead of a persistent toolbar toggle.
        self.snap_action = QAction(self)
        self.delete_action = QAction(self)
        self.delete_action.setShortcuts([
            QKeySequence(QKeySequence.StandardKey.Delete),
            QKeySequence(Qt.Key.Key_Backspace),
        ])
        self.delete_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon)
        )
        self.delete_action.triggered.connect(self._delete_contextual_selection)
        toolbar.addAction(self.delete_action)
        toolbar.addSeparator()
        language_menu = QMenu(self)
        self.language_menu = language_menu
        self.language_group = QActionGroup(self)
        self.language_group.setExclusive(True)
        self.language_actions: dict[str, QAction] = {}
        self._rebuild_language_menu()
        self.language_button = QToolButton()
        self.language_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.language_button.setMenu(language_menu)
        self.panels_action = QAction(self)
        self.panels_action.setCheckable(True)
        self.panels_action.setChecked(True)
        self.panels_action.setShortcut(QKeySequence("Ctrl+Alt+L"))
        self.panels_action.toggled.connect(self._set_sidebar_visible)
        self.panels_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMenuButton)
        )
        self.inspector_panel_action = QAction(self)
        self.inspector_panel_action.setCheckable(True)
        self.inspector_panel_action.setChecked(True)
        self.inspector_panel_action.setShortcut(QKeySequence("Ctrl+Alt+R"))
        self.inspector_panel_action.toggled.connect(
            self._set_inspector_panel_visible
        )
        self.bottom_panel_action = QAction(self)
        self.bottom_panel_action.setCheckable(True)
        self.bottom_panel_action.setChecked(True)
        self.bottom_panel_action.setShortcut(QKeySequence("Ctrl+Alt+B"))
        self.bottom_panel_action.toggled.connect(self._set_bottom_panel_visible)
        self.settings_action = QAction(self)
        self.settings_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        )
        self.settings_action.triggered.connect(self._show_settings)

        toolbar.addWidget(self._toolbar_spacer())
        toolbar.addAction(self.panels_action)
        toolbar.addAction(self.settings_action)
        toolbar.addWidget(self.language_button)
        toolbar.addSeparator()
        self.playlist_files_action = QAction(self)
        self.playlist_files_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogListView)
        )
        self.playlist_files_action.triggered.connect(self._export_playlist_files)
        toolbar.addAction(self.playlist_files_action)
        self.export_action = QAction(self)
        self.export_action.setEnabled(True)
        self.export_action.setShortcut(QKeySequence("Ctrl+E"))
        self.export_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        )
        self.export_action.triggered.connect(self._export_video)
        toolbar.addAction(self.export_action)
        self.preview_action = QAction(self)
        self.preview_action.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        # Ctrl+Alt+3 continues the Playlist (1) / Timeline (2) bottom-tab shortcuts.
        self.preview_action.setShortcut(QKeySequence("Ctrl+Alt+3"))
        self.preview_action.triggered.connect(lambda: self._show_bottom_panel(2))
        self.export_button = toolbar.widgetForAction(self.export_action)
        if self.export_button is not None:
            self.export_button.setObjectName("exportButton")
            self.export_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        # File/edit/view controls live in the menu bar.  Keep only the primary
        # everyday controls in the toolbar so the top area stays balanced.
        toolbar.clear()
        toolbar.addAction(self.new_action)
        toolbar.addAction(self.open_action)
        toolbar.addAction(self.save_action)
        toolbar.addSeparator()
        toolbar.addAction(self.undo_action)
        toolbar.addAction(self.redo_action)
        toolbar.addSeparator()
        toolbar.addAction(self.center_horizontal_action)
        toolbar.addAction(self.center_vertical_action)
        toolbar.addWidget(self._toolbar_spacer())
        self.project_status_label = QLabel()
        self.project_status_label.setObjectName("projectStatusChip")
        self.project_status_label.setToolTip(
            "현재 프로젝트 이름과 저장 상태" if self.translator.is_korean
            else "Current project name and save state"
        )
        toolbar.addWidget(self.project_status_label)
        toolbar.addAction(self.export_action)
        self.export_button = toolbar.widgetForAction(self.export_action)
        if self.export_button is not None:
            self.export_button.setObjectName("exportButton")
            self.export_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        # Compact secondary actions leave Export visible on laptops.
        for action in (
            self.new_action, self.open_action, self.undo_action, self.redo_action,
            self.center_horizontal_action, self.center_vertical_action,
        ):
            button = toolbar.widgetForAction(action)
            if button is not None:
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.store.selection_set_changed.connect(
            lambda _selected_ids, _active: self._update_alignment_toolbar_actions()
        )
        self.store.source_changed.connect(
            lambda _source: self._update_alignment_toolbar_actions()
        )
        self._update_alignment_toolbar_actions()

    def _rebuild_language_menu(self) -> None:
        """Rebuild the language picker after external packs change."""
        for action in tuple(self.language_group.actions()):
            self.language_group.removeAction(action)
            action.deleteLater()
        self.language_menu.clear()
        self.language_actions.clear()

        for option in self.translator.available_languages():
            action = QAction(option.display_name, self)
            action.setCheckable(True)
            action.setChecked(option.locale == self.translator.locale)
            action.triggered.connect(
                lambda checked=False, locale=option.locale: (
                    self.translator.set_language(locale) if checked else None
                )
            )
            self.language_group.addAction(action)
            self.language_menu.addAction(action)
            self.language_actions[option.locale] = action

    def _sync_language_actions(self) -> None:
        """Keep the toolbar language selection in sync with Settings."""
        for locale, action in self.language_actions.items():
            action.setChecked(locale == self.translator.locale)

    @staticmethod
    def _toolbar_spacer() -> QWidget:
        """Create the expanding spacer that keeps output controls right-aligned."""
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return spacer

    @staticmethod
    def _alignment_toolbar_icon(horizontal: bool) -> QIcon:
        """Draw a compact, theme-neutral artboard-center alignment icon."""
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#77879E")
        guide_pen = QPen(color, 1.0, Qt.PenStyle.DashLine)
        shape_pen = QPen(color, 1.8)
        if horizontal:
            painter.setPen(guide_pen)
            painter.drawLine(12, 2, 12, 22)
            painter.setPen(shape_pen)
            painter.drawRect(5, 5, 14, 5)
            painter.drawRect(7, 14, 10, 5)
        else:
            painter.setPen(guide_pen)
            painter.drawLine(2, 12, 22, 12)
            painter.setPen(shape_pen)
            painter.drawRect(5, 5, 5, 14)
            painter.drawRect(14, 7, 5, 10)
        painter.end()
        return QIcon(pixmap)

    def _update_alignment_toolbar_actions(self) -> None:
        """Enable artboard-center controls only for an editable selection."""
        enabled = any(
            source is not None and not source.locked
            for source in (self.store.get(source_id) for source_id in self.store.selected_ids)
        )
        self.center_horizontal_action.setEnabled(enabled)
        self.center_vertical_action.setEnabled(enabled)

    def _build_canvas_edit_actions(self) -> None:
        """Register shortcuts shared by the Canvas and its Layer panel.

        The Layer tree owns keyboard focus after a layer is clicked.  Canvas-child
        shortcuts therefore stopped working even though the selected graphics item
        was synchronized correctly.  Window shortcuts let Qt see the key sequence
        first, while the dispatch guard keeps them out of Inspector, Playlist, and
        other text-editing controls.
        """
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._canvas_shortcut_actions: list[QAction] = []

        def register(shortcut: str, callback: object) -> None:
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)

            def dispatch() -> None:
                if self._canvas_shortcut_scope_active():
                    callback()  # type: ignore[operator]

            action.triggered.connect(dispatch)
            self.addAction(action)
            self._canvas_shortcut_actions.append(action)

        # Ctrl+D and Ctrl+A are already window actions in the Edit menu.  Do not
        # register a second QAction for either sequence, because duplicate window
        # shortcuts become ambiguous and Qt suppresses both activations.
        register("Esc", self._clear_canvas_selection)
        # Arrow-key nudging is handled inside LiveCanvas.keyPressEvent so it only
        # fires while the Canvas view itself has focus (Arrow = 1px,
        # Shift+Arrow = 10px), keeping arrow navigation intact in the Layer tree
        # and every spin box.
        register("Ctrl+Shift+H", lambda: self._center_selected_sources(horizontal=True))
        register("Ctrl+Shift+V", lambda: self._center_selected_sources(horizontal=False))
        register("Ctrl+]", lambda: self._move_selected_to_edge(front=True))
        register("Ctrl+[", lambda: self._move_selected_to_edge(front=False))
        register("Ctrl+G", self._group_selected_sources)
        register("Ctrl+Shift+G", self._ungroup_selected_sources)
        register("Ctrl+L", self._toggle_selected_lock)
        register("Ctrl+0", self.canvas_fit)
        register("Ctrl+=", lambda: self._adjust_canvas_zoom(1.15))
        register("Ctrl++", lambda: self._adjust_canvas_zoom(1.15))
        register("Ctrl+-", lambda: self._adjust_canvas_zoom(1.0 / 1.15))
        register("Home", self.canvas_fit)
        # F2 rename is handled in LiveCanvas.keyPressEvent so the Layer tree keeps
        # its own built-in F2 item rename when that panel holds focus.
        QApplication.instance().focusChanged.connect(self._sync_canvas_shortcut_actions)
        self._sync_canvas_shortcut_actions(None, QApplication.focusWidget())

    def _sync_canvas_shortcut_actions_to_focus(self) -> None:
        self._sync_canvas_shortcut_actions(None, QApplication.focusWidget())

    def _sync_canvas_shortcut_actions(
        self, previous: QWidget | None, current: QWidget | None,
    ) -> None:
        """Enable global key sequences only while Canvas editing owns focus."""
        del previous
        enabled = self._canvas_shortcut_scope_active(current)
        for action in self._canvas_shortcut_actions:
            action.setEnabled(enabled)
        if hasattr(self, "duplicate_action"):
            editing_text = bool(
                current is not None
                and (current.inherits("QLineEdit") or current.inherits("QTextEdit")
                     or current.inherits("QPlainTextEdit")
                     or current.inherits("QAbstractSpinBox")
                     or current.inherits("QComboBox"))
            )
            self.duplicate_action.setEnabled(not editing_text)
            self.select_all_action.setEnabled(not editing_text)
            self.delete_action.setEnabled(not editing_text)
            canvas_scope = self._canvas_shortcut_scope_active(current)
            self.cut_action.setEnabled(canvas_scope)
            self.copy_action.setEnabled(canvas_scope)
            self.paste_action.setEnabled(canvas_scope)

    def _canvas_shortcut_scope_active(self, focus: QWidget | None = None) -> bool:
        """Return whether keyboard focus belongs to Canvas editing UI."""
        focus = focus or QApplication.focusWidget()
        if focus is None:
            return False
        return (
            focus is self.canvas
            or self.canvas.isAncestorOf(focus)
            or focus is self.layer_panel
            or self.layer_panel.isAncestorOf(focus)
        )

    def _selected_editable_sources(self) -> list[Source]:
        """Return selected, editable Canvas source models in drawing order."""
        selected_ids = {
            item.source.id for item in self.canvas.scene_model.selectedItems()
            if hasattr(item, "source") and not item.source.locked
        }
        return [source for source in self.store.sources() if source.id in selected_ids]

    def _duplicate_selected_sources(self) -> None:
        """Duplicate every selected source with a visible offset."""
        sources = self._selected_editable_sources()
        if not sources:
            return
        highest_z = max((source.z_index for source in self.store.sources()), default=0)
        for index, source in enumerate(sources, start=1):
            payload = source.to_dict()
            payload.pop("id", None)
            copied = Source.from_dict(payload)
            copied.name = f"{source.name} copy"
            copied.x += 24
            copied.y += 24
            copied.z_index = highest_z + index
            self.store.add(copied)
        self.statusBar().showMessage(
            "선택한 요소를 복제했습니다." if self.translator.is_korean
            else "Duplicated selected sources.", 2500
        )

    def _copy_selected_sources(self) -> bool:
        """Copy selected Canvas sources to a versioned application clipboard payload."""
        sources = self._selected_editable_sources()
        if not sources:
            return False
        payload_sources: list[dict[str, object]] = []
        for source in sources:
            payload = source.to_dict()
            # Clipboard groups are intentionally detached. Group IDs belong to
            # one project and must never point at an unrelated group after a
            # cross-project paste.
            payload["group_id"] = None
            payload_sources.append(payload)
        payload = {
            "schema": "playlist-video-studio/sources",
            "version": 1,
            "sources": payload_sources,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        mime_data = QMimeData()
        mime_data.setData(
            SOURCE_CLIPBOARD_MIME, QByteArray(encoded.encode("utf-8"))
        )
        mime_data.setText(encoded)
        QApplication.clipboard().setMimeData(mime_data)
        self._clipboard_paste_serial = 0
        self.statusBar().showMessage(
            f"요소 {len(sources)}개를 복사했습니다."
            if self.translator.is_korean
            else f"Copied {len(sources)} source(s).",
            2500,
        )
        return True

    def _cut_selected_sources(self) -> None:
        """Copy and then remove the selected editable Canvas sources."""
        sources = self._selected_editable_sources()
        if not sources or not self._copy_selected_sources():
            return
        for source in sources:
            self.store.remove(source.id)
        self.statusBar().showMessage(
            f"요소 {len(sources)}개를 잘라냈습니다."
            if self.translator.is_korean
            else f"Cut {len(sources)} source(s).",
            2500,
        )

    @staticmethod
    def _clipboard_source_payload() -> list[dict[str, object]] | None:
        """Read and validate the lightweight outer clipboard contract."""
        mime_data = QApplication.clipboard().mimeData()
        if mime_data is None:
            return None
        raw = ""
        if mime_data.hasFormat(SOURCE_CLIPBOARD_MIME):
            try:
                raw = bytes(mime_data.data(SOURCE_CLIPBOARD_MIME)).decode(
                    "utf-8", errors="strict"
                )
            except UnicodeError:
                return None
        elif mime_data.hasText():
            raw = mime_data.text()
        if not raw or len(raw.encode("utf-8")) > 5_000_000:
            return None
        try:
            payload = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "playlist-video-studio/sources"
            or payload.get("version") != 1
            or not isinstance(payload.get("sources"), list)
            or len(payload["sources"]) > 1_000
            or not all(isinstance(entry, dict) for entry in payload["sources"])
        ):
            return None
        return [entry.copy() for entry in payload["sources"]]

    def _paste_sources(self) -> None:
        """Paste copied sources above existing layers with fresh IDs and an offset."""
        payloads = self._clipboard_source_payload()
        if not payloads:
            self.statusBar().showMessage(
                "붙여넣을 요소가 없습니다."
                if self.translator.is_korean
                else "There are no copied sources to paste.",
                2500,
            )
            return
        self._clipboard_paste_serial += 1
        offset = 24.0 * self._clipboard_paste_serial
        highest_z = max((source.z_index for source in self.store.sources()), default=0.0)
        pasted: list[Source] = []
        try:
            for index, payload in enumerate(payloads, start=1):
                payload.pop("id", None)
                payload["group_id"] = None
                copied = Source.from_dict(payload)
                copied.name = (
                    f"{copied.name} 복사본"
                    if self.translator.is_korean
                    else f"{copied.name} copy"
                )
                copied.x += offset
                copied.y += offset
                copied.z_index = highest_z + index
                pasted.append(copied)
        except (KeyError, TypeError, ValueError) as error:
            LOGGER.warning("Rejected invalid source clipboard payload: %s", error)
            self.statusBar().showMessage(
                "복사된 요소 데이터가 올바르지 않습니다."
                if self.translator.is_korean
                else "The copied source data is invalid.",
                3500,
            )
            return
        for source in pasted:
            self.store.add(source)
        pasted_ids = [source.id for source in pasted]
        self.store.select_many(pasted_ids, pasted_ids[-1] if pasted_ids else None)
        self.statusBar().showMessage(
            f"요소 {len(pasted)}개를 붙여넣었습니다."
            if self.translator.is_korean
            else f"Pasted {len(pasted)} source(s).",
            2500,
        )

    def _playlist_focus_active(self) -> bool:
        focus = QApplication.focusWidget()
        return bool(
            focus is not None
            and (focus is self.playlist_editor or self.playlist_editor.isAncestorOf(focus))
        )

    def _duplicate_contextual_selection(self) -> None:
        """Apply Ctrl+D to tracks when Playlist owns focus, otherwise to Canvas."""
        if self._playlist_focus_active():
            self.playlist_editor.duplicate_selected()
        else:
            self._duplicate_selected_sources()

    def _delete_contextual_selection(self) -> None:
        """Apply Delete to the currently focused editing surface."""
        if self._playlist_focus_active():
            self.playlist_editor.remove_selected()
        else:
            self._delete_selected_sources()

    def _select_all_contextual(self) -> None:
        """Apply Ctrl+A to Playlist rows or Canvas sources by focus context."""
        if self._playlist_focus_active():
            self.playlist_editor.list_widget.selectAll()
        else:
            self._select_all_canvas_sources()

    def _select_all_canvas_sources(self) -> None:
        """Select all visible Canvas sources while avoiding selection-signal churn."""
        items = [item for item in self.canvas.scene_model.items()
                 if hasattr(item, "source") and item.isVisible()]
        source_ids = [item.source.id for item in items]
        self.store.select_many(source_ids, source_ids[-1] if source_ids else None)

    def _clear_canvas_selection(self) -> None:
        """Clear selection from Canvas, Layer panel, and Inspector together."""
        self.store.select(None)

    def _adjust_canvas_zoom(self, factor: float) -> None:
        """Apply a bounded keyboard zoom around the Canvas view centre."""
        self.canvas.zoom_by(factor)

    def _build_zoom_controls(self) -> None:
        """Add a compact zoom readout and stepper to the status bar."""
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 8, 0)
        row.setSpacing(1)
        self.zoom_out_button = QToolButton()
        self.zoom_out_button.setText("−")
        self.zoom_out_button.setAutoRaise(True)
        self.zoom_out_button.clicked.connect(
            lambda: self._adjust_canvas_zoom(1.0 / 1.15)
        )
        self.zoom_reset_button = QToolButton()
        self.zoom_reset_button.setAutoRaise(True)
        self.zoom_reset_button.setMinimumWidth(52)
        self.zoom_reset_button.clicked.connect(self._toggle_canvas_zoom_100)
        self.zoom_in_button = QToolButton()
        self.zoom_in_button.setText("+")
        self.zoom_in_button.setAutoRaise(True)
        self.zoom_in_button.clicked.connect(lambda: self._adjust_canvas_zoom(1.15))
        for widget in (self.zoom_out_button, self.zoom_reset_button, self.zoom_in_button):
            row.addWidget(widget)
        self.statusBar().addPermanentWidget(container)
        self.canvas.zoom_changed.connect(self._update_zoom_label)
        self._update_zoom_label(self.canvas.transform().m11())

    def _update_zoom_label(self, zoom: float) -> None:
        self.zoom_reset_button.setText(f"{round(zoom * 100)}%")

    def _toggle_canvas_zoom_100(self) -> None:
        """Switch between 1:1 and fit-to-view from the status bar readout."""
        if abs(self.canvas.transform().m11() - 1.0) < 0.01:
            self.canvas.fit_artboard()
        else:
            self.canvas.set_zoom(1.0)

    def _nudge_selected_sources(self, x_delta: float, y_delta: float) -> None:
        """Move selected sources by an exact keyboard increment."""
        self.source_layout_controller.nudge(self._selected_editable_sources(), x_delta, y_delta)

    def _edit_canvas_source(self, source_id: str) -> None:
        """Open the in-place editor for a double-clicked Canvas source."""
        source = self.store.get(source_id)
        if source is None:
            return
        self.store.select(source_id)
        if source.source_type in {SourceType.TEXT, SourceType.LYRICS}:
            dialog = TextEditorDialog(source.text, self.translator, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.store.update(source_id, text=dialog.text())

    def _rename_selected_source(self) -> None:
        """Jump keyboard focus to the Inspector name field for a quick rename."""
        if self.store.selected is None:
            return
        if not self.inspector_panel_action.isChecked():
            self.inspector_panel_action.setChecked(True)
        self.inspector.name_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.inspector.name_edit.selectAll()

    def _center_selected_sources(self, horizontal: bool) -> None:
        """Center the collective selection on the artboard along one axis."""
        self.source_layout_controller.center(
            self._selected_editable_sources(), self.canvas.scene_model.artboard_rect, horizontal,
        )

    def _move_selected_to_edge(self, front: bool) -> None:
        """Bring selected sources in front of, or behind, every other source."""
        sources = self._selected_editable_sources()
        if not sources:
            return
        self.store.move_layers_to_edge((source.id for source in sources), front)

    def _group_selected_sources(self) -> None:
        """Create a quick group from the currently selected sources."""
        sources = self._selected_editable_sources()
        if len(sources) >= 2:
            self.store.add_group("Group", (source.id for source in sources))

    def _ungroup_selected_sources(self) -> None:
        """Clear group membership for the selected sources."""
        self.store.assign_group(
            (source.id for source in self._selected_editable_sources()), None
        )

    def _toggle_selected_lock(self) -> None:
        """Toggle lock state for one or more selected sources."""
        selected = [
            item.source for item in self.canvas.scene_model.selectedItems()
            if hasattr(item, "source")
        ]
        if not selected:
            return
        should_lock = any(not source.locked for source in selected)
        for source in selected:
            self.store.update(source.id, locked=should_lock)

    def _unlock_all_sources(self) -> None:
        """Clear the lock flag on every locked source, from any entry point."""
        unlocked = self.store.unlock_all()
        if unlocked:
            korean = self.translator.is_korean
            self.statusBar().showMessage(
                f"{unlocked}개 요소의 잠금을 해제했습니다." if korean
                else f"Unlocked {unlocked} source(s).",
                3000,
            )

    def _handle_canvas_context_command(self, command: str) -> None:
        """Route Canvas menu commands through the editor's shared operations."""
        handlers = {
            "duplicate": self._duplicate_selected_sources,
            "delete": self._delete_selected_sources,
            "move_forward": lambda: self._move_selected_one_layer(front=True),
            "move_backward": lambda: self._move_selected_one_layer(front=False),
            "bring_front": lambda: self._move_selected_to_edge(front=True),
            "send_back": lambda: self._move_selected_to_edge(front=False),
            "center_horizontal": lambda: self._center_selected_sources(horizontal=True),
            "center_vertical": lambda: self._center_selected_sources(horizontal=False),
            "align_left": lambda: self._align_selected_sources("left"),
            "align_hcenter": lambda: self._align_selected_sources("hcenter"),
            "align_right": lambda: self._align_selected_sources("right"),
            "align_top": lambda: self._align_selected_sources("top"),
            "align_vcenter": lambda: self._align_selected_sources("vcenter"),
            "align_bottom": lambda: self._align_selected_sources("bottom"),
            "distribute_horizontal": lambda: self._distribute_selected_sources("horizontal"),
            "distribute_vertical": lambda: self._distribute_selected_sources("vertical"),
            "group": self._group_selected_sources,
            "ungroup": self._ungroup_selected_sources,
            "toggle_visible": self._toggle_selected_visibility,
            "toggle_lock": self._toggle_selected_lock,
            "unlock_all_layers": self._unlock_all_sources,
            "select_all": self._select_all_canvas_sources,
            "fit_canvas": self.canvas_fit,
        }
        handler = handlers.get(command)
        if handler is not None:
            handler()

    def _move_selected_one_layer(self, front: bool) -> None:
        """Move the selected block by one layer without disturbing its order."""
        self.store.move_layers(
            (source.id for source in self._selected_editable_sources()),
            1 if front else -1,
        )

    def _align_selected_sources(self, mode: str) -> None:
        """Align two or more selected sources to their collective bounds."""
        self.source_layout_controller.align(self._selected_editable_sources(), mode)

    def _distribute_selected_sources(self, axis: str) -> None:
        """Space selected sources with equal gaps, keeping the outside sources fixed."""
        self.source_layout_controller.distribute(self._selected_editable_sources(), axis)

    def _toggle_selected_visibility(self) -> None:
        """Show or hide all selected Canvas sources as one operation."""
        selected_ids = set(self.store.selected_ids)
        sources = [
            source for source in self.store.sources() if source.id in selected_ids
        ]
        if not sources:
            return
        visible = not all(source.visible for source in sources)
        for source in sources:
            self.store.update(source.id, visible=visible)

    def _build_menu_bar(self) -> None:
        """Build a localized menu bar organized by the user's editing workflow."""
        menu_bar = self.menuBar()
        self.file_menu = menu_bar.addMenu("")
        self.file_menu.addAction(self.new_action)
        self.file_menu.addAction(self.open_action)
        self.recent_projects_menu = self.file_menu.addMenu("")
        self.recent_projects_menu.aboutToShow.connect(
            self._rebuild_recent_projects_menu
        )
        self.recent_projects.changed.connect(self._rebuild_recent_projects_menu)
        self._rebuild_recent_projects_menu()
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.save_action)
        self.file_menu.addAction(self.save_as_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.export_action)
        self.file_menu.addAction(self.playlist_files_action)
        self.file_menu.addSeparator()
        self.import_m3u_action = QAction(self)
        self.import_m3u_action.triggered.connect(self._choose_m3u_playlist)
        self.file_menu.addAction(self.import_m3u_action)
        self.export_m3u_action = QAction(self)
        self.export_m3u_action.triggered.connect(self._export_m3u_playlist)
        self.file_menu.addAction(self.export_m3u_action)
        self.file_menu.addSeparator()
        self.exit_action = QAction(self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self.exit_action.triggered.connect(self.close)
        self.file_menu.addAction(self.exit_action)

        self.project_menu = menu_bar.addMenu("")
        self.project_settings_action = QAction(self)
        self.project_settings_action.triggered.connect(self._show_project_settings)
        self.project_menu.addAction(self.project_settings_action)
        self.project_menu.addSeparator()
        self.project_menu.addAction(self.presets_action)
        self.save_preset_action = QAction(self)
        self.save_preset_action.triggered.connect(self._save_current_as_preset)
        self.project_menu.addAction(self.save_preset_action)
        self.project_menu.addSeparator()
        self.upgrade_project_action = QAction(self)
        self.upgrade_project_action.setEnabled(False)
        self.upgrade_project_action.triggered.connect(self._upgrade_legacy_project)
        self.project_menu.addAction(self.upgrade_project_action)

        self.edit_menu = menu_bar.addMenu("")
        self.edit_menu.addAction(self.undo_action)
        self.edit_menu.addAction(self.redo_action)
        self.edit_menu.addSeparator()
        self.cut_action = QAction(self)
        self.cut_action.setShortcut(QKeySequence.StandardKey.Cut)
        self.cut_action.triggered.connect(self._cut_selected_sources)
        self.edit_menu.addAction(self.cut_action)
        self.copy_action = QAction(self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self._copy_selected_sources)
        self.edit_menu.addAction(self.copy_action)
        self.paste_action = QAction(self)
        self.paste_action.setShortcut(QKeySequence.StandardKey.Paste)
        self.paste_action.triggered.connect(self._paste_sources)
        self.edit_menu.addAction(self.paste_action)
        self.edit_menu.addSeparator()
        self.duplicate_action = QAction("복제", self)
        self.duplicate_action.setShortcut(QKeySequence("Ctrl+D"))
        self.duplicate_action.triggered.connect(self._duplicate_contextual_selection)
        self.edit_menu.addAction(self.duplicate_action)
        self.edit_menu.addAction(self.delete_action)
        self.edit_menu.addSeparator()
        self.select_all_action = QAction("전체 선택", self)
        self.select_all_action.setShortcut(QKeySequence("Ctrl+A"))
        self.select_all_action.triggered.connect(self._select_all_contextual)
        self.edit_menu.addAction(self.select_all_action)
        self.clear_selection_action = QAction(self)
        self.clear_selection_action.triggered.connect(self._clear_canvas_selection)
        self.edit_menu.addAction(self.clear_selection_action)
        # A bound method, not a lambda: the app-wide clipboard outlives this window,
        # and only a method connection is dropped when the window is destroyed.
        QApplication.clipboard().dataChanged.connect(self._sync_canvas_shortcut_actions_to_focus)

        self.insert_menu = menu_bar.addMenu("")
        source_categories = (
            ("basic", (SourceType.IMAGE, SourceType.VIDEO, SourceType.TEXT, SourceType.SHAPE)),
            ("playback", (
                SourceType.PROGRESS_BAR, SourceType.TIME, SourceType.ALBUM_COVER,
                SourceType.LYRICS, SourceType.TRACK_LIST, SourceType.NOW_PLAYING,
            )),
            ("branding", (
                SourceType.LOGO, SourceType.WATERMARK, SourceType.BACKGROUND,
            )),
            ("audio_effects", (
                SourceType.AUDIO_VISUALIZER, SourceType.AUDIO_WAVEFORM,
                SourceType.AUDIO_LEVEL_METER, SourceType.PARTICLE_OVERLAY,
            )),
        )
        self.insert_category_menus: dict[str, QMenu] = {}
        self.source_insert_actions: dict[SourceType, QAction] = {}
        for category, source_types in source_categories:
            category_menu = self.insert_menu.addMenu("")
            self.insert_category_menus[category] = category_menu
            for source_type in source_types:
                action = QAction(self)
                action.triggered.connect(
                    lambda checked=False, kind=source_type: self._add_source(kind)
                )
                category_menu.addAction(action)
                self.source_insert_actions[source_type] = action

        self.view_menu = menu_bar.addMenu("")
        self.view_menu.addAction(self.fit_action)
        self.view_menu.addAction(self.grid_action)
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.panels_action)
        self.view_menu.addAction(self.inspector_panel_action)
        self.view_menu.addAction(self.bottom_panel_action)
        self.reset_layout_action = QAction(self)
        self.reset_layout_action.triggered.connect(self._reset_workspace_layout)
        self.view_menu.addAction(self.reset_layout_action)
        self.view_menu.addSeparator()
        self.show_playlist_action = QAction(self)
        self.show_playlist_action.setShortcut(QKeySequence("Ctrl+Alt+1"))
        self.show_playlist_action.triggered.connect(lambda: self._show_bottom_panel(0))
        self.view_menu.addAction(self.show_playlist_action)
        self.show_timeline_action = QAction(self)
        self.show_timeline_action.setShortcut(QKeySequence("Ctrl+Alt+2"))
        self.show_timeline_action.triggered.connect(lambda: self._show_bottom_panel(1))
        self.view_menu.addAction(self.show_timeline_action)
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.preview_action)

        self.tools_menu = menu_bar.addMenu("")
        self.lyrics_search_action = QAction(self)
        self.lyrics_search_action.triggered.connect(self._show_lyrics_search)
        self.tools_menu.addAction(self.lyrics_search_action)
        self.lrc_generator_action = QAction(self)
        self.lrc_generator_action.triggered.connect(self._show_lrc_generator)
        self.tools_menu.addAction(self.lrc_generator_action)
        self.mp3_metadata_action = QAction(self)
        self.mp3_metadata_action.triggered.connect(self._show_mp3_metadata_editor)
        self.tools_menu.addAction(self.mp3_metadata_action)
        self.automix_editor_action = QAction(self)
        self.automix_editor_action.triggered.connect(lambda: self.preview_controller.edit_transition())
        settings = getattr(self, "_project_settings", None)
        self.automix_editor_action.setEnabled(settings is not None and settings.transition_mode == "automix")
        self.tools_menu.addAction(self.automix_editor_action)
        self.tools_menu.addSeparator()
        self.tools_menu.addAction(self.settings_action)
        self.tools_menu.addSeparator()
        self.language_menu.setTitle("언어")
        self.tools_menu.addMenu(self.language_menu)
        self.help_menu = menu_bar.addMenu("")
        self.help_action = QAction(self)
        self.help_action.setShortcut(QKeySequence("F1"))
        self.help_action.triggered.connect(self._show_help)
        self.help_menu.addAction(self.help_action)
        self.shortcuts_action = QAction(self)
        self.shortcuts_action.triggered.connect(self._show_shortcuts)
        self.help_menu.addAction(self.shortcuts_action)
        self.help_menu.addSeparator()
        self.check_updates_action = QAction(self)
        self.check_updates_action.triggered.connect(
            lambda: self._check_for_updates(manual=True)
        )
        self.help_menu.addAction(self.check_updates_action)
        self.help_menu.addSeparator()
        self.about_action = QAction(self)
        self.about_action.setMenuRole(QAction.MenuRole.AboutRole)
        self.about_action.triggered.connect(self._show_about)
        self.help_menu.addAction(self.about_action)
        # Toolbar actions bring their own icons; every other row gets one here.
        # Checkable rows (grid, panels, languages) keep their check mark instead.
        menu_icons = {
            self.save_as_action: "save_as", self.recent_projects_menu.menuAction(): "recent",
            self.import_m3u_action: "import_playlist", self.export_m3u_action: "export_playlist",
            self.exit_action: "exit", self.project_settings_action: "project_settings",
            self.save_preset_action: "save_preset", self.upgrade_project_action: "upgrade",
            self.cut_action: "cut", self.copy_action: "copy", self.paste_action: "paste",
            self.duplicate_action: "duplicate", self.select_all_action: "select_all",
            self.clear_selection_action: "clear_selection", self.reset_layout_action: "reset_layout",
            self.show_playlist_action: "track_list", self.show_timeline_action: "timeline",
            self.lrc_generator_action: "lyrics", self.automix_editor_action: "automix",
            self.lyrics_search_action: "lyrics",
            self.mp3_metadata_action: "audio",
            self.language_menu.menuAction(): "language", self.help_action: "help",
            self.shortcuts_action: "shortcuts", self.check_updates_action: "check_updates",
            self.about_action: "about",
            **{self.insert_category_menus[category].menuAction(): name for category, name in (
                ("basic", "add_basic"), ("playback", "now_playing"),
                ("branding", "logo"), ("audio_effects", "audio_visualizer"),
            )},
            **{action: kind.value for kind, action in self.source_insert_actions.items()},
        }
        for action, name in menu_icons.items():
            action.setIcon(menu_icon(name))
        self._sync_canvas_shortcut_actions(None, QApplication.focusWidget())

    def _rebuild_recent_projects_menu(self) -> None:
        """Populate File > Recent projects from the current valid MRU list."""
        menu = getattr(self, "recent_projects_menu", None)
        if not isinstance(menu, QMenu):
            return
        menu.clear()
        korean = self.translator.is_korean
        projects = self.recent_projects.projects()
        if not projects:
            empty_action = menu.addAction(
                "최근 프로젝트가 없습니다." if korean else "No recent projects"
            )
            empty_action.setEnabled(False)
            return

        for index, path in enumerate(projects, start=1):
            action = menu.addAction(f"{index}. {path.name}")
            action.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
            )
            action.setToolTip(str(path))
            action.setStatusTip(str(path))
            action.triggered.connect(
                lambda _checked=False, selected=Path(path):
                self._open_recent_project(selected)
            )
        menu.addSeparator()
        clear_action = menu.addAction(
            "최근 프로젝트 목록 지우기" if korean else "Clear recent projects"
        )
        clear_action.setIcon(menu_icon("clear"))
        clear_action.triggered.connect(self._confirm_clear_recent_projects)

    def _confirm_clear_recent_projects(self) -> bool:
        """Clear only the MRU history after an explicit user confirmation."""
        korean = self.translator.is_korean
        response = QMessageBox.question(
            self,
            "최근 프로젝트 목록 지우기" if korean else "Clear recent projects",
            (
                "최근에 연 프로젝트 목록을 모두 지울까요?\n\n"
                "프로젝트 파일 자체는 삭제되지 않습니다."
                if korean else
                "Clear the entire recent projects list?\n\n"
                "The project files themselves will not be deleted."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return False
        self.recent_projects.clear()
        self.statusBar().showMessage(
            "최근 프로젝트 목록을 지웠습니다."
            if korean else "Cleared the recent projects list.",
            3000,
        )
        return True

    def _open_recent_project(self, path: Path) -> bool:
        """Open one MRU entry through the same guarded workflow as File > Open."""
        project_path = Path(path).expanduser()
        korean = self.translator.is_korean
        if not project_path.is_file():
            self.recent_projects.remove(project_path)
            QMessageBox.warning(
                self,
                "프로젝트를 찾을 수 없음" if korean else "Project not found",
                f"최근 프로젝트 파일을 찾을 수 없어 목록에서 제거했습니다.\n\n{project_path}"
                if korean else
                f"The recent project could not be found and was removed from the list.\n\n{project_path}",
            )
            return False
        return self._open_project_with_confirmation(project_path)

    def _build_workspace(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        top_splitter = CanvasCenteredSplitter(
            Qt.Orientation.Horizontal, center_index=1,
        )
        top_splitter.setChildrenCollapsible(False)
        self.main_splitter = top_splitter
        left_workspace = QFrame()
        left_workspace.setObjectName("leftWorkspace")
        self.left_workspace = left_workspace
        left_workspace.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding,
        )
        left_layout = QVBoxLayout(left_workspace)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self.source_sidebar = self._make_source_sidebar()
        self.content_library_panel = ContentLibraryPanel(
            self.project_content_service, self.translator,
            used_keys_provider=lambda: self.project_content_service.referenced_keys(
                self._project_document()
            ),
        )
        self.content_library_panel.add_requested.connect(self._add_library_content)
        _refresh_content_markers = self.content_library_panel.schedule_used_refresh
        self.playlist_service.playlist_changed.connect(_refresh_content_markers)
        for _store_signal in (
            self.store.source_added, self.store.source_removed,
            self.store.source_changed, self.store.sources_replaced,
        ):
            _store_signal.connect(_refresh_content_markers)
        self.left_tabs = QTabWidget()
        self.left_tabs.setObjectName("leftProjectTabs")
        self.left_tabs.addTab(self.source_sidebar, "")
        self.left_tabs.addTab(self.content_library_panel, "")
        self.layer_panel = LayerPanel(self.store, self.translator)
        self.left_tabs.addTab(self.layer_panel, "")
        self.left_tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.left_tabs.tabBar().setExpanding(True)
        left_layout.addWidget(self.left_tabs)
        top_splitter.addWidget(left_workspace)
        center = QWidget()
        center.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(16, 16, 16, 12)
        self.canvas = LiveCanvas(self.store, self.translator)
        self.canvas.files_dropped.connect(self._handle_dropped_files)
        self.canvas.source_template_dropped.connect(self._handle_source_template_drop)
        self.canvas.cut_requested.connect(self._cut_selected_sources)
        self.canvas.copy_requested.connect(self._copy_selected_sources)
        self.canvas.paste_requested.connect(self._paste_sources)
        self.canvas.command_requested.connect(self._handle_canvas_context_command)
        self.canvas.nudge_requested.connect(self._nudge_selected_sources)
        self.canvas.edit_requested.connect(self._edit_canvas_source)
        self.canvas.rename_requested.connect(self._rename_selected_source)
        self.canvas_stack = QStackedWidget()
        self.canvas_stack.setObjectName("canvasWorkspaceStack")
        self.canvas_stack.addWidget(self.canvas)
        self._preview_gpu_composition_anchor: QWidget | None = None
        if (
            self._preview_backend_for_session == "gpu_layers"
            and GPU_TEXTURE_SURFACE_AVAILABLE
            and GpuTexturePreviewSurface is not None
        ):
            # Adding the first QOpenGLWidget to an already-visible top-level
            # window can make Qt recreate that native window. Register a hidden
            # surface before MainWindow is shown so opening Preview never causes
            # the visible close/reopen flash on Windows.
            self._preview_gpu_composition_anchor = GpuTexturePreviewSurface()
            self._preview_gpu_composition_anchor.setObjectName(
                "previewGpuCompositionAnchor"
            )
            self.canvas_stack.addWidget(self._preview_gpu_composition_anchor)
            self.canvas_stack.setCurrentWidget(self.canvas)
        center_layout.addWidget(self.canvas_stack, 1)
        top_splitter.addWidget(center)
        self.inspector = SourceInspector(
            self.store, self.translator,
            tracks_provider=lambda: self.playlist_service.tracks,
        )
        self.inspector.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding,
        )
        self.inspector.set_mix_transitions_active(self.project_settings.transition_mode != "none")
        self.inspector.animation_preview_requested.connect(
            self._preview_source_animation
        )
        self.inspector_stack = QStackedWidget()
        self.inspector_stack.setObjectName("inspectorWorkspaceStack")
        self.inspector_stack.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding,
        )
        self.preview_track_inspector = QFrame()
        self.preview_track_inspector.setObjectName("previewTrackInspectorPage")
        self.preview_track_inspector_layout = QVBoxLayout(
            self.preview_track_inspector
        )
        self.preview_track_inspector_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_track_inspector_layout.setSpacing(0)
        self.inspector_stack.addWidget(self.inspector)
        self.inspector_stack.addWidget(self.preview_track_inspector)
        self.inspector_stack.setCurrentWidget(self.inspector)
        top_splitter.addWidget(self.inspector_stack)
        top_splitter.setStretchFactor(0, 0)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setStretchFactor(2, 0)
        settings = QSettings()

        def saved_panel_size(
            key: str, default: int, minimum: int, maximum: int,
        ) -> int:
            try:
                value = int(settings.value(key, default))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(maximum, value))

        self._sidebar_open_width = saved_panel_size(
            "workspace/left_panel_width", self._sidebar_open_width, 180, 1600,
        )
        self._inspector_open_width = saved_panel_size(
            "workspace/right_panel_width", self._inspector_open_width, 220, 1600,
        )
        top_splitter.setSizes([
            self._sidebar_open_width, 990, self._inspector_open_width,
        ])
        top_splitter.lock_edge_sizes({
            0: self._sidebar_open_width,
            2: self._inspector_open_width,
        })
        self.workspace_splitter = CanvasCenteredSplitter(
            Qt.Orientation.Vertical, center_index=0,
        )
        self.workspace_splitter.setObjectName("workspaceSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.addWidget(top_splitter)
        top_splitter.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setObjectName("bottomWorkspaceTabs")
        self.bottom_tabs.setDocumentMode(True)
        self.bottom_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        self.playlist_editor = PlaylistEditor(self.playlist_service, self.translator)
        self.playlist_editor.request_files.connect(self._choose_audio_files)
        self.playlist_editor.files_dropped.connect(self._handle_dropped_files)
        self.playlist_editor.lyrics_dropped.connect(self._handle_lyrics_drop)
        self.playlist_editor.track_double_clicked.connect(self._show_track_details)
        self.playlist_editor.set_transitions(
            self.project_settings.transition_mode == "automix", self.project_settings.automix_overrides,
        )
        self.playlist_editor.transition_edit_requested.connect(self.preview_controller.edit_transition)
        self.playlist_editor.automix_editor_requested.connect(lambda: self.preview_controller.edit_transition())
        self.playlist_editor.transition_reset_requested.connect(lambda key: self._set_automix_override(key, None))
        self.playlist_editor.tracks_removed.connect(
            lambda count: self._show_undo_hint(
                f"플레이리스트에서 {count}곡을 삭제했습니다."
                if self.translator.is_korean else
                f"Removed {count} track(s) from the Playlist."
            )
        )
        self.bottom_tabs.addTab(self.playlist_editor, "")
        self.timeline_panel = TimelinePanel(
            self.playlist_service, self.store, self.translator
        )
        self.timeline_panel.preview_requested.connect(self.preview_controller.preview_at)
        self.bottom_tabs.addTab(self.timeline_panel, "")
        self.preview_tab_page = QFrame()
        self.preview_tab_page.setObjectName("previewWorkspaceTab")
        self.preview_tab_layout = QVBoxLayout(self.preview_tab_page)
        self.preview_tab_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_tab_layout.setSpacing(0)
        self.bottom_tabs.addTab(self.preview_tab_page, "")
        self.bottom_workspace_stack = QStackedWidget()
        self.bottom_workspace_stack.setObjectName("bottomWorkspaceStack")
        self.bottom_workspace_stack.addWidget(self.bottom_tabs)
        self.workspace_splitter.addWidget(self.bottom_workspace_stack)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 0)
        saved_sizes = settings.value("workspace/vertical_splitter", [650, 270])
        try:
            sizes = [max(120, int(value)) for value in saved_sizes]
        except (TypeError, ValueError):
            sizes = [650, 270]
        legacy_bottom_height = sizes[1] if len(sizes) == 2 else self._bottom_open_height
        self._bottom_open_height = saved_panel_size(
            "workspace/bottom_panel_height", legacy_bottom_height, 180, 1200,
        )
        self.workspace_splitter.setSizes([650, self._bottom_open_height])
        self.workspace_splitter.lock_edge_sizes({1: self._bottom_open_height})
        try:
            saved_tab = int(QSettings().value("workspace/bottom_tab", 0))
        except (TypeError, ValueError):
            saved_tab = 0
        self._last_edit_bottom_tab = max(0, min(1, saved_tab))
        self.bottom_tabs.setCurrentIndex(self._last_edit_bottom_tab)
        self.bottom_tabs.currentChanged.connect(self._bottom_workspace_tab_changed)
        self._workspace_settings_timer = QTimer(self)
        self._workspace_settings_timer.setSingleShot(True)
        self._workspace_settings_timer.setInterval(250)
        self._workspace_settings_timer.timeout.connect(self._save_workspace_layout)
        self.workspace_splitter.splitterMoved.connect(
            lambda _position, _index: self._workspace_settings_timer.start()
        )
        self.main_splitter.splitterMoved.connect(
            lambda _position, _index: self._workspace_settings_timer.start()
        )
        root_layout.addWidget(self.workspace_splitter, 1)
        self.setCentralWidget(root)
        self._restore_workspace_panel_visibility()

    def _reset_workspace_layout(self) -> None:
        """Put every panel back to its first-launch size (dragging them wide is sticky)."""
        self._sidebar_open_width = self._DEFAULT_LEFT_PANEL_WIDTH
        self._inspector_open_width = self._DEFAULT_RIGHT_PANEL_WIDTH
        self._bottom_open_height = self._DEFAULT_BOTTOM_PANEL_HEIGHT
        horizontal = self.main_splitter.sizes()
        if len(horizontal) == 3:
            left = 0 if self.left_workspace.isHidden() else self._sidebar_open_width
            right = 0 if self.inspector_stack.isHidden() else self._inspector_open_width
            self.main_splitter.setSizes([left, max(1, sum(horizontal) - left - right), right])
            self.main_splitter.lock_edge_sizes({0: left, 2: right})
        vertical = self.workspace_splitter.sizes()
        if len(vertical) == 2 and not self.bottom_workspace_stack.isHidden():
            bottom = self._bottom_open_height
            self.workspace_splitter.setSizes([max(1, sum(vertical) - bottom), bottom])
            self.workspace_splitter.lock_edge_sizes({1: bottom})
        self._save_workspace_layout()
        self._schedule_canvas_fit(50)

    def _save_workspace_layout(self) -> None:
        """Persist each panel's last useful open size after resizing settles."""
        horizontal = self.main_splitter.sizes()
        if len(horizontal) == 3:
            if not self.left_workspace.isHidden() and horizontal[0] >= 180:
                self._sidebar_open_width = horizontal[0]
            if not self.inspector_stack.isHidden() and horizontal[2] >= 220:
                self._inspector_open_width = horizontal[2]
        vertical = self.workspace_splitter.sizes()
        if (len(vertical) == 2 and not self.bottom_workspace_stack.isHidden()
                and vertical[1] >= 180):
            self._bottom_open_height = vertical[1]

        settings = QSettings()
        settings.setValue("workspace/left_panel_width", self._sidebar_open_width)
        settings.setValue("workspace/right_panel_width", self._inspector_open_width)
        settings.setValue("workspace/bottom_panel_height", self._bottom_open_height)
        # Retain the former key for compatibility with builds that only knew
        # the vertical splitter pair. Never persist a hidden panel's zero size.
        top_height = vertical[0] if len(vertical) == 2 else 650
        settings.setValue(
            "workspace/vertical_splitter",
            [max(120, top_height), self._bottom_open_height],
        )
        settings.sync()

    def _restore_workspace_panel_visibility(self) -> None:
        """Restore the three independent workspace panel visibility choices."""
        settings = QSettings()

        def preference(key: str) -> bool:
            try:
                return bool(settings.value(key, True, type=bool))
            except TypeError:
                value = settings.value(key, True)
                return str(value).strip().lower() not in {"0", "false", "no", "off"}

        entries = (
            (
                self.panels_action, self.left_workspace,
                preference("workspace/left_panel_visible"),
            ),
            (
                self.inspector_panel_action, self.inspector_stack,
                preference("workspace/right_panel_visible"),
            ),
            (
                self.bottom_panel_action, self.bottom_workspace_stack,
                preference("workspace/bottom_panel_visible"),
            ),
        )
        for action, widget, visible in entries:
            previous = action.blockSignals(True)
            action.setChecked(visible)
            action.blockSignals(previous)
            widget.setVisible(visible)

    def _make_source_sidebar(self) -> QWidget:
        """Build the source palette with a scrollable source-card area."""
        panel = QFrame()
        panel.setObjectName("sidePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(8)
        self.sidebar_title = QLabel()
        self.sidebar_title.setObjectName("panelTitle")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.source_result_label = QLabel()
        self.source_result_label.setObjectName("sourceResultCount")
        header.addWidget(self.sidebar_title)
        header.addStretch()
        header.addWidget(self.source_result_label)
        layout.addLayout(header)

        # Category tabs. "all" shows every stacked section; a category tab shows
        # only its own. A search spans everything and pins the bar to "all";
        # clicking any tab during a search clears the query first.
        self._source_tab_categories = ("all", "basic", "playback", "audio", "scene")
        self._active_source_category = "all"
        self._source_tab_syncing = False
        self.source_tab_bar = QTabBar()
        self.source_tab_bar.setObjectName("sourceCategoryTabs")
        self.source_tab_bar.setExpanding(False)
        self.source_tab_bar.setDrawBase(False)
        self.source_tab_bar.setUsesScrollButtons(True)
        for _category in self._source_tab_categories:
            self.source_tab_bar.addTab("")
        self.source_tab_bar.tabBarClicked.connect(self._on_source_tab_clicked)
        layout.addWidget(self.source_tab_bar)

        self.source_search = QLineEdit()
        self.source_search.setObjectName("sourceSearch")
        self.source_search.setClearButtonEnabled(True)
        self.source_search.addAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogBack),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.source_search.textChanged.connect(self._filter_source_cards)
        layout.addWidget(self.source_search)

        self.source_cards_scroll = QScrollArea()
        self.source_cards_scroll.setObjectName("sourceCardsScroll")
        self.source_cards_scroll.setWidgetResizable(True)
        self.source_cards_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.source_cards_scroll.setFrameShape(QFrame.Shape.NoFrame)
        cards_widget = QWidget()
        cards_layout = QVBoxLayout(cards_widget)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(12)
        descriptions = [
            ("image", SourceType.IMAGE), ("video", SourceType.VIDEO), ("text", SourceType.TEXT), ("shape", SourceType.SHAPE),
            ("progress_bar", SourceType.PROGRESS_BAR), ("album_cover", SourceType.ALBUM_COVER),
            ("time", SourceType.TIME), ("logo", SourceType.LOGO), ("watermark", SourceType.WATERMARK),
            ("background", SourceType.BACKGROUND), ("audio_visualizer", SourceType.AUDIO_VISUALIZER),
            ("lyrics", SourceType.LYRICS),
            ("track_list", SourceType.TRACK_LIST), ("now_playing", SourceType.NOW_PLAYING),
            ("audio_waveform", SourceType.AUDIO_WAVEFORM),
            ("audio_level_meter", SourceType.AUDIO_LEVEL_METER),
            ("particle_overlay", SourceType.PARTICLE_OVERLAY),
        ]
        variant_parents = {
            SourceType.TIME: SourceType.TEXT,
            SourceType.LOGO: SourceType.IMAGE,
            SourceType.WATERMARK: SourceType.IMAGE,
        }
        self._source_variant_parents = variant_parents
        self._source_search_terms: dict[SourceType, str] = {}
        description_keys = {source_type: key for key, source_type in descriptions}
        source_categories = (
            ("basic", (
                SourceType.TEXT, SourceType.SHAPE, SourceType.IMAGE, SourceType.VIDEO,
            )),
            ("playback", (
                SourceType.LYRICS, SourceType.ALBUM_COVER,
                SourceType.PROGRESS_BAR, SourceType.TRACK_LIST,
                SourceType.NOW_PLAYING,
            )),
            ("audio", (
                SourceType.AUDIO_VISUALIZER, SourceType.AUDIO_WAVEFORM,
                SourceType.AUDIO_LEVEL_METER,
            )),
            ("scene", (
                SourceType.BACKGROUND, SourceType.PARTICLE_OVERLAY,
            )),
        )
        self._source_category_sections: dict[str, QWidget] = {}
        self._source_category_titles: dict[str, QLabel] = {}
        self._source_type_categories: dict[SourceType, str] = {}
        for category, category_types in source_categories:
            section = QWidget()
            section.setObjectName("sourceCategorySection")
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(5)
            category_label = QLabel()
            category_label.setObjectName("sourceCategoryTitle")
            section_layout.addWidget(category_label)
            self._source_category_sections[category] = section
            self._source_category_titles[category] = category_label
            cards_layout.addWidget(section)
            for source_type in category_types:
                self._source_type_categories[source_type] = category
                self._add_source_palette_card(
                    source_type, description_keys, variant_parents,
                    section_layout,
                )
        cards_layout.addStretch(1)
        self.source_cards_scroll.setWidget(cards_widget)
        layout.addWidget(self.source_cards_scroll, 1)
        return panel

    def _add_source_palette_card(
        self, source_type: SourceType,
        description_keys: dict[SourceType, str],
        variant_parents: dict[SourceType, SourceType],
        section_layout: QVBoxLayout,
    ) -> None:
        """Add one top-level palette card and its property-only variants."""
        group = QFrame()
        group.setObjectName("sourceTemplateGroup")
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(4)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        button = SourceTemplateButton(source_type.value, source_type.value)
        button.set_card_icon(self._source_palette_icon(source_type))
        button.clicked.connect(
            lambda checked=False, kind=source_type: self._add_source(kind)
        )
        row_layout.addWidget(button, 1)
        variants = [
            child for child, parent in variant_parents.items()
            if parent is source_type
        ]
        if variants:
            toggle = QToolButton()
            toggle.setObjectName("sourceVariantToggle")
            toggle.setCheckable(True)
            toggle.setFixedWidth(30)
            toggle.setText("▸")
            toggle.toggled.connect(
                lambda expanded, parent_type=source_type: self._set_source_variants_expanded(
                    parent_type, expanded
                )
            )
            row_layout.addWidget(toggle)
            self._source_variant_toggles[source_type] = toggle
        group_layout.addWidget(row)
        self._source_buttons[source_type] = button
        self._source_search_terms[source_type] = (
            f"{description_keys[source_type]} {source_type.value}"
        ).lower()
        if variants:
            variant_container = QWidget()
            variant_container.setObjectName("sourceVariantContainer")
            variant_layout = QVBoxLayout(variant_container)
            variant_layout.setContentsMargins(18, 0, 0, 0)
            variant_layout.setSpacing(4)
            for child_type in variants:
                child_button = SourceTemplateButton(
                    child_type.value, source_type.value
                )
                child_button.set_card_icon(
                    self._source_palette_icon(child_type)
                )
                child_button.clicked.connect(
                    lambda checked=False, kind=child_type: self._add_source(kind)
                )
                self._source_buttons[child_type] = child_button
                self._source_search_terms[child_type] = (
                    f"{description_keys[child_type]} {child_type.value} "
                    f"{description_keys[source_type]} {source_type.value}"
                ).lower()
                variant_layout.addWidget(child_button)
            variant_container.hide()
            group_layout.addWidget(variant_container)
            self._source_variant_containers[source_type] = variant_container
        self._source_card_groups[source_type] = group
        section_layout.addWidget(group)

    def _source_palette_icon(self, source_type: SourceType) -> QIcon:
        """Return a glyph that depicts what the source is (T for text, a clock for time, ...)."""
        icon = source_icon(source_type.value)
        return icon if icon is not None else self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

    def _filter_source_cards(self, query: str) -> None:
        """Show palette sources matching the search and the active category tab."""
        normalized = query.strip().lower()
        if normalized and self.source_tab_bar.currentIndex() != 0:
            # A search always spans every category; pin the bar to "all".
            self._source_tab_syncing = True
            self.source_tab_bar.setCurrentIndex(0)
            self._source_tab_syncing = False
        active_category = (
            "all" if normalized
            else self._source_tab_categories[self.source_tab_bar.currentIndex()]
        )
        self._active_source_category = active_category
        matched_count = 0
        for parent_type, group in self._source_card_groups.items():
            in_tab = (
                active_category == "all"
                or self._source_type_categories.get(parent_type) == active_category
            )
            parent_button = self._source_buttons[parent_type]
            parent_text = (
                f"{self._source_search_terms.get(parent_type, '')} "
                f"{parent_button.property('paletteText') or ''}"
            ).lower()
            parent_match = in_tab and (not normalized or normalized in parent_text)
            if parent_match:
                matched_count += 1
            child_types = [
                child for child, parent in self._source_variant_parents.items()
                if parent is parent_type
            ]
            child_matches: dict[SourceType, bool] = {}
            for child_type in child_types:
                child_button = self._source_buttons[child_type]
                searchable = (
                    f"{self._source_search_terms.get(child_type, '')} "
                    f"{child_button.property('paletteText') or ''}"
                ).lower()
                child_matches[child_type] = in_tab and (
                    not normalized or normalized in searchable
                )
                if child_matches[child_type]:
                    matched_count += 1
                child_button.setVisible(child_matches[child_type])
            group.setVisible(parent_match or any(child_matches.values()))
            parent_button.setVisible(True)
            container = self._source_variant_containers.get(parent_type)
            if container is not None:
                expanded = self._source_variant_toggles[parent_type].isChecked()
                container.setVisible((in_tab and expanded and not normalized) or (
                    bool(normalized) and any(child_matches.values())
                ))
        for category, section in self._source_category_sections.items():
            section.setVisible(any(
                not group.isHidden()
                for source_type, group in self._source_card_groups.items()
                if self._source_type_categories.get(source_type) == category
            ))
        korean = self.translator.is_korean
        self.source_result_label.setText(
            f"{matched_count}개 결과" if korean and normalized else
            f"{matched_count}개" if korean else
            f"{matched_count} results" if normalized else
            f"{matched_count} sources"
        )

    def _on_source_tab_clicked(self, index: int) -> None:
        """Switch category, clearing any active search first (fires on every click)."""
        if self._source_tab_syncing or not 0 <= index < len(self._source_tab_categories):
            return
        if self.source_search.text():
            self.source_search.blockSignals(True)
            self.source_search.clear()
            self.source_search.blockSignals(False)
        self.source_tab_bar.setCurrentIndex(index)
        self._filter_source_cards("")

    def _set_source_variants_expanded(
        self, parent_type: SourceType, expanded: bool,
    ) -> None:
        """Expand one parent's property-only template variants."""
        toggle = self._source_variant_toggles.get(parent_type)
        container = self._source_variant_containers.get(parent_type)
        if toggle is not None:
            toggle.setText("▾" if expanded else "▸")
        if container is not None:
            container.setVisible(expanded)

    def _source_hover_help(self, source_type: SourceType) -> tuple[str, str]:
        """Return a concise purpose and Inspector-setting summary for a source."""
        korean = self.translator.is_korean
        korean_help = {
            SourceType.IMAGE: (
                "사진이나 그래픽 파일을 캔버스에 표시합니다.",
                "이미지 파일 · 맞춤 방식 · 밝기 · 대비 · 흐림 · 그림자",
            ),
            SourceType.VIDEO: (
                "곡별 또는 전체 타임라인에 맞춰 영상 파일을 재생합니다.",
                "실행 타이밍 · 영상 목록 · 반복 방식 · 사이클 · 속도 · 필터 · 흐림",
            ),
            SourceType.TEXT: (
                "제목과 설명 또는 동적 트랙 정보를 표시합니다.",
                "텍스트 · 글꼴 · 크기 · 정렬 · 줄바꿈 · 색상 · 외곽선",
            ),
            SourceType.SHAPE: (
                "디자인을 구성하는 도형과 색상 면을 추가합니다.",
                "도형 종류 · 채우기 · 그라데이션 · 외곽선 · 모서리 둥글기",
            ),
            SourceType.PROGRESS_BAR: (
                "현재 곡 또는 전체 재생 진행률을 표시합니다.",
                "진행 방식 · 스타일 · 진행 값 · 트랙 색상 · 채우기 색상",
            ),
            SourceType.ALBUM_COVER: (
                "재생 중인 곡의 앨범 이미지를 자동으로 표시합니다.",
                "이미지 맞춤 · 프레임 스타일 · 밝기 · 대비 · 그림자",
            ),
            SourceType.TIME: (
                "현재 시간이나 재생 시간을 동적으로 표시합니다.",
                "시간 형식 · 글꼴 · 크기 · 정렬 · 색상 · 외곽선",
            ),
            SourceType.LOGO: (
                "브랜드 또는 채널 로고 이미지를 배치합니다.",
                "로고 파일 · 이미지 맞춤 · 투명도 · 밝기 · 대비 · 그림자",
            ),
            SourceType.WATERMARK: (
                "영상 위에 워터마크 이미지를 표시합니다.",
                "이미지 파일 · 맞춤 방식 · 투명도 · 위치 · 크기 · 그림자",
            ),
            SourceType.BACKGROUND: (
                "캔버스 전체의 색상, 이미지 또는 앨범 아트 배경을 만듭니다.",
                "배경 방식 · 이미지 맞춤 · 앰비언트 효과 · 밝기 · 대비 · 흐림",
            ),
            SourceType.AUDIO_VISUALIZER: (
                "음악의 주파수 변화에 반응하는 시각 효과를 표시합니다.",
                "스타일 · 막대 수 · 선 굵기 · 감도 · 반응성 · 어택 · 릴리즈 · 스무딩",
            ),
            SourceType.LYRICS: (
                "재생 위치에 맞춰 가사 또는 자막을 표시합니다.",
                "자막 스타일 · 전환 · 문맥 줄 · 줄 간격 · 이전 줄 효과 · 타이밍 보정",
            ),
            SourceType.TRACK_LIST: (
                "현재 곡 주변의 플레이리스트 항목을 표시합니다.",
                "표시 곡 수 · 목록 스타일 · 표시 범위 · 곡 정보 · 간격 · 강조 색상",
            ),
            SourceType.NOW_PLAYING: (
                "현재 재생 중인 곡 정보를 카드 형태로 표시합니다.",
                "카드 스타일 · 표시 시간 · 퇴장 효과 · 글꼴 · 정렬 · 색상",
            ),
            SourceType.AUDIO_WAVEFORM: (
                "음원의 파형을 재생 진행과 함께 표시합니다.",
                "파형 스타일 · 채우기 색상 · 크기 · 투명도 · 애니메이션",
            ),
            SourceType.AUDIO_LEVEL_METER: (
                "음량 레벨과 피크를 실시간 미터로 표시합니다.",
                "모드 · 스타일 · 방향 · 감도 · 어택 · 릴리즈 · 구간 · 피크 · 색상",
            ),
            SourceType.PARTICLE_OVERLAY: (
                "캔버스 위에 움직이는 파티클 또는 노이즈 효과를 추가합니다.",
                "스타일 · 밀도 · 속도 · 크기 · 방향 · 반짝임 · 광택 · 색상 · 시드",
            ),
        }
        english_help = {
            SourceType.IMAGE: ("Display a photo or graphic file on the Canvas.", "Image file · fit mode · brightness · contrast · blur · shadow"),
            SourceType.VIDEO: ("Play video files per track or across the whole timeline.", "Timing · media list · repeat mode · cycles · speed · filters · blur"),
            SourceType.TEXT: ("Display a title, description, or dynamic track information.", "Text · font · size · alignment · wrapping · color · outline"),
            SourceType.SHAPE: ("Add a shape or color surface to the design.", "Shape · fill · gradient · outline · corner radius"),
            SourceType.PROGRESS_BAR: ("Show current-track or playlist progress.", "Progress mode · style · value · track color · fill color"),
            SourceType.ALBUM_COVER: ("Automatically show the current track's album artwork.", "Image fit · frame style · brightness · contrast · shadow"),
            SourceType.TIME: ("Dynamically show clock or playback time.", "Time format · font · size · alignment · color · outline"),
            SourceType.LOGO: ("Place a brand or channel logo image.", "Logo file · image fit · opacity · brightness · contrast · shadow"),
            SourceType.WATERMARK: ("Display a watermark image over the video.", "Image file · fit mode · opacity · position · size · shadow"),
            SourceType.BACKGROUND: ("Create a full-Canvas color, image, or album-art background.", "Background mode · image fit · ambient effect · brightness · contrast · blur"),
            SourceType.AUDIO_VISUALIZER: ("Show a visual effect that reacts to music frequencies.", "Style · bars · line width · sensitivity · reactivity · attack · release · smoothing"),
            SourceType.LYRICS: ("Show lyrics or subtitles synchronized to playback.", "Text color · transition · context lines · spacing · previous-line effect · timing offset"),
            SourceType.TRACK_LIST: ("Show playlist entries around the current track.", "Track count · list style · window · metadata · spacing · highlight colors"),
            SourceType.NOW_PLAYING: ("Show current-track information as a card.", "Card style · duration · exit effect · font · alignment · colors"),
            SourceType.AUDIO_WAVEFORM: ("Show the audio waveform together with playback progress.", "Waveform style · fill color · size · opacity · animation"),
            SourceType.AUDIO_LEVEL_METER: ("Show audio levels and peaks as a live meter.", "Mode · style · direction · sensitivity · attack · release · segments · peak · colors"),
            SourceType.PARTICLE_OVERLAY: ("Add moving particles or noise over the Canvas.", "Style · density · speed · size · direction · twinkle · glow · colors · seed"),
        }
        return (korean_help if korean else english_help)[source_type]

    def _source_palette_summary(self, source_type: SourceType) -> str:
        """Return a short secondary line sized for the narrow source palette."""
        korean = self.translator.is_korean
        summaries = {
            SourceType.IMAGE: ("사진과 그래픽", "Photos and graphics"),
            SourceType.VIDEO: ("영상 클립 재생", "Play video clips"),
            SourceType.TEXT: ("제목과 동적 정보", "Titles and dynamic info"),
            SourceType.SHAPE: ("색상 면과 도형", "Color surfaces and shapes"),
            SourceType.PROGRESS_BAR: ("곡·전체 진행률", "Track or playlist progress"),
            SourceType.ALBUM_COVER: ("현재 곡의 앨범 커버", "Current album artwork"),
            SourceType.TIME: ("시간과 재생 위치", "Clock and playback time"),
            SourceType.LOGO: ("브랜드 로고 이미지", "Brand logo image"),
            SourceType.WATERMARK: ("반투명 워터마크", "Transparent watermark"),
            SourceType.BACKGROUND: ("캔버스 전체 배경", "Full Canvas background"),
            SourceType.AUDIO_VISUALIZER: ("주파수 반응 효과", "Frequency-reactive effect"),
            SourceType.LYRICS: ("실시간 가사와 자막", "Timed lyrics and subtitles"),
            SourceType.TRACK_LIST: ("플레이리스트 목록", "Playlist track list"),
            SourceType.NOW_PLAYING: ("현재 곡 정보 카드", "Current-track info card"),
            SourceType.AUDIO_WAVEFORM: ("재생 반응 오디오 파형", "Playback-reactive waveform"),
            SourceType.AUDIO_LEVEL_METER: ("실시간 음량 미터", "Live audio level meter"),
            SourceType.PARTICLE_OVERLAY: ("파티클과 노이즈 효과", "Particles and noise effects"),
        }
        localized = summaries.get(source_type, ("캔버스 요소", "Canvas source"))
        return localized[0 if korean else 1]

    def _source_type_label(self, source_type: SourceType) -> str:
        """Return the same localized source name for menus and palette buttons."""
        korean = self.translator.is_korean
        try:
            return self.translator.text(source_type.value)
        except KeyError:
            labels = {
                SourceType.LYRICS: "가사 / 자막" if korean else "Lyrics / subtitles",
                SourceType.TRACK_LIST: "트랙 목록" if korean else "Track list",
                SourceType.NOW_PLAYING: "현재 재생 카드" if korean else "Now playing card",
                SourceType.AUDIO_WAVEFORM: "오디오 파형" if korean else "Audio waveform",
                SourceType.AUDIO_LEVEL_METER: "오디오 레벨 미터" if korean else "Audio level meter",
                SourceType.PARTICLE_OVERLAY: "파티클 / 노이즈" if korean else "Particles / noise",
                SourceType.VIDEO: "영상" if korean else "Video",
            }
            return labels.get(source_type, source_type.value.replace("_", " ").title())

    def _update_source_button_help(
        self, source_type: SourceType, button: QPushButton, label: str,
    ) -> None:
        """Install localized rich hover help without changing the compact layout."""
        description, settings = self._source_hover_help(source_type)
        korean = self.translator.is_korean
        settings_heading = "추가 후 설정" if korean else "Settings after adding"
        click_hint = (
            "클릭하거나 캔버스로 드래그하면 추가됩니다."
            if korean else "Click or drag it onto the Canvas to add it."
        )
        button.setToolTip(
            f"<div style='width: 330px'><b>{label}</b><br>"
            f"{description}<br><br><b>{settings_heading}</b><br>"
            f"{settings}<br><br><i>{click_hint}</i></div>"
        )
        button.setToolTipDuration(15_000)
        button.setAccessibleDescription(f"{description} {settings_heading}: {settings}")

    def _add_welcome_sources(self) -> None:
        artboard = self.canvas.scene_model.artboard_rect
        width = artboard.width()
        height = artboard.height()
        margin_x = max(40.0, min(100.0, width * 0.078125))
        title_width = max(180.0, min(520.0, width - margin_x * 2))
        progress_width = max(180.0, min(760.0, width - margin_x * 2))
        title_y = max(40.0, min(85.0, height * 0.118))
        progress_y = max(title_y + 140.0, height - max(80.0, height * 0.1944))
        progress_y = min(progress_y, height - 24.0)
        self.store.add(Source(SourceType.BACKGROUND, "Background", width=width, height=height,
                                fill_color="#202A29", locked=False, z_index=-10, text=""))
        self.store.add(Source(SourceType.TEXT, "Playlist title", x=margin_x, y=title_y,
                              width=title_width,
                                height=100, fill_color="#00000000", border_radius=0,
                                font_size=42, text_alignment="left",
                                text="Late Night Playlist", z_index=1))
        self.store.add(Source(SourceType.PROGRESS_BAR, "Progress", x=margin_x,
                              y=progress_y, width=progress_width,
                                height=6, fill_color="#79C7B4", border_radius=0, z_index=2))

    def _add_source(self, source_type: SourceType, position: object | None = None) -> None:
        """Add a base source or one of its property-only palette templates."""
        count = len(self.store.sources())
        template_type = source_type
        source_type = self._source_variant_parents.get(source_type, source_type)
        name = template_type.value.replace("_", " ").title()
        dimensions = (260.0, 90.0)
        if template_type in {SourceType.ALBUM_COVER, SourceType.LOGO}:
            dimensions = (180.0, 180.0)
        if source_type is SourceType.VIDEO:
            dimensions = (480.0, 270.0)
        if source_type in {SourceType.AUDIO_VISUALIZER, SourceType.AUDIO_WAVEFORM}:
            dimensions = (460.0, 100.0)
        if source_type is SourceType.LYRICS:
            # Timed preview normally displays previous/current/next cues. The
            # former generic 260x90 box was only large enough for its one-line
            # editor placeholder, making preview lyrics appear outside the
            # position chosen on the Canvas.
            dimensions = (620.0, 220.0)
        if source_type is SourceType.AUDIO_LEVEL_METER:
            dimensions = (80.0, 180.0)
        if source_type is SourceType.PARTICLE_OVERLAY:
            dimensions = (1280.0, 720.0)
        if source_type is SourceType.TRACK_LIST:
            dimensions = (460.0, 260.0)
        if source_type is SourceType.NOW_PLAYING:
            dimensions = (440.0, 170.0)
        if source_type is SourceType.BACKGROUND:
            dimensions = (1280.0, 720.0)
        default_text = name
        if template_type is SourceType.TIME:
            default_text = "%current_time% / %total_time%"
        elif source_type is SourceType.LYRICS:
            default_text = "Lyrics are not available for this track."
        elif source_type is SourceType.TRACK_LIST:
            default_text = "▶ 01. Current track\n  02. Next track"
        elif source_type is SourceType.NOW_PLAYING:
            default_text = "NOW PLAYING\nTrack title\nArtist"
        artboard = self.canvas.scene_model.artboard_rect
        default_x = 170 + (count % 4) * 35
        default_y = 190 + (count % 3) * 35
        if position is not None and hasattr(position, "x") and hasattr(position, "y"):
            default_x = max(
                artboard.left(),
                min(float(position.x()) - dimensions[0] / 2, artboard.right() - dimensions[0]),
            )
            default_y = max(
                artboard.top(),
                min(float(position.y()) - dimensions[1] / 2, artboard.bottom() - dimensions[1]),
            )
        source = source_registry.get(source_type).source_factory(
            source_type=source_type,
            name=name,
            x=default_x,
            y=default_y,
            width=dimensions[0], height=dimensions[1],
            border_radius=12 if source_type is not SourceType.PROGRESS_BAR else 8,
            fill_color="#1685D1" if source_type is not SourceType.BACKGROUND else "#263042",
            text=default_text,
            z_index=count,
            locked=False,
        )
        if source_type is SourceType.LYRICS:
            source.subtitle_context_lines = -1
            source.subtitle_next_lines = -1
        elif source_type is SourceType.TRACK_LIST:
            source.track_list_count = 0
        if template_type is SourceType.LOGO:
            source.image_fit_mode = "contain"
            source.border_radius = 0.0
            source.text = ""
        elif template_type is SourceType.WATERMARK:
            source.image_fit_mode = "contain"
            source.opacity = 0.45
            source.border_radius = 0.0
            source.text = ""
        self.store.add(source)

    def _handle_source_template_drop(
        self, source_type_value: str, parent_type_value: str, position: object,
    ) -> None:
        """Create a palette template centered at its Canvas drop point."""
        try:
            source_type = SourceType(source_type_value)
            parent_type = SourceType(parent_type_value)
        except ValueError:
            return
        expected_parent = self._source_variant_parents.get(source_type, source_type)
        if parent_type is not expected_parent:
            return
        self._add_source(source_type, position)

    def _maybe_start_automix_analysis(self) -> None:
        """Kick off background AutoMix analysis when it can produce something useful.

        A no-op when AutoMix is off, the playlist is empty, or no FFmpeg is
        configured yet -- silently, since this is a background convenience,
        not a user-initiated action (roadmap Phase 6 section 13 reserves
        the FFmpeg-missing UX for actual export/preview attempts).
        """
        if self.project_settings.transition_mode != "automix":
            return
        tracks = [track for track in self.playlist_service.tracks if track.enabled]
        if not tracks:
            return
        try:
            ffmpeg_executable = FFmpegRenderer(
                self.settings_service.current.ffmpeg_path or None
            ).executable
        except FFmpegNotFoundError:
            return
        # "auto": the light default analyzer (see analysis/registry.py) -- the
        # same policy FFmpegRenderer._render_automix_audio_segments uses for
        # Preview/Export, so this playlist-badge analysis and the render path
        # can never disagree on which analyzer produced a given result.
        # enable_structure_analysis=True: Sonara (optional, ~2 MB, no
        # heavyweight ML runtime unlike Beat This!) runs after rhythm
        # analysis in the same background worker when installed; when it
        # is not, this is a no-op (see _AutoMixAnalysisWorker._run_structure_analysis) --
        # this Commit B phase only collects structure data (intro/outro/
        # sections/energy curve) for a future planner, it does not yet
        # change AutoMix's actual transition choices.
        self.automix_analysis_controller.start(
            tracks, ffmpeg_executable, provider_id="auto", enable_structure_analysis=True,
        )

    AUTOMIX_ANALYSIS_ACTIVITY = "automix_analysis"

    def _automix_analysis_running_changed(self, running: bool) -> None:
        if running:
            self._automix_activity_timer.start()
        else:
            self._automix_activity_timer.stop()
            self.activity_progress.finish(self.AUTOMIX_ANALYSIS_ACTIVITY)

    def _update_automix_analysis_activity(self, show: bool = False) -> None:
        """Mirror the background AutoMix analysis pass in the status-bar progress.

        ``show`` adds it (after the pass outlived _automix_activity_timer);
        otherwise only an already shown entry is refreshed.
        """
        key = self.AUTOMIX_ANALYSIS_ACTIVITY
        controller = self.automix_analysis_controller
        if not controller.is_running or not (show or key in self.activity_progress.active_keys):
            return
        from app.controllers.automix_analysis_controller import RHYTHM_STAGE, STRUCTURE_STAGE

        korean = self.translator.is_korean
        stages = controller.stages
        names = {
            RHYTHM_STAGE: "박자·보컬 분석" if korean else "Beats & vocals",
            STRUCTURE_STAGE: "곡 구조 분석" if korean else "Song structure",
        }
        steps = [
            (names[stage], completed / total if total else 1.0,
             f"{completed} / {total}{'곡' if korean else ''}")
            for stage, (completed, total) in stages.items()
        ]
        done = sum(completed for completed, _total in stages.values())
        total = sum(count for _completed, count in stages.values())
        self.activity_progress.update(
            key, done / total if total else None,
            detail=("편집은 계속할 수 있습니다. 결과는 저장되어 미리보기와 내보내기에서 다시 쓰입니다."
                    if korean else "You can keep editing; results are cached for Preview and Export."),
            label="AutoMix 곡 분석" if korean else "AutoMix track analysis",
            steps=steps,
        )

    def _automix_analyses_received(self, analyses: dict[str, TrackAnalysis]) -> None:
        """Merge a completed background analysis pass into the session cache."""
        self.automix_analyses.update(analyses)
        self.playlist_editor.set_analyses(dict(self.automix_analyses))

    def _automix_structures_received(self, structures: dict[str, TrackStructureAnalysis]) -> None:
        """Merge a completed background structure-analysis pass into the session cache.

        Session-only, in-memory data (same as automix_analyses) -- no UI
        currently reads this; it exists so a future planner (Commit C) has
        somewhere to find it without re-running analysis.
        """
        self.automix_structures.update(structures)

    TRACK_ANALYSIS_ACTIVITY = "track_analysis"

    def _analyze_track(self, track_id: str, dialog: TrackDetailsDialog | None = None) -> None:
        """Analyze one track now, whatever the transition mode (Track information's button)."""
        track = next((entry for entry in self.playlist_service.tracks if entry.id == track_id), None)
        if track is None:
            return
        korean = self.translator.is_korean
        try:
            ffmpeg_executable = FFmpegRenderer(self.settings_service.current.ffmpeg_path or None).executable
        except FFmpegNotFoundError:
            QMessageBox.warning(
                dialog or self,
                "FFmpeg 필요" if korean else "FFmpeg required",
                "곡을 분석하려면 FFmpeg가 필요합니다. 도구 → 설정에서 FFmpeg를 설치하거나 경로를 지정하세요."
                if korean else
                "Analyzing a track needs FFmpeg. Install it or set its path in Tools → Settings.",
            )
            return
        # Same analyzer and cache as AutoMix, so Preview/Export reuse the result.
        self.track_analysis_controller.start(
            [track], ffmpeg_executable, provider_id="auto", enable_structure_analysis=True,
        )
        self._analyzing_track_id = track_id
        self.activity_progress.begin(
            self.TRACK_ANALYSIS_ACTIVITY,
            f"곡 분석 · {track.title}" if korean else f"Analyzing · {track.title}",
            0.0,
        )
        if dialog is not None:
            dialog.begin_analysis()

    def _track_analysis_step(self, track_id: str, step: str, fraction: float) -> None:
        from app.widgets.track_analysis_panel import STEP_NAMES, STEPS

        if track_id != self._analyzing_track_id:
            return
        korean = self.translator.is_korean
        name = STEP_NAMES[step][0 if korean else 1] if step in STEPS else (
            "저장된 결과 불러오기" if korean else "Loading the stored result")
        self.activity_progress.update(
            self.TRACK_ANALYSIS_ACTIVITY, 0.9 if step == STEPS[-1] else 0.85 * fraction, detail=name,
        )

    def _track_analysis_failed(self, failures: dict[str, str]) -> None:
        if self._analyzing_track_id in failures:
            korean = self.translator.is_korean
            self.statusBar().showMessage(
                f"곡을 분석하지 못했습니다: {failures[self._analyzing_track_id]}" if korean
                else f"Could not analyze the track: {failures[self._analyzing_track_id]}",
                8000,
            )

    def _track_analysis_running_changed(self, running: bool) -> None:
        if running:
            return
        self.activity_progress.finish(self.TRACK_ANALYSIS_ACTIVITY)
        track_id, self._analyzing_track_id = self._analyzing_track_id, ""
        track = next((entry for entry in self.playlist_service.tracks if entry.id == track_id), None)
        if track is not None and track_id in self.automix_analyses:
            self.statusBar().showMessage(
                f"'{track.title}' 분석을 마쳤습니다." if self.translator.is_korean
                else f"Finished analyzing '{track.title}'.",
                5000,
            )

    def _show_track_details(self, track_id: str) -> None:
        """Open track metadata and timed-lyrics editing for a playlist card."""
        track = next((entry for entry in self.playlist_service.tracks if entry.id == track_id), None)
        if track is None:
            return
        content_lyrics = [
            (Path(content.path).name, content.path)
            for content in self.project_content_service.items
            if content.media_type == "lyrics"
        ]
        try:
            ffmpeg_executable = FFmpegRenderer.find_executable(self.settings_service.current.ffmpeg_path or None)
        except FFmpegNotFoundError:
            ffmpeg_executable = None  # the dialog plays the original, without EQ
        dialog = TrackDetailsDialog(
            track, self.translator, self, content_lyrics=content_lyrics,
            analysis=self.automix_analyses.get(track_id),
            structure=self.automix_structures.get(track_id),
            ffmpeg_executable=ffmpeg_executable,
        )
        controller = self.track_analysis_controller
        dialog.analysis_requested.connect(lambda: self._analyze_track(track_id, dialog))
        controller.track_step_changed.connect(dialog.analysis_step)
        controller.analyses_updated.connect(dialog.analyses_received)
        controller.structures_updated.connect(dialog.structures_received)
        controller.analyses_failed.connect(dialog.analyses_failed)
        controller.running_changed.connect(dialog.analysis_running_changed)
        if controller.is_running and self._analyzing_track_id == track_id:
            dialog.begin_analysis()  # reopened while its analysis still runs
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()  # drops its connections to the analysis controller
        if accepted:
            self.playlist_service.update_track(
                track_id,
                title=dialog.selected_title,
                artist=dialog.selected_artist,
                album=dialog.selected_album,
                cover_path=dialog.selected_cover_path,
                video_paths=dialog.selected_video_paths,
                lyrics_path=dialog.selected_lyrics_path,
                lyrics=dialog.selected_lyrics,
                lyrics_timing_offset_seconds=dialog.selected_timing_offset,
                volume_db=dialog.selected_volume_db,
                eq_db=dialog.selected_eq_db,
            )

    def _choose_audio_files(self) -> None:
        """Let the user select audio files or M3U8 playlists for the playlist."""
        korean = self.translator.is_korean
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "음악 파일 추가" if korean else "Add music files",
            "",
            ("음악 파일 및 플레이리스트" if korean else "Audio files and playlists")
            + " (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.m3u8 *.m3u);;"
            + ("M3U8 플레이리스트" if korean else "M3U8 playlists") + " (*.m3u8 *.m3u)",
        )
        if paths:
            self._add_music_paths([Path(path) for path in paths])

    def _offer_music_for_empty_playlist(self, title: str) -> bool:
        """Explain why Preview/Export cannot start and offer the fix.

        Returns True once the Playlist has an enabled track again.
        """
        korean = self.translator.is_korean
        if self.playlist_service.tracks:
            # Songs exist but every one is excluded: adding more is not the fix.
            QMessageBox.warning(
                self, title,
                "플레이리스트의 모든 곡이 내보내기에서 제외되어 있습니다.\n\n"
                "곡을 선택한 뒤 Space 키 또는 오른쪽 클릭 메뉴의 '내보내기에 포함'으로 "
                "다시 포함해 주세요."
                if korean else
                "Every track in the Playlist is excluded from export.\n\n"
                "Select tracks and press Space, or right-click and choose "
                "'Include in export'.",
            )
            self._show_bottom_panel(0)
            return False
        answer = QMessageBox.warning(
            self, title,
            "플레이리스트에 곡이 없습니다.\n\n지금 음악 파일을 추가할까요?"
            if korean else
            "The Playlist has no tracks yet.\n\nAdd music files now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._choose_audio_files()
        return any(track.enabled for track in self.playlist_service.tracks)

    def _choose_m3u_playlist(self) -> None:
        """File menu: add the songs of one M3U8 playlist."""
        korean = self.translator.is_korean
        path, _ = QFileDialog.getOpenFileName(
            self,
            "M3U8 플레이리스트 가져오기" if korean else "Import M3U8 playlist",
            "",
            ("M3U8 플레이리스트" if korean else "M3U8 playlists") + " (*.m3u8 *.m3u)",
        )
        if path:
            self._add_music_paths([Path(path)])

    def _add_music_paths(self, paths: list[Path]) -> None:
        """Import audio files and playlists; only the songs become project content."""
        audio_count, accepted, notes = self._import_audio_files(
            [path for path in paths if path.suffix.lower() in AUDIO_EXTENSIONS]
        )
        playlist_count, playlist_accepted, playlist_notes = self._import_m3u_playlists(
            [path for path in paths if path.suffix.lower() in PLAYLIST_FILE_EXTENSIONS]
        )
        self.project_content_service.add_paths([*accepted, *playlist_accepted])
        self._notify_sidecar_lyrics([*notes, *playlist_notes])
        korean = self.translator.is_korean
        if playlist_count and audio_count:
            total = audio_count + playlist_count
            self.statusBar().showMessage(
                f"음악 {total}곡을 추가했습니다 (플레이리스트 파일에서 {playlist_count}곡)."
                if korean else
                f"Added {total} song(s) ({playlist_count} from the playlist file).",
                7000,
            )
        elif playlist_count:
            self.statusBar().showMessage(
                f"플레이리스트 파일에서 음악 {playlist_count}곡을 추가했습니다."
                if korean else
                f"Added {playlist_count} song(s) from the playlist file.",
                7000,
            )
        elif audio_count:
            self.statusBar().showMessage(
                f"플레이리스트에 음악 {audio_count}곡을 추가했습니다."
                if korean else f"Added {audio_count} song(s) to the Playlist.",
                5000,
            )

    def _import_m3u_playlists(
        self, paths: list[Path],
    ) -> tuple[int, list[Path], list[str]]:
        """Show each playlist's songs for review, then add them like dropped audio.

        The playlist file itself is never returned, so it never becomes project content.
        """
        added, accepted, notes = 0, [], []
        korean = self.translator.is_korean
        for path in paths:
            try:
                entries = read_m3u(path)
            except M3uPlaylistError as error:
                QMessageBox.warning(
                    self, "플레이리스트 파일 오류" if korean else "Playlist file error", str(error),
                )
                continue
            if not entries.audio_paths:
                message = (
                    f"'{path.name}'에서 찾을 수 있는 지원 음악 파일이 없습니다."
                    if korean else
                    f"'{path.name}' lists no supported music file that could be found."
                )
                if entries.skipped:
                    message += (
                        f"\n\n찾을 수 없거나 지원하지 않는 항목: {len(entries.skipped)}개"
                        if korean else
                        f"\n\nMissing or unsupported entries: {len(entries.skipped)}"
                    )
                QMessageBox.information(
                    self, "추가할 곡 없음" if korean else "No songs to add", message,
                )
                continue

            def review(candidates: list, playlist: Path = path, skipped: list[str] = entries.skipped) -> bool:
                dialog = M3uImportDialog(candidates, playlist, skipped, self.translator, self)
                return dialog.exec() == QDialog.DialogCode.Accepted

            count, paths_added, lyric_notes = self._import_audio_files(entries.audio_paths, review=review)
            added += count
            accepted.extend(paths_added)
            notes.extend(lyric_notes)
        return added, accepted, notes

    def _import_audio_files(
        self, paths: list[str] | list[Path],
        review: Callable[[list[AudioImportCandidate]], bool] | None = None,
    ) -> tuple[int, list[Path], list[str]]:
        """Inspect audio tags, request missing project metadata, and add tracks.

        ``review`` sees the inspected songs first and can cancel the whole import
        (the M3U8 preview), before the missing-metadata step.
        """
        if not paths or getattr(self, "_audio_import_worker", None) is not None:
            return 0, [], []
        korean = self.translator.is_korean
        self.activity_progress.begin(
            "content_add", "콘텐츠 추가" if korean else "Adding content",
            detail=(f"오디오 {len(paths)}개 분석 중" if korean
                    else f"Inspecting {len(paths)} audio file(s)"),
        )
        try:
            candidates = self._inspect_audio_files(paths)
            if not candidates:
                return 0, [], []
            if review is not None and not review(candidates):
                return 0, [], []
            tracks = [candidate.track for candidate in candidates]
            if any(candidate.missing_fields for candidate in candidates):
                self.activity_progress.update(
                    "content_add", detail=(
                        "누락된 곡 정보를 확인하는 중" if korean
                        else "Waiting for missing track information"
                    ),
                )
                dialog = AudioMetadataDialog(candidates, self.translator, self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return 0, [], []
                tracks = dialog.selected_tracks
            self.activity_progress.update(
                "content_add", 0.8,
                "프로젝트 콘텐츠에 등록하는 중" if korean
                else "Registering project content",
            )
            lyric_notes = self._attach_sidecar_lyrics(tracks)
            added = self.playlist_service.add_tracks(tracks)
            accepted_paths = [Path(track.file_path) for track in tracks]
            return added, accepted_paths, lyric_notes
        finally:
            self.activity_progress.finish("content_add")

    def _inspect_audio_files(self, paths: list[str] | list[Path]) -> list[AudioImportCandidate]:
        """Keep metadata/FFprobe and cover decoding off the GUI thread."""
        korean = self.translator.is_korean
        worker = AudioImportWorker(self.playlist_service, paths)
        loop = QEventLoop()
        dialog = QProgressDialog(
            "음악 파일을 확인하고 있습니다…" if korean else "Inspecting music files…",
            "취소" if korean else "Cancel", 0, 1000, self,
        )
        dialog.setWindowTitle("음악 추가" if korean else "Adding music")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(250)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)

        def on_progress(done: int, total: int, name: str, stage: str) -> None:
            if worker.cancel_event.is_set():
                return
            label = ("앨범 커버 준비" if korean else "Preparing album covers") if stage == "covers" else (
                "곡 정보 확인" if korean else "Reading track information"
            )
            detail = f"{label} · {done + 1} / {total} · {name}"
            fraction = ((1 if stage == "covers" else 0) + done / max(1, total)) / 2
            dialog.setValue(round(1000 * fraction))
            dialog.setLabelText(detail)
            self.activity_progress.update("content_add", 0.7 * fraction, detail)

        worker.progress.connect(on_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
        dialog.canceled.connect(worker.cancel_event.set)
        dialog.canceled.connect(loop.quit)
        # Exclude input until the delayed modal popup is visible. The parent
        # stays disabled for the whole inspection, including very fast imports.
        was_enabled = self.isEnabled()
        self._audio_import_worker = worker
        self.setEnabled(False)
        dialog.setEnabled(True)
        try:
            worker.start()
            loop.exec()
            worker.progress.disconnect(on_progress)
            cancelled = worker.cancel_event.is_set()
            if cancelled:
                dialog.setCancelButton(None)
                dialog.setLabelText("취소 중…" if korean else "Cancelling…")
                dialog.show()
            stop_qthread_now(worker)
            if cancelled:
                self.statusBar().showMessage(
                    "음악 추가를 취소했습니다." if korean else "Music import cancelled.", 5000,
                )
                return []
            if worker.error is not None:
                QMessageBox.warning(
                    self, "음악 추가 오류" if korean else "Music import error", str(worker.error),
                )
                return []
            return worker.candidates
        finally:
            dialog.canceled.disconnect()
            dialog.hide()
            dialog.deleteLater()
            self.setEnabled(was_enabled)
            self._audio_import_worker = None

    def _attach_sidecar_lyrics(self, tracks: list) -> list[str]:
        """Attach a same-name lyric file, or prompt for a similar one, per settings.

        Returns a short "track ← file" note for every attachment so the caller
        can surface a status-bar notification.
        """
        mode = self.settings_service.current.lyrics_auto_attach_mode
        if mode == "never":
            return []
        korean = self.translator.is_korean
        notes: list[str] = []
        folder_entries: dict[Path, list[Path]] = {}
        for track in tracks:
            if track.lyrics or track.lyrics_path:
                continue
            folder = Path(track.file_path).parent
            if folder not in folder_entries:
                try:
                    folder_entries[folder] = sorted(
                        entry for entry in folder.iterdir()
                        if entry.suffix.lower() in LYRICS_EXTENSIONS and entry.is_file()
                    )
                except OSError:
                    folder_entries[folder] = []
            exact, similar = find_sidecar_lyrics(track.file_path, entries=folder_entries[folder])
            sidecar = None
            candidates = exact or similar
            if len(candidates) > 1:
                sidecar = self._choose_sidecar_lyrics(track.title, candidates, korean)
            elif exact:
                sidecar = exact[0]
                if mode == "ask" and not self._confirm_sidecar_lyrics(
                    track.title, sidecar, korean,
                ):
                    sidecar = None
            elif similar and self._confirm_sidecar_lyrics(
                track.title, similar[0], korean,
            ):
                sidecar = similar[0]
            if sidecar is None:
                continue
            try:
                cues = LyricsService.load(sidecar.path)
            except LyricsError:
                continue
            if not cues:
                continue
            track.lyrics = cues
            track.lyrics_path = str(sidecar.path.resolve())
            notes.append(f"'{track.title}' ← {sidecar.path.name}")
        return notes

    def _choose_sidecar_lyrics(self, track_title: str, candidates: list, korean: bool):
        """Always let the user resolve competing lyric files, even in auto mode."""
        labels = [f"{entry.path.name} · " + (
            ("실시간 가사 (TTML)" if korean else "Real-time lyrics (TTML)")
            if entry.path.suffix.lower() == ".ttml" else
            ("줄 단위 가사" if korean else "Line lyrics")
        ) for entry in candidates]
        chosen, accepted = QInputDialog.getItem(
            self, "가사 파일 선택" if korean else "Choose lyric file",
            (f"'{track_title}' 곡의 가사 파일이 여러 개 있습니다. 추가할 파일을 선택하세요."
             if korean else f"Multiple lyric files were found for '{track_title}'. Choose one to attach."),
            labels, 0, False,
        )
        return candidates[labels.index(chosen)] if accepted and chosen in labels else None

    def _confirm_sidecar_lyrics(
        self, track_title: str, sidecar: object, korean: bool,
    ) -> bool:
        """Ask before attaching a lyric file that was matched by name."""
        exact = bool(getattr(sidecar, "exact", False))
        name = Path(getattr(sidecar, "path", "")).name
        if korean:
            title = "가사·자막 파일 발견"
            relation = "이름이 같은" if exact else "이름이 비슷한"
            body = (
                f"'{track_title}' 곡과 {relation} 자막 파일이 있습니다:\n{name}\n\n"
                "가사로 함께 추가할까요?"
            )
        else:
            title = "Lyric file found"
            relation = "the same name as" if exact else "a similar name to"
            body = (
                f"A subtitle file with {relation} '{track_title}' was found:\n"
                f"{name}\n\nAttach it as lyrics?"
            )
        return QMessageBox.question(
            self, title, body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) == QMessageBox.StandardButton.Yes

    def _notify_sidecar_lyrics(self, notes: list[str]) -> None:
        """Show a transient status-bar note for auto-attached lyric files."""
        if not notes:
            return
        korean = self.translator.is_korean
        if len(notes) == 1:
            message = (
                f"가사·자막 파일을 자동으로 연결했습니다 · {notes[0]}"
                if korean else
                f"Attached a lyric file automatically · {notes[0]}"
            )
        else:
            message = (
                f"가사·자막 파일 {len(notes)}개를 자동으로 연결했습니다"
                if korean else
                f"Attached {len(notes)} lyric files automatically"
            )
        self.statusBar().showMessage(message, 7000)

    def _add_library_content(self, path: str, media_type: str) -> None:
        """Turn a reusable library entry into the appropriate project object."""
        content_path = Path(path)
        if not content_path.is_file():
            QMessageBox.warning(
                self,
                "콘텐츠를 찾을 수 없음" if self.translator.is_korean else "Content not found",
                str(content_path),
            )
            return
        if media_type == "image":
            self._add_dropped_images([content_path], None)
        elif media_type == "audio":
            _added, _paths, lyric_notes = self._import_audio_files([content_path])
            self._notify_sidecar_lyrics(lyric_notes)
        elif media_type == "video":
            self._add_dropped_videos([content_path], None)
        elif media_type == "font":
            font_id = QFontDatabase.addApplicationFont(str(content_path))
            families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            source = Source(
                SourceType.TEXT, content_path.stem, x=180, y=180,
                width=520, height=110, text="Playlist title",
                font_family=families[0] if families else "Segoe UI",
                font_path=str(content_path.resolve()), z_index=len(self.store.sources()),
            )
            self.store.add(source)
            self.store.select(source.id)
        elif media_type == "lyrics":
            selected_ids = [
                str(item.data(Qt.ItemDataRole.UserRole))
                for item in self.playlist_editor.list_widget.selectedItems()
            ]
            tracks = self.playlist_service.tracks
            target = next((track for track in tracks if track.id in selected_ids), None)
            if target is None and len(tracks) == 1:
                target = tracks[0]
            if target is None:
                QMessageBox.information(
                    self,
                    "가사 연결" if self.translator.is_korean else "Attach lyrics",
                    "플레이리스트에서 가사를 연결할 곡 하나를 선택해 주세요."
                    if self.translator.is_korean else
                    "Select one playlist track, then add this lyrics file again.",
                )
                return
            self._attach_lyrics_to_track(content_path, target.id)

    def _handle_lyrics_drop(self, path: str, track_id: str) -> None:
        """Attach a dropped lyrics file to the exact playlist row under the pointer."""
        korean = self.translator.is_korean
        if not track_id:
            QMessageBox.information(
                self,
                "가사 연결" if korean else "Attach lyrics",
                "가사 파일을 적용할 곡 위에 직접 놓아 주세요."
                if korean else "Drop the lyrics file directly onto a playlist track.",
            )
            return
        self._attach_lyrics_to_track(Path(path), track_id)

    def _attach_lyrics_to_track(self, content_path: Path, track_id: str) -> bool:
        """Load, compare, and attach lyrics without silently replacing existing work."""
        korean = self.translator.is_korean
        target = next(
            (track for track in self.playlist_service.tracks if track.id == track_id),
            None,
        )
        if target is None:
            QMessageBox.warning(
                self, "가사 연결" if korean else "Attach lyrics",
                "선택한 곡을 찾을 수 없습니다."
                if korean else "The selected track could not be found.",
            )
            return False
        if not content_path.is_file():
            QMessageBox.warning(
                self, "가사 파일을 찾을 수 없음" if korean else "Lyrics not found",
                str(content_path),
            )
            return False
        try:
            incoming_cues = LyricsService.load(content_path)
        except LyricsError as error:
            QMessageBox.warning(self, "Lyrics", str(error))
            return False
        if not incoming_cues:
            QMessageBox.warning(
                self, "가사 연결" if korean else "Attach lyrics",
                "가사 파일에 인식할 수 있는 타이밍 가사가 없습니다."
                if korean else "The file contains no recognizable timed lyrics.",
            )
            return False

        current_cues = list(target.lyrics)
        if not current_cues and target.lyrics_path:
            try:
                current_cues = LyricsService.load(target.lyrics_path)
            except LyricsError:
                # The project may still contain usable embedded cues or a stale
                # path. The comparison remains useful even with an empty side.
                current_cues = []
        if target.lyrics or target.lyrics_path:
            dialog = LyricsCompareDialog(
                current_cues, incoming_cues, target.lyrics_path,
                str(content_path.resolve()), self.translator, self,
            )
            if (
                dialog.exec() != QDialog.DialogCode.Accepted
                or not dialog.replace_requested
            ):
                return False

        resolved = str(content_path.resolve())
        self.playlist_service.update_track(
            target.id, lyrics_path=resolved, lyrics=incoming_cues,
        )
        self.project_content_service.add_paths([content_path])
        if not any(
            source.source_type is SourceType.LYRICS for source in self.store.sources()
        ):
            self._add_source(SourceType.LYRICS)
        self.statusBar().showMessage(
            f"'{target.title}' 곡에 가사를 적용했습니다."
            if korean else f"Attached lyrics to '{target.title}'.",
            5000,
        )
        return True

    @staticmethod
    def _window_drop_paths(event: QDragEnterEvent | QDropEvent) -> list[str]:
        if not event.mimeData().hasUrls():
            return []
        return [
            path
            for url in event.mimeData().urls()
            if url.isLocalFile() and (path := url.toLocalFile())
        ]

    @staticmethod
    def _drop_contains_lyrics(paths: list[str]) -> bool:
        return any(
            Path(path).suffix.lower() in LYRICS_EXTENSIONS for path in paths
        )

    def _drag_returned_to_project_content(
        self, event: QDragEnterEvent | QDropEvent,
    ) -> bool:
        """Treat the Project Content panel as an invalid target for its own drag."""
        if event.source() is not self.content_library_panel.list:
            return False
        if not self.content_library_panel.isVisible():
            return False
        window_position = event.position().toPoint()
        global_position = self.mapToGlobal(window_position)
        panel_position = self.content_library_panel.mapFromGlobal(global_position)
        return self.content_library_panel.rect().contains(panel_position)

    @staticmethod
    def _reject_returned_project_content_drag(
        event: QDragEnterEvent | QDropEvent,
    ) -> None:
        # Consume IgnoreAction at the top-level window. This prevents the event
        # from falling through to the normal global file-drop/import path.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept general files globally, but reserve lyrics for Playlist rows."""
        if self._drag_returned_to_project_content(event):
            self._reject_returned_project_content_drag(event)
            return
        paths = self._window_drop_paths(event)
        if paths and not self._drop_contains_lyrics(paths):
            event.acceptProposedAction()
            return
        # A lyric drag rejected by child widgets must stay rejected here too;
        # otherwise it bubbles to MainWindow and the cursor becomes allowed.
        event.ignore()

    def dragMoveEvent(self, event: QDragEnterEvent) -> None:
        """Keep lyrics forbidden everywhere except a Playlist track target."""
        self.dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        """Route general file drops while refusing lyrics outside Playlist rows."""
        if self._drag_returned_to_project_content(event):
            self._reject_returned_project_content_drag(event)
            return
        paths = self._window_drop_paths(event)
        if not paths or self._drop_contains_lyrics(paths):
            event.ignore()
            return
        self._handle_dropped_files(paths)
        event.acceptProposedAction()

    def _handle_dropped_files(self, raw_paths: list[str], position: object | None = None) -> None:
        """Classify a mixed file drop and add it to the appropriate editor area."""
        paths = [Path(raw_path) for raw_path in raw_paths if Path(raw_path).is_file()]
        project_paths = [
            path for path in paths
            if path.suffix.lower() == ProjectService.PACKAGE_SUFFIX
            or path.name.lower().endswith(".project.json")
        ]
        if project_paths:
            self._confirm_and_load_dropped_project(project_paths[0])
            return
        image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".svg"}
        image_paths = [path for path in paths if path.suffix.lower() in image_extensions]
        video_extensions = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
        video_paths = [path for path in paths if path.suffix.lower() in video_extensions]
        audio_paths = [path for path in paths if path.suffix.lower() in AUDIO_EXTENSIONS]
        playlist_paths = [path for path in paths if path.suffix.lower() in PLAYLIST_FILE_EXTENSIONS]
        image_count = self._add_dropped_images(image_paths, position)
        video_count = self._add_dropped_videos(video_paths, position)
        audio_count, accepted_audio_paths, lyric_notes = self._import_audio_files(
            audio_paths
        )
        playlist_count, playlist_audio_paths, playlist_notes = self._import_m3u_playlists(playlist_paths)
        audio_count += playlist_count
        lyric_notes += playlist_notes
        # Only the songs a playlist lists become project content, never the .m3u8 itself.
        self.project_content_service.add_paths(
            [*image_paths, *video_paths, *accepted_audio_paths, *playlist_audio_paths]
        )
        if image_count or video_count or audio_count:
            korean = self.translator.is_korean
            message = (
                f"이미지 {image_count}개, 영상 {video_count}개, 음악 {audio_count}개를 추가했습니다."
                if korean else f"Added {image_count} image(s), {video_count} video source(s), and {audio_count} music file(s)."
            )
            if lyric_notes:
                message += (
                    f" 가사·자막 {len(lyric_notes)}개 연결됨."
                    if korean else
                    f" Attached {len(lyric_notes)} lyric file(s)."
                )
            self.statusBar().showMessage(message, 7000)
            return
        if playlist_paths:
            return  # the playlist review was cancelled or already explained why nothing was added
        korean = self.translator.is_korean
        self.statusBar().showMessage(
            "지원되는 이미지, 영상, 음악, 플레이리스트(M3U8) 또는 프로젝트 파일을 놓아 주세요."
            if korean else "Drop supported image, video, music, playlist (M3U8), or project files.",
            5000,
        )

    def _add_dropped_images(self, paths: list[Path], position: object | None) -> int:
        """Create Canvas image sources at the drop point while preserving aspect ratio."""
        if not paths:
            return 0
        korean = self.translator.is_korean
        self.activity_progress.begin(
            "content_add", "콘텐츠 추가" if korean else "Adding content",
            detail=(f"이미지 {len(paths)}개 처리 중" if korean
                    else f"Processing {len(paths)} image(s)"),
        )
        QApplication.processEvents()
        try:
            artboard = self.canvas.scene_model.artboard_rect
            point = position if hasattr(position, "x") and hasattr(position, "y") else artboard.center()
            count = 0
            last_source: Source | None = None
            for index, path in enumerate(paths):
                self.activity_progress.update(
                    "content_add", index / len(paths),
                    f"{path.name} ({index + 1}/{len(paths)})",
                )
                pixmap = load_pixmap(path)
                if pixmap.isNull():
                    continue
                source_width = float(pixmap.width())
                source_height = float(pixmap.height())
                scale = min(1.0, 520.0 / source_width, 360.0 / source_height)
                width = max(48.0, source_width * scale)
                height = max(48.0, source_height * scale)
                x = max(0.0, min(float(point.x()) + index * 24, artboard.width() - width))
                y = max(0.0, min(float(point.y()) + index * 24, artboard.height() - height))
                last_source = Source(
                    source_type=SourceType.IMAGE,
                    name=path.stem,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    border_radius=0.0,
                    content_path=str(path.resolve()),
                    text="",
                    z_index=len(self.store.sources()) + index,
                )
                self.store.add(last_source)
                count += 1
                if index % 4 == 0:
                    QApplication.processEvents()
            if last_source is not None:
                self.store.select(last_source.id)
            return count
        finally:
            self.activity_progress.finish("content_add")

    def _confirm_and_load_dropped_project(self, path: Path) -> None:
        """Ask before replacing the active work with a dropped project document."""
        korean = self.translator.is_korean
        if self.project_session.dirty:
            previous_project_path = self.current_project_path
            if (self._confirm_unsaved_changes()
                    and self._load_project_path(path)):
                try:
                    self.autosave.clear(previous_project_path)
                except ProjectError as error:
                    self.statusBar().showMessage(str(error), 5000)
            return
        response = QMessageBox.question(
            self,
            "프로젝트 열기" if korean else "Open project",
            f"'{path.name}' 프로젝트를 열까요?\n현재 작업은 저장하지 않으면 사라질 수 있습니다."
            if korean else
            f"Open '{path.name}'?\nUnsaved current work may be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response == QMessageBox.StandardButton.Yes:
            self._load_project_path(path)

    def _add_dropped_videos(self, paths: list[Path], position: object | None) -> int:
        """Create one video element whose ordered media list is the dropped files."""
        valid = [path.resolve() for path in paths if path.is_file()]
        if not valid:
            return 0
        artboard = self.canvas.scene_model.artboard_rect
        point = position if hasattr(position, "x") and hasattr(position, "y") else artboard.center()
        width, height = 480.0, 270.0
        source = Source(
            SourceType.VIDEO,
            valid[0].stem if len(valid) == 1 else f"Video playlist ({len(valid)})",
            x=max(0.0, min(float(point.x()) - width / 2, artboard.width() - width)),
            y=max(0.0, min(float(point.y()) - height / 2, artboard.height() - height)),
            width=width,
            height=height,
            content_path=str(valid[0]),
            video_paths=[str(path) for path in valid],
            video_repeat_mode="once" if len(valid) == 1 else "sequence",
            text="",
            border_radius=0.0,
            z_index=len(self.store.sources()),
        )
        self.store.add(source)
        self.store.select(source.id)
        return 1

    def _stage_export_frame(
        self, image: QImage, duration_seconds: float, stream_key: str = "base",
    ) -> RenderFrame:
        return self.export_orchestrator.stage_frame(image, duration_seconds, stream_key)

    def _cancel_export_png_pipeline(self) -> None:
        self.export_orchestrator.cancel_png_pipeline()

    @staticmethod
    def _export_animation_sample_rate(output_fps: int) -> int:
        return ExportOrchestrator.animation_sample_rate(output_fps)

    def _clear_export_frame_staging(self) -> None:
        self.export_orchestrator.clear_frame_staging()

    def _lock_main_form_for_export(self) -> None:
        self.export_orchestrator.lock_main_form()

    def _unlock_main_form_after_export(self) -> None:
        self.export_orchestrator.unlock_main_form()

    @staticmethod
    def _export_notification_allowed(
        settings: AppSettings,
        step: str,
        application_active: bool,
    ) -> bool:
        return ExportOrchestrator.notification_allowed(settings, step, application_active)

    def _sync_export_notification_tray(self, settings: AppSettings) -> None:
        self.export_orchestrator.sync_notification_tray(settings)

    def _restore_from_export_notification(self) -> None:
        self.export_orchestrator.restore_from_notification()

    def _show_system_notification(
        self,
        title: str,
        message: str,
        *,
        critical: bool = False,
    ) -> bool:
        return self.export_orchestrator.show_system_notification(title, message, critical=critical)

    def _notify_export_stage(self, stage: str) -> None:
        self.export_orchestrator.notify_stage(stage)

    @staticmethod
    def _format_bytes(count: int) -> str:
        return ExportOrchestrator.format_bytes(count)

    def _freeze_export_storage_monitor(self) -> None:
        self.export_orchestrator.freeze_storage_monitor()

    def _handle_export_storage_path(
        self, kind: str, path: str | Path | None,
    ) -> None:
        self.export_orchestrator.handle_storage_path(kind, path)

    def _handle_export_storage_snapshot(self, snapshot: object) -> None:
        self.export_orchestrator.handle_storage_snapshot(snapshot)

    def _stop_export_storage_monitor(self) -> None:
        self.export_orchestrator.stop_storage_monitor()

    def _prepare_export_staging_space(
        self, render_settings: RenderSettings, duration_seconds: float,
        layer_count: int, use_streamed_visuals: bool, korean: bool,
        piped_visuals: bool = False,
    ) -> bool:
        return self.export_orchestrator.prepare_staging_space(
            render_settings, duration_seconds, layer_count, use_streamed_visuals, korean,
            piped_visuals=piped_visuals,
        )

    def _export_video(self) -> None:
        self.export_orchestrator.export_video()

    def _minimize_during_export(self) -> None:
        self.export_orchestrator.minimize_during_export()

    def _restore_export_dialog_after_minimize(self) -> None:
        self.export_orchestrator.restore_dialog_after_minimize()

    def changeEvent(self, event: QEvent) -> None:
        """React to major taskbar-window transitions after Qt lays them out."""
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        old_state = (
            event.oldState() if hasattr(event, "oldState")
            else Qt.WindowState.WindowNoState
        )
        new_state = self.windowState()
        self._handle_canvas_window_state_change(old_state, new_state)
        if self._export_restore_pending and not self.isMinimized():
            QTimer.singleShot(0, self._restore_export_dialog_after_minimize)

    def _handle_canvas_window_state_change(
        self, old_state: Qt.WindowState, new_state: Qt.WindowState,
    ) -> None:
        """Fit after maximize/restore without resetting zoom on ordinary resizes."""
        was_minimized = bool(old_state & Qt.WindowState.WindowMinimized)
        is_minimized = bool(new_state & Qt.WindowState.WindowMinimized)
        maximize_changed = bool(
            old_state & Qt.WindowState.WindowMaximized
        ) != bool(new_state & Qt.WindowState.WindowMaximized)
        fullscreen_changed = bool(
            old_state & Qt.WindowState.WindowFullScreen
        ) != bool(new_state & Qt.WindowState.WindowFullScreen)
        if is_minimized:
            self._canvas_fit_pending = True
            self._canvas_fit_timer.stop()
            return
        if was_minimized or maximize_changed or fullscreen_changed:
            self._schedule_canvas_fit(100)

    def _schedule_canvas_fit(self, delay_ms: int = 0) -> None:
        """Coalesce layout-driven fit requests until the Canvas is usable."""
        self._canvas_fit_pending = True
        if self.isMinimized():
            self._canvas_fit_timer.stop()
            return
        self._canvas_fit_timer.start(max(0, int(delay_ms)))

    def _apply_scheduled_canvas_fit(self) -> None:
        """Apply one pending fit without making the project look edited."""
        if not self._canvas_fit_pending:
            return
        if self.isMinimized() or self._inline_preview is not None:
            return
        self._canvas_fit_pending = False
        with self.project_session.suspend_changes():
            self.canvas.fit_artboard()

    def _bottom_workspace_tab_changed(self, index: int) -> None:
        self.preview_controller.bottom_workspace_tab_changed(index)

    def _finish_inline_preview(self, _result: int = 0) -> None:
        self.preview_controller.finish_inline_preview(_result)

    def _preview_source_animation(self, source_id: str) -> None:
        self.preview_controller.preview_source_animation(source_id)

    def _arm_animation_preview_cancel(self) -> None:
        self.preview_controller.arm_animation_preview_cancel()

    def _cancel_animation_preview(self, *_args: object) -> None:
        self.preview_controller.cancel_animation_preview(*_args)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        self.preview_controller.handle_viewport_event(event)
        return super().eventFilter(watched, event)

    def _finish_canvas_animation_preview(self) -> None:
        self.preview_controller.finish_canvas_animation_preview()

    @staticmethod
    def _playlist_duration(tracks: list) -> float:
        return ExportOrchestrator.playlist_duration(tracks)

    def _export_upscale_warnings(
        self, active_tracks: list, render_settings: RenderSettings,
        render_scale: float, korean: bool,
    ) -> list[str]:
        return self.export_orchestrator.upscale_warnings(
            active_tracks, render_settings, render_scale, korean,
        )

    def _export_visualizers(
        self, tracks: list | None = None, render_scale: float = 1.0,
    ) -> list[VisualizerOverlay]:
        return self.export_orchestrator.visualizers(tracks, render_scale)

    @staticmethod
    def _export_layer_worker_count(
        render_settings: RenderSettings, stream_count: int,
    ) -> int:
        return ExportOrchestrator.layer_worker_count(render_settings, stream_count)

    @staticmethod
    def _export_png_queue_capacity(render_settings: RenderSettings) -> int:
        return ExportOrchestrator.png_queue_capacity(render_settings)

    def _export_video_clips(
        self, tracks: list, playlist_duration: float,
        work_mode: str = WORK_MODE_AUTO, render_scale: float = 1.0,
    ) -> list[VideoClipOverlay]:
        return self.export_orchestrator.video_clips(
            tracks, playlist_duration, work_mode, render_scale,
        )

    def _show_shortcuts(self) -> None:
        """Open the dedicated, grouped keyboard shortcut reference."""
        ShortcutsDialog(self.translator, self).exec()

    def _show_help(self) -> None:
        """Open the offline user guide on the tab (and topic) for what is in use."""
        tab, topic = self._help_context()
        HelpDialog(self.translator, self, tab=tab, topic=topic).exec()

    def _help_context(self) -> tuple[str, str | None]:
        """Preview while it plays; otherwise Canvas, on the focused panel's topic."""
        if self._inline_preview is not None:
            return "preview", "full_preview"
        focus = QApplication.focusWidget()
        panels = (
            (getattr(self, "playlist_editor", None), "playlist"),
            (getattr(self, "timeline_panel", None), "timeline"),
            (getattr(self, "layer_panel", None), "layers"),
            (getattr(self, "content_library_panel", None), "project_content"),
            (getattr(self, "source_sidebar", None), "sources"),
            (getattr(self, "inspector", None), "properties"),
            (getattr(self, "canvas", None), "canvas_editing"),
        )
        for panel, topic in panels:
            if isinstance(panel, QWidget) and focus is not None and (
                    focus is panel or panel.isAncestorOf(focus)):
                return "canvas", topic
        return "canvas", "workspace"

    def _show_about(self) -> None:
        """Open program identity, runtime details, and support diagnostics."""
        ffmpeg_path: Path | None = None
        try:
            ffmpeg_path = FFmpegRenderer(
                self.settings_service.current.ffmpeg_path or None
            ).executable
        except FFmpegNotFoundError:
            pass
        AboutDialog(self.translator, ffmpeg_path, log_directory(), self).exec()

    def schedule_automatic_update_check(self) -> None:
        """Check once after startup without delaying project selection or first paint."""
        QTimer.singleShot(1200, self, lambda: self._check_for_updates(manual=False))

    def _check_for_updates(self, manual: bool, reinstall: bool = False) -> None:
        """Request the latest stable GitHub Release on a background thread.

        ``reinstall`` (Settings → Maintenance) downloads its Setup whatever its version.
        """
        if self._pending_update_installer is not None or self._update_install_authorized:
            return
        if self._update_check_worker and self._update_check_worker.isRunning():
            if manual:
                self.statusBar().showMessage(
                    "이미 업데이트를 확인하고 있습니다."
                    if self.translator.is_korean else
                    "An update check is already in progress.",
                    4000,
                )
            return
        if self._update_download_worker and self._update_download_worker.isRunning():
            return
        self._update_check_manual = manual
        self._update_check_reinstall = reinstall
        self.check_updates_action.setEnabled(False)
        if manual:
            self.statusBar().showMessage(
                "GitHub에서 최신 버전을 확인하는 중입니다…"
                if self.translator.is_korean else
                "Checking GitHub for the latest version…"
            )
        worker = UpdateCheckWorker(self._update_service)
        self._update_check_worker = worker
        worker.release_found.connect(self._update_release_found)
        worker.failed.connect(self._update_check_failed)
        worker.finished.connect(self._update_check_finished)
        korean = self.translator.is_korean
        self.activity_progress.begin(
            "update_check",
            "업데이트 확인" if korean else "Checking for updates",
            detail="GitHub 릴리즈 확인 중" if korean else "Checking GitHub releases",
        )
        worker.start()

    def _update_release_found(self, release: ReleaseInfo) -> None:
        """Compare versions, honor automatic dismissal, and show release notes."""
        if self._close_after_update_check:
            return  # the user is closing; don't pop release notes on the way out
        manual = self._update_check_manual
        korean = self.translator.is_korean
        if self._update_check_reinstall:
            if release.can_install:
                self._start_update_download(release)
            else:
                QMessageBox.warning(
                    self,
                    "강제 재설치" if korean else "Force reinstall",
                    "최신 릴리즈에 검증된 Setup 파일이 없어 재설치할 수 없습니다."
                    if korean else
                    "The latest release has no verified Setup file, so it cannot be reinstalled.",
                )
            return
        try:
            release_version = normalized_version(release.version)
            current_version = normalized_version(__version__)
        except Exception as error:
            self._update_check_failed(str(error))
            return
        if current_version > release_version:
            QMessageBox.warning(
                self,
                "버전 확인 경고" if korean else "Version check warning",
                (
                    f"현재 프로그램 버전({__version__})이 GitHub 최신 릴리즈"
                    f"({release.version})보다 높습니다.\n"
                    "개발 또는 아직 공개되지 않은 빌드일 수 있으며 업데이트는 실행하지 않습니다."
                )
                if korean else
                (
                    f"The installed version ({__version__}) is newer than the latest "
                    f"GitHub release ({release.version}).\n"
                    "This may be a development or not-yet-published build. No update will run."
                ),
            )
            return
        if current_version == release_version:
            if manual:
                UpdateAvailableDialog(
                    release, __version__, korean, False, self, up_to_date=True,
                ).exec()
            return

        settings = QSettings()
        skipped = str(settings.value("updates/skipped_release_tag", "") or "")
        if not manual and skipped == release.tag_name:
            return
        dialog = UpdateAvailableDialog(release, __version__, korean, not manual, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            if not manual:
                settings.setValue("updates/skipped_release_tag", release.tag_name)
                settings.sync()
            return
        settings.remove("updates/skipped_release_tag")
        settings.sync()
        self._start_update_download(release)

    def _update_check_failed(self, message: str) -> None:
        """Keep automatic network failures quiet but explain manual failures."""
        LOGGER.warning("Update check failed: %s", message)
        if self._update_check_manual and not self._close_after_update_check:
            korean = self.translator.is_korean
            QMessageBox.warning(
                self,
                "업데이트 확인 실패" if korean else "Update check failed",
                f"최신 릴리즈를 확인하지 못했습니다.\n\n{message}"
                if korean else f"Could not check the latest release.\n\n{message}",
            )

    def _update_check_finished(self) -> None:
        self.activity_progress.finish("update_check")
        self.check_updates_action.setEnabled(True)
        self.statusBar().clearMessage()
        if self._update_check_worker:
            self._update_check_worker.deleteLater()
        self._update_check_worker = None
        if self._close_after_update_check:
            self._close_after_update_check = False
            QTimer.singleShot(0, self.close)

    def _start_update_download(self, release: ReleaseInfo) -> None:
        """Download the selected GitHub Setup into the per-user update directory."""
        if not release.can_install:
            return
        data_root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        target = Path(data_root or Path.cwd() / ".app-data") / "updates"
        korean = self.translator.is_korean
        dialog = UpdateDownloadDialog(release, korean, self)
        worker = UpdateDownloadWorker(self._update_service, release, target)
        self._update_download_dialog = dialog
        self._update_download_worker = worker
        self._downloaded_update_path = None
        worker.progress.connect(dialog.update_progress)
        worker.progress.connect(
            lambda fraction, message: self.activity_progress.update(
                "update_download", fraction, message,
            )
        )
        worker.succeeded.connect(self._update_download_succeeded)
        worker.failed.connect(self._update_download_failed)
        worker.cancelled.connect(self._update_download_cancelled)
        worker.finished.connect(self._update_download_finished)
        dialog.cancel_requested.connect(worker.cancel)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        QApplication.processEvents()
        self.activity_progress.begin(
            "update_download",
            "업데이트 다운로드" if korean else "Downloading update",
            detail=release.version,
        )
        worker.start()

    def _update_download_succeeded(self, path: Path) -> None:
        self._downloaded_update_path = Path(path)
        if self._update_download_dialog:
            self._update_download_dialog.complete(True)
            self._update_download_dialog = None

    def _update_download_failed(self, message: str) -> None:
        LOGGER.error("Update download failed: %s", message)
        if self._update_download_dialog:
            self._update_download_dialog.complete(False)
            self._update_download_dialog = None
        korean = self.translator.is_korean
        QMessageBox.critical(
            self,
            "업데이트 다운로드 실패" if korean else "Update download failed",
            f"Setup을 안전하게 다운로드하지 못했습니다.\n\n{message}"
            if korean else f"The Setup file could not be downloaded safely.\n\n{message}",
        )

    def _update_download_cancelled(self) -> None:
        if self._update_download_dialog:
            self._update_download_dialog.complete(False)
            self._update_download_dialog = None
        self.statusBar().showMessage(
            "업데이트 다운로드를 취소했습니다."
            if self.translator.is_korean else
            "Update download cancelled.",
            5000,
        )

    def _update_download_finished(self) -> None:
        self.activity_progress.finish("update_download")
        path = self._downloaded_update_path
        self._downloaded_update_path = None
        if self._update_download_worker:
            self._update_download_worker.deleteLater()
        self._update_download_worker = None
        if path is not None:
            QTimer.singleShot(0, lambda installer=path: self._launch_update_setup(installer))

    def _launch_update_setup(self, installer: Path) -> None:
        """Back up the workspace, install unattended after shutdown, and resume it."""
        korean = self.translator.is_korean
        self._pending_update_installer = installer
        busy = self.project_controller.saving or self._export_preparation_cancel is not None or any(
            worker is not None and worker.isRunning() for worker in (
                self._render_worker, self._ffmpeg_install_worker, self._ffmpeg_catalog_worker,
                self._update_check_worker, self._update_download_worker,
            )
        )
        if busy:
            self.statusBar().showMessage(
                "현재 작업이 끝나면 자동으로 업데이트합니다."
                if korean else "The update will install automatically after the current operation finishes."
            )
            QTimer.singleShot(500, self, lambda: self._launch_update_setup(installer))
            return
        self._pending_update_installer = None
        if not installer.is_file():
            QMessageBox.critical(
                self,
                "업데이트 오류" if korean else "Update error",
                "다운로드한 Setup 파일을 찾을 수 없습니다."
                if korean else "The downloaded Setup file could not be found.",
            )
            return
        if self._animation_preview_active:
            self.animation_preview_controller.cancel()
        try:
            # Wait for older autosaves so they cannot overwrite the final handoff.
            self.autosave_controller.wait_for_pending()
            snapshot = self.autosave.save(
                self._project_document(), self.current_project_path, dirty=self.project_session.dirty,
            )
        except ProjectError as error:
            self._show_project_error(error)
            return
        arguments = automatic_setup_arguments(
            installer, snapshot.path, os.getpid(), korean=korean,
        )
        ready_path = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("/UPDATEREADY=")))
        started = launch_setup(installer, arguments)
        if not started:
            QMessageBox.critical(
                self,
                "업데이트 실행 실패" if korean else "Could not start update",
                "Setup 파일을 실행하지 못했습니다. 다운로드 폴더에서 직접 실행해 주세요."
                if korean else
                "Setup could not be started. Run it manually from the update folder.",
            )
            return
        self._pending_update_installer = installer
        self.statusBar().showMessage(
            "설치 프로그램의 권한 승인을 기다리는 중입니다. 취소하면 편집기를 계속 사용할 수 있습니다."
            if korean else "Waiting for Setup authorization. Cancel to keep using the editor."
        )
        self._wait_for_update_setup(ready_path, snapshot.path, 180)

    def _wait_for_update_setup(self, ready_path: Path, recovery_path: Path, attempts: int) -> None:
        """Only shut down after the elevated Setup confirms it can install."""
        busy = self.project_controller.saving or self._export_preparation_cancel is not None or any(
            worker is not None and worker.isRunning() for worker in (
                self._render_worker, self._ffmpeg_install_worker, self._ffmpeg_catalog_worker,
                self._update_check_worker, self._update_download_worker,
            )
        )
        if ready_path.is_file() and not busy:
            try:
                self.autosave_controller.wait_for_pending()
                # The user may have kept editing while the UAC prompt was open.
                snapshot = self.autosave.save(self._project_document(), self.current_project_path, dirty=self.project_session.dirty)
                if snapshot.path != recovery_path:
                    snapshot.path.replace(recovery_path)
                ready_path.unlink(missing_ok=True)
            except (ProjectError, OSError) as error:
                self._pending_update_installer = None
                self._show_project_error(ProjectError(str(error)))
                return
            self._pending_update_installer = None
            self._update_install_authorized = True
            self.close()
            return
        if attempts <= 0:
            self._pending_update_installer = None
            self.statusBar().showMessage(
                "업데이트가 시작되지 않았습니다. 편집을 계속하거나 업데이트를 다시 실행하세요."
                if self.translator.is_korean else "Setup did not start. Continue editing or retry the update.", 7000,
            )
            return
        QTimer.singleShot(500, self, lambda: self._wait_for_update_setup(ready_path, recovery_path, attempts - 1))

    def _reset_program(self) -> None:
        """Close normally (unsaved work is asked about); closeEvent then resets and restarts."""
        busy = self._export_preparation_cancel is not None or any(
            worker is not None and worker.isRunning() for worker in (
                self._render_worker, self._ffmpeg_install_worker, self._ffmpeg_catalog_worker,
                self._update_check_worker, self._update_download_worker,
            )
        )
        if busy:
            korean = self.translator.is_korean
            QMessageBox.warning(
                self,
                "프로그램 초기화" if korean else "Reset program",
                "진행 중인 작업(내보내기·다운로드 등)이 끝난 뒤 다시 시도해 주세요."
                if korean else
                "Try again once the running task (export, download, …) has finished.",
            )
            return
        self._reset_after_close = True
        if not self.close():
            self._reset_after_close = False

    def check_ffmpeg_availability(self) -> bool:
        """Explain unavailable features and offer the existing installer settings."""
        try:
            FFmpegRenderer.find_executable(self.settings_service.current.ffmpeg_path or None)
        except FFmpegNotFoundError:
            if self._ffmpeg_notice is None:
                korean = self.translator.is_korean
                notice = QMessageBox(self)
                notice.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                notice.setIcon(QMessageBox.Icon.Warning)
                notice.setWindowTitle("FFmpeg 설치 필요" if korean else "FFmpeg required")
                notice.setText("FFmpeg를 찾을 수 없습니다." if korean else "FFmpeg could not be found.")
                notice.setInformativeText(
                    "음악에 맞춰 반응, 오디오 시각화, 믹스 미리보기와 영상 내보내기에는 FFmpeg가 필요합니다. "
                    "실시간 가사 표시는 사용할 수 있습니다.\n\n설정 → FFmpeg에서 설치하거나 기존 실행 파일을 지정해 주세요."
                    if korean else
                    "Music reactions, audio visualizers, mix preview and video export require FFmpeg. "
                    "Real-time lyric display remains available.\n\nInstall FFmpeg or select an existing executable in Settings → FFmpeg."
                )
                notice.setStandardButtons(QMessageBox.StandardButton.Close)
                install = notice.addButton("설치 설정 열기" if korean else "Open install settings", QMessageBox.ButtonRole.AcceptRole)
                notice.buttonClicked.connect(lambda button: self._show_settings(True) if button is install else None)
                notice.finished.connect(lambda _result: setattr(self, "_ffmpeg_notice", None))
                self._ffmpeg_notice = notice
                notice.show()
            return False
        return True

    def _show_settings(self, focus_ffmpeg: bool = False) -> None:
        """Show and persist the Phase 4A application settings."""
        dialog = SettingsDialog(
            self.settings_service.current,
            self.translator.language,
            self.theme_service.preference,
            self.translator,
            self,
            active_preview_backend=self._preview_backend_for_session,
        )
        self._settings_dialog = dialog
        dialog.catalog_requested.connect(
            lambda force=False: self._load_ffmpeg_catalog(dialog, force=force)
        )
        dialog.download_requested.connect(
            lambda: self._start_ffmpeg_install(dialog)
        )
        dialog.update_requested.connect(
            lambda: self._start_ffmpeg_update(dialog)
        )
        dialog.reinstall_requested.connect(
            lambda: self._start_ffmpeg_reinstall(dialog)
        )
        dialog.delete_requested.connect(
            lambda: self._delete_managed_ffmpeg(dialog)
        )
        if focus_ffmpeg:
            dialog.open_ffmpeg_page()
        if dialog.exec() != dialog.DialogCode.Accepted:
            self._settings_dialog = None
            if dialog.maintenance_action == "reinstall":
                self._check_for_updates(manual=True, reinstall=True)
            elif dialog.maintenance_action == "reset":
                self._reset_program()
            return
        selected_settings = dialog.app_settings
        self.settings_service.save(selected_settings)
        self.theme_service.set_preference(dialog.selected_theme)
        self.translator.set_language(dialog.selected_language)
        self._settings_dialog = None
        korean = self.translator.is_korean
        renderer_changed = (
            selected_settings.preview_backend != self._preview_backend_for_session
        )
        self.statusBar().showMessage(
            (
                "설정을 저장했습니다. 미리보기 렌더러는 프로그램 재시작 후 적용됩니다."
                if korean else
                "Settings saved. The preview renderer takes effect after restarting the program."
            )
            if renderer_changed else
            ("설정을 저장했습니다." if korean else "Settings saved."),
            7000 if renderer_changed else 4000,
        )

    def _load_ffmpeg_catalog(
        self, settings_dialog: SettingsDialog, *, force: bool = False,
    ) -> None:
        """Fetch selectable versions and managed state away from the UI thread."""
        if force:
            self._ffmpeg_catalog_cache = None
        if self._ffmpeg_catalog_cache is not None:
            releases, current = self._ffmpeg_catalog_cache
            settings_dialog.set_managed_installation(current)
            settings_dialog.set_ffmpeg_catalog(releases)
            return
        if self._ffmpeg_catalog_worker and self._ffmpeg_catalog_worker.isRunning():
            return
        worker = FFmpegCatalogWorker(ManagedFFmpegInstaller())
        self._ffmpeg_catalog_worker = worker
        worker.succeeded.connect(self._ffmpeg_catalog_succeeded)
        worker.failed.connect(self._ffmpeg_catalog_failed)
        worker.finished.connect(self._release_ffmpeg_catalog_worker)
        worker.start()

    def _ffmpeg_catalog_succeeded(
        self, payload: object,
    ) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        releases, current = payload
        if not isinstance(releases, list) or not all(
            isinstance(release, FFmpegReleaseOption) for release in releases
        ):
            return
        installation = (
            current if isinstance(current, ManagedFFmpegInstallation) else None
        )
        self._ffmpeg_catalog_cache = (releases, installation)
        if self._settings_dialog is not None:
            self._settings_dialog.set_managed_installation(installation)
            self._settings_dialog.set_ffmpeg_catalog(releases)

    def _ffmpeg_catalog_failed(
        self, message: str, current: object,
    ) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.set_managed_installation(
                current if isinstance(current, ManagedFFmpegInstallation) else None
            )
            self._settings_dialog.set_ffmpeg_catalog_error(message)

    def _release_ffmpeg_catalog_worker(self) -> None:
        if self._ffmpeg_catalog_worker:
            self._ffmpeg_catalog_worker.deleteLater()
        self._ffmpeg_catalog_worker = None
        if self._close_after_ffmpeg_catalog_cancel:
            self._close_after_ffmpeg_catalog_cancel = False
            QTimer.singleShot(0, self.close)

    def _start_ffmpeg_install(
        self, settings_dialog: SettingsDialog,
        release: FFmpegReleaseOption | None = None, *, force: bool = False,
    ) -> None:
        """Install the selected checksum-verified build after required consent."""
        if self._ffmpeg_install_worker and self._ffmpeg_install_worker.isRunning():
            return
        release = release or settings_dialog.selected_ffmpeg_release
        korean = self.translator.is_korean
        if release is None:
            QMessageBox.warning(
                settings_dialog,
                "FFmpeg 버전 확인" if korean else "Select an FFmpeg version",
                "설치할 버전을 먼저 선택해 주세요."
                if korean else "Select a version to install first.",
            )
            return
        if not release.recommended and not self._confirm_ffmpeg_release(
            settings_dialog, release,
            "재설치" if korean and force else "설치" if korean else
            "Reinstall" if force else "Install",
        ):
            return
        settings_dialog.set_ffmpeg_installing(True)
        progress = FFmpegInstallProgressDialog(korean, settings_dialog)
        progress.set_busy(
            "Preparing download",
            f"GitHub에서 FFmpeg {release.series} ({release.build}) 설치를 준비하는 중입니다."
            if korean else
            f"Preparing FFmpeg {release.series} ({release.build}) from GitHub.",
        )
        worker = FFmpegInstallWorker(
            ManagedFFmpegInstaller(), release, force=force,
        )
        self._ffmpeg_install_dialog = progress
        self._ffmpeg_install_worker = worker
        worker.progress.connect(progress.update_progress)
        worker.progress.connect(
            lambda stage, fraction, message: self.activity_progress.update(
                "ffmpeg_install", fraction, f"{stage} · {message}",
            )
        )
        worker.succeeded.connect(self._ffmpeg_install_succeeded)
        worker.failed.connect(self._ffmpeg_install_failed)
        worker.cancelled.connect(self._ffmpeg_install_cancelled)
        worker.finished.connect(self._ffmpeg_install_finished)
        progress.cancel_requested.connect(worker.cancel)
        # Show the application-modal child before starting the worker so every
        # other window is disabled and the first network request is visible.
        # QDialog.open() forces WindowModal for parented dialogs. show() keeps
        # the explicit ApplicationModal setting from the installer dialog.
        progress.show()
        progress.raise_()
        progress.activateWindow()
        QApplication.processEvents()
        self.activity_progress.begin(
            "ffmpeg_install",
            (f"FFmpeg {release.series} 재설치" if force else
             f"FFmpeg {release.series} 다운로드 및 설치") if korean else
            (f"Reinstalling FFmpeg {release.series}" if force else
             f"Downloading and installing FFmpeg {release.series}"),
            detail=(f"선택한 빌드: {release.build}" if korean else
                    f"Selected build: {release.build}"),
        )
        worker.start()

    def _confirm_ffmpeg_release(
        self, parent: QWidget, release: FFmpegReleaseOption, operation: str,
    ) -> bool:
        """Warn for non-recommended or destructive version operations with notes."""
        korean = self.translator.is_korean
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(
            "비권장 FFmpeg 버전" if korean else "Non-recommended FFmpeg version"
        )
        box.setText(
            f"FFmpeg {release.series}은(는) 이 Playlist Canvas 패치의 권장 버전이 아닙니다."
            if korean else
            f"FFmpeg {release.series} is not recommended for this Playlist Canvas patch."
        )
        excerpt = release.notes[:1000]
        box.setInformativeText(
            f"호환성이나 내보내기 결과가 달라질 수 있습니다.\n\n패치/배포 내용:\n{excerpt}\n\n{operation}을 계속할까요?"
            if korean else
            f"Compatibility or export output may differ.\n\nPatch/release notes:\n{excerpt}\n\nContinue with {operation.lower()}?"
        )
        box.setDetailedText(release.notes)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _start_ffmpeg_update(self, settings_dialog: SettingsDialog) -> None:
        release = settings_dialog.recommended_ffmpeg_release
        if release is not None:
            # Updating always targets the app-tested version, so it intentionally
            # skips the non-recommended warning.
            self._start_ffmpeg_install(settings_dialog, release)

    def _start_ffmpeg_reinstall(self, settings_dialog: SettingsDialog) -> None:
        current = settings_dialog.managed_installation
        release = next((
            entry for entry in settings_dialog.ffmpeg_releases
            if current is not None and entry.series == current.series
        ), None)
        if current is None or release is None:
            return
        korean = self.translator.is_korean
        response = QMessageBox.question(
            settings_dialog,
            "FFmpeg 재설치" if korean else "Reinstall FFmpeg",
            (f"현재 관리 중인 FFmpeg {current.series or current.version}을(를) "
             "다시 다운로드하고 검증한 뒤 교체할까요?")
            if korean else
            (f"Download, verify, and replace the managed FFmpeg "
             f"{current.series or current.version}?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response == QMessageBox.StandardButton.Yes:
            self._start_ffmpeg_install(settings_dialog, release, force=True)

    def _delete_managed_ffmpeg(self, settings_dialog: SettingsDialog) -> None:
        current = settings_dialog.managed_installation
        if current is None:
            return
        korean = self.translator.is_korean
        response = QMessageBox.warning(
            settings_dialog,
            "FFmpeg 삭제" if korean else "Delete FFmpeg",
            "앱에서 관리하는 현재 FFmpeg를 삭제합니다. 삭제 후에는 다시 설치하기 전까지 영상을 내보낼 수 없습니다. 계속할까요?"
            if korean else
            "Delete the currently app-managed FFmpeg. Video export will be unavailable until it is installed again. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = ManagedFFmpegInstaller().uninstall_current()
        except FFmpegInstallError as error:
            QMessageBox.critical(
                settings_dialog,
                "FFmpeg 삭제 오류" if korean else "Could not delete FFmpeg",
                str(error),
            )
            return
        if removed is None:
            return
        configured_text = self.settings_service.current.ffmpeg_path.strip()
        removed_was_active = False
        if configured_text:
            try:
                removed_was_active = (
                    Path(configured_text).resolve(strict=False)
                    == removed.executable.resolve(strict=False)
                )
            except OSError:
                removed_was_active = Path(configured_text) == removed.executable
        if removed_was_active:
            settings_dialog.ffmpeg_edit.clear()
            self.settings_service.save(replace(
                self.settings_service.current, ffmpeg_path=""
            ))
        settings_dialog.set_managed_installation(None)
        self._ffmpeg_catalog_cache = None
        QMessageBox.information(
            settings_dialog,
            "FFmpeg 삭제 완료" if korean else "FFmpeg deleted",
            "관리 설치본을 삭제했습니다. 필요하면 권장 버전을 다시 설치할 수 있습니다."
            if korean else
            "The managed installation was deleted. You can reinstall the recommended version when needed.",
        )

    def _ffmpeg_install_succeeded(self, installation: ManagedFFmpegInstallation) -> None:
        """Persist the verified managed executable as the active FFmpeg path."""
        if self._ffmpeg_install_dialog:
            self._ffmpeg_install_dialog.complete(True)
            self._ffmpeg_install_dialog = None
        self.settings_service.save(replace(
            self.settings_service.current, ffmpeg_path=str(installation.executable)
        ))
        if self._settings_dialog:
            self._settings_dialog.ffmpeg_edit.setText(str(installation.executable))
            self._settings_dialog.set_managed_installation(installation)
            self._settings_dialog.set_ffmpeg_installing(False)
            releases = list(self._settings_dialog.ffmpeg_releases)
            self._ffmpeg_catalog_cache = (
                (releases, installation) if releases else None
            )
        korean = self.translator.is_korean
        message = (
            f"FFmpeg {installation.version} 설치 및 검증 완료"
            if korean else f"FFmpeg {installation.version} installed and verified"
        )
        self.statusBar().showMessage(message, 7000)
        QMessageBox.information(
            self._settings_dialog or self,
            "FFmpeg 설치 완료" if korean else "FFmpeg installed", message
        )

    def _ffmpeg_install_failed(self, message: str) -> None:
        """Report a failed installation while retaining every prior installation intact."""
        LOGGER.error("Managed FFmpeg installation failed: %s", message)
        if self._ffmpeg_install_dialog:
            self._ffmpeg_install_dialog.complete(False)
            self._ffmpeg_install_dialog = None
        if self._settings_dialog:
            self._settings_dialog.set_ffmpeg_install_error(message)
        korean = self.translator.is_korean
        QMessageBox.warning(
            self._settings_dialog or self,
            "FFmpeg 설치 오류" if korean else "FFmpeg installation error", message
        )

    def _ffmpeg_install_cancelled(self) -> None:
        """Close the install UI after the worker has removed temporary download data."""
        if self._ffmpeg_install_dialog:
            self._ffmpeg_install_dialog.complete(False)
            self._ffmpeg_install_dialog = None
        if self._settings_dialog:
            self._settings_dialog.set_ffmpeg_installing(False)
        self.statusBar().showMessage(
            "FFmpeg 설치를 취소했습니다."
            if self.translator.is_korean else "FFmpeg installation cancelled.",
            5000,
        )

    def _ffmpeg_install_finished(self) -> None:
        """Release completed installer resources after queued outcome signals are handled."""
        self.activity_progress.finish("ffmpeg_install")
        QTimer.singleShot(0, self._release_ffmpeg_install_worker)

    def _release_ffmpeg_install_worker(self) -> None:
        if self._ffmpeg_install_worker:
            self._ffmpeg_install_worker.deleteLater()
        self._ffmpeg_install_worker = None

    def _export_playlist_files(self) -> None:
        """Create companion timestamps using the same timing as the selected mix."""
        default_directory = (
            self.current_project_path.parent
            if self.current_project_path is not None
            else Path.cwd()
        )
        dialog = PlaylistExportDialog(default_directory, self.translator, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        korean = self.translator.is_korean
        existing = [
            path.name for path in (
                dialog.output_directory / "description.txt",
                dialog.output_directory / "playlist.csv",
            ) if path.exists()
        ]
        overwrite = False
        if existing:
            answer = QMessageBox.question(
                self,
                "파일 덮어쓰기" if korean else "Overwrite files",
                (f"이미 존재하는 파일을 덮어쓸까요?\n{', '.join(existing)}")
                if korean else
                f"Overwrite the existing files?\n{', '.join(existing)}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        try:
            compiled_plan = None
            if self.project_settings.transition_mode != "none":
                from PySide6.QtWidgets import QProgressDialog
                from app.controllers.preview_audio_controller import prepare_audio_for_ui
                from app.renderer.ffmpeg_renderer import RenderCancelledError

                tracks = [track for track in self.playlist_service.tracks if track.enabled]
                if not tracks:
                    raise PlaylistExportError("Select at least one playlist track before creating files.")
                renderer = FFmpegRenderer(self.settings_service.current.ffmpeg_path or None)
                cancel = threading.Event()
                progress = QProgressDialog(
                    "믹스 시간 확인 중…" if korean else "Preparing mix timestamps…",
                    "취소" if korean else "Cancel", 0, 0, self,
                )
                progress.setWindowModality(Qt.WindowModality.WindowModal)
                progress.canceled.connect(cancel.set)
                self._export_preparation_cancel = cancel
                progress.show()
                self.activity_progress.begin(
                    "playlist_files", "타임라인 내보내기" if korean else "Exporting timeline",
                    detail="믹스 시간 확인 중" if korean else "Preparing mix timestamps",
                )

                def report(_stage: str, fraction: float, message: str) -> None:
                    progress.setLabelText(message)
                    self.activity_progress.update("playlist_files", fraction, detail=message or None)

                try:
                    with TemporaryDirectory(prefix="playlist-timestamps-", ignore_cleanup_errors=True) as directory:
                        _, compiled_plan = prepare_audio_for_ui(
                            renderer, tracks, Path(directory), self.project_settings.transition_mode,
                            self.project_settings.crossfade_seconds, RenderSettings(), cancel, report,
                            automix_settings=automix_settings_for(self.project_settings),
                        )
                except RenderCancelledError:
                    return
                finally:
                    self.activity_progress.finish("playlist_files")
                    progress.close()
                    progress.deleteLater()
                    self._export_preparation_cancel = None
                    self._resume_close_after_export_cancel()
            result = self.playlist_export_service.export(
                self.playlist_service.tracks,
                dialog.output_directory,
                dialog.timestamp_format,
                overwrite=overwrite,
                **({"compiled_plan": compiled_plan} if compiled_plan is not None else {}),
            )
        except (PlaylistExportError, RenderError) as error:
            QMessageBox.warning(
                self,
                "타임라인 내보내기 오류" if korean else "Timeline export error",
                str(error),
            )
            return
        if dialog.copy_description:
            QApplication.clipboard().setText(result.description_text)
        message = (
            f"description.txt와 playlist.csv를 만들었습니다.\n{result.description_path.parent}"
            if korean else
            f"Created description.txt and playlist.csv.\n{result.description_path.parent}"
        )
        self.statusBar().showMessage(message.replace("\n", " "), 7000)
        QMessageBox.information(
            self,
            "타임라인 내보내기 완료" if korean else "Timeline export complete",
            message,
        )

    def _export_m3u_playlist(self) -> None:
        """Save the enabled tracks, in Playlist order, as an M3U8 file."""
        korean = self.translator.is_korean
        tracks = [track for track in self.playlist_service.tracks if track.enabled]
        if not tracks:
            QMessageBox.information(
                self,
                "M3U8 플레이리스트 내보내기" if korean else "Export M3U8 playlist",
                "내보낼 곡이 없습니다. 플레이리스트에 곡을 추가하거나 사용으로 켜 주세요."
                if korean else "There are no songs to export. Add songs or enable them in the Playlist.",
            )
            return
        project = self.current_project_path
        folder = project.parent if project is not None else Path.home()
        name = project.name.split(".")[0] if project is not None else "playlist"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "M3U8 플레이리스트 내보내기" if korean else "Export M3U8 playlist",
            str(folder / f"{name}.m3u8"),
            ("M3U8 플레이리스트" if korean else "M3U8 playlist") + " (*.m3u8)",
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".m3u8":
            target = target.with_name(target.name + ".m3u8")
        try:
            count = write_m3u8(tracks, target)
        except M3uPlaylistError as error:
            QMessageBox.warning(
                self, "M3U8 내보내기 오류" if korean else "M3U8 export error", str(error),
            )
            return
        self.statusBar().showMessage(
            f"M3U8 플레이리스트에 {count}곡을 저장했습니다 · {target}"
            if korean else f"Saved {count} song(s) to the M3U8 playlist · {target}",
            7000,
        )

    def _export_cancelled(self) -> None:
        self.export_orchestrator.cancelled()

    def _export_failed(self, message: str) -> None:
        self.export_orchestrator.failed(message)

    def _export_succeeded(self, result: RenderResult) -> None:
        self.export_orchestrator.succeeded(result)

    def _export_finished(self) -> None:
        self.export_orchestrator.finished()

    def _resume_close_after_export_cancel(self) -> None:
        self.export_orchestrator.resume_close_after_cancel()

    def _release_render_worker(self) -> None:
        self.export_orchestrator.release_render_worker()

    def _choose_preset(self) -> None:
        """Select and apply a complete visual canvas preset to this project."""
        dialog = DesignPresetDialog(self.translator, self)
        if not dialog.exec() or dialog.selected_preset is None:
            return
        preset = dialog.selected_preset
        korean = self.translator.is_korean
        answer = QMessageBox.warning(
            self,
            "프리셋 적용 경고" if korean else "Apply preset warning",
            (
                f"'{preset.name('ko')}' 프리셋을 적용하면 현재 캔버스의 모든 요소가 "
                "프리셋 요소로 교체됩니다.\n\n재생목록은 유지되며, 적용 후 Ctrl+Z로 되돌릴 수 있습니다.\n"
                "계속 적용할까요?"
                if korean else
                f"Applying '{preset.name('en')}' will replace every current Canvas "
                "source with the preset sources.\n\nThe playlist will be preserved, and "
                "you can undo afterward with Ctrl+Z.\nApply this preset?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._apply_preset(preset)

    def _apply_preset(self, preset: PresetDefinition) -> None:
        """Replace canvas sources with a preset while preserving the playlist."""
        artboard = self.canvas.scene_model.artboard_rect
        self.store.replace(self._preset_sources_for_canvas(
            preset, artboard.width(), artboard.height(),
        ))
        message = "프리셋을 적용했습니다. Ctrl+Z로 되돌릴 수 있습니다."
        if self.translator.language is not Language.KOREAN:
            message = "Preset applied. Press Ctrl+Z to undo."
        self.statusBar().showMessage(message, 4000)

    def _save_current_as_preset(self) -> None:
        """Save every current canvas source as a reusable user preset."""
        korean = self.translator.is_korean
        sources = self.store.sources()
        if not sources:
            QMessageBox.information(
                self,
                "프리셋으로 저장" if korean else "Save as preset",
                "캔버스에 저장할 요소가 없습니다."
                if korean else "The canvas has no sources to save.",
            )
            return
        name, accepted = QInputDialog.getText(
            self,
            "프리셋으로 저장" if korean else "Save as preset",
            "프리셋 이름" if korean else "Preset name",
        )
        if not accepted or not name.strip():
            return
        artboard = self.canvas.scene_model.artboard_rect
        try:
            preset = UserPresetService.save(
                name, sources, artboard.width(), artboard.height(),
            )
        except (UserPresetError, OSError) as error:
            QMessageBox.warning(
                self,
                "저장 실패" if korean else "Save failed",
                str(error),
            )
            return
        self.statusBar().showMessage(
            f"'{preset.name('ko')}' 프리셋을 저장했습니다. 디자인 프리셋 창에서 다시 사용할 수 있습니다."
            if korean else
            f"Saved preset '{preset.name('en')}'. Reuse it from the Design Presets dialog.",
            5000,
        )

    @staticmethod
    def _preset_sources_for_canvas(
        preset: PresetDefinition, canvas_width: float, canvas_height: float,
    ) -> list[Source]:
        """Adapt a preset's authored layout to the project without changing its ratio."""
        return MainWindow._adapt_sources_to_canvas(
            preset.builder(), preset.source_width, preset.source_height,
            canvas_width, canvas_height,
        )

    @staticmethod
    def _adapt_sources_to_canvas(
        sources: list[Source], source_width: float, source_height: float,
        target_width: float, target_height: float,
    ) -> list[Source]:
        """Scale source sizes uniformly and distribute their centres by canvas ratio."""
        if min(source_width, source_height, target_width, target_height) <= 0:
            return [Source.from_dict(source.to_dict()) for source in sources]
        adapted = [Source.from_dict(source.to_dict()) for source in sources]
        x_ratio = target_width / source_width
        y_ratio = target_height / source_height
        factor = min(x_ratio, y_ratio)
        scalable_fields = (
            "border_radius", "outline_width", "font_size",
            "visualizer_line_width", "track_list_row_spacing",
            "track_list_item_padding", "subtitle_line_spacing",
            "subtitle_previous_blur", "level_meter_gap",
            "particle_min_size", "particle_max_size",
        )
        for source in adapted:
            if source.source_type is SourceType.BACKGROUND:
                source.x = 0.0
                source.y = 0.0
                source.width = target_width
                source.height = target_height
            else:
                center_x = (source.x + source.width / 2.0) * x_ratio
                center_y = (source.y + source.height / 2.0) * y_ratio
                source.width *= factor
                source.height *= factor
                source.x = max(
                    0.0, min(target_width - source.width, center_x - source.width / 2.0)
                )
                source.y = max(
                    0.0, min(target_height - source.height, center_y - source.height / 2.0)
                )
            for field in scalable_fields:
                setattr(source, field, getattr(source, field) * factor)
            for line_style in source.subtitle_line_styles:
                if "font_size" in line_style:
                    line_style["font_size"] = float(line_style["font_size"]) * factor
                if "font_size_offset" in line_style:
                    line_style["font_size_offset"] = (
                        float(line_style["font_size_offset"]) * factor
                    )
            source.shadow.blur_radius *= factor
            source.shadow.offset_x *= factor
            source.shadow.offset_y *= factor
        return adapted

    def _new_project(self, *, confirm_unsaved: bool = True) -> bool:
        return self.project_controller.new_project(confirm_unsaved=confirm_unsaved)

    def _show_project_start_dialog(self) -> bool:
        return self.project_controller.show_project_start_dialog()

    def _project_document(self) -> ProjectDocument:
        return self.project_controller.document()

    def _connect_history(self) -> None:
        """Observe all editable Phase 1/2 state and coalesce snapshot commits."""
        self.store.source_added.connect(lambda _source: self._schedule_history())
        self.store.source_added.connect(lambda _source: self._synchronize_content_library())
        self.store.source_removed.connect(lambda _source_id: self._schedule_history())
        self.store.source_changed.connect(lambda _source: self._schedule_history())
        self.store.source_changed.connect(lambda _source: self._synchronize_content_library())
        self.store.sources_replaced.connect(self._schedule_history)
        self.store.groups_changed.connect(self._schedule_history)
        self.playlist_service.playlist_changed.connect(self._schedule_history)
        self.playlist_service.playlist_changed.connect(self._synchronize_content_library)
        self.project_content_service.changed.connect(self._schedule_history)
        # Canvas zoom is view state, not document state: it must not mark the
        # project unsaved or land on the undo stack. Its value is still captured
        # in _project_document() whenever a real edit or an explicit save runs.
        self.history.changed.connect(self._update_history_actions)
        self.history.changed.connect(
            lambda _can_undo, _can_redo: self.canvas.prune_retired_items(
                self.history.retained_source_ids()
            )
        )

    def _finish_initialization(self) -> None:
        """Set the initial canvas view and establish the first undo snapshot."""
        self.canvas.fit_artboard()
        self.history.reset(self._project_document().to_dict())
        self.project_session.ready = True
        self.project_session.dirty = False
        self._update_project_status()
        self.motion.fade_in(self.centralWidget())

    def _synchronize_content_library(self) -> None:
        """Register newly referenced Inspector and playlist media in the library."""
        self.project_content_service.synchronize(self._project_document())

    def show_startup_dialog(self) -> bool:
        return self.project_controller.show_startup_dialog()

    def _on_theme_changed(self, preference: str, effective: str) -> None:
        """Apply a selected theme and softly transition the refreshed workspace."""
        self.current_theme = preference
        self._apply_style()
        if self._inline_preview is not None:
            self._inline_preview.refresh_theme()
            if (
                self._inline_preview_track_panel is not None
                and self._inline_preview_controls is not None
            ):
                self._inline_preview_track_panel.setStyleSheet(
                    self._inline_preview_controls.styleSheet()
                )
        if self.centralWidget() is not None:
            self.motion.fade_in(self.centralWidget(), 140)

    def _set_sidebar_visible(
        self, visible: bool, *, persist: bool = True, sync_action: bool = True,
        restore_sizes: list[int] | None = None,
    ) -> None:
        """Collapse or reveal the editing sidebar with a short width animation."""
        if persist:
            QSettings().setValue("workspace/left_panel_visible", bool(visible))
        self._panel_transition_serial["left"] += 1
        serial = self._panel_transition_serial["left"]
        self._sidebar_transition = True
        if sync_action:
            self.panels_action.setEnabled(False)
        if visible:
            target_width = max(180, self._sidebar_open_width)
            start_width = max(0, self.left_workspace.width())
            if self.left_workspace.isHidden():
                start_width = 0
            self.left_workspace.setVisible(True)
            self.left_workspace.setMinimumWidth(0)
            self.left_workspace.setMaximumWidth(start_width)

            def finish_expand() -> None:
                if self._panel_transition_serial["left"] != serial:
                    return
                self.left_workspace.setMaximumWidth(16_777_215)
                if restore_sizes is not None and len(restore_sizes) == 3:
                    # During Preview the right-hand page can temporarily change
                    # splitter constraints. Pin the remembered left edge for one
                    # layout pass so Qt does not proportionally shrink it while
                    # the normal inspector page is being restored.
                    restored_left = max(180, int(restore_sizes[0]))
                    self.left_workspace.setMinimumWidth(restored_left)
                    self.main_splitter.setSizes(restore_sizes)
                    self.left_workspace.setMinimumWidth(180)
                    self._sidebar_open_width = restored_left
                else:
                    self.left_workspace.setMinimumWidth(180)
                self._sidebar_transition = False
                self.main_splitter.lock_edge_sizes()
                if sync_action:
                    self.panels_action.setEnabled(True)
                self.motion.fade_in(self.left_workspace, 120)

            self.motion.animate_width(
                self.left_workspace, start_width, target_width,
                on_finished=finish_expand,
            )
            return
        start_width = max(0, self.left_workspace.width())
        if persist or sync_action:
            self._sidebar_open_width = max(180, start_width)
        self.left_workspace.setMinimumWidth(0)
        self.left_workspace.setMaximumWidth(start_width)

        def finish_collapse() -> None:
            if self._panel_transition_serial["left"] != serial:
                return
            self.left_workspace.setVisible(False)
            self.left_workspace.setMaximumWidth(16_777_215)
            self.left_workspace.setMinimumWidth(180)
            self._sidebar_transition = False
            self.main_splitter.lock_edge_sizes()
            if sync_action:
                self.panels_action.setEnabled(True)

        self.motion.animate_width(
            self.left_workspace, start_width, 0,
            on_finished=finish_collapse,
        )

    def _set_inspector_panel_visible(
        self, visible: bool, *, persist: bool = True, sync_action: bool = True,
    ) -> None:
        """Show or hide the right properties workspace independently."""
        if persist:
            QSettings().setValue("workspace/right_panel_visible", bool(visible))
        self._panel_transition_serial["right"] += 1
        serial = self._panel_transition_serial["right"]
        if sync_action:
            self.inspector_panel_action.setEnabled(False)
        sizes = self.main_splitter.sizes()
        if not visible:
            if len(sizes) == 3:
                self._inspector_open_width = max(
                    220, sizes[2], self.inspector_stack.width(),
                )
            start_width = max(220, self.inspector_stack.width())
            self.inspector_stack.setMinimumWidth(0)
            self.inspector_stack.setMaximumWidth(start_width)

            def finish_collapse() -> None:
                if self._panel_transition_serial["right"] != serial:
                    return
                self.inspector_stack.setVisible(False)
                self.inspector_stack.setMaximumWidth(16_777_215)
                self.inspector_stack.setMinimumWidth(self.inspector.minimumWidth())
                self.main_splitter.lock_edge_sizes()
                if sync_action:
                    self.inspector_panel_action.setEnabled(True)

            self.motion.animate_width(
                self.inspector_stack, start_width, 0,
                on_finished=finish_collapse,
            )
            return
        start_width = max(0, self.inspector_stack.width())
        if self.inspector_stack.isHidden():
            start_width = 0
        self.inspector_stack.setVisible(True)
        self.inspector_stack.setMinimumWidth(0)
        self.inspector_stack.setMaximumWidth(start_width)
        current = self.main_splitter.sizes()
        total = max(900, sum(current))
        left = current[0] if len(current) == 3 else 0
        maximum = max(220, total - left - 300)
        target = min(maximum, max(220, self._inspector_open_width))

        def finish_expand() -> None:
            if self._panel_transition_serial["right"] != serial:
                return
            self.inspector_stack.setMaximumWidth(16_777_215)
            self.inspector_stack.setMinimumWidth(self.inspector.minimumWidth())
            self.main_splitter.lock_edge_sizes()
            if sync_action:
                self.inspector_panel_action.setEnabled(True)
            self.motion.fade_in(self.inspector_stack, 120)

        self.motion.animate_width(
            self.inspector_stack, start_width, target,
            on_finished=finish_expand,
        )

    def _set_bottom_panel_visible(
        self, visible: bool, *, persist: bool = True, sync_action: bool = True,
    ) -> None:
        """Show or hide the shared Playlist, Timeline, and Preview workspace."""
        if persist:
            QSettings().setValue("workspace/bottom_panel_visible", bool(visible))
        self._panel_transition_serial["bottom"] += 1
        serial = self._panel_transition_serial["bottom"]
        if sync_action:
            self.bottom_panel_action.setEnabled(False)
        sizes = self.workspace_splitter.sizes()
        if not visible:
            if len(sizes) == 2:
                self._bottom_open_height = max(
                    180, sizes[1], self.bottom_workspace_stack.height(),
                )
            start_height = max(180, self.bottom_workspace_stack.height())
            self.bottom_workspace_stack.setMinimumHeight(0)
            self.bottom_workspace_stack.setMaximumHeight(start_height)

            def finish_collapse() -> None:
                if self._panel_transition_serial["bottom"] != serial:
                    return
                self.bottom_workspace_stack.setVisible(False)
                self.bottom_workspace_stack.setMaximumHeight(16_777_215)
                self.bottom_workspace_stack.setMinimumHeight(180)
                self.workspace_splitter.lock_edge_sizes()
                if sync_action:
                    self.bottom_panel_action.setEnabled(True)

            self.motion.animate_height(
                self.bottom_workspace_stack, start_height, 0,
                on_finished=finish_collapse,
            )
            return
        start_height = max(0, self.bottom_workspace_stack.height())
        if self.bottom_workspace_stack.isHidden():
            start_height = 0
        self.bottom_workspace_stack.setVisible(True)
        self.bottom_workspace_stack.setMinimumHeight(0)
        self.bottom_workspace_stack.setMaximumHeight(start_height)
        current = self.workspace_splitter.sizes()
        total = max(500, sum(current))
        target = min(
            max(180, self._bottom_open_height), max(180, total - 300),
        )

        def finish_expand() -> None:
            if self._panel_transition_serial["bottom"] != serial:
                return
            self.bottom_workspace_stack.setMaximumHeight(16_777_215)
            self.bottom_workspace_stack.setMinimumHeight(180)
            self.workspace_splitter.lock_edge_sizes()
            if sync_action:
                self.bottom_panel_action.setEnabled(True)
            self.motion.fade_in(self.bottom_workspace_stack, 120)

        self.motion.animate_height(
            self.bottom_workspace_stack, start_height, target,
            on_finished=finish_expand,
        )

    def _schedule_history(self) -> None:
        self.history_controller.schedule()

    def _update_project_status(self) -> None:
        self.project_controller.update_status()

    def _commit_history(self) -> None:
        self.history_controller.commit()

    def _update_history_actions(self, can_undo: bool, can_redo: bool) -> None:
        self.history_controller.update_actions(can_undo, can_redo)

    def _undo(self) -> None:
        self.history_controller.undo()

    def _redo(self) -> None:
        self.history_controller.redo()

    def _autosave_project(self) -> None:
        self.autosave_controller.autosave()

    def _autosave_failed(self, message: str) -> None:
        self.autosave_controller.failed(message)

    def _autosave_succeeded(self, _path: object) -> None:
        self.autosave_controller.succeeded(_path)

    def _offer_recovery(self, *, snapshot_path: Path | None = None, confirm: bool = True) -> bool:
        if snapshot_path is None and confirm:
            return self.autosave_controller.offer_recovery()
        return self.autosave_controller.offer_recovery(snapshot_path=snapshot_path, confirm=confirm)

    def _clear_recovery(self) -> None:
        self.autosave_controller.clear_recovery()

    def _save_project(
        self, force_choose: bool = False, *, wait_for_completion: bool = False,
    ) -> bool:
        return self.project_controller.save(
            force_choose, wait_for_completion=wait_for_completion,
        )

    def _wait_for_project_save(self, worker: ProjectSaveWorker) -> None:
        self.project_controller.wait_for_save(worker)

    def _project_save_failed(self, message: str) -> None:
        self.project_controller.save_failed(message)

    def _project_save_finished_successfully(self, saved_path: object) -> None:
        self.project_controller.save_finished_successfully(saved_path)

    def _open_project(self) -> None:
        self.project_controller.open_project()

    def _open_project_with_confirmation(self, path: Path) -> bool:
        return self.project_controller.open_project_with_confirmation(path)

    def open_project_path(self, path: Path) -> bool:
        """Open a project requested by Explorer or another external launcher."""
        return self.project_controller.open_project_path(path)

    def _confirm_unsaved_changes(self) -> bool:
        return self.project_controller.confirm_unsaved_changes()

    def _load_project_path(self, path: Path) -> bool:
        return self.project_controller.load_path(path)

    @staticmethod
    def _project_load_guidance(error: BaseException, korean: bool) -> str:
        return ProjectController.load_guidance(error, korean)

    def _resolve_project_media(self, document: ProjectDocument, project_path: Path) -> bool:
        return self.project_controller.resolve_media(document, project_path)

    def _apply_project(self, document: ProjectDocument) -> None:
        self.project_controller.apply(document)

    def _project_thumbnail_image(self) -> QImage:
        return self.project_controller.thumbnail_image()

    def _set_automix_override(self, key: str, override) -> None:
        """Store one manual AutoMix junction (``None``: back to automatic) in the project, undoably."""
        overrides = dict(self.project_settings.automix_overrides)
        if override is None:
            if overrides.pop(key, None) is None:
                return
        else:
            overrides[key] = override.to_dict()
        self.project_settings = replace(self.project_settings, automix_overrides=overrides)
        self._schedule_history()
        self._update_project_status()

    def _show_project_settings(self) -> None:
        """Edit project-scoped identity, content policy, and thumbnail."""
        artboard = self.canvas.scene_model.artboard_rect
        original_canvas_size = (round(artboard.width()), round(artboard.height()))
        thumbnail = QPixmap.fromImage(self._project_thumbnail_image())
        dialog = ProjectSettingsDialog(
            self.project_settings, self.translator, thumbnail, self,
            canvas_size=original_canvas_size,
        )
        if dialog.exec() == dialog.DialogCode.Accepted:
            self.project_settings = dialog.selected_settings
            if dialog.selected_canvas_size != original_canvas_size:
                self._resize_project_canvas(
                    original_canvas_size,
                    dialog.selected_canvas_size,
                    dialog.scale_canvas_content,
                )
            self._schedule_history()
            self._update_project_status()
            self._maybe_start_automix_analysis()

    def _resize_project_canvas(
        self, old_size: tuple[int, int], new_size: tuple[int, int],
        scale_content: bool,
    ) -> None:
        """Resize the artboard while preserving or ratio-adapting current sources."""
        old_width, old_height = old_size
        new_width, new_height = new_size
        selected_ids = self.store.selected_ids
        active_id = self.store.selected.id if self.store.selected is not None else None
        sources = self.store.sources()
        if scale_content:
            resized = self._adapt_sources_to_canvas(
                sources, old_width, old_height, new_width, new_height,
            )
        else:
            resized = [Source.from_dict(source.to_dict()) for source in sources]
            for source in resized:
                if (
                    source.source_type is SourceType.BACKGROUND
                    and abs(source.x) < 0.01 and abs(source.y) < 0.01
                    and abs(source.width - old_width) < 0.01
                    and abs(source.height - old_height) < 0.01
                ):
                    source.width = new_width
                    source.height = new_height
        with self.project_session.suspend_changes():
            self.canvas.scene_model.set_artboard_size(new_width, new_height)
            self.store.replace(resized, self.store.groups())
            valid_selection = [
                source_id for source_id in selected_ids
                if self.store.get(source_id) is not None
            ]
            self.store.select_many(
                valid_selection,
                active_id if active_id in valid_selection else None,
            )
            self.canvas.fit_artboard()

    def _show_lyrics_search(self) -> None:
        """Find lyrics using typed metadata or a song already in the playlist."""
        selected_ids = self.playlist_editor._selected_ids()
        dialog = LyricsSearchDialog(
            self.playlist_service.tracks, self.translator, self,
            initial_track_id=selected_ids[0] if len(selected_ids) == 1 else "",
            on_attach=self._attach_found_lyrics,
        )
        dialog.exec()
        dialog.deleteLater()

    def _attach_found_lyrics(self, document, track_id: str) -> bool:
        """Embed downloaded cues; retain the normal comparison before replacement."""
        from copy import deepcopy
        target = next((track for track in self.playlist_service.tracks if track.id == track_id), None)
        korean = self.translator.is_korean
        if target is None or not document.cues:
            QMessageBox.warning(self, "가사 연결" if korean else "Attach lyrics",
                "연결할 곡 또는 시간 정보가 있는 가사를 찾을 수 없습니다." if korean else
                "The target track or timed lyrics are no longer available.")
            return False
        if target.lyrics or target.lyrics_path:
            comparison = LyricsCompareDialog(target.lyrics, document.cues, target.lyrics_path,
                                             document.filename, self.translator, self)
            accepted = comparison.exec() == QDialog.DialogCode.Accepted and comparison.replace_requested
            comparison.deleteLater()
            if not accepted:
                return False
        self.playlist_service.update_track(target.id, lyrics=deepcopy(document.cues), lyrics_path="")
        if not any(source.source_type is SourceType.LYRICS for source in self.store.sources()):
            self._add_source(SourceType.LYRICS)
        self.statusBar().showMessage("선택한 곡에 가사를 연결했습니다." if korean else "Lyrics attached to the selected track.", 5000)
        return True

    def _show_lrc_generator(self) -> None:
        """Open the audio-assisted LRC authoring workflow."""
        selected_ids = self.playlist_editor._selected_ids()
        selected_track = next(
            (
                track for track in self.playlist_service.tracks
                if len(selected_ids) == 1 and track.id == selected_ids[0]
            ),
            None,
        )
        dialog = LrcGeneratorDialog(
            self.project_content_service.items,
            self.translator,
            self,
            playlist_tracks=self.playlist_service.tracks,
            target_track_id=selected_track.id if selected_track else "",
            initial_audio_path=selected_track.file_path if selected_track else "",
            initial_cues=selected_track.lyrics if selected_track else None,
            initial_title=selected_track.title if selected_track else "",
            initial_artist=selected_track.artist if selected_track else "",
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if accepted and dialog.result_track_id:
            cues = dialog.timed_cues()
            lyrics_path = (
                str(dialog.saved_paths[-1].resolve())
                if dialog.saved_paths else ""
            )
            self.playlist_service.update_track(
                dialog.result_track_id,
                lyrics_path=lyrics_path,
                lyrics=[cue.copy() for cue in cues],
            )
        if dialog.saved_paths and dialog.add_saved_files_to_project:
            added = self.project_content_service.add_paths(dialog.saved_paths)
            if added:
                korean = self.translator.is_korean
                self.statusBar().showMessage(
                    f"저장한 LRC 파일 {added}개를 프로젝트 콘텐츠에 추가했습니다."
                    if korean else
                    f"Added {added} saved LRC file(s) to project content.",
                    5000,
                )

    def _show_mp3_metadata_editor(self) -> None:
        """Open the standalone editor for source MP3 ID3 tags."""
        Mp3MetadataEditorDialog(self.translator, self).exec()

    def _offer_legacy_upgrade(self) -> None:
        self.project_controller.offer_legacy_upgrade()

    def _upgrade_legacy_project(self) -> None:
        self.project_controller.upgrade_legacy_project()

    def _show_project_error(self, error: ProjectError) -> None:
        self.project_controller.show_error(error)

    def _toggle_grid(self, visible: bool) -> None:
        self.canvas.scene_model.show_grid = visible
        self.canvas.scene_model.update()
        self._schedule_history()

    def retranslate(self) -> None:
        """Refresh all user-interface strings for the active language."""
        text = self.translator.text
        korean = self.translator.is_korean
        self.setWindowTitle(text("app_title"))
        self.new_action.setText(text("new"))
        self.open_action.setText(text("open"))
        self.save_action.setText(text("save"))
        self.save_as_action.setText(
            "다른 이름으로 저장" if self.translator.is_korean else "Save As"
        )
        self.undo_action.setText("실행 취소" if self.translator.is_korean else "Undo")
        self.redo_action.setText("다시 실행" if self.translator.is_korean else "Redo")
        self.center_horizontal_action.setText("가로 중앙" if korean else "Center H")
        self.center_vertical_action.setText("세로 중앙" if korean else "Center V")
        horizontal_help = (
            "선택 요소를 캔버스의 가로 중앙에 배치합니다. (Ctrl+Shift+H)"
            if korean else
            "Center selected sources horizontally on the canvas. (Ctrl+Shift+H)"
        )
        vertical_help = (
            "선택 요소를 캔버스의 세로 중앙에 배치합니다. (Ctrl+Shift+V)"
            if korean else
            "Center selected sources vertically on the canvas. (Ctrl+Shift+V)"
        )
        self.center_horizontal_action.setToolTip(horizontal_help)
        self.center_horizontal_action.setStatusTip(horizontal_help)
        self.center_vertical_action.setToolTip(vertical_help)
        self.center_vertical_action.setStatusTip(vertical_help)
        self.zoom_out_button.setToolTip(
            "축소 (Ctrl+-)" if korean else "Zoom out (Ctrl+-)"
        )
        self.zoom_in_button.setToolTip(
            "확대 (Ctrl+=)" if korean else "Zoom in (Ctrl+=)"
        )
        self.zoom_reset_button.setToolTip(
            "100% ↔ 화면 맞춤" if korean else "100% ↔ fit to view"
        )
        self.cut_action.setText("잘라내기" if korean else "Cut")
        self.copy_action.setText("복사" if korean else "Copy")
        self.paste_action.setText("붙여넣기" if korean else "Paste")
        self.duplicate_action.setText(
            "복제" if self.translator.is_korean else "Duplicate"
        )
        self.select_all_action.setText(
            "전체 선택" if self.translator.is_korean else "Select all"
        )
        self.presets_action.setText(
            "디자인 프리셋" if self.translator.is_korean
            else "Design Presets"
        )
        self.save_preset_action.setText(
            "현재 캔버스를 프리셋으로 저장…"
            if self.translator.is_korean
            else "Save current canvas as preset…"
        )
        self.fit_action.setText(text("fit_canvas"))
        self.grid_action.setText(text("grid"))
        self.canvas.scene_model.set_placeholder_language(korean)
        self.delete_action.setText(text("delete"))
        self.export_action.setText(text("export"))
        self.preview_action.setText("미리보기" if self.translator.is_korean else "Preview")
        self.settings_action.setText("설정" if self.translator.is_korean else "Settings")
        self.lyrics_search_action.setText("가사 찾아보기…" if korean else "Find lyrics…")
        self.lrc_generator_action.setText(
            "LRC 파일 생성기" if self.translator.is_korean
            else "LRC File Generator"
        )
        self.mp3_metadata_action.setText(
            "MP3 메타데이터 편집기" if self.translator.is_korean
            else "MP3 Metadata Editor"
        )
        self.automix_editor_action.setText("AutoMix 편집기" if korean else "AutoMix Editor")
        self.automix_editor_action.setStatusTip(
            "곡 사이 전환을 두 곡 타임라인에서 직접 편집합니다. (AutoMix 전환 모드에서 사용)" if korean
            else "Edit the transitions between songs on a two-track timeline (AutoMix transition mode)."
        )
        self.file_menu.setTitle("파일" if self.translator.is_korean else "File")
        self.recent_projects_menu.setTitle(
            "최근 프로젝트" if korean else "Recent projects"
        )
        self._rebuild_recent_projects_menu()
        self.project_menu.setTitle("프로젝트" if self.translator.is_korean else "Project")
        self.edit_menu.setTitle("편집" if self.translator.is_korean else "Edit")
        self.insert_menu.setTitle("추가" if self.translator.is_korean else "Add")
        self.view_menu.setTitle("보기" if self.translator.is_korean else "View")
        self.tools_menu.setTitle("도구" if self.translator.is_korean else "Tools")
        self.help_menu.setTitle("도움말" if self.translator.is_korean else "Help")
        self.exit_action.setText("종료" if self.translator.is_korean else "Exit")
        self.clear_selection_action.setText(
            "선택 해제" if self.translator.is_korean else "Clear selection"
        )
        self.help_action.setText(
            "사용 설명서" if self.translator.is_korean
            else "User Guide"
        )
        self.shortcuts_action.setText(
            "단축키 안내" if self.translator.is_korean else "Keyboard shortcuts"
        )
        self.check_updates_action.setText(
            "업데이트 확인" if self.translator.is_korean
            else "Check for updates"
        )
        self.about_action.setText(
            "프로그램 정보" if self.translator.is_korean
            else "About Playlist Canvas"
        )
        self.project_settings_action.setText(
            "프로젝트 설정" if self.translator.is_korean else "Project settings"
        )
        self.upgrade_project_action.setText(
            "레거시 프로젝트 업그레이드…"
            if self.translator.is_korean else "Upgrade legacy project…"
        )
        self.show_playlist_action.setText(
            "플레이리스트 열기" if self.translator.is_korean else "Show Playlist"
        )
        self.show_timeline_action.setText(
            "타임라인 열기" if self.translator.is_korean else "Show Timeline"
        )
        self.playlist_files_action.setText(
            "타임라인 내보내기" if self.translator.is_korean else "Export timeline"
        )
        self.import_m3u_action.setText(
            "M3U8 플레이리스트 가져오기…" if korean else "Import M3U8 playlist…"
        )
        self.import_m3u_action.setStatusTip(
            "M3U8 파일에 담긴 곡을 플레이리스트에 추가합니다."
            if korean else "Add the songs listed in an M3U8 file to the Playlist."
        )
        self.export_m3u_action.setText(
            "M3U8 플레이리스트 내보내기…" if korean else "Export M3U8 playlist…"
        )
        self.export_m3u_action.setStatusTip(
            "사용 중인 곡을 플레이리스트 순서대로 M3U8 파일로 저장합니다."
            if korean else "Save the enabled songs, in Playlist order, as an M3U8 file."
        )
        category_titles = {
            "basic": "기본 요소" if korean else "Basic sources",
            "playback": "재생 정보" if korean else "Playback information",
            "branding": "브랜딩 및 배경" if korean else "Branding and background",
            "audio_effects": "오디오 시각 효과" if korean else "Audio visuals",
        }
        for category, menu in self.insert_category_menus.items():
            menu.setTitle(category_titles[category])
        for source_type, action in self.source_insert_actions.items():
            label = self._source_type_label(source_type)
            action.setText(label)
            description, settings = self._source_hover_help(source_type)
            action.setToolTip(f"{description} {settings}")
            action.setStatusTip(description)
        self.language_menu.setTitle(text("language"))
        self.panels_action.setText(
            "왼쪽 패널 표시" if korean else "Show left panel"
        )
        self.inspector_panel_action.setText(
            "오른쪽 속성 패널 표시" if korean else "Show right properties panel"
        )
        self.bottom_panel_action.setText(
            "하단 작업 패널 표시" if korean else "Show bottom workspace"
        )
        self.reset_layout_action.setText(
            "패널 크기 초기화" if korean else "Reset panel sizes"
        )
        self.reset_layout_action.setToolTip(
            "왼쪽·오른쪽·하단 패널을 기본 크기로 되돌려 캔버스를 넓게 만듭니다."
            if korean else
            "Return the side and bottom panels to their default sizes, giving the Canvas room."
        )
        self.panels_action.setToolTip(
            "요소·프로젝트 콘텐츠·레이어 패널을 표시하거나 숨깁니다."
            if korean else
            "Show or hide the Sources, Project content, and Layers panel."
        )
        self.inspector_panel_action.setToolTip(
            "오른쪽 속성 패널을 표시하거나 숨깁니다."
            if korean else "Show or hide the right properties panel."
        )
        self.bottom_panel_action.setToolTip(
            "플레이리스트·타임라인·미리보기 패널을 표시하거나 숨깁니다."
            if korean else
            "Show or hide the Playlist, Timeline, and Preview workspace."
        )
        self.sidebar_title.setText(text("add_to_canvas"))
        self.left_tabs.setTabText(
            0, "요소" if self.translator.is_korean else "Sources"
        )
        self.left_tabs.setTabText(
            1, "프로젝트 콘텐츠" if self.translator.is_korean else "Project content"
        )
        self.left_tabs.setTabText(
            2, "레이어" if self.translator.is_korean else "Layers"
        )
        self.left_tabs.setTabToolTip(
            0, "캔버스에 추가할 요소" if korean else "Sources to add to the Canvas"
        )
        self.left_tabs.setTabToolTip(
            1, "프로젝트 콘텐츠" if korean else "Project content"
        )
        self.left_tabs.setTabToolTip(
            2, "캔버스 레이어와 그룹" if korean else "Canvas layers and groups"
        )
        self.bottom_tabs.setTabText(
            0, "플레이리스트" if self.translator.is_korean else "Playlist"
        )
        self.bottom_tabs.setTabText(
            1, "타임라인" if self.translator.is_korean else "Timeline"
        )
        self.bottom_tabs.setTabText(
            2, "미리보기" if self.translator.is_korean else "Preview"
        )
        for index, tooltip in enumerate((
            ("곡 추가·순서·포함 여부 편집 (Ctrl+Alt+1)" if korean
             else "Add, order, and include tracks (Ctrl+Alt+1)"),
            ("곡별 시작·종료 시간과 요소 표시 시간 확인 (Ctrl+Alt+2)" if korean
             else "Track start/end times and source display timing (Ctrl+Alt+2)"),
            ("전체 플레이리스트를 실제 음원과 함께 재생 · 재생 중에는 편집이 잠깁니다 (Ctrl+Alt+3)"
             if korean else
             "Play the complete playlist with audio · editing is locked while it plays (Ctrl+Alt+3)"),
        )):
            self.bottom_tabs.setTabToolTip(index, tooltip)
        self.source_search.setPlaceholderText(
            "요소 검색…" if korean else "Search sources…"
        )
        category_titles = {
            "basic": "기본 요소" if korean else "BASIC",
            "playback": "재생 정보" if korean else "PLAYBACK",
            "audio": "오디오 효과" if korean else "AUDIO EFFECTS",
            "scene": "장면 꾸미기" if korean else "SCENE",
        }
        for category, label in self._source_category_titles.items():
            label.setText(category_titles[category])
        tab_titles = {
            "all": "전체" if korean else "All",
            "basic": "기본" if korean else "Basic",
            "playback": "재생" if korean else "Playback",
            "audio": "오디오" if korean else "Audio",
            "scene": "장면" if korean else "Scene",
        }
        for index, category in enumerate(self._source_tab_categories):
            self.source_tab_bar.setTabText(index, tab_titles[category])
        for source_type, button in self._source_buttons.items():
            label = self._source_type_label(source_type)
            is_variant = source_type in self._source_variant_parents
            title = (
                f"{label} 템플릿" if korean and is_variant else
                f"{label} template" if is_variant else label
            )
            button.set_card_text(
                title, self._source_palette_summary(source_type),
                variant=is_variant,
            )
            self._update_source_button_help(source_type, button, label)
        for parent_type, toggle in self._source_variant_toggles.items():
            expanded = toggle.isChecked()
            toggle.setText("▾" if expanded else "▸")
            parent_label = self._source_type_label(parent_type)
            toggle.setToolTip(
                f"{parent_label} 변형 템플릿 펼치기/접기"
                if korean else f"Expand or collapse {parent_label} templates"
            )
            toggle.setAccessibleName(toggle.toolTip())
        self._filter_source_cards(self.source_search.text())
        self._update_project_status()
        self.snap_action.setToolTip(
            "객체를 그리드와 정렬 가이드에 맞춥니다."
            if self.translator.is_korean
            else "Snap objects to the grid and alignment guides."
        )
        self.grid_action.setToolTip(
            "캔버스 작업 공간 전체에 40px 간격의 그리드를 표시합니다. 영상에는 포함되지 않습니다."
            if self.translator.is_korean else
            "Show a 40 px grid across the complete Canvas workspace. It is not included in video output."
        )
        self.export_action.setToolTip(
            "현재 Canvas와 Playlist를 MP4 파일로 렌더링합니다. (Ctrl+E)"
            if self.translator.is_korean
            else "Render the current Canvas and Playlist as an MP4 file. (Ctrl+E)"
        )
        self.preview_action.setToolTip(
            "하단 미리보기 탭에서 전체 플레이리스트를 실제 음원과 함께 확인합니다. (Ctrl+Alt+3)"
            if self.translator.is_korean
            else "Open the bottom Preview tab with the complete playlist and actual audio. (Ctrl+Alt+3)"
        )
        # Icon-only toolbar buttons: the tooltip is the only place their shortcut shows.
        for action in (
            self.new_action, self.open_action, self.save_action,
            self.undo_action, self.redo_action,
        ):
            shortcut = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            action.setToolTip(f"{action.text()} ({shortcut})" if shortcut else action.text())
        self.playlist_files_action.setToolTip(
            "믹싱 타임라인의 곡 시작 시간과 곡 정보를 YouTube 설명문·CSV로 저장합니다."
            if self.translator.is_korean
            else "Export mix timeline timestamps and track details as a YouTube description and CSV."
        )
        self.settings_action.setToolTip(
            "FFmpeg, 출력, 렌더링 기본값을 설정합니다."
            if self.translator.is_korean
            else "Configure FFmpeg, output, and rendering defaults."
        )

    def _toggle_snap(self, enabled: bool) -> None:
        self.canvas.scene_model.snap_enabled = enabled
        self.canvas.scene_model.clear_alignment_guides()
        self._schedule_history()

    def _delete_selected_sources(self) -> None:
        """Remove each selected, non-locked source from the canvas."""
        selected_ids = [
            item.source.id
            for item in self.canvas.scene_model.selectedItems()
            if hasattr(item, "source") and not item.source.locked
        ]
        for source_id in selected_ids:
            self.store.remove(source_id)
        if selected_ids:
            self._show_undo_hint(
                f"요소 {len(selected_ids)}개를 삭제했습니다."
                if self.translator.is_korean else
                f"Deleted {len(selected_ids)} source(s)."
            )

    def _show_undo_hint(self, message: str) -> None:
        """Confirm a removal in the status bar and name the way back."""
        korean = self.translator.is_korean
        self.statusBar().showMessage(
            f"{message} 실행 취소(Ctrl+Z)로 되돌릴 수 있습니다." if korean
            else f"{message} Undo (Ctrl+Z) brings it back.",
            6000,
        )

    def canvas_fit(self) -> None:
        self.canvas.fit_artboard()

    def _show_bottom_panel(self, index: int) -> None:
        """Reveal and focus a bottom workspace tab."""
        if self.bottom_workspace_stack.isHidden():
            self.bottom_panel_action.setChecked(True)
        selected = max(0, min(self.bottom_tabs.count() - 1, index))
        self.bottom_tabs.setCurrentIndex(selected)
        sizes = self.workspace_splitter.sizes()
        if len(sizes) == 2 and sizes[1] < 180:
            total = max(500, sum(sizes))
            self.workspace_splitter.setSizes([max(300, total - 280), 280])
        if selected == 0:
            self.playlist_editor.list_widget.setFocus(
                Qt.FocusReason.ShortcutFocusReason
            )
        elif selected == 1:
            (self.timeline_panel.visual if self.timeline_panel.view_combo.currentIndex() == 0
             else self.timeline_panel.track_table).setFocus(
                Qt.FocusReason.ShortcutFocusReason
            )

    def closeEvent(self, event: QCloseEvent) -> None:
        """Avoid destroying a running FFmpeg thread during application shutdown."""
        import_worker = getattr(self, "_audio_import_worker", None)
        if import_worker is not None:
            import_worker.cancel_event.set()
            event.ignore()
            return
        if self.project_controller.saving:
            message = (
                "프로젝트 저장이 완료된 후 종료해 주세요."
                if self.translator.is_korean
                else "Wait for the project save to finish before closing."
            )
            self.statusBar().showMessage(message, 5000)
            event.ignore()
            return
        if self._animation_preview_active:
            self.animation_preview_controller.cancel()
        if self._inline_preview is not None:
            self._finish_inline_preview()
        if self._export_preparation_cancel is not None:
            if self._export_preparation_cancel.is_set():
                cancellation_requested = True
            elif self._export_dialog is not None:
                cancellation_requested = self._export_dialog.request_cancel()
            else:
                self._export_preparation_cancel.set()
                cancellation_requested = True
            if cancellation_requested:
                self._close_after_export_cancel = True
                message = (
                    "내보내기를 안전하게 취소한 뒤 종료합니다."
                    if self.translator.is_korean else
                    "Closing after export is cancelled safely..."
                )
                self.statusBar().showMessage(message, 5000)
            event.ignore()
            return
        if self._render_worker and self._render_worker.isRunning():
            if self._export_dialog is not None:
                cancellation_requested = (
                    self._export_dialog.is_cancelling
                    or self._export_dialog.request_cancel()
                )
            else:
                self._render_worker.cancel()
                cancellation_requested = True
            if cancellation_requested:
                self._close_after_export_cancel = True
                message = (
                    "내보내기를 안전하게 취소한 뒤 종료합니다."
                    if self.translator.is_korean else
                    "Closing after export is cancelled safely..."
                )
                self.statusBar().showMessage(message, 5000)
            event.ignore()
            return
        if self._ffmpeg_install_worker and self._ffmpeg_install_worker.isRunning():
            self._ffmpeg_install_worker.cancel()
            message = "FFmpeg 다운로드를 취소하는 중입니다." if self.translator.is_korean else "Cancelling FFmpeg download..."
            self.statusBar().showMessage(message, 5000)
            event.ignore()
            return
        if self._ffmpeg_catalog_worker and self._ffmpeg_catalog_worker.isRunning():
            self._ffmpeg_catalog_worker.cancel()
            self._close_after_ffmpeg_catalog_cancel = True
            message = (
                "FFmpeg 버전 확인을 중단한 뒤 종료합니다."
                if self.translator.is_korean else
                "Closing after the FFmpeg version check stops..."
            )
            self.statusBar().showMessage(message, 5000)
            event.ignore()
            return
        if self._update_check_worker and self._update_check_worker.isRunning():
            # The request can't be interrupted (bounded by its urlopen timeout),
            # and a QThread destroyed at exit while still running aborts the
            # whole process, so close once it finishes instead.
            self._close_after_update_check = True
            message = (
                "업데이트 확인이 끝난 뒤 종료합니다."
                if self.translator.is_korean else
                "Closing after the update check finishes..."
            )
            self.statusBar().showMessage(message, 5000)
            event.ignore()
            return
        if self._update_download_worker and self._update_download_worker.isRunning():
            self._update_download_worker.cancel()
            message = "업데이트 다운로드를 취소하는 중입니다." if self.translator.is_korean else "Cancelling update download..."
            self.statusBar().showMessage(message, 5000)
            event.ignore()
            return
        if self.project_session.dirty and not self._update_install_authorized:
            korean = self.translator.is_korean
            response = QMessageBox.warning(
                self,
                "저장되지 않은 변경 사항" if korean else "Unsaved changes",
                "프로젝트에 저장되지 않은 변경 사항이 있습니다. 종료하기 전에 저장할까요?"
                if korean else "This project has unsaved changes. Save before closing?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if response == QMessageBox.StandardButton.Save:
                self._save_project(wait_for_completion=True)
                if self.project_session.dirty:
                    # The user cancelled Save As or saving failed; stay in the editor.
                    event.ignore()
                    return
            elif response != QMessageBox.StandardButton.Discard:
                # Fail closed: Cancel, Escape, or an unrecognized response keeps
                # the unsaved workspace open. Only explicit Discard may exit.
                event.ignore()
                return
        self._automix_analysis_timer.stop()
        self.automix_analysis_controller.shutdown()
        self.track_analysis_controller.shutdown()
        if self._workspace_settings_timer.isActive():
            self._workspace_settings_timer.stop()
        self._autosave_timer.stop()
        self._autosave_debounce_timer.stop()
        # Let a running recovery write finish before _clear_recovery(), otherwise
        # its atomic replace re-creates the file we just deleted and the next
        # launch offers a stale recovery after a clean exit. No timeout: a slow
        # disk past a bounded wait would leave the QThread running at exit, and
        # destroying a running QThread aborts the process.
        self.autosave_controller.wait_for_pending()
        self._save_workspace_layout()
        QSettings().sync()
        if not self._update_install_authorized:
            self._clear_recovery()
        self.canvas.release_video_decoders()
        self.smooth_scroll.uninstall()
        application = QApplication.instance()
        if application is not None:
            try:
                application.focusChanged.disconnect(
                    self._sync_canvas_shortcut_actions
                )
            except (RuntimeError, TypeError):
                pass
        if self._reset_after_close:
            from app.services.maintenance_service import reset_program_data, restart_arguments

            reset_program_data()
            QProcess.startDetached(*restart_arguments())
        event.accept()

    def _apply_style(self) -> None:
        """Apply the shared studio skin without restyling every open window."""
        from app.ui.design_system import apply_studio_style
        apply_studio_style(QApplication.instance())
        for panel in (self.source_sidebar, self.layer_panel, self.playlist_editor):
            panel.setGraphicsEffect(None)
        self.canvas.set_theme_colors(
            QColor("#17191B"), QColor("#202326"), QColor(255, 255, 255, 10),
            QColor("#646A70"),
        )
