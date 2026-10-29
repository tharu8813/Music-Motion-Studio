"""Per-export video quality dialog that does not overwrite defaults by default."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
    QVBoxLayout,
    QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.dialogs.adaptive_dialog import AdaptiveDialog
from app.services.app_settings_service import (
    AUDIO_BITRATES,
    ENCODING_PRESETS,
    RESOLUTIONS,
    VIDEO_ENCODERS,
    AppSettings,
)
from app.utils.i18n import Language, Translator
from app.services.video_encoder_service import AUTO_VIDEO_ENCODER
from app.services.export_validation_service import EXPORT_FPS_OPTIONS
from app.utils.time_format import format_clock
from app.services.export_formats import VIDEO_FORMATS, AUDIO_FORMATS, SUBTITLE_FORMATS


class ExportSettingsDialog(AdaptiveDialog):
    """Collect render quality and the final output file in one place."""

    QUALITY_PROFILES = {
        "balanced": (18, "medium", "192k"),
        "fast": (22, "veryfast", "192k"),
        "high": (15, "medium", "320k"),
        "compact": (24, "medium", "192k"),
    }

    def __init__(self, settings: AppSettings, track_count: int, duration_seconds: float,
                 translator: Translator, default_output_path: str | Path,
                 parent: QWidget | None = None,
                 canvas_size: tuple[int, int] | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("other", "export"), translator)
        self.translator = translator
        self._base_settings = settings
        self.canvas_size = canvas_size
        self._applying_quality_profile = False
        self.setMinimumWidth(760)
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
        self.audio_bitrate_combo = QComboBox()
        self.audio_bitrate_combo.addItems(AUDIO_BITRATES)
        self.audio_bitrate_combo.setCurrentText(settings.audio_bitrate)
        self.advanced_check = QCheckBox()
        default_path = Path(default_output_path).expanduser().resolve()
        self.export_type_combo = QComboBox()
        self.export_type_combo.addItem("", "video")
        self.export_type_combo.addItem("", "audio")
        self.export_type_combo.addItem("", "subtitles")
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
        self.output_path_edit.setMinimumWidth(330)
        self.output_browse_button = QPushButton()
        self.output_browse_button.clicked.connect(self._browse_output_path)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("infoCallout")
        self.summary_label.setWordWrap(True)
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
        advanced_grid.addWidget(self.crf_label, 0, 2)
        advanced_grid.addWidget(self.crf_spin, 0, 3)
        advanced_grid.addWidget(self.preset_label, 1, 0)
        advanced_grid.addWidget(self.preset_combo, 1, 1)
        advanced_grid.addWidget(self.audio_label, 1, 2)
        advanced_grid.addWidget(self.audio_bitrate_combo, 1, 3)
        advanced_grid.setColumnStretch(1, 1)
        advanced_grid.setColumnStretch(3, 1)
        output_group = QGroupBox()
        output_layout = QFormLayout(output_group)
        format_row = QHBoxLayout()
        format_row.addWidget(self.export_type_combo, 1)
        format_row.addWidget(self.format_label)
        format_row.addWidget(self.format_combo, 1)
        output_layout.addRow(self.export_type_label, format_row)
        output_layout.addRow(self.filename_label, self.filename_edit)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.output_path_edit, 1)
        folder_row.addWidget(self.output_browse_button)
        output_layout.addRow(self.output_directory_label, folder_row)
        output_layout.addRow(self.output_filename_label)
        layout, outer = self.build_scrollable_body()
        layout.addWidget(self.summary_label)
        layout.addWidget(output_group)
        main_columns = QHBoxLayout()
        main_columns.addWidget(
            render_group, 1, Qt.AlignmentFlag.AlignTop,
        )
        main_columns.addWidget(
            quality_group, 1, Qt.AlignmentFlag.AlignTop,
        )
        layout.addLayout(main_columns)
        # Keep the quality profile card visible: it is the beginner-facing
        # entry point and the advanced controls remain available below it.
        # Both controls already share the same settings update path.
        layout.addWidget(advanced_group)
        footer = QHBoxLayout()
        footer.addWidget(self.save_default_check)
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
        self.quality_group = quality_group
        self.render_group = render_group
        self.advanced_group = advanced_group
        self.output_group = output_group
        self.track_count = track_count
        self.duration_seconds = duration_seconds
        initial_profile = self._matching_quality_profile(
            settings.crf, settings.preset, settings.audio_bitrate,
            settings.video_codec,
        )
        self._populate_quality_modes(initial_profile)
        self.advanced_group.setVisible(initial_profile == "custom")
        self.advanced_check.setChecked(initial_profile == "custom")
        self.quality_mode_combo.currentIndexChanged.connect(
            self._quality_mode_changed
        )
        self.advanced_check.toggled.connect(self._advanced_visibility_changed)
        self.codec_combo.currentIndexChanged.connect(self._advanced_value_changed)
        self.crf_spin.valueChanged.connect(self._advanced_value_changed)
        self.preset_combo.currentIndexChanged.connect(self._advanced_value_changed)
        self.audio_bitrate_combo.currentIndexChanged.connect(
            self._advanced_value_changed
        )
        self.resolution_combo.currentIndexChanged.connect(
            self._update_workload_hint
        )
        self.fps_combo.currentIndexChanged.connect(self._update_workload_hint)
        self.filename_edit.textChanged.connect(self._update_output_filename)
        self.format_combo.currentIndexChanged.connect(self._format_changed)
        self.export_type_combo.currentIndexChanged.connect(self._export_type_changed)
        self.setTabOrder(self.export_type_combo, self.format_combo)
        self.setTabOrder(self.format_combo, self.filename_edit)
        self.setTabOrder(self.filename_edit, self.output_path_edit)
        self.setTabOrder(self.output_path_edit, self.output_browse_button)
        self.setTabOrder(self.output_browse_button, self.resolution_combo)
        self.setTabOrder(self.resolution_combo, self.fps_combo)
        self.setTabOrder(self.fps_combo, self.quality_mode_combo)
        self.setTabOrder(self.quality_mode_combo, self.optimize_button)
        self.setTabOrder(self.optimize_button, self.advanced_check)
        self.setTabOrder(self.advanced_check, self.codec_combo)
        self.setTabOrder(self.codec_combo, self.crf_spin)
        self.setTabOrder(self.crf_spin, self.preset_combo)
        self.setTabOrder(self.preset_combo, self.audio_bitrate_combo)
        self.setTabOrder(self.audio_bitrate_combo, self.save_default_check)
        self.setTabOrder(
            self.save_default_check,
            self.button_box.button(QDialogButtonBox.StandardButton.Ok),
        )
        self.retranslate()
        self._export_type_changed()
        preferred_width = min(960, max(820, self.sizeHint().width()))
        self.resize(preferred_width, self.body_scroll.widget().sizeHint().height() + 90)

    def _advanced_visibility_changed(self, visible: bool) -> None:
        self.advanced_group.setVisible(visible and not self.subtitle_only)
        # Let the body scroll instead of forcing the action buttons offscreen.
        self.body_scroll.widget().adjustSize()

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
            self.codec_combo.setCurrentIndex(
                max(0, self.codec_combo.findData(AUTO_VIDEO_ENCODER))
            )
            self.crf_spin.setValue(crf)
            self.preset_combo.setCurrentText(preset)
            self.audio_bitrate_combo.setCurrentText(audio_bitrate)
        finally:
            self._applying_quality_profile = False
        self._update_quality_description()

    def _advanced_value_changed(self, _value: object = None) -> None:
        if self._applying_quality_profile:
            return
        matching = self._matching_quality_profile(
            self.crf_spin.value(), self.preset_combo.currentText(),
            self.audio_bitrate_combo.currentText(),
            str(self.codec_combo.currentData()),
        )
        # Encoder selection is deliberately treated as an expert override even
        # if the remaining values happen to match a simple profile.
        if self.sender() is self.codec_combo:
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
        return AppSettings(
            ffmpeg_path=self._base_settings.ffmpeg_path,
            output_directory=str(self.output_path.parent),
            resolution_name=str(base_name),
            fps=int(self.fps_combo.currentText()),
            video_codec=str(self.codec_combo.currentData()),
            crf=self.crf_spin.value(),
            preset=self.preset_combo.currentText(),
            audio_bitrate=self.audio_bitrate_combo.currentText(),
            work_mode=self._base_settings.work_mode,
            smooth_scrolling=self._base_settings.smooth_scrolling,
            smooth_scroll_duration_ms=self._base_settings.smooth_scroll_duration_ms,
            preview_backend=self._base_settings.preview_backend,
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
        return self.save_default_check.isChecked()

    @property
    def output_path(self) -> Path:
        """Combine folder, filename, and the explicitly selected extension."""
        folder = Path(self.output_path_edit.text().strip()).expanduser()
        name = self.filename_edit.text().strip()
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix in VIDEO_FORMATS + AUDIO_FORMATS + SUBTITLE_FORMATS:
            name = name[:-(len(suffix) + 1)]
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

    def _export_type_changed(self, _index: int = -1) -> None:
        formats = SUBTITLE_FORMATS if self.subtitle_only else AUDIO_FORMATS if self.audio_only else VIDEO_FORMATS
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        for name in formats:
            self.format_combo.addItem(f"{name.upper()} (*.{name})", name)
        self.format_combo.blockSignals(False)
        video = not (self.audio_only or self.subtitle_only)
        self.render_group.setVisible(video)
        self.quality_group.setVisible(video)
        self.advanced_group.setVisible(not self.subtitle_only and (self.audio_only or self.advanced_check.isChecked()))
        self.save_default_check.setVisible(not self.subtitle_only)
        for widget in (self.codec_label, self.codec_combo, self.crf_label,
                       self.crf_spin, self.preset_label, self.preset_combo):
            widget.setVisible(video)
        advanced_layout = self.advanced_group.layout()
        advanced_layout.addWidget(self.audio_label, 0 if self.audio_only else 1,
                                  0 if self.audio_only else 2)
        advanced_layout.addWidget(self.audio_bitrate_combo, 0 if self.audio_only else 1,
                                  1 if self.audio_only else 3)
        advanced_layout.setColumnStretch(3, 0 if self.audio_only else 1)
        self.retranslate()
        self._format_changed()
        if self.isVisible():
            self.layout().activate()
            self.fit_to_screen()

    def _format_changed(self, _index: int = -1) -> None:
        lossless = self.output_format in {"wav", "flac"}
        self.audio_bitrate_combo.setEnabled(not lossless)
        self.audio_label.setText(
            ("오디오 품질 (무손실)" if lossless else "오디오 비트레이트")
            if self.translator.is_korean else
            ("Audio quality (lossless)" if lossless else "Audio bitrate")
        )
        self._update_output_filename()

    def _update_output_filename(self, _text: str = "") -> None:
        self.output_filename_label.setText(
            ("저장 파일: " if self.translator.is_korean else "File: ")
            + self.output_path.name
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
            answer = QMessageBox.question(
                self,
                "파일 덮어쓰기" if korean else "Replace existing file",
                f"{output.name} 파일을 덮어쓸까요?"
                if korean else f"Replace the existing file '{output.name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            # Native Windows dialogs may return either the StandardButton enum
            # or its integer value depending on the PySide/Python build. Object
            # identity rejects the latter even after the user clicks Yes.
            if answer != QMessageBox.StandardButton.Yes:
                return
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
        self.format_label.setText("파일 형식" if korean else "File format")
        self.filename_label.setText("파일 이름" if korean else "File name")
        self.output_directory_label.setText("저장 폴더" if korean else "Save folder")
        self.advanced_group.setTitle(
            ("오디오 설정" if korean else "Audio settings") if self.audio_only else
            ("고급 인코딩 설정" if korean else "Advanced encoding settings")
        )
        self.quality_mode_label.setText("용도" if korean else "Purpose")
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
        self.crf_label.setText("화질 (CRF, 낮을수록 고화질)" if korean else "Quality (CRF, lower is higher quality)")
        self.preset_label.setText("인코딩 속도" if korean else "Encoding speed")
        self.audio_label.setText("오디오 품질 (AAC)" if korean else "Audio quality (AAC)")
        self.optimize_button.setText(
            "권장 설정으로 되돌리기" if korean else "Restore recommended settings"
        )
        self.advanced_check.setText(
            "고급 인코딩 설정 표시" if korean else "Show advanced encoding settings"
        )
        self.optimize_button.setToolTip(
            "해상도와 FPS는 유지하고 하드웨어 자동 선택과 H.264 권장값을 적용합니다."
            if korean else
            "Keeps resolution and FPS while restoring automatic hardware selection and recommended H.264 values."
        )
        self.save_default_check.setText(
            "이 값을 다음 내보내기의 기본값으로 저장" if korean else "Save these values as future export defaults"
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
        self._format_changed()
        for label, field in (
            (self.export_type_label, self.export_type_combo),
            (self.format_label, self.format_combo),
            (self.filename_label, self.filename_edit),
            (self.output_directory_label, self.output_path_edit),
            (self.resolution_label, self.resolution_combo), (self.fps_label, self.fps_combo),
            (self.quality_mode_label, self.quality_mode_combo),
            (self.codec_label, self.codec_combo), (self.crf_label, self.crf_spin),
            (self.preset_label, self.preset_combo), (self.audio_label, self.audio_bitrate_combo),
        ):
            label.setBuddy(field)
            field.setAccessibleName(label.text())

    @staticmethod
    def _format_duration(seconds: float) -> str:
        return format_clock(round(seconds))
