"""Edit cue motion and previous/current/next styles with a real renderer preview."""

from copy import deepcopy
from dataclasses import replace

from PySide6.QtCore import QElapsedTimer, QTimer, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from app.animation.lyrics import INTRO_EXIT_LABELS, INTRO_STYLE_LABELS, LYRIC_EFFECT_LABELS, role_style
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.help_dialog import install_help_shortcut
from app.models.playlist import PlaylistTrack
from app.models.source import Source
from app.preview.canvas_snapshot import CanvasSnapshot
from app.ui.design_system import COLORS
from app.utils.i18n import Translator


class LyricsAnimationDialog(QDialog):
    HELP = {
        "subtitle_animation": ("가사 줄이 바뀔 때의 효과입니다. 없음은 즉시 전환하고, 순차 이동은 다음 가사에만 시차를 적용합니다.", "Effect when cues change. None switches instantly; staggered flow delays only upcoming cues."),
        "subtitle_animation_duration": ("한 번의 줄 전환이 끝나는 시간입니다. 인트로 임시 줄도 이 시간을 따릅니다.", "Time for a cue transition, including temporary intro rows."),
        "subtitle_flow_direction": ("다음 가사가 현재 위치로 들어오고 이전 가사가 나가는 방향입니다.", "Direction in which upcoming cues enter and previous cues leave."),
        "subtitle_motion_easing": ("이동 속도가 변하는 방식입니다. 효과에 맞게는 선택한 효과에 권장되는 속도 곡선을 사용합니다.", "How transition speed changes. Match effect selects the effect's default easing."),
        "subtitle_motion_distance": ("기본 줄 이동에 추가되는 거리입니다. 라이즈·슬라이드·바운스에 적용합니다.", "Extra travel added to the cue flow for Rise, Slide and Bounce."),
        "subtitle_stagger": ("순차 이동에서 다음 가사에 줄별 지연을 줍니다. 0이면 함께 이동하며 전체 이동은 전환 시간 안에 끝납니다.", "Delay between upcoming cues in staggered flow. Zero moves them together; all finish within the transition duration."),
        "subtitle_stagger_order": ("순차 이동에서 현재 가사에 가까운 줄 또는 먼 줄부터 이동시킵니다.", "Move nearest or farthest upcoming cues first in staggered flow."),
        "subtitle_zoom_amount": ("전환 중 크기가 변하는 정도입니다. 글로우·줌 효과에 적용합니다.", "Scale change during Glow and Zoom transitions."),
        "subtitle_glow_strength": ("글로우 효과의 빛 밝기입니다. 줄 자체의 투명도와 별도로 적용합니다.", "Brightness of the Glow halo, separate from lyric opacity."),
        "subtitle_glow_radius": ("글로우 빛이 글자 밖으로 퍼지는 범위입니다.", "How far the Glow halo spreads beyond the glyphs."),
        "subtitle_anchor": ("현재 가사와 인트로 줄의 기준 위치입니다. 0은 위/왼쪽, 0.5는 중앙, 1은 아래/오른쪽입니다.", "Anchor for current cues and intro rows: 0 top/left, 0.5 center, 1 bottom/right."),
        "text_alignment": ("각 가사 영역 안에서 글자를 정렬합니다. 줄이 이동하는 방향과는 별개입니다.", "Text alignment inside each cue area, independent of flow direction."),
        "subtitle_context_lines": ("현재 가사 앞에 표시할 이전 가사 묶음 수입니다. 자동은 요소 크기에 맞추고 0은 숨깁니다. 한 묶음에 여러 줄이 있을 수 있습니다.", "Previous cue groups to display. Auto fits the source; zero hides them. A group may contain multiple text lines."),
        "subtitle_next_lines": ("미리 보여 줄 다음 가사 묶음 수입니다. 자동은 요소 크기에 맞추고 0은 숨깁니다.", "Upcoming cue groups shown in advance. Auto fits the source; zero hides them."),
        "subtitle_line_spacing": ("세로 이동에서는 가사 줄 간격, 가로 이동에서는 가사 영역 사이의 여백입니다.", "Gap between text rows in vertical flow and between cue columns in horizontal flow."),
        "subtitle_previous_distance_fade": ("이전 가사가 현재 위치에 가까울수록 블러를 약하게 하고 불투명하게 표시합니다. 0은 동일, 1은 거리별로 적용합니다.", "Make nearby previous cues clearer and more opaque. Zero is uniform; one applies full distance falloff."),
        "subtitle_next_distance_fade": ("다음 가사가 현재 위치에 가까울수록 블러를 약하게 하고 불투명하게 표시합니다. 이전 가사와 별도로 적용합니다.", "Make nearby upcoming cues clearer and more opaque, independently of previous cues."),
        "subtitle_intro_enabled": ("첫 가사가 시작되기 전 임시 대기 줄을 표시합니다. 첫 가사 전환과 함께 사라집니다.", "Show a temporary waiting row before the first lyric; it exits with the first cue transition."),
        "subtitle_intro_midtrack": ("긴 자막 공백을 보컬 분석하고, 음악이 있지만 보컬이 없는 구간에만 임시 줄을 표시합니다.", "Analyze long lyric gaps and show a temporary row only when music plays without vocals."),
        "subtitle_intro_style": ("대기 중 표시할 점·막대·링·공전·파동·빛의 움직임입니다. 퇴장 효과와 따로 선택할 수 있습니다.", "Waiting animation: dots, bars, ring, orbit, ripples or comet. Choose its exit separately."),
        "subtitle_intro_exit": ("대기 줄이 끝날 때 사라지는 방식입니다. 가사 이동과 같은 시간에 끝나며, 대기 표시 스타일과 독립적으로 적용됩니다.", "How the waiting indicator disappears. Ends with the lyric transition, independently of its waiting style."),
        "subtitle_intro_gap": ("중간 대기 줄을 표시할 최소 무보컬 구간 길이입니다. 첫 가사 전 대기에는 적용하지 않습니다.", "Minimum vocal-free interlude length. Does not affect the wait before the first lyric."),
        "subtitle_intro_period": ("점이 차례로 밝아지는 등 표시 스타일이 한 번 반복되는 시간입니다. 줄 전환 시간과는 별개입니다.", "Duration of one indicator cycle, separate from cue transition duration."),
        "subtitle_intro_scale": ("현재 가사 글꼴 크기를 기준으로 임시 표시의 크기를 조절합니다.", "Indicator size relative to the current lyric font."),
        "role_scale": ("기본 또는 줄별 글꼴 크기에 곱할 배율입니다. 1이면 원래 크기를 유지합니다.", "Multiplier for the base or per-line font size. One preserves the original size."),
        "role_opacity": ("0은 투명, 1은 불투명입니다. 거리별 흐림을 켜면 이전·다음의 가장 먼 가사 값으로 사용합니다.", "Zero is transparent, one opaque. With falloff, this sets the farthest previous or upcoming cue."),
        "role_blur": ("글자 흐림 반경입니다. 0은 선명합니다. 거리별 흐림을 켜면 가까운 가사의 블러가 더 약해집니다.", "Glyph blur radius. Zero is sharp; distance falloff reduces blur on nearby cues."),
    }
    SETTINGS = (
        "subtitle_animation", "subtitle_animation_duration", "subtitle_flow_direction",
        "subtitle_motion_easing", "subtitle_motion_distance", "subtitle_stagger",
        "subtitle_stagger_order", "subtitle_anchor", "subtitle_zoom_amount",
        "subtitle_glow_strength", "subtitle_glow_radius", "subtitle_context_lines",
        "subtitle_next_lines", "subtitle_line_spacing", "text_alignment", "subtitle_role_styles",
        "subtitle_previous_distance_fade", "subtitle_next_distance_fade",
        "subtitle_intro_enabled", "subtitle_intro_midtrack", "subtitle_intro_style", "subtitle_intro_exit",
        "subtitle_intro_gap", "subtitle_intro_period", "subtitle_intro_scale",
    )

    def __init__(self, source: Source, translator: Translator, parent=None):
        super().__init__(parent)
        self._translator = translator
        install_help_shortcut(self, ("canvas", "lyrics_transition"), translator=translator)
        self._korean = translator.is_korean
        self._loading = True
        self._basic = Source.from_dict(source.to_dict())
        self._draft = Source.from_dict(source.to_dict() | source.subtitle_advanced_settings | {
            "subtitle_advanced_categories": [], "subtitle_advanced_settings": {},
        })
        self._styles = {role: {key: value for key, value in role_style(self._draft, role).items()
                              if key in {"scale", "opacity", "blur"}}
                        for role in ("previous", "current", "next")}
        self.controls = {}
        self.category_toggles = {}
        self.category_panels = {}
        self.category_notes = {}
        self.setObjectName("lyricsAnimationDialog")
        self.setWindowTitle(self._tr("자막 전환 설정", "Subtitle transition settings"))
        self.setMinimumSize(840, 600)
        self.resize(980, 700)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)
        title = QLabel(self.windowTitle())
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        hint = QLabel(self._tr(
            "기본 설정은 속성 패널에서, 고급 설정은 이 창에서 조절합니다. 고급 모드가 꺼진 카테고리의 값은 적용하지 않습니다.",
            "Edit basic settings in Properties. Advanced values are ignored for categories whose advanced mode is off.",
        ))
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)
        body = QHBoxLayout()
        body.setSpacing(22)
        tabs = QTabWidget()
        self.tabs = tabs
        tabs.setFixedWidth(350)
        body.addWidget(tabs)
        motion_page = self._page(tabs, "motion", "움직임", "Motion")
        motion = self._group(motion_page, "전환 효과", "Transition effect")
        self._combo(motion, "subtitle_animation", "전환 효과", "Effect", LYRIC_EFFECT_LABELS)
        self._spin(motion, "subtitle_animation_duration", "전환 시간", "Duration", 0.05, 3, 0.05, " s")
        motion = self._group(motion_page, "이동 방향과 속도", "Direction and speed")
        self._combo(motion, "subtitle_flow_direction", "이동 방향", "Flow direction", {
            "up": ("아래 → 위", "Bottom → top"), "down": ("위 → 아래", "Top → bottom"),
            "left": ("오른쪽 → 왼쪽", "Right → left"), "right": ("왼쪽 → 오른쪽", "Left → right"),
        })
        self._combo(motion, "subtitle_motion_easing", "속도 곡선", "Easing", {
            "auto": ("효과에 맞게", "Match effect"), "smooth": ("부드럽게", "Ease in and out"),
            "linear": ("일정하게", "Linear"), "ease_in": ("점점 빠르게", "Ease in"),
            "ease_out": ("점점 느리게", "Ease out"),
        })
        self._spin(motion, "subtitle_motion_distance", "추가 이동 거리", "Extra travel", 0, 200, 1, " px")
        motion = self._group(motion_page, "다음 가사 순차 이동", "Upcoming cue sequence")
        self._spin(motion, "subtitle_stagger", "다음 가사 이동 시차", "Upcoming cue delay", 0, 0.8, 0.05)
        self._combo(motion, "subtitle_stagger_order", "다음 가사 이동 순서", "Upcoming cue order", {
            "near_first": ("가까운 가사부터", "Nearest first"), "far_first": ("먼 가사부터", "Farthest first"),
        })
        motion = self._group(motion_page, "크기와 글로우", "Scale and glow")
        self._spin(motion, "subtitle_zoom_amount", "크기 변화 세기", "Zoom amount", 0, 0.5, 0.05)
        self._spin(motion, "subtitle_glow_strength", "글로우 세기", "Glow strength", 0, 1, 0.05)
        self._spin(motion, "subtitle_glow_radius", "글로우 반경", "Glow radius", 0, 30, 0.5, " px")

        layout_page = self._page(tabs, "layout", "줄 배치", "Layout")
        layout_form = self._group(layout_page, "기준 위치와 정렬", "Position and alignment")
        self._spin(layout_form, "subtitle_anchor", "현재 가사 기준 위치", "Current cue anchor", 0, 1, 0.05)
        anchor_hint = QLabel(self._tr("0은 위/왼쪽, 0.5는 중앙, 1은 아래/오른쪽입니다.",
                                      "0: top/left · 0.5: center · 1: bottom/right"))
        anchor_hint.setObjectName("mutedLabel")
        anchor_hint.setWordWrap(True)
        layout_form.addRow(anchor_hint)
        self._combo(layout_form, "text_alignment", "글자 정렬", "Text alignment", {
            "left": ("왼쪽", "Left"), "center": ("가운데", "Center"), "right": ("오른쪽", "Right"),
        })
        layout_form = self._group(layout_page, "표시할 줄과 간격", "Visible cues and spacing")
        for key, ko, en in (("subtitle_context_lines", "이전 가사 수", "Previous cues"),
                            ("subtitle_next_lines", "다음 가사 수", "Next cues")):
            spin = QSpinBox()
            spin.setRange(-1, 6)
            spin.setSpecialValueText(self._tr("자동", "Auto"))
            spin.setValue(getattr(self._draft, key))
            self.controls[key] = spin
            layout_form.addRow(self._tr(ko, en), spin)
            self._help(layout_form, key, spin)
        self._spin(layout_form, "subtitle_line_spacing", "줄 간격", "Line spacing", 0, 120, 1, " px")

        role_page = self._page(tabs, "styles", "줄 스타일", "Role styles")
        role_form = self._group(role_page, "스타일 편집 대상", "Style target")
        self.role = QComboBox()
        for key, label in (("previous", self._tr("이전 가사", "Previous")),
                           ("current", self._tr("현재 가사", "Current")),
                           ("next", self._tr("다음 가사", "Next"))):
            self.role.addItem(label, key)
        role_form.addRow(self._tr("편집할 위치", "Role"), self.role)
        self.role.setToolTip(self._tr("이전·현재·다음 가사의 스타일을 각각 편집합니다.", "Edit previous, current and next styles independently."))
        self.role_scale = self._number(0.25, 3, 0.05)
        self.role_opacity = self._number(0, 1, 0.05)
        self.role_blur = self._number(0, 12, 0.5)
        self.role_blur.setSuffix(" px")
        role_form = self._group(role_page, "크기 배율", "Size scale")
        role_form.addRow(self._tr("크기 배율", "Size scale"), self.role_scale)
        self._help(role_form, "role_scale", self.role_scale)
        role_form = self._group(role_page, "불투명도와 블러", "Opacity and blur")
        for ko, en, key, widget in (("불투명도", "Opacity", "role_opacity", self.role_opacity),
                                     ("블러", "Blur", "role_blur", self.role_blur)):
            role_form.addRow(self._tr(ko, en), widget)
            self._help(role_form, key, widget)
        role_form = self._group(role_page, "거리에 따른 흐림", "Distance falloff")
        self.distance_group = role_form.parentWidget()
        self.distance_labels = {}
        for role in ("previous", "next"):
            key = f"subtitle_{role}_distance_fade"
            self._spin(role_form, key, "거리별 흐림 강도", "Distance falloff", 0, 1, 0.05)
            self.distance_labels[role] = role_form.labelForField(self.controls[key])
        self.distance_hint = QLabel(self._tr(
            "현재 가사에 가까울수록 블러는 약해지고 선명해집니다. 위 불투명도·블러는 선택한 쪽의 가장 먼 가사 값입니다. 0은 모든 줄에 동일하게, 1은 거리별로 적용합니다. 이전·다음은 각각 설정됩니다.",
            "Closer cues become clearer. Opacity and blur above set the farthest cue on this side. 0 keeps cues uniform; 1 varies them by distance. Previous and next are configured separately.",
        ))
        self.distance_hint.setObjectName("mutedLabel")
        self.distance_hint.setWordWrap(True)
        role_form.addRow(self.distance_hint)
        role_hint = QLabel(self._tr(
            "여기서는 크기 배율·불투명도·블러만 조절합니다. 색상과 글꼴은 속성 패널 및 줄별 스타일에서 설정하며, 고급 모드를 켜도 계속 편집할 수 있습니다.",
            "Only size scale, opacity and blur are set here. Colors and typography remain editable in Properties and Per-line styles, even with advanced mode on.",
        ))
        role_hint.setObjectName("mutedLabel")
        role_hint.setWordWrap(True)
        role_page.addWidget(role_hint)

        intro_page = self._page(tabs, "intro", "인트로", "Intro")
        intro = self._group(intro_page, "표시 조건", "When to show")
        for key, ko, en in (
            ("subtitle_intro_enabled", "첫 가사 전 대기 표시", "Show before the first lyric"),
            ("subtitle_intro_midtrack", "곡 중간의 간주에도 표시", "Show during instrumental interludes"),
        ):
            check = QCheckBox(self._tr(ko, en))
            check.setChecked(getattr(self._draft, key))
            self.controls[key] = check
            intro.addRow(check)
            self._help(intro, key, check)
        self._spin(intro, "subtitle_intro_gap", "최소 간주 길이", "Minimum interlude", 2, 30, 0.5, " s")
        intro = self._group(intro_page, "표시 스타일", "Indicator appearance")
        self._combo(intro, "subtitle_intro_style", "표시 스타일", "Indicator style", INTRO_STYLE_LABELS)
        self._combo(intro, "subtitle_intro_exit", "퇴장 효과", "Exit effect", INTRO_EXIT_LABELS)
        self._spin(intro, "subtitle_intro_period", "한 사이클 시간", "Cycle duration", 1, 10, 0.25, " s")
        self._spin(intro, "subtitle_intro_scale", "표시 크기", "Indicator scale", 0.25, 3, 0.05)
        intro_hint = QLabel(self._tr(
            "첫 가사 전에는 목록 앞에, 중간 간주에서는 직전·다음 가사 사이에 임시 줄이 추가됩니다. 기준 위치를 유지하며 끝나면 사라집니다. 색상·투명도는 현재 가사 스타일을 따릅니다. "
            "중간 표시는 긴 자막 텀을 배경에서 보컬 분석한 뒤, 음악이 있고 보컬이 없는 구간에만 적용합니다.",
            "A temporary row precedes the first lyric or sits between the previous and next cues during an interlude. It keeps the current cue anchor and disappears when finished. Color and opacity follow the current lyric style. "
            "Interludes are analyzed in the background and appear only in long lyric gaps with music and no vocals.",
        ))
        intro_hint.setObjectName("mutedLabel")
        intro_hint.setWordWrap(True)
        intro.addRow(intro_hint)
        self.intro_preview_mode = QComboBox()
        self.intro_preview_mode.addItem(self._tr("첫 가사 전", "Before first lyric"), "start")
        self.intro_preview_mode.addItem(self._tr("곡 중간 · 보컬 없는 예시", "Mid-track · instrumental sample"), "middle")
        self.intro_preview_mode.addItem(self._tr("첫 가사 · 퇴장 확인", "First lyric · exit preview"), "start_exit")
        self.intro_preview_mode.addItem(self._tr("간주 · 퇴장 확인", "Interlude · exit preview"), "middle_exit")
        intro_preview = self._group(intro_page, "예시 미리보기", "Sample preview")
        intro_preview.addRow(self._tr("미리보기 구간", "Preview segment"), self.intro_preview_mode)
        self.intro_preview_mode.setToolTip(self._tr("미리보기 예시만 바꿉니다. 곡의 실제 가사나 분석 구간은 변경하지 않습니다.",
                                                   "Changes only the preview sample, not the track lyrics or analyzed intervals."))

        preview_column = QVBoxLayout()
        caption = QLabel(self._tr("실시간 미리보기", "Live preview"))
        caption.setObjectName("panelTitle")
        preview_header = QHBoxLayout()
        preview_header.addWidget(caption)
        preview_header.addStretch()
        preview_header.addWidget(QLabel(self._tr("미리보기 줄 수", "Preview cues")))
        self.preview_count = QSpinBox()
        self.preview_count.setRange(0, 13)
        self.preview_count.setSpecialValueText(self._tr("자동", "Auto"))
        self.preview_count.setAccessibleName(self._tr("미리보기 가사 수", "Preview cue count"))
        self.preview_count.setToolTip(self._tr(
            "미리보기에서만 표시할 가사 수입니다. 자동은 적용될 줄 배치 설정을 따릅니다. 영상의 가사 수는 줄 배치 탭에서 설정하세요.",
            "Preview only. Auto follows the effective layout. Set the actual video cue counts in the Layout tab.",
        ))
        preview_header.addWidget(self.preview_count)
        preview_column.addLayout(preview_header)
        self.effective_summary = QLabel()
        self.effective_summary.setObjectName("infoCallout")
        self.effective_summary.setWordWrap(True)
        self.effective_summary.setAccessibleName(self._tr("영상 적용 설정 요약", "Effective video settings"))
        preview_column.addWidget(self.effective_summary)
        self.preview = QLabel()
        self.preview.setObjectName("subtitleMotionPreview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(400, 300)
        preview_column.addWidget(self.preview, 1)
        preview_note = QLabel(self._tr("예시 가사 · 실제 영상과 같은 전환 효과", "Sample lyrics · same effects as your video"))
        preview_note.setObjectName("mutedLabel")
        preview_column.addWidget(preview_note)
        replay = QPushButton(self._tr("다시 재생", "Replay"))
        replay.setAutoDefault(False)
        replay.setToolTip(self._tr("현재 적용될 설정으로 예시 가사 전환을 처음부터 재생합니다.", "Replay sample transitions using the effective settings."))
        replay.clicked.connect(self._replay)
        self.pause = QPushButton(self._tr("일시정지", "Pause"))
        self.pause.setCheckable(True)
        self.pause.setAutoDefault(False)
        self.pause.setToolTip(self._tr("전환을 멈춰 현재 프레임의 스타일을 비교합니다.", "Freeze the transition to compare styles on the current frame."))
        self.pause.toggled.connect(self._pause_preview)
        playback = QHBoxLayout()
        playback.addWidget(replay)
        playback.addWidget(self.pause)
        playback.addStretch()
        preview_column.addLayout(playback)
        body.addLayout(preview_column, 1)
        root.addLayout(body, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self._tr("설정 적용", "Apply settings"))
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(self._tr("변경 취소", "Discard changes"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._scene = CanvasScene(self)
        self._scene.set_artboard_size(680, 300)
        self._scene.artboard_color = QColor(COLORS["shadow"])
        self._preview_source = replace(self._draft, x=0, y=0, width=680, height=300, scale=1,
                                       rotation=0, opacity=1, timeline_start=0, timeline_duration=0,
                                       animation_in="none", animation_out="none", loop_motion="none",
                                       personal_color_enabled=False)
        self._item = SourceItem(self._preview_source)
        self._scene.addItem(self._item)
        sample_lines = ("이전 가사는 멀어지고", "지금 이 가사를 부르고", "다음 가사가 다가와", "새로운 줄이 이어져") if self._korean else (
            "The previous line moves on", "This is the current lyric", "The next line comes into view", "Another line follows",
        )
        self._preview_texts = list(sample_lines) * 4
        self._preview_texts[len(self._preview_texts) // 2] = self._tr(
            "♪ 쉿, 이 줄을 찾은 당신은 가사 장인 ♪", "♪ Secret unlocked: you are a lyric wizard ♪",
        )
        self._clock = QElapsedTimer()
        self._elapsed_offset = 0.0
        self._paused_seconds = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._render_preview)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_preview)
        for widget in self.controls.values():
            signal = widget.currentIndexChanged if isinstance(widget, QComboBox) else (
                widget.toggled if isinstance(widget, QCheckBox) else widget.valueChanged)
            signal.connect(self._changed)
        tabs.currentChanged.connect(self._refresh_preview)
        self.intro_preview_mode.currentIndexChanged.connect(self._refresh_preview)
        self.preview_count.valueChanged.connect(self._change_preview_count)
        for toggle in self.category_toggles.values():
            toggle.toggled.connect(self._changed)
        self.role.currentIndexChanged.connect(self._load_role)
        for widget in (self.role_scale, self.role_opacity, self.role_blur):
            widget.valueChanged.connect(self._store_role)
        self._loading = False
        self._load_role()
        self._changed()

    def _tr(self, ko, en):
        return ko if self._korean else en

    @staticmethod
    def _number(minimum, maximum, step):
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        spin.setKeyboardTracking(False)
        return spin

    def _page(self, tabs, category, ko, en):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 16, 10, 10)
        toggle = QCheckBox(self._tr(f"{ko} 고급 모드", f"Advanced {en.lower()}"))
        toggle.setChecked(category in self._basic.subtitle_advanced_categories)
        toggle.setToolTip(self._tr("켜면 이 카테고리의 아래 값만 우선 적용되고, 속성 패널의 관련 기본 설정만 잠깁니다. 끄면 기본 값으로 돌아가며 고급 값은 보관됩니다.",
                                  "Override only this category and lock only its basic Properties. Turning off restores basic values and retains this advanced draft."))
        layout.addWidget(toggle)
        note = QLabel()
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        panel = QWidget()
        groups = QVBoxLayout(panel)
        groups.setContentsMargins(0, 0, 0, 0)
        groups.setSpacing(12)
        layout.addWidget(panel)
        self.category_toggles[category] = toggle
        self.category_panels[category] = panel
        self.category_notes[category] = note
        layout.addStretch()
        scroll.setWidget(page)
        tabs.addTab(scroll, self._tr(ko, en))
        return groups

    def _group(self, page, ko, en):
        group = QGroupBox(self._tr(ko, en))
        group.setObjectName("lyricOptionGroup")
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        page.addWidget(group)
        return form

    def _help(self, form, key, widget):
        text = self.HELP[key][0 if self._korean else 1]
        if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
            text += self._tr(f" 범위: {widget.minimum():g}–{widget.maximum():g}.",
                             f" Range: {widget.minimum():g}–{widget.maximum():g}.")
        widget.setToolTip(text)
        label = form.labelForField(widget)
        if label:
            widget.setAccessibleName(label.text())
        widget.setAccessibleDescription(text)
        if label:
            label.setToolTip(text)

    def _combo(self, form, key, ko, en, labels):
        combo = QComboBox()
        for value, pair in labels.items():
            combo.addItem(pair[0 if self._korean else 1], value)
        combo.setCurrentIndex(combo.findData(getattr(self._draft, key)))
        self.controls[key] = combo
        form.addRow(self._tr(ko, en), combo)
        self._help(form, key, combo)

    def _spin(self, form, key, ko, en, minimum, maximum, step, suffix=""):
        spin = self._number(minimum, maximum, step)
        spin.setSuffix(suffix)
        spin.setValue(getattr(self._draft, key))
        self.controls[key] = spin
        form.addRow(self._tr(ko, en), spin)
        self._help(form, key, spin)

    def _values(self):
        values = {key: widget.currentData() if isinstance(widget, QComboBox) else (
                      widget.isChecked() if isinstance(widget, QCheckBox) else widget.value())
                  for key, widget in self.controls.items()}
        values["subtitle_role_styles"] = deepcopy(self._styles)
        return values

    def settings(self):
        return {"subtitle_advanced_categories": [key for key, toggle in self.category_toggles.items() if toggle.isChecked()],
                "subtitle_advanced_settings": self._values()}

    def _changed(self, *_args):
        if self._loading:
            return
        values = self._values()
        for key, value in values.items():
            setattr(self._draft, key, value)
        effective = replace(self._basic, **self.settings()).resolved_lyrics()
        label_index = 0 if self._korean else 1
        effect_name = LYRIC_EFFECT_LABELS[effective.subtitle_animation][label_index]
        direction = {"up": ("아래 → 위", "Bottom → top"), "down": ("위 → 아래", "Top → bottom"),
                     "left": ("오른쪽 → 왼쪽", "Right → left"), "right": ("왼쪽 → 오른쪽", "Left → right")}[effective.subtitle_flow_direction][label_index]
        previous = self._tr("자동", "Auto") if effective.subtitle_context_lines < 0 else str(effective.subtitle_context_lines)
        upcoming = self._tr("자동", "Auto") if effective.subtitle_next_lines < 0 else str(effective.subtitle_next_lines)
        self.effective_summary.setText(self._tr(
            f"영상에 적용: {effect_name} · {direction} · 이전 {previous} / 다음 {upcoming}\n미리보기 줄 수는 확인용이며 영상의 줄 배치를 바꾸지 않습니다.",
            f"Video: {effect_name} · {direction} · Previous {previous} / next {upcoming}\nPreview cue count does not change the video's layout."
        ))
        for key in self.SETTINGS:
            setattr(self._preview_source, key, deepcopy(getattr(effective, key)))
        for key, panel in self.category_panels.items():
            active = self.category_toggles[key].isChecked()
            panel.setEnabled(active)
            self.category_notes[key].setText(self._tr("고급 값 적용 · 속성 패널의 관련 설정만 잠김" if active else "고급 값 적용 안 함 · 속성 패널의 기본 값만 사용",
                                                      "Advanced values active · only related Properties locked" if active else "Advanced values ignored · basic Properties used"))
        effect = self._draft.subtitle_animation
        intro_enabled = self._draft.subtitle_intro_enabled or self._draft.subtitle_intro_midtrack
        for key in ("subtitle_intro_style", "subtitle_intro_exit", "subtitle_intro_period", "subtitle_intro_scale"):
            self.controls[key].setEnabled(intro_enabled)
        self.controls["subtitle_intro_gap"].setEnabled(self._draft.subtitle_intro_midtrack)
        for key in ("subtitle_animation_duration", "subtitle_motion_easing"):
            self.controls[key].setEnabled(effect != "none")
        self.controls["subtitle_motion_distance"].setEnabled(effect in {"rise", "slide", "bounce"})
        self.controls["subtitle_stagger"].setEnabled(effect == "cascade")
        self.controls["subtitle_stagger_order"].setEnabled(effect == "cascade")
        self.controls["subtitle_zoom_amount"].setEnabled(effect in {"glow", "zoom"})
        for key in ("subtitle_glow_strength", "subtitle_glow_radius"):
            self.controls[key].setEnabled(effect == "glow")
        count = self.preview_count.value()
        if count:
            self._preview_source.subtitle_context_lines = (count - 1) // 2
            self._preview_source.subtitle_next_lines = count - 1 - (count - 1) // 2
        previous = max(0, self._preview_source.subtitle_context_lines) if self._preview_source.subtitle_context_lines >= 0 else 1
        upcoming = max(0, self._preview_source.subtitle_next_lines) if self._preview_source.subtitle_next_lines >= 0 else 1
        horizontal = self._preview_source.subtitle_flow_direction in {"left", "right"}
        self._preview_source.width = max(680, (2 * max(previous, upcoming) + 1) * 220) if horizontal else 680
        self._item.apply_source()
        self._preview_source.height = max(300, (2 * max(previous, upcoming) + 1) * self._item._lyric_line_height() + 24) if not horizontal else 300
        self._item.apply_source()
        self._scene.set_artboard_size(self._preview_source.width, self._preview_source.height)
        self._load_role()
        self._refresh_timer.start(0)

    def _change_preview_count(self, count):
        self._changed()

    def _load_role(self, *_args):
        self._loading = True
        role = self.role.currentData()
        for side in ("previous", "next"):
            distance_control = self.controls[f"subtitle_{side}_distance_fade"]
            distance_control.setVisible(role == side)
            distance_control.setEnabled(role == side)
            self.distance_labels[side].setVisible(role == side)
        self.distance_hint.setVisible(role in {"previous", "next"})
        self.distance_group.setVisible(role in {"previous", "next"})
        style = role_style(self._draft, role)
        self.role_scale.setValue(float(style["scale"]))
        self.role_opacity.setValue(float(style["opacity"]))
        self.role_blur.setValue(float(style["blur"]))
        for widget in (self.role_scale, self.role_opacity, self.role_blur):
            widget.setEnabled(True)
        self._loading = False

    def _store_role(self, *_args):
        if self._loading:
            return
        role = self.role.currentData()
        self._styles[role] = {
                "scale": self.role_scale.value(),
                "opacity": self.role_opacity.value(), "blur": self.role_blur.value(),
        }
        self._changed()

    def _replay(self, *_args):
        self._refresh_timer.stop()
        self.pause.setChecked(False)
        self._elapsed_offset = 0.0
        self._clock.start()
        self._render_preview(0.0)
        if self.isVisible():
            self._timer.start()

    def _preview_seconds(self):
        return self._paused_seconds if self.pause.isChecked() else self._elapsed_offset + self._clock.elapsed() / 1000

    def _pause_preview(self, paused):
        if paused:
            self._paused_seconds = self._elapsed_offset + self._clock.elapsed() / 1000
            self._timer.stop()
            self._render_preview(self._paused_seconds)
        else:
            self._elapsed_offset = self._paused_seconds
            self._clock.start()
            if self.isVisible():
                self._timer.start()
        self.pause.setText(self._tr("계속 재생", "Resume") if paused else self._tr("일시정지", "Pause"))

    def _refresh_preview(self, *_args):
        self._refresh_timer.stop()
        if self.pause.isChecked():
            self._render_preview(self._paused_seconds)
        else:
            self._replay()

    def _preview_scale(self):
        return min(1.0, max(1, self.preview.width()) / self._preview_source.width,
                   max(1, self.preview.height()) / self._preview_source.height)

    def _render_preview(self, elapsed=None):
        if self.tabs.currentIndex() == 3:
            self._render_intro_preview(elapsed)
            return
        interval = max(1.8, self._preview_source.subtitle_animation_duration + 0.5)
        texts = self._preview_texts
        track = PlaylistTrack("", "Preview", duration_seconds=interval * len(texts), lyrics=[
            {"start": index * interval, "end": (index + 1) * interval, "text": text}
            for index, text in enumerate(texts)
        ])
        first = max(1, self._preview_source.subtitle_context_lines)
        offset = interval * first
        elapsed = (self._preview_seconds() % (interval * (len(texts) - first)) + offset) if elapsed is None else elapsed + offset
        image = CanvasSnapshot.capture_track(self._scene, track, 1, 1, 0, elapsed_seconds=elapsed,
                                             output_scale=self._preview_scale())
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
        ))

    def _render_intro_preview(self, elapsed):
        # The centered intro needs room for the two example lyrics beneath it.
        height = max(self._preview_source.height, 5 * self._item._lyric_line_height() + 24)
        if height != self._preview_source.height:
            self._preview_source.height = height
            self._item.apply_source()
            self._scene.set_artboard_size(self._preview_source.width, height)
        wait = max(6.0, self._preview_source.subtitle_intro_period * 2)
        gap = max(8.0, self._preview_source.subtitle_intro_gap + 1)
        track = PlaylistTrack("", "Intro preview", duration_seconds=wait + gap + 8, lyrics=[
            {"start": wait, "end": wait + 2, "text": self._preview_texts[1]},
            {"start": wait + 2 + gap, "end": wait + 6 + gap, "text": self._preview_texts[2]},
        ])
        mode = self.intro_preview_mode.currentData()
        middle = mode in {"middle", "middle_exit"}
        clock = self._preview_seconds() if elapsed is None else elapsed
        seconds = wait + 2 + clock % (gap + 3) if middle else clock % (wait + 3)
        if mode in {"start_exit", "middle_exit"}:
            duration = (self._preview_source.subtitle_animation_duration
                        if self._preview_source.subtitle_animation != "none" else 0)
            end = wait + 2 + gap if middle else wait
            seconds = end - .25 + clock % (duration + 1)
        image = CanvasSnapshot.capture_track(self._scene, track, 1, 1, 0, elapsed_seconds=seconds,
                                              output_scale=self._preview_scale(),
                                              lyric_instrumental_spans=((wait + 2, wait + 2 + gap),))
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
        ))

    def showEvent(self, event):
        super().showEvent(event)
        self._replay()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        self._refresh_timer.stop()
        super().hideEvent(event)
