"""Visual authoring of a source's short entrance and exit, in an isolated draft."""
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

from PySide6.QtCore import QElapsedTimer, QSignalBlocker, QSize, QTimer, Qt
from PySide6.QtGui import QUndoCommand, QUndoStack
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSlider, QSplitter, QStyle, QToolButton, QVBoxLayout, QWidget)

from app.animation.custom_motion import CustomMotion, MOTION_FIELDS, exit_pose_differences, resting_pose
from app.animation.keyframes import Keyframe
from app.dialogs.adaptive_dialog import AdaptiveDialog
from app.dialogs.motion_canvas import MotionCanvas
from app.dialogs.motion_timeline import MotionTimeline, MotionRuler
from app.dialogs.motion_curve_presets import CurvePresetMenu, PRESETS, curve_icon, preset_name
from app.models.playlist import PlaylistTrack
from app.preview.cadence import preview_interval_ms, screen_preview_fps
from app.ui.studio_icons import menu_icon


class DraftCommand(QUndoCommand):
    def __init__(self, dialog, before, after):
        super().__init__("모션 편집 / Edit motion")
        self.dialog, self.before, self.after = dialog, before, after

    def undo(self):
        self.dialog.restore(self.before)

    def redo(self):
        self.dialog.restore(self.after)


class CustomMotionDialog(AdaptiveDialog):
    def __init__(self, source, project_scene, tracks, translator, edge="in", parent=None, *, compiled_plan=None):
        super().__init__(parent)
        self.source = deepcopy(source)
        self.translator = translator
        self.compiled_plan = compiled_plan
        self._clock_windows = None
        from app.timeline.render_plan import visual_segments
        self._segments = {segment.track_id: segment for segment in visual_segments(compiled_plan)} if compiled_plan else {}
        self.edge = edge
        self.progress = 0.0
        self.active_field = "dx"
        self.advanced = False
        self._filling = self._closed = False
        self._gesture_before = None
        self._resume_after_gesture = False
        self._guide_scheduled = False
        self.drafts = {"in": source.custom_intro_keyframes or CustomMotion.default(True),
                       "out": source.custom_outro_keyframes or CustomMotion.default(False)}
        self.durations = {"in": source.animation_in_duration, "out": source.animation_out_duration}
        self.touched = {edge}
        self.changes = None
        self.tracks = list(tracks) or [PlaylistTrack("", "미리보기 / Preview", duration_seconds=10)]
        self.undo_stack = QUndoStack(self)
        self.setObjectName("customMotionDialog")
        self.setWindowTitle("사용자 지정 애니메이션" if self.korean else "Custom animation")
        self.compact = self.screen().availableGeometry().height() < 800
        self.resize(1200, 880)
        self.setMinimumSize(840, 600 if self.compact else 720)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*(16, 8, 16, 8) if self.compact else (24, 20, 24, 20))
        outer.setSpacing(8 if self.compact else 12)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.title = QLabel(self.windowTitle())
        self.title.setObjectName("dialogTitle")
        self.target_label = QLabel(source.name)
        self.target_label.setObjectName("mutedLabel")
        heading.addWidget(self.title)
        heading.addWidget(self.target_label)
        header.addLayout(heading, 1)
        self.guide_button = self.icon_button("help", "사용 안내 다시 보기" if self.korean else "Show getting started")
        self.shortcut_button = self.icon_button("shortcuts", "단축키 (Shift+F1)" if self.korean else "Shortcuts (Shift+F1)")
        header.addWidget(self.guide_button)
        header.addWidget(self.shortcut_button)
        self.edge_combo = QComboBox()
        self.edge_combo.addItem("곡 시작 애니메이션" if self.korean else "Track entrance", "in")
        self.edge_combo.addItem("곡 끝 애니메이션" if self.korean else "Track exit", "out")
        self.edge_combo.setCurrentIndex(0 if edge == "in" else 1)
        # Keep the frequently used animation scope in its own compact row;
        # mixing it into the window title row made everything feel equal.
        self.animation_toolbar = QFrame()
        self.animation_toolbar.setObjectName("card")
        animation_layout = QHBoxLayout(self.animation_toolbar)
        animation_layout.setContentsMargins(12, 8, 12, 8)
        animation_layout.setSpacing(9)
        animation_label = QLabel("편집 구간" if self.korean else "Editing")
        animation_label.setBuddy(self.edge_combo)
        animation_layout.addWidget(animation_label)
        animation_layout.addWidget(self.edge_combo)
        duration_label = QLabel("지속 시간" if self.korean else "Duration")
        animation_layout.addWidget(duration_label)
        self.duration = QDoubleSpinBox()
        self.duration.setRange(.1, 10)
        self.duration.setDecimals(3)
        self.duration.setSingleStep(.05)
        self.duration.setSuffix(" s")
        self.duration.setKeyboardTracking(False)
        duration_label.setBuddy(self.duration)
        animation_layout.addWidget(self.duration)
        animation_layout.addStretch()
        outer.addLayout(header)
        outer.addWidget(self.animation_toolbar)
        self.instructions = QLabel(
            "① 타임라인에서 시점 선택   ② 미리보기에서 이동·크기 조절   ③ 재생 후 적용"
            if self.korean else
            "① Choose a time   ② Move or resize in the preview   ③ Play, then apply"
        )
        self.instructions.setObjectName("mutedLabel")
        self.instructions.setWordWrap(True)
        outer.addWidget(self.instructions)
        self.instructions.setVisible(not self.compact)
        self.connection_host = QWidget()
        connection = QHBoxLayout(self.connection_host)
        connection.setContentsMargins(0, 0, 0, 0)
        self.connection_notice = QLabel()
        self.connection_notice.setWordWrap(True)
        connection.addWidget(self.connection_notice, 1)
        self.connect_pose = QPushButton("시작 애니메이션의 마지막 자세 가져오기" if self.korean else "Use entrance's final pose")
        self.connect_pose.setToolTip("종료 모션의 첫 키프레임만 맞춥니다. 필요한 경우 꺼진 속성을 켭니다. 다른 점과 곡선은 보존합니다."
            if self.korean else "Match only the exit's first keys. Enable differing disabled properties if needed. Keep other keys and curves.")
        connection.addWidget(self.connect_pose)
        outer.addWidget(self.connection_host)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        canvas_host = QFrame()
        canvas_host.setObjectName("card")
        canvas_layout = QVBoxLayout(canvas_host)
        canvas_layout.setContentsMargins(12, 10, 12, 12)
        canvas_layout.setSpacing(10)
        view_tools = QHBoxLayout()
        view_tools.setSpacing(6)
        preview_title = QLabel("미리보기" if self.korean else "Preview")
        preview_title.setObjectName("panelTitle")
        view_tools.addWidget(preview_title)
        self.track = QComboBox()
        self.track.setMaximumWidth(190)
        self.track.setAccessibleName("예시 콘텐츠 곡 / Example content track")
        self.track.setToolTip("선택한 곡의 콘텐츠와 영상 시간을 표시합니다. 모든 곡에서 같은 애니메이션을 사용합니다." if self.korean else
                              "Shows the selected track's content and video time. The animation is shared across tracks.")
        for item in self.tracks:
            self.track.addItem(item.title, item.id)
        view_tools.addWidget(self.track)
        view_tools.addStretch()
        self.original = QCheckBox("원본" if self.korean else "Original")
        self.guides = QCheckBox("가이드" if self.korean else "Guides")
        view_tools.addWidget(self.original)
        view_tools.addWidget(self.guides)
        self.fit = QToolButton()
        self.fit.setText("맞춤" if self.korean else "Fit")
        self.actual = QToolButton()
        self.actual.setText("100%")
        self.zoom_out, self.zoom_in = QToolButton(), QToolButton()
        self.zoom_out.setText("−")
        self.zoom_in.setText("+")
        for button in (self.fit, self.actual, self.zoom_out, self.zoom_in):
            button.setObjectName("motionViewButton")
            view_tools.addWidget(button)
        self.view_buttons = (self.fit, self.actual, self.zoom_out, self.zoom_in)
        self.view_menu_button = QToolButton()
        self.view_menu_button.setText("보기" if self.korean else "View")
        self.view_menu_button.setToolTip("맞춤·100%·확대·축소" if self.korean else "Fit, 100%, zoom in/out")
        self.view_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        view_menu = QMenu(self.view_menu_button)
        for text, button in (("맞춤" if self.korean else "Fit", self.fit), ("100%", self.actual),
                ("확대" if self.korean else "Zoom in", self.zoom_in), ("축소" if self.korean else "Zoom out", self.zoom_out)):
            view_menu.addAction(text, button.click)
        self.view_menu_button.setMenu(view_menu)
        view_tools.addWidget(self.view_menu_button)
        self.view_menu_button.setVisible(self.compact)
        for button in self.view_buttons:
            button.setVisible(not self.compact)
        canvas_layout.addLayout(view_tools)
        self.canvas = MotionCanvas(project_scene, source, self.korean, self)
        self.video_sync = QCheckBox("영상 동기화" if self.korean else "Sync video")
        self.video_sync.setChecked(True)
        self.video_sync.setVisible(self.canvas.video_stills)
        self.video_sync.setToolTip("재생 위치에 맞춰 기존 디코더로 갱신합니다. 끄면 마지막 정지 화면을 유지합니다."
            if self.korean else "Update with the existing decoder at the playhead. Turn off to keep the last still.")
        if self.compact:
            self.canvas.setMinimumHeight(204)
        canvas_layout.addWidget(self.canvas, 1)
        self.limitations = QLabel()
        self.limitations.setWordWrap(True)
        self.limitations.setObjectName("mutedLabel")
        self.preview_title = preview_title
        media_status = QHBoxLayout()
        media_status.addWidget(self.video_sync)
        media_status.addWidget(self.limitations, 1)
        canvas_layout.addLayout(media_status)
        self.limitations.setVisible(bool(self.limitations.text()))
        preview_title.setToolTip(self.limitations.text())
        splitter.addWidget(canvas_host)
        properties = QFrame()
        properties.setObjectName("card")
        properties.setMinimumWidth(320)
        property_layout = QVBoxLayout(properties)
        property_layout.setContentsMargins(16, 12, 16, 14)
        property_layout.setSpacing(12)
        property_title = QLabel("현재 시점의 값" if self.korean else "Values at playhead")
        property_title.setObjectName("panelTitle")
        property_heading = QHBoxLayout()
        property_heading.addWidget(property_title, 1)
        self.reset_value_button = self.icon_button(QStyle.StandardPixmap.SP_DialogResetButton,
            "선택 속성을 현재 시점의 기본값으로 (Ctrl+Backspace)" if self.korean else "Reset selected property at playhead (Ctrl+Backspace)")
        property_heading.addWidget(self.reset_value_button)
        property_layout.addLayout(property_heading)
        self.edit_state = QLabel()
        self.edit_state.setObjectName("mutedLabel")
        property_layout.addWidget(self.edit_state)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        property_layout.addLayout(form)
        self.fields = {}
        self.sliders = {}
        self.slider_units = {}
        names = {"dx": ("X 이동량", "X offset"), "dy": ("Y 이동량", "Y offset"), "scale": ("크기", "Scale"),
                 "rotation": ("회전", "Rotation"), "opacity": ("투명도", "Opacity")}
        for name, (low, high, default) in MOTION_FIELDS.items():
            if name in ("dx", "scale"):
                group_label = QLabel(
                    ("위치" if name == "dx" else "크기와 모양")
                    if self.korean else
                    ("Position" if name == "dx" else "Size and appearance")
                )
                group_label.setObjectName("motionColumnHeading")
                form.addRow(group_label)
            spin = QDoubleSpinBox()
            percent = name in {"scale", "opacity"}
            spin.setRange(low * (100 if percent else 1), high * (100 if percent else 1))
            spin.setDecimals(2)
            spin.setSingleStep(1)
            spin.setKeyboardTracking(False)
            spin.setSuffix(" %" if percent else "°" if name == "rotation" else " px")
            spin.setAccessibleName(names[name][0 if self.korean else 1])
            spin.setFixedWidth(102)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setObjectName("inspectorValueSlider")
            slider.setAccessibleName(names[name][0 if self.korean else 1] + (" 슬라이더" if self.korean else " slider"))
            unit = 1 if name in {"dx", "dy"} else 10
            limit = (project_scene.artboard_rect.width() if name == "dx" else project_scene.artboard_rect.height() if name == "dy" else 180 if name == "rotation" else 300 if name == "scale" else 100)
            slider.setRange(0 if percent else -round(limit*unit), round(limit*unit))
            if name == "scale":
                slider.setMinimum(10)
            slider.setSingleStep(unit)
            slider.setPageStep(10*unit)
            self.sliders[name] = slider
            self.slider_units[name] = unit
            row = QWidget()
            controls = QHBoxLayout(row)
            controls.setContentsMargins(0, 0, 0, 0)
            controls.setSpacing(8)
            controls.addWidget(slider, 1)
            controls.addWidget(spin)
            form.addRow(names[name][0 if self.korean else 1], row)
            self.fields[name] = spin
            spin.valueChanged.connect(lambda value, key=name: self.edit_pose(**{key: value / (100 if key in {"scale", "opacity"} else 1)}))
            slider.sliderPressed.connect(self.begin_gesture)
            slider.sliderReleased.connect(self.end_gesture)
            slider.valueChanged.connect(lambda value, key=name: self.edit_pose(**{
                key: value/self.slider_units[key]/(100 if key in {"scale", "opacity"} else 1)}))
        self.undo = self.icon_button(QStyle.StandardPixmap.SP_ArrowBack, "되돌리기" if self.korean else "Undo")
        self.redo = self.icon_button(QStyle.StandardPixmap.SP_ArrowForward, "다시 실행" if self.korean else "Redo")
        property_layout.addStretch()
        note = QLabel("기본 위치에 대한 이동량과 배율입니다. 점이 없는 시점을 수정하면 새 키프레임을 만듭니다."
            if self.korean else "Offsets and multipliers of the base source. Editing between keys adds a keyframe.")
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        property_layout.addWidget(note)
        self.relative_note = note
        note.setVisible(not self.compact)
        property_scroll = QScrollArea()
        self.property_scroll = property_scroll
        property_scroll.setMinimumWidth(340)
        property_scroll.setWidgetResizable(True)
        property_scroll.setFrameShape(QFrame.Shape.NoFrame)
        property_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        property_scroll.setWidget(properties)
        splitter.addWidget(property_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([800, 340])
        self.preview_splitter = splitter
        lanes_host = QFrame()
        lanes_host.setObjectName("card")
        lane_layout = QVBoxLayout(lanes_host)
        lane_layout.setContentsMargins(12, 10, 12, 10)
        lane_layout.setSpacing(6)
        lane_heading = QHBoxLayout()
        lane_title = QLabel("키프레임" if self.korean else "Keyframes")
        lane_title.setObjectName("panelTitle")
        lane_heading.addWidget(lane_title)
        mode_label = QLabel("편집 수준" if self.korean else "Editor level")
        lane_heading.addWidget(mode_label)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("간단 모드" if self.korean else "Simple mode", False)
        self.mode_combo.addItem("고급 모드" if self.korean else "Advanced mode", True)
        mode_label.setBuddy(self.mode_combo)
        self.mode_combo.setToolTip(
            "간단 모드는 공통 시점을, 고급 모드는 속성별 곡선·보간을 편집합니다."
            if self.korean else
            "Simple: shared keyframes. Advanced: per-property curves and easing."
        )
        lane_heading.addWidget(self.mode_combo)
        lane_hint = QLabel()
        self.lane_hint = lane_hint
        lane_hint.setObjectName("mutedLabel")
        lane_heading.addWidget(lane_hint)
        lane_heading.addStretch()
        lane_heading.addWidget(self.undo)
        lane_heading.addWidget(self.redo)
        lane_layout.addLayout(lane_heading)
        self.timeline_layout = lane_layout
        self.simple_host = QWidget()
        simple_layout = QGridLayout(self.simple_host)
        simple_layout.setContentsMargins(0, 0, 0, 0)
        simple_layout.setSpacing(0)
        self.simple_ruler = MotionRuler()
        self.simple_ruler.seek_requested.connect(self.seek)
        simple_layout.addWidget(self.simple_ruler, 0, 0)
        simple_layout.setColumnStretch(0, 1)
        simple_row = QHBoxLayout()
        self.simple_timeline = MotionTimeline()
        self.simple_timeline.korean = self.korean
        self.simple_timeline.setAccessibleName("공통 키프레임 시간축" if self.korean else "Shared keyframe timeline")
        self.simple_timeline.point_selected.connect(self.seek)
        self.simple_timeline.seek_requested.connect(self.seek)
        self.simple_timeline.add_requested.connect(self.add_common_point)
        self.simple_timeline.delete_requested.connect(self.delete_common_point)
        self.simple_timeline.point_moved.connect(self.move_common_point)
        self.simple_timeline.gesture_started.connect(self.begin_gesture)
        self.simple_timeline.gesture_finished.connect(self.end_gesture)
        simple_layout.addWidget(self.simple_timeline, 1, 0)
        self.simple_add = self.icon_button("keyframe_add", "현재 자세 추가 (K)" if self.korean else "Add current pose (K)")
        self.simple_delete = self.icon_button("keyframe_remove", "현재 시점의 키프레임 삭제" if self.korean else "Delete keys at playhead")
        self.simple_previous = self.icon_button(QStyle.StandardPixmap.SP_MediaSkipBackward, "이전 자세" if self.korean else "Previous pose")
        self.simple_next = self.icon_button(QStyle.StandardPixmap.SP_MediaSkipForward, "다음 자세" if self.korean else "Next pose")
        self.simple_add.clicked.connect(lambda: self.add_common_point(self.progress))
        self.simple_delete.clicked.connect(lambda: self.delete_common_point(self.progress))
        self.simple_previous.clicked.connect(lambda: self.navigate_common(-1))
        self.simple_next.clicked.connect(lambda: self.navigate_common(1))
        # The two most important actions deserve readable labels instead of
        # four equally weighted unlabeled icons.
        for button, ko, en, width in (
            (self.simple_add, "자세 추가", "Add pose", 96),
            (self.simple_delete, "삭제", "Delete", 76),
        ):
            button.setText(ko if self.korean else en)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setFixedSize(width, 30)
        for button in (self.simple_previous, self.simple_next,
                       self.simple_add, self.simple_delete):
            simple_row.addWidget(button)
        simple_layout.addLayout(simple_row, 1, 1)
        self.simple_host.setToolTip("값 입력·화면 조작은 바꾼 속성에만 점을 만듭니다. 자세 추가는 켜진 모든 속성, 삭제·이동은 해당 시점의 켜진 점에 적용합니다."
            if self.korean else "Values and canvas edits key only changed properties. Add pose keys all enabled properties; delete/move affects enabled keys at that time.")
        lane_layout.addWidget(self.simple_host)
        lanes_grid = QGridLayout()
        lanes_grid.setContentsMargins(0, 0, 0, 0)
        lanes_grid.setSpacing(0)
        lanes_grid.setColumnStretch(1, 1)
        self.ruler = MotionRuler()
        self.ruler.seek_requested.connect(self.seek)
        lanes_grid.addWidget(self.ruler, 0, 1)
        for text, column in (("속성" if self.korean else "Property", 0), ("구간 곡선" if self.korean else "Interval curve", 2)):
            label = QLabel(text)
            label.setObjectName("motionColumnHeading")
            lanes_grid.addWidget(label, 0, column)
        self.lanes = {}
        for row, (name, labels) in enumerate(names.items(), 1):
            label = labels[0 if self.korean else 1]
            if name in {"dx", "dy"}:
                label = "X" if name == "dx" else "Y"
            left = QWidget()
            left.setObjectName("motionLaneSide")
            left_layout = QHBoxLayout(left)
            left_layout.setContentsMargins(0, 0, 10, 0)
            left_layout.setSpacing(2)
            toggle = QCheckBox()
            toggle.setChecked(True)
            toggle.setToolTip(f"{label} · " + ("모션 켜기/끄기 · 꺼도 키프레임은 보존됩니다." if self.korean else "Enable/disable motion. Keyframes are preserved."))
            toggle.setAccessibleName(toggle.toolTip())
            left_layout.addWidget(toggle)
            title = QToolButton()
            title.setText(label)
            title.setMinimumWidth(64)
            title.setObjectName("motionLaneLabel")
            left_layout.addWidget(title)
            buttons = []
            for icon, ko, en in (("keyframe_add", "키프레임 추가", "Add keyframe"), ("keyframe_remove", "키프레임 삭제", "Delete keyframe"),
                    (QStyle.StandardPixmap.SP_MediaSkipBackward, "이전 키프레임 선택", "Select previous keyframe"),
                    (QStyle.StandardPixmap.SP_MediaSkipForward, "다음 키프레임 선택", "Select next keyframe")):
                button = self.icon_button(icon, f"{label} · {ko if self.korean else en}")
                left_layout.addWidget(button)
                buttons.append(button)
            timeline = MotionTimeline()
            timeline.korean = self.korean
            timeline.setAccessibleName(f"{label} · " + ("키프레임 트랙" if self.korean else "Keyframe track"))
            right = QWidget()
            right.setObjectName("motionLaneSide")
            right_layout = QHBoxLayout(right)
            right_layout.setContentsMargins(6, 0, 0, 0)
            right_layout.setSpacing(4)
            graph = self.icon_button("motion_curve", "베지어 곡선 편집" if self.korean else "Edit Bezier curve")
            preset = QToolButton()
            preset.setObjectName("motionPresetButton")
            preset.setMinimumWidth(132)
            preset.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            right_layout.addWidget(graph)
            right_layout.addWidget(preset)
            lane = SimpleNamespace(timeline=timeline, title=title, toggle=toggle, left=left, right=right, add=buttons[0], delete=buttons[1],
                previous=buttons[2], next=buttons[3], graph=graph, preset=preset, preset_key="")
            self.lanes[name] = lane
            title.clicked.connect(lambda checked=False, n=name: self.select_time(n, self.progress))
            toggle.toggled.connect(lambda enabled, n=name: self.toggle_field(n, enabled))
            lanes_grid.addWidget(left, row, 0)
            lanes_grid.addWidget(timeline, row, 1)
            lanes_grid.addWidget(right, row, 2)
            timeline.seek_requested.connect(lambda value, n=name: self.select_time(n, value))
            timeline.point_selected.connect(lambda value, n=name: self.select_time(n, value))
            timeline.point_moved.connect(lambda old, new, n=name: self.move_point(old, new, n))
            timeline.add_requested.connect(lambda value, n=name: self.add_point(value, n))
            timeline.delete_requested.connect(lambda value, n=name: self.delete_point(value, n))
            timeline.gesture_started.connect(self.begin_gesture)
            timeline.gesture_finished.connect(self.end_gesture)
            lane.add.clicked.connect(lambda checked=False, n=name: self.add_point(self.progress, n))
            lane.delete.clicked.connect(lambda checked=False, n=name: self.delete_point(self.progress, n))
            lane.previous.clicked.connect(lambda checked=False, n=name: self.navigate(-1, n))
            lane.next.clicked.connect(lambda checked=False, n=name: self.navigate(1, n))
            graph.clicked.connect(lambda checked=False, n=name: self.edit_graph(n))
            preset.clicked.connect(lambda checked=False, n=name: self.open_presets(n))
        grid_host = QWidget()
        grid_host.setLayout(lanes_grid)
        self.lane_scroll = QScrollArea()
        self.lane_scroll.setWidgetResizable(True)
        self.lane_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.lane_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lane_scroll.setWidget(grid_host)
        self.lane_scroll.setFixedHeight(125 if self.compact else 245)
        lane_layout.addWidget(self.lane_scroll)
        self.timeline_host = lanes_host
        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)
        self.workspace_splitter.setObjectName("motionWorkspaceSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.addWidget(self.preview_splitter)
        self.workspace_splitter.addWidget(self.timeline_host)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([570, 190])
        outer.addWidget(self.workspace_splitter, 1)
        self.activate_field("dx")
        playback = QHBoxLayout()
        self.restart = self.icon_button(QStyle.StandardPixmap.SP_MediaSkipBackward, "처음으로" if self.korean else "Go to start")
        self.play = QToolButton()
        self.play.setObjectName("motionPlayButton")
        self.play.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.play.setIconSize(QSize(16, 16))
        self._play_icons = {False: self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay),
                            True: self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPause)}
        self.loop = QCheckBox("반복" if self.korean else "Loop")
        self.loop.setChecked(True)
        self.clock = QLabel()
        self.clock.setObjectName("motionClock")
        self.clock.setMinimumWidth(132)
        self.context = QLabel()
        self.context.setObjectName("mutedLabel")
        for widget in (self.restart, self.play, self.loop, self.clock):
            playback.addWidget(widget)
        playback.addStretch()
        self.context_label = QLabel()
        self.context_label.setObjectName("mutedLabel")
        playback.addWidget(self.context_label)
        playback.addWidget(self.context)
        lane_layout.insertLayout(1, playback)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName("mutedLabel")
        self.notice.setMinimumHeight(16)
        outer.addWidget(self.notice)
        legacy = source.keyframes
        self.has_legacy = bool(any(legacy.global_curves.values()) or any(any(c.values()) for c in
            (*legacy.single_track_curves.values(), *legacy.per_track_curves.values())))
        self.legacy_ack = QCheckBox("이 요소의 이전 키프레임 재생을 중지하고 데이터를 보존합니다." if self.korean else
                                    "Stop legacy keyframe playback for this source and preserve its data.")
        self.legacy_ack.setVisible(self.has_legacy and legacy.enabled)
        self.legacy_info = QLabel()
        self.legacy_info.setWordWrap(True)
        if self.has_legacy:
            scopes = []
            if any(legacy.global_curves.values()):
                scopes.append("전체 영상" if self.korean else "whole video")
            if any(any(c.values()) for c in legacy.single_track_curves.values()):
                scopes.append("특정 곡" if self.korean else "selected track")
            if any(any(c.values()) for c in legacy.per_track_curves.values()):
                scopes.append("곡별" if self.korean else "per track")
            self.legacy_info.setText(("이전 키프레임: " if self.korean else "Legacy keys: ") + ", ".join(scopes) +
                (" · 데이터는 저장 시 보존됩니다. 시간 구조가 달라 자동 변환하지 않습니다." if self.korean else
                 " · Data stays in saved projects. Different time models prevent automatic conversion."))
        self.legacy_info.setVisible(self.has_legacy)
        outer.addWidget(self.legacy_info)
        outer.addWidget(self.legacy_ack)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("적용" if self.korean else "Apply")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.setProperty("primary", True)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소" if self.korean else "Cancel")
        outer.addWidget(self.buttons)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(preview_interval_ms(screen_preview_fps(self.screen())))
        self.elapsed = QElapsedTimer()
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.setInterval(self.timer.interval())
        self.render_timer.timeout.connect(self.render_now)
        self.timer.timeout.connect(self.tick)
        self.edge_combo.currentIndexChanged.connect(self.change_edge)
        self.mode_combo.currentIndexChanged.connect(lambda: self.set_advanced(self.mode_combo.currentData()))
        self.connect_pose.clicked.connect(self.link_exit_pose)
        self.duration.valueChanged.connect(self.change_duration)
        self.track.currentIndexChanged.connect(self.request_render)
        self.original.toggled.connect(self.request_render)
        self.original.toggled.connect(self.refresh_values)
        self.video_sync.toggled.connect(self.request_render)
        self.guides.toggled.connect(self.set_guides)
        self.fit.clicked.connect(self.canvas.fit_canvas)
        self.actual.clicked.connect(lambda: self.canvas.zoom())
        self.zoom_in.clicked.connect(lambda: self.canvas.zoom(1.25))
        self.zoom_out.clicked.connect(lambda: self.canvas.zoom(.8))
        self.canvas.pose_changed.connect(self.edit_canvas_pose)
        self.canvas.gesture_started.connect(self.begin_gesture)
        self.canvas.gesture_finished.connect(self.end_gesture)
        self.canvas.render_requested.connect(self.request_render)
        self.play.clicked.connect(self.toggle_play)
        self.restart.clicked.connect(lambda: self.seek(self.play_minimum))
        self.undo.clicked.connect(self.undo_stack.undo)
        self.redo.clicked.connect(self.undo_stack.redo)
        self.undo_stack.canUndoChanged.connect(self.undo.setEnabled)
        self.undo_stack.canRedoChanged.connect(self.redo.setEnabled)
        self.apply_button.clicked.connect(self.apply_changes)
        self.buttons.rejected.connect(self.reject)
        self.legacy_ack.toggled.connect(self.refresh_values)
        self.guide_button.clicked.connect(self.show_guide)
        self.shortcut_button.clicked.connect(lambda: self.show_help("motion_shortcuts"))
        self.reset_value_button.clicked.connect(self.reset_active_value)
        from app.dialogs.motion_editor_shortcuts import MotionEditorShortcuts
        self.shortcuts = MotionEditorShortcuts(self)
        self.set_advanced(False)
        self.refresh_values()
        self.request_render()
        available = self.screen().availableGeometry()
        self.resize(min(1180, max(840, available.width()-40)),
            min(880, max(600 if self.compact else 720, available.height()-50)))

    @property
    def korean(self):
        return self.translator.is_korean

    def icon_button(self, icon, label):
        button = QToolButton()
        button.setObjectName("motionIconButton")
        button.setIcon(menu_icon(icon) if isinstance(icon, str) else self.style().standardIcon(icon))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setFixedSize(28, 28)
        return button

    @property
    def motion(self):
        return self.drafts[self.edge]

    @property
    def play_minimum(self):
        return -.5 if self.edge == "out" else 0.0

    @property
    def play_maximum(self):
        return 1.5 if self.edge == "in" else 1.0

    def showEvent(self, event):
        super().showEvent(event)
        if not self._guide_scheduled:
            self._guide_scheduled = True
            self.windowHandle().screenChanged.connect(self.adapt_to_screen)
            QTimer.singleShot(0, self.adapt_to_screen)
            QTimer.singleShot(0, self.maybe_show_guide)

    def adapt_to_screen(self, *_):
        if self._closed:
            return
        self._sync_preview_cadence()
        # Qt can place a new dialog on a different monitor from the one
        # reported before show(), so adapt to the actual window's screen.
        self.compact = self.screen().availableGeometry().height() < 800
        root = self.layout()
        margins = (16, 8, 16, 8) if self.compact else (24, 20, 24, 20)
        root.setContentsMargins(*margins)
        root.setSpacing(8 if self.compact else 12)
        self.setMinimumHeight(600 if self.compact else 720)
        self.canvas.setMinimumHeight(204 if self.compact else 240)
        self.lane_scroll.setFixedHeight(125 if self.compact else 245)
        self.instructions.setVisible(not self.compact)
        self.relative_note.setVisible(not self.compact)
        self.limitations.setVisible(bool(self.limitations.text()))
        self.view_menu_button.setVisible(self.compact)
        for button in self.view_buttons:
            button.setVisible(not self.compact)
        root.activate()
        self.fit_to_screen()

    def set_advanced(self, advanced):
        if self._gesture_before is not None:
            return
        self.advanced = bool(advanced)
        with QSignalBlocker(self.mode_combo):
            self.mode_combo.setCurrentIndex(int(self.advanced))
        self.lane_scroll.setVisible(self.advanced)
        self.simple_host.setVisible(not self.advanced)
        self.lane_hint.setText(
            ("속성별 키프레임과 곡선" if self.korean else "Per-property timing and easing")
            if self.advanced else
            ("현재 시점의 자세 편집" if self.korean else "Edit the pose at the playhead")
        )
        self.instructions.setText(
            ("① 속성별 타임라인 선택   ② 곡선 편집   ③ 미리보기로 확인"
             if self.korean else "① Select a property   ② Edit its curve   ③ Preview")
            if self.advanced else
            ("① 시점 선택   ② 미리보기에서 위치·크기 조절   ③ 재생 후 적용"
             if self.korean else "① Pick a time   ② Adjust position or size   ③ Play, then apply")
        )
        self.refresh_values()

    def draft_source(self):
        updates = {}
        for edge in self.touched:
            updates[f"animation_{edge}"] = "custom"
            updates[f"animation_{edge}_duration"] = self.durations[edge]
            updates["custom_intro_keyframes" if edge == "in" else "custom_outro_keyframes"] = self.drafts[edge]
        return replace(self.source, **updates)

    def preview_clock(self, track, index):
        self._media_clock_rate = 1
        track_start = sum(t.duration_seconds for t in self.tracks[:index])
        entering, duration = self.edge == "in", self.durations[self.edge]
        segment = self._segments.get(track.id)
        if segment is None:
            local, global_time = self.canvas.frame_times(entering, duration, self.progress, track, track_start)
            return local, global_time, track_start
        window = self.compiled_plan.presentation.windows[segment.window_index]
        if self.source.timeline_start > 0 or self.source.timeline_duration > 0:
            _, global_time = self.canvas.frame_times(entering, duration, self.progress, track, window.timeline_start)
        else:
            key = tuple(self.durations.items()), frozenset(self.touched)
            if self._clock_windows is None or self._clock_windows[0] != key:
                from app.animation.custom_motion_render import motion_windows
                source = self.draft_source()
                sources = [source if item.source.id == source.id else item.source for item in self.canvas.project_scene.items()
                    if hasattr(item, "source")]
                self._clock_windows = key, motion_windows(source, self.compiled_plan, sources)
            start, end, intro, outro = self._clock_windows[1][segment.window_index]
            global_time = start+self.progress*intro if entering else end-outro+self.progress*outro
            self._media_clock_rate = (intro if entering else outro)/duration
        local = window.source_time_at_start+(global_time-window.timeline_start)*window.playback_rate
        return local, global_time, window.timeline_start

    def refresh_connection(self):
        source = self.draft_source()
        differences = exit_pose_differences(source)
        self.connection_host.setVisible(bool(differences))
        self.connection_notice.setText("시작 마지막 자세와 종료 첫 자세가 다릅니다." if self.korean else
            "The entrance's final pose differs from the exit's first pose.")
        self.connection_notice.setToolTip(", ".join(self.fields[name].accessibleName() for name in differences))
        self.connect_pose.setEnabled(bool(differences) and not self.source.locked and not self.original.isChecked())

    def link_exit_pose(self):
        if not self.connect_pose.isEnabled() or self._gesture_before is not None:
            return
        before = self.state()
        source = self.draft_source()
        self.drafts["out"] = self.drafts["out"].with_start_pose(resting_pose(source))
        self.touched.add("out")
        self.edge, self.progress = "out", 0.0
        with QSignalBlocker(self.edge_combo):
            self.edge_combo.setCurrentIndex(1)
        self.record(before)

    def add_common_point(self, progress):
        if not self.can_edit or not 0 <= progress <= 1:
            return
        self.seek(progress)
        self.edit_pose(**{name: getattr(self.motion.at(progress), name) for name in MOTION_FIELDS
            if self.motion.field_enabled(name) and not any(abs(k.time-progress) < 1e-6 for k in self.motion.keys(name))})

    def delete_common_point(self, progress):
        if not self.can_edit or progress in (0, 1):
            return
        before = self.state()
        motion = self.motion
        for name in MOTION_FIELDS:
            if motion.field_enabled(name):
                motion = motion.without_key(name, progress)
        if motion != self.motion:
            self.drafts[self.edge] = motion
            self.touched.add(self.edge)
            self.record(before)

    def move_common_point(self, old, new):
        if not self.can_edit or old in (0, 1) or not 0 < new < 1:
            return
        fields = [name for name in MOTION_FIELDS if self.motion.field_enabled(name) and any(k.time == old for k in self.motion.keys(name))]
        if not fields or any(abs(t-new) <= 1e-6 and t != old for t in self.motion.key_times()):
            return
        before = self.state()
        motion = self.motion
        for name in fields:
            motion = motion.move_key(name, old, new)
        self.drafts[self.edge] = motion
        self.progress = new
        self.touched.add(self.edge)
        self.record(before)

    def navigate_common(self, direction):
        times = [t for t in self.motion.key_times() if (t-self.progress)*direction > 1e-6]
        if times:
            self.seek(min(times) if direction > 0 else max(times))

    def maybe_show_guide(self):
        from app.dialogs.motion_editor_guide import MOTION_GUIDE_KEY
        from app.dialogs.welcome_dialog import first_time
        if not self._closed and self.isVisible() and first_time(MOTION_GUIDE_KEY):
            self.show_guide()

    def modal_info(self, callback):
        resume = self.timer.isActive()
        self.timer.stop()
        self.refresh_values()
        callback()
        if resume and not self._closed:
            self.elapsed.start()
            self.timer.start()
            self.refresh_values()

    def show_guide(self):
        from app.dialogs.motion_editor_guide import MOTION_GUIDE
        from app.dialogs.welcome_dialog import GuideDialog
        from app.dialogs.help_dialog import install_help_shortcut
        def show():
            guide = GuideDialog(MOTION_GUIDE, ("키프레임 편집 시작하기", "Getting started with keyframes"), self.korean, self)
            install_help_shortcut(guide, ("canvas", "custom_motion"), self.translator)
            guide.exec()
            guide.deleteLater()
        self.modal_info(show)

    def show_help(self, topic="custom_motion"):
        from app.dialogs.help_dialog import open_help
        self.modal_info(lambda: open_help(self, self.translator, "canvas", topic))

    def reset_active_value(self):
        self.edit_pose(**{self.active_field: MOTION_FIELDS[self.active_field][2]})

    @property
    def can_edit(self):
        return not self.source.locked and 0 <= self.progress <= 1 and not self.original.isChecked()

    def activate_field(self, name):
        self.active_field = name
        lane = self.lanes[name]
        self.timeline, self.graph = lane.timeline, lane.graph
        self.add, self.delete = lane.add, lane.delete
        self.previous, self.next = lane.previous, lane.next
        if hasattr(self, "lane_scroll"):
            self.lane_scroll.ensureWidgetVisible(lane.title, 0, 4)

    def select_time(self, name, progress):
        self.activate_field(name)
        self.seek(progress)

    def toggle_field(self, name, enabled):
        if self._filling or not self.can_edit:
            return
        before = self.state()
        self.activate_field(name)
        self.drafts[self.edge] = self.motion.set_field_enabled(name, enabled)
        self.touched.add(self.edge)
        self.record(before)

    def state(self):
        return dict(self.drafts), dict(self.durations), set(self.touched), self.edge, self.progress, self.active_field

    def restore(self, state):
        drafts, durations, touched, edge, progress, name = state
        self.drafts, self.durations, self.touched = dict(drafts), dict(durations), set(touched)
        self.edge, self.progress = edge, progress
        self.activate_field(name)
        with QSignalBlocker(self.edge_combo):
            self.edge_combo.setCurrentIndex(0 if edge == "in" else 1)
        self.refresh_values()
        self.request_render()

    def record(self, before):
        if self._gesture_before is None and before != self.state():
            self.undo_stack.push(DraftCommand(self, before, self.state()))
        self.refresh_values()
        self.request_render()

    def begin_gesture(self):
        self._gesture_before = self.state()
        self._resume_after_gesture = self.timer.isActive()
        if self._resume_after_gesture:
            self.timer.stop()
            self.canvas.playing = False
            self.canvas.media.pause()

    def end_gesture(self):
        before, self._gesture_before = self._gesture_before, None
        if before is not None:
            self.record(before)
        if self._resume_after_gesture:
            self.elapsed.start()
            self.timer.start()
            self._resume_after_gesture = False

    def edit_pose(self, **changes):
        if self._filling or not self.can_edit:
            return
        before = self.state()
        created = any(not any(abs(p.time-self.progress) < 1e-6 for p in self.motion.keys(name)) for name in changes) if changes else False
        changes = {name: min(MOTION_FIELDS[name][1], max(MOTION_FIELDS[name][0], value)) for name, value in changes.items()}
        changes = {name: value for name, value in changes.items() if self.motion.field_enabled(name)}
        if not changes:
            return
        motion = self.motion
        for name, value in changes.items():
            motion = motion.with_key(name, self.progress, value=value)
        self.drafts[self.edge] = motion
        self.touched.add(self.edge)
        if created:
            self.notice.setText("현재 시점에 새 키프레임을 만들었습니다." if self.korean else "Created a new keyframe at the current time.")
        self.record(before)

    def edit_canvas_pose(self, changes):
        current = self.motion.at(self.progress)
        self.edit_pose(**{name: value for name, value in changes.items() if abs(value-getattr(current, name)) > 1e-9})

    def add_point(self, progress, name=None):
        if not self.can_edit or not 0 <= progress <= 1 or not self.motion.field_enabled(name or self.active_field):
            return
        name = name or self.active_field
        self.select_time(name, progress)
        self.edit_pose(**{name: getattr(self.motion.at(progress), name)})

    def delete_point(self, progress, name=None):
        if not self.can_edit or progress in (0, 1) or not self.motion.field_enabled(name or self.active_field):
            return
        name = name or self.active_field
        before = self.state()
        self.activate_field(name)
        self.drafts[self.edge] = self.motion.without_key(name, progress)
        self.touched.add(self.edge)
        self.record(before)

    def move_point(self, old, new, name=None):
        if not self.can_edit or old in (0, 1) or not 0 < new < 1 or not self.motion.field_enabled(name or self.active_field):
            return
        name = name or self.active_field
        point = next((p for p in self.motion.keys(name) if p.time == old), None)
        if point is None or any(abs(p.time-new) <= 1e-6 for p in self.motion.keys(name)):
            return
        before = self.state()
        self.activate_field(name)
        self.drafts[self.edge] = self.motion.move_key(name, old, new)
        self.progress = new
        self.touched.add(self.edge)
        self.record(before)

    def change_duration(self, value):
        if self._filling or not self.can_edit:
            return
        before = self.state()
        self.durations[self.edge] = value
        self.touched.add(self.edge)
        self.record(before)

    def change_edge(self):
        if self._filling:
            return
        self.edge = self.edge_combo.currentData()
        self.seek(0)

    def edit_graph(self, name=None):
        if not self.can_edit or not self.motion.field_enabled(name or self.active_field):
            return
        name = name or self.active_field
        self.activate_field(name)
        from app.timeline.bezier_editor import BezierEditor
        segment = self.motion.segment(name, self.progress)
        if segment is None:
            return
        point, following = segment
        duration = self.durations[self.edge]
        factor = 100 if name in {"scale", "opacity"} else 1
        dialog = BezierEditor(replace(point, time=point.time*duration, value=point.value*factor),
            replace(following, time=following.time*duration, value=following.value*factor),
            self.lanes[name].title.text(), self.korean, self)
        resume = self.timer.isActive()
        self.timer.stop()
        self.canvas.playing = False
        self.canvas.media.pause()
        self.lanes[name].preset.setText("사용자 지정..." if self.korean else "Custom...")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            before = self.state()
            self.drafts[self.edge] = self.motion.with_key(name, point.time, interpolation="bezier", bezier=dialog.handles, custom=True)
            self.touched.add(self.edge)
            self.record(before)
        self.refresh_values()
        dialog.deleteLater()
        if resume and not self._closed:
            self.elapsed.start()
            self.timer.start()
            self.refresh_values()

    def open_presets(self, name):
        if not self.can_edit or not self.motion.field_enabled(name):
            return
        self.activate_field(name)
        segment = self.motion.segment(name, self.progress)
        if segment is None:
            return
        menu = CurvePresetMenu(self.korean, preset_name(self.motion, name, segment[0]), self)
        resume = self.timer.isActive()
        self.timer.stop()
        self.refresh_values()
        def closed():
            if resume and not self._closed:
                self.elapsed.start()
                self.timer.start()
                self.refresh_values()
        menu.aboutToHide.connect(closed)
        menu.chosen.connect(lambda choice: self.apply_preset(name, choice))
        button = self.lanes[name].preset
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))

    def apply_preset(self, name, choice):
        if not self.can_edit or not self.motion.field_enabled(name):
            return
        self.activate_field(name)
        if choice == "custom":
            self.edit_graph(name)
            return
        segment = self.motion.segment(name, self.progress)
        preset = next((p for p in PRESETS if p[0] == choice), None)
        if segment is None or preset is None:
            return
        before = self.state()
        self.drafts[self.edge] = self.motion.with_key(name, segment[0].time,
            interpolation=preset[3], bezier=preset[4], custom=False)
        self.touched.add(self.edge)
        self.record(before)

    def seek(self, progress):
        self.progress = round(min(self.play_maximum, max(self.play_minimum, progress)), 6)
        self.refresh_values()
        self.request_render()

    def navigate(self, direction, name=None):
        name = name or self.active_field
        self.activate_field(name)
        times = [p.time for p in self.motion.keys(name) if (p.time-self.progress)*direction > 1e-6]
        if times:
            self.seek(min(times) if direction > 0 else max(times))

    def toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
        else:
            if self.progress >= self.play_maximum:
                self.seek(self.play_minimum)
            self.elapsed.start()
            self.timer.start()
        self.refresh_values()

    def _sync_preview_cadence(self):
        interval = preview_interval_ms(screen_preview_fps(self.screen()))
        if interval != self.timer.interval():
            self.timer.setInterval(interval)
            self.render_timer.setInterval(interval)

    def tick(self):
        self._sync_preview_cadence()
        progress = self.progress + self.elapsed.restart() / 1000 / max(.1, self.durations[self.edge])
        if progress >= self.play_maximum:
            if self.loop.isChecked():
                progress = self.play_minimum + (progress-self.play_minimum) % (self.play_maximum-self.play_minimum)
            else:
                progress = self.play_maximum
                self.timer.stop()
        self.seek(progress)

    def set_guides(self, checked):
        self.canvas.guides = checked
        self.canvas.viewport().update()

    def refresh_values(self, *_):
        self._filling = True
        try:
            self.duration.setValue(self.durations[self.edge])
            self.duration.setEnabled(self.can_edit)
            pose = self.motion.at(self.progress)
            for name, spin in self.fields.items():
                value = getattr(pose, name)*(100 if name in {"scale", "opacity"} else 1)
                spin.setValue(value)
                enabled = self.can_edit and self.motion.field_enabled(name)
                spin.setEnabled(enabled)
                slider = self.sliders[name]
                slider_value = round(value*self.slider_units[name])
                # Numeric entry can exceed the initial comfortable slider span.
                # Expand without clamping the authored value or changing units.
                if slider_value < slider.minimum():
                    slider.setMinimum(slider_value)
                if slider_value > slider.maximum():
                    slider.setMaximum(slider_value)
                if name in {"dx", "dy", "rotation"}:
                    extent = max(abs(slider.minimum()), abs(slider.maximum()))
                    slider.setRange(-extent, extent)
                slider.setValue(slider_value)
                slider.setEnabled(enabled)
            for name, lane in self.lanes.items():
                keys = self.motion.keys(name)
                point = next((p for p in keys if abs(p.time-self.progress) < 1e-6), None)
                segment = self.motion.segment(name, self.progress)
                timeline = lane.timeline
                timeline.points, timeline.progress, timeline.selected = keys, self.progress, self.progress
                timeline.minimum, timeline.maximum = self.play_minimum, self.play_maximum
                enabled = self.motion.field_enabled(name)
                editable = self.can_edit and enabled
                timeline.editable = editable
                timeline.enabled_track = enabled
                timeline.active = name == self.active_field
                timeline.segment = (segment[0].time, segment[1].time) if segment else None
                timeline.update()
                lane.toggle.setChecked(enabled)
                lane.toggle.setEnabled(self.can_edit)
                lane.add.setEnabled(editable)
                lane.delete.setEnabled(editable and bool(point) and self.progress not in (0, 1))
                lane.previous.setEnabled(any(p.time < self.progress-1e-6 for p in keys))
                lane.next.setEnabled(any(p.time > self.progress+1e-6 for p in keys))
                lane.graph.setEnabled(editable and bool(segment))
                lane.preset.setEnabled(editable and bool(segment))
                lane.preset_key = preset_name(self.motion, name, segment[0]) if segment else ""
                item = next((p for p in PRESETS if p[0] == lane.preset_key), None)
                lane.preset.setText((item[1] if self.korean else item[2]) + " ▾" if item else "—")
                active = name == self.active_field
                for widget in (lane.title, lane.left, lane.right):
                    if widget.property("active") != active:
                        widget.setProperty("active", active)
                        widget.style().unpolish(widget)
                        widget.style().polish(widget)
                        widget.update()
                if segment:
                    lane.preset.setIcon(curve_icon(segment[0].interpolation, segment[0].bezier))
                    lane.preset.setIconSize(QSize(32, 16))
                if segment:
                    lane.graph.setToolTip(f"{segment[0].time*self.durations[self.edge]:.3f} s → {segment[1].time*self.durations[self.edge]:.3f} s")
            self.ruler.minimum, self.ruler.maximum = self.play_minimum, self.play_maximum
            self.ruler.duration = self.durations[self.edge]
            self.ruler.update()
            common = self.simple_timeline
            common.points = tuple(Keyframe(t, 0) for t in self.motion.key_times())
            common.progress = common.selected = self.progress
            common.minimum, common.maximum = self.play_minimum, self.play_maximum
            common.editable = self.can_edit and any(self.motion.field_enabled(n) for n in MOTION_FIELDS)
            common.editable_times = {k.time for n in MOTION_FIELDS if self.motion.field_enabled(n) for k in self.motion.keys(n)}
            common.active = True
            common.update()
            self.simple_ruler.minimum, self.simple_ruler.maximum = self.play_minimum, self.play_maximum
            self.simple_ruler.duration = self.durations[self.edge]
            self.simple_ruler.update()
            self.simple_add.setEnabled(common.editable)
            self.simple_delete.setEnabled(common.editable and self.progress not in (0, 1) and any(
                self.motion.field_enabled(n) and any(abs(k.time-self.progress) < 1e-6 for k in self.motion.keys(n)) for n in MOTION_FIELDS))
            self.simple_previous.setEnabled(any(t < self.progress-1e-6 for t in self.motion.key_times()))
            self.simple_next.setEnabled(any(t > self.progress+1e-6 for t in self.motion.key_times()))
            self.clock.setText(f"{self.progress * self.durations[self.edge]:.3f} / {self.durations[self.edge]:.3f} s")
            self.play.setText(("일시 정지" if self.korean else "Pause") if self.timer.isActive() else ("재생" if self.korean else "Play"))
            self.play.setIcon(self._play_icons[self.timer.isActive()])
            self.play.setAccessibleName(self.play.text())
            state = (("꺼짐" if self.korean else "Disabled") if not self.motion.field_enabled(self.active_field) else
                ("편집 가능" if self.korean else "Editable") if self.can_edit else ("미리보기 전용" if self.korean else "Preview only"))
            self.edit_state.setText(f"{self.lanes[self.active_field].title.text()} · {state}" if self.advanced else
                (("현재 자세 · " if self.korean else "Current pose · ") + f"{self.progress*self.durations[self.edge]:.3f} s"))
            self.reset_value_button.setVisible(self.advanced)
            self.refresh_connection()
            self.context_label.setText(("효과 후 미리보기" if self.edge == "in" else "효과 전 미리보기") if self.korean else
                                       ("After effect" if self.edge == "in" else "Before effect"))
            self.context.setToolTip(self.context_label.text() + (" · 읽기 전용" if self.korean else " · Preview only"))
            self.context.setText(f"{self.durations[self.edge]/2:.3f} s · 50%")
            self.reset_value_button.setEnabled(self.can_edit and self.motion.field_enabled(self.active_field))
            self.undo.setEnabled(self.undo_stack.canUndo())
            self.redo.setEnabled(self.undo_stack.canRedo())
            self.canvas.playing = self.timer.isActive()
            if not self.canvas.playing:
                self.canvas.media.pause()
            self.apply_button.setEnabled(not self.source.locked and (not (self.has_legacy and self.source.keyframes.enabled) or self.legacy_ack.isChecked()))
            if not 0 <= self.progress <= 1:
                self.notice.setText("효과 밖 미리보기 · 이 구간은 편집할 수 없습니다." if self.korean else "Outside the effect · Preview only, editing disabled.")
            elif self.notice.text() in ("효과 밖 미리보기 · 이 구간은 편집할 수 없습니다.", "Outside the effect · Preview only, editing disabled."):
                self.notice.clear()
            self.notice.setVisible(bool(self.notice.text()))
        finally:
            self._filling = False

    def request_render(self, *_):
        if not self._closed and not self.render_timer.isActive():
            self.render_timer.start()

    def render_now(self, *, full_size=False):
        if self._closed:
            return None
        # A direct render consumes the queued request too. Otherwise a slow
        # seek can immediately render the same state again on the next event.
        self.render_timer.stop()
        index = max(0, self.track.currentIndex())
        track = self.tracks[index]
        local, global_time, track_start = self.preview_clock(track, index)
        bass, levels, reaction_notice = self.reaction_preview(track, local)
        messages = []
        self.canvas.video_sync = self.video_sync.isChecked()
        image = self.canvas.render_frame(self.motion, self.edge == "in", self.durations[self.edge], self.progress,
            track, index, len(self.tracks), original=self.original.isChecked(), full_size=full_size,
            held_pose=resting_pose(self.draft_source()), track_start=track_start,
            bass_level=bass, music_levels=levels, playlist_tracks=self.tracks,
            clock=(local, global_time), compiled_plan=self.compiled_plan, segment=self._segments.get(track.id),
            context_source=self.draft_source(), clock_rate=self._media_clock_rate)
        self.canvas.handles.editable = self.can_edit
        self.canvas.handles.enabled_fields = {name for name in MOTION_FIELDS if self.motion.field_enabled(name)}
        self.canvas.handles.update()
        video_notice = self.canvas.media.status_text(self.korean)
        if video_notice:
            messages.append(video_notice)
        if reaction_notice:
            messages.append(reaction_notice)
        text = " · ".join(messages)
        if self.limitations.text() != text:
            self.limitations.setText(text)
            self.preview_title.setToolTip(text)
            self.limitations.setVisible(bool(text))
        return image

    def reaction_preview(self, track, local):
        from app.preview.audio_reaction_cache import REACTION_CACHE
        from app.renderer.python_visualizer import PythonVisualizerRenderer
        cached = REACTION_CACHE.get(track)
        real = sample = False
        bass, levels = .5, {}
        fps, envelope, envelopes = cached if cached is not None else (30, None, {})
        for item in self.canvas.project_scene.items():
            source = getattr(item, "source", None)
            if source is None or not source.visible:
                continue
            if source.uses_bass_reaction:
                if source.source_type.value == "background":
                    available = envelope is not None and len(envelope) > 0
                    if available:
                        bass = float(envelope[min(max(0, round(local*fps)), len(envelope)-1)])
                else:
                    data = envelopes.get(source.music_reaction_profile)
                    available = data is not None and len(data) > 0
                    levels[source.id] = PythonVisualizerRenderer.music_reaction_level(data, fps, local, source.music_reactive_offset) if available else .5
                real |= available
                sample |= not available
            if source.source_type.value in {"audio_visualizer", "audio_waveform", "audio_level_meter", "particle_overlay"}:
                sample = True
        notice = (("음악 반응: 실제 분석 + 예시 (시각화·미분석 효과)" if self.korean else "Audio reaction: cached data + samples (visualizers/uncached effects)") if real and sample else
                  ("음악 반응: 저장된 실제 분석" if self.korean else "Audio reaction: cached real analysis") if real else
                  ("음악 반응·파티클: 예시 (이 창에서는 분석하지 않음)" if self.korean else "Audio reaction/particles: samples (no analysis in this editor)") if sample else "")
        return bass, levels, notice

    def apply_changes(self):
        if not self.apply_button.isEnabled():
            return
        changes = {}
        for edge in self.touched:
            motion = self.drafts[edge]
            if CustomMotion.from_dict(asdict(motion), edge == "in") != motion:
                self.notice.setText("키프레임 값이 유효하지 않습니다." if self.korean else "Invalid keyframe values.")
                self.notice.show()
                return
            changes[f"animation_{edge}"] = "custom"
            changes[f"animation_{edge}_duration"] = self.durations[edge]
            changes["custom_intro_keyframes" if edge == "in" else "custom_outro_keyframes"] = motion
        if self.has_legacy and self.source.keyframes.enabled:
            changes["keyframes"] = replace(deepcopy(self.source.keyframes), enabled=False)
        self.changes = changes
        self.accept()

    def done(self, result):
        if self._closed:
            return
        self._closed = True
        self.shortcuts.close()
        self.timer.stop()
        self.render_timer.stop()
        self.canvas.close_resources()
        super().done(result)
