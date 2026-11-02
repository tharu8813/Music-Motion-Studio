"""Graph thumbnails in a menu-sized preset palette, shared by property lanes."""
from functools import lru_cache
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGridLayout, QMenu, QToolButton, QWidget, QWidgetAction
from app.animation.keyframes import DEFAULT_BEZIER, Keyframe, evaluate_curve
from app.ui.design_system import COLORS

PRESETS = (
    ("hold", "유지", "Hold", "hold", DEFAULT_BEZIER),
    ("linear", "선형", "Linear", "linear", DEFAULT_BEZIER),
    ("ease_in", "서서히 시작", "Ease in", "ease_in", DEFAULT_BEZIER),
    ("ease_out", "서서히 종료", "Ease out", "ease_out", DEFAULT_BEZIER),
    ("ease_in_out", "부드럽게", "Ease in out", "ease_in_out", DEFAULT_BEZIER),
    ("overshoot", "반동", "Overshoot", "bezier", (.34, 1.56, .64, 1)),
    ("custom", "사용자 지정...", "Custom...", "bezier", DEFAULT_BEZIER),
)


def preset_name(motion, field, point):
    custom = next((c.custom_segments for c in motion.curves if c.field == field), ())
    if point.time in custom:
        return "custom"
    if point.interpolation == "bezier":
        return "overshoot" if point.bezier == PRESETS[5][4] else "custom"
    return point.interpolation


@lru_cache(maxsize=64)
def curve_icon(interpolation, handles):
    pixmap = QPixmap(100, 48)
    pixmap.fill(QColor(COLORS["field"]))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(COLORS["border"]), 1))
    painter.drawLine(8, 40, 92, 40)
    painter.drawLine(8, 6, 8, 40)
    path = QPainterPath()
    keys = (Keyframe(0, 0, interpolation, handles), Keyframe(1, 1))
    for i in range(81):
        value = evaluate_curve(keys, i/80, 0)
        x, y = 8 + i/80*84, 40-value*28
        path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
    painter.setPen(QPen(QColor(COLORS["accent"]), 2))
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


class CurvePresetMenu(QMenu):
    chosen = Signal(str)

    def __init__(self, korean, selected, parent=None):
        super().__init__(parent)
        self.setObjectName("motionCurvePresetMenu")
        host = QWidget()
        grid = QGridLayout(host)
        self.buttons = {}
        for i, (key, ko, en, interpolation, handles) in enumerate(PRESETS):
            button = QToolButton(host)
            button.setText(ko if korean else en)
            button.setAccessibleName(button.text())
            button.setIcon(curve_icon(interpolation, handles))
            button.setIconSize(QSize(100, 48))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setCheckable(True)
            button.setChecked(key == selected)
            button.setMinimumSize(118, 80)
            button.clicked.connect(lambda checked=False, name=key: self.choose(name))
            grid.addWidget(button, i//3, i%3)
            self.buttons[key] = button
        action = QWidgetAction(self)
        action.setDefaultWidget(host)
        self.addAction(action)

    def choose(self, name):
        self.close()
        self.chosen.emit(name)
