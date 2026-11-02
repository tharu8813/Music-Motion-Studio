"""A segment timing graph; changes become one history operation on Apply."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QHBoxLayout,
    QLabel, QVBoxLayout, QWidget,
)

from app.animation.keyframes import DEFAULT_BEZIER, cubic_coordinate, normalize_bezier
from app.ui.design_system import COLORS


class BezierGraph(QWidget):
    handles_changed = Signal(tuple)

    def __init__(self, handles, parent=None):
        super().__init__(parent)
        self.handles = normalize_bezier(handles)
        self.dragged = None
        self.setMinimumSize(360, 240)
        self.setMouseTracking(True)
        self.setAccessibleName("베지어 제어점 그래프 / Bezier control point graph")
        self.fit_range()

    def fit_range(self):
        self.lower = min(0, self.handles[1], self.handles[3]) - .15
        self.upper = max(1, self.handles[1], self.handles[3]) + .15

    def plot_rect(self):
        return QRectF(54, 20, max(1, self.width() - 82), max(1, self.height() - 62))

    def position(self, x, y):
        rect = self.plot_rect()
        return QPointF(rect.left() + x * rect.width(),
                       rect.bottom() - (y - self.lower) / (self.upper - self.lower) * rect.height())

    def set_handles(self, handles):
        self.handles = normalize_bezier(handles)
        if self.dragged is None:
            self.fit_range()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS["field"]))
        painter.setPen(QColor(COLORS["muted"]))
        rect = self.plot_rect()
        for i in range(5):
            x = i / 4
            painter.setPen(QPen(QColor(COLORS["border"]), 1))
            painter.drawLine(self.position(x, self.lower), self.position(x, self.upper))
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QRectF(rect.left() + x * rect.width() - 20, rect.bottom() + 6, 40, 20),
                             Qt.AlignmentFlag.AlignCenter, f"{x:g}")
        for y in (self.lower, 0, .5, 1, self.upper):
            pos = self.position(0, y)
            painter.setPen(QPen(QColor(COLORS["border"]), 1))
            painter.drawLine(pos, self.position(1, y))
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QRectF(0, pos.y() - 10, 44, 20), Qt.AlignmentFlag.AlignRight, f"{y:.2g}")
        x1, y1, x2, y2 = self.handles
        start, end = self.position(0, 0), self.position(1, 1)
        first, second = self.position(x1, y1), self.position(x2, y2)
        painter.setPen(QPen(QColor(COLORS["muted"]), 1, Qt.PenStyle.DashLine))
        painter.drawLine(start, first)
        painter.drawLine(end, second)
        path = QPainterPath(start)
        for i in range(1, 241):
            t = i / 240
            path.lineTo(self.position(cubic_coordinate(t, x1, x2), cubic_coordinate(t, y1, y2)))
        painter.setPen(QPen(QColor(COLORS["warning"]), 2.5))
        painter.drawPath(path)
        for index, point in enumerate((first, second)):
            painter.setBrush(QColor(COLORS["accent"]))
            painter.setPen(QPen(QColor(COLORS["text"]), 1.5))
            painter.drawEllipse(point, 7, 7)
            painter.drawText(point + QPointF(12, -10), f"P{index + 1}")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            points = (self.position(*self.handles[:2]), self.position(*self.handles[2:]))
            distances = [(point - event.position()).manhattanLength() for point in points]
            nearest = min(range(2), key=distances.__getitem__)
            if distances[nearest] <= 22:
                self.dragged = nearest
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragged is not None:
            rect = self.plot_rect()
            x = (event.position().x() - rect.left()) / rect.width()
            y = self.lower + (rect.bottom() - event.position().y()) / rect.height() * (self.upper - self.lower)
            handles = list(self.handles)
            handles[2 * self.dragged:2 * self.dragged + 2] = (x, y)
            self.handles = normalize_bezier(handles)
            self.handles_changed.emit(self.handles)
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.dragged is not None:
            self.dragged = None
            self.fit_range()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class BezierEditor(QDialog):
    def __init__(self, point, following, label, korean=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("베지어 그래프" if korean else "Bezier graph")
        self.resize(780, 560)
        layout = QVBoxLayout(self)
        title = QLabel(f"{label} · {point.time:g} s → {following.time:g} s · {point.value:g} → {following.value:g}")
        layout.addWidget(title)
        hint = QLabel("다음 키프레임까지의 변화입니다. P1·P2를 드래그하거나 아래 좌표를 입력하세요.\n"
                      "가로: 시간(0–1) · 세로: 변화량 · 범위를 넘는 변화는 속성의 최솟값·최댓값에서 제한됩니다."
                      if korean else "Change until the next keyframe. Drag P1/P2 or enter their coordinates below.\n"
                      "Horizontal: time (0–1) · Vertical: progress · Overshoot is limited by the property's bounds.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.graph = BezierGraph(point.bezier, self)
        layout.addWidget(self.graph, 1)
        controls = QHBoxLayout()
        self.preset = QComboBox()
        for name, handles in (
            (("프리셋…" if korean else "Presets…"), None),
            (("기본" if korean else "Default"), DEFAULT_BEZIER),
            (("선형" if korean else "Linear"), (0, 0, 1, 1)),
            (("서서히 시작" if korean else "Ease in"), (.42, 0, 1, 1)),
            (("서서히 종료" if korean else "Ease out"), (0, 0, .58, 1)),
            (("부드럽게" if korean else "Ease in out"), (.42, 0, .58, 1)),
            (("반동" if korean else "Overshoot"), (.34, 1.56, .64, 1)),
        ):
            self.preset.addItem(name, handles)
        controls.addWidget(self.preset)
        self.fields = []
        for index, name in enumerate(("P1 X", "P1 Y", "P2 X", "P2 Y")):
            field = QDoubleSpinBox()
            field.setRange(0 if index % 2 == 0 else -2, 1 if index % 2 == 0 else 3)
            field.setDecimals(4)
            field.setSingleStep(.01)
            field.setAccessibleName(name)
            field.setKeyboardTracking(False)
            controls.addWidget(QLabel(name))
            controls.addWidget(field)
            self.fields.append(field)
            field.valueChanged.connect(self.coordinates_changed)
        layout.addLayout(controls)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("적용" if korean else "Apply")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소" if korean else "Cancel")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.graph.handles_changed.connect(self.set_handles)
        self.preset.currentIndexChanged.connect(self.choose_preset)
        self.set_handles(point.bezier)

    @property
    def handles(self):
        return self.graph.handles

    def choose_preset(self, index):
        handles = self.preset.itemData(index)
        if handles is not None:
            self.set_handles(handles)

    def set_handles(self, handles):
        for field, value in zip(self.fields, handles):
            field.blockSignals(True)
            field.setValue(value)
            field.blockSignals(False)
        self.graph.set_handles(handles)

    def coordinates_changed(self):
        self.graph.set_handles(tuple(field.value() for field in self.fields))
