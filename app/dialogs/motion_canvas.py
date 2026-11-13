"""An owned project snapshot and a handle overlay for the short-motion editor."""
from copy import deepcopy
from dataclasses import replace
from math import atan2, degrees, hypot

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from app.animation.curves import AnimationPose, SAMPLE_TEMPO, music_reactive_pose, source_loop_pose
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
        center = self.source.width/2
        self.setTransform(QTransform().translate(center, 0).scale(total.scale_x, 1).translate(-center, 0))

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
        video_items, durations = [], {}
        for original in reversed(project_scene.items()):
            if not isinstance(original, SourceItem) or not original.isVisible():
                continue
            model = deepcopy(getattr(original, "_editor_keyframe_base", original.source))
            frame = None
            if model.source_type is SourceType.VIDEO:
                self.video_stills = True
                frame = original.video_preview_frame()
                player = original._video_player
                if player is not None and player.duration() > 0 and original._video_preview_path:
                    durations[original._video_preview_path] = player.duration()/1000
            # Avoid opening poster decoders until the editor selects a clock.
            item = SourceItem(replace(model, source_type=SourceType.IMAGE, content_path="")
                if model.source_type is SourceType.VIDEO else model)
            item.source = model
            if model.source_type is SourceType.VIDEO:
                video_items.append(item)
            if frame is not None and not frame.isNull():
                item._raw_pixmap = item._pixmap = QPixmap.fromImage(frame)
            self.project_scene.addItem(item)
            if model.id == source.id:
                self.target = item
        if self.target is None:
            model = deepcopy(source)
            self.target = SourceItem(replace(model, source_type=SourceType.IMAGE, content_path="")
                if source.source_type is SourceType.VIDEO else model)
            self.target.source = model
            if source.source_type is SourceType.VIDEO:
                video_items.append(self.target)
                self.video_stills = True
            self.project_scene.addItem(self.target)
        self.base = deepcopy(source)
        from app.dialogs.motion_preview_media import MotionPreviewMedia
        self.media = MotionPreviewMedia(video_items, durations, self)
        self.media.changed.connect(self.render_requested)
        self.video_sync = True
        self.playing = False
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

    def render_frame(self, motion, entering, duration, progress, track, track_index, track_total, *, original=False, full_size=False,
                     held_pose=None, track_start=0, bass_level=0, music_levels=None, playlist_tracks=None,
                     clock=None, compiled_plan=None, segment=None, context_source=None, clock_rate=1):
        target = self.target.source
        # Only the private clone changes. SourceStore, history and autosave
        # never receive a draft or a playback event.
        context_source = context_source or self.base
        target.animation_in = "custom" if entering else context_source.animation_in
        target.animation_out = "custom" if not entering else context_source.animation_out
        target.animation_in_duration = duration if entering else context_source.animation_in_duration
        target.animation_out_duration = duration if not entering else context_source.animation_out_duration
        target.loop_motion = "none" if original else self.base.loop_motion
        target.custom_intro_keyframes = motion if entering else context_source.custom_intro_keyframes
        target.custom_outro_keyframes = motion if not entering else context_source.custom_outro_keyframes
        target.keyframes.enabled = False
        local, global_time = clock if clock is not None else self.frame_times(entering, duration, progress, track, track_start)
        # Author the complete effect even if the selected project's exposure
        # is shorter than it. Other sources keep their real visibility windows.
        target.timeline_start = target.timeline_duration = 0
        scale = 1 if full_size else min(2, max(.25, self.transform().m11()*self.devicePixelRatioF(),
            self.viewport().width()*self.devicePixelRatioF()/max(1, self.project_scene.artboard_rect.width())))
        # Use the original exposure window for video scheduling; the target's
        # motion exposure is expanded only to author the complete effect.
        video_window = target.timeline_start, target.timeline_duration
        target.timeline_start, target.timeline_duration = self.base.timeline_start, self.base.timeline_duration
        try:
            self.media.sync(track, local, global_time, track_start, playing=self.playing, enabled=self.video_sync, scale=scale, clock_rate=clock_rate)
        finally:
            target.timeline_start, target.timeline_duration = video_window
        pose = held_pose if not entering and progress < 0 and held_pose is not None else motion.at(progress)
        phase, phase_duration, junction = ("in" if entering else "out") if progress >= 0 else None, None, None
        if compiled_plan is not None and segment is not None:
            from app.preview.frame_state import segment_junction
            from app.renderer.export_timeline import ExportTimelinePlanner
            sources = [item.source for item in self.project_scene.items() if isinstance(item, SourceItem) and item.source.visible]
            intro, outro = ExportTimelinePlanner._animation_durations(track, sources)
            phase = "in" if local < intro else "out" if local >= track.duration_seconds-outro else None
            phase_duration = intro if phase == "in" else outro if phase == "out" else None
            junction = segment_junction(segment, global_time)
        image = CanvasSnapshot.capture_track(self.project_scene, track, track_index + 1, track_total, 0,
            elapsed_seconds=local, timeline_seconds=global_time, animation_phase=phase,
            animation_progress=progress, output_scale=scale, bass_level=bass_level, music_levels=music_levels,
            playlist_tracks=playlist_tracks, compiled_plan=compiled_plan, animation_phase_duration=phase_duration, junction=junction,
            track_tempo=SAMPLE_TEMPO,
            motion_pose_overrides={target.id: AnimationPose() if original else pose})
        if not full_size:
            self.background.setPixmap(QPixmap.fromImage(image))
            self.background.setScale(1 / scale)
            loop = source_loop_pose(target, global_time, local, SAMPLE_TEMPO)
            total = pose.combined(loop)
            if target.source_type is SourceType.BACKGROUND and target.background_bass_reactive:
                bass_scale = 1 + max(0, min(1, bass_level))*target.background_bass_strength
                total = total.combined(AnimationPose(dx=-target.width*(bass_scale-1)/2, dy=-target.height*(bass_scale-1)/2, scale=bass_scale))
            elif target.source_type is SourceType.TEXT and target.uses_bass_reaction:
                level = music_levels.get(target.id, 0) if music_levels is not None else bass_level
                total = total.combined(music_reactive_pose(target.music_reactive_effect, level,
                    target.music_reactive_strength, target.font_size))
            self.handles.set_pose(pose, total)
            self.handles.editable = not original and not self.base.locked
            self.handles.update()
            if self.fit_mode:
                self.fit_canvas()
        return image

    def frame_times(self, entering, duration, progress, track, track_start=0):
        local = progress*duration if entering else max(0, track.duration_seconds-duration)+progress*duration
        global_time = track_start+local
        if self.base.timeline_start > 0 or self.base.timeline_duration > 0:
            global_time = self.base.timeline_start + (progress*duration if entering else
                max(0, (self.base.timeline_duration or track.duration_seconds)-duration)+progress*duration)
            local = global_time-track_start
        return local, global_time

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
        self.media.close()
        for item in self.project_scene.items():
            if isinstance(item, SourceItem):
                item.release_video_decoder()
        self.project_scene.clear()
