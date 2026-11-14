"""Playback-only playlist ruler with waveform, transition and seek painting.

The dialog re-exports these names for callers that used its old import path.
This widget owns only its QSlider and animation; it never manages preview
workers, media players or render lifecycles.
"""

from __future__ import annotations

from bisect import bisect_right
from math import ceil
from pathlib import Path

import numpy as np
from PySide6.QtCore import (
    QAbstractAnimation, QEasingCurve, QLineF, QPointF, QRectF, Qt,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSlider, QWidget

from app.models.playlist import PlaylistTrack
from app.preview.timeline_waveform import SAMPLE_RATE, SAMPLES_PER_BIN
from app.timeline.models import TransitionType
from app.timeline.render_plan import AudioRenderClip, AudioRenderTransition
from app.ui.design_system import COLORS

TIMELINE_SCALE = 100


def _transition_display_regions(
    transitions: tuple[AudioRenderTransition, ...],
) -> tuple[tuple[float, float, TransitionType], ...]:
    """(start_seconds, end_seconds, type) per transition, sorted by start.

    Kept as a pure function of `AudioRenderPlan.transitions` -- the single
    source of truth for transition timing -- separate from
    ``PlaylistTimeline.paintEvent``'s pixel math, so the region computation
    is testable without constructing a live QWidget.
    """
    return tuple(
        sorted(
            (
                (transition.timeline_start, transition.timeline_start + transition.duration, transition.type)
                for transition in transitions
                if transition.type != TransitionType.CUT
            ),
            key=lambda region: region[0],
        )
    )


class PlaylistTimeline(QSlider):
    """Global playback slider with visible track boundaries and track numbers.

    ``schedule`` is the same (index, track, start, end) tuple
    ExportPreviewDialog derives once from CompiledRenderPlan's
    MetadataPlan.chapters (non-overlapping presentation ownership, not raw
    AudioRenderClip/PresentationWindow end -- AutoMix audio clips overlap on
    purpose, but track *ownership* must not), so Preview and this ruler can
    never disagree on track boundaries, and paintEvent (called every
    repaint) never recompiles it. ``set_schedule`` updates both this and the
    transition overlay together when the fast-path AutoMix render replaces
    the plan mid-preview.
    """

    def __init__(
        self, schedule: tuple[tuple[int, PlaylistTrack, float, float], ...],
        transitions: tuple[AudioRenderTransition, ...] = (),
        parent: QWidget | None = None, *,
        clips: tuple[AudioRenderClip, ...] = (),
    ) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._dragging = False
        self._drag_span: tuple[float, float, int, int] | None = None
        self._current_track_mode = False
        self._focused_track = -1
        self._view_position = 0.0
        self._waveforms: dict[str, np.ndarray] = {}
        self._waveform_columns_cache: dict[str, tuple[int, tuple[float, ...]]] = {}
        self.setMinimumHeight(46)
        self._scroll_animation = QVariantAnimation(self)
        self._scroll_animation.setDuration(420)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._scroll_animation.valueChanged.connect(self._set_view_position)
        self.valueChanged.connect(self._follow_current_track)
        self.set_schedule(schedule, transitions, clips=clips)

    def set_schedule(
        self, schedule: tuple[tuple[int, PlaylistTrack, float, float], ...],
        transitions: tuple[AudioRenderTransition, ...] = (), *,
        clips: tuple[AudioRenderClip, ...] = (),
    ) -> None:
        """Replace track boundaries and the transition overlay, then repaint."""
        self.schedule = schedule
        self._starts = tuple(row[2] for row in schedule)
        self._transition_regions = _transition_display_regions(transitions)
        self._clips_by_track = {clip.track_id: clip for clip in clips}
        self._waveform_columns_cache.clear()
        self._reset_view_position()

    def set_waveform(self, track_id: str, peaks: np.ndarray) -> None:
        """Add display-only source-audio peaks without changing seek or mix timing."""
        values = np.asarray(peaks, dtype=np.float32)
        if values.ndim != 1 or values.size == 0:
            return
        self._waveforms[track_id] = np.clip(np.nan_to_num(values, nan=0.0), 0.0, 1.0)
        self._waveform_columns_cache.pop(track_id, None)
        self.update()

    def _waveform_columns(self, index: int, width: int) -> tuple[float, ...]:
        """Cache max-pooled bars per pixel width, not per playback frame."""
        _, track, start, end = self.schedule[index]
        cached = self._waveform_columns_cache.get(track.id)
        if cached is not None and cached[0] == width:
            return cached[1]
        peaks = self._waveforms.get(track.id)
        if peaks is None or width < 2 or end <= start:
            return ()
        bins_per_second = SAMPLE_RATE / SAMPLES_PER_BIN
        count = min(ceil((end - start) * bins_per_second), width // 2)
        timeline_edges = np.linspace(start, end, count + 1)
        clip = self._clips_by_track.get(track.id)
        if clip is None:
            source_edges = timeline_edges - start
        else:
            # Map each display column through the same tempo steps as the mix.
            segments = clip.rate_segments()
            time_knots = [clip.timeline_start]
            source_knots = [clip.source_in]
            for source_start, source_end, rate in segments:
                time_knots.append(time_knots[-1] + (source_end - source_start) / rate)
                source_knots.append(source_end)
            source_edges = np.interp(timeline_edges, time_knots, source_knots)
        bin_edges = np.round(source_edges * bins_per_second, 8)
        first_bins = np.floor(bin_edges[:-1]).astype(int).clip(0, len(peaks))
        last_bins = np.ceil(bin_edges[1:]).astype(int).clip(0, len(peaks))
        columns = tuple(
            float(peaks[first:last].max()) if last > first and high > low else 0.0
            for first, last, low, high in zip(first_bins, last_bins, bin_edges, bin_edges[1:])
        )
        self._waveform_columns_cache[track.id] = (width, columns)
        return columns

    def set_current_track_mode(self, enabled: bool) -> None:
        self._current_track_mode = enabled
        self._reset_view_position()

    @property
    def view_is_scrolling(self) -> bool:
        return self._scroll_animation.state() == QAbstractAnimation.State.Running

    def _current_track(self) -> int:
        return max(0, bisect_right(self._starts, self.value() / TIMELINE_SCALE) - 1)

    def _set_view_position(self, position: float) -> None:
        self._view_position = position
        self.update()

    def _reset_view_position(self) -> None:
        self._scroll_animation.stop()
        self._focused_track = self._current_track()
        self._set_view_position(float(self._focused_track))

    def _follow_current_track(self) -> None:
        if not self._current_track_mode or self._dragging:
            return
        index = self._current_track()
        if index == self._focused_track:
            return
        self._focused_track = index
        self._scroll_animation.stop()
        target = float(index)
        if self._view_position == target:
            return
        self._scroll_animation.setStartValue(self._view_position)
        self._scroll_animation.setEndValue(target)
        self._scroll_animation.start()

    def _seek_to_track(self, index: int) -> None:
        # Round up so a fractional mix boundary lands in the selected song.
        self.setValue(ceil(self.schedule[index][2] * TIMELINE_SCALE - 1e-7))

    def _focused_blocks(self):
        """Project each nearby track into a slot, allowing its edge to peek in."""
        position = self._view_position
        margin = min(80, max(30, self.width() * 0.10))
        span = max(1.0, self.width() - 2 * margin)
        stride = span + 8
        for index in range(max(0, int(position) - 1), min(len(self.schedule), int(position) + 3)):
            left = margin + (index - position) * stride
            if left < self.width() - 9 and left + span > 9:
                yield index, left, span

    def paintEvent(self, event) -> None:  # type: ignore[override]
        """One block per track (the playing one outlined, the part heard filled),
        transitions in amber across the joins, and the playhead on top."""
        total = max(0.01, self.maximum() / TIMELINE_SCALE)
        current_seconds = self.value() / TIMELINE_SCALE
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        usable_width = max(1, self.width() - 18)

        def x_at(seconds: float) -> float:
            return 9 + seconds / total * usable_width

        top, height = 8.0, max(16.0, self.height() - 18.0)
        if self._current_track_mode and self.schedule:
            self._paint_current_track(painter, current_seconds, top, height)
            painter.end()
            return
        played_x = x_at(current_seconds)
        for index, (_schedule_index, track, start, end) in enumerate(self.schedule):
            active = start <= current_seconds < end or (
                index == len(self.schedule) - 1 and current_seconds >= start
            )
            block = QRectF(x_at(start) + 1, top, max(2.0, x_at(end) - x_at(start) - 2), height)
            self._paint_track_block(painter, block, block, index, track, active, played_x)
        for start, end, _transition_type in self._transition_regions:
            self._paint_transition(painter, x_at(start), x_at(end), top, height)
        self._paint_playhead(painter, played_x, top, height)
        painter.end()

    def _paint_current_track(self, painter: QPainter, seconds: float, top: float, height: float) -> None:
        viewport = QRectF(9, 0, max(1, self.width() - 18), self.height())
        painter.setClipRect(viewport)
        active_index = self._current_track()
        for index, left, span in self._focused_blocks():
            _, track, start, end = self.schedule[index]
            active = index == active_index
            x = left + max(0.0, min(1.0, (seconds - start) / max(0.01, end - start))) * span
            block = QRectF(left, top, span, height)
            if not active:
                block.adjust(0, 5, 0, -5)
            self._paint_track_block(painter, block, block.intersected(viewport), index, track, active, x,
                                    focused=True)
            if end > start:
                for transition_start, transition_end, _ in self._transition_regions:
                    if transition_start >= end:
                        break
                    first, last = max(start, transition_start), min(end, transition_end)
                    if last > first:
                        self._paint_transition(painter, left + (first - start) / (end - start) * span,
                                               left + (last - start) / (end - start) * span,
                                               block.top(), block.height())
            if active:
                self._paint_playhead(painter, x, top, height)

    def _paint_track_block(self, painter: QPainter, block: QRectF, visible: QRectF, index: int,
                           track: PlaylistTrack, active: bool, played_x: float, *, focused: bool = False) -> None:
        palette = self.palette()
        accent = palette.highlight().color()
        if active:
            painter.setPen(QPen(accent, 1.8 if focused else 1.2))
        else:
            painter.setPen(QPen(palette.mid().color(), 1) if focused else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["selection"]) if focused and active else palette.alternateBase())
        painter.drawRoundedRect(block, 4, 4)
        if played_x > block.left() and (active or not focused):
            heard = QColor(accent)
            heard.setAlpha(95 if focused else (70 if active else 38))
            painter.save()
            painter.setClipRect(QRectF(block.left(), block.top(), played_x - block.left(), block.height()),
                                Qt.ClipOperation.IntersectClip)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(heard)
            painter.drawRoundedRect(block, 4, 4)
            painter.restore()
        self._paint_waveform(painter, block, index, played_x)
        painter.setPen(palette.text().color() if active else palette.placeholderText().color())
        if visible.width() >= 20 and visible.width() < block.width() - 1 and not active:
            label = f"‹ {index + 1:02d}" if visible.left() > block.left() else f"{index + 1:02d} ›"
            painter.drawText(visible, Qt.AlignmentFlag.AlignCenter, label)
        elif visible.width() >= 28:
            label = f"{index + 1:02d}  {track.title or Path(track.file_path).stem}"
            text_rect = visible.adjusted(7, 0, -5, 0)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight,
                                                              int(text_rect.width())))

    def _paint_waveform(self, painter: QPainter, block: QRectF, index: int, played_x: float) -> None:
        """Draw subtle symmetric audio peaks behind the readable track label."""
        if block.width() < 12:
            return
        columns = self._waveform_columns(index, round(block.width()))
        if not columns:
            return
        center = block.center().y()
        height = max(1.0, (block.height() - 10) * 0.48)
        step = block.width() / len(columns)
        played, remaining = [], []
        for i, amplitude in enumerate(columns):
            x = block.left() + (i + 0.5) * step
            # Subtle dynamic-range compression keeps quiet sections visible.
            extent = max(0.5, height * amplitude ** 0.65)
            line = QLineF(x, center - extent, x, center + extent)
            (played if x <= played_x else remaining).append(line)
        painter.save()
        painter.setClipRect(block.adjusted(2, 2, -2, -2), Qt.ClipOperation.IntersectClip)
        accent = self.palette().highlight().color()
        accent.setAlpha(90)
        painter.setPen(QPen(accent, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if remaining:
            painter.drawLines(remaining)
        accent.setAlpha(175)
        painter.setPen(QPen(accent, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if played:
            painter.drawLines(played)
        painter.restore()

    @staticmethod
    def _paint_transition(painter: QPainter, x0: float, x1: float, top: float, height: float) -> None:
        if x1 - x0 < 1:
            return
        amber = QColor(COLORS["warning"])
        painter.setPen(Qt.PenStyle.NoPen)
        amber.setAlpha(46)
        painter.setBrush(amber)
        painter.drawRect(QRectF(x0, top, x1 - x0, height))
        amber.setAlpha(220)
        painter.setBrush(amber)
        painter.drawRoundedRect(QRectF(x0, top + height + 3, x1 - x0, 3), 1.5, 1.5)

    def _paint_playhead(self, painter: QPainter, played_x: float, top: float, height: float) -> None:
        palette = self.palette()
        painter.setPen(QPen(palette.text().color(), 1.6))
        painter.drawLine(QPointF(played_x, top - 5), QPointF(played_x, top + height + 4))
        painter.setPen(QPen(palette.window().color(), 1.5))
        painter.setBrush(palette.text().color())
        painter.drawEllipse(QPointF(played_x, top - 3), 4.5, 4.5)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            x_position = event.position().x()
            if self._current_track_mode and self.schedule:
                hit = next(((index, left, span) for index, left, span in self._focused_blocks()
                            if max(9, left) <= x_position <= min(self.width() - 9, left + span)), None)
                if hit is None:
                    event.accept()
                    return
                index, left, span = hit
                if index != self._current_track():
                    self._seek_to_track(index)
                    event.accept()
                    return
                self._scroll_animation.stop()
                _, _, start, end = self.schedule[index]
                lower = ceil(start * TIMELINE_SCALE - 1e-7)
                upper = (ceil(end * TIMELINE_SCALE - 1e-7) - 1
                         if index + 1 < len(self.schedule) else self.maximum())
                self._drag_span = (left, span, lower, max(lower, upper))
            self._dragging = True
            self.setSliderDown(True)
            self._set_value_from_position(x_position)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self._set_value_from_position(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._set_value_from_position(event.position().x())
            self._dragging = False
            if self._drag_span is not None:
                self._drag_span = None
                self._focused_track = -1
                self._follow_current_track()
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _set_value_from_position(self, x_position: float) -> None:
        if self._drag_span is not None:
            left, span, lower, upper = self._drag_span
            value = round(lower + (x_position - left) / span * (upper - lower))
            self.setValue(max(lower, min(upper, value)))
            return
        ratio = max(0.0, min(1.0, (x_position - 9) / max(1, self.width() - 18)))
        self.setValue(round(self.minimum() + ratio * (self.maximum() - self.minimum())))
