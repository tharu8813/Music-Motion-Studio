"""Application settings dialog for Phase 4A."""

from __future__ import annotations

from app.ui.design_system import COLORS

import subprocess
from pathlib import Path

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.services.app_settings_service import (
    ENCODING_PRESETS,
    RESOLUTIONS,
    VIDEO_ENCODERS,
    AUDIO_BITRATES,
    EXPORT_NOTIFICATION_MODES,
    LYRICS_AUTO_ATTACH_MODES,
    AppSettings,
)
from app.services.theme_service import Theme
from app.services.language_pack_service import LanguagePackError
from app.utils.i18n import Language, LanguageSelection, Translator
from app.utils.subprocess_utils import hidden_process_kwargs
from app.ffmpeg.managed_installer import (
    FFmpegReleaseOption,
    ManagedFFmpegInstallation,
)
from app.services.video_encoder_service import (
    AUTO_VIDEO_ENCODER,
    VideoEncoderAdvisor,
)
from app.renderer.ffmpeg_renderer import (
    WORK_MODE_AUTO,
    WORK_MODE_MAX_SPEED,
    WORK_MODE_STABLE,
)
from app.services.export_validation_service import EXPORT_FPS_OPTIONS


_TAB_HELP_TOPICS = ("settings", "export", "ffmpeg", "lyrics_files", "export_process", "maintenance")
"""F1 opens the help topic of the tab being shown (General, Export, FFmpeg, Content, Notifications, Maintenance)."""


class SettingsDialog(QDialog):
    """Edits app-wide render, appearance, and localization preferences."""

    download_requested = Signal()
    update_requested = Signal()
    reinstall_requested = Signal()
    delete_requested = Signal()
    catalog_requested = Signal(bool)

    def __init__(self, settings: AppSettings, language: LanguageSelection, theme: Theme,
                 translator: Translator, parent: object | None = None, *,
                 active_preview_backend: str | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(
            self, lambda dialog: ("other", _TAB_HELP_TOPICS[max(0, dialog.tabs.currentIndex())]), translator,
        )
        self.setObjectName("appSettingsDialog")
        self.translator = translator
        self.setMinimumSize(720, 520)
        self.resize(820, 600)
        self._ffmpeg_installing = False
        self._ffmpeg_catalog_loading = False
        self._ffmpeg_catalog_loaded = False
        self._focus_ffmpeg_install_when_ready = False
        self._ffmpeg_releases: list[FFmpegReleaseOption] = []
        self._managed_installation: ManagedFFmpegInstallation | None = None
        self._ffmpeg_status_override: tuple[bool, str] | None = None
        self._active_preview_backend = (
            active_preview_backend or settings.preview_backend
        )
        self.title_label = QLabel()
        self.title_label.setObjectName("dialogTitle")
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("mutedLabel")
        self.subtitle_label.setWordWrap(True)
        self.ffmpeg_about_card = QFrame()
        self.ffmpeg_about_card.setObjectName("settingsStatusCard")
        self.ffmpeg_about_title = QLabel()
        self.ffmpeg_about_title.setObjectName("panelTitle")
        self.ffmpeg_about_description = QLabel()
        self.ffmpeg_about_description.setObjectName("mutedLabel")
        self.ffmpeg_about_description.setWordWrap(True)
        ffmpeg_about_layout = QVBoxLayout(self.ffmpeg_about_card)
        ffmpeg_about_layout.setContentsMargins(14, 11, 14, 11)
        ffmpeg_about_layout.setSpacing(3)
        ffmpeg_about_layout.addWidget(self.ffmpeg_about_title)
        ffmpeg_about_layout.addWidget(self.ffmpeg_about_description)
        self.ffmpeg_edit = QLineEdit(settings.ffmpeg_path)
        self.ffmpeg_edit.setClearButtonEnabled(True)
        self.ffmpeg_browse_button = QPushButton()
        self.ffmpeg_test_button = QPushButton()
        self.ffmpeg_download_button = QPushButton()
        self.ffmpeg_download_button.setObjectName("primaryButton")
        self.ffmpeg_download_button.setMinimumWidth(180)
        self.ffmpeg_version_combo = QComboBox()
        self.ffmpeg_version_combo.setMinimumWidth(260)
        self.ffmpeg_refresh_versions_button = QPushButton()
        self.ffmpeg_update_button = QPushButton()
        self.ffmpeg_reinstall_button = QPushButton()
        self.ffmpeg_delete_button = QPushButton()
        self.ffmpeg_delete_button.setObjectName("dangerButton")
        self.ffmpeg_release_info = QLabel()
        self.ffmpeg_release_info.setObjectName("mutedLabel")
        self.ffmpeg_release_info.setWordWrap(True)
        self.ffmpeg_version_combo.addItem("", None)
        self.ffmpeg_version_combo.setEnabled(False)
        self.ffmpeg_status_card = QFrame()
        self.ffmpeg_status_card.setObjectName("settingsStatusCard")
        self.ffmpeg_status_dot = QFrame()
        self.ffmpeg_status_dot.setFixedSize(12, 12)
        self.ffmpeg_status_label = QLabel()
        self.ffmpeg_status_label.setObjectName("panelTitle")
        self.ffmpeg_status_detail = QLabel()
        self.ffmpeg_status_detail.setObjectName("mutedLabel")
        self.ffmpeg_status_detail.setWordWrap(True)
        self.managed_install_label = QLabel()
        self.managed_install_label.setObjectName("mutedLabel")
        self.managed_install_label.setWordWrap(True)
        self.output_edit = QLineEdit(settings.output_directory)
        self.output_edit.setClearButtonEnabled(True)
        self.output_browse_button = QPushButton()
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(RESOLUTIONS)
        self.resolution_combo.setCurrentText(settings.resolution_name)
        self.fps_combo = QComboBox()
        self.fps_combo.addItems([str(value) for value in EXPORT_FPS_OPTIONS])
        self.fps_combo.setCurrentText(str(settings.fps))
        self.codec_combo = QComboBox()
        for label, encoder in VIDEO_ENCODERS.items():
            self.codec_combo.addItem(label, encoder)
        self.codec_combo.setCurrentIndex(self.codec_combo.findData(settings.video_codec))
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setValue(settings.crf)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(ENCODING_PRESETS)
        self.preset_combo.setCurrentText(settings.preset)
        self.audio_bitrate_combo = QComboBox()
        self.audio_bitrate_combo.addItems(AUDIO_BITRATES)
        self.audio_bitrate_combo.setCurrentText(settings.audio_bitrate)
        self.work_mode_combo = QComboBox()
        self.work_mode_combo.addItem("", WORK_MODE_STABLE)
        self.work_mode_combo.addItem("", WORK_MODE_AUTO)
        self.work_mode_combo.addItem("", WORK_MODE_MAX_SPEED)
        self.work_mode_combo.setCurrentIndex(max(
            0, self.work_mode_combo.findData(settings.work_mode),
        ))
        self.work_mode_hint = QLabel()
        self.work_mode_hint.setObjectName("mutedLabel")
        self.work_mode_hint.setWordWrap(True)
        work_mode_panel = QWidget()
        work_mode_layout = QVBoxLayout(work_mode_panel)
        work_mode_layout.setContentsMargins(0, 0, 0, 0)
        work_mode_layout.setSpacing(4)
        work_mode_layout.addWidget(self.work_mode_combo)
        work_mode_layout.addWidget(self.work_mode_hint)
        self.work_mode_combo.currentIndexChanged.connect(
            self._update_work_mode_hint
        )
        for combo in (
            self.resolution_combo, self.fps_combo, self.codec_combo,
            self.preset_combo, self.audio_bitrate_combo, self.work_mode_combo,
        ):
            combo.setMinimumWidth(260)
        self.render_hint_label = QLabel()
        self.render_hint_label.setObjectName("mutedLabel")
        self.render_hint_label.setWordWrap(True)

        self.language_combo = QComboBox()
        self._populate_language_combo(language.value)
        self.appearance_hint = QLabel()
        self.appearance_hint.setObjectName("mutedLabel")
        self.appearance_hint.setWordWrap(True)
        self.language_pack_status = QLabel()
        self.language_pack_status.setObjectName("mutedLabel")
        self.language_pack_status.setWordWrap(True)
        self.language_pack_import_button = QPushButton()
        self.language_pack_remove_button = QPushButton()
        self.language_pack_folder_button = QPushButton()
        self.language_pack_reload_button = QPushButton()
        language_pack_controls = QWidget()
        language_pack_layout = QHBoxLayout(language_pack_controls)
        language_pack_layout.setContentsMargins(0, 0, 0, 0)
        language_pack_layout.setSpacing(6)
        language_pack_layout.addWidget(self.language_pack_import_button)
        language_pack_layout.addWidget(self.language_pack_remove_button)
        language_pack_layout.addWidget(self.language_pack_folder_button)
        language_pack_layout.addWidget(self.language_pack_reload_button)
        language_pack_layout.addStretch(1)
        language_pack_panel = QWidget()
        language_pack_panel_layout = QVBoxLayout(language_pack_panel)
        language_pack_panel_layout.setContentsMargins(0, 0, 0, 0)
        language_pack_panel_layout.setSpacing(5)
        language_pack_panel_layout.addWidget(language_pack_controls)
        language_pack_panel_layout.addWidget(self.language_pack_status)
        self.smooth_scroll_check = QCheckBox()
        self.smooth_scroll_check.setChecked(settings.smooth_scrolling)
        self.smooth_scroll_duration_slider = QSlider(Qt.Orientation.Horizontal)
        self.smooth_scroll_duration_slider.setRange(80, 420)
        self.smooth_scroll_duration_slider.setSingleStep(10)
        self.smooth_scroll_duration_slider.setPageStep(40)
        self.smooth_scroll_duration_slider.setValue(settings.smooth_scroll_duration_ms)
        self.smooth_scroll_duration_value = QLabel()
        self.smooth_scroll_duration_value.setObjectName("mutedLabel")
        self.smooth_scroll_duration_value.setMinimumWidth(92)
        self.smooth_scroll_duration_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        smooth_scroll_speed_row = QWidget()
        smooth_scroll_speed_layout = QHBoxLayout(smooth_scroll_speed_row)
        smooth_scroll_speed_layout.setContentsMargins(0, 0, 0, 0)
        smooth_scroll_speed_layout.addWidget(self.smooth_scroll_duration_slider, 1)
        smooth_scroll_speed_layout.addWidget(self.smooth_scroll_duration_value)
        self.smooth_scroll_check.toggled.connect(self._update_smooth_scroll_ui)
        self.smooth_scroll_duration_slider.valueChanged.connect(
            self._update_smooth_scroll_ui
        )
        self.preview_backend_combo = QComboBox()
        self.preview_backend_combo.addItem("", "gpu_layers")
        self.preview_backend_combo.addItem("", "cpu")
        self.preview_backend_combo.setCurrentIndex(
            max(0, self.preview_backend_combo.findData(settings.preview_backend))
        )
        self.preview_backend_combo.setMinimumWidth(260)
        self.preview_backend_hint = QLabel()
        self.preview_backend_hint.setObjectName("mutedLabel")
        self.preview_backend_hint.setWordWrap(True)
        self.preview_backend_restart_hint = QLabel()
        self.preview_backend_restart_hint.setObjectName("warningLabel")
        self.preview_backend_restart_hint.setWordWrap(True)
        self.preview_backend_restart_hint.hide()
        preview_backend_panel = QWidget()
        preview_backend_layout = QVBoxLayout(preview_backend_panel)
        preview_backend_layout.setContentsMargins(0, 0, 0, 0)
        preview_backend_layout.setSpacing(4)
        preview_backend_layout.addWidget(self.preview_backend_combo)
        preview_backend_layout.addWidget(self.preview_backend_hint)
        preview_backend_layout.addWidget(self.preview_backend_restart_hint)
        self.preview_backend_combo.currentIndexChanged.connect(
            self._update_preview_backend_restart_hint
        )

        self.lyrics_auto_attach_combo = QComboBox()
        for mode in LYRICS_AUTO_ATTACH_MODES:
            self.lyrics_auto_attach_combo.addItem("", mode)
        self.lyrics_auto_attach_combo.setCurrentIndex(max(
            0, self.lyrics_auto_attach_combo.findData(
                settings.lyrics_auto_attach_mode
            ),
        ))
        self.lyrics_auto_attach_combo.setMinimumWidth(260)
        self.lyrics_auto_attach_hint = QLabel()
        self.lyrics_auto_attach_hint.setObjectName("mutedLabel")
        self.lyrics_auto_attach_hint.setWordWrap(True)
        lyrics_auto_attach_panel = QWidget()
        lyrics_auto_attach_layout = QVBoxLayout(lyrics_auto_attach_panel)
        lyrics_auto_attach_layout.setContentsMargins(0, 0, 0, 0)
        lyrics_auto_attach_layout.setSpacing(4)
        lyrics_auto_attach_layout.addWidget(self.lyrics_auto_attach_combo)
        lyrics_auto_attach_layout.addWidget(self.lyrics_auto_attach_hint)

        self.export_notifications_check = QCheckBox()
        self.export_notifications_check.setChecked(
            settings.export_notifications_enabled
        )
        self.export_notification_mode_combo = QComboBox()
        self.export_notification_mode_combo.addItem("", "unfocused")
        self.export_notification_mode_combo.addItem("", "always")
        selected_notification_mode = (
            settings.export_notification_mode
            if settings.export_notification_mode in EXPORT_NOTIFICATION_MODES
            else "unfocused"
        )
        self.export_notification_mode_combo.setCurrentIndex(max(
            0,
            self.export_notification_mode_combo.findData(
                selected_notification_mode
            ),
        ))
        self.export_notification_mode_combo.setMinimumWidth(260)
        self.export_notify_visuals_check = QCheckBox()
        self.export_notify_audio_check = QCheckBox()
        self.export_notify_effects_check = QCheckBox()
        self.export_notify_encode_check = QCheckBox()
        self.export_notify_complete_check = QCheckBox()
        self.export_notify_failures_check = QCheckBox()
        notification_values = (
            settings.export_notify_visuals,
            settings.export_notify_audio,
            settings.export_notify_effects,
            settings.export_notify_encode,
            settings.export_notify_complete,
            settings.export_notify_failures,
        )
        for checkbox, checked in zip(
            self._export_notification_stage_checks(),
            notification_values,
            strict=True,
        ):
            checkbox.setChecked(checked)
        notification_stage_widget = QWidget()
        notification_stage_layout = QGridLayout(notification_stage_widget)
        notification_stage_layout.setContentsMargins(0, 0, 0, 0)
        notification_stage_layout.setHorizontalSpacing(18)
        notification_stage_layout.setVerticalSpacing(5)
        for index, checkbox in enumerate(
            self._export_notification_stage_checks()
        ):
            notification_stage_layout.addWidget(
                checkbox, index // 2, index % 2,
            )
        self.export_notification_hint = QLabel()
        self.export_notification_hint.setObjectName("mutedLabel")
        self.export_notification_hint.setWordWrap(True)
        notification_group = QGroupBox()
        notification_form = QFormLayout(notification_group)
        self.export_notification_master_label = QLabel()
        self.export_notification_mode_label = QLabel()
        self.export_notification_steps_label = QLabel()
        notification_form.addRow(
            self.export_notification_master_label,
            self.export_notifications_check,
        )
        notification_form.addRow(
            self.export_notification_mode_label,
            self.export_notification_mode_combo,
        )
        notification_form.addRow(
            self.export_notification_steps_label,
            notification_stage_widget,
        )
        notification_form.addRow("", self.export_notification_hint)
        self.export_notifications_check.toggled.connect(
            self._update_export_notification_ui
        )

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        self.button_box.accepted.connect(self._accept_settings)
        self.button_box.rejected.connect(self.reject)
        self.ffmpeg_browse_button.clicked.connect(self._browse_ffmpeg)
        self.ffmpeg_test_button.clicked.connect(self._test_ffmpeg)
        self.ffmpeg_download_button.clicked.connect(self.download_requested.emit)
        self.ffmpeg_update_button.clicked.connect(self.update_requested.emit)
        self.ffmpeg_reinstall_button.clicked.connect(self.reinstall_requested.emit)
        self.ffmpeg_delete_button.clicked.connect(self.delete_requested.emit)
        # A bound method, not a lambda: a lambda capturing self keeps the
        # dialog alive forever, and ~QApplication then crashes deleting it.
        self.ffmpeg_refresh_versions_button.clicked.connect(
            self._refresh_ffmpeg_catalog
        )
        self.ffmpeg_version_combo.currentIndexChanged.connect(
            self._update_ffmpeg_release_info
        )
        self.output_browse_button.clicked.connect(self._browse_output)
        self.ffmpeg_edit.textChanged.connect(self._refresh_ffmpeg_status)
        self.language_combo.currentIndexChanged.connect(self._update_language_pack_ui)
        self.language_pack_import_button.clicked.connect(self._import_language_pack)
        self.language_pack_remove_button.clicked.connect(self._remove_language_pack)
        self.language_pack_folder_button.clicked.connect(self._open_language_pack_folder)
        self.language_pack_reload_button.clicked.connect(self._reload_language_packs)

        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.addWidget(self.ffmpeg_edit, 1)
        ffmpeg_row.addWidget(self.ffmpeg_browse_button)
        version_row = QHBoxLayout()
        version_row.addWidget(self.ffmpeg_version_combo, 1)
        version_row.addWidget(self.ffmpeg_refresh_versions_button)
        ffmpeg_actions_widget = QWidget()
        ffmpeg_actions = QGridLayout(ffmpeg_actions_widget)
        ffmpeg_actions.setContentsMargins(0, 0, 0, 0)
        ffmpeg_actions.setHorizontalSpacing(6)
        ffmpeg_actions.setVerticalSpacing(6)
        ffmpeg_actions.addWidget(self.ffmpeg_download_button, 0, 0, 1, 2)
        ffmpeg_actions.addWidget(self.ffmpeg_update_button, 1, 0)
        ffmpeg_actions.addWidget(self.ffmpeg_reinstall_button, 1, 1)
        ffmpeg_actions.addWidget(self.ffmpeg_test_button, 2, 0)
        ffmpeg_actions.addWidget(self.ffmpeg_delete_button, 2, 1)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.output_browse_button)

        ffmpeg_group = QGroupBox()
        ffmpeg_form = QFormLayout(ffmpeg_group)
        self.ffmpeg_path_label = QLabel()
        self.ffmpeg_version_label = QLabel()
        self.output_label = QLabel()
        ffmpeg_form.addRow(self.ffmpeg_path_label, ffmpeg_row)
        ffmpeg_form.addRow(self.ffmpeg_version_label, version_row)
        ffmpeg_form.addRow("", self.ffmpeg_release_info)
        ffmpeg_form.addRow("", ffmpeg_actions_widget)
        ffmpeg_form.addRow("", self.managed_install_label)

        status_grid = QGridLayout(self.ffmpeg_status_card)
        status_grid.setContentsMargins(14, 12, 14, 12)
        status_grid.addWidget(self.ffmpeg_status_dot, 0, 0, 2, 1)
        status_grid.addWidget(self.ffmpeg_status_label, 0, 1)
        status_grid.addWidget(self.ffmpeg_status_detail, 1, 1)

        output_group = QGroupBox()
        output_form = QFormLayout(output_group)
        output_form.addRow(self.output_label, output_row)
        render_group = QGroupBox()
        render_form = QFormLayout(render_group)
        self.resolution_label = QLabel()
        self.fps_label = QLabel()
        self.codec_label = QLabel()
        self.crf_label = QLabel()
        self.preset_label = QLabel()
        self.audio_bitrate_label = QLabel()
        self.work_mode_label = QLabel()
        render_form.addRow(self.resolution_label, self.resolution_combo)
        render_form.addRow(self.fps_label, self.fps_combo)
        render_form.addRow(self.codec_label, self.codec_combo)
        render_form.addRow(self.crf_label, self.crf_spin)
        render_form.addRow(self.preset_label, self.preset_combo)
        render_form.addRow(self.audio_bitrate_label, self.audio_bitrate_combo)
        render_form.addRow(self.work_mode_label, work_mode_panel)
        render_form.addRow("", self.render_hint_label)
        app_group = QGroupBox()
        app_form = QFormLayout(app_group)
        self.language_label = QLabel()
        self.language_pack_label = QLabel()
        self.smooth_scroll_label = QLabel()
        self.smooth_scroll_speed_label = QLabel()
        self.preview_backend_label = QLabel()
        app_form.addRow(self.language_label, self.language_combo)
        app_form.addRow("", self.appearance_hint)
        app_form.addRow(self.language_pack_label, language_pack_panel)
        app_form.addRow(self.smooth_scroll_label, self.smooth_scroll_check)
        app_form.addRow(self.smooth_scroll_speed_label, smooth_scroll_speed_row)
        app_form.addRow(self.preview_backend_label, preview_backend_panel)
        content_group = QGroupBox()
        content_form = QFormLayout(content_group)
        self.lyrics_auto_attach_label = QLabel()
        content_form.addRow(
            self.lyrics_auto_attach_label, lyrics_auto_attach_panel,
        )
        # AutoMix analysis results are cached per file and never pruned on
        # their own (a changed file or analyzer version just stops matching).
        self.automix_cache_label = QLabel()
        self.automix_cache_usage_label = QLabel()
        self.automix_cache_usage_label.setObjectName("mutedLabel")
        self.automix_cache_clear_button = QPushButton()
        self.automix_cache_clear_button.clicked.connect(self._clear_automix_cache)
        automix_cache_row = QHBoxLayout()
        automix_cache_row.addWidget(self.automix_cache_usage_label, 1)
        automix_cache_row.addWidget(self.automix_cache_clear_button)
        content_form.addRow(self.automix_cache_label, automix_cache_row)

        # Reinstall and reset need the main window (download, closing the
        # program), so they close Settings and leave the action for the owner.
        self.maintenance_action = ""
        self.integrity_button = QPushButton()
        self.program_reinstall_button = QPushButton()
        self.reset_button = QPushButton()
        self.reset_button.setObjectName("dangerButton")
        self.integrity_label, self.program_reinstall_label, self.reset_label = QLabel(), QLabel(), QLabel()
        self.integrity_hint, self.program_reinstall_hint, self.reset_hint = QLabel(), QLabel(), QLabel()
        maintenance_group = QGroupBox()
        maintenance_form = QFormLayout(maintenance_group)
        for label, hint, button in (
            (self.integrity_label, self.integrity_hint, self.integrity_button),
            (self.program_reinstall_label, self.program_reinstall_hint, self.program_reinstall_button),
            (self.reset_label, self.reset_hint, self.reset_button),
        ):
            hint.setObjectName("mutedLabel")
            hint.setWordWrap(True)
            hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            button.setMinimumWidth(120)
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(hint, 1)
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
            maintenance_form.addRow(label, row_widget)
        self.integrity_button.clicked.connect(self._check_integrity)
        self.program_reinstall_button.clicked.connect(self._confirm_program_reinstall)
        self.reset_button.clicked.connect(self._confirm_reset)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")
        self.tabs.setDocumentMode(True)
        # Each tab scrolls on its own: the General tab alone needed ~900 px, so the
        # dialog opened taller than a laptop screen.
        self.general_page = self._scroll_page(app_group)
        self.export_page = self._scroll_page(output_group, render_group)
        self.ffmpeg_page = self._scroll_page(self.ffmpeg_about_card, self.ffmpeg_status_card, ffmpeg_group)
        self.content_page = self._scroll_page(content_group)
        self.notifications_page = self._scroll_page(notification_group)
        self.maintenance_page = self._scroll_page(maintenance_group)
        self.tabs.addTab(self.general_page, "")
        self.tabs.addTab(self.export_page, "")
        self.tabs.addTab(self.ffmpeg_page, "")
        self.tabs.addTab(self.content_page, "")
        self.tabs.addTab(self.notifications_page, "")
        self.tabs.addTab(self.maintenance_page, "")
        self.tabs.currentChanged.connect(self._settings_tab_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)
        layout.addWidget(self.tabs, 1)
        self.button_box.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryButton")
        layout.addWidget(self.button_box)
        for form in (app_form, content_form, render_form, output_form, ffmpeg_form, notification_form,
                     maintenance_form):
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setHorizontalSpacing(24)
            form.setVerticalSpacing(14)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.ffmpeg_group = ffmpeg_group
        self.output_group = output_group
        self.render_group = render_group
        self.notification_group = notification_group
        self.app_group = app_group
        self.content_group = content_group
        self.maintenance_group = maintenance_group
        self.retranslate()
        self._update_smooth_scroll_ui()
        self._update_work_mode_hint()
        self._update_preview_backend_restart_hint()
        self._refresh_ffmpeg_status()
        self._update_language_pack_ui()
        self._refresh_ffmpeg_manager_actions()

    @staticmethod
    def _scroll_page(*widgets: QWidget) -> QScrollArea:
        """One settings tab: its groups stacked in a vertically scrolling page."""
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(24)
        for widget in widgets:
            layout.addWidget(widget)
        layout.addStretch()
        page.setWidget(body)
        return page

    def open_ffmpeg_page(self) -> None:
        """Show the FFmpeg setup controls and highlight the install action."""
        self.tabs.setCurrentWidget(self.ffmpeg_page)
        self._focus_ffmpeg_install_when_ready = True
        self.request_ffmpeg_catalog()
        if self.ffmpeg_download_button.isEnabled():
            self.ffmpeg_download_button.setFocus(Qt.FocusReason.OtherFocusReason)

    @property
    def selected_ffmpeg_release(self) -> FFmpegReleaseOption | None:
        data = self.ffmpeg_version_combo.currentData()
        return data if isinstance(data, FFmpegReleaseOption) else None

    @property
    def recommended_ffmpeg_release(self) -> FFmpegReleaseOption | None:
        return next((release for release in self._ffmpeg_releases if release.recommended), None)

    @property
    def ffmpeg_releases(self) -> tuple[FFmpegReleaseOption, ...]:
        return tuple(self._ffmpeg_releases)

    @property
    def managed_installation(self) -> ManagedFFmpegInstallation | None:
        return self._managed_installation

    def request_ffmpeg_catalog(self, *, force: bool = False) -> None:
        """Ask the owner to refresh selectable versions once per dialog session."""
        if self._ffmpeg_catalog_loading or (self._ffmpeg_catalog_loaded and not force):
            return
        self._ffmpeg_catalog_loading = True
        self.ffmpeg_version_combo.setEnabled(False)
        self.ffmpeg_refresh_versions_button.setEnabled(False)
        self.ffmpeg_release_info.setText(
            "사용 가능한 FFmpeg 버전을 확인하고 있습니다…"
            if self.translator.is_korean else
            "Checking available FFmpeg versions…"
        )
        self.catalog_requested.emit(force)

    def _refresh_ffmpeg_catalog(self) -> None:
        self.request_ffmpeg_catalog(force=True)

    def set_ffmpeg_catalog(self, releases: list[FFmpegReleaseOption]) -> None:
        """Populate version choices with the app-tested branch first."""
        self._ffmpeg_releases = list(releases)
        self._ffmpeg_catalog_loading = False
        self._ffmpeg_catalog_loaded = bool(releases)
        self.ffmpeg_version_combo.blockSignals(True)
        self.ffmpeg_version_combo.clear()
        korean = self.translator.is_korean
        for release in releases:
            suffix = "(권장)" if korean and release.recommended else (
                " (Recommended)" if release.recommended else ""
            )
            label = f"{release.series}{suffix}"
            if release.series == "master":
                label = "master (개발판)" if korean else "master (Development)"
            self.ffmpeg_version_combo.addItem(label, release)
        self.ffmpeg_version_combo.blockSignals(False)
        self.ffmpeg_version_combo.setEnabled(bool(releases) and not self._ffmpeg_installing)
        self.ffmpeg_refresh_versions_button.setEnabled(not self._ffmpeg_installing)
        self._update_ffmpeg_release_info()
        self._refresh_ffmpeg_manager_actions()
        if self._focus_ffmpeg_install_when_ready and self.ffmpeg_download_button.isEnabled():
            self._focus_ffmpeg_install_when_ready = False
            self.ffmpeg_download_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_ffmpeg_catalog_error(self, message: str) -> None:
        self._ffmpeg_catalog_loading = False
        self._ffmpeg_catalog_loaded = False
        self.ffmpeg_refresh_versions_button.setEnabled(True)
        self.ffmpeg_release_info.setText(
            ("버전 목록을 불러오지 못했습니다. 새로고침을 눌러 다시 시도하세요.\n"
             if self.translator.is_korean else
             "Could not load the version list. Select Refresh to try again.\n")
            + message
        )
        self._refresh_ffmpeg_manager_actions()

    def set_managed_installation(
        self, installation: ManagedFFmpegInstallation | None,
    ) -> None:
        self._managed_installation = installation
        self._refresh_ffmpeg_manager_actions()
        self._refresh_ffmpeg_status()

    def _settings_tab_changed(self, _index: int) -> None:
        if self.tabs.currentWidget() is self.ffmpeg_page:
            self.request_ffmpeg_catalog()

    def _update_ffmpeg_release_info(self, _index: int = -1) -> None:
        release = self.selected_ffmpeg_release
        if release is None:
            return
        korean = self.translator.is_korean
        kind = (
            "Music Motion Studio 권장 버전" if korean else "Recommended for Music Motion Studio"
        ) if release.recommended else (
            "호환성 확인이 필요한 버전" if korean else "Compatibility should be verified"
        )
        published = release.published_at[:10] if release.published_at else "-"
        self.ffmpeg_release_info.setText(
            f"{kind} · 빌드 {release.build} · 배포 {published}"
            if korean else
            f"{kind} · build {release.build} · released {published}"
        )
        self.ffmpeg_release_info.setToolTip(release.notes)
        self._refresh_ffmpeg_manager_actions()

    def _refresh_ffmpeg_manager_actions(self) -> None:
        release = self.selected_ffmpeg_release
        current = self._managed_installation
        interactive = not self._ffmpeg_installing
        self.ffmpeg_download_button.setEnabled(interactive and release is not None)
        recommended = self.recommended_ffmpeg_release
        update_available = bool(
            current and recommended
            and (current.series != recommended.series or current.version != recommended.build)
        )
        reinstall_available = bool(
            current and any(
                release.series == current.series for release in self._ffmpeg_releases
            )
        )
        self.ffmpeg_update_button.setEnabled(interactive and update_available)
        self.ffmpeg_reinstall_button.setEnabled(interactive and reinstall_available)
        self.ffmpeg_delete_button.setEnabled(interactive and current is not None)

    @property
    def app_settings(self) -> AppSettings:
        """Return validated scalar values collected from dialog controls."""
        return AppSettings(
            ffmpeg_path=self.ffmpeg_edit.text(),
            output_directory=self.output_edit.text(),
            resolution_name=self.resolution_combo.currentText(),
            fps=int(self.fps_combo.currentText()),
            video_codec=self.codec_combo.currentData(),
            crf=self.crf_spin.value(),
            preset=self.preset_combo.currentText(),
            audio_bitrate=self.audio_bitrate_combo.currentText(),
            work_mode=str(self.work_mode_combo.currentData() or WORK_MODE_AUTO),
            smooth_scrolling=self.smooth_scroll_check.isChecked(),
            smooth_scroll_duration_ms=self.smooth_scroll_duration_slider.value(),
            preview_backend=str(
                self.preview_backend_combo.currentData() or "gpu_layers"
            ),
            export_notifications_enabled=(
                self.export_notifications_check.isChecked()
            ),
            export_notification_mode=str(
                self.export_notification_mode_combo.currentData()
                or "unfocused"
            ),
            export_notify_visuals=self.export_notify_visuals_check.isChecked(),
            export_notify_audio=self.export_notify_audio_check.isChecked(),
            export_notify_effects=self.export_notify_effects_check.isChecked(),
            export_notify_encode=self.export_notify_encode_check.isChecked(),
            export_notify_complete=self.export_notify_complete_check.isChecked(),
            export_notify_failures=self.export_notify_failures_check.isChecked(),
            lyrics_auto_attach_mode=str(
                self.lyrics_auto_attach_combo.currentData() or "always"
            ),
        )

    @property
    def selected_theme(self) -> Theme:
        """Return the selected theme preference."""
        return Theme.DARK

    @property
    def selected_language(self) -> Language | str:
        """Return the selected application language."""
        return str(self.language_combo.currentData() or Language.ENGLISH.value)

    def _populate_language_combo(self, selected_locale: str) -> None:
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        for option in self.translator.available_languages():
            self.language_combo.addItem(option.display_name, option.locale)
        index = self.language_combo.findData(selected_locale)
        if index < 0:
            index = self.language_combo.findData(Language.ENGLISH.value)
        self.language_combo.setCurrentIndex(max(0, index))
        self.language_combo.blockSignals(False)

    def _import_language_pack(self) -> None:
        korean = self.translator.is_korean
        path, _ = QFileDialog.getOpenFileName(
            self,
            "언어팩 가져오기" if korean else "Import language pack",
            str(Path.home()),
            "Music Motion Studio language pack (*.json);;JSON files (*.json)",
        )
        if not path:
            return
        try:
            pack = self.translator.pack_service.import_pack(Path(path))
        except (LanguagePackError, OSError) as error:
            QMessageBox.warning(
                self,
                "언어팩 오류" if korean else "Language pack error",
                str(error),
            )
            return
        self.translator.refresh_packs()
        self._populate_language_combo(pack.locale)
        self._update_language_pack_ui()
        QMessageBox.information(
            self,
            "언어팩 설치 완료" if korean else "Language pack installed",
            f"{pack.native_name} ({pack.locale})",
        )

    def _remove_language_pack(self) -> None:
        locale = str(self.language_combo.currentData() or "")
        pack = self.translator.pack_service.packs.get(locale)
        if pack is None:
            return
        korean = self.translator.is_korean
        response = QMessageBox.question(
            self,
            "언어팩 제거" if korean else "Remove language pack",
            f"{pack.native_name} 언어팩을 제거할까요?"
            if korean else f"Remove the {pack.native_name} language pack?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        try:
            self.translator.pack_service.remove_pack(locale)
        except (LanguagePackError, OSError) as error:
            QMessageBox.warning(
                self,
                "언어팩 오류" if korean else "Language pack error",
                str(error),
            )
            return
        self.translator.refresh_packs()
        self._populate_language_combo(Language.ENGLISH.value)
        self._update_language_pack_ui()

    def _open_language_pack_folder(self) -> None:
        korean = self.translator.is_korean
        directory = self.translator.pack_service.directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(
                self, "언어팩 폴더 오류" if korean else "Language pack folder error",
                str(error),
            )
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            QMessageBox.warning(
                self, "언어팩 폴더 오류" if korean else "Language pack folder error",
                "언어팩 폴더를 열 수 없습니다."
                if korean else "Could not open the language pack folder.",
            )

    def _reload_language_packs(self) -> None:
        selected = str(self.language_combo.currentData() or Language.ENGLISH.value)
        self.translator.refresh_packs()
        self._populate_language_combo(selected)
        self._update_language_pack_ui()

    def _update_language_pack_ui(self, _index: int = -1) -> None:
        locale = str(self.language_combo.currentData() or "")
        pack = self.translator.pack_service.packs.get(locale)
        korean = self.translator.is_korean
        self.language_pack_remove_button.setEnabled(pack is not None)
        if pack is not None:
            self.language_pack_status.setText(
                f"{pack.native_name} · {pack.locale} · v{pack.version} · {pack.author}"
            )
        else:
            error_count = len(self.translator.pack_service.errors)
            suffix = (
                f" · 오류 파일 {error_count}개 무시됨" if korean and error_count else
                f" · {error_count} invalid file(s) ignored" if error_count else ""
            )
            self.language_pack_status.setText(
                ("기본 내장 언어" if korean else "Built-in language") + suffix
            )

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self._ffmpeg_browse_title(), self.ffmpeg_edit.text(),
            "FFmpeg executable (ffmpeg.exe ffmpeg);;All files (*)",
        )
        if path:
            self.ffmpeg_edit.setText(path)

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, self._output_browse_title(), self.output_edit.text() or str(Path.home())
        )
        if path:
            self.output_edit.setText(path)

    def _test_ffmpeg(self) -> None:
        path = Path(self.ffmpeg_edit.text().strip())
        if not path.is_file():
            self._show_test_result(False, "FFmpeg executable was not found at the selected path.")
            return
        try:
            completed = subprocess.run(
                [str(path), "-version"], capture_output=True, text=True, timeout=10, check=False,
                **hidden_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._show_test_result(False, str(error))
            return
        version = completed.stdout.splitlines()[0] if completed.stdout else ""
        if completed.returncode != 0:
            self._show_test_result(False, version or "FFmpeg did not return version data.")
            return
        try:
            encoders = subprocess.run(
                [str(path), "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                **hidden_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self._show_test_result(False, str(error))
            return
        encoder = str(self.codec_combo.currentData())
        automatic = encoder == AUTO_VIDEO_ENCODER
        if automatic:
            encoder = VideoEncoderAdvisor.automatic_encoder()
        if encoders.returncode != 0 or encoder not in encoders.stdout.split():
            self._show_test_result(False, f"Selected encoder is unavailable: {encoder}")
            return
        prefix = "Auto-selected" if automatic else "Encoder available"
        self._show_test_result(True, f"{version}\n{prefix}: {encoder}")

    def _accept_settings(self) -> None:
        """Reject a visibly broken executable path instead of saving silent failure."""
        raw_path = self.ffmpeg_edit.text().strip()
        if raw_path and not Path(raw_path).is_file():
            korean = self.translator.is_korean
            QMessageBox.warning(
                self,
                "FFmpeg 경로 확인" if korean else "Check FFmpeg path",
                "선택한 FFmpeg 실행 파일을 찾을 수 없습니다. 경로를 지우거나 올바른 파일을 선택해 주세요."
                if korean else
                "The selected FFmpeg executable does not exist. Clear the path or choose a valid file.",
            )
            self.tabs.setCurrentWidget(self.ffmpeg_page)
            self.ffmpeg_edit.setFocus()
            return
        self.accept()

    def _update_smooth_scroll_ui(self, _value: object = None) -> None:
        enabled = self.smooth_scroll_check.isChecked()
        self.smooth_scroll_duration_slider.setEnabled(enabled)
        self.smooth_scroll_duration_value.setEnabled(enabled)
        duration = self.smooth_scroll_duration_slider.value()
        korean = self.translator.is_korean
        feel = (
            "빠름" if korean and duration <= 130 else
            "균형" if korean and duration <= 240 else
            "매우 부드러움" if korean else
            "Fast" if duration <= 130 else
            "Balanced" if duration <= 240 else
            "Very smooth"
        )
        self.smooth_scroll_duration_value.setText(f"{duration} ms · {feel}")

    def set_ffmpeg_installing(self, installing: bool) -> None:
        """Expose immediate, persistent feedback while the background install runs."""
        self._ffmpeg_installing = installing
        self.ffmpeg_version_combo.setEnabled(
            not installing and bool(self._ffmpeg_releases)
        )
        self.ffmpeg_refresh_versions_button.setEnabled(not installing)
        if installing:
            self._ffmpeg_status_override = (
                True,
                "FFmpeg를 다운로드하고 검증하는 중입니다. 진행 창에서 상태를 확인하세요."
                if self.translator.is_korean else
                "Downloading and verifying FFmpeg. Follow progress in the install window.",
            )
        else:
            self._ffmpeg_status_override = None
        self._refresh_ffmpeg_manager_actions()
        self._refresh_ffmpeg_status()

    def set_ffmpeg_install_error(self, message: str) -> None:
        """Keep an installation error visible inside Settings after a message box closes."""
        self._ffmpeg_installing = False
        self._ffmpeg_status_override = (False, message)
        self._refresh_ffmpeg_manager_actions()
        self._refresh_ffmpeg_status()

    def _refresh_ffmpeg_status(self, *_args: object) -> None:
        korean = self.translator.is_korean
        path = Path(self.ffmpeg_edit.text().strip()) if self.ffmpeg_edit.text().strip() else None
        if self._ffmpeg_status_override is not None:
            positive, detail = self._ffmpeg_status_override
            title = (
                "설치 진행 중" if self._ffmpeg_installing and korean else
                "Installing FFmpeg" if self._ffmpeg_installing else
                "설치 확인 필요" if korean else "FFmpeg needs attention"
            )
            color = COLORS["warning"] if positive else COLORS["danger"]
        elif path is not None and path.is_file():
            title = "FFmpeg 사용 가능" if korean else "FFmpeg configured"
            managed = self._managed_installation
            detail = (
                f"관리 설치 · {managed.series or 'FFmpeg'} · {managed.version}\n{path}"
                if korean and managed else
                f"Managed install · {managed.series or 'FFmpeg'} · {managed.version}\n{path}"
                if managed else str(path)
            )
            color = COLORS["success"]
        elif path is not None:
            title = "FFmpeg 파일 없음" if korean else "FFmpeg file not found"
            detail = str(path)
            color = COLORS["danger"]
        else:
            title = "FFmpeg가 설정되지 않음" if korean else "FFmpeg is not configured"
            detail = (
                "자동 다운로드를 사용하거나 설치된 ffmpeg.exe를 직접 선택하세요."
                if korean else
                "Use automatic download or select an existing ffmpeg executable."
            )
            color = COLORS["muted"]
        self.ffmpeg_status_dot.setStyleSheet(
            f"background: {color}; border: 0; border-radius: 6px;"
        )
        self.ffmpeg_status_label.setText(title)
        self.ffmpeg_status_detail.setText(detail)

    def _show_test_result(self, success: bool, detail: str) -> None:
        korean = self.translator.is_korean
        QMessageBox.information(
            self,
            "FFmpeg 확인" if korean else "FFmpeg check",
            ("FFmpeg을 확인했습니다.\n" if korean else "FFmpeg is ready.\n") + detail
            if success else
            ("FFmpeg 확인에 실패했습니다.\n" if korean else "FFmpeg check failed.\n") + detail,
        )

    def retranslate(self) -> None:
        """Set all labels for the currently active language."""
        korean = self.translator.is_korean
        self.setWindowTitle("설정" if korean else "Settings")
        self.title_label.setText("애플리케이션 설정" if korean else "Application settings")
        self.subtitle_label.setText(
            "화면 표시, 기본 출력 품질과 FFmpeg 설치를 관리합니다."
            if korean else
            "Manage appearance, default export quality, and the FFmpeg installation."
        )
        self.tabs.setTabText(0, "일반" if korean else "General")
        self.tabs.setTabText(1, "내보내기" if korean else "Export")
        self.tabs.setTabText(2, "FFmpeg")
        self.tabs.setTabText(3, "콘텐츠" if korean else "Content")
        self.tabs.setTabText(4, "알림" if korean else "Notifications")
        self.tabs.setTabText(5, "유지 관리" if korean else "Maintenance")
        self.maintenance_group.setTitle("프로그램 관리" if korean else "Program maintenance")
        self.integrity_label.setText("무결성 검사" if korean else "Integrity check")
        self.integrity_hint.setText(
            "설치된 프로그램 파일이 빠지거나 손상되지 않았는지 공식 배포본의 SHA-256 목록과 비교합니다."
            if korean else
            "Compares the installed program files with the official build's SHA-256 list to find "
            "missing or damaged files."
        )
        self.integrity_button.setText("검사 시작" if korean else "Check now")
        self.program_reinstall_label.setText("강제 재설치" if korean else "Force reinstall")
        self.program_reinstall_hint.setText(
            "GitHub에서 최신 공식 Setup을 내려받아 버전과 관계없이 다시 설치합니다. "
            "프로젝트와 설정은 유지됩니다."
            if korean else
            "Downloads the latest official Setup from GitHub and installs it again regardless of "
            "version. Projects and settings are kept."
        )
        self.program_reinstall_button.setText("재설치" if korean else "Reinstall")
        self.reset_label.setText("프로그램 초기화" if korean else "Reset program")
        self.reset_hint.setText(
            "모든 설정·최근 프로젝트 목록·창 배치와 분석·미리보기 캐시를 지우고 처음 설치한 상태로 "
            "다시 시작합니다. 프로젝트 파일, 사용자 프리셋, 언어팩, 설치된 FFmpeg는 지우지 않습니다."
            if korean else
            "Clears every setting, the recent projects list, the window layout and the analysis and "
            "preview caches, then restarts as freshly installed. Project files, user presets, language "
            "packs and the installed FFmpeg are kept."
        )
        self.reset_button.setText("초기화" if korean else "Reset")
        self.ffmpeg_about_title.setText(
            "FFmpeg이란?" if korean else "What is FFmpeg?"
        )
        self.ffmpeg_about_description.setText(
            "FFmpeg은 영상과 음성을 읽고, 결합하고, 압축하여 최종 동영상 파일로 만드는 "
            "오픈 소스 미디어 처리 엔진입니다. Music Motion Studio에서는 프로젝트 편집은 "
            "FFmpeg 없이도 가능하지만 동영상 내보내기에는 필요합니다. 잘 모르겠다면 "
            "권장 버전을 선택해 설치하면 되며, 프로그램 전용 폴더에 안전하게 설치됩니다."
            if korean else
            "FFmpeg is an open-source media engine that reads, combines, and compresses video "
            "and audio into the final video file. You can edit a Music Motion Studio project without "
            "FFmpeg, but video export requires it. If you are unsure, install the recommended "
            "version; it is safely kept in the application-only folder."
        )
        self.ffmpeg_group.setTitle("FFmpeg 실행 파일" if korean else "FFmpeg executable")
        self.output_group.setTitle("출력 위치" if korean else "Output location")
        self.render_group.setTitle("기본 렌더링" if korean else "Default rendering")
        self.notification_group.setTitle(
            "내보내기 알림" if korean else "Export notifications"
        )
        self.app_group.setTitle("앱" if korean else "Application")
        self.content_group.setTitle("콘텐츠 추가" if korean else "Adding content")
        self.automix_cache_label.setText("AutoMix 분석 캐시" if korean else "AutoMix analysis cache")
        self.automix_cache_clear_button.setText("비우기" if korean else "Clear")
        self.automix_cache_clear_button.setToolTip(
            "저장된 곡 분석 결과를 지웁니다. 다음 AutoMix 때 다시 분석합니다."
            if korean else
            "Delete saved track analyses; AutoMix analyzes them again next time."
        )
        self._refresh_automix_cache_usage()
        self.lyrics_auto_attach_label.setText(
            "가사·자막 파일 자동 연결" if korean else "Auto-attach lyric files"
        )
        self.lyrics_auto_attach_combo.setItemText(
            0, "항상 함께 추가 (권장)" if korean else "Always attach (Recommended)",
        )
        self.lyrics_auto_attach_combo.setItemText(
            1, "물어보기" if korean else "Ask each time",
        )
        self.lyrics_auto_attach_combo.setItemText(
            2, "추가하지 않음" if korean else "Never attach",
        )
        self.lyrics_auto_attach_hint.setText(
            "노래를 추가할 때 같은 폴더에 파일 이름이 같은 .lrc/.srt/.vtt/.ttml 파일이 있으면 "
            "가사·자막으로 함께 연결합니다. '추가하지 않음'이 아니면, 이름이 비슷한 파일이 "
            "있을 때는 설정과 관계없이 항상 추가 여부를 물어봅니다. 같은 곡의 가사 파일이 여러 개면 추가할 파일을 선택합니다."
            if korean else
            "When you add a song, a .lrc/.srt/.vtt/.ttml file with the same name in the "
            "same folder is attached as lyrics. Unless set to Never, a similarly "
            "named file always prompts before it is attached, regardless of this "
            "setting. Multiple lyric files for the same song always open a file chooser."
        )
        self.ffmpeg_path_label.setText("FFmpeg 경로" if korean else "FFmpeg path")
        self.ffmpeg_version_label.setText("설치 버전" if korean else "Version to install")
        self.output_label.setText("기본 출력 폴더" if korean else "Default output folder")
        self.resolution_label.setText("해상도" if korean else "Resolution")
        self.fps_label.setText("FPS")
        self.codec_label.setText("비디오 인코더" if korean else "Video encoder")
        automatic_index = self.codec_combo.findData(AUTO_VIDEO_ENCODER)
        if automatic_index >= 0:
            self.codec_combo.setItemText(
                automatic_index,
                "자동 선택 (권장)" if korean else "Automatic (Recommended)",
            )
        self.crf_label.setText("CRF (낮을수록 고화질)" if korean else "CRF (lower is higher quality)")
        self.preset_label.setText("인코딩 Preset" if korean else "Encoding preset")
        self.audio_bitrate_label.setText(
            "오디오 품질 (AAC)" if korean else "Audio quality (AAC)"
        )
        self.work_mode_label.setText("작업 모드" if korean else "Work mode")
        self.work_mode_combo.setItemText(
            0, "안정" if korean else "Stable",
        )
        self.work_mode_combo.setItemText(
            1, "자동 (권장)" if korean else "Automatic (Recommended)",
        )
        self.work_mode_combo.setItemText(
            2, "최대 속도" if korean else "Maximum speed",
        )
        self.language_label.setText("언어" if korean else "Language")
        self.appearance_hint.setText(
            "Music Motion Studio는 일관된 편집 환경을 위해 다크 스튜디오 테마만 제공합니다."
            if korean else
            "Music Motion Studio uses one dark studio theme so the editing workspace stays consistent."
        )
        self.language_pack_label.setText("외부 언어팩" if korean else "External language packs")
        self.language_pack_import_button.setText("가져오기" if korean else "Import")
        self.language_pack_remove_button.setText("제거" if korean else "Remove")
        self.language_pack_folder_button.setText("폴더 열기" if korean else "Open folder")
        self.language_pack_reload_button.setText("새로고침" if korean else "Reload")
        self.smooth_scroll_label.setText("스크롤 동작" if korean else "Scrolling")
        self.smooth_scroll_speed_label.setText(
            "부드러움" if korean else "Smoothness"
        )
        self.smooth_scroll_check.setText(
            "부드러운 스크롤 사용" if korean else "Enable smooth scrolling"
        )
        self.smooth_scroll_duration_slider.setToolTip(
            "값이 높을수록 더 천천히 부드럽게 이동합니다."
            if korean else
            "Higher values scroll more slowly and smoothly."
        )

        self.preview_backend_label.setText(
            "미리보기 렌더러" if korean else "Preview renderer"
        )
        self.preview_backend_combo.setItemText(
            0, "GPU 레이어 (권장)" if korean else "GPU layers (Recommended)",
        )
        self.preview_backend_combo.setItemText(
            1, "CPU 호환 모드" if korean else "CPU compatibility mode",
        )
        self.preview_backend_hint.setText(
            "GPU 레이어는 합성·캐시·자동 품질 조절을 사용합니다. 그래픽 드라이버와 충돌하는 경우에만 CPU 모드를 선택하세요. 렌더러는 미리보기 화면에서 변경할 수 없습니다."
            if korean else
            "GPU layers use accelerated composition, caching, and adaptive quality. Select CPU only for graphics-driver compatibility. The renderer cannot be changed from Preview."
        )
        self.preview_backend_restart_hint.setText(
            "미리보기 렌더러 변경은 프로그램 재시작 후 적용됩니다. 프로그램을 완전히 종료한 후 다시 실행해 주세요. 현재 실행 중인 미리보기에는 영향을 주지 않습니다."
            if korean else
            "The preview renderer change takes effect after fully closing and restarting the program. It does not affect previews in the current session."
        )
        self.export_notification_master_label.setText(
            "알림 사용" if korean else "Notifications"
        )
        self.export_notifications_check.setText(
            "내보내기 진행 알림 사용"
            if korean else "Enable export progress notifications"
        )
        self.export_notification_mode_label.setText(
            "표시 조건" if korean else "Show when"
        )
        self.export_notification_mode_combo.setItemText(
            0,
            "프로그램이 포커스 중이 아닐 때만"
            if korean else "Only when the app is not focused",
        )
        self.export_notification_mode_combo.setItemText(
            1, "항상 표시" if korean else "Always show",
        )
        self.export_notification_steps_label.setText(
            "알림 단계" if korean else "Notify for"
        )
        stage_names = (
            ("화면 준비", "오디오 준비", "효과 준비", "영상 만들기", "완료", "오류 및 취소")
            if korean else
            ("Visual preparation", "Audio preparation", "Effects preparation", "Create video", "Completion", "Errors and cancellation")
        )
        for checkbox, text in zip(
            self._export_notification_stage_checks(), stage_names, strict=True,
        ):
            checkbox.setText(text)
        self.export_notification_hint.setText(
            "선택한 단계가 시작될 때 Windows 알림을 한 번 표시합니다. 운영체제의 알림 설정에서 Music Motion Studio 알림이 차단되어 있으면 표시되지 않을 수 있습니다."
            if korean else
            "Shows one Windows notification when each selected stage starts. Notifications may not appear if Music Motion Studio is blocked in system notification settings."
        )
        self.ffmpeg_browse_button.setText("찾아보기" if korean else "Browse")
        self.ffmpeg_test_button.setText("확인" if korean else "Check")
        self.ffmpeg_download_button.setText(
            "선택 버전 다운로드 및 설치" if korean else "Download and install selected version"
        )
        self.ffmpeg_refresh_versions_button.setText(
            "새로고침" if korean else "Refresh"
        )
        self.ffmpeg_update_button.setText("업데이트" if korean else "Update")
        self.ffmpeg_reinstall_button.setText("재설치" if korean else "Reinstall")
        self.ffmpeg_delete_button.setText("삭제" if korean else "Delete")
        self.ffmpeg_download_button.setToolTip(
            "콤보 상자에서 선택한 버전을 다운로드하고 SHA-256 검증 후 적용합니다."
            if korean else
            "Download the selected version, verify SHA-256, and activate it."
        )
        self.ffmpeg_update_button.setToolTip(
            "현재 관리 설치본을 이 Music Motion Studio 버전의 권장 FFmpeg로 교체합니다."
            if korean else
            "Replace the managed install with the FFmpeg version recommended for this Music Motion Studio release."
        )
        self.ffmpeg_reinstall_button.setToolTip(
            "현재 관리 중인 버전을 다시 다운로드·검증하여 교체합니다."
            if korean else
            "Download, verify, and replace the currently managed version."
        )
        self.ffmpeg_delete_button.setToolTip(
            "Music Motion Studio가 설치한 FFmpeg만 삭제합니다. 직접 지정한 외부 FFmpeg는 삭제하지 않습니다."
            if korean else
            "Delete only FFmpeg installed by Music Motion Studio. External FFmpeg files are never removed."
        )
        self.managed_install_label.setText(
            "Windows 64비트용 BtbN FFmpeg GPL 배포본을 앱 전용 폴더에 내려받고 SHA-256으로 검증합니다."
            if korean else
            "Downloads the BtbN FFmpeg GPL Windows 64-bit build into the app-only folder and verifies SHA-256."
        )
        self.ffmpeg_edit.setPlaceholderText(
            "예: C:\\Tools\\ffmpeg\\bin\\ffmpeg.exe" if korean else
            "Example: C:\\Tools\\ffmpeg\\bin\\ffmpeg.exe"
        )
        self.output_edit.setPlaceholderText(
            "비워 두면 기본 비디오 폴더 사용" if korean else
            "Leave empty to use the default Videos folder"
        )
        self.render_hint_label.setText(
            "권장 시작값: Full HD, 30 FPS, H.264, CRF 18, preset medium. "
            "CRF가 낮을수록 화질과 파일 크기가 증가합니다."
            if korean else
            "Recommended starting point: Full HD, 30 FPS, H.264, CRF 18, preset medium. "
            "Lower CRF increases quality and file size."
        )
        self.codec_combo.setToolTip(
            "자동 선택은 NVIDIA GPU가 감지되면 NVENC를 사용하고, 그렇지 않으면 CPU H.264를 사용합니다. GPU 인코더는 그래픽 드라이버와 FFmpeg 지원이 필요합니다."
            if korean else
            "Automatic selection uses NVENC when an NVIDIA GPU is detected, otherwise CPU H.264. GPU encoders require compatible graphics drivers and FFmpeg support."
        )
        self.preset_combo.setToolTip(
            "느린 preset은 일반적으로 더 작은 파일을 만들지만 인코딩 시간이 길어집니다."
            if korean else
            "Slower presets generally produce smaller files but take longer to encode."
        )
        self.output_browse_button.setText("찾아보기" if korean else "Browse")
        self._update_smooth_scroll_ui()
        self._update_work_mode_hint()
        self.button_box.button(QDialogButtonBox.StandardButton.Save).setText(
            "저장" if korean else "Save"
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "취소" if korean else "Cancel"
        )
        self._refresh_ffmpeg_status()
        if self._ffmpeg_releases:
            # Rebuild labels such as "(권장)" after a live language switch.
            releases = list(self._ffmpeg_releases)
            selected = self.selected_ffmpeg_release
            self.set_ffmpeg_catalog(releases)
            if selected is not None:
                index = next((
                    row for row in range(self.ffmpeg_version_combo.count())
                    if self.ffmpeg_version_combo.itemData(row) == selected
                ), -1)
                if index >= 0:
                    self.ffmpeg_version_combo.setCurrentIndex(index)
        self._update_language_pack_ui()
        self._update_preview_backend_restart_hint()
        self._update_export_notification_ui()

    def _export_notification_stage_checks(self) -> tuple[QCheckBox, ...]:
        """Return stage controls in their stable display and persistence order."""
        return (
            self.export_notify_visuals_check,
            self.export_notify_audio_check,
            self.export_notify_effects_check,
            self.export_notify_encode_check,
            self.export_notify_complete_check,
            self.export_notify_failures_check,
        )

    def _update_export_notification_ui(self, _checked: object = None) -> None:
        """Disable subordinate options without discarding their selections."""
        enabled = self.export_notifications_check.isChecked()
        self.export_notification_mode_combo.setEnabled(enabled)
        self.export_notification_hint.setEnabled(enabled)
        for checkbox in self._export_notification_stage_checks():
            checkbox.setEnabled(enabled)

    def _update_work_mode_hint(self, _index: int = -1) -> None:
        """Explain the resource and stability trade-off of preparation modes."""
        korean = self.translator.is_korean
        mode = str(self.work_mode_combo.currentData() or WORK_MODE_AUTO)
        if mode == WORK_MODE_STABLE:
            text = (
                "동시 작업을 1개로 제한합니다. 속도는 느리지만 저사양 PC, 4K 작업 또는 메모리가 부족한 환경에 가장 안전합니다."
                if korean else
                "Limits preparation to one concurrent job. Slower, but safest for low-end PCs, 4K projects, or limited memory."
            )
        elif mode == WORK_MODE_MAX_SPEED:
            text = (
                "곡과 레이어 준비에 더 많은 CPU 작업을 사용합니다. 속도는 빠르지만 CPU·메모리 사용량이 증가하며 4K에서는 안전 상한이 자동 적용됩니다."
                if korean else
                "Uses more CPU jobs for track and layer preparation. Faster, with higher CPU and memory use; a safety cap still applies at 4K."
            )
        else:
            text = (
                "PC 성능, 해상도와 작업 수를 기준으로 병렬 처리량을 자동 조절합니다. 대부분의 사용자에게 권장됩니다."
                if korean else
                "Adjusts concurrency from PC performance, resolution, and workload. Recommended for most users."
            )
        self.work_mode_hint.setText(text)
        self.work_mode_combo.setToolTip(text)

    def _update_preview_backend_restart_hint(self, _index: int = -1) -> None:
        """Explain that renderer changes are intentionally deferred to restart."""
        changed = (
            str(self.preview_backend_combo.currentData() or "gpu_layers")
            != self._active_preview_backend
        )
        self.preview_backend_restart_hint.setVisible(changed)

    def _refresh_automix_cache_usage(self) -> None:
        from app.automix.cache import cache_usage

        korean = self.translator.is_korean
        entries, size = cache_usage()
        megabytes = size / (1024 * 1024)
        self.automix_cache_usage_label.setText(
            f"분석 {entries}개 · {megabytes:.1f} MB" if korean else f"{entries} analyses · {megabytes:.1f} MB"
        )
        self.automix_cache_clear_button.setEnabled(size > 0)

    def _clear_automix_cache(self) -> None:
        from app.automix.cache import clear_caches

        clear_caches()
        self._refresh_automix_cache_usage()

    def _request_maintenance(self, action: str) -> None:
        self.maintenance_action = action
        self.reject()

    def _check_integrity(self) -> None:
        from app.services.maintenance_service import installation_root, verify_installation

        korean = self.translator.is_korean
        title = "무결성 검사" if korean else "Integrity check"
        root = installation_root()
        if root is None:
            QMessageBox.information(
                self, title,
                "소스 코드에서 실행 중이라 비교할 설치 파일 목록이 없습니다. 설치된 프로그램에서 사용할 수 있습니다."
                if korean else
                "Running from source, so there is no installed file list to compare. "
                "This check works in the installed program.",
            )
            return
        progress = QProgressDialog(
            "설치된 파일을 확인하는 중…" if korean else "Checking installed files…",
            "취소" if korean else "Cancel", 0, 1000, self,
        )
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        def step(fraction: float) -> bool:
            progress.setValue(int(fraction * 1000))
            QApplication.processEvents()
            return not progress.wasCanceled()

        # ponytail: hashes on the UI thread, repainting between files; move to a
        # worker thread if one large DLL ever stalls the progress bar noticeably.
        try:
            report = verify_installation(root, step)
        except (OSError, ValueError, KeyError, TypeError):
            progress.close()
            self._offer_program_reinstall(
                title,
                "설치 파일 목록(integrity.json)을 읽을 수 없습니다. 이전 버전으로 설치되었거나 파일이 손상되었습니다."
                if korean else
                "The installed file list (integrity.json) could not be read. The program was installed "
                "by an older version or the file is damaged.",
            )
            return
        progress.close()
        if report is None:
            return
        if report.ok:
            QMessageBox.information(
                self, title,
                f"설치 파일 {report.checked}개가 모두 정상입니다."
                if korean else f"All {report.checked} installed files are intact.",
            )
            return
        problems = (
            [("없음: " if korean else "Missing: ") + name for name in report.missing]
            + [("손상: " if korean else "Damaged: ") + name for name in report.damaged]
        )
        listed = "\n".join(problems[:10])
        if len(problems) > 10:
            listed += f"\n… 외 {len(problems) - 10}개" if korean else f"\n… and {len(problems) - 10} more"
        self._offer_program_reinstall(
            title,
            (f"설치 파일 {report.checked}개 중 {len(problems)}개에 문제가 있습니다.\n\n{listed}"
             if korean else
             f"{len(problems)} of {report.checked} installed files have problems.\n\n{listed}"),
        )

    def _offer_program_reinstall(self, title: str, problem: str) -> None:
        korean = self.translator.is_korean
        answer = QMessageBox.warning(
            self, title,
            problem + ("\n\n강제 재설치로 복구할까요?" if korean else "\n\nRepair it with a forced reinstall?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._request_maintenance("reinstall")

    def _confirm_program_reinstall(self) -> None:
        korean = self.translator.is_korean
        answer = QMessageBox.question(
            self,
            "강제 재설치" if korean else "Force reinstall",
            "GitHub에서 최신 공식 Setup을 내려받아 다시 설치합니다.\n"
            "다운로드와 SHA-256 검증이 끝나면 프로그램을 종료하고 Setup을 실행합니다.\n"
            "프로젝트와 설정은 그대로 유지됩니다. 계속할까요?"
            if korean else
            "Download the latest official Setup from GitHub and install it again.\n"
            "After the download is SHA-256 verified, Music Motion Studio closes and runs Setup.\n"
            "Projects and settings are kept. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._request_maintenance("reinstall")

    def _confirm_reset(self) -> None:
        korean = self.translator.is_korean
        answer = QMessageBox.warning(
            self,
            "프로그램 초기화" if korean else "Reset program",
            "프로그램을 처음 설치한 상태로 되돌립니다.\n\n"
            "지워지는 항목: 모든 설정, 최근 프로젝트 목록, 창 배치, AutoMix 편집기 프리셋, "
            "AutoMix 분석 캐시, 미리보기 캐시, 내려받은 업데이트 파일\n"
            "유지되는 항목: 프로젝트 파일, 사용자 프리셋, 언어팩, 설치된 FFmpeg\n\n"
            "프로그램이 종료된 뒤 다시 시작됩니다. 계속할까요?"
            if korean else
            "Return the program to its freshly installed state.\n\n"
            "Cleared: every setting, the recent projects list, the window layout, AutoMix editor "
            "presets, the AutoMix analysis cache, the preview cache and downloaded updates\n"
            "Kept: project files, user presets, language packs and the installed FFmpeg\n\n"
            "Music Motion Studio closes and starts again. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._request_maintenance("reset")

    def _ffmpeg_browse_title(self) -> str:
        return "FFmpeg 실행 파일 선택" if self.translator.is_korean else "Choose FFmpeg executable"

    def _output_browse_title(self) -> str:
        return "기본 출력 폴더 선택" if self.translator.is_korean else "Choose default output folder"
