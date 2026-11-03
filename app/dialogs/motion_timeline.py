"""One property lane with interval selection and read-only context time."""
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QMenu, QWidget
from app.ui.design_system import COLORS


class MotionTimeline(QWidget):
    seek_requested = Signal(float)
    point_selected = Signal(float)
    point_moved = Signal(float, float)
    add_requested = Signal(float)
    delete_requested = Signal(float)
    gesture_started = Signal()
    gesture_finished = Signal()
    segment_selected = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(38)
        self.setMouseTracking(True)
        self.points = ()
        self.progress = self.selected = 0.0
        self.duration = .6
        self.dragged = None
        self.korean = True
        self.minimum, self.maximum = 0.0, 1.0
        self.segment = None
        self.editable = True
        self.active = False
        self.enabled_track = True
        self.editable_times = None
        self.setAccessibleName("애니메이션 키프레임 / Animation keyframes")

    def point_x(self, progress):
        return 12 + (progress-self.minimum) / (self.maximum-self.minimum) * max(1, self.width()-24)

    def time_at(self, x):
        return round(min(self.maximum, max(self.minimum, self.minimum +
            (x-12) / max(1, self.width()-24) * (self.maximum-self.minimum))), 6)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLORS["info_surface"] if self.active else COLORS["field"]))
        if self.minimum < 0:
            painter.fillRect(0, 0, int(self.point_x(0)), self.height(), QColor(COLORS["alternate"]))
        if self.maximum > 1:
            painter.fillRect(int(self.point_x(1)), 0, self.width(), self.height(), QColor(COLORS["alternate"]))
        if self.segment and self.active:
            first, last = self.segment
            painter.fillRect(int(self.point_x(first)), 9, max(1, int(self.point_x(last)-self.point_x(first))), 20, QColor(COLORS["selection"]))
        painter.setPen(QPen(QColor(COLORS["border"])))
        painter.drawLine(QPointF(12, 19), QPointF(self.width()-12, 19))
        for point in self.points:
            x = self.point_x(point.time)
            enabled = self.enabled_track and (self.editable_times is None or point.time in self.editable_times)
            color = "disabled_text" if not enabled else "accent" if abs(point.time-self.selected) < 1e-6 else "muted"
            painter.setBrush(QColor(COLORS[color]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(QPolygonF([QPointF(x, 11), QPointF(x+6, 19), QPointF(x, 27), QPointF(x-6, 19)]))
        painter.setPen(QPen(QColor(COLORS["accent"]), 1.5))
        painter.drawLine(QPointF(self.point_x(self.progress), 0), QPointF(self.point_x(self.progress), self.height()))
        painter.setPen(QColor(COLORS["panel"]))
        painter.drawLine(0, self.height()-1, self.width(), self.height()-1)

    def hit(self, x):
        return min((p.time for p in self.points if abs(self.point_x(p.time) - x) < 9),
                   key=lambda t: abs(self.point_x(t) - x), default=None)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = self.hit(event.position().x())
        if point is None:
            value = self.time_at(event.position().x())
            self.segment_selected.emit(value)
            self.seek_requested.emit(value)
        else:
            self.dragged = point if self.editable and (self.editable_times is None or point in self.editable_times) else None
            self.point_selected.emit(point)
            if self.dragged is not None:
                self.gesture_started.emit()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.dragged is not None and self.dragged not in (0, 1):
            value = self.time_at(event.position().x())
            if self.editable and 0 < value < 1 and not any(abs(p.time-value) <= 1e-6 and p.time != self.dragged for p in self.points):
                self.point_moved.emit(self.dragged, value)
                self.dragged = value
        elif event.buttons() & Qt.MouseButton.LeftButton and self.dragged is None:
            self.seek_requested.emit(self.time_at(event.position().x()))

    def mouseReleaseEvent(self, event):
        if self.dragged is not None:
            self.dragged = None
            self.gesture_finished.emit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            value = self.time_at(event.position().x())
            if self.editable and 0 <= value <= 1:
                self.add_requested.emit(value)

    def contextMenuEvent(self, event):
        progress = self.time_at(event.pos().x())
        hit = self.hit(event.pos().x())
        menu = QMenu(self)
        add = menu.addAction("여기에 키프레임 추가" if self.korean else "Add keyframe here")
        add.setEnabled(self.editable and 0 <= progress <= 1)
        delete = menu.addAction("키프레임 삭제" if self.korean else "Delete keyframe")
        delete.setEnabled(self.editable and hit is not None and hit not in (0, 1)
            and (self.editable_times is None or hit in self.editable_times))
        chosen = menu.exec(event.globalPos())
        if chosen is add:
            self.add_requested.emit(progress)
        elif chosen is delete:
            self.delete_requested.emit(hit)


class MotionRuler(MotionTimeline):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(24)
        self.editable = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS["panel"]))
        painter.setPen(QColor(COLORS["muted"]))
        right_edge = -100
        for value in sorted({self.minimum, 0, 1, self.maximum}):
            x = self.point_x(value)
            text = f"{value*self.duration:.2f}s"
            width = painter.fontMetrics().horizontalAdvance(text)
            left = int(min(self.width()-width, max(0, x-width/2)))
            if left >= right_edge+8:
                painter.drawText(left, 16, text)
                right_edge = left+width
