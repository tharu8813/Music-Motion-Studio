"""Preview-backed grid popup for visual properties, preserving QComboBox data/signals.

The collapsed control is still a regular QComboBox. Only its popup changes:
a keyboard-accessible, scrollable grid with illustrative or renderer-derived
previews. Previews never mutate project state or start background playback.
"""

from __future__ import annotations

from math import cos, pi, sin
from time import monotonic

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QListView,
    QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from app.animation.curves import animation_pose, loop_pose, music_reactive_pose
from app.animation.lyrics import intro_exit_pose
from app.ui.design_system import COLORS
from app.utils.mask_shapes import mask_path


_MOTION_KINDS = frozenset({"entrance", "exit", "loop", "reactive", "lyric", "intro", "intro_exit"})
_TILE_SIZE = QSize(138, 116)
_ICON_SIZE = QSize(112, 72)


def _paint_motion(painter: QPainter, rect: QRectF, pose, color: QColor) -> None:
    painter.save()
    painter.setOpacity(max(0.0, min(1.0, pose.opacity)))
    painter.translate(rect.center())
    painter.translate(pose.dx * 0.75, pose.dy * 0.75)
    painter.rotate(pose.rotation)
    painter.scale(pose.scale * pose.scale_x, pose.scale)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(-23, -14, 46, 28), 6, 6)
    painter.setBrush(QColor("#25312D"))
    painter.drawRoundedRect(QRectF(-15, -3, 30, 6), 3, 3)
    painter.restore()


def _preview_icon(kind: str, value: object, seconds: float) -> QIcon:
    """Draw at native pixel size; QIcon is recreated only while popup is visible."""
    pix = QPixmap(_ICON_SIZE)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = _ICON_SIZE.width(), _ICON_SIZE.height()
    stage = QRectF(3, 2, w - 6, h - 4)
    p.setPen(QPen(QColor(COLORS["border"]), 1))
    p.setBrush(QColor(COLORS["stage"]))
    p.drawRoundedRect(stage, 7, 7)
    accent = QColor(COLORS["accent"])
    muted = QColor(COLORS["muted"])
    key = str(value)
    t = (seconds % 1.6) / 1.6

    if kind in {"entrance", "exit", "loop", "reactive", "lyric"}:
        if key == "custom":
            p.setPen(QPen(accent, 2))
            p.drawLine(22, 47, 45, 24)
            p.drawLine(45, 24, 82, 42)
            p.setBrush(accent)
            for x, y in ((22, 47), (45, 24), (82, 42)):
                p.drawEllipse(QRectF(x - 3, y - 3, 6, 6))
        else:
            sample = "slide_up" if key in {"slide", "cascade"} else (
                "fade" if key in {"crossfade", "glow"} else key
            )
            if kind == "loop":
                pose = loop_pose(key, seconds, 1.6, 1.0, 180, 100)
            elif kind == "reactive":
                pose = music_reactive_pose(key, (sin(seconds * pi * 2) + 1) / 2, 0.7, 34)
            else:
                pose = animation_pose(sample, t, kind != "exit", 180, 100)
            _paint_motion(p, QRectF(33, 19, 46, 28), pose, accent)
            if kind == "lyric" and key == "glow":
                p.setPen(QPen(QColor(COLORS["accent_hover"]), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(QRectF(31, 17, 50, 32), 7, 7)
    elif kind in {"mask", "shape"}:
        sample = QRectF(30, 12, 52, 46)
        path = mask_path(sample, key)
        p.setPen(QPen(QColor(COLORS["accent_hover"]), 1))
        p.setBrush(accent)
        p.drawPath(path)
    elif kind in {"progress", "knob"}:
        track = QRectF(13, 30, 86, 10)
        if kind == "progress" and key == "youtube":
            track = QRectF(13, 33, 86, 5)
        elif kind == "progress" and key == "apple":
            track = QRectF(13, 32, 86, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COLORS["border"]))
        p.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        fill = QRectF(track.x(), track.y(), track.width() * .69, track.height())
        brush = QLinearGradient(fill.topLeft(), fill.topRight())
        brush.setColorAt(0.0, QColor("#8ACFA7") if key == "spotify" else accent)
        brush.setColorAt(1.0, QColor("#6D9EBF") if key == "gradient" else (
            QColor("#E4E9E7") if key == "apple" else accent
        ))
        p.setBrush(brush)
        p.drawRoundedRect(fill, fill.height() / 2, fill.height() / 2)
        if kind == "knob" and key in {"circle", "bar", "auto"}:
            p.setBrush(QColor(COLORS["text"]))
            if key == "bar":
                p.drawRoundedRect(QRectF(70, 25, 4, 20), 2, 2)
            else:
                p.drawEllipse(QRectF(69, 27, 16, 16))
        elif kind == "progress" and key in {"spotify", "apple"}:
            p.setBrush(QColor("#F3F5F4"))
            p.drawEllipse(QRectF(68, 27, 16, 16))
    elif kind == "album_frame":
        box = QRectF(34, 11, 44, 48)
        if key == "polaroid":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#E7E7E3"))
            p.drawRect(QRectF(30, 7, 52, 57))
            box = QRectF(34, 11, 44, 40)
        p.setPen(QPen(accent, 2 if key == "glass" else 1))
        p.setBrush(QColor("#688C87"))
        if key == "circle":
            p.drawEllipse(QRectF(34, 13, 44, 44))
        else:
            p.drawRoundedRect(box, 9 if key in {"rounded", "glass"} else 1,
                              9 if key in {"rounded", "glass"} else 1)
        p.setPen(QPen(QColor("#CFE4DB"), 2))
        p.drawLine(44, 35, 54, 26)
        p.drawLine(54, 26, 68, 37)
    elif kind in {"visualizer", "waveform", "meter"}:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainterPath

        p.setPen(QPen(accent, 2))
        heights = [12 + (0.5 + 0.5 * sin(i * .75 + seconds * 4)) * 30 for i in range(13)]
        points = [QPointF(11 + i * 7.5, 51 - y) for i, y in enumerate(heights)]
        if kind == "waveform" or key in {"wave", "line"}:
            path = QPainterPath(points[0])
            for point in points[1:]:
                path.lineTo(point)
            if key in {"filled", "mirror"}:
                path.lineTo(points[-1].x(), 53)
                path.lineTo(points[0].x(), 53)
                path.closeSubpath()
                color = QColor(accent)
                color.setAlpha(120)
                p.fillPath(path, color)
            p.setPen(QPen(accent, 2))
            for start, end in zip(points, points[1:]):
                p.drawLine(start, end)
                if key == "mirror":
                    p.drawLine(QPointF(start.x(), 72 - start.y()),
                               QPointF(end.x(), 72 - end.y()))
        elif key in {"radial", "arc"}:
            for i in range(14):
                angle = i * 2 * pi / 14
                length = heights[i % len(heights)] * .45
                p.drawLine(
                    int(56 + 16 * cos(angle)), int(35 + 16 * sin(angle)),
                    int(56 + (16 + length) * cos(angle)),
                    int(35 + (16 + length) * sin(angle)),
                )
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            for i, y in enumerate(heights):
                height = max(5, y)
                if key == "dots":
                    for d in range(0, int(height), 7):
                        p.drawEllipse(QRectF(9 + 7 * i, 52 - d, 4, 4))
                elif key in {"led", "segments"}:
                    for d in range(0, int(height), 7):
                        p.drawRoundedRect(QRectF(10 + i * 7, 52 - d, 4, 5), 1, 1)
                else:
                    p.drawRoundedRect(QRectF(10 + i * 7, 53 - height, 4, height), 2, 2)
    elif kind == "particle":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        for i in range(15):
            x = 12 + ((i * 31 + int(seconds * (6 if key != "snow" else 2))) % 90)
            y = 10 + ((i * 19 + int(seconds * 8)) % 50)
            radius = 1.5 if key in {"dust", "noise", "snow", "stars"} else 3.5
            p.drawEllipse(QRectF(x, y, radius * 2, radius * 2))
    elif kind == "intro":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        if key in {"ring", "orbit", "ripple"}:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(accent, 2))
            radius = 11 + (t * 12 if key == "ripple" else 0)
            p.drawEllipse(QRectF(56 - radius, 35 - radius, radius * 2, radius * 2))
            if key == "orbit":
                p.setBrush(accent)
                p.drawEllipse(QRectF(56 + 17 * cos(t * 2 * pi) - 4,
                                     35 + 17 * sin(t * 2 * pi) - 4, 8, 8))
        elif key == "bars":
            for i in range(3):
                height = 8 + (sin(seconds * 5 + i * 2) + 1) * 13
                p.drawRoundedRect(QRectF(42 + i * 13, 50 - height, 6, height), 3, 3)
        else:
            for i in range(3):
                size = 5 + (sin(seconds * 5 - i * 1.5) + 1) * 3
                p.drawEllipse(QRectF(33 + i * 20, 35 - size / 2, size, size))
    elif kind == "intro_exit":
        pose = intro_exit_pose(key, t)
        p.save()
        p.setOpacity(pose.opacity)
        p.translate(56, 35 + pose.travel * 40)
        p.scale(pose.scale, pose.scale)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        for i in range(3):
            if key != "wipe" or i < round(pose.reveal * 3):
                p.drawEllipse(QRectF((-22 + i * 22) * pose.spread - 4, -4, 8, 8))
        p.restore()
    elif kind in {"now_playing", "track_list"}:
        p.setPen(Qt.PenStyle.NoPen)
        fill = QColor(COLORS["alternate"]) if key not in {"glass", "minimal"} else QColor(COLORS["selection"])
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(12, 9, 88, 54), 7 if key != "minimal" else 1, 7 if key != "minimal" else 1)
        for i in range(3 if kind == "track_list" else 2):
            p.setBrush(accent if i == 1 else muted)
            p.drawRoundedRect(QRectF(21, 19 + i * 13, 57 if i == 1 else 44, 4), 2, 2)
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(QRectF(27, 18, 58, 34), 6, 6)
    p.end()
    return QIcon(pix)


class VisualPresetComboBox(QComboBox):
    """A conventional combobox with a visual, keyboard-navigable grid popup.

    Data values, indexes, signals, retranslation and model persistence remain
    owned by QComboBox, not by the popup. Unknown/custom values are preserved.
    """

    def __init__(self, preview_kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.preview_kind = preview_kind
        self._popup: QFrame | None = None
        self._list: QListWidget | None = None
        self._started = 0.0
        self._suppress_next_open = False
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._refresh_icons)

    def showPopup(self) -> None:
        if self._suppress_next_open:
            self._suppress_next_open = False
            return
        if not self.isEnabled() or self.count() == 0:
            return
        if self._popup is not None:
            self.hidePopup()
            return
        popup = QFrame(self, Qt.WindowType.Popup)
        popup.setObjectName("visualPresetPopup")
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        popup.installEventFilter(self)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(0)
        view = QListWidget(popup)
        view.setObjectName("visualPresetList")
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setFlow(QListView.Flow.LeftToRight)
        view.setWrapping(True)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setMovement(QListView.Movement.Static)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setWordWrap(True)
        view.setIconSize(_ICON_SIZE)
        view.setGridSize(_TILE_SIZE)
        view.setSpacing(0)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setFrameShape(QFrame.Shape.NoFrame)
        for index in range(self.count()):
            item = QListWidgetItem(self.itemText(index))
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setToolTip(self.itemText(index))
            item.setIcon(_preview_icon(self.preview_kind, self.itemData(index), 0))
            view.addItem(item)
        view.setCurrentRow(self.currentIndex())
        view.itemClicked.connect(self._choose)
        view.itemActivated.connect(self._choose)
        layout.addWidget(view)
        self._popup = popup
        self._list = view

        # IconMode uses the VIEWPORT width, not the outer window width, to
        # calculate wrapping. Reserve space for its scrollbar and frame:
        # otherwise a nominal 3-column popup silently wraps to two and
        # leaves a column-sized empty strip (especially on Windows/DPI).
        screen = self.screen()
        bounds = screen.availableGeometry() if screen is not None else self.geometry()
        margin = layout.contentsMargins()
        outer_width = margin.left() + margin.right() + 2  # popup border
        scrollbar_width = max(18, view.verticalScrollBar().sizeHint().width() + 4)
        cols = min(3, self.count(), max(
            1, (bounds.width() - 24 - outer_width - scrollbar_width) // _TILE_SIZE.width()
        ))
        rows = (self.count() + cols - 1) // cols
        max_height = max(120, min(480, bounds.height() - 24))
        desired_height = rows * _TILE_SIZE.height() + margin.top() + margin.bottom() + 2
        needs_scrollbar = desired_height > max_height
        width = cols * _TILE_SIZE.width() + outer_width + (
            scrollbar_width if needs_scrollbar else 4
        )
        height = min(desired_height, max_height)
        # Extra viewport pixels are deliberate breathing room, not a spare
        # (blank) preset column; all columns have enough room to wrap.
        view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if needs_scrollbar
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        origin = self.mapToGlobal(QPoint(0, self.height()))
        x = max(bounds.left(), min(origin.x(), bounds.right() - width + 1))
        y = origin.y() if origin.y() + height <= bounds.bottom() else (
            self.mapToGlobal(QPoint(0, 0)).y() - height
        )
        y = max(bounds.top(), min(y, bounds.bottom() - height + 1))
        popup.setGeometry(x, y, width, height)
        self._started = monotonic()
        popup.show()
        view.setFocus()
        if self.preview_kind in _MOTION_KINDS | {"visualizer", "waveform", "meter", "particle"}:
            self._timer.start()

    def hidePopup(self) -> None:
        if self._popup is not None:
            self._popup.close()

    def mousePressEvent(self, event) -> None:
        # The parent QComboBox remains clickable while its own Qt.Popup is
        # open, but should TOGGLE it rather than immediately reopening it.
        if self._popup is not None:
            self.hidePopup()
            event.accept()
            return
        super().mousePressEvent(event)

    def _reset_reopen_guard(self) -> None:
        self._suppress_next_open = False

    def _choose(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(index, int) and 0 <= index < self.count():
            self.setCurrentIndex(index)
        if self._popup is not None:
            self._popup.close()
        self.setFocus(Qt.FocusReason.PopupFocusReason)

    def _refresh_icons(self) -> None:
        if self._list is None or not self._list.isVisible():
            self._timer.stop()
            return
        seconds = monotonic() - self._started
        viewport = self._list.viewport().rect()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if self._list.visualItemRect(item).intersects(viewport):
                item.setIcon(_preview_icon(self.preview_kind, self.itemData(i), seconds))

    def eventFilter(self, watched, event):
        if watched is self._popup and event.type() == QEvent.Type.Hide:
            # Windows may close the Popup before dispatching the *same* click
            # to the combo. Suppress only that click; next deliberate clicks
            # must work normally.
            over_combo = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
            if over_combo:
                self._suppress_next_open = True
                QTimer.singleShot(0, self._reset_reopen_guard)
            self._timer.stop()
            self._list = None
            self._popup = None
            watched.deleteLater()
        return super().eventFilter(watched, event)
