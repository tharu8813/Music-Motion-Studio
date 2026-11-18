"""Preview-backed grid popup for visual properties, preserving QComboBox data/signals.

The collapsed control is still a regular QComboBox. Only its popup changes:
a keyboard-accessible, scrollable grid with illustrative or renderer-derived
previews. Previews never mutate project state or start background playback.
"""

from __future__ import annotations

from dataclasses import replace
from math import ceil, cos, pi, sin
from time import monotonic
from weakref import ref

from PySide6.QtCore import QEvent, QModelIndex, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QListView, QStyle,
    QStyledItemDelegate, QToolTip, QVBoxLayout, QWidget,
)

from app.animation.curves import animation_pose, loop_pose, music_reactive_pose
from app.canvas.renderers.base import linear_gradient
from app.animation.lyrics import cue_effect_pose, intro_exit_pose, lyric_progress, stagger_progress
from app.models.source import Source, SourceType
from app.services.app_settings_service import preset_preview_on_hover
from app.ui.design_system import COLORS
from app.utils.level_meter_painter import paint_level_meter
from app.utils.particle_painter import paint_particles
from app.utils.visualizer_paint import draw_visualizer_content
from app.utils.mask_shapes import mask_path
from app.utils.progress_style_paint import progress_fill_brush, paint_progress_design
from app.utils.now_playing_style_paint import paint_now_playing_surface
from app.utils.album_frame_paint import album_art_rect, paint_album_frame_back, paint_album_frame_front
from app.utils.waveform_paint import paint_waveform


_ANIMATED_KINDS = frozenset({
    "entrance", "exit", "loop", "reactive", "lyric", "intro", "intro_exit",
    "visualizer", "particle",
})
_SELECTABLE = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
_TILE_WIDTH = 144
_ICON_SIZE = QSize(112, 72)


def _preview_color(kind: str, source: Source | None, accent: QColor) -> QColor:
    """Use glyph colors for text effects, not their transparent box fill."""
    if source is None:
        return QColor(accent)
    glyph_motion = kind in {"entrance", "exit", "loop", "reactive", "lyric", "intro", "intro_exit"}
    if glyph_motion and source.source_type in {SourceType.TEXT, SourceType.TIME, SourceType.LYRICS}:
        value = (
            source.subtitle_accent_color
            if source.source_type is SourceType.LYRICS and source.subtitle_accent_enabled
            else source.outline_color
        )
    else:
        value = source.fill_color
    color = QColor(value)
    if not color.isValid():
        return QColor(accent)
    if color.alphaF() < 0.2:
        return QColor(accent)
    if glyph_motion:
        stage = QColor(COLORS["stage"])
        if abs(color.lightness() - stage.lightness()) * color.alphaF() < 50:
            return QColor(accent)
    return color


def _paint_motion(painter: QPainter, rect: QRectF, pose, color: QColor) -> None:
    painter.save()
    painter.setOpacity(max(0.0, min(1.0, pose.opacity)))
    painter.translate(rect.center())
    # Normalize huge source-space motion to the small thumbnail stage.
    painter.translate(
        max(-20.0, min(20.0, pose.dx * 0.75)),
        max(-16.0, min(16.0, pose.dy * 0.75)),
    )
    painter.rotate(pose.rotation)
    painter.scale(pose.scale * pose.scale_x, pose.scale)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(-23, -14, 46, 28), 6, 6)
    painter.setBrush(QColor("#25312D"))
    painter.drawRoundedRect(QRectF(-15, -3, 30, 6), 3, 3)
    painter.restore()


def _gallery_progress(kind: str, elapsed: float) -> float:
    """Readable, repeating demonstration time, independent of saved effect duration.

    Actual animation_in/out_duration continues to control Canvas/export.
    Holding a visible frame between cycles prevents quick exits and lets
    users compare motion shapes without racing the popup.
    """
    lead, action, hold = {
        "entrance": (0.12, 0.90, 0.38),
        "exit": (0.50, 1.35, 0.40),
        "lyric": (0.30, 1.45, 0.50),
    }[kind]
    phase = max(0.0, elapsed) % (lead + action + hold)
    if phase <= lead:
        return 0.0
    return min(1.0, (phase - lead) / action)


def _paint_lyric_thumbnail(
    painter: QPainter, style: str, progress: float, source: Source | None, color: QColor,
) -> None:
    """Paint legible previous/current/next *text*, not three anonymous strokes.

    Use real lyric transition poses but magnify tiny default motion within the
    thumbnail so glow, zoom and travel are distinguishable. Only the sample
    is modified: no Source fields are changed or persisted.
    """
    base = source if source is not None else Source(SourceType.LYRICS, "Lyrics")
    sample = replace(
        base, subtitle_animation=style,
        subtitle_motion_distance=max(30.0, base.subtitle_motion_distance),
        subtitle_zoom_amount=max(0.28, base.subtitle_zoom_amount),
        subtitle_glow_strength=max(0.9, base.subtitle_glow_strength),
        subtitle_glow_radius=max(7.0, base.subtitle_glow_radius),
        subtitle_stagger=max(0.36, base.subtitle_stagger),
    )
    labels = ("LAST LINE", "SING WITH ME", "NEXT LINE")
    for index, label in enumerate(labels):
        current = index == 1
        row_progress = (
            stagger_progress(sample, progress, index, 0, 2)
            if style == "cascade" else lyric_progress(sample, progress)
        )
        travel, factor, glow, _reveal, opacity = cue_effect_pose(
            sample, row_progress, 0.8, current=True, transitioning=style != "none",
        )
        if style == "cascade":
            # Cascade delays successive cue rows; the underlying pose is
            # static for this style, so show the ordered reveal explicitly.
            # Retain a visible cue while the remaining rows stagger in.
            opacity = (0.42 if current else 0.28) + (0.58 if current else 0.72) * row_progress
            travel = 12.0 * (1.0 - row_progress)
            factor = 0.86 + 0.14 * row_progress
        elif not current:
            travel, factor, glow, opacity = 0.0, 1.0, 0.0, 1.0

        direction = sample.subtitle_flow_direction
        shift = max(-14.0, min(14.0, travel * 0.70))
        dx = (shift if direction == "left" else -shift) if direction in {"left", "right"} else 0.0
        dy = (-shift if direction == "down" else shift) if direction not in {"left", "right"} else 0.0
        alpha = (1.0 if current else 0.40) * max(0.06, min(1.0, opacity))
        font = QFont("Segoe UI")
        font.setPixelSize(11 if current else 9)
        font.setBold(current)

        painter.save()
        painter.setOpacity(alpha)
        painter.translate(56.0 + dx, (35.0 if current else 14.0 if index == 0 else 56.0) + dy)
        painter.scale(factor, factor)
        painter.setFont(font)
        bounds = QRectF(-48, -10, 96, 20)
        alignment = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine
        if current and style == "glow":
            metrics = QFontMetricsF(font)
            text_path = QPainterPath()
            text_path.addText(
                QPointF(-metrics.horizontalAdvance(label) / 2.0, metrics.ascent() * 0.40),
                font, label,
            )
            halo = QColor(color)
            halo.setAlphaF(min(0.70, 0.17 + glow * 0.36))
            painter.setPen(QPen(halo, 6.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(text_path)
        painter.setPen(QPen(color if current else QColor(COLORS["muted"])))
        painter.drawText(bounds, alignment, label)
        painter.restore()


def _preview_icon(
    kind: str, value: object, seconds: float, source: Source | None = None, *,
    scale: float = 1.0, demo_progress: float | None = None, demo_loop_period: float | None = None,
) -> QIcon:
    """Render at the display scale while keeping the sample coordinates in logical pixels."""
    pix = QPixmap(ceil(_ICON_SIZE.width() * scale), ceil(_ICON_SIZE.height() * scale))
    pix.setDevicePixelRatio(scale)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    w, h = _ICON_SIZE.width(), _ICON_SIZE.height()
    stage = QRectF(3, 2, w - 6, h - 4)
    p.setPen(QPen(QColor(COLORS["border"]), 1))
    p.setBrush(QColor(COLORS["stage"]))
    p.drawRoundedRect(stage, 7, 7)
    clip = QPainterPath()
    clip.addRoundedRect(stage.adjusted(1, 1, -1, -1), 6, 6)
    p.setClipPath(clip)
    accent = QColor(COLORS["accent"])
    muted = QColor(COLORS["muted"])
    key = str(value)
    if kind == "entrance":
        period = max(0.3, source.animation_in_duration if source else 1.6)
    elif kind == "exit":
        period = max(0.3, source.animation_out_duration if source else 1.6)
    elif kind == "lyric":
        period = max(0.8, source.subtitle_animation_duration if source else 1.6)
    else:
        period = 1.6
    t = demo_progress if demo_progress is not None else (seconds % period) / period
    color = _preview_color(kind, source, accent)

    if kind == "lyric":
        _paint_lyric_thumbnail(p, key, t, source, color)
    elif kind in {"entrance", "exit", "loop", "reactive"}:
        if key == "custom":
            p.setPen(QPen(accent, 2))
            p.drawLine(22, 47, 45, 24)
            p.drawLine(45, 24, 82, 42)
            p.setBrush(accent)
            for x, y in ((22, 47), (45, 24), (82, 42)):
                p.drawEllipse(QRectF(x - 3, y - 3, 6, 6))
        else:
            # Text and media can use a transparent fill. Keep the illustrative
            # sample visible against the stage while retaining legible colors.
            if kind == "loop":
                pose = loop_pose(
                    key, seconds,
                    demo_loop_period if demo_loop_period is not None else (
                        source.loop_motion_period if source else 1.6
                    ),
                    max(1.0, source.loop_motion_amount) if source else 1.0,
                    source.width if source else 180, source.height if source else 100,
                )
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
        fill = color
        if source is not None and source.gradient.enabled and not source.text_gradient:
            gradient = linear_gradient(track, source.gradient.angle)
            gradient.setColorAt(0, QColor(source.gradient.start_color))
            gradient.setColorAt(1, QColor(source.gradient.end_color))
            fill = gradient
        fill_brush = progress_fill_brush(style, track, QBrush(fill), color)
        ratio = max(0.0, min(1.0, source.progress_value if source is not None else .62))
        if not paint_progress_design(p, track, style, ratio, track_color, fill_brush, color):
            p.setBrush(track_color)
            p.drawRoundedRect(track, radius, radius)
            p.setBrush(fill_brush)
            p.drawRoundedRect(
                QRectF(track.left(), track.top(), track.width() * ratio, track.height()),
                radius, radius,
            )
        p.setBrush(fill_brush)
        center = track.left() + track.width() * ratio
        if resolved_knob == "spotify":
            p.drawEllipse(QRectF(center - track.height() * .34,
                                 track.center().y() - track.height() * .34,
                                 track.height() * .68, track.height() * .68))
        elif resolved_knob == "circle":
            radius = full.height() / 2
            center = max(full.left() + radius, min(full.right() - radius, center))
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
        outer = QRectF(30, 10, 52, 52) if key in {"vinyl", "filmstrip"} else QRectF(32, 8, 48, 56)
        art = QRectF(outer)
        p.setPen(Qt.PenStyle.NoPen)
        if key in {"vinyl", "filmstrip"}:
            paint_album_frame_back(p, outer, key)
            art = album_art_rect(outer, key)
        if key == "polaroid":
            p.setPen(QPen(QColor("#FFFFFF"), 1))
            p.setBrush(QColor("#F8FAFC"))
            p.drawRoundedRect(outer, 5, 5)
            art = outer.adjusted(6, 6, -6, -18)
        clip = QPainterPath()
        if key == "circle":
            art = QRectF(32, 12, 48, 48)
            clip.addEllipse(art)
        elif key == "vinyl":
            clip.addEllipse(art)
        elif key == "filmstrip":
            clip.addRect(art)
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
        p.fillRect(art, QBrush(image_fill))
        p.setPen(QPen(QColor("#DDE9E2"), 2.5))
        p.drawLine(int(art.left() + 6), int(art.bottom() - 13),
                   int(art.center().x()), int(art.top() + 16))
        p.drawLine(int(art.center().x()), int(art.top() + 16),
                   int(art.right() - 5), int(art.bottom() - 10))
        p.restore()
        paint_album_frame_front(p, outer, key)
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
        rect = QRectF(9, 13, 94, 46)
        count = max(24, min(128, source.visualizer_bars if source else 32))
        levels = [0.22 + 0.64 * abs(sin(index * 0.38 + 0.8)) for index in range(count)]
        paint_waveform(
            p, rect, levels, style=key, color=color,
            line_width=source.visualizer_line_width if source else 2.0,
        )
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
        phase = (seconds / (source.subtitle_intro_period if source else 3.0)) % 1.0
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
        text = QColor(source.outline_color) if source else QColor("#F8FAFC")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        text = paint_now_playing_surface(
            p, rect, key, color, text,
            source.border_radius if source else 7,
        )
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
            elif key == "spotlight" and current:
                highlight = QColor(source.track_list_current_background) if source else QColor(COLORS["selection"])
                highlight.setAlpha(max(140, highlight.alpha()))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(highlight)
                p.drawRoundedRect(row, 4, 4)
                p.fillRect(
                    QRectF(row.left(), row.top(), 3, row.height()),
                    QColor(source.track_list_current_color) if source else accent,
                )
            elif key == "outline" and current:
                p.setPen(QPen(QColor(source.track_list_current_color) if source else accent, 1.6))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(row, 4, 4)
            elif key == "stripes":
                stripe = QColor(
                    source.track_list_current_background if current else source.track_list_inactive_color
                ) if source else QColor(COLORS["selection"] if current else COLORS["muted"])
                stripe.setAlpha(145 if current else (36 if index % 2 == 0 else 16))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(stripe)
                p.drawRoundedRect(row, 2, 2)
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


class _PresetTileDelegate(QStyledItemDelegate):
    """Paint thumbnails without writing decoration or selection into the combo model."""

    def __init__(self, combo: VisualPresetComboBox, view: QListView) -> None:
        super().__init__(view)
        self._combo = ref(combo)
        self.hover_only = preset_preview_on_hover()
        self.seconds = 0.0
        self._still_icons: dict[tuple[str, float], QIcon] = {}

    def sizeHint(self, option, index) -> QSize:
        return self.parent().gridSize()

    def invalidate_previews(self) -> None:
        self._still_icons.clear()
        self.parent().viewport().update()

    def paint(self, painter, option, index) -> None:
        enabled = index.flags() & _SELECTABLE == _SELECTABLE
        combo = self._combo()
        if combo is None:
            return
        applied = index.row() == combo.currentIndex()
        hovered = enabled and bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = enabled and bool(option.state & QStyle.StateFlag.State_HasFocus)
        tile = QRectF(option.rect).adjusted(3.5, 3.5, -3.5, -3.5)
        background, border = "field", "border"
        if applied:
            background, border = "selection", "selection_border"
        elif hovered:
            background, border = "hover", "selection_border"
        if focused:
            border = "accent"
        if not enabled:
            background, border = "disabled", "border"

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(COLORS[border]), 2 if focused else 1))
        painter.setBrush(QColor(COLORS[background]))
        painter.drawRoundedRect(tile, 7, 7)

        stage = QRect(0, 0, min(_ICON_SIZE.width(), int(tile.width()) - 12), _ICON_SIZE.height())
        stage.moveTopLeft(tile.topLeft().toPoint())
        stage.translate(round((tile.width() - stage.width()) / 2), 6)
        value = str(index.data(Qt.ItemDataRole.UserRole))
        scale = painter.device().devicePixelRatioF()
        painter.setOpacity(1.0 if enabled else .45)
        if stage.isValid():
            animated = enabled and (hovered or not self.hover_only)
            self._icon(combo, value, scale, animated).paint(painter, stage)
        painter.setOpacity(1.0)

        if applied:
            x, y = stage.right() - 9, stage.top() + 9
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS["accent"]))
            painter.drawEllipse(QPointF(x, y), 8, 8)
            mark = QPainterPath(QPointF(x - 3.5, y))
            mark.lineTo(x - .8, y + 2.5)
            mark.lineTo(x + 3.8, y - 2.6)
            painter.setPen(QPen(QColor(COLORS["stage"]), 1.5, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(mark)

        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        label = QRect(int(tile.left()) + 6, stage.bottom() + 4,
                      int(tile.width()) - 12, 2 * option.fontMetrics.lineSpacing() + 2)
        flags = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap
        painter.setFont(option.font)
        text_bounds = painter.boundingRect(label, flags, text)
        if text_bounds.height() > label.height() or text_bounds.width() > label.width():
            text = option.fontMetrics.elidedText(text, Qt.TextElideMode.ElideRight, label.width())
        painter.setPen(QColor(COLORS["text" if enabled else "muted"]))
        painter.setClipRect(label)
        painter.drawText(label, flags, text)
        painter.restore()

    def _icon(self, combo: VisualPresetComboBox, value: str, scale: float, active: bool) -> QIcon:
        kind, source = combo.preview_kind, combo._preview_source
        # Keep entrance/exit still frames bright and legible; lyric and loop
        # samples instead show an easily distinguishable transition phase.
        if kind == "entrance":
            seconds = max(0.3, source.animation_in_duration if source else 1.6) * 0.72
        elif kind == "exit":
            seconds = max(0.3, source.animation_out_duration if source else 1.6) * 0.26
        elif kind == "lyric":
            seconds = max(0.8, source.subtitle_animation_duration if source else 1.6) * 0.26
        elif kind == "loop":
            seconds = (source.loop_motion_period if source else 1.6) * 0.25
        else:
            seconds = .6
        if active and kind in _ANIMATED_KINDS and value not in {"none", "custom"}:
            if kind in {"entrance", "exit", "lyric"}:
                return _preview_icon(
                    kind, value, 0.0, source, scale=scale,
                    demo_progress=_gallery_progress(kind, self.seconds),
                )
            if kind == "loop":
                return _preview_icon(
                    kind, value, self.seconds, source, scale=scale,
                    demo_loop_period=1.30,
                )
            return _preview_icon(kind, value, seconds + self.seconds, source, scale=scale)
        key = (value, scale)
        if key not in self._still_icons:
            self._still_icons[key] = _preview_icon(kind, value, seconds, source, scale=scale)
        return self._still_icons[key]

    def helpEvent(self, event, view, option, index) -> bool:
        if super().helpEvent(event, view, option, index):
            return True
        if event.type() == QEvent.Type.ToolTip and index.isValid():
            QToolTip.showText(event.globalPos(), str(index.data() or ""), view, option.rect)
            return True
        return False


class VisualPresetComboBox(QComboBox):
    """A QComboBox with a thumbnail grid over the same model and stored values."""

    def __init__(self, preview_kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.preview_kind = preview_kind
        self._preview_source: Source | None = None
        self._popup: QFrame | None = None
        self._list: QListView | None = None
        self._started = 0.0
        self._timer = QTimer(self)
        # Fluid motion without running full Canvas renders for every tile.
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._refresh_previews)
        self.currentIndexChanged.connect(self._update_applied_tile)

    def set_preview_source(self, source: Source | None) -> None:
        """Use the selected element's appearance, even after in-place edits."""
        if source is not self._preview_source:
            self.hidePopup()
        self._preview_source = source
        self.invalidate_previews()

    def invalidate_previews(self) -> None:
        """Discard cached icons when mutable Source appearance changes."""
        if self._list is not None:
            self._list.itemDelegate().invalidate_previews()

    def setModel(self, model) -> None:
        self.hidePopup()
        super().setModel(model)

    def showPopup(self) -> None:
        if self._popup is not None or not self.isEnabled() or self.count() == 0:
            return
        popup = QFrame(self, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        popup.setObjectName("visualPresetPopup")
        popup.installEventFilter(self)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(6, 6, 6, 6)

        view = QListView(popup)
        view.setObjectName("visualPresetList")
        view.setAccessibleName(self.accessibleName())
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setFlow(QListView.Flow.LeftToRight)
        view.setWrapping(True)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setMovement(QListView.Movement.Static)
        view.setUniformItemSizes(True)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setMouseTracking(True)
        view.setModel(self.model())
        view.setModelColumn(self.modelColumn())
        view.setRootIndex(self.rootModelIndex())
        view.setItemDelegate(_PresetTileDelegate(self, view))
        view.clicked.connect(self._choose)
        view.activated.connect(self._choose)
        view.installEventFilter(self)
        layout.addWidget(view)
        self._popup, self._list = popup, view

        popup.ensurePolished()
        view.ensurePolished()
        self._layout_popup()
        model = view.model()
        model.rowsInserted.connect(self._layout_popup)
        model.rowsRemoved.connect(self._layout_popup)
        model.modelReset.connect(self._layout_popup)
        active = self._model_index(self.currentIndex())
        if active.flags() & _SELECTABLE != _SELECTABLE:
            active = next((index for row in range(self.count())
                           if (index := self._model_index(row)).flags() & _SELECTABLE == _SELECTABLE),
                          QModelIndex())

        self._started = monotonic()
        popup.show()
        if self._popup is not popup:
            return
        view.setFocus(Qt.FocusReason.PopupFocusReason)
        if active.isValid():
            view.setCurrentIndex(active)
            view.scrollTo(active, QAbstractItemView.ScrollHint.EnsureVisible)
        if self.preview_kind in _ANIMATED_KINDS:
            self._timer.start()

    def _model_index(self, row: int) -> QModelIndex:
        return self.model().index(row, self.modelColumn(), self.rootModelIndex())

    def _layout_popup(self) -> None:
        if self._popup is None:
            return
        if not self.count():
            self.hidePopup()
            return
        anchor = QRect(self.mapToGlobal(self.rect().topLeft()), self.size())
        screen = QGuiApplication.screenAt(anchor.center()) or self.screen()
        self._position_popup(anchor, screen.availableGeometry().adjusted(6, 6, -6, -6))

    def _position_popup(self, anchor: QRect, bounds: QRect) -> None:
        popup, view = self._popup, self._list
        margins = popup.layout().contentsMargins() + popup.contentsMargins()
        # Qt reserves a few viewport pixels beyond the icon grid itself.
        grid_slack = 4
        scrollbar_width = view.verticalScrollBar().sizeHint().width()
        horizontal_chrome = margins.left() + margins.right() + grid_slack
        content_width = max(1, bounds.width() - horizontal_chrome - scrollbar_width)
        columns = min(3, self.count(), max(1, content_width // _TILE_WIDTH))
        cell_width = min(_TILE_WIDTH, content_width)
        cell_height = _ICON_SIZE.height() + 2 * view.fontMetrics().lineSpacing() + 23
        view.setGridSize(QSize(cell_width, cell_height))

        rows = (self.count() + columns - 1) // columns
        chrome_height = margins.top() + margins.bottom()
        below, above = anchor.bottom() + 5, anchor.top() - 4
        space_below = bounds.bottom() + 1 - below
        space_above = above - bounds.top()
        desired = min(4, rows) * cell_height + chrome_height
        open_up = desired > space_below and space_above > space_below
        available = space_above if open_up else space_below
        shown_rows = max(1, (available - chrome_height) // cell_height)
        height = min(desired, shown_rows * cell_height + chrome_height, bounds.height())
        scrolling = rows * cell_height + chrome_height > height
        width = columns * cell_width + horizontal_chrome
        # Reserve the gutter before Qt lays out wrapping, including on scaled displays.
        if scrolling:
            width += scrollbar_width
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn if scrolling
                                        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup.layout().setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)
        x = max(bounds.left(), min(anchor.left(), bounds.right() - width + 1))
        y = above - height if open_up else below
        popup.setGeometry(x, max(bounds.top(), min(y, bounds.bottom() - height + 1)), width, height)

    def hidePopup(self) -> None:
        if self._popup is not None:
            self._popup.close()
        super().hidePopup()

    def mousePressEvent(self, event) -> None:
        if self._popup is not None and event.button() == Qt.MouseButton.LeftButton:
            self.hidePopup()
            event.accept()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        # Scrolling the inspector over a closed field should never edit a source.
        event.ignore()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self.hidePopup()
        elif event.type() in {
            QEvent.Type.StyleChange, QEvent.Type.FontChange, QEvent.Type.PaletteChange,
        }:
            self.invalidate_previews()
            if self._popup is not None:
                self._layout_popup()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if getattr(self, "_popup", None) is not None:
            self._layout_popup()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if getattr(self, "_popup", None) is not None:
            self._layout_popup()

    def _choose(self, index: QModelIndex) -> None:
        if (self._popup is None or not index.isValid()
                or index != self._model_index(index.row()) or index.flags() & _SELECTABLE != _SELECTABLE):
            return
        row = index.row()
        text = self.itemText(row)
        self.hidePopup()
        self.setCurrentIndex(row)
        self.activated.emit(row)
        self.textActivated.emit(text)

    def _refresh_previews(self) -> None:
        view = self._list
        if view is None:
            return
        viewport = view.viewport()
        delegate = view.itemDelegate()
        delegate.seconds = monotonic() - self._started
        if not delegate.hover_only:
            viewport.update()
            return
        if viewport.underMouse():
            hovered = view.indexAt(viewport.mapFromGlobal(QCursor.pos()))
            viewport.update(view.visualRect(hovered))

    def _update_applied_tile(self) -> None:
        if self._list is not None:
            self._list.viewport().update()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self._popup and self._popup is not None:
            if kind == QEvent.Type.MouseButtonPress and self.rect().contains(
                self.mapFromGlobal(event.globalPosition().toPoint())
            ):
                # Qt would otherwise replay the dismissing click on the field.
                watched.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
                self.hidePopup()
                return True
            if kind == QEvent.Type.Hide:
                self._timer.stop()
                model = self._list.model()
                model.rowsInserted.disconnect(self._layout_popup)
                model.rowsRemoved.disconnect(self._layout_popup)
                model.modelReset.disconnect(self._layout_popup)
                self._list = self._popup = None
                watched.deleteLater()
        elif watched is self._list and self._list is not None and kind in (
            QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride,
        ):
            key = event.key()
            confirm = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space)
            dismiss = key in (Qt.Key.Key_Escape, Qt.Key.Key_F4) or (
                key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and event.modifiers() == Qt.KeyboardModifier.AltModifier
            )
            if confirm or dismiss:
                event.accept()
                if kind == QEvent.Type.KeyPress:
                    if confirm:
                        self._choose(watched.currentIndex())
                    else:
                        self.hidePopup()
                return True
        return super().eventFilter(watched, event)
