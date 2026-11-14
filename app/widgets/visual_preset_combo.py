"""Preview-backed grid popup for visual properties, preserving QComboBox data/signals.

The collapsed control is still a regular QComboBox. Only its popup changes:
a keyboard-accessible, scrollable grid with illustrative or renderer-derived
previews. Previews never mutate project state or start background playback.
"""

from __future__ import annotations

from dataclasses import replace
from math import cos, pi, sin
from time import monotonic

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QListView,
    QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from app.animation.curves import animation_pose, loop_pose, music_reactive_pose
from app.animation.lyrics import cue_effect_pose, intro_exit_pose, stagger_progress
from app.models.source import Source, SourceType
from app.ui.design_system import COLORS
from app.utils.level_meter_painter import paint_level_meter
from app.utils.particle_painter import paint_particles
from app.utils.visualizer_paint import draw_visualizer_content
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


def _preview_icon(kind: str, value: object, seconds: float, source: Source | None = None) -> QIcon:
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
    color = QColor(source.fill_color) if source is not None else accent
    if not color.isValid():
        color = accent

    if kind == "lyric":
        # Lyric cue transitions do NOT use the source entrance/exit curves.
        # These are the exact tuple values consumed by the Canvas lyric painter.
        sample = replace(source, subtitle_animation=key) if source is not None else (
            Source(SourceType.LYRICS, "Preview", subtitle_animation=key)
        )
        for idx in range(3):
            phase = stagger_progress(sample, t, idx, 0, 2) if key == "cascade" else t
            travel, scale, glow, _reveal, opacity = cue_effect_pose(
                sample, phase, 0.55, current=True, transitioning=key != "none"
            )
            p.save()
            p.setOpacity(max(0.15, min(1.0, opacity)) * (1.0 if idx == 1 else .4))
            dx = 0.0
            dy = travel * .62
            if sample.subtitle_flow_direction in {"left", "right"}:
                dx, dy = (dy if sample.subtitle_flow_direction == "left" else -dy), 0.0
            p.translate(56 + dx, 17 + idx * 17 + dy)
            p.scale(scale, scale)
            p.setPen(QPen(color, 3 if idx == 1 else 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(-25, 0, 23 if idx == 1 else 12, 0)
            if key == "glow" and idx == 1:
                halo = QColor(color)
                halo.setAlphaF(min(.45, max(0, glow * .5)))
                p.setPen(QPen(halo, 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(-25, 0, 23, 0)
            p.restore()
    elif kind in {"entrance", "exit", "loop", "reactive"}:
        if key == "custom":
            p.setPen(QPen(accent, 2))
            p.drawLine(22, 47, 45, 24)
            p.drawLine(45, 24, 82, 42)
            p.setBrush(accent)
            for x, y in ((22, 47), (45, 24), (82, 42)):
                p.drawEllipse(QRectF(x - 3, y - 3, 6, 6))
        else:
            if kind == "loop":
                pose = loop_pose(key, seconds, 1.6, 1.0, 180, 100)
            elif kind == "reactive":
                pose = music_reactive_pose(key, (sin(seconds * pi * 2) + 1) / 2, .7, 34)
            else:
                pose = animation_pose(key, t, kind == "entrance", 180, 100)
            _paint_motion(p, QRectF(33, 19, 46, 28), pose, color)
    elif kind in {"mask", "shape"}:
        sample = QRectF(30, 12, 52, 46)
        path = mask_path(sample, key)
        p.setPen(QPen(QColor(COLORS["accent_hover"]), 1))
        p.setBrush(accent)
        p.drawPath(path)
    elif kind in {"progress", "knob"}:
        # Mirror app/canvas/renderers/progress_renderer.py (default sample).
        style = (source.progress_style if source is not None else "spotify") if kind == "knob" else key
        knob = key if kind == "knob" else (source.progress_knob if source is not None else "auto")
        resolved_knob = ("spotify" if style == "spotify" else "none") if knob == "auto" else knob
        full = QRectF(13, 28, 86, 14)
        track = QRectF(full)
        if resolved_knob not in {"none", "spotify"}:
            track = QRectF(full.left(), full.center().y() - full.height() * .2, full.width(), full.height() * .4)
        radius = 0 if style == "youtube" else track.height() / 2
        track_color = QColor(source.progress_track_color) if source is not None else QColor("#303842")
        if style == "apple":
            track_color = QColor(color)
            track_color.setAlpha(80)
        elif style == "spotify":
            track_color = QColor("#1B2530")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track_color)
        p.drawRoundedRect(track, radius, radius)
        fill = color
        if source is not None and source.gradient.enabled and not source.text_gradient:
            gradient = QLinearGradient(track.topLeft(), track.topRight())
            gradient.setColorAt(0, QColor(source.gradient.start_color))
            gradient.setColorAt(1, QColor(source.gradient.end_color))
            fill = gradient
        p.setBrush(fill)
        progress_width = track.width() * (source.progress_value if source is not None else .62)
        progress_width = max(0.0, min(track.width(), progress_width))
        p.drawRoundedRect(QRectF(track.left(), track.top(), progress_width, track.height()), radius, radius)
        center = track.left() + progress_width
        if resolved_knob == "spotify":
            p.drawEllipse(QRectF(center - track.height() * .34,
                                 track.center().y() - track.height() * .34,
                                 track.height() * .68, track.height() * .68))
        elif resolved_knob == "circle":
            radius = full.height() / 2
            center = max(radius, min(full.width() - radius, center))
            p.drawEllipse(QRectF(center - radius, full.center().y() - radius,
                                 radius * 2, radius * 2))
        elif resolved_knob == "bar":
            width = max(2.0, full.height() * .3)
            left = max(0.0, min(full.width() - width, center - width / 2))
            p.drawRoundedRect(QRectF(left, full.top(), width, full.height()),
                              width / 2, width / 2)
    elif kind == "album_frame":
        # Album covers use paint_image_content's clip and surface rules.
        # The picture is a deliberately generic test tile, not a fake cover.
        outer = QRectF(32, 8, 48, 56)
        art = QRectF(outer)
        p.setPen(Qt.PenStyle.NoPen)
        if key == "polaroid":
            p.setPen(QPen(QColor("#FFFFFF"), 1))
            p.setBrush(QColor("#F8FAFC"))
            p.drawRoundedRect(outer, 5, 5)
            art = outer.adjusted(6, 6, -6, -18)
        clip = QPainterPath()
        if key == "circle":
            art = QRectF(32, 12, 48, 48)
            clip.addEllipse(art)
        elif key != "polaroid":
            clip.addRoundedRect(art, source.border_radius if source else 0,
                                source.border_radius if source else 0)
        else:
            clip.addRect(art)
        p.save()
        p.setClipPath(clip)
        image_fill = QLinearGradient(art.topLeft(), art.bottomRight())
        image_fill.setColorAt(0, QColor("#3B6566"))
        image_fill.setColorAt(1, QColor("#9BBEB1"))
        p.fillRect(art, image_fill)
        p.setPen(QPen(QColor("#DDE9E2"), 2.5))
        p.drawLine(int(art.left() + 6), int(art.bottom() - 13),
                   int(art.center().x()), int(art.top() + 16))
        p.drawLine(int(art.center().x()), int(art.top() + 16),
                   int(art.right() - 5), int(art.bottom() - 10))
        p.restore()
        if key == "glass":
            p.setBrush(QColor(255, 255, 255, 40))
            p.setPen(QPen(QColor(255, 255, 255, 180), 1.5))
            p.drawRoundedRect(outer, source.border_radius if source else 0,
                              source.border_radius if source else 0)
    elif kind == "visualizer":
        # Shared Canvas / export drawing primitive; one representative audio frame.
        levels = [0.16 + .76 * abs(sin(i * .61 + .8 + seconds * 1.2)) ** .9
                  for i in range(14)]
        draw_visualizer_content(
            p, QRectF(13, 9, 86, 53), levels, style=key, color=color,
            line_width=2.0, inner_radius=source.visualizer_inner_radius if source else .55,
        )
    elif kind == "meter":
        # Use exactly the same painter as the Canvas, preview and export.
        paint_level_meter(
            p, QRectF(30, 9, 52, 54), (0.72, 0.54), (0.82, .66), style=key,
            orientation=source.level_meter_orientation if source else "vertical",
            segments=source.level_meter_segments if source else 10,
            gap=source.level_meter_gap if source else 4,
            track_color=source.level_meter_track_color if source else "#263244",
            low_color=source.level_meter_low_color if source else "#22C55E",
            mid_color=source.level_meter_mid_color if source else "#FACC15",
            high_color=source.level_meter_high_color if source else "#EF4444",
            show_peak=source.level_meter_show_peak if source else True,
        )
    elif kind == "waveform":
        # The current Canvas waveform draws this same stroke for all three
        # stored waveform_style values; don't invent a fill or mirror that the
        # final render does not yet produce.
        from PySide6.QtCore import QPointF
        rect = QRectF(9, 13, 94, 46)
        path = QPainterPath(QPointF(rect.left(), rect.center().y()))
        for index in range(32):
            x = rect.left() + index * rect.width() / 31
            level = .22 + .64 * abs(sin(index * .38 + .8))
            y = rect.center().y() - sin(index * .72) * level * rect.height() * .36
            path.lineTo(x, y)
        p.setPen(QPen(color, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
    elif kind == "particle":
        # Reuse the actual particle painter, including deterministic sample
        # placement, geometry, alpha, seeded shape and animation.
        p.save()
        p.setClipRect(QRectF(5, 4, 102, 64))
        paint_particles(
            p, QRectF(5, 4, 102, 64), style=key,
            color=source.fill_color if source else COLORS["accent"],
            secondary_color=source.particle_secondary_color if source else "#9ABBE2",
            density=source.particle_density if source else 25,
            speed=source.particle_speed if source else 1.0,
            minimum_size=source.particle_min_size if source else 2.0,
            maximum_size=source.particle_max_size if source else 6.0,
            particle_opacity=source.particle_opacity if source else .8,
            direction=source.particle_direction if source else -90.0,
            drift=source.particle_drift if source else .3,
            twinkle=source.particle_twinkle if source else .5,
            glow=source.particle_glow if source else .7,
            seed=source.particle_seed if source else 13,
            time_seconds=seconds,
        )
        p.restore()
    elif kind == "intro":
        # Mirrors the phase, per-dot pulse, orbit and ring shapes in
        # lyrics_renderer.paint_intro using a fixed-size sample glyph.
        from app.animation.curves import ease_in_out_cubic
        p.setPen(Qt.PenStyle.NoPen)
        radius = 4.4
        spacing = radius * 4.8
        phase = t
        if key == "orbit":
            for index in range(3):
                angle = 2 * pi * (phase + index / 3)
                p.setOpacity(1 - index * .22)
                p.setBrush(color)
                p.drawEllipse(QRectF(56 + cos(angle) * radius * 2.8 - radius * .7,
                                     35 + sin(angle) * radius * 2.8 - radius * .7,
                                     radius * 1.4, radius * 1.4))
        elif key == "ripple":
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(color, max(1.0, radius * .5)))
            for index in range(3):
                pulse = (phase + index / 3) % 1
                p.setOpacity(sin(pi * pulse) ** 2)
                size = radius * (.4 + 3.1 * pulse)
                p.drawEllipse(QRectF(56 - size, 35 - size, size * 2, size * 2))
        elif key == "ring":
            ring = QRectF(56 - radius * 3, 35 - radius * 3, radius * 6, radius * 6)
            p.setPen(QPen(color, max(1.0, radius * .7),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setOpacity(.18)
            p.drawEllipse(ring)
            p.setOpacity(1.0)
            p.drawArc(ring, round((90 - phase * 360) * 16), -100 * 16)
        elif key == "comet":
            p.setBrush(color)
            for index in range(5):
                pulse = (phase - index * .07) % 1
                p.setOpacity((1 - index / 6) * sin(pi * pulse) ** 2)
                size = radius * (1 - index * .12)
                p.drawEllipse(QRectF(56 + (pulse - .5) * spacing * 2 - size,
                                     35 - size, size * 2, size * 2))
        else:
            count = 5 if key == "bars" else 3
            for index in range(count):
                pulse = (1 + sin(2 * pi * (phase - index * .13))) / 2
                brightness, shift, scale = .25 + .75 * pulse, 0.0, 1.0
                if key == "dots":
                    light = ease_in_out_cubic((phase - .1 - index * .17) / .18)
                    light *= 1 - ease_in_out_cubic((phase - .8) / .2)
                    brightness = .18 + .82 * light
                elif key == "breathing":
                    pulse = (1 - sin(2 * pi * phase + pi / 2)) / 2
                    brightness, scale = .2 + .8 * pulse, .85 + .15 * pulse
                elif key == "wave":
                    shift = -radius * 1.8 * pulse
                p.setOpacity(brightness)
                p.setBrush(color)
                x = 56 + (index - (count - 1) / 2) * spacing
                if key == "bars":
                    height = radius * (2 + 4 * pulse)
                    p.drawRoundedRect(QRectF(x - radius * .55, 35 - height / 2,
                                             radius * 1.1, height), radius * .55, radius * .55)
                else:
                    size = radius * scale
                    p.drawEllipse(QRectF(x - size, 35 + shift - size, size * 2, size * 2))
        p.setOpacity(1.0)
    elif kind == "intro_exit":
        pose = intro_exit_pose(key, t)
        p.save()
        p.setOpacity(pose.opacity)
        p.translate(56, 35 + pose.travel * 35)
        p.scale(pose.scale * pose.spread, pose.scale * pose.spread)
        if pose.reveal < 1.0:
            p.setClipRect(QRectF(-30, -15, 60 * pose.reveal, 30))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        for i in range(3):
            p.drawEllipse(QRectF(-24 + i * 24, -4, 8, 8))
        p.restore()
    elif kind == "now_playing":
        rect = QRectF(12, 9, 88, 54)
        p.setPen(Qt.PenStyle.NoPen)
        if key == "minimal":
            p.setBrush(Qt.BrushStyle.NoBrush)
        elif key == "glass":
            card = QColor(color)
            if card.lightness() > 185:
                card = QColor("#1B2638")
            card.setAlpha(205)
            p.setBrush(card)
            outline = QColor(source.outline_color) if source else QColor("#F8FAFC")
            outline.setAlpha(185)
            p.setPen(QPen(outline, 1.5))
        else:
            p.setBrush(color)
        radius = source.border_radius if source is not None else 7
        p.drawRoundedRect(rect, radius, radius)
        text = QColor(source.outline_color) if source is not None else QColor("#F8FAFC")
        p.setPen(QPen(text, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(24, 24, 50, 24)
        p.setPen(QPen(text, 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(24, 40, 81, 40)
    elif kind == "track_list":
        rect = QRectF(12, 8, 88, 57)
        p.setPen(Qt.PenStyle.NoPen)
        if key != "minimal":
            background = QColor(color)
            if key == "glass":
                background.setAlpha(min(190, max(72, background.alpha())))
            p.setBrush(background)
            p.drawRoundedRect(rect, source.border_radius if source else 7,
                              source.border_radius if source else 7)
        for index in range(3):
            row = QRectF(19, 15 + index * 15, 73, 12)
            current = index == 1
            if key in {"cards", "glass", "pills"}:
                c = QColor(source.track_list_current_background) if source else QColor(COLORS["selection"])
                if not current:
                    c = QColor(source.track_list_inactive_color) if source else QColor(COLORS["muted"])
                    c.setAlpha(42 if key == "glass" else 28)
                p.setBrush(c)
                p.setPen(Qt.PenStyle.NoPen)
                rad = row.height() / 2 if key == "pills" else 5
                p.drawRoundedRect(row, rad, rad)
            elif key == "queue" and current:
                p.fillRect(QRectF(row.left(), row.top(), 3, row.height()), QColor(COLORS["accent"]))
            c = QColor(source.track_list_current_color if current else source.track_list_inactive_color) if source else (
                accent if current else muted
            )
            if key == "scroll" and not current:
                c.setAlphaF(.4)
            p.setPen(QPen(c, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(25, int(row.center().y()), 79 if current else 66, int(row.center().y()))
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
        self._preview_source: Source | None = None
        self._popup: QFrame | None = None
        self._list: QListWidget | None = None
        self._started = 0.0
        self._suppress_next_open = False
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._refresh_icons)

    def set_preview_source(self, source: Source | None) -> None:
        """Use the selected element's real colors and settings for samples."""
        self._preview_source = source

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
            item.setIcon(_preview_icon(self.preview_kind, self.itemData(index), 0, self._preview_source))
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
                item.setIcon(_preview_icon(self.preview_kind, self.itemData(i), seconds, self._preview_source))

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
