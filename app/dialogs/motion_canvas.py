"""An owned project snapshot and a handle overlay for the short-motion editor."""
from copy import deepcopy
from dataclasses import replace
from math import atan2, degrees, hypot

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from app.animation.curves import loop_pose
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.source import SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.ui.design_system import COLORS


class MotionHandleItem(SourceItem):
    pose_changed = Signal(dict)
    gesture_started = Signal()
    gesture_finished = Signal()

    def __init__(self, source):
        # Handles own a harmless geometry-only source. They never borrow a main
        # project GraphicsItem or start a multimedia decoder.
        super().__init__(replace(deepcopy(source), source_type=SourceType.SHAPE, content_path=""))
        self._suppress_position_sync = True
        self.setZValue(1)
        self.setSelected(True)
        self.mode = None
        self.editable = True
        self.pose = None
        self.enabled_fields = {"dx", "dy", "scale", "rotation", "opacity"}

    def paint(self, painter, option, widget=None):
        if not self.editable:
            return
        painter.setOpacity(1)
        pen = QPen(QColor("#B7D0C4"), 1.5)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.content_rect())
        painter.setBrush(QColor("#182321"))
        if "scale" in self.enabled_fields:
            for rect in self.resize_handle_rects().values():
                painter.drawRect(rect)
        if "rotation" in self.enabled_fields:
            painter.drawLine(QPointF(self.source.width / 2, 0), self.rotation_handle_rect().center())
            painter.drawEllipse(self.rotation_handle_rect())

    def set_pose(self, pose, total):
        self.pose = pose
        self.setPos(self.source.x + total.dx, self.source.y + total.dy)
        self.setScale(self.source.scale * total.scale)
        self.setRotation(self.source.rotation + total.rotation)

    def mousePressEvent(self, event):
        if not self.editable or event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        handle = self.edit_handle_at(event.pos())
        self.mode = "rotate" if handle == "rotate" else "scale" if handle else "move"
        if ((self.mode == "rotate" and "rotation" not in self.enabled_fields)
                or (self.mode == "scale" and "scale" not in self.enabled_fields)
                or (self.mode == "move" and not self.enabled_fields.intersection({"dx", "dy"}))):
            self.mode = None
            event.ignore()
            return
        self._press = event.scenePos()
        self._initial = self.pose
        self._center = self.mapToScene(self.content_rect().center())
        self._radius = max(1, hypot(self._press.x() - self._center.x(), self._press.y() - self._center.y()))
        self._angle = atan2(self._press.y() - self._center.y(), self._press.x() - self._center.x())
        self.gesture_started.emit()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.mode is None:
            return
        position, initial = event.scenePos(), self._initial
        if self.mode == "move":
            delta = position - self._press
            changes = dict(dx=initial.dx + delta.x(), dy=initial.dy + delta.y())
        elif self.mode == "scale":
            radius = hypot(position.x() - self._center.x(), position.y() - self._center.y())
            changes = dict(scale=min(10, max(.01, initial.scale * radius / self._radius)))
        else:
            angle = atan2(position.y() - self._center.y(), position.x() - self._center.x())
            delta = (degrees(angle - self._angle) + 180) % 360 - 180
            changes = dict(rotation=initial.rotation + delta)
        self.pose_changed.emit(changes)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.mode is not None:
            self.mode = None
            self.gesture_finished.emit()
            event.accept()
        else:
            event.ignore()


class MotionCanvas(QGraphicsView):
    """Rasterize an isolated project Scene through the production Canvas path."""
    pose_changed = Signal(dict)
    gesture_started = Signal()
    gesture_finished = Signal()
    render_requested = Signal()

    def __init__(self, project_scene, source, korean=True, parent=None):
        display = QGraphicsScene()
        super().__init__(display, parent)
        display.setParent(self)
        self.project_scene = CanvasScene(self)
        self.project_scene.set_artboard_size(project_scene.artboard_rect.width(), project_scene.artboard_rect.height())
        self.project_scene.artboard_rect = QRectF(project_scene.artboard_rect)
        self.project_scene.placeholder_labels_korean = korean
        for name in ("artboard_color", "artboard_border_color", "workspace_color"):
            if hasattr(project_scene, name):
                setattr(self.project_scene, name, QColor(getattr(project_scene, name)))
        self.target = None
        self.video_stills = False
        for original in reversed(project_scene.items()):
            if not isinstance(original, SourceItem):
                continue
            model = deepcopy(getattr(original, "_editor_keyframe_base", original.source))
            frame = None
            if model.source_type is SourceType.VIDEO:
                self.video_stills = True
                frame = original.video_preview_frame()
                model = replace(model, source_type=SourceType.IMAGE, content_path="")
            item = SourceItem(model)
            if frame is not None and not frame.isNull():
                item._raw_pixmap = item._pixmap = QPixmap.fromImage(frame)
            self.project_scene.addItem(item)
            if model.id == source.id:
                self.target = item
        if self.target is None:
            self.target = SourceItem(deepcopy(source))
            self.project_scene.addItem(self.target)
        self.base = deepcopy(source)
        self.background = display.addPixmap(QPixmap())
        self.handles = MotionHandleItem(source)
        display.addItem(self.handles)
        self.handles.pose_changed.connect(self.pose_changed)
        self.handles.gesture_started.connect(self.gesture_started)
        self.handles.gesture_finished.connect(self.gesture_finished)
        display.setSceneRect(self.project_scene.artboard_rect)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setMinimumSize(360, 240)
        self.setBackgroundBrush(QColor(COLORS["stage"]))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.fit_mode = True
        self.guides = False
        self._space_pan = False
        self.space_pan_used = False
        self._panning = False
        self._pan_button = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setToolTip("휠: 확대/축소 · 빈 곳/가운데 버튼 드래그 또는 Space+드래그: 화면 이동" if korean else
                        "Wheel: zoom · Drag empty space, middle-drag or Space+drag: pan")
        self.setAccessibleName("프로젝트 Canvas 미리보기 / Project Canvas preview")

    def render_frame(self, motion, entering, duration, progress, track, track_index, track_total, *, original=False, full_size=False):
        target = self.target.source
        # Only the private clone changes. SourceStore, history and autosave
        # never receive a draft or a playback event.
        target.animation_in = "custom" if entering and not original else "none"
        target.animation_out = "custom" if not entering and not original else "none"
        target.animation_in_duration = target.animation_out_duration = duration
        target.loop_motion = "none" if original else self.base.loop_motion
        target.custom_intro_keyframes = motion if entering else target.custom_intro_keyframes
        target.custom_outro_keyframes = motion if not entering else target.custom_outro_keyframes
        target.keyframes.enabled = False
        local = progress * duration if entering else max(0, track.duration_seconds - duration) + progress * duration
        global_time = local
        if self.base.timeline_start > 0 or self.base.timeline_duration > 0:
            global_time = self.base.timeline_start + (progress * duration if entering else
                max(0, (self.base.timeline_duration or track.duration_seconds) - duration) + progress * duration)
        # Author the complete effect even if the selected project's exposure
        # is shorter than it. Other sources keep their real visibility windows.
        target.timeline_start = target.timeline_duration = 0
        scale = 1 if full_size else min(2, max(.25, self.transform().m11()*self.devicePixelRatioF(),
            self.viewport().width()*self.devicePixelRatioF()/max(1, self.project_scene.artboard_rect.width())))
        image = CanvasSnapshot.capture_track(self.project_scene, track, track_index + 1, track_total, 0,
            elapsed_seconds=local, timeline_seconds=global_time, animation_phase="in" if entering else "out",
            animation_progress=progress, output_scale=scale)
        if not full_size:
            self.background.setPixmap(QPixmap.fromImage(image))
            self.background.setScale(1 / scale)
            pose = motion.at(progress)
            loop = loop_pose(target.loop_motion, global_time, target.loop_motion_period,
                             target.loop_motion_amount, target.width, target.height)
            self.handles.set_pose(pose, pose.combined(loop))
            self.handles.editable = not original and not self.base.locked
            self.handles.update()
            if self.fit_mode:
                self.fit_canvas()
        return image

    def fit_canvas(self):
        self.fit_mode = True
        board = self.project_scene.artboard_rect
        self.setSceneRect(board)
        self.fitInView(board.adjusted(-16, -20, 16, 20), Qt.AspectRatioMode.KeepAspectRatio)
        self.centerOn(board.center())

    def manual_view(self):
        center = self.mapToScene(self.viewport().rect().center())
        self.fit_mode = False
        # View-only bounds allow panning even when the artboard fits. The
        # production Scene/artboard coordinates and image remain untouched.
        board = self.project_scene.artboard_rect
        self.setSceneRect(board.adjusted(-100*board.width(), -100*board.height(), 100*board.width(), 100*board.height()))
        self.centerOn(center)

    def zoom(self, factor=None, anchor=None):
        if anchor is None:
            anchor = self.viewport().rect().center()
        scene_anchor = self.mapToScene(anchor)
        self.manual_view()
        current = self.transform().m11()
        target = 1 if factor is None else min(8, max(.05, current*factor))
        self.scale(target/current, target/current)
        offset = scene_anchor-self.mapToScene(anchor)
        self.centerOn(self.mapToScene(self.viewport().rect().center())+offset)
        self.render_requested.emit()

    def wheelEvent(self, event):
        if self.handles.mode is not None:
            event.accept()
            return
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            self.zoom(1.2 ** max(-10, min(10, delta/120)), event.position().toPoint())
            event.accept()
        else:
            event.ignore()

    def set_space_pan(self, enabled):
        if enabled and not self._space_pan:
            self.space_pan_used = False
        self._space_pan = enabled
        self.update_pan_cursor()

    def update_pan_cursor(self):
        if self._panning or self._space_pan:
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor if self._panning else Qt.CursorShape.OpenHandCursor)
        else:
            self.viewport().unsetCursor()

    def cancel_navigation(self):
        self._panning = self._space_pan = False
        self._pan_button = None
        self.update_pan_cursor()

    def mousePressEvent(self, event):
        empty = self.itemAt(event.position().toPoint()) is not self.handles or not self.handles.editable
        pan = event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton and (self._space_pan or empty))
        if pan and self.handles.mode is None:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.manual_view()
            self._panning = True
            self._pan_button = event.button()
            self._pan_origin = event.position().toPoint()
            self.space_pan_used = self.space_pan_used or self._space_pan
            self.update_pan_cursor()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            position = event.position().toPoint()
            delta = position-self._pan_origin
            self._pan_origin = position
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value()-delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._panning and event.button() == self._pan_button:
            self._panning = False
            self._pan_button = None
            self.update_pan_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit_mode:
            self.fit_canvas()
        self.render_requested.emit()

    def drawForeground(self, painter, rect):
        if not self.guides:
            return
        board = self.project_scene.artboard_rect
        pen = QPen(QColor("#5B756D"), 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(QPointF(board.center().x(), board.top()), QPointF(board.center().x(), board.bottom()))
        painter.drawLine(QPointF(board.left(), board.center().y()), QPointF(board.right(), board.center().y()))
        painter.drawRect(board.adjusted(board.width() * .05, board.height() * .05, -board.width() * .05, -board.height() * .05))

    def close_resources(self):
        self.cancel_navigation()
        for item in self.project_scene.items():
            if isinstance(item, SourceItem):
                item.suspend_video_preview()
        self.project_scene.clear()
