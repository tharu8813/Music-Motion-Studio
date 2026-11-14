"""Edit lyric layout, cue transitions and real-time effects with a renderer preview."""

from copy import deepcopy
from dataclasses import replace
import re

from PySide6.QtCore import QElapsedTimer, QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from app.animation.lyrics import INTRO_EXIT_LABELS, INTRO_STYLE_LABELS, LYRIC_EFFECT_LABELS, role_style
from app.canvas.live_canvas import CanvasScene
from app.canvas.renderers.lyrics_renderer import _grapheme_boundaries
from app.canvas.source_item import SourceItem
from app.dialogs.help_dialog import install_help_shortcut
from app.models.playlist import PlaylistTrack
from app.models.source import SUBTITLE_ADVANCED_GROUPS, Source
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.cadence import preview_interval_ms, screen_preview_fps
from app.ui.design_system import COLORS
from app.ui.form_layout import style_dialog_actions
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
        "subtitle_line_spacing": ("같은 시간의 가사 묶음 안에서 원래 줄바꿈 사이의 여백입니다. 원문과 자동 번역문 사이는 조금 좁게 표시하며, 자동 줄바꿈에는 추가 여백을 넣지 않습니다.", "Gap between original lines within one cue. Automatic translations sit slightly closer; wrapped continuations have no extra gap."),
        "subtitle_cue_spacing": ("이전·현재·다음 가사 묶음 사이의 여백입니다. 묶음 안 줄 간격과 별개이며, 가로 이동에서는 가사 영역 사이의 여백입니다.", "Gap between timed cue groups, independent of inner line spacing. In horizontal flow, this is the gap between cue columns."),
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
        "subtitle_previous_blur": ("‘가사 모양 직접 설정’이 꺼져 있을 때 이전 가사에 적용되는 흐림입니다.", "Blur applied to previous lyrics when Customize cue appearance is off."),
        "subtitle_karaoke_enabled": ("단어·음절 시간이 있는 TTML 가사를 부르는 순서에 따라 강조합니다.", "Highlight TTML lyrics in singing order using their word or syllable timings."),
        "subtitle_karaoke_glow_enabled": ("색이 채워지는 글자가 점점 밝게 빛납니다.", "Make each character glow brighter as its highlight fills."),
        "subtitle_karaoke_glow_strength": ("글자가 거의 다 채워졌을 때의 빛 밝기입니다.", "Glow brightness as a character approaches full fill."),
        "subtitle_karaoke_lift": ("부르는 중에 올라가고 현재 가사 줄이 유지되는 동안 그 높이를 유지합니다. 0이면 움직임을 끕니다.", "Rise as the text fills, then stay raised while the cue is current. Zero turns the motion off."),
        "subtitle_karaoke_lift_unit": ("글자마다 올라가거나 TTML에 기록된 단어·음절이 함께 올라갑니다.", "Lift individual characters or move each timed TTML word or syllable together."),
        "subtitle_karaoke_edge_blur": ("채워진 색과 대기 중인 색을 섞는 경계의 너비입니다. 0이면 경계가 선명합니다.", "Width of the blend between sung and pending colors. Zero keeps the edge sharp."),
        "subtitle_karaoke_blur_mode": ("긴 음, 채우는 동안, 또는 채운 뒤 현재 줄에 있는 동안 빛을 더합니다. 효과가 끝나면 서서히 사라집니다.", "Add a halo to sustained units, while filling, or through the rest of the current cue. The halo fades out when the effect ends."),
        "subtitle_karaoke_blur_unit": ("빛을 더할 글자 또는 단어·음절을 선택합니다.", "Choose whether characters or timed words and syllables glow."),
        "subtitle_karaoke_blur_radius": ("글자 주변으로 빛이 퍼지는 범위입니다. 0이면 꺼집니다.", "How far the halo spreads around the glyphs. Zero turns it off."),
        "subtitle_karaoke_blur_min_duration": ("이 시간보다 긴 음에만 적용합니다. 부르는 동안 서서히 밝아지고, 긴 음일수록 최대 밝기가 높아집니다.", "Applies to units held longer than this. The glow builds while sung and reaches a higher peak on longer notes."),
        "subtitle_karaoke_glow_fade_in": ("채우기 시작할 때 글로우가 밝아지는 시간입니다. 0이면 페이드인을 생략합니다.", "Time for the glow to fade in when filling starts. Zero skips the fade-in."),
        "subtitle_karaoke_glow_fade_out": ("글자나 단어가 채워진 뒤 빛이 사라지는 시간입니다. 현재 줄에 있는 동안 모드에서는 줄이 끝날 때 시작합니다. 0이면 즉시 꺼집니다.", "Time for the glow to fade out after filling. While cue is current starts the fade at cue end. Zero switches the glow off immediately."),
    }
    SETTINGS = tuple(key for group in SUBTITLE_ADVANCED_GROUPS.values() for key in group)
    REALTIME_CONTROLS = (
        "subtitle_karaoke_enabled", "subtitle_karaoke_glow_enabled", "subtitle_karaoke_glow_strength",
        "subtitle_karaoke_lift", "subtitle_karaoke_lift_unit",
    )
    DIRECT_SETTINGS = (
        "subtitle_previous_blur", "subtitle_karaoke_edge_blur", "subtitle_karaoke_blur_mode",
        "subtitle_karaoke_blur_unit", "subtitle_karaoke_blur_radius", "subtitle_karaoke_blur_min_duration",
        "subtitle_karaoke_glow_fade_in", "subtitle_karaoke_glow_fade_out",
        *REALTIME_CONTROLS,
    )
    UNIT_LABELS = {"character": ("글자", "Character"), "word": ("단어·음절", "Word or syllable")}

    def __init__(self, source: Source, translator: Translator, parent=None):
        super().__init__(parent)
        self._translator = translator
        install_help_shortcut(self, ("canvas", "lyrics_transition"), translator=translator)
        self._korean = translator.is_korean
        self._loading = True
        self._basic = Source.from_dict(source.to_dict())
        self._draft = Source.from_dict(self._basic.to_dict() | self._basic.subtitle_advanced_settings | {
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
        self.setMinimumSize(820, 580)
        self.resize(1020, 700)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)
        title = QLabel(self.windowTitle())
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        body = QHBoxLayout()
        body.setSpacing(20)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("lyricScopeTabs")
        self.tabs.setMinimumWidth(365)
        self.tabs.tabBar().setExpanding(True)
        self.general_tabs = self._scope("전체", "General")
        self.line_tabs = self._scope("줄 가사", "Line lyrics")
        realtime_page = self._page(self.tabs, "실시간 가사", "Real-time lyrics")
        self._build_realtime_page(realtime_page)
        realtime_page.addStretch()
        body.addWidget(self.tabs, 5)

        motion_page = self._page(self.line_tabs, "줄 전환", "Cue transitions")
        motion_panel = self._category(motion_page, "motion", "줄 전환 직접 설정", "Customize cue transitions",
                                      "가사 줄이 바뀔 때의 효과와 이동 방향을 조절합니다.",
                                      "Adjust the effect and direction as lyric cues change.")
        motion_page.addStretch()
        motion = self._group(motion_panel, "효과와 방향", "Effect and direction")
        self._combo(motion, "subtitle_animation", "전환 효과", "Effect", LYRIC_EFFECT_LABELS)
        self._combo(motion, "subtitle_flow_direction", "이동 방향", "Flow direction", {
            "up": ("아래 → 위", "Bottom → top"), "down": ("위 → 아래", "Top → bottom"),
            "left": ("오른쪽 → 왼쪽", "Right → left"), "right": ("왼쪽 → 오른쪽", "Left → right"),
        })
        self._spin(motion, "subtitle_animation_duration", "전환에 걸리는 시간", "Transition duration", 0.05, 3, 0.05, " s")
        self._combo(motion, "subtitle_motion_easing", "속도 변화", "Speed curve", {
            "auto": ("효과에 맞게", "Match effect"), "smooth": ("부드럽게", "Ease in and out"),
            "linear": ("일정하게", "Linear"), "ease_in": ("점점 빠르게", "Ease in"),
            "ease_out": ("점점 느리게", "Ease out"),
        })
        self._spin(motion, "subtitle_motion_distance", "기본 이동에 더할 거리", "Extra travel", 0, 200, 1, " px")
        self._spin(motion, "subtitle_stagger", "다음 가사 사이의 시차", "Delay between upcoming cues", 0, 0.8, 0.05)
        self._combo(motion, "subtitle_stagger_order", "먼저 움직일 가사", "Move first", {
            "near_first": ("가까운 가사부터", "Nearest first"), "far_first": ("먼 가사부터", "Farthest first"),
        })
        self._spin(motion, "subtitle_zoom_amount", "크기 변화 폭", "Scale change", 0, 0.5, 0.05)
        self._spin(motion, "subtitle_glow_strength", "전환 빛 밝기", "Transition glow brightness", 0, 1, 0.05)
        self._spin(motion, "subtitle_glow_radius", "전환 빛 범위", "Transition glow spread", 0, 30, 0.5, " px")

        layout_page = self._page(self.general_tabs, "가사 배치", "Lyric layout")
        layout_panel = self._category(layout_page, "layout", "가사 배치 직접 설정", "Customize lyric layout",
                                      "전체 가사의 기준 위치, 표시할 가사 수와 간격을 조절합니다.",
                                      "Set the anchor, cue count and spacing for all lyrics.")
        layout_page.addStretch()
        layout_form = self._group(layout_panel, "기준 위치와 정렬", "Position and alignment")
        self._spin(layout_form, "subtitle_anchor", "현재 가사 기준 위치", "Current cue anchor", 0, 1, 0.05)
        anchor_hint = QLabel(self._tr("0은 위/왼쪽, 0.5는 중앙, 1은 아래/오른쪽입니다.",
                                      "0: top/left · 0.5: center · 1: bottom/right"))
        anchor_hint.setObjectName("mutedLabel")
        anchor_hint.setWordWrap(True)
        layout_form.addRow(anchor_hint)
        self._combo(layout_form, "text_alignment", "글자 정렬", "Text alignment", {
            "left": ("왼쪽", "Left"), "center": ("가운데", "Center"), "right": ("오른쪽", "Right"),
        })
        layout_form = self._group(layout_panel, "표시할 가사와 간격", "Visible cues and spacing")
        for key, ko, en in (("subtitle_context_lines", "이전 가사 수", "Previous cues"),
                            ("subtitle_next_lines", "다음 가사 수", "Next cues")):
            spin = QSpinBox()
            spin.setRange(-1, 6)
            spin.setSpecialValueText(self._tr("자동", "Auto"))
            spin.setValue(getattr(self._draft, key))
            self.controls[key] = spin
            layout_form.addRow(self._tr(ko, en), spin)
            self._help(layout_form, key, spin)
        self._spin(layout_form, "subtitle_line_spacing", "묶음 안 줄 간격", "Lines within a cue", 0, 120, 1, " px")
        self._spin(layout_form, "subtitle_cue_spacing", "가사 묶음 간격", "Between lyric cues", 0, 120, 1, " px")
        role_page = self._page(self.line_tabs, "가사 모양", "Cue appearance")
        role_panel = self._category(role_page, "styles", "가사 모양 직접 설정", "Customize cue appearance",
                                    "이전·현재·다음 가사의 크기, 불투명도와 흐림을 각각 조절합니다.",
                                    "Set size, opacity and blur separately for previous, current and upcoming lyrics.")
        basic_style = self._group(role_page, "기본 모양", "Basic appearance")
        self._spin(basic_style, "subtitle_previous_blur", "이전 가사의 흐림", "Previous lyric blur", 0, 8, 0.5, " px")
        self.basic_style = basic_style.parentWidget()
        role_page.addStretch()
        role_form = self._group(role_panel, "이전·현재·다음 가사", "Previous, current and upcoming cues")
        self.role = QComboBox()
        for key, label in (("previous", self._tr("이전 가사", "Previous")),
                           ("current", self._tr("현재 가사", "Current")),
                           ("next", self._tr("다음 가사", "Next"))):
            self.role.addItem(label, key)
        role_form.addRow(self._tr("편집할 가사", "Edit cues"), self.role)
        self.role.setToolTip(self._tr("이전·현재·다음 가사의 스타일을 각각 편집합니다.", "Edit previous, current and next styles independently."))
        self.role_scale = self._number(0.25, 3, 0.05)
        self.role_opacity = self._number(0, 1, 0.05)
        self.role_blur = self._number(0, 12, 0.5)
        self.role_blur.setSuffix(" px")
        role_form.addRow(self._tr("크기 배율", "Size scale"), self.role_scale)
        self._help(role_form, "role_scale", self.role_scale)
        role_form.addRow(self._tr("불투명도", "Opacity"), self.role_opacity)
        self._help(role_form, "role_opacity", self.role_opacity)
        role_form.addRow(self._tr("글자 흐림", "Text blur"), self.role_blur)
        self._help(role_form, "role_blur", self.role_blur)
        for side in ("previous", "next"):
            self._spin(role_form, f"subtitle_{side}_distance_fade", "가까운 가사 선명하게", "Sharpen nearby cues", 0, 1, 0.05)

        intro_page = self._page(self.general_tabs, "대기 표시", "Waiting indicator")
        intro_panel = self._category(intro_page, "intro", "대기 표시 직접 설정", "Customize waiting indicator",
                                     "첫 가사 전이나 보컬이 없는 간주에 대기 애니메이션을 표시합니다.",
                                     "Show a waiting animation before the first lyric or during instrumental gaps.")
        intro_page.addStretch()
        intro = self._group(intro_panel, "표시할 구간", "When to show")
        for key, ko, en in (
            ("subtitle_intro_enabled", "첫 가사 전 대기 표시", "Show before the first lyric"),
            ("subtitle_intro_midtrack", "곡 중간의 간주에도 표시", "Show during instrumental interludes"),
        ):
            self._check(intro, key, ko, en)
        self._spin(intro, "subtitle_intro_gap", "최소 간주 길이", "Minimum interlude", 2, 30, 0.5, " s")
        intro = self._group(intro_panel, "대기 애니메이션", "Waiting animation")
        self.intro_appearance = intro.parentWidget()
        self._combo(intro, "subtitle_intro_style", "대기 중 움직임", "Waiting motion", INTRO_STYLE_LABELS)
        self._combo(intro, "subtitle_intro_exit", "사라지는 방식", "Exit effect", INTRO_EXIT_LABELS)
        self._spin(intro, "subtitle_intro_period", "한 번 반복하는 시간", "Cycle duration", 1, 10, 0.25, " s")
        self._spin(intro, "subtitle_intro_scale", "표시 크기", "Indicator scale", 0.25, 3, 0.05)
        self.intro_preview_mode = QComboBox()
        self.intro_preview_mode.addItem(self._tr("첫 가사 전", "Before first lyric"), "start")
        self.intro_preview_mode.addItem(self._tr("곡 중간 · 보컬 없는 예시", "Mid-track · instrumental sample"), "middle")
        self.intro_preview_mode.addItem(self._tr("첫 가사 · 퇴장 확인", "First lyric · exit preview"), "start_exit")
        self.intro_preview_mode.addItem(self._tr("간주 · 퇴장 확인", "Interlude · exit preview"), "middle_exit")
        self.intro_preview_mode.setToolTip(self._tr("미리보기 예시만 바꿉니다. 곡의 실제 가사나 분석 구간은 변경하지 않습니다.",
                                                   "Changes only the preview sample, not the track lyrics or analyzed intervals."))

        preview_column = QVBoxLayout()
        caption = QLabel(self._tr("예시 미리보기", "Sample preview"))
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
            "예시에 표시할 가사 수입니다. 자동은 ‘전체 → 가사 배치’에 적용되는 값을 따릅니다.",
            "Cue count for the sample. Auto follows the effective settings in General → Lyric layout.",
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
        self.preview.setAccessibleName(caption.text())
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview.setMinimumSize(355, 175)
        preview_column.addWidget(self.preview, 1)
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
        self.preview_multiline = QCheckBox(self._tr("여러 줄 예시", "Multi-line sample"))
        self.preview_multiline.setChecked(True)
        self.preview_multiline.setToolTip(self._tr("미리보기 예시에만 두 번째 줄을 추가해 두 간격을 비교합니다. 실제 가사는 변경하지 않습니다.",
            "Add a second sample line to compare inner and cue gaps. Actual lyrics are unchanged."))
        self.preview_multiline.toggled.connect(self._changed)
        playback.addWidget(self.preview_multiline)
        playback.addStretch()
        preview_column.addLayout(playback)
        self.preview_intro = QWidget()
        preview_intro = QFormLayout(self.preview_intro)
        preview_intro.setContentsMargins(0, 0, 0, 0)
        preview_intro.addRow(self._tr("예시 구간", "Sample segment"), self.intro_preview_mode)
        preview_column.addWidget(self.preview_intro)
        self.preview_timing = QWidget()
        timing = QFormLayout(self.preview_timing)
        timing.setContentsMargins(0, 0, 0, 0)
        self.preview_timing_form = timing
        self.preview_unit = QComboBox()
        for unit, labels in self.UNIT_LABELS.items():
            self.preview_unit.addItem(self._tr(*labels), unit)
        self.preview_unit.setCurrentIndex(self.preview_unit.findData("word"))
        timing.addRow(self._tr("예시 타이밍 단위", "Sample timing unit"), self.preview_unit)
        self.preview_character_duration = self._number(0.05, 1.5, 0.05)
        self.preview_word_duration = self._number(0.1, 3, 0.05)
        for editor, duration, label in (
            (self.preview_character_duration, 0.25, self._tr("글자당 지속시간", "Time per character")),
            (self.preview_word_duration, 0.65, self._tr("단어당 지속시간", "Time per word")),
        ):
            editor.setSuffix(self._tr(" 초", " s"))
            editor.setValue(duration)
            timing.addRow(label, editor)
        self.preview_timing.setToolTip(self._tr("이 설정은 예시 가사에만 적용됩니다.", "These settings affect only the sample lyrics."))
        preview_column.addWidget(self.preview_timing)
        body.addLayout(preview_column, 6)
        root.addLayout(body, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        style_dialog_actions(buttons)
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
                                       subtitle_timing_offset=0, subtitle_karaoke_offset=0,
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
        self._sample_key = None
        self._clock = QElapsedTimer()
        self._elapsed_offset = 0.0
        self._paused_seconds = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(preview_interval_ms(screen_preview_fps(self.screen())))
        self._timer.timeout.connect(self._render_preview)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_preview)
        for widget in self.controls.values():
            signal = widget.currentIndexChanged if isinstance(widget, QComboBox) else (
                widget.toggled if isinstance(widget, QCheckBox) else widget.valueChanged)
            signal.connect(self._changed)
        self._preview_mode = None
        self.tabs.currentChanged.connect(self._switch_preview)
        self.general_tabs.currentChanged.connect(self._switch_preview)
        self.intro_preview_mode.currentIndexChanged.connect(self._refresh_preview)
        self.preview_count.valueChanged.connect(self._change_preview_count)
        for toggle in self.category_toggles.values():
            toggle.toggled.connect(self._changed)
        self.role.currentIndexChanged.connect(self._load_role)
        for widget in (self.role_scale, self.role_opacity, self.role_blur):
            widget.valueChanged.connect(self._store_role)
        self.preview_unit.currentIndexChanged.connect(self._changed)
        self.preview_character_duration.valueChanged.connect(self._changed)
        self.preview_word_duration.valueChanged.connect(self._changed)
        self._loading = False
        self._load_role()
        self._changed()
        self.preview.installEventFilter(self)

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

    def _scope(self, ko, en):
        tabs = QTabWidget()
        tabs.setObjectName("lyricSectionTabs")
        tabs.setDocumentMode(True)
        self.tabs.addTab(tabs, self._tr(ko, en))
        return tabs

    def _page(self, tabs, ko, en):
        scroll = QScrollArea()
        scroll.setObjectName("lyricSettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(12)
        scroll.setWidget(page)
        tabs.addTab(scroll, self._tr(ko, en))
        return layout

    def _hint(self, layout, ko, en):
        label = QLabel(self._tr(ko, en))
        label.setObjectName("mutedLabel")
        label.setWordWrap(True)
        layout.addWidget(label)
        return label

    def _category(self, layout, category, ko, en, hint_ko, hint_en):
        self._hint(layout, hint_ko, hint_en)
        toggle = QCheckBox(self._tr(ko, en))
        toggle.setObjectName("lyricCustomToggle")
        toggle.setChecked(category in self._basic.subtitle_advanced_categories)
        toggle.setToolTip(self._tr("이 항목의 값을 직접 설정합니다. 끄면 기존 기본값을 사용하고 입력한 값은 보관합니다.",
                                  "Set the values for this section. Turning it off restores the defaults and keeps your edits."))
        layout.addWidget(toggle)
        note = self._hint(layout, "기본 설정을 사용 중입니다. 직접 설정을 켜면 이 창의 값을 적용합니다.",
                          "Basic settings are active. Turn on customization to use the values in this dialog.")
        panel = QWidget()
        groups = QVBoxLayout(panel)
        groups.setContentsMargins(0, 0, 0, 0)
        groups.setSpacing(12)
        layout.addWidget(panel)
        self.category_toggles[category] = toggle
        self.category_panels[category] = panel
        self.category_notes[category] = note
        return groups

    def _build_realtime_page(self, page):
        self._hint(page, "단어·음절 시간이 있는 TTML 가사에 적용합니다.",
                   "Applies to TTML lyrics with word or syllable timing.")
        appearance = self._group(page, "실시간 강조", "Real-time highlighting")
        self._check(appearance, "subtitle_karaoke_enabled", "실시간 가사 사용", "Enable real-time lyrics")
        self._spin(appearance, "subtitle_karaoke_edge_blur", "채움 경계의 부드러움", "Fill edge softness", 0, 50, 1, " px")
        glow = self._group(page, "빛 효과", "Glow effects")
        self._check(glow, "subtitle_karaoke_glow_enabled", "채워지는 글자에 빛 추가", "Glow as each character fills")
        self._spin(glow, "subtitle_karaoke_glow_strength", "글자 빛 밝기", "Character glow brightness", 0, 1, 0.05)
        self._combo(glow, "subtitle_karaoke_blur_mode", "추가로 빛나는 구간", "Additional glow timing", {
            "none": ("끄기", "Off"), "long": ("긴 단어·글자만", "Long units only"),
            "filling": ("색이 채워지는 동안", "While filling"), "continuous": ("현재 가사로 표시되는 동안", "While cue is current"),
        })
        self._combo(glow, "subtitle_karaoke_blur_unit", "함께 빛나는 단위", "Glow unit", self.UNIT_LABELS)
        self._spin(glow, "subtitle_karaoke_blur_radius", "빛이 퍼지는 범위", "Glow spread", 0, 12, 0.5, " px")
        self._spin(glow, "subtitle_karaoke_blur_min_duration", "빛을 더할 최소 음 길이", "Minimum note duration", 0.05, 3, 0.05, " s")
        self._spin(glow, "subtitle_karaoke_glow_fade_in", "빛이 밝아지는 시간", "Glow fade-in", 0, 3, 0.05, " s")
        self._spin(glow, "subtitle_karaoke_glow_fade_out", "빛이 사라지는 시간", "Glow fade-out", 0, 3, 0.05, " s")
        lift = self._group(page, "위로 떠오르기", "Upward motion")
        self._spin(lift, "subtitle_karaoke_lift", "올라가는 높이", "Lift height", 0, 16, 0.5, " px")
        self._combo(lift, "subtitle_karaoke_lift_unit", "함께 올라가는 단위", "Lift unit", self.UNIT_LABELS)

    def _group(self, page, ko, en):
        group = QGroupBox(self._tr(ko, en))
        group.setObjectName("lyricOptionGroup")
        form = QFormLayout(group)
        form.setContentsMargins(12, 6, 12, 10)
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

    def _check(self, form, key, ko, en):
        check = QCheckBox(self._tr(ko, en))
        check.setChecked(getattr(self._draft, key))
        self.controls[key] = check
        form.addRow(check)
        self._help(form, key, check)

    def _spin(self, form, key, ko, en, minimum, maximum, step, suffix=""):
        spin = self._number(minimum, maximum, step)
        spin.setSuffix(self._tr(" 초", " s") if suffix == " s" else suffix)
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
        values = self._values()
        return {
            "subtitle_advanced_categories": [key for key, toggle in self.category_toggles.items() if toggle.isChecked()],
            "subtitle_advanced_settings": {key: values[key] for key in self.SETTINGS},
            **{key: values[key] for key in self.DIRECT_SETTINGS},
        }

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
        self._video_summary = self._tr(
            f"영상 적용 · {effect_name} · {direction} · 이전 {previous} / 다음 {upcoming}",
            f"Video · {effect_name} · {direction} · Previous {previous} / next {upcoming}",
        )
        for key in (*self.SETTINGS, *self.DIRECT_SETTINGS):
            setattr(self._preview_source, key, deepcopy(getattr(effective, key)))
        for key, panel in self.category_panels.items():
            active = self.category_toggles[key].isChecked()
            panel.setEnabled(active)
            panel.setVisible(active or key != "styles")
            self.category_notes[key].setVisible(not active)
        self.basic_style.setVisible(not self.category_toggles["styles"].isChecked())
        effect = self._draft.subtitle_animation
        intro_enabled = self._draft.subtitle_intro_enabled or self._draft.subtitle_intro_midtrack
        self.intro_appearance.setVisible(intro_enabled)
        self._show_option("subtitle_intro_gap", self._draft.subtitle_intro_midtrack)
        for key in ("subtitle_animation_duration", "subtitle_motion_easing"):
            self._show_option(key, effect != "none")
        self._show_option("subtitle_motion_distance", effect in {"rise", "slide", "bounce"})
        self._show_option("subtitle_stagger", effect == "cascade")
        self._show_option("subtitle_stagger_order", effect == "cascade")
        self._show_option("subtitle_zoom_amount", effect in {"glow", "zoom"})
        for key in ("subtitle_glow_strength", "subtitle_glow_radius"):
            self._show_option(key, effect == "glow")
        self._show_option("subtitle_karaoke_glow_strength", self._draft.subtitle_karaoke_glow_enabled)
        self._show_option("subtitle_karaoke_lift_unit", self._draft.subtitle_karaoke_lift > 0)
        glow_mode = self._draft.subtitle_karaoke_blur_mode
        for key in ("subtitle_karaoke_blur_unit", "subtitle_karaoke_blur_radius",
                    "subtitle_karaoke_glow_fade_in", "subtitle_karaoke_glow_fade_out"):
            self._show_option(key, glow_mode != "none")
        self._show_option("subtitle_karaoke_blur_min_duration", glow_mode == "long")
        count = self.preview_count.value()
        if count:
            self._preview_source.subtitle_context_lines = (count - 1) // 2
            self._preview_source.subtitle_next_lines = count - 1 - (count - 1) // 2
        previous = max(0, self._preview_source.subtitle_context_lines) if self._preview_source.subtitle_context_lines >= 0 else 1
        upcoming = max(0, self._preview_source.subtitle_next_lines) if self._preview_source.subtitle_next_lines >= 0 else 1
        horizontal = self._preview_source.subtitle_flow_direction in {"left", "right"}
        self._preview_source.width = max(680, (2 * max(previous, upcoming) + 1) * 220) if horizontal else 680
        self._item.apply_source()
        cue_height = self._item._lyric_line_height() * (2 if self.preview_multiline.isChecked() else 1)
        cue_height += self._preview_source.subtitle_cue_spacing - self._preview_source.subtitle_line_spacing
        self._normal_preview_height = max(300, (2 * max(previous, upcoming) + 1) * cue_height + 24) if not horizontal else 300
        self._preview_source.height = self._normal_preview_height
        self._item.apply_source()
        self._scene.set_artboard_size(self._preview_source.width, self._preview_source.height)
        self._load_role()
        self._sync_preview_controls()
        self._refresh_timer.start(0)

    def _show_option(self, key, visible):
        widget = self.controls[key]
        widget.parentWidget().layout().setRowVisible(widget, visible)

    def _change_preview_count(self, count):
        self._changed()

    def _load_role(self, *_args):
        self._loading = True
        role = self.role.currentData()
        for side in ("previous", "next"):
            self._show_option(f"subtitle_{side}_distance_fade", role == side)
        style = role_style(self._draft, role)
        self.role_scale.setValue(float(style["scale"]))
        self.role_opacity.setValue(float(style["opacity"]))
        self.role_blur.setValue(float(style["blur"]))
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

    def _sync_preview_controls(self):
        scope = self.tabs.currentWidget()
        intro = scope is self.general_tabs and self.general_tabs.currentIndex() == 1
        timed = scope is not self.general_tabs and scope is not self.line_tabs
        self._preview_mode = "intro" if intro else "timed" if timed else "line"
        self.preview_intro.setVisible(intro)
        self.preview_timing.setVisible(timed)
        self.preview_multiline.setVisible(not intro)
        by_character = self.preview_unit.currentData() == "character"
        self.preview_timing_form.setRowVisible(self.preview_character_duration, by_character)
        self.preview_timing_form.setRowVisible(self.preview_word_duration, not by_character)
        if timed and self._draft.subtitle_karaoke_enabled:
            summary = self._tr("TTML 예시 · 영상의 TTML 가사에도 이 효과를 적용합니다.",
                               "TTML sample · These effects also apply to TTML lyrics in the video.")
        elif timed:
            summary = self._tr("TTML 예시 · 영상에도 적용하려면 ‘실시간 가사 사용’을 켜세요.",
                               "TTML sample · Enable real-time lyrics to use these effects in the video.")
        elif intro:
            summary = self._tr("예시 가사 · 첫 가사 전과 간주에서의 대기 표시를 확인합니다.",
                               "Sample lyrics · Preview the wait before the first lyric or during an instrumental gap.")
        else:
            summary = self._video_summary
        self.effective_summary.setText(summary)

    def _switch_preview(self, _index):
        previous_mode = self._preview_mode
        self._sync_preview_controls()
        if previous_mode != self._preview_mode:
            self._render_preview()

    def _preview_track(self):
        timed = self._preview_mode == "timed"
        unit = self.preview_unit.currentData()
        duration = (self.preview_character_duration if unit == "character" else self.preview_word_duration).value()
        multiline = self.preview_multiline.isChecked()
        interval = max(1.8, self._preview_source.subtitle_animation_duration + 0.5)
        key = (timed, unit, duration, multiline, interval)
        if key == self._sample_key:
            return self._sample_track, self._sample_interval
        second = self._tr("같은 묶음의 두 번째 줄", "Second line in the same cue")
        texts = [f"{text}\n{second}" if multiline else text for text in self._preview_texts]
        spans = []
        for text in texts:
            lines, offset = [], 0
            for line in text.splitlines():
                if unit == "word":
                    units = [(offset + word.start(), word.group()) for word in re.finditer(r"\S+", line)]
                else:
                    edges = _grapheme_boundaries(line)
                    units = [(offset + a, line[a:b]) for a, b in zip(edges, edges[1:]) if line[a:b].strip()]
                lines.append(units)
                offset += len(line) + 1
            spans.append(lines)
        if timed:
            interval = max(interval, max(len(line) for lines in spans for line in lines) * duration + 0.35)
        cues = []
        for index, (text, lines) in enumerate(zip(texts, spans)):
            start = index * interval
            cue = {"start": start, "end": (index + 1) * interval, "text": text}
            if timed:
                cue.update(format="ttml", words=[
                    {"start": start + number * duration, "end": start + (number + 1) * duration,
                     "text": value, "offset": offset}
                    for units in lines for number, (offset, value) in enumerate(units)
                ])
            cues.append(cue)
        self._sample_track = PlaylistTrack("", "Preview", duration_seconds=interval * len(cues), lyrics=cues)
        self._sample_key, self._sample_interval = key, interval
        return self._sample_track, interval

    def _preview_scale(self):
        return min(1.0, max(1, self.preview.width()) / self._preview_source.width,
                   max(1, self.preview.height()) / self._preview_source.height)

    def _render_preview(self, elapsed=None):
        interval = preview_interval_ms(screen_preview_fps(self.screen()))
        if interval != self._timer.interval():
            self._timer.setInterval(interval)
        if self._preview_mode == "intro":
            self._preview_source.subtitle_karaoke_enabled = False
            self._render_intro_preview(elapsed)
            return
        if self._preview_source.height != self._normal_preview_height:
            self._preview_source.height = self._normal_preview_height
            self._item.apply_source()
            self._scene.set_artboard_size(self._preview_source.width, self._normal_preview_height)
        self._preview_source.subtitle_karaoke_enabled = self._preview_mode == "timed"
        track, interval = self._preview_track()
        first = max(1, self._preview_source.subtitle_context_lines)
        offset = interval * first
        elapsed = (self._preview_seconds() % (interval * (len(track.lyrics) - first)) + offset) if elapsed is None else elapsed + offset
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

    def eventFilter(self, watched, event):
        if watched is self.preview and event.type() == QEvent.Type.Resize and self.pause.isChecked():
            self._refresh_timer.start(0)
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._replay()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        self._refresh_timer.stop()
        super().hideEvent(event)
