"""Keyframe affordances; SourceInspector remains responsible for source-specific UI."""
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.animation.keyframes import MODES, PROPERTIES, point_at, property_value, supports_property


INSPECTOR_FIELDS = {name: (name, f"{name}_spin") for name in PROPERTIES if "." not in name}
INSPECTOR_FIELDS.update({
    "border_radius": ("border_radius", "radius_spin"),
    "outline_width": ("outline", "outline_spin"),
    "shadow.opacity": ("shadow_opacity", "shadow_opacity_spin"),
    "shadow.blur_radius": ("shadow_blur", "shadow_blur_spin"),
    "shadow.offset_x": ("shadow_x", "shadow_x_spin"),
    "shadow.offset_y": ("shadow_y", "shadow_y_spin"),
})


class InspectorKeyframeBinding:
    def __init__(self, inspector, controller, *, show_controls=True):
        self.inspector = inspector
        self.controller = controller
        self.show_controls = show_controls
        self.buttons = {}
        for name in PROPERTIES:
            spec = PROPERTIES[name]
            field, attribute = INSPECTOR_FIELDS[name]
            getattr(inspector, attribute).setRange(spec.minimum, spec.maximum)
            host = inspector._field_hosts[field]
            parent_layout = next(form for form in inspector._content.findChildren(QFormLayout)
                                 if form.getWidgetPosition(host)[0] >= 0)
            # QFormLayout owns a row; preserve labels and field visibility.
            row, role = parent_layout.getWidgetPosition(host)
            parent_layout.removeWidget(host)
            wrapper = QWidget()
            layout = QHBoxLayout(wrapper)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(4)
            layout.addWidget(host, 1)
            button = QPushButton("◇")
            button.setFixedWidth(28)
            button.setObjectName("keyframeDiamondButton")
            button.setStyleSheet("padding: 0px; min-width: 0px;")
            button.clicked.connect(lambda _=False, key=name: controller.toggle(key))
            layout.addWidget(button)
            parent_layout.setWidget(row, role, wrapper)
            inspector._field_hosts[field] = wrapper
            self.buttons[name] = button
        self.panel = QWidget()
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 12)
        self.enabled = QCheckBox()
        self.mode = QComboBox()
        for mode in MODES:
            self.mode.addItem(mode, mode)
        self.track = QComboBox()
        self.auto = QCheckBox()
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName("mutedLabel")
        self.open = QPushButton()
        navigation = QHBoxLayout()
        self.previous = QPushButton("‹ ◆")
        self.next = QPushButton("◆ ›")
        navigation.addWidget(self.previous)
        navigation.addWidget(self.next)
        for widget in (self.enabled, self.mode, self.track, self.auto, self.note):
            layout.addWidget(widget)
        layout.addLayout(navigation)
        layout.addWidget(self.open)
        inspector.animation_group.layout().insertWidget(0, self.panel)
        self.enabled.toggled.connect(lambda enabled: controller.configure(enabled=enabled))
        self.mode.currentIndexChanged.connect(lambda _: controller.configure(mode=self.mode.currentData()))
        self.track.currentIndexChanged.connect(lambda _: controller.choose_track(self.track.currentData() or ""))
        self.auto.toggled.connect(lambda enabled: setattr(controller, "auto_key", enabled))
        self.open.clicked.connect(controller.editor_requested.emit)
        self.previous.clicked.connect(lambda: controller.navigate(-1))
        self.next.clicked.connect(lambda: controller.navigate(1))
        controller.changed.connect(self.refresh)
        controller.position_changed.connect(self.refresh_values)
        inspector.translator.language_changed.connect(self.refresh)
        self.refresh()
        self.panel.setVisible(show_controls)

    def refresh(self):
        inspector, controller = self.inspector, self.controller
        source = controller.source
        korean = inspector.translator.is_korean
        single = bool(source and len(controller.store.selected_ids) == 1 and not source.locked)
        self.panel.setEnabled(single)
        self.enabled.setText("키프레임 활성화" if korean else "Enable keyframes")
        self.auto.setText("자동 키프레임" if korean else "Auto keyframe")
        self.open.setText("키프레임 편집기 열기" if korean else "Open keyframe editor")
        self.previous.setToolTip("이전 키프레임" if korean else "Previous keyframe")
        self.next.setToolTip("다음 키프레임" if korean else "Next keyframe")
        self.note.setText("자동 OFF: 현재 키프레임이 있으면 해당 값, 없으면 기본값을 수정합니다. 모드를 바꿔도 데이터는 보존됩니다."
                          if korean else "Auto OFF: edit the current keyframe when present; otherwise edit the base value. Switching modes preserves data.")
        self.mode.setAccessibleName("적용 시간 기준" if korean else "Animation time basis")
        self.track.setAccessibleName("편집할 곡" if korean else "Track to edit")
        with QSignalBlocker(self.mode), QSignalBlocker(self.enabled), QSignalBlocker(self.track), QSignalBlocker(self.auto):
            for index, label in enumerate(("전체 영상", "특정 곡", "모든 곡 개별") if korean else
                                          ("Whole video", "Single track", "Each track independently")):
                self.mode.setItemText(index, label)
            self.track.clear()
            self.track.addItem("곡 선택…" if korean else "Choose a track…", "")
            if source:
                animation = source.keyframes
                mapping = animation.single_track_curves if animation.mode == "single_track" else animation.per_track_curves
                for track, _, _ in controller.windows:
                    has_points = any(mapping.get(track.id, {}).values())
                    self.track.addItem(f"{'◆ ' if has_points else ''}{track.title}", track.id)
                if controller.track_id and self.track.findData(controller.track_id) < 0:
                    self.track.addItem("사용할 수 없는 곡" if korean else "Unavailable track", controller.track_id)
                self.mode.setCurrentIndex(MODES.index(animation.mode))
                self.enabled.setChecked(animation.enabled)
            self.track.setCurrentIndex(max(0, self.track.findData(controller.track_id)))
            self.auto.setChecked(controller.auto_key)
        self.track.setVisible(bool(source and source.keyframes.mode != "global"))
        self.refresh_values()

    def refresh_values(self, *_):
        inspector, controller = self.inspector, self.controller
        source = controller.source
        korean = inspector.translator.is_korean
        single = bool(source and len(controller.store.selected_ids) == 1 and not source.locked)
        values = controller.edit_values() if single else {}
        for name, button in self.buttons.items():
            points = controller.curves().get(name, [])
            current = point_at(points, controller.time)
            button.setText("◆" if current else "◇")
            supported = bool(source and supports_property(source, name))
            button.setVisible(supported and self.show_controls)
            button.setEnabled(controller.editable and supported)
            button.setStyleSheet("padding: 0px; min-width: 0px;" + ("color: #DEC18D;" if points else ""))
            description = (("현재 키프레임 삭제" if current else "현재 위치에 키프레임 추가") if korean else
                           ("Remove current keyframe" if current else "Add keyframe at playhead"))
            button.setAccessibleName(f"{PROPERTIES[name].labels[0 if korean else 1]}: {description}")
            button.setToolTip(description + (" · 애니메이션 있음" if korean else " · Animated") if points else description)
            if single:
                spin = getattr(inspector, INSPECTOR_FIELDS[name][1])
                previous = inspector._updating
                inspector._updating = True
                try:
                    spin.setValue(values.get(name, property_value(source, name)))
                finally:
                    inspector._updating = previous
