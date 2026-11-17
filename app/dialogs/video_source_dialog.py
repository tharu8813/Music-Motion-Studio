"""Video media, timing, repeat, and filter settings."""

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.models.source import Source
from app.models.source_components import VideoComponent
from app.models.source_registry import source_registry


class VideoSourceDialog(QDialog):
    """Edit video-only properties while keeping Cancel non-mutating."""

    def __init__(self, source: Source, korean: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("canvas", "video_source"))
        video = source_registry.component_for(source)
        if not isinstance(video, VideoComponent):
            raise ValueError("Video settings require a video source.")
        self.korean = korean
        self.setWindowTitle("영상 재생 설정" if korean else "Video playback settings")
        self.setMinimumSize(700, 610)
        self.resize(740, 650)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)
        title = QLabel(
            "이 영상 요소가 사용할 영상과 재생 방법을 설정합니다."
            if korean else
            "Choose which videos this source uses and how they play."
        )
        title.setObjectName("videoDialogTitle")
        root.addWidget(title)

        self.scope_group = QGroupBox(
            "1. 어디에서 영상을 가져올까요?"
            if korean else "1. Where should the videos come from?"
        )
        scope_layout = QVBoxLayout(self.scope_group)
        scope_layout.setSpacing(8)
        self.timing = QComboBox()
        self.timing.addItem(
            "곡마다 다른 영상 사용" if korean else "Use different videos for each track",
            "track",
        )
        self.timing.addItem(
            "전체 재생목록에서 같은 영상 사용"
            if korean else "Use the same videos across the whole playlist",
            "timeline",
        )
        self.timing.setCurrentIndex(max(0, self.timing.findData(video.timing_mode)))
        self.timing.setMinimumHeight(34)
        scope_layout.addWidget(self.timing)
        self.scope_summary = QLabel()
        self.scope_summary.setObjectName("videoScopeSummary")
        self.scope_summary.setWordWrap(True)
        scope_layout.addWidget(self.scope_summary)

        self.track_scope_panel = QFrame()
        self.track_scope_panel.setObjectName("videoTrackScopePanel")
        track_scope_layout = QVBoxLayout(self.track_scope_panel)
        track_scope_layout.setContentsMargins(12, 10, 12, 10)
        track_scope_layout.setSpacing(4)
        track_scope_title = QLabel(
            "영상 파일은 각 곡에서 선택합니다"
            if korean else "Choose video files on each track"
        )
        track_scope_title.setObjectName("videoPanelTitle")
        track_scope_help = QLabel(
            "재생목록에서 곡을 더블클릭한 뒤 ‘이 곡의 영상’ 탭에 영상을 추가하세요. "
            "해당 곡이 재생되는 동안에만 그 목록을 사용합니다."
            if korean else
            "Double-click a track in the playlist, then add media on its ‘Videos for this track’ tab. "
            "That list is used only while the track is playing."
        )
        track_scope_help.setObjectName("mutedLabel")
        track_scope_help.setWordWrap(True)
        track_scope_layout.addWidget(track_scope_title)
        track_scope_layout.addWidget(track_scope_help)
        scope_layout.addWidget(self.track_scope_panel)

        self.timeline_media_group = QGroupBox(
            "전체 재생목록용 영상" if korean else "Videos for the whole playlist"
        )
        media_layout = QVBoxLayout(self.timeline_media_group)
        self.timeline_media_help = QLabel(
            "여기에 추가한 영상은 곡이 바뀌어도 이어서 재생됩니다."
            if korean else "These videos continue playing when the track changes."
        )
        self.timeline_media_help.setObjectName("mutedLabel")
        self.timeline_media_help.setWordWrap(True)
        media_layout.addWidget(self.timeline_media_help)
        self.media_list = QListWidget()
        self.media_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for path in video.paths:
            self.media_list.addItem(path)
        media_layout.addWidget(self.media_list, 1)
        media_actions = QHBoxLayout()
        self.add_button = QPushButton("영상 추가…" if korean else "Add videos…")
        self.remove_button = QPushButton("선택 제거" if korean else "Remove selected")
        self.up_button = QPushButton("위로" if korean else "Up")
        self.down_button = QPushButton("아래로" if korean else "Down")
        self.add_button.clicked.connect(self._add_media)
        self.remove_button.clicked.connect(self._remove_media)
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        for button in (
            self.add_button, self.remove_button, self.up_button, self.down_button,
        ):
            media_actions.addWidget(button)
        media_actions.addStretch(1)
        media_layout.addLayout(media_actions)
        scope_layout.addWidget(self.timeline_media_group, 1)
        root.addWidget(self.scope_group, 1)

        self.playback_group = QGroupBox(
            "2. 어떻게 재생할까요?" if korean else "2. How should they play?"
        )
        playback_form = QFormLayout(self.playback_group)
        self.repeat = QComboBox()
        for label, value in (
            ("한 번만 재생" if korean else "Play once", "once"),
            ("첫 번째 영상 계속 반복" if korean else "Keep looping the first video", "loop_one"),
            ("여러 영상을 순서대로 반복" if korean else "Repeat videos in order", "sequence"),
            ("여러 영상을 무작위로 반복" if korean else "Repeat videos randomly", "random"),
        ):
            self.repeat.addItem(label, value)
        self.repeat.setCurrentIndex(max(0, self.repeat.findData(video.repeat_mode)))
        self.repeat_help = QLabel()
        self.repeat_help.setObjectName("mutedLabel")
        self.repeat_help.setWordWrap(True)
        repeat_host = QWidget()
        repeat_layout = QVBoxLayout(repeat_host)
        repeat_layout.setContentsMargins(0, 0, 0, 0)
        repeat_layout.setSpacing(3)
        repeat_layout.addWidget(self.repeat)
        repeat_layout.addWidget(self.repeat_help)
        self.cycles = QSpinBox()
        self.cycles.setRange(1, 100_000)
        self.cycles.setValue(video.cycle_count)
        self.unlimited = QCheckBox("끝날 때까지 반복" if korean else "Repeat until the end")
        self.unlimited.setChecked(video.cycle_unlimited)
        self.cycle_host = QWidget()
        cycle_row = QHBoxLayout(self.cycle_host)
        cycle_row.setContentsMargins(0, 0, 0, 0)
        cycle_row.addWidget(self.cycles, 1)
        cycle_row.addWidget(self.unlimited)
        self.cycle_label = QLabel("반복 횟수" if korean else "Cycles")
        self.random_seed = QSpinBox()
        self.random_seed.setRange(0, 999_999)
        self.random_seed.setValue(video.random_seed)
        self.random_seed.setToolTip(
            "같은 값이면 영상이 같은 순서로 섞입니다. 값을 바꾸면 새로운 순서가 됩니다."
            if korean else "The same seed repeats the same shuffle; change it for a new order."
        )
        self.random_seed_label = QLabel("무작위 순서 시드" if korean else "Shuffle seed")
        playback_form.addRow("재생 방식" if korean else "Playback", repeat_host)
        playback_form.addRow(self.cycle_label, self.cycle_host)
        playback_form.addRow(self.random_seed_label, self.random_seed)
        root.addWidget(self.playback_group)

        self.appearance_group = QGroupBox(
            "3. 속도와 화면 효과" if korean else "3. Speed and appearance"
        )
        appearance_form = QFormLayout(self.appearance_group)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.05, 8.0)
        self.speed.setSingleStep(0.05)
        self.speed.setValue(video.speed)
        self.speed.setSuffix("×")
        self.saturation = QDoubleSpinBox()
        self.saturation.setRange(0.0, 3.0)
        self.saturation.setSingleStep(0.05)
        self.saturation.setValue(video.saturation)
        self.grayscale = QCheckBox("흑백으로 표시" if korean else "Show in grayscale")
        self.grayscale.setChecked(video.grayscale)
        self.muted = QCheckBox("영상 소리 사용 안 함" if korean else "Do not use video audio")
        self.muted.setChecked(True)
        self.muted.setEnabled(False)
        self.muted.setToolTip(
            "플레이리스트 음악과 섞이지 않도록 영상 소리는 항상 사용하지 않습니다."
            if korean else
            "Video audio is always disabled so it does not mix with playlist music."
        )
        appearance_form.addRow("재생 속도" if korean else "Speed", self.speed)
        appearance_form.addRow("채도" if korean else "Saturation", self.saturation)
        appearance_form.addRow("화면" if korean else "Picture", self.grayscale)
        appearance_form.addRow("오디오" if korean else "Audio", self.muted)
        root.addWidget(self.appearance_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        if korean:
            buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.timing.currentIndexChanged.connect(self._update_enabled)
        self.repeat.currentIndexChanged.connect(self._update_enabled)
        self.unlimited.toggled.connect(self._update_enabled)
        self.media_list.itemSelectionChanged.connect(self._update_media_actions)
        self.media_list.currentRowChanged.connect(self._update_media_actions)
        self._apply_style()
        self._update_enabled()

    @property
    def values(self) -> dict[str, object]:
        paths = [self.media_list.item(i).text() for i in range(self.media_list.count())]
        return {
            "video_timing_mode": self.timing.currentData(),
            "video_repeat_mode": self.repeat.currentData(),
            "video_cycle_count": self.cycles.value(),
            "video_cycle_unlimited": self.unlimited.isChecked(),
            "video_speed": self.speed.value(),
            "video_muted": True,
            "video_saturation": self.saturation.value(),
            "video_grayscale": self.grayscale.isChecked(),
            "video_random_seed": self.random_seed.value(),
            "video_paths": paths,
            "content_path": paths[0] if paths else "",
        }

    def _add_media(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "영상 추가" if self.korean else "Add videos", "",
            "Video files (*.mp4 *.mov *.mkv *.webm *.avi *.m4v)",
        )
        existing = {
            self.media_list.item(i).text().casefold()
            for i in range(self.media_list.count())
        }
        for path in paths:
            resolved = str(Path(path).resolve())
            if resolved.casefold() not in existing:
                self.media_list.addItem(resolved)
                existing.add(resolved.casefold())
        self._update_enabled()

    def _remove_media(self) -> None:
        for item in self.media_list.selectedItems():
            self.media_list.takeItem(self.media_list.row(item))
        self._update_enabled()

    def _move(self, delta: int) -> None:
        row, target = self.media_list.currentRow(), self.media_list.currentRow() + delta
        if row < 0 or target < 0 or target >= self.media_list.count():
            return
        item = self.media_list.takeItem(row)
        self.media_list.insertItem(target, item)
        self.media_list.setCurrentRow(target)

    def _update_enabled(self, *_args: object) -> None:
        track_mode = self.timing.currentData() == "track"
        self.track_scope_panel.setVisible(track_mode)
        self.timeline_media_group.setVisible(not track_mode)
        if track_mode:
            self.scope_summary.setText(
                "곡이 바뀔 때마다 해당 곡에 등록된 영상으로 자동 전환합니다."
                if self.korean else
                "Automatically switches to the videos assigned to the active track."
            )
        else:
            count = self.media_list.count()
            self.scope_summary.setText(
                f"곡과 관계없이 전체 영상 시간에 맞춰 재생합니다 · 현재 {count}개"
                if self.korean else
                f"Plays on the complete video timeline, independent of tracks · {count} selected"
            )
        repeat_mode = self.repeat.currentData()
        cycles_apply = repeat_mode in {"sequence", "random"}
        random_order = repeat_mode == "random"
        self.random_seed_label.setVisible(random_order)
        self.random_seed.setVisible(random_order)
        self.cycle_label.setVisible(cycles_apply)
        self.cycle_host.setVisible(cycles_apply)
        self.unlimited.setEnabled(cycles_apply)
        self.cycles.setEnabled(cycles_apply and not self.unlimited.isChecked())
        repeat_help = {
            "once": (
                "목록의 첫 번째 영상을 한 번 재생한 뒤 투명해집니다.",
                "Plays the first video once, then becomes transparent.",
            ),
            "loop_one": (
                "목록의 첫 번째 영상 하나를 계속 반복합니다.",
                "Continuously loops only the first video in the list.",
            ),
            "sequence": (
                "목록 순서대로 모두 재생하면 한 사이클입니다.",
                "One cycle plays every video in list order.",
            ),
            "random": (
                "한 사이클마다 영상 순서를 섞어서 재생합니다.",
                "Shuffles the video order for every cycle.",
            ),
        }
        localized = repeat_help[str(repeat_mode)]
        self.repeat_help.setText(localized[0 if self.korean else 1])
        self._update_media_actions()

    def _update_media_actions(self, *_args: object) -> None:
        selected = bool(self.media_list.selectedItems())
        row = self.media_list.currentRow()
        self.remove_button.setEnabled(selected)
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < self.media_list.count() - 1)

    def _validate_accept(self) -> None:
        if self.timing.currentData() == "timeline" and self.media_list.count() == 0:
            QMessageBox.warning(
                self, "영상이 필요합니다" if self.korean else "Video required",
                "‘전체 재생목록에서 같은 영상 사용’을 선택했으므로 영상을 하나 이상 추가해 주세요."
                if self.korean else
                "Add at least one video for ‘Use the same videos across the whole playlist’.",
            )
            self.media_list.setFocus()
            return
        self.accept()

    def _apply_style(self) -> None:
        """Video controls inherit the shared studio skin."""
        self.setStyleSheet("")
