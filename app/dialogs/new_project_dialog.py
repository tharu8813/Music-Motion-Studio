"""New project: identity, canvas, starting design (with a live preview), transitions,
content storage and the first songs -- everything Project Settings can change later."""

from __future__ import annotations

from collections.abc import Callable
from math import gcd
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor, QDragEnterEvent, QDropEvent, QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.dialogs.adaptive_dialog import AdaptiveDialog
from app.models.project import (
    DEFAULT_CROSSFADE_SECONDS, MAX_CROSSFADE_SECONDS, MIN_CROSSFADE_SECONDS, ProjectSettings,
)
from app.models.source import Source
from app.presets.preset_service import PresetDefinition
from app.presets.user_preset_service import all_presets
from app.services.m3u_playlist import PLAYLIST_FILE_EXTENSIONS
from app.services.playlist_service import AUDIO_EXTENSIONS
from app.utils.i18n import Translator


CANVAS_PRESETS: tuple[tuple[str, int, int], ...] = (
    ("16:9", 1280, 720),
    ("9:16", 720, 1280),
    ("1:1", 1080, 1080),
    ("4:3", 1440, 1080),
    ("3:4", 1080, 1440),
    ("8:19", 800, 1900),
    ("21:9", 1680, 720),
)
TRANSITIONS = ("none", "crossfade", "automix")
_PREVIEW_BOX = (272, 153)

PreviewSources = Callable[[PresetDefinition, float, float], list[Source]]
"""(preset, canvas width, canvas height) -> the preset's sources adapted to that canvas."""


class NewProjectDialog(AdaptiveDialog):
    """Choose how a new project starts; every choice stays editable in Project Settings."""

    def __init__(
        self, translator: Translator, parent: QWidget | None = None, *,
        preview_sources: PreviewSources | None = None,
    ) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("canvas", "start"), translator)
        self.translator = translator
        self._preview_sources = preview_sources
        self._preview_cache: dict[tuple[str, int, int], QPixmap] = {}
        self.music_paths: list[Path] = []
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(820)
        # Music files and playlists dragged from Explorer join the starting songs.
        self.setAcceptDrops(True)

        root, outer = self.build_scrollable_body()
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        self.description = QLabel()
        self.description.setObjectName("mutedLabel")
        self.description.setWordWrap(True)
        root.addWidget(self.description)
        columns = QHBoxLayout()
        columns.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(12)
        right = QVBoxLayout()
        right.setSpacing(12)
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        root.addLayout(columns)

        # -- left: identity, canvas, design ------------------------------------
        self.identity_group = QGroupBox()
        identity_form = QFormLayout(self.identity_group)
        self.title_label, self.author_label = QLabel(), QLabel()
        self.title_edit = QLineEdit()
        self.author_edit = QLineEdit()
        for edit in (self.title_edit, self.author_edit):
            edit.setClearButtonEnabled(True)
        identity_form.addRow(self.title_label, self.title_edit)
        identity_form.addRow(self.author_label, self.author_edit)
        left.addWidget(self.identity_group)

        self.canvas_group = QGroupBox()
        canvas_layout = QVBoxLayout(self.canvas_group)
        form = QFormLayout()
        canvas_layout.addLayout(form)
        self.preset_label = QLabel()
        self.preset_combo = QComboBox()
        for ratio, width, height in CANVAS_PRESETS:
            self.preset_combo.addItem(ratio, (width, height))
        self.preset_combo.addItem("", None)
        self.width_label = QLabel()
        self.width_spin = QSpinBox()
        self.width_spin.setRange(64, 16_384)
        self.width_spin.setSingleStep(2)
        self.width_spin.setValue(1280)
        self.height_label = QLabel()
        self.height_spin = QSpinBox()
        self.height_spin.setRange(64, 16_384)
        self.height_spin.setSingleStep(2)
        self.height_spin.setValue(720)
        self.summary = QLabel()
        self.summary.setObjectName("mutedLabel")
        form.addRow(self.preset_label, self.preset_combo)
        form.addRow(self.width_label, self.width_spin)
        form.addRow(self.height_label, self.height_spin)
        canvas_layout.addWidget(self.summary)
        left.addWidget(self.canvas_group)

        self.design_group = QGroupBox()
        design_form = QFormLayout(self.design_group)
        self.design_preset_label = QLabel()
        self.design_preset_combo = QComboBox()
        self.design_preset_combo.addItem("", None)
        for preset in all_presets():
            self.design_preset_combo.addItem(
                preset.name(self.translator.language.value), preset.identifier,
            )
        design_form.addRow(self.design_preset_label, self.design_preset_combo)
        left.addWidget(self.design_group)

        # -- right: preview (captioned with the design), transitions, storage -------------
        self.preview_label = QLabel()
        self.preview_label.setFixedSize(*_PREVIEW_BOX)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Under the preview, not in the left form: a wrapped label there squeezed that column.
        self.design_description = QLabel()
        self.design_description.setObjectName("mutedLabel")
        self.design_description.setWordWrap(True)
        self.design_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.preview_label, 0, Qt.AlignmentFlag.AlignHCenter)
        right.addWidget(self.design_description)

        self.options_group = QGroupBox()
        options_form = QFormLayout(self.options_group)
        self.transition_label = QLabel()
        self.transition_combo = QComboBox()
        for mode in TRANSITIONS:
            self.transition_combo.addItem("", mode)
        self.crossfade_label = QLabel()
        self.crossfade_spin = QDoubleSpinBox()
        self.crossfade_spin.setRange(MIN_CROSSFADE_SECONDS, MAX_CROSSFADE_SECONDS)
        self.crossfade_spin.setDecimals(1)
        self.crossfade_spin.setSingleStep(0.5)
        self.crossfade_spin.setValue(DEFAULT_CROSSFADE_SECONDS)
        self.crossfade_spin.setSuffix(" s")
        self.transition_help = QLabel()
        self.transition_help.setObjectName("mutedLabel")
        self.transition_help.setWordWrap(True)
        self.storage_label = QLabel()
        self.storage_combo = QComboBox()
        self.storage_combo.addItem("", "embed")
        self.storage_combo.addItem("", "reference")
        self.storage_help = QLabel()
        self.storage_help.setObjectName("mutedLabel")
        self.storage_help.setWordWrap(True)
        options_form.addRow(self.transition_label, self.transition_combo)
        options_form.addRow(self.crossfade_label, self.crossfade_spin)
        options_form.addRow("", self.transition_help)
        options_form.addRow(self.storage_label, self.storage_combo)
        options_form.addRow("", self.storage_help)
        right.addWidget(self.options_group)

        left.addStretch(1)
        right.addStretch(1)

        # -- bottom, full width: the first songs ---------------------------------------
        self.music_group = QGroupBox()
        music_layout = QHBoxLayout(self.music_group)
        self.music_list = QListWidget()
        self.music_list.setMaximumHeight(64)
        self.music_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.music_empty = QLabel()
        self.music_empty.setObjectName("mutedLabel")
        self.music_empty.setWordWrap(True)
        music_buttons = QVBoxLayout()
        self.add_music_button = QPushButton()
        self.remove_music_button = QPushButton()
        self.music_count = QLabel()  # shown in the group title
        music_buttons.addWidget(self.add_music_button)
        music_buttons.addWidget(self.remove_music_button)
        music_layout.addWidget(self.music_empty, 1)
        music_layout.addWidget(self.music_list, 1)
        music_layout.addLayout(music_buttons)
        root.addWidget(self.music_group)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        create_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if create_button is not None:
            create_button.setProperty("primary", True)
        outer.addWidget(self.buttons)
        remove_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.music_list)
        remove_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        remove_shortcut.activated.connect(self._remove_music)

        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        self.design_preset_combo.currentIndexChanged.connect(self._design_preset_changed)
        self.width_spin.valueChanged.connect(self._update_summary)
        self.height_spin.valueChanged.connect(self._update_summary)
        self.transition_combo.currentIndexChanged.connect(self._update_options)
        self.storage_combo.currentIndexChanged.connect(self._update_options)
        self.add_music_button.clicked.connect(self._choose_music)
        self.remove_music_button.clicked.connect(self._remove_music)
        self.music_list.itemSelectionChanged.connect(self._update_music)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()
        self._preset_changed(0)
        self.resize(860, self.body_scroll.widget().sizeHint().height() + 90)

    # -- results ------------------------------------------------------------------

    @property
    def canvas_size(self) -> tuple[int, int]:
        """Return the canvas dimensions chosen for the new project."""
        return self.width_spin.value(), self.height_spin.value()

    @property
    def selected_design_preset(self) -> PresetDefinition | None:
        """Return the optional design used to populate the new project."""
        identifier = self.design_preset_combo.currentData()
        return next(
            (preset for preset in all_presets()
             if preset.identifier == identifier),
            None,
        )

    @property
    def project_settings(self) -> ProjectSettings:
        """The new project's identity, transitions and content storage."""
        return ProjectSettings(
            title=self.title_edit.text().strip() or "Untitled Project",
            author=self.author_edit.text().strip(),
            content_mode=self.storage_combo.currentData(),
            transition_mode=self.transition_combo.currentData(),
            crossfade_seconds=self.crossfade_spin.value(),
        )

    # -- behavior ---------------------------------------------------------------------

    def _korean(self) -> bool:
        return self.translator.is_korean

    def _preset_changed(self, index: int) -> None:
        size = self.preset_combo.itemData(index)
        custom = size is None
        self.width_spin.setEnabled(custom)
        self.height_spin.setEnabled(custom)
        self.width_label.setVisible(custom)
        self.width_spin.setVisible(custom)
        self.height_label.setVisible(custom)
        self.height_spin.setVisible(custom)
        if not custom:
            width, height = size
            self.width_spin.setValue(width)
            self.height_spin.setValue(height)
        self._update_summary()

    def _design_preset_changed(self, _index: int) -> None:
        preset = self.selected_design_preset
        korean = self._korean()
        self.design_description.setText(
            preset.description(self.translator.language.value)
            if preset else (
                "기본 배경, 제목 및 진행 표시줄로 시작합니다."
                if korean else
                "Start with the default background, title, and progress bar."
            )
        )
        self._update_preview()

    def _update_summary(self) -> None:
        width, height = self.canvas_size
        divisor = gcd(width, height)
        ratio = f"{width // divisor}:{height // divisor}"
        korean = self._korean()
        if self.preset_combo.currentData() is None:
            self.summary.setText(
                f"사용자 지정 캔버스 {width} × {height}  ·  화면 비율 {ratio}"
                if korean else f"Custom canvas {width} × {height}  ·  Aspect ratio {ratio}"
            )
        else:
            self.summary.setText(
                f"화면 비율 {ratio}  ·  캔버스 {width} × {height}" if korean
                else f"Aspect ratio {ratio}  ·  Canvas {width} × {height}"
            )
        self._update_preview()

    def _update_preview(self) -> None:
        """The chosen design on the chosen canvas, letterboxed in a fixed box."""
        width, height = self.canvas_size
        box_width, box_height = _PREVIEW_BOX
        scale = min(box_width / width, box_height / height)
        target = (max(1, round(width * scale)), max(1, round(height * scale)))
        preset = self.selected_design_preset
        pixmap = None
        if preset is not None and self._preview_sources is not None:
            key = (preset.identifier, width, height)
            pixmap = self._preview_cache.get(key)
            if pixmap is None:
                from app.presets.preset_preview import render_preset_thumbnail

                pixmap = render_preset_thumbnail(self._preview_sources(preset, width, height), *target)
                self._preview_cache[key] = pixmap
        canvas = QPixmap(box_width, box_height)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF((box_width - target[0]) / 2, (box_height - target[1]) / 2, *target)
        if pixmap is not None:
            painter.drawPixmap(frame.toRect(), pixmap)
        else:  # the default project, drawn as its layout: title top-left, progress bar at the bottom
            painter.fillRect(frame, QColor("#202A29"))
            text = self.palette().color(QPalette.ColorRole.Text)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(text.red(), text.green(), text.blue(), 150))
            margin = frame.width() * 0.08
            painter.drawRoundedRect(QRectF(frame.left() + margin, frame.top() + frame.height() * 0.12,
                                           frame.width() * 0.45, max(4.0, frame.height() * 0.07)), 2, 2)
            painter.setBrush(QColor("#79C7B4"))
            painter.drawRoundedRect(QRectF(frame.left() + margin, frame.bottom() - frame.height() * 0.17,
                                           (frame.width() - 2 * margin) * 0.4, 3), 1.5, 1.5)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1))
        painter.drawRect(frame.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()
        self.preview_label.setPixmap(canvas)

    def _update_options(self, *_args: object) -> None:
        korean = self._korean()
        mode = self.transition_combo.currentData()
        crossfade = mode == "crossfade"
        self.crossfade_label.setVisible(crossfade)
        self.crossfade_spin.setVisible(crossfade)
        self.transition_help.setText({
            "none": ("한 곡이 끝나면 다음 곡이 바로 시작합니다." if korean
                     else "Each track starts as soon as the previous one ends."),
            "crossfade": ("곡이 끝나기 지정한 초 전부터 다음 곡이 겹쳐 재생됩니다(분석 없음)." if korean
                          else "The next track fades in over the last seconds of each track (no analysis)."),
            "automix": ("곡을 분석해 템포에 맞춰 이어 줍니다. 맞지 않는 곡은 크로스페이드로 대체됩니다." if korean
                        else "Tracks are analyzed and blended on tempo; mismatched ones fall back to a crossfade."),
        }[mode])
        self.storage_help.setText(
            ("이미지·음원·폰트·가사를 프로젝트 안에 복사합니다. 파일은 커지지만 다른 PC에서도 열립니다."
             if korean else "Copies images, audio, fonts and lyrics into the project: larger, but portable.")
            if self.storage_combo.currentData() == "embed" else
            ("원본 파일 경로를 씁니다. 프로젝트는 작지만 원본을 옮기면 다시 연결해야 합니다."
             if korean else "Keeps the original file paths: small, but moved files must be relinked.")
        )

    def _choose_music(self) -> None:
        korean = self._korean()
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "시작 음악 선택" if korean else "Choose starting music",
            "",
            ("음악 파일 및 플레이리스트" if korean else "Audio files and playlists")
            + " (*.mp3 *.wav *.flac *.aac *.m4a *.ogg *.m3u8 *.m3u)",
        )
        self._add_music_files(paths)

    def _add_music_files(self, paths: list[str]) -> int:
        """Append supported, not-yet-listed music files or playlists; return how many."""
        addable = AUDIO_EXTENSIONS | PLAYLIST_FILE_EXTENSIONS
        known = {str(path).casefold() for path in self.music_paths}
        added = 0
        for raw in paths:
            path = Path(raw)
            if path.suffix.lower() in addable and str(path).casefold() not in known:
                self.music_paths.append(path)
                known.add(str(path).casefold())
                self.music_list.addItem(path.name)
                self.music_list.item(self.music_list.count() - 1).setToolTip(str(path))
                added += 1
        self._update_music()
        return added

    @staticmethod
    def _dropped_music(event: QDragEnterEvent | QDropEvent) -> list[str]:
        if not event.mimeData().hasUrls():
            return []
        addable = AUDIO_EXTENSIONS | PLAYLIST_FILE_EXTENSIONS
        return [
            url.toLocalFile() for url in event.mimeData().urls()
            if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in addable
        ]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt API name
        if self._dropped_music(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt API name
        paths = self._dropped_music(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._add_music_files([path for path in paths if Path(path).is_file()])

    def _remove_music(self) -> None:
        for row in sorted((self.music_list.row(item) for item in self.music_list.selectedItems()), reverse=True):
            self.music_list.takeItem(row)
            del self.music_paths[row]
        self._update_music()

    def _update_music(self) -> None:
        korean = self._korean()
        count = len(self.music_paths)
        playlists = sum(1 for path in self.music_paths if path.suffix.lower() in PLAYLIST_FILE_EXTENSIONS)
        self.music_list.setVisible(count > 0)
        self.music_empty.setVisible(count == 0)
        self.remove_music_button.setEnabled(bool(self.music_list.selectedItems()))
        self.music_count.setText(
            "" if not count else
            (f"파일 {count}개" + (f" (플레이리스트 {playlists}개)" if playlists else "")) if korean else
            (f"{count} file(s)" + (f" ({playlists} playlist(s))" if playlists else ""))
        )
        title = "시작 음악 (선택)" if korean else "Starting music (optional)"
        self.music_group.setTitle(f"{title}  ·  {self.music_count.text()}" if count else title)

    def retranslate(self) -> None:
        korean = self._korean()
        selected_identifier = self.design_preset_combo.currentData()
        self.setWindowTitle("새 프로젝트" if korean else "New project")
        self.description.setText(
            "새 프로젝트를 어떻게 시작할지 정하세요. 여기서 고른 내용은 모두 나중에 프로젝트 설정에서 바꿀 수 있습니다."
            if korean else
            "Choose how the new project starts. Everything here can be changed later in Project Settings."
        )
        self.identity_group.setTitle("프로젝트 정보" if korean else "Project information")
        self.title_label.setText("이름" if korean else "Name")
        self.author_label.setText("작성자" if korean else "Author")
        self.title_edit.setPlaceholderText("예: 새벽 감성 플레이리스트" if korean else "e.g. Late night playlist")
        self.author_edit.setPlaceholderText(
            "선택 사항 · 설명은 프로젝트 설정에서" if korean else "Optional · description in Project Settings"
        )
        self.canvas_group.setTitle("화면 비율 및 캔버스" if korean else "Aspect ratio and canvas")
        self.preset_label.setText("화면 비율" if korean else "Aspect ratio")
        self.width_label.setText("너비" if korean else "Width")
        self.height_label.setText("높이" if korean else "Height")
        self.preset_combo.setItemText(self.preset_combo.count() - 1, "사용자 지정" if korean else "Custom")
        self.width_spin.setSuffix(" px")
        self.height_spin.setSuffix(" px")
        # A tooltip, not a wrapped label: in this narrow column it broke the column's height.
        self.canvas_group.setToolTip(
            "내보내기 해상도는 출력 품질 설정이며 이 프로젝트의 화면 비율을 변경하지 않습니다."
            if korean else
            "Export resolution controls output quality and does not change this project's aspect ratio."
        )
        self.design_group.setTitle("시작 디자인" if korean else "Starting design")
        self.design_preset_label.setText("디자인 프리셋" if korean else "Design preset")
        self.design_preset_combo.setItemText(0, "기본 프로젝트" if korean else "Default project")
        for index, preset in enumerate(all_presets(), start=1):
            if index < self.design_preset_combo.count():
                self.design_preset_combo.setItemText(index, preset.name(self.translator.language.value))
        selected_index = self.design_preset_combo.findData(selected_identifier)
        self.design_preset_combo.setCurrentIndex(max(0, selected_index))
        self.options_group.setTitle("재생과 저장" if korean else "Playback and storage")
        self.transition_label.setText("곡 전환" if korean else "Transitions")
        for index, (ko, en) in enumerate((
            ("없음 (즉시 전환)", "None (instant cut)"),
            ("크로스페이드", "Crossfade"),
            ("AutoMix (템포 인식 자동 전환)", "AutoMix (tempo-aware)"),
        )):
            self.transition_combo.setItemText(index, ko if korean else en)
        self.crossfade_label.setText("전환 길이" if korean else "Crossfade length")
        self.storage_label.setText("콘텐츠 저장" if korean else "Content storage")
        self.storage_combo.setItemText(0, "프로젝트에 포함" if korean else "Include in the project")
        self.storage_combo.setItemText(1, "원본 위치 참조" if korean else "Reference original files")
        self.music_empty.setText(
            "만들면서 플레이리스트에 넣을 음악 파일이나 M3U8 플레이리스트를 고르거나 이 창으로 끌어오세요."
            if korean else
            "Pick music files or an M3U8 playlist to put in the Playlist right away, or drop them here."
        )
        self.add_music_button.setText("음악 추가…" if korean else "Add music…")
        self.remove_music_button.setText("선택 제거" if korean else "Remove selected")
        self._design_preset_changed(self.design_preset_combo.currentIndex())
        self._update_options()
        self._update_music()
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setText("프로젝트 만들기" if korean else "Create project")
        cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText("취소" if korean else "Cancel")
        for label, field in (
            (self.title_label, self.title_edit), (self.author_label, self.author_edit),
            (self.preset_label, self.preset_combo), (self.width_label, self.width_spin),
            (self.height_label, self.height_spin),
            (self.design_preset_label, self.design_preset_combo),
            (self.transition_label, self.transition_combo),
            (self.crossfade_label, self.crossfade_spin), (self.storage_label, self.storage_combo),
        ):
            label.setBuddy(field)
            field.setAccessibleName(label.text())
        self._update_summary()
