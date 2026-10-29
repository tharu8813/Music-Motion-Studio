"""Direct manipulation of the existing playlist and source timing models."""

from dataclasses import dataclass, replace
from math import ceil, floor

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QMenu

from app.models.source import Source
from app.timeline.track_schedule import resolve_track_windows
from app.ui.design_system import COLORS
from app.ui.studio_icons import source_icon
from app.utils.time_format import format_clock
from app.widgets.transition_inspector import _nice_step


@dataclass(frozen=True)
class TimelineBlock:
    kind: str
    identifier: str
    name: str
    start: float
    end: float
    row: int
    full: bool = False
    locked: bool = False
    visible: bool = True
    minimum: float = 0.0


def clock(seconds: float) -> str:
    """Hundredths keep short edits visible without rounding up a whole second."""
    ticks = max(0, round(seconds * 100))
    return f"{format_clock(ticks // 100)}.{ticks % 100:02d}"


class VisualTimeline(QAbstractScrollArea):
    timing_committed = Signal(str, str, float, float)
    selection_changed = Signal(str, str)
    position_changed = Signal(float)
    zoom_changed = Signal(int)
    hint_changed = Signal(str)
    reset_requested = Signal(str, str)
    numeric_edit_requested = Signal(str, str)

    LABEL = 176
    RULER = 32
    ROW = 50
    GRIP = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("visualTimeline")
        self.setFrameShape(self.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.korean = True
        self.snapping = True
        self.playhead = 0.0
        self.selected: tuple[str, str] | None = None
        self.tracks = []
        self.sources: list[Source] = []
        self.blocks: list[TimelineBlock] = []
        self.duration = 60.0
        self.pixels_per_second = 1.0
        self._fit_mode = True
        self._drag = None
        self._draft = None
        self._pan = None
        self._last_point = QPointF()
        self._snap_guide = None
        self._auto_scroll = QTimer(self)
        self._auto_scroll.setInterval(40)
        self._auto_scroll.timeout.connect(self._scroll_drag)
        self.horizontalScrollBar().valueChanged.connect(self.viewport().update)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)

    def set_content(self, timeline_tracks, sources) -> None:
        self.cancel_drag()
        self.tracks = list(timeline_tracks)
        self.sources = list(reversed(sources))
        self.blocks = self._build_blocks()
        ends = [block.end for block in self.blocks]
        self.duration = max(10.0, max(ends, default=60.0))
        if self.selected and not any((b.kind, b.identifier) == self.selected for b in self.blocks):
            self.selected = None
        self._update_scrollbars()
        self.set_playhead(min(self.playhead, self.duration))
        self.viewport().update()

    def _build_blocks(self) -> list[TimelineBlock]:
        tracks = self.tracks
        if self._draft and self._draft[0].kind == "track":
            target, start, _duration = self._draft
            copies = [replace(track, start_time_seconds=start) if track.id == target.identifier else track
                      for track, _start, _end in tracks]
            tracks = [(window.track, window.start, window.end) for window in resolve_track_windows(copies)]
        total = max((end for _track, _start, end in tracks), default=60.0)
        result = [TimelineBlock("track", track.id, track.title, start, end, 0,
                                minimum=tracks[index - 1][2] if index else 0.0)
                  for index, (track, start, end) in enumerate(tracks)]
        for row, source in enumerate(self.sources, 1):
            start, duration = source.timeline_start, source.timeline_duration
            if self._draft and self._draft[0].identifier == source.id:
                _target, start, duration = self._draft
            end = start + duration if duration > 0 else max(start + 0.1, total)
            result.append(TimelineBlock("source", source.id, source.name, start, end, row,
                                        duration == 0, source.locked, source.visible))
        return result

    def set_selected(self, kind: str, identifier: str) -> None:
        self.selected = (kind, identifier)
        block = next((b for b in self.blocks if (b.kind, b.identifier) == self.selected), None)
        if block is not None:
            top = block.row * self.ROW
            scroll = self.verticalScrollBar()
            visible = self.viewport().height() - self.RULER
            if top < scroll.value():
                scroll.setValue(top)
            elif top + self.ROW > scroll.value() + visible:
                scroll.setValue(top + self.ROW - visible)
        self.viewport().update()

    def set_playhead(self, seconds: float) -> None:
        self.playhead = max(0.0, min(self.duration, seconds))
        self.position_changed.emit(self.playhead)
        self.viewport().update()

    def _fit_scale(self) -> float:
        return max(0.001, (self.viewport().width() - self.LABEL - 24) / max(10, self.duration))

    def fit_all(self) -> None:
        self._fit_mode = True
        self.horizontalScrollBar().setValue(0)
        self._update_scrollbars()

    def zoom(self, factor: float, anchor_x: float | None = None) -> None:
        anchor_x = self.LABEL + (self.viewport().width() - self.LABEL) / 2 if anchor_x is None else anchor_x
        anchor_time = self.time_at(anchor_x)
        self._fit_mode = False
        self.pixels_per_second = max(self._fit_scale(), min(200.0, self.pixels_per_second * factor))
        self._update_scrollbars()
        self.horizontalScrollBar().setValue(round(anchor_time * self.pixels_per_second - anchor_x + self.LABEL))

    def time_at(self, x: float) -> float:
        return max(0.0, (x - self.LABEL + self.horizontalScrollBar().value()) / self.pixels_per_second)

    def x_at(self, seconds: float) -> float:
        return self.LABEL + seconds * self.pixels_per_second - self.horizontalScrollBar().value()

    def block_rect(self, block: TimelineBlock) -> QRectF:
        return QRectF(self.x_at(block.start), self.RULER + block.row * self.ROW + 6
                      - self.verticalScrollBar().value(), max(6, (block.end - block.start) * self.pixels_per_second),
                      self.ROW - 12)

    def _update_scrollbars(self) -> None:
        if self._fit_mode:
            self.pixels_per_second = self._fit_scale()
        width = max(1, self.viewport().width() - self.LABEL)
        extent = max(self.duration, max((block.end for block in self._build_blocks()), default=0) if self._draft else 0)
        self.horizontalScrollBar().setRange(0, max(0, ceil((extent + max(5, extent * .05))
                                                         * self.pixels_per_second) - width))
        self.horizontalScrollBar().setPageStep(width)
        self.verticalScrollBar().setRange(0, max(0, (len(self.sources) + 1) * self.ROW
                                                    - self.viewport().height() + self.RULER))
        self.verticalScrollBar().setPageStep(max(1, self.viewport().height() - self.RULER))
        self.zoom_changed.emit(max(1, round(100 * self.pixels_per_second / self._fit_scale())))
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_scrollbars()

    def scrollContentsBy(self, _dx, _dy) -> None:
        # Lane labels and ruler remain pinned while clip coordinates scroll.
        self.viewport().update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.viewport().width(), self.viewport().height()
        painter.fillRect(self.viewport().rect(), QColor(COLORS["field"]))
        blocks = self._build_blocks() if self._draft else self.blocks
        row_count = len(self.sources) + 1
        for row in range(row_count):
            y = self.RULER + row * self.ROW - self.verticalScrollBar().value()
            if y + self.ROW <= self.RULER or y >= height:
                continue
            painter.fillRect(QRectF(self.LABEL, max(self.RULER, y), width - self.LABEL, self.ROW),
                             QColor(COLORS["field"] if row % 2 else COLORS["window"]))
            painter.setPen(QColor(COLORS["border"]))
            painter.drawLine(QPointF(self.LABEL, y + self.ROW), QPointF(width, y + self.ROW))
        start, end = self.time_at(self.LABEL), self.time_at(width)
        step = _nice_step(end - start, width - self.LABEL)
        painter.save()
        painter.setClipRect(QRectF(self.LABEL, self.RULER, width - self.LABEL, height - self.RULER))
        grid = floor(start / step) * step
        while grid <= end:
            painter.setPen(QColor(COLORS["border"]))
            painter.drawLine(QPointF(self.x_at(grid), self.RULER), QPointF(self.x_at(grid), height))
            grid += step
        for block in blocks:
            rect = self.block_rect(block)
            if not rect.intersects(QRectF(self.LABEL, self.RULER, width - self.LABEL, height)):
                continue
            selected = self.selected == (block.kind, block.identifier)
            fill = QColor(COLORS["selection"] if block.kind == "track" else COLORS["alternate"])
            if not block.visible:
                fill = QColor(COLORS["disabled"])
            painter.setBrush(fill)
            pen = QPen(QColor(COLORS["accent"] if selected else COLORS["selection_border"] if block.kind == "track" else COLORS["border"]),
                       2 if selected else 1)
            if block.full:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
            painter.save()
            painter.setClipRect(rect.adjusted(9, 2, -9, -2), Qt.ClipOperation.IntersectClip)
            text_rect = rect.adjusted(10, 0, -8, 0)
            text_rect.setLeft(max(text_rect.left(), self.LABEL + 10))
            text_rect.setRight(min(text_rect.right(), width - 10))
            painter.setPen(QColor(COLORS["text"] if block.visible else COLORS["disabled_text"]))
            font = painter.font(); font.setBold(selected); painter.setFont(font)
            name = painter.fontMetrics().elidedText(block.name, Qt.TextElideMode.ElideRight, max(0, int(text_rect.width())))
            painter.drawText(text_rect.adjusted(0, 2, 0, -15), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
            font.setBold(False); font.setPointSizeF(max(7, font.pointSizeF() - 1)); painter.setFont(font)
            painter.setPen(QColor(COLORS["muted"]))
            detail = ("곡 끝까지" if self.korean else "To playlist end") if block.full else clock(block.end - block.start)
            painter.drawText(text_rect.adjusted(0, 20, 0, -1), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, detail)
            painter.restore()
            if selected and block.kind == "source" and not block.locked:
                painter.setPen(QPen(QColor(COLORS["accent"]), 2))
                for x in (rect.left() + 5, rect.right() - 5):
                    painter.drawLine(QPointF(x, rect.center().y() - 6), QPointF(x, rect.center().y() + 6))
        if self._snap_guide is not None:
            painter.setPen(QPen(QColor(COLORS["text"]), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(self.x_at(self._snap_guide), self.RULER), QPointF(self.x_at(self._snap_guide), height))
        painter.setPen(QPen(QColor(COLORS["warning"]), 1.5))
        painter.drawLine(QPointF(self.x_at(self.playhead), self.RULER), QPointF(self.x_at(self.playhead), height))
        painter.restore()
        # Fixed lane headers cover clips that have scrolled past the left edge.
        painter.fillRect(QRectF(0, self.RULER, self.LABEL, height - self.RULER), QColor(COLORS["panel"]))
        for row in range(row_count):
            y = self.RULER + row * self.ROW - self.verticalScrollBar().value()
            if y < self.RULER or y >= height:
                continue
            lane = QRectF(12, y, self.LABEL - 24, self.ROW)
            painter.setPen(QColor(COLORS["text"]))
            if row == 0:
                label = "음악" if self.korean else "Music"
            else:
                source = self.sources[row - 1]
                icon = source_icon(source.source_type.value)
                painter.drawPixmap(round(lane.left()), round(y + 16), icon.pixmap(18, 18))
                lane.adjust(26, 0, 0, 0)
                label = source.name
                if source.locked:
                    label += " · 잠금" if self.korean else " · Locked"
                elif not source.visible:
                    label += " · 숨김" if self.korean else " · Hidden"
                    painter.setPen(QColor(COLORS["disabled_text"]))
            painter.drawText(lane, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, round(lane.width())))
        painter.fillRect(QRectF(0, 0, width, self.RULER), QColor(COLORS["panel"]))
        painter.setPen(QColor(COLORS["muted"]))
        painter.drawText(QRectF(12, 0, self.LABEL - 24, self.RULER), Qt.AlignmentFlag.AlignVCenter,
                         "시간 / 레이어" if self.korean else "Time / layers")
        painter.save(); painter.setClipRect(QRectF(self.LABEL, 0, width - self.LABEL, self.RULER))
        tick = floor(start / step) * step
        while tick <= end:
            x = self.x_at(tick)
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QRectF(x + 5, 0, 82, self.RULER - 3), Qt.AlignmentFlag.AlignVCenter,
                             clock(tick) if step < 1 else format_clock(round(tick)))
            painter.drawLine(QPointF(x, self.RULER - 6), QPointF(x, self.RULER))
            tick += step
        x = self.x_at(self.playhead)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(COLORS["warning"]))
        painter.drawPolygon(QPolygonF([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x + 5, 10),
                                       QPointF(x, 15), QPointF(x - 5, 10)]))
        painter.restore()
        if not self.blocks:
            painter.setPen(QColor(COLORS["muted"]))
            painter.drawText(QRectF(self.LABEL + 16, self.RULER + 12, max(1, width - self.LABEL - 32),
                                   max(40, height - self.RULER - 24)),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             "음악이나 요소를 추가하면 시간 범위를 여기서 편집할 수 있습니다."
                             if self.korean else "Add music or sources to edit their timing here.")

    def _hit(self, point: QPointF):
        if point.x() < self.LABEL or point.y() < self.RULER:
            return None
        for block in reversed(self.blocks):
            rect = self.block_rect(block)
            if rect.contains(point):
                mode = "move"
                if block.kind == "source" and not block.locked:
                    if abs(point.x() - rect.left()) <= self.GRIP:
                        mode = "start"
                    elif abs(point.x() - rect.right()) <= self.GRIP:
                        mode = "end"
                return block, mode
        return None

    def mousePressEvent(self, event) -> None:
        point = event.position()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan = (point.x(), self.horizontalScrollBar().value())
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        hit = self._hit(point)
        if hit:
            block, mode = hit
            self.set_selected(block.kind, block.identifier)
            self.selection_changed.emit(block.kind, block.identifier)
            self._show_hint(block)
            if block.locked:
                return
            self._fit_mode = False
            self._drag = (block, mode, point.x(), self.horizontalScrollBar().value())
            self._last_point = point
            self._auto_scroll.start()
        elif point.x() >= self.LABEL:
            self.set_playhead(self.time_at(point.x()))
            self._pan = ("seek", 0)

    def _snap(self, value: float, block: TimelineBlock, invert: bool, offset: float = 0) -> float:
        self._snap_guide = None
        if self.snapping == invert:
            return value
        step = _nice_step(self.time_at(self.viewport().width()) - self.time_at(self.LABEL),
                          self.viewport().width() - self.LABEL) / 5
        candidates = [0.0, self.playhead, round(value / step) * step]
        candidates.extend(edge for other in self.blocks if other.identifier != block.identifier
                          for edge in (other.start, other.end))
        choices = [(abs(point - value), point, point) for point in candidates]
        if offset:
            choices.extend((abs(point - value - offset), point - offset, point) for point in candidates)
        distance, snapped, guide = min(choices)
        if distance * self.pixels_per_second <= 8:
            self._snap_guide = guide
            return snapped
        return value

    def _update_draft(self, point: QPointF, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
        block, mode, press_x, press_scroll = self._drag
        delta = (point.x() - press_x + self.horizontalScrollBar().value() - press_scroll) / self.pixels_per_second
        start, end = block.start, block.end
        invert = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if mode == "move":
            proposed = self._snap(start + delta, block, invert, end - start if not block.full else 0)
            start = max(block.minimum, proposed)
            if start != proposed:
                self._snap_guide = block.minimum if self.snapping != invert else None
            duration = 0.0 if block.full else end - block.start
        elif mode == "start":
            start = max(0, min(end - .1, self._snap(start + delta, block, invert)))
            duration = end - start
        else:
            end = max(start + .1, self._snap(end + delta, block, invert))
            duration = end - start
        self._draft = (block, round(start, 2), round(duration, 2))
        self._update_scrollbars()
        self._show_hint(replace(block, start=start, end=start + duration if duration else block.end))

    def mouseMoveEvent(self, event) -> None:
        point = event.position(); self._last_point = point
        if self._drag:
            self._update_draft(point, event.modifiers())
            return
        if self._pan:
            if self._pan[0] == "seek":
                self.set_playhead(self.time_at(point.x()))
            else:
                self.horizontalScrollBar().setValue(round(self._pan[1] - point.x() + self._pan[0]))
            return
        hit = self._hit(point)
        cursor = Qt.CursorShape.ArrowCursor
        if hit:
            block, mode = hit
            cursor = Qt.CursorShape.ForbiddenCursor if block.locked else (
                Qt.CursorShape.SizeHorCursor if mode != "move" else Qt.CursorShape.OpenHandCursor)
            self._show_hint(block)
        elif point.x() >= self.LABEL:
            cursor = Qt.CursorShape.CrossCursor
        self.viewport().setCursor(cursor)

    def _scroll_drag(self) -> None:
        if not self._drag:
            return
        x = self._last_point.x()
        direction = -1 if x < self.LABEL + 18 else 1 if x > self.viewport().width() - 18 else 0
        if direction:
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + direction * 18)
            self._update_draft(self._last_point, QApplication.keyboardModifiers())

    def mouseReleaseEvent(self, event) -> None:
        draft = self._draft
        self.cancel_drag()
        self._pan = None
        if draft:
            block, start, duration = draft
            old_duration = 0 if block.full else block.end - block.start
            if (start, duration) != (round(block.start, 2), round(old_duration, 2)):
                self.timing_committed.emit(block.kind, block.identifier, start, duration)
        self.viewport().unsetCursor()

    def cancel_drag(self) -> None:
        self._auto_scroll.stop()
        self._drag = self._draft = None
        self._snap_guide = None
        self.viewport().update()

    def _show_hint(self, block: TimelineBlock) -> None:
        end = "곡 끝까지" if self.korean and block.full else "Playlist end" if block.full else clock(block.end)
        hint = f"{block.name}  ·  {clock(block.start)} → {end}"
        if block.locked:
            hint += "  ·  잠금 해제 후 편집" if self.korean else "  ·  Unlock to edit"
        elif block.kind == "track":
            hint += "  ·  시작 위치 조절 · 원본 길이 유지" if self.korean else "  ·  Move start · Original length retained"
        self.hint_changed.emit(hint)
        self.viewport().setToolTip(hint)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.25 if event.angleDelta().y() > 0 else .8, max(self.LABEL, event.position().x()))
            event.accept()
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier or event.angleDelta().x():
            delta = event.angleDelta().x() or event.angleDelta().y()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta)
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.cancel_drag(); self._pan = None
        elif key in {Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus}:
            self.zoom(.8 if key == Qt.Key.Key_Minus else 1.25)
        elif key == Qt.Key.Key_Home:
            self.set_playhead(0)
        elif key in {Qt.Key.Key_Up, Qt.Key.Key_Down} and self.blocks:
            index = next((i for i, b in enumerate(self.blocks) if (b.kind, b.identifier) == self.selected), -1)
            block = self.blocks[max(0, min(len(self.blocks) - 1, index + (-1 if key == Qt.Key.Key_Up else 1)))]
            self.set_selected(block.kind, block.identifier)
            self.selection_changed.emit(block.kind, block.identifier)
            self._show_hint(block)
        elif key in {Qt.Key.Key_Left, Qt.Key.Key_Right}:
            delta = (-1 if key == Qt.Key.Key_Left else 1) * (.1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
            block = next((b for b in self.blocks if (b.kind, b.identifier) == self.selected), None)
            if block and not block.locked:
                self.timing_committed.emit(block.kind, block.identifier, round(max(block.minimum, block.start + delta), 2),
                                           0 if block.full else round(block.end - block.start, 2))
            else:
                self.set_playhead(self.playhead + delta)
        else:
            return super().keyPressEvent(event)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        hit = self._hit(QPointF(event.pos()))
        if not hit:
            return
        block, _mode = hit
        self.set_selected(block.kind, block.identifier)
        self.selection_changed.emit(block.kind, block.identifier)
        menu = QMenu(self)
        numeric = menu.addAction("숫자로 편집…" if self.korean else "Edit numerically…")
        reset = menu.addAction(("곡 끝까지 표시" if self.korean else "Show to playlist end") if block.kind == "source"
                               else ("자동 이어붙이기" if self.korean else "Sequence automatically"))
        reset.setEnabled(not block.locked)
        chosen = menu.exec(event.globalPos())
        if chosen is numeric:
            self.numeric_edit_requested.emit(block.kind, block.identifier)
        elif chosen is reset:
            self.reset_requested.emit(block.kind, block.identifier)
