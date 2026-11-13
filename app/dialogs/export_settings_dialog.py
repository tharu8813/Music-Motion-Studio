"""Project-centered export configuration, with video/audio/subtitle options."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut, open_help
from app.dialogs.adaptive_dialog import AdaptiveDialog
from app.services.app_settings_service import (
    AUDIO_BITRATES,
    ENCODING_PRESETS,
    RESOLUTIONS,
    VIDEO_ENCODERS,
    AppSettings,
)
from app.utils.i18n import Language, Translator
from app.services.video_encoder_service import (
    AUTO_VIDEO_ENCODER, DEFAULT_NATIVE_NVENC_PRESET, NATIVE_NVENC_PRESETS,
)
from app.services.export_validation_service import EXPORT_FPS_OPTIONS
from app.utils.time_format import format_clock
from app.services.export_formats import VIDEO_FORMATS, AUDIO_FORMATS, SUBTITLE_FORMATS
from app.services.export_file_naming import available_export_path


class ExportSettingsDialog(AdaptiveDialog):
    """Collect render quality and the final output file in one place."""

    QUALITY_PROFILES = {
        "balanced": (18, "medium", "192k"),
        "fast": (22, "veryfast", "192k"),
        "high": (15, "medium", "320k"),
        "compact": (24, "medium", "192k"),
    }
    NATIVE_QUALITY_PROFILES = {
        "balanced": (0, DEFAULT_NATIVE_NVENC_PRESET),
        "fast": (24, "p1"),
        "high": (17, "p5"),
        "compact": (28, DEFAULT_NATIVE_NVENC_PRESET),
    }

    def __init__(self, settings: AppSettings, track_count: int, duration_seconds: float,
                 translator: Translator, default_output_path: str | Path,
                 parent: QWidget | None = None,
                 canvas_size: tuple[int, int] | None = None,
                 project_info: dict[str, str] | None = None,
                 project_thumbnail: QImage | None = None,
                 initial_export: dict[str, str] | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("other", "export"), translator)
        self.translator = translator
        self._base_settings = settings
        self.canvas_size = canvas_size
        self.project_info = project_info or {}
        self._initial_export = initial_export or {}
        self._applying_quality_profile = False
        self._filename_user_edited = False
        self.setMinimumWidth(920)
        self.setSizeGripEnabled(True)
        self.quality_mode_combo = QComboBox()
        self.quality_mode_label = QLabel()
        self.quality_description_label = QLabel()
        self.quality_description_label.setObjectName("mutedLabel")
        self.quality_description_label.setWordWrap(True)
        self.workload_label = QLabel()
        self.workload_label.setObjectName("mutedLabel")
        self.workload_label.setWordWrap(True)
        self.resolution_combo = QComboBox()
        self._populate_resolutions(settings)
        self.fps_combo = QComboBox()
        self.fps_combo.addItems([str(value) for value in EXPORT_FPS_OPTIONS])
        self.fps_combo.setCurrentText(str(settings.fps))
        self.codec_combo = QComboBox()
        for label, codec in VIDEO_ENCODERS.items():
            self.codec_combo.addItem(label, codec)
        self.codec_combo.setCurrentIndex(max(0, self.codec_combo.findData(settings.video_codec)))
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setValue(settings.crf)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(ENCODING_PRESETS)
        self.preset_combo.setCurrentText(settings.preset)
        self.native_codec_combo = QComboBox()
        self.native_codec_combo.addItem("H.264 (NVENC)")
        self.native_codec_combo.setEnabled(False)
        self.native_cq_spin = QSpinBox()
        self.native_cq_spin.setRange(0, 51)
        self.native_cq_spin.setValue(settings.native_nvenc_cq)
        self.native_preset_combo = QComboBox()
        for preset in NATIVE_NVENC_PRESETS:
            self.native_preset_combo.addItem(preset.upper(), preset)
        preset_index = self.native_preset_combo.findData(settings.native_nvenc_preset)
        if preset_index < 0:
            preset_index = self.native_preset_combo.findData(DEFAULT_NATIVE_NVENC_PRESET)
        self.native_preset_combo.setCurrentIndex(preset_index)
        self.audio_bitrate_combo = QComboBox()
        self.audio_bitrate_combo.addItems(AUDIO_BITRATES)
        self.audio_bitrate_combo.setCurrentText(settings.audio_bitrate)
        self.advanced_check = QCheckBox()
        default_path = Path(default_output_path).expanduser().resolve()
        self._auto_filename_base = default_path.stem
        self._confirmed_output_path: Path | None = None
        self.export_type_combo = QComboBox()
        self.export_type_combo.addItem("", "video")
        self.export_type_combo.addItem("", "audio")
        self.export_type_combo.addItem("", "subtitles")
        self.export_type_buttons: list[QToolButton] = []
        self.export_type_button_group = QButtonGroup(self)
        self.export_type_button_group.setExclusive(True)
        for index, icon_name in enumerate((
            QStyle.StandardPixmap.SP_MediaPlay,
            QStyle.StandardPixmap.SP_MediaVolume,
            QStyle.StandardPixmap.SP_FileIcon,
        )):
            button = QToolButton(self)
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setIcon(self.style().standardIcon(icon_name))
            button.setMinimumHeight(40)
            button.setChecked(index == 0)
            button.clicked.connect(
                lambda _checked=False, selected=index:
                self.export_type_combo.setCurrentIndex(selected)
            )
            self.export_type_button_group.addButton(button, index)
            self.export_type_buttons.append(button)
        self.format_combo = QComboBox()
        self.filename_edit = QLineEdit(default_path.stem)
        self.output_path_edit = QLineEdit(str(default_path.parent))
        self.output_directory_edit = self.output_path_edit
        self.export_type_label = QLabel()
        self.format_label = QLabel()
        self.filename_label = QLabel()
        self.output_directory_label = QLabel()
        self.output_filename_label = QLabel()
        self.output_filename_label.setObjectName("mutedLabel")
        self.filename_warning_label = QLabel()
        self.filename_warning_label.setObjectName("warningLabel")
        self.filename_warning_label.setWordWrap(True)
        self.filename_warning_label.hide()
        self.output_path_edit.setMinimumWidth(330)
        self.output_browse_button = QPushButton()
        self.output_browse_button.clicked.connect(self._browse_output_path)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("infoCallout")
        self.summary_label.setWordWrap(True)
        # Retained only for backwards-compatible callers; never shown.
        self.save_default_check = QCheckBox()
        self.optimize_button = QPushButton()
        self.optimize_button.clicked.connect(self._apply_balanced_optimization)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.button_box.accepted.connect(self._accept_if_valid)
        self.button_box.rejected.connect(self.reject)

        quality_group = QGroupBox()
        quality_layout = QVBoxLayout(quality_group)
        quality_form = QFormLayout()
        quality_form.addRow(self.quality_mode_label, self.quality_mode_combo)
        quality_layout.addLayout(quality_form)
        quality_layout.addWidget(self.quality_description_label)
        quality_actions = QHBoxLayout()
        quality_actions.addWidget(self.optimize_button)
        quality_actions.addStretch(1)
        quality_layout.addLayout(quality_actions)
        quality_layout.addWidget(self.advanced_check)

        render_group = QGroupBox()
        render_form = QFormLayout(render_group)
        self.engine_label = QLabel()
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("", "native_gpu")
        self.engine_combo.addItem("", "ffmpeg")
        selected_engine = self.engine_combo.findData(
            self._initial_export.get("engine", "native_gpu")
        )
        self.engine_combo.setCurrentIndex(max(0, selected_engine))
        self.engine_help_button = QToolButton(self)
        self.engine_help_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxQuestion)
        )
        self.engine_help_button.clicked.connect(
            lambda: open_help(self, self.translator, "other", "export_engines")
        )
        engine_row = QHBoxLayout()
        engine_row.addWidget(self.engine_combo, 1)
        engine_row.addWidget(self.engine_help_button)
        render_form.addRow(self.engine_label, engine_row)
        self.resolution_label = QLabel()
        self.fps_label = QLabel()
        self.codec_label = QLabel()
        self.crf_label = QLabel()
        self.preset_label = QLabel()
        self.audio_label = QLabel()
        render_form.addRow(self.resolution_label, self.resolution_combo)
        render_form.addRow(self.fps_label, self.fps_combo)
        render_form.addRow(self.workload_label)
        advanced_group = QGroupBox()
        advanced_grid = QGridLayout(advanced_group)
        advanced_grid.addWidget(self.codec_label, 0, 0)
        advanced_grid.addWidget(self.codec_combo, 0, 1)
        advanced_grid.addWidget(self.native_codec_combo, 0, 1)
        advanced_grid.addWidget(self.crf_label, 0, 2)
        advanced_grid.addWidget(self.crf_spin, 0, 3)
        advanced_grid.addWidget(self.native_cq_spin, 0, 3)
        advanced_grid.addWidget(self.preset_label, 1, 0)
        advanced_grid.addWidget(self.preset_combo, 1, 1)
        advanced_grid.addWidget(self.native_preset_combo, 1, 1)
        advanced_grid.addWidget(self.audio_label, 1, 2)
        advanced_grid.addWidget(self.audio_bitrate_combo, 1, 3)
        advanced_grid.setColumnStretch(1, 1)
        advanced_grid.setColumnStretch(3, 1)
        output_group = QGroupBox()
        output_layout = QFormLayout(output_group)
        type_row = QHBoxLayout()
        type_row.setSpacing(7)
        for button in self.export_type_buttons:
            type_row.addWidget(button, 1)
        output_layout.addRow(self.export_type_label, type_row)
        output_layout.addRow(self.format_label, self.format_combo)
        output_layout.addRow(self.filename_label, self.filename_edit)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.output_path_edit, 1)
        folder_row.addWidget(self.output_browse_button)
        output_layout.addRow(self.output_directory_label, folder_row)
        output_layout.addRow(self.output_filename_label)
        output_layout.addRow(self.filename_warning_label)

        # Project identity occupies the left column. The export controls
        # occupy the right, instead of appearing above unrelated quality cards.
        project_group = QGroupBox()
        project_layout = QVBoxLayout(project_group)
        project_layout.setSpacing(10)
        self.thumbnail_label = QLabel()
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setMinimumHeight(205)
        self.thumbnail_label.setObjectName("settingsStatusCard")
        if project_thumbnail is not None and not project_thumbnail.isNull():
            picture = project_thumbnail.scaled(
                350, 205, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.thumbnail_label.setPixmap(QPixmap.fromImage(picture))
        else:
            self.thumbnail_label.setText(
                "미리보기 없음" if translator.is_korean else "No canvas thumbnail"
            )
        self.project_title_label = QLabel(self.project_info.get("title") or
                                          ("제목 없는 프로젝트" if translator.is_korean else "Untitled project"))
        self.project_title_label.setObjectName("panelTitle")
        self.project_title_label.setWordWrap(True)
        self.project_metadata_label = QLabel()
        self.project_metadata_label.setObjectName("mutedLabel")
        self.project_metadata_label.setWordWrap(True)
        self.project_metadata_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        project_layout.addWidget(self.thumbnail_label)
        project_layout.addWidget(self.project_title_label)
        project_layout.addWidget(self.project_metadata_label)
        project_layout.addWidget(self.summary_label)
        project_layout.addStretch(1)
        self.project_group = project_group

        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(output_group)
        right_layout.addWidget(render_group)
        right_layout.addWidget(advanced_group)
        right_layout.addStretch(1)
        layout, outer = self.build_scrollable_body()
        main_columns = QHBoxLayout()
        main_columns.setSpacing(14)
        main_columns.addWidget(project_group, 2, Qt.AlignmentFlag.AlignTop)
        main_columns.addWidget(right_container, 3, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(main_columns)
        # 'Purpose' quality profiles are no longer part of the export UI.
        # Advanced encoding controls remain directly available instead.
        self.quality_group = quality_group
        quality_group.hide()
        quality_actions.removeWidget(self.optimize_button)
        advanced_grid.addWidget(self.optimize_button, 2, 0, 1, 4)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.button_box)
        outer.addLayout(footer)
        self.main_columns = main_columns
        self.summary_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum,
        )
        for section in (
            output_group, render_group, quality_group, advanced_group,
        ):
            section.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum,
            )
        self.render_group = render_group
        self.advanced_group = advanced_group
        self.output_group = output_group
        self.track_count = track_count
        self.duration_seconds = duration_seconds
        initial_profile = self._current_quality_profile()
        self._populate_quality_modes(initial_profile)
        self.advanced_group.setVisible(True)
        self.advanced_check.setChecked(True)
        self.quality_mode_combo.currentIndexChanged.connect(
            self._quality_mode_changed
        )
        self.advanced_check.toggled.connect(self._advanced_visibility_changed)
        self.codec_combo.currentIndexChanged.connect(self._advanced_value_changed)
        self.crf_spin.valueChanged.connect(self._advanced_value_changed)
        self.preset_combo.currentIndexChanged.connect(self._advanced_value_changed)
        self.native_cq_spin.valueChanged.connect(self._advanced_value_changed)
        self.native_preset_combo.currentIndexChanged.connect(self._advanced_value_changed)
        self.audio_bitrate_combo.currentIndexChanged.connect(
            self._advanced_value_changed
        )
        self.resolution_combo.currentIndexChanged.connect(
            self._update_workload_hint
        )
        self.fps_combo.currentIndexChanged.connect(self._update_workload_hint)
        self.filename_edit.textChanged.connect(self._filename_changed)
        self.output_path_edit.textChanged.connect(self._folder_changed)
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        self.export_type_combo.currentIndexChanged.connect(self._export_type_changed)
        self.engine_combo.currentIndexChanged.connect(self._engine_changed)
        self.setTabOrder(self.export_type_buttons[-1], self.format_combo)
        self.setTabOrder(self.format_combo, self.filename_edit)
        self.setTabOrder(self.filename_edit, self.output_path_edit)
        self.setTabOrder(self.output_path_edit, self.output_browse_button)
        self.setTabOrder(self.output_browse_button, self.engine_combo)
        self.setTabOrder(self.engine_combo, self.resolution_combo)
        self.setTabOrder(self.resolution_combo, self.fps_combo)
        self.setTabOrder(self.fps_combo, self.quality_mode_combo)
        self.setTabOrder(self.quality_mode_combo, self.optimize_button)
        self.setTabOrder(self.optimize_button, self.advanced_check)
        self.setTabOrder(self.advanced_check, self.codec_combo)
        self.setTabOrder(self.codec_combo, self.crf_spin)
        self.setTabOrder(self.crf_spin, self.preset_combo)
        self.setTabOrder(self.preset_combo, self.native_cq_spin)
        self.setTabOrder(self.native_cq_spin, self.native_preset_combo)
        self.setTabOrder(self.native_preset_combo, self.audio_bitrate_combo)
        self.setTabOrder(
            self.audio_bitrate_combo,
            self.button_box.button(QDialogButtonBox.StandardButton.Ok),
        )
        initial_kind = self.export_type_combo.findData(self._initial_export.get("kind", "video"))
        if initial_kind >= 0:
            self.export_type_combo.setCurrentIndex(initial_kind)
        self.retranslate()
        self._export_type_changed()
        initial_format = self.format_combo.findData(self._initial_export.get("format", "mp4"))
        if initial_format >= 0:
            self.format_combo.setCurrentIndex(initial_format)
        self._folder_changed()
        self.resize(1060, min(760, self.body_scroll.widget().sizeHint().height() + 95))

    def _advanced_visibility_changed(self, _visible: bool) -> None:
        # 'Purpose' and its disclosure are gone; direct encoding controls
        # remain discoverable without selecting a hidden quality profile.
        self.advanced_group.setVisible(not self.subtitle_only)

    @classmethod
    def _matching_quality_profile(
        cls, crf: int, preset: str, audio_bitrate: str,
        video_codec: str = "libx264",
    ) -> str:
        if video_codec not in {AUTO_VIDEO_ENCODER, "libx264"}:
            return "custom"
        values = (int(crf), str(preset), str(audio_bitrate))
        return next(
            (name for name, profile in cls.QUALITY_PROFILES.items() if profile == values),
            "custom",
        )

    def _populate_quality_modes(self, selected: str | None = None) -> None:
        """Create localized, purpose-first choices without exposing encoder jargon."""
        korean = self.translator.is_korean
        current = selected or str(self.quality_mode_combo.currentData() or "balanced")
        labels = (
            (
                ("권장 · 대부분의 영상", "balanced"),
                ("빠른 내보내기", "fast"),
                ("고화질 보관용", "high"),
                ("작은 파일", "compact"),
                ("사용자 설정", "custom"),
            )
            if korean else
            (
                ("Recommended · Most videos", "balanced"),
                ("Fast export", "fast"),
                ("High-quality archive", "high"),
                ("Smaller file", "compact"),
                ("Custom", "custom"),
            )
        )
        blocked = self.quality_mode_combo.blockSignals(True)
        self.quality_mode_combo.clear()
        for label, identifier in labels:
            self.quality_mode_combo.addItem(label, identifier)
        self.quality_mode_combo.setCurrentIndex(
            max(0, self.quality_mode_combo.findData(current))
        )
        self.quality_mode_combo.blockSignals(blocked)
        self._update_quality_description()

    def _quality_mode_changed(self, _index: int = -1) -> None:
        profile_name = str(self.quality_mode_combo.currentData() or "balanced")
        if profile_name == "custom":
            self.advanced_check.setChecked(True)
            self._update_quality_description()
            return
        crf, preset, audio_bitrate = self.QUALITY_PROFILES[profile_name]
        self._applying_quality_profile = True
        try:
            if self._uses_native_encoder:
                cq, native_preset = self.NATIVE_QUALITY_PROFILES[profile_name]
                self.native_cq_spin.setValue(cq)
                self.native_preset_combo.setCurrentIndex(self.native_preset_combo.findData(native_preset))
            else:
                self.codec_combo.setCurrentIndex(self.codec_combo.findData(AUTO_VIDEO_ENCODER))
                self.crf_spin.setValue(crf)
                self.preset_combo.setCurrentText(preset)
            self.audio_bitrate_combo.setCurrentText(audio_bitrate)
        finally:
            self._applying_quality_profile = False
        self._update_quality_description()

    def _current_quality_profile(self) -> str:
        audio_bitrate = self.audio_bitrate_combo.currentText()
        if self._uses_native_encoder:
            values = (self.native_cq_spin.value(), self.native_preset_combo.currentData())
            return next((
                name for name, profile in self.NATIVE_QUALITY_PROFILES.items()
                if profile == values and self.QUALITY_PROFILES[name][2] == audio_bitrate
            ), "custom")
        return self._matching_quality_profile(
            self.crf_spin.value(), self.preset_combo.currentText(),
            audio_bitrate, str(self.codec_combo.currentData()),
        )

    def _advanced_value_changed(self, _value: object = None) -> None:
        if self._applying_quality_profile:
            return
        matching = self._current_quality_profile()
        if not self._uses_native_encoder and self.sender() is self.codec_combo:
            matching = "custom"
        index = self.quality_mode_combo.findData(matching)
        if index >= 0 and index != self.quality_mode_combo.currentIndex():
            blocked = self.quality_mode_combo.blockSignals(True)
            self.quality_mode_combo.setCurrentIndex(index)
            self.quality_mode_combo.blockSignals(blocked)
        self._update_quality_description()

    def _update_quality_description(self) -> None:
        korean = self.translator.is_korean
        profile = str(self.quality_mode_combo.currentData() or "balanced")
        descriptions = {
            "balanced": (
                "권장 설정입니다. 화질, 인코딩 시간, 파일 크기의 균형이 좋습니다. 사용 가능한 NVIDIA GPU가 있으면 자동으로 사용합니다.",
                "Recommended default. A good balance of quality, export time, and file size. An available NVIDIA GPU is used automatically.",
            ),
            "fast": (
                "화질과 용량을 조금 양보하고 더 빠르게 내보냅니다.",
                "Exports faster with a modest tradeoff in quality and file size efficiency.",
            ),
            "high": (
                "편집 원본 보관이나 큰 화면에 적합합니다. 시간이 오래 걸리고 파일이 커집니다.",
                "Best for archives and large screens. Export takes longer and creates a larger file.",
            ),
            "compact": (
                "공유하기 쉬운 작은 파일을 만듭니다. 세밀한 화면에서는 화질 차이가 보일 수 있습니다.",
                "Creates a smaller file for easy sharing. Fine details may lose some quality.",
            ),
            "custom": (
                "고급 설정을 직접 변경한 상태입니다. 호환되지 않는 GPU 인코더를 선택하면 내보내기가 실패할 수 있습니다.",
                "Advanced values are customized. An unsupported GPU encoder can cause export to fail.",
            ),
        }
        if self._uses_native_encoder:
            descriptions["balanced"] = (
                "권장 설정입니다. 화질은 NVENC에 맡기고 균형 잡힌 인코딩 속도를 사용합니다.",
                "Recommended default. Uses automatic NVENC quality and a balanced encoding speed.",
            )
            descriptions["custom"] = (
                "고급 인코딩 설정에서 Rust GPU의 화질, 속도, 오디오 비트레이트를 조절할 수 있습니다.",
                "Adjust Rust GPU quality, encoding speed, and audio bitrate in advanced settings.",
            )
        ko_text, en_text = descriptions.get(profile, descriptions["custom"])
        self.quality_description_label.setText(ko_text if korean else en_text)
        self._update_workload_hint()

    def _update_workload_hint(self, _index: int = -1) -> None:
        """Explain render cost in plain language from resolution and FPS."""
        data = self.resolution_combo.currentData()
        if not isinstance(data, tuple) or len(data) < 2:
            return
        width, height = int(data[0]), int(data[1])
        try:
            fps = int(self.fps_combo.currentText())
        except ValueError:
            fps = 30
        work = width * height * fps
        korean = self.translator.is_korean
        if work > 3840 * 2160 * 35:
            level = "매우 높음" if korean else "Very high"
            detail = (
                "4K 또는 높은 FPS는 준비와 인코딩 시간이 크게 늘어납니다."
                if korean else
                "4K or a high frame rate greatly increases preparation and encoding time."
            )
        elif work > 1920 * 1080 * 40:
            level = "높음" if korean else "High"
            detail = (
                "복잡한 애니메이션이 많으면 내보내기 시간이 길어질 수 있습니다."
                if korean else
                "Complex animation can noticeably increase export time."
            )
        else:
            level = "보통" if korean else "Moderate"
            detail = (
                "대부분의 컴퓨터와 온라인 업로드에 적합합니다."
                if korean else
                "Suitable for most computers and online uploads."
            )
        self.workload_label.setText(
            f"예상 작업량: {level} · {width} × {height}, {fps} FPS\n{detail}"
            if korean else
            f"Estimated workload: {level} · {width} × {height}, {fps} FPS\n{detail}"
        )

    @property
    def app_settings(self) -> AppSettings:
        """Return this export's settings while retaining app-wide path defaults."""
        resolution_data = self.resolution_combo.currentData()
        if not isinstance(resolution_data, tuple) or len(resolution_data) != 3:
            width, height = self._base_settings.resolution
            base_name = self._base_settings.resolution_name
        else:
            width, height, base_name = resolution_data
        return replace(
            self._base_settings,
            output_directory=str(self.output_path.parent),
            resolution_name=str(base_name),
            fps=int(self.fps_combo.currentText()),
            video_codec=str(self.codec_combo.currentData()),
            crf=self.crf_spin.value(),
            preset=self.preset_combo.currentText(),
            audio_bitrate=self.audio_bitrate_combo.currentText(),
            native_nvenc_preset=str(self.native_preset_combo.currentData()),
            native_nvenc_cq=self.native_cq_spin.value(),
            render_width=int(width),
            render_height=int(height),
        )

    def _populate_resolutions(self, settings: AppSettings) -> None:
        """Offer quality tiers that always retain the active project's ratio."""
        if self.canvas_size is None:
            for name, size in RESOLUTIONS.items():
                self.resolution_combo.addItem(name, (*size, name))
            index = self.resolution_combo.findData(
                (*settings.resolution, settings.resolution_name)
            )
            self.resolution_combo.setCurrentIndex(max(0, index))
            return
        canvas_width, canvas_height = self.canvas_size
        ratio = canvas_width / max(1, canvas_height)

        def even(value: float) -> int:
            return max(2, round(value / 2.0) * 2)

        selected_index = 0
        for index, (base_name, base_size) in enumerate(RESOLUTIONS.items()):
            long_edge = max(base_size)
            if canvas_width >= canvas_height:
                width = long_edge
                height = even(long_edge / max(ratio, 1e-9))
            else:
                height = long_edge
                width = even(long_edge * ratio)
            quality = base_name.partition("(")[2].removesuffix(")")
            label = f"{width} × {height} (프로젝트 비율 · {quality})"
            if self.translator.language is not Language.KOREAN:
                label = f"{width} × {height} (Project ratio · {quality})"
            self.resolution_combo.addItem(label, (width, height, base_name))
            if base_name == settings.resolution_name:
                selected_index = index
        exact_width = even(canvas_width)
        exact_height = even(canvas_height)
        existing = {
            (self.resolution_combo.itemData(index)[0], self.resolution_combo.itemData(index)[1])
            for index in range(self.resolution_combo.count())
        }
        if (exact_width, exact_height) not in existing:
            label = (
                f"{exact_width} × {exact_height} (프로젝트 캔버스)"
                if self.translator.is_korean else
                f"{exact_width} × {exact_height} (Project canvas)"
            )
            self.resolution_combo.addItem(
                label, (exact_width, exact_height, settings.resolution_name)
            )
        self.resolution_combo.setCurrentIndex(selected_index)

    @property
    def save_as_default(self) -> bool:
        """Whether the chosen render values should become the next export defaults."""
        return False  # Never overwrite global defaults from a project export.

    @property
    def output_path(self) -> Path:
        """Combine folder, filename, and the explicitly selected extension."""
        folder = Path(self.output_path_edit.text().strip()).expanduser()
        name = self.filename_edit.text().strip()
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix in VIDEO_FORMATS + AUDIO_FORMATS + SUBTITLE_FORMATS:
            name = name[:-(len(suffix) + 1)]
        if self._confirmed_output_path is not None:
            return self._confirmed_output_path
        return (folder / f"{name}.{self.output_format}").resolve()

    @property
    def audio_only(self) -> bool:
        return self.export_type_combo.currentData() == "audio"

    @property
    def subtitle_only(self) -> bool:
        return self.export_type_combo.currentData() == "subtitles"

    @property
    def output_format(self) -> str:
        return str(self.format_combo.currentData() or "mp4")

    @property
    def _uses_native_encoder(self) -> bool:
        return (not self.audio_only and not self.subtitle_only
                and self.engine_combo.currentData() == "native_gpu")

    def _export_type_changed(self, _index: int = -1) -> None:
        formats = SUBTITLE_FORMATS if self.subtitle_only else AUDIO_FORMATS if self.audio_only else VIDEO_FORMATS
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        for name in formats:
            self.format_combo.addItem(f"{name.upper()} (*.{name})", name)
        self.format_combo.blockSignals(False)
        video = not (self.audio_only or self.subtitle_only)
        self.render_group.setVisible(video)
        self.quality_group.hide()
        self.advanced_group.setVisible(not self.subtitle_only)
        for index, button in enumerate(self.export_type_buttons):
            button.setChecked(index == self.export_type_combo.currentIndex())
        advanced_layout = self.advanced_group.layout()
        advanced_layout.addWidget(self.audio_label, 0 if self.audio_only else 1,
                                  0 if self.audio_only else 2)
        advanced_layout.addWidget(self.audio_bitrate_combo, 0 if self.audio_only else 1,
                                  1 if self.audio_only else 3)
        advanced_layout.setColumnStretch(3, 0 if self.audio_only else 1)
        self.retranslate()
        self._advanced_value_changed()
        if self.isVisible():
            self.layout().activate()
            self.fit_to_screen()

    def _engine_changed(self, _index: int = -1) -> None:
        self._update_encoding_controls()
        self._advanced_value_changed()

    def _update_encoding_controls(self) -> None:
        video = not (self.audio_only or self.subtitle_only)
        native = self._uses_native_encoder
        self.format_combo.setEnabled(not native)
        if native:
            self.format_combo.setCurrentIndex(self.format_combo.findData("mp4"))
        for label in (self.codec_label, self.crf_label, self.preset_label):
            label.setVisible(video)
        for widget in (self.codec_combo, self.crf_spin, self.preset_combo):
            widget.setVisible(video and not native)
            widget.setEnabled(video and not native)
        self.native_codec_combo.setVisible(native)
        for widget in (self.native_cq_spin, self.native_preset_combo):
            widget.setVisible(native)
            widget.setEnabled(native)
        korean = self.translator.is_korean
        self.crf_label.setText(
            ("영상 화질 (CQ)" if native else "영상 화질")
            if korean else ("Video quality (CQ)" if native else "Video quality")
        )
        for label, field in (
            (self.codec_label, self.native_codec_combo if native else self.codec_combo),
            (self.crf_label, self.native_cq_spin if native else self.crf_spin),
            (self.preset_label, self.native_preset_combo if native else self.preset_combo),
        ):
            label.setBuddy(field)
            field.setAccessibleName(label.text())
        self._format_changed()

    def _format_changed(self, _index: int = -1) -> None:
        lossless = self.output_format in {"wav", "flac"}
        self.audio_bitrate_combo.setEnabled(not lossless)
        self.audio_label.setEnabled(not lossless)
        self.audio_label.setText(
            ("오디오 품질 (무손실)" if lossless else "오디오 비트레이트")
            if self.translator.is_korean else
            ("Audio quality (lossless)" if lossless else "Audio bitrate")
        )
        self._update_output_filename()

    def _filename_changed(self, _text: str = "") -> None:
        self._filename_user_edited = True
        self._auto_filename_base = self.filename_edit.text().strip()
        self._confirmed_output_path = None
        self._update_output_filename()

    def _folder_changed(self, _text: str = "") -> None:
        """Resolve a conflicting filename immediately when the folder changes."""
        self._confirmed_output_path = None
        folder_text = self.output_path_edit.text().strip()
        name = self._auto_filename_base.strip()
        if folder_text and name:
            try:
                candidate = Path(folder_text).expanduser() / f"{name}.{self.output_format}"
                available = available_export_path(candidate)
                if self.filename_edit.text().strip() != available.stem:
                    blocked = self.filename_edit.blockSignals(True)
                    self.filename_edit.setText(available.stem)
                    self.filename_edit.blockSignals(blocked)
                self._filename_user_edited = False
            except (OSError, ValueError):
                pass
        self._update_output_filename()

    def _update_output_filename(self, _text: str = "") -> None:
        """Keep the displayed filename and actual destination synchronized.

        Automatic output names can be numbered; manual edits are never silently
        overwritten. A manually entered collision instead displays a warning
        and is rejected by the Start export validation.
        """
        output = self.output_path
        if not self._filename_user_edited and output.exists():
            try:
                base = self._auto_filename_base.strip() or output.stem
                available = available_export_path(output.with_name(f"{base}.{self.output_format}"))
                if available.stem != self.filename_edit.text().strip():
                    blocked = self.filename_edit.blockSignals(True)
                    self.filename_edit.setText(available.stem)
                    self.filename_edit.blockSignals(blocked)
                    output = self.output_path
            except (OSError, ValueError):
                pass
        collision = output.exists()
        self.filename_warning_label.setText(
            "같은 이름의 파일이 이미 있습니다. 다른 이름을 입력하세요."
            if self.translator.is_korean else
            "A file with this name already exists. Choose a different name."
        )
        self.filename_warning_label.setVisible(collision)
        self.output_filename_label.setText(
            ("저장 파일: " if self.translator.is_korean else "File: ")
            + output.name
        )

    def _browse_output_path(self) -> None:
        korean = self.translator.is_korean
        selected = QFileDialog.getExistingDirectory(
            self,
            "저장 폴더 선택" if korean else "Choose export folder",
            self.output_path_edit.text().strip(),
        )
        if selected:
            self.output_path_edit.setText(selected)

    def _accept_if_valid(self) -> None:
        """Validate the destination before expensive frame preparation begins."""
        korean = self.translator.is_korean
        if not self.output_path_edit.text().strip():
            QMessageBox.warning(
                self,
                "출력 경로 필요" if korean else "Output path required",
                "내보낼 파일의 저장 폴더를 선택하세요."
                if korean else "Choose the folder for the exported file.",
            )
            return
        name = self.filename_edit.text().strip()
        stem = name.split(".")[0].upper()
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        if (not name or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in name)
                or name.endswith((".", " ")) or stem in reserved
                or not self.output_path.stem):
            QMessageBox.warning(
                self, "파일 이름 확인" if korean else "Check filename",
                "사용 가능한 파일 이름을 입력하세요. 경로와 특수문자는 사용할 수 없습니다."
                if korean else "Enter a valid filename without paths or reserved characters.",
            )
            return
        folder = Path(self.output_path_edit.text().strip()).expanduser()
        if folder.exists() and not folder.is_dir():
            QMessageBox.warning(
                self, "저장 폴더 확인" if korean else "Check export folder",
                "파일이 아닌 저장 폴더를 선택하세요." if korean else "Select a folder for the export.",
            )
            return
        output = self.output_path
        if output.exists():
            QMessageBox.warning(
                self, "파일 이름 중복" if korean else "Filename already exists",
                "같은 이름의 파일이 이미 있습니다. 다른 이름을 입력하세요."
                if korean else "A file with this name already exists. Choose a different name.",
            )
            return
        # Freeze the accepted destination so a later filesystem change cannot
        # silently redirect the worker to an unrelated filename.
        self._confirmed_output_path = output
        self.output_path_edit.setText(str(output.parent))
        self.accept()

    def _apply_balanced_optimization(self) -> None:
        """Restore the safe beginner profile without changing resolution or FPS."""
        index = self.quality_mode_combo.findData("balanced")
        if index >= 0:
            self.quality_mode_combo.setCurrentIndex(index)
        self._quality_mode_changed(index)

    def retranslate(self) -> None:
        """Refresh dialog wording for the currently selected app language."""
        korean = self.translator.is_korean
        self.setWindowTitle("내보내기 설정" if korean else "Export settings")
        selected_profile = str(self.quality_mode_combo.currentData() or "balanced")
        self._populate_quality_modes(selected_profile)
        self.render_group.setTitle("기본 영상 설정" if korean else "Basic video settings")
        self.advanced_group.setTitle("고급 인코딩 설정" if korean else "Advanced encoding settings")
        self.output_group.setTitle("출력 파일" if korean else "Output file")
        self.export_type_label.setText("내보내기 항목" if korean else "Export type")
        self.export_type_combo.setItemText(0, "영상 + 오디오" if korean else "Video + audio")
        self.export_type_combo.setItemText(1, "오디오만 내보내기" if korean else "Audio only")
        self.export_type_combo.setItemText(2, "자막 내보내기" if korean else "Subtitles")
        for button, label in zip(
            self.export_type_buttons,
            ("영상 + 오디오", "오디오", "자막")
            if korean else ("Video + audio", "Audio", "Subtitles"),
            strict=True,
        ):
            button.setText(label)
        self.project_group.setTitle("프로젝트 정보" if korean else "Project information")
        info = self.project_info
        author = info.get("author") or ("미지정" if korean else "Not specified")
        created = info.get("created_at", "")
        if created:
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except ValueError:
                pass
        version = info.get("created_app_version") or ("알 수 없음" if korean else "Unknown")
        project_path = info.get("path") or ("저장되지 않은 프로젝트" if korean else "Unsaved project")
        self.project_metadata_label.setText(
            (
                f"생성자: {author}\n생성 날짜: {created or '-'}\n생성 프로그램 버전: {version}\n프로젝트 경로: {project_path}"
            ) if korean else
            (
                f"Creator: {author}\nCreated: {created or '-'}\nCreated in version: {version}\nProject path: {project_path}"
            )
        )
        self.engine_help_button.setToolTip(
            "Rust GPU와 FFmpeg 엔진의 차이점 보기" if korean else "Compare Rust GPU and FFmpeg engines"
        )
        self.format_label.setText("파일 형식" if korean else "File format")
        self.filename_label.setText("파일 이름" if korean else "File name")
        self.output_directory_label.setText("저장 폴더" if korean else "Save folder")
        self.engine_label.setText("엔진" if korean else "Engine")
        self.engine_combo.setItemText(
            self.engine_combo.findData("native_gpu"),
            "Rust GPU (베타)" if korean else "Rust GPU (Beta)",
        )
        self.engine_combo.setItemText(
            self.engine_combo.findData("ffmpeg"),
            "FFmpeg (레거시)" if korean else "FFmpeg (Legacy)",
        )
        self.advanced_group.setTitle(
            ("오디오 설정" if korean else "Audio settings") if self.audio_only else
            ("고급 인코딩 설정" if korean else "Advanced encoding settings")
        )
        self.quality_mode_label.setText("")
        self.output_browse_button.setText("찾아보기" if korean else "Browse")
        self.resolution_label.setText("해상도" if korean else "Resolution")
        self.fps_label.setText("프레임 레이트" if korean else "Frame rate")
        self.codec_label.setText("비디오 인코더" if korean else "Video encoder")
        automatic_index = self.codec_combo.findData(AUTO_VIDEO_ENCODER)
        if automatic_index >= 0:
            self.codec_combo.setItemText(
                automatic_index,
                "자동 선택 (권장)" if korean else "Automatic (Recommended)",
            )
        self.preset_label.setText("인코딩 속도" if korean else "Encoding speed")
        quality_hint = "숫자가 낮을수록 화질이 높습니다." if korean else "Lower values preserve more detail."
        self.crf_spin.setToolTip(quality_hint)
        self.native_cq_spin.setSpecialValueText("자동" if korean else "Auto")
        self.native_cq_spin.setToolTip(quality_hint)
        native_preset_labels = (
            ("속도 우선 (P1)", "균형 (P3)", "화질 우선 (P5)")
            if korean else ("Fast (P1)", "Balanced (P3)", "Quality (P5)")
        )
        for index, text in enumerate(native_preset_labels):
            self.native_preset_combo.setItemText(index, text)
        self.optimize_button.setText(
            "권장 설정으로 되돌리기" if korean else "Restore recommended settings"
        )
        self.optimize_button.setToolTip(
            "해상도와 FPS를 유지하고 현재 엔진의 권장 인코딩 설정을 적용합니다."
            if korean else
            "Keeps resolution and frame rate and restores the selected engine’s recommended settings."
        )
        self.advanced_check.setText(
            "고급 인코딩 설정 표시" if korean else "Show advanced encoding settings"
        )

        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText(
            "내보내기 시작" if korean else "Start export"
        )
        start_button = self.button_box.button(QDialogButtonBox.StandardButton.Ok)
        if not start_button.property("primary"):
            start_button.setProperty("primary", True)
            start_button.style().unpolish(start_button)
            start_button.style().polish(start_button)
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "취소" if korean else "Cancel"
        )
        duration = self._format_duration(self.duration_seconds)
        self.summary_label.setText(
            f"내보낼 곡 {self.track_count}개 · 총 재생 시간 {duration}\n"
            + (
                f"프로젝트 캔버스 {self.canvas_size[0]} × {self.canvas_size[1]}의 화면 비율을 유지합니다.\n"
                if self.canvas_size is not None else ""
            )
            + "품질과 출력 위치를 확인한 후 내보내기를 시작하세요."
            if korean else
            f"{self.track_count} track(s) · total duration {duration}\n"
            + (
                f"The {self.canvas_size[0]} × {self.canvas_size[1]} project canvas ratio will be preserved.\n"
                if self.canvas_size is not None else ""
            )
            + "Review the quality and output destination, then start the export."
        )
        self._update_quality_description()
        if self.audio_only:
            self.summary_label.setText(
                f"내보낼 곡 {self.track_count}개 · 총 재생 시간 {duration}\n"
                "화면 없이 플레이리스트의 오디오만 저장합니다. 곡 전환과 음량 처리가 적용됩니다."
                if korean else
                f"{self.track_count} track(s) · total duration {duration}\n"
                "Save playlist audio with track transitions and loudness processing."
            )
        elif self.subtitle_only:
            self.summary_label.setText(
                f"내보낼 곡 {self.track_count}개 · 총 재생 시간 {duration}\n"
                "믹싱 시간에 맞춰 가사를 저장합니다. 겹치는 가사는 나가는 곡을 윗줄, 들어오는 곡을 아랫줄에 표시합니다."
                if korean else
                f"{self.track_count} track(s) · total duration {duration}\n"
                "Save lyrics on the mix timeline. Overlapping lyrics show the outgoing track above the incoming track."
            )
        self._update_encoding_controls()
        for label, field in (
            (self.export_type_label, self.export_type_combo),
            (self.format_label, self.format_combo),
            (self.filename_label, self.filename_edit),
            (self.output_directory_label, self.output_path_edit),
            (self.engine_label, self.engine_combo),
            (self.resolution_label, self.resolution_combo), (self.fps_label, self.fps_combo),
            (self.audio_label, self.audio_bitrate_combo),
        ):
            label.setBuddy(field)
            field.setAccessibleName(label.text())

    @staticmethod
    def _format_duration(seconds: float) -> str:
        return format_clock(round(seconds))
