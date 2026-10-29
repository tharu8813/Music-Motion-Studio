"""Graphics item that presents and edits a :class:`Source`."""

from __future__ import annotations

from app.utils.performance import timed

from collections.abc import Callable
from contextlib import contextmanager
from math import atan2, ceil, cos, degrees, radians, sin
from pathlib import Path
from time import monotonic

import numpy as np
from scipy.ndimage import gaussian_filter
from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import (
    QColor, QBrush, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter,
    QPainterPath, QPainterPathStroker, QPen, QPixmap, QTextLayout, QTextOption,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsSceneHoverEvent,
    QGraphicsSceneMouseEvent,
)
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink

from app.canvas.renderers import (
    album_cover_renderer,
    audio_level_meter_renderer,
    audio_visualizer_renderer,
    audio_waveform_renderer,
    background_renderer,
    image_renderer,
    logo_renderer,
    lyrics_renderer,
    now_playing_renderer,
    particle_overlay_renderer,
    progress_renderer,
    shape_renderer,
    text_renderer,
    time_renderer,
    track_list_renderer,
    video_renderer,
    watermark_renderer,
)
from app.canvas.renderers.base import device_pixel_ratio, linear_gradient
from app.models.source import TEXT_SOURCE_TYPES, Source, SourceType
from app.models.source_registry import source_registry
from app.preview.text_template import expand_placeholder_labels
from app.utils.font_loader import load_application_font
from app.utils.image_loader import load_pixmap
from app.utils.level_meter_painter import paint_level_meter
from app.utils.particle_painter import paint_particles
from app.video.frame_filter import (
    VideoFrameFilterSettings, VideoFrameFilterSignals, VideoFrameFilterTask,
    _rgb_pixels, apply_color_filters,
)
from app.video.preview_decoder import (
    VideoDecoderStats, video_position_needs_seek, video_seek_tolerance_ms,
)


_CAPITALIZATION = {
    "upper": QFont.Capitalization.AllUppercase,
    "lower": QFont.Capitalization.AllLowercase,
    "capitalize": QFont.Capitalization.Capitalize,
    "small_caps": QFont.Capitalization.SmallCaps,
}


class SourceItem(QGraphicsObject):
    """A selectable, movable and resizable canvas item backed by a Source."""

    changed_by_user = Signal(str, dict)
    duplicate_requested = Signal(str, float, float)
    edit_requested = Signal(str)
    interaction_hint = Signal(str)
    video_frame_ready = Signal()

    _handle_size = 10.0
    _middle_handle_min_span = 36.0
    _direct_gpu_pixel_formats = frozenset({
        "Format_RGBA8888", "Format_RGBX8888",
        "Format_BGRA8888", "Format_BGRX8888",
        "Format_ARGB8888", "Format_XRGB8888",
        "Format_ABGR8888", "Format_XBGR8888",
        "Format_NV12", "Format_NV21",
    })

    def __init__(self, source: Source) -> None:
        super().__init__()
        self.source = source
        self._resizing = False
        self._resize_origin_scene = QPointF()
        self._resize_size = (source.width, source.height)
        self._resize_handle: str | None = None
        self._next_press_edit_handle: str | None = None
        self._resize_anchor_scene = QPointF()
        self._rotating = False
        self._rotation_center_scene = QPointF()
        self._last_rotation_angle = 0.0
        self._raw_rotation = source.rotation
        self._suppress_position_sync = False
        self._duplicate_on_release = False
        self._duplicate_dragged = False
        self._duplicate_origin = QPointF()
        self._pending_user_changes: dict[str, object] = {}
        self._pixmap = QPixmap()
        self._video_player: QMediaPlayer | None = None
        self._video_sink: QVideoSink | None = None
        self._video_preview_path = ""
        self._video_preview_suppressed = False
        self._video_timeline_preview_active = False
        self._video_editor_poster_pending = False
        self._video_display_scale = 1.0
        self._video_preview_fps = 30
        self._video_last_frame_accepted = 0.0
        self._video_should_play = False
        self._video_applied_playback_rate: float | None = None
        self._video_loop_mode: QMediaPlayer.Loops | None = None
        self._video_pause_after_frame = False
        self._video_pending_seek_ms: int | None = None
        self._video_last_seek_time = 0.0
        self._video_last_requested_target_ms: int | None = None
        self._video_last_request_time = 0.0
        self._video_accepted_frames = 0
        self._video_dropped_frames = 0
        self._video_throttled_frames = 0
        self._video_pressure_drops = 0
        self._video_seek_count = 0
        self._video_source_switches = 0
        self._video_frame_handle = ""
        self._video_gpu_backed_frame = False
        self._video_gpu_color_filter = False
        self._video_applied_gpu_color_filter = False
        self._video_last_raw_frame = QImage()
        # Keep the filtered decoder image independently from the editor pixmap.
        # GPU preview can upload this image directly instead of rendering the
        # pixmap back through QGraphicsScene and capturing it into another image.
        self._video_presented_frame = QImage()
        self._video_presented_gpu_frame = QVideoFrame()
        self._video_presented_gpu_serial = 0
        self._video_presented_revision = 0
        self._video_direct_gpu_frame = False
        # The runnable keeps this unparented signal bridge alive if the Canvas
        # closes while a final frame is still being filtered.
        self._video_filter_signals = VideoFrameFilterSignals()
        self._video_filter_signals.finished.connect(self._video_filter_finished)
        self._video_filter_busy = False
        self._video_pending_frame: tuple[QImage, object] | None = None
        self._video_filter_generation = 0
        self._image_filter_key: tuple[str, float, float, float] | None = None
        self._raw_pixmap = QPixmap()
        self._raw_pixmap_path: str | None = None
        self._lyric_fonts: dict[str, QFont] = {}
        self._lyric_font_cache: dict[tuple, QFont] = {}
        self._lyric_advance_cache: dict[tuple, float] = {}
        self._lyric_wrap_cache: dict[tuple, tuple[tuple[str, int], ...]] = {}
        self._lyric_height_cache: tuple[tuple, float] | None = None
        self._lyric_ghost_cache: dict[tuple[object, ...], tuple[QPixmap, float]] = {}
        self._lyric_coverage_cache: dict[tuple, tuple[np.ndarray, int, int]] = {}
        self._lyric_reveal_cache: dict[tuple[object, ...], tuple[tuple[float, ...], QPainterPath]] = {}
        self._lyric_resource_key: tuple[object, ...] | None = None
        self._text_outline_path_key: tuple[object, ...] | None = None
        self._text_outline_path = QPainterPath()
        self._text_shadow_cache: dict[tuple[object, ...], tuple[QPixmap, float]] = {}
        # Preview/export assigns this transient value while a timed lyric cue
        # enters. Keeping it on the graphics item avoids serializing render state.
        self._subtitle_transition_progress = 1.0
        self._subtitle_transition_raw = 1.0
        self._subtitle_intro_state = None
        self._subtitle_word_reveal: tuple[float, ...] = ()
        self._subtitle_gap_unblurred = False
        self._subtitle_before_first = False
        self._subtitle_blur_restore_lines = frozenset()
        self._subtitle_blur_restore_progress = 1.0
        self._music_reaction_level = 0.0
        self._music_preview_current_line: int | None = None
        self._subtitle_cue_layout: tuple[tuple[int, int, int], ...] = ()
        self._subtitle_anchor_line = -1
        self._subtitle_anchor_line_count = 1
        self._subtitle_previous_line_count = 0
        self._subtitle_leaving_line_count = 0
        self._subtitle_entering_line_count = 0
        self._subtitle_prefetch_line_count = 0
        self._subtitle_prefetch_progress = 0.0
        self._subtitle_prefetch_entry_line_count = 0
        self._subtitle_emphasis = 1.0
        self._subtitle_previous_emphasis = 1.0
        self._subtitle_incoming_visible = True
        self._subtitle_line_style_indices: tuple[int, ...] = ()
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.apply_source()

    def boundingRect(self) -> QRectF:
        """Include painted shape effects so partial repaints clear them on moves."""
        margin = 30.0 if self.isSelected() else 1.0
        bounds = self.content_rect().adjusted(-margin, -margin, margin, margin)
        if self.source.source_type is SourceType.SHAPE:
            rect = self.content_rect()
            stroke = max(0.0, self.source.outline_width) / 2
            painted = rect.adjusted(-stroke, -stroke, stroke, stroke)
            if self.source.shape_kind == "line":
                # Legacy lines use a height-wide pen with square end caps.
                cap = max(1.0, self.source.height) / 2
                painted = rect.adjusted(-cap, 0, cap, 0)
            if self.source.shadow.enabled:
                shadow = self.source.shadow
                spread = max(0.0, shadow.blur_radius * 0.18)
                painted = painted.united(rect.translated(
                    -shadow.offset_x if self.source.flip_horizontal else shadow.offset_x,
                    -shadow.offset_y if self.source.flip_vertical else shadow.offset_y,
                ).adjusted(-spread, -spread, spread, spread))
            bounds = bounds.united(painted.adjusted(-1, -1, 1, 1))
        return bounds

    def content_rect(self) -> QRectF:
        """Return the visible content rectangle in item coordinates."""
        return QRectF(0, 0, self.source.width, self.source.height)

    def resize_handle_rects(self) -> dict[str, QRectF]:
        """Return resize handles that have enough room not to crowd each other.

        Corner handles remain available for resizing a very small source.  A
        middle handle disappears only along a short edge: east/west handles
        need sufficient height, while north/south handles need sufficient
        width.  Painting and hit testing both use this same filtered mapping.
        """
        half = self._handle_size / 2
        width = self.source.width
        height = self.source.height
        positions = {
            "nw": QPointF(0, 0),
            "n": QPointF(width / 2, 0),
            "ne": QPointF(width, 0),
            "e": QPointF(width, height / 2),
            "se": QPointF(width, height),
            "s": QPointF(width / 2, height),
            "sw": QPointF(0, height),
            "w": QPointF(0, height / 2),
        }
        handles = {
            name: QRectF(point.x() - half, point.y() - half,
                         self._handle_size, self._handle_size)
            for name, point in positions.items()
        }
        if height < self._middle_handle_min_span:
            handles.pop("e")
            handles.pop("w")
        if width < self._middle_handle_min_span:
            handles.pop("n")
            handles.pop("s")
        return handles

    def prioritize_edit_handle_for_next_press(self, handle: str | None) -> None:
        """Use the view's tolerant hit-test result for the next mouse press.

        The view works in device pixels while this item receives local
        coordinates.  Passing the already-resolved handle prevents a cursor at
        a rounded handle edge from turning into an item move on mouse-down.
        """
        self._next_press_edit_handle = handle

    def _resize_handle_at(self, position: QPointF, tolerance: float = 0.0) -> str | None:
        """Return the resize handle under *position*, if one is present."""
        for name, rect in self.resize_handle_rects().items():
            if rect.adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(position):
                return name
        return None

    def edit_handle_at(self, position: QPointF, tolerance: float = 0.0) -> str | None:
        """Return the active edit handle at a local position.

        This public hit test is also used by the view so a selected handle can
        take mouse priority over a different source painted above it.
        """
        if self.source.locked or not self.isSelected():
            return None
        if self.rotation_handle_rect().adjusted(
            -tolerance, -tolerance, tolerance, tolerance,
        ).contains(position):
            return "rotate"
        return self._resize_handle_at(position, tolerance)

    @staticmethod
    def cursor_for_edit_handle(
        handle: str | None, rotation: float = 0.0,
    ) -> Qt.CursorShape:
        """Return the screen-direction cursor for a resize or rotation handle."""
        if handle == "rotate":
            return Qt.CursorShape.CrossCursor
        handle_angles = {
            "e": 0.0, "se": 45.0, "s": 90.0, "sw": 135.0,
            "w": 180.0, "nw": 225.0, "n": 270.0, "ne": 315.0,
        }
        if handle not in handle_angles:
            return Qt.CursorShape.ArrowCursor
        # Standard resize cursors cover four undirected axes. Quantizing after
        # adding item rotation keeps the cursor aligned with the actual on-screen
        # resize direction for rotated sources as well.
        axis = round((handle_angles[handle] + rotation) / 45.0) % 4
        return {
            0: Qt.CursorShape.SizeHorCursor,
            1: Qt.CursorShape.SizeFDiagCursor,
            2: Qt.CursorShape.SizeVerCursor,
            3: Qt.CursorShape.SizeBDiagCursor,
        }[axis]

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        """Preview the operation represented by the selection handle."""
        handle = self.edit_handle_at(event.pos())
        if handle is None:
            self.unsetCursor()
        else:
            self.setCursor(self.cursor_for_edit_handle(handle, self.rotation()))
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        if not self._resizing and not self._rotating:
            self.unsetCursor()
        super().hoverLeaveEvent(event)

    @staticmethod
    def _anchor_point_for_size(handle: str, width: float, height: float) -> QPointF:
        """Return the opposite local anchor that stays fixed while resizing."""
        anchors = {
            "nw": QPointF(width, height),
            "n": QPointF(width / 2, height),
            "ne": QPointF(0, height),
            "e": QPointF(0, height / 2),
            "se": QPointF(0, 0),
            "s": QPointF(width / 2, 0),
            "sw": QPointF(width, 0),
            "w": QPointF(width, height / 2),
        }
        return anchors.get(handle, QPointF(0, 0))

    def _resize_aspect_ratio(
        self, modifiers: Qt.KeyboardModifiers,
    ) -> float | None:
        """Resolve the aspect constraint active for the current resize drag."""
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if self.source.source_type is SourceType.ALBUM_COVER or (alt and shift):
            return 1.0
        if not alt:
            return None
        width, height = self._resize_size
        return max(0.001, width / max(0.001, height))

    def _resize_aspect_driver(
        self, width: float, height: float, handle: str,
    ) -> str:
        """Choose the pointer-controlled dimension for a constrained resize."""
        if handle in {"e", "w"}:
            return "width"
        if handle in {"n", "s"}:
            return "height"
        original_width, original_height = self._resize_size
        width_change = abs(width / max(0.001, original_width) - 1.0)
        height_change = abs(height / max(0.001, original_height) - 1.0)
        return "width" if width_change >= height_change else "height"

    @staticmethod
    def _constrain_resize_aspect(
        width: float, height: float, ratio: float, driver: str,
    ) -> tuple[float, float]:
        """Return a minimum-safe size that exactly follows *ratio*."""
        ratio = max(0.001, ratio)
        if driver == "height":
            height = max(24.0, height, 32.0 / ratio)
            return height * ratio, height
        width = max(32.0, width, 24.0 * ratio)
        return width, width / ratio

    def _resize_delta(self, scene_position: QPointF) -> QPointF:
        """Convert a scene-space pointer displacement into stable local units."""
        scene_delta = scene_position - self._resize_origin_scene
        angle = radians(self.rotation())
        scale = max(0.1, self.scale())
        return QPointF(
            (cos(angle) * scene_delta.x() + sin(angle) * scene_delta.y()) / scale,
            (-sin(angle) * scene_delta.x() + cos(angle) * scene_delta.y()) / scale,
        )

    def rotation_handle_rect(self) -> QRectF:
        """Return the top-centre rotation handle rectangle."""
        half = self._handle_size / 2
        return QRectF(self.source.width / 2 - half, -28, self._handle_size,
                     self._handle_size)

    def apply_source(self) -> None:
        """Apply current model properties without emitting changes."""
        if self.source.font_path:
            load_application_font(self.source.font_path)
        with self.lyric_settings():
            self._rebuild_lyric_resources()
        self.prepareGeometryChange()
        self._sync_transform_origin()
        previous_suppression = self._suppress_position_sync
        self._suppress_position_sync = True
        try:
            self.setPos(self.source.x, self.source.y)
        finally:
            self._suppress_position_sync = previous_suppression
        self.setRotation(self.source.rotation)
        self.setScale(self.source.scale)
        self.setOpacity(1.0)
        self.setZValue(self.source.z_index)
        self.setVisible(self.source.visible)
        self.setFlag(QGraphicsItem.ItemIsMovable, not self.source.locked)
        self.setFlag(QGraphicsItem.ItemIsSelectable, not self.source.locked)
        if self.source.locked and self.isSelected():
            self.setSelected(False)
        image_path = self.source.content_path
        if self.source.source_type is SourceType.BACKGROUND and self.source.background_mode != "image":
            image_path = ""
        if self.source.source_type is SourceType.VIDEO:
            self._configure_video_preview()
            image_path = ""
        elif self._video_player is not None:
            self.release_video_decoder()
        filter_key = (
            image_path, self.source.brightness, self.source.contrast, self.source.blur
        )
        if self.source.source_type is not SourceType.VIDEO and filter_key != self._image_filter_key:
            if image_path != self._raw_pixmap_path:
                self._raw_pixmap = load_pixmap(image_path) if image_path else QPixmap()
                self._raw_pixmap_path = image_path
            self._pixmap = self._apply_image_filters(self._raw_pixmap)
            self._image_filter_key = filter_key
        self.update()

    def _configure_video_preview(self) -> None:
        """Load one editor poster frame without continuously decoding video."""
        paths = self.source.video_paths
        path = paths[0] if paths else ""
        self._video_should_play = False
        self._ensure_video_decoder()
        if path == self._video_preview_path:
            if not self.isVisible():
                self._video_player.stop()
            elif path and self._pixmap.isNull() and not self._video_editor_poster_pending:
                self._video_editor_poster_pending = True
                self._set_video_loops(QMediaPlayer.Loops.Once)
                self._video_player.setPosition(0)
                self._video_player.play()
            elif not self._video_timeline_preview_active:
                self._video_player.pause()
            return
        self._video_filter_generation += 1
        self._video_pending_frame = None
        self._video_preview_path = path
        self._video_timeline_preview_active = False
        self._video_editor_poster_pending = False
        self._video_last_frame_accepted = 0.0
        self._video_last_seek_time = 0.0
        self._video_last_requested_target_ms = None
        self._video_last_request_time = 0.0
        self._pixmap = QPixmap()
        self._video_presented_frame = QImage()
        self._video_presented_gpu_frame = QVideoFrame()
        self._video_presented_gpu_serial = 0
        self._video_presented_revision = 0
        if path and Path(path).is_file():
            self._video_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
            self._set_video_loops(QMediaPlayer.Loops.Once)
            self._video_player.setPosition(0)
            if self.isVisible():
                # Qt Multimedia needs to start decoding before its first frame
                # reaches QVideoSink.  _video_frame_changed pauses immediately
                # after that poster frame is accepted.
                self._video_editor_poster_pending = True
                self._video_player.play()
            else:
                self._video_player.stop()
        else:
            self._video_player.stop()

    def _ensure_video_decoder(self) -> QMediaPlayer:
        """Create one reusable decoder and connect its deferred-seek signals."""
        if self._video_player is None:
            self._video_player = QMediaPlayer(self)
            self._video_sink = QVideoSink(self)
            self._video_sink.videoFrameChanged.connect(self._video_frame_changed)
            self._video_player.setVideoOutput(self._video_sink)
            self._video_player.mediaStatusChanged.connect(
                self._video_media_status_changed,
            )
        return self._video_player

    def _set_video_loops(self, loops: QMediaPlayer.Loops) -> None:
        if self._video_player is not None and loops != self._video_loop_mode:
            self._video_player.setLoops(loops)
            self._video_loop_mode = loops

    def _set_video_playback_rate(self, speed: float) -> None:
        rate = max(0.05, min(8.0, float(speed)))
        if self._video_player is not None and rate != self._video_applied_playback_rate:
            self._video_player.setPlaybackRate(rate)
            self._video_applied_playback_rate = rate

    def _video_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status not in {
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        } or self._video_pending_seek_ms is None:
            return
        target = self._video_pending_seek_ms
        self._video_pending_seek_ms = None
        self._apply_video_seek(target)

    def _apply_video_seek(self, target_ms: int, *, request_time: float | None = None) -> None:
        if self._video_player is None:
            return
        self._video_last_seek_time = (
            monotonic() if request_time is None else float(request_time)
        )
        self._video_last_frame_accepted = 0.0
        self._video_player.setPosition(max(0, int(target_ms)))
        self._video_seek_count += 1
        if self._video_should_play:
            self._video_player.play()
        else:
            # Some backends emit a paused seek frame directly; others need one
            # decode tick. Pause immediately after that frame reaches the sink.
            self._video_pause_after_frame = True
            self._video_player.play()

    @timed("video.frame_callback_seconds")
    def _video_frame_changed(self, frame: QVideoFrame) -> None:
        now = monotonic()
        if self._video_timeline_preview_active:
            minimum_interval = 1.0 / max(1, self._video_preview_fps)
            # Qt's nominal 30/60 fps callbacks arrive a few milliseconds early.
            # ponytail: soft cap allows 10% jitter; use accumulated deadlines if
            # a strict rate cap is needed. Exact intervals dropped every other frame.
            if now - self._video_last_frame_accepted < minimum_interval * 0.90:
                self._video_dropped_frames += 1
                self._video_throttled_frames += 1
                return
        try:
            handle = frame.handleType()
            self._video_frame_handle = handle.name
            self._video_gpu_backed_frame = (
                handle is not QVideoFrame.HandleType.NoHandle
            )
        except (AttributeError, RuntimeError):
            self._video_frame_handle = ""
            self._video_gpu_backed_frame = False
        pixel_format_name = getattr(frame.pixelFormat(), "name", "")
        if (
            self._video_direct_gpu_frame
            and self._video_gpu_backed_frame
            and pixel_format_name in self._direct_gpu_pixel_formats
            and not frame.mirrored()
            and int(frame.rotation().value) == 0
        ):
            self._video_last_frame_accepted = now
            self._video_accepted_frames += 1
            self._video_presented_gpu_frame = QVideoFrame(frame)
            self._video_presented_gpu_serial += 1
            self._video_presented_revision += 1
            self._video_applied_gpu_color_filter = self._video_gpu_color_filter
            if self._video_editor_poster_pending and not self._video_timeline_preview_active:
                self._video_editor_poster_pending = False
                if self._video_player is not None:
                    self._video_player.pause()
            if self._video_pause_after_frame:
                self._video_pause_after_frame = False
                if self._video_player is not None:
                    self._video_player.pause()
            self.video_frame_ready.emit()
            return
        image = frame.toImage()
        if image.isNull():
            self._video_dropped_frames += 1
            self._video_pressure_drops += 1
            return
        self._video_last_frame_accepted = now
        self._video_accepted_frames += 1
        self._video_last_raw_frame = QImage(image)
        if self._video_editor_poster_pending and not self._video_timeline_preview_active:
            self._video_editor_poster_pending = False
            if self._video_player is not None:
                self._video_player.pause()
        if self._video_pause_after_frame:
            self._video_pause_after_frame = False
            if self._video_player is not None:
                self._video_player.pause()
        generation = self._video_filter_key()
        # Decode callbacks can arrive faster than a costly blur. Keep only the
        # newest frame, so effects never create an unbounded GUI event backlog.
        if self._video_pending_frame is not None:
            self._video_dropped_frames += 1
            self._video_pressure_drops += 1
        self._video_pending_frame = (QImage(image), generation)
        self._start_next_video_filter()

    def _video_filter_key(self) -> tuple[object, ...]:
        return (
            self._video_filter_generation,
            self._video_preview_path,
            max(8, round(self.source.width * self.source.scale * self._video_display_scale)),
            max(8, round(self.source.height * self.source.scale * self._video_display_scale)),
            self.source.brightness,
            self.source.contrast,
            self.source.video_saturation,
            self.source.video_grayscale,
            self.source.blur,
            self._video_gpu_color_filter,
        )

    def _start_next_video_filter(self) -> None:
        if self._video_filter_busy or self._video_pending_frame is None:
            return
        image, generation = self._video_pending_frame
        self._video_pending_frame = None
        settings = self._video_filter_settings(generation)
        self._video_filter_busy = True
        QThreadPool.globalInstance().start(VideoFrameFilterTask(
            image, settings, generation, self._video_filter_signals,
        ))

    @staticmethod
    def _video_filter_settings(generation: tuple[object, ...]) -> VideoFrameFilterSettings:
        """Split color work from scaling/blur when the GPU shader owns it."""
        return VideoFrameFilterSettings(
            width=int(generation[2]),
            height=int(generation[3]),
            brightness=0.0 if generation[9] else float(generation[4]),
            contrast=0.0 if generation[9] else float(generation[5]),
            saturation=1.0 if generation[9] else float(generation[6]),
            grayscale=False if generation[9] else bool(generation[7]),
            blur=float(generation[8]),
        )

    def _video_filter_finished(self, generation: object, image: object) -> None:
        self._video_filter_busy = False
        if (generation == self._video_filter_key()
                and isinstance(image, QImage) and not image.isNull()
                and not self._video_preview_suppressed and self.isVisible()):
            self._video_presented_frame = QImage(image)
            self._pixmap = QPixmap.fromImage(image)
            self._video_presented_revision += 1
            self._video_applied_gpu_color_filter = bool(generation[9])
            self.update()
            # Full preview renders the scene into a separate CPU/GPU surface.
            # During playback its timer picks up this pixmap, but a paused seek
            # has no next timer tick, so explicitly announce the completed frame.
            self.video_frame_ready.emit()
        self._start_next_video_filter()

    def set_video_gpu_color_filter(self, enabled: bool) -> None:
        """Move color-only video effects between CPU and GPU preview paths."""
        enabled = bool(enabled)
        if enabled == self._video_gpu_color_filter:
            return
        self._video_gpu_color_filter = enabled
        self._video_filter_generation += 1
        self._video_pending_frame = None
        if not self._video_last_raw_frame.isNull():
            self._video_pending_frame = (
                QImage(self._video_last_raw_frame), self._video_filter_key(),
            )
            self._start_next_video_filter()

    def set_video_direct_gpu_frame(self, enabled: bool) -> None:
        """Retain supported RHI frames for plane upload instead of toImage()."""
        enabled = bool(enabled)
        if enabled == self._video_direct_gpu_frame:
            return
        self._video_direct_gpu_frame = enabled
        self._video_presented_gpu_frame = QVideoFrame()
        if not enabled:
            self._video_presented_gpu_serial = 0
        self._video_presented_revision += 1

    @property
    def video_gpu_color_filter_ready(self) -> bool:
        """Return whether the displayed pixmap intentionally omits color effects."""
        return (
            self._video_gpu_color_filter
            and self._video_applied_gpu_color_filter
        )

    def video_preview_frame(self) -> QImage:
        """Return the latest filtered frame suitable for direct GPU upload."""
        return QImage(self._video_presented_frame)

    def video_preview_gpu_frame(self) -> tuple[QVideoFrame, int]:
        """Return the retained RHI frame and its monotonically increasing serial."""
        return QVideoFrame(self._video_presented_gpu_frame), self._video_presented_gpu_serial

    @property
    def video_preview_revision(self) -> int:
        """Return a cheap revision token for preview composition deduplication."""
        return self._video_presented_revision

    @property
    def video_preview_scale(self) -> float:
        """Return the current decoder-resolution budget for native frame upload."""
        return self._video_display_scale

    @property
    def video_preview_active(self) -> bool:
        """Return whether the retained frame belongs to the active timeline video."""
        return (
            self._video_timeline_preview_active
            and not self._video_preview_suppressed
            and (
                not self._video_presented_frame.isNull()
                or self._video_presented_gpu_frame.isValid()
            )
        )

    def set_video_preview_budget(self, display_scale: float, fps: int) -> None:
        """Bound live-preview conversion work to the selected preview quality."""
        scale = max(0.25, min(1.0, float(display_scale)))
        bounded_fps = max(10, min(60, int(fps)))
        scale_changed = scale != self._video_display_scale
        fps_changed = bounded_fps != self._video_preview_fps
        if not scale_changed and not fps_changed:
            return
        self._video_display_scale = scale
        self._video_preview_fps = bounded_fps
        if scale_changed:
            self._video_filter_generation += 1
            self._video_pending_frame = None

    def set_video_preview_playback(self, playing: bool, speed: float = 1.0) -> None:
        """Synchronize decoder run state with the main preview transport."""
        self._video_should_play = bool(playing)
        player = self._video_player
        if player is None:
            return
        self._set_video_playback_rate(speed)
        if not self._video_timeline_preview_active:
            return
        if self._video_should_play:
            self._video_pause_after_frame = False
            if player.playbackState() is not QMediaPlayer.PlaybackState.PlayingState:
                player.play()
        elif (not self._video_pause_after_frame
              and player.playbackState() is not QMediaPlayer.PlaybackState.PausedState):
            player.pause()

    def set_video_preview_position(
        self, path: str | None, seconds: float = 0.0, *, force_seek: bool = False,
    ) -> None:
        """Seek the editor decoder to a scheduler-selected video frame."""
        if self.source.source_type is not SourceType.VIDEO:
            return
        if not path or not self.isVisible():
            already_inactive = (
                self._video_preview_suppressed
                and not self._video_timeline_preview_active
                and self._video_pending_seek_ms is None
                and (
                    self._video_player is None
                    or self._video_player.playbackState()
                    is QMediaPlayer.PlaybackState.StoppedState
                )
            )
            if already_inactive:
                return
            self._video_preview_suppressed = True
            self._video_timeline_preview_active = False
            self._video_editor_poster_pending = False
            if self._video_player is not None:
                self._video_player.stop()
            self._video_pending_seek_ms = None
            self._video_last_requested_target_ms = None
            self._video_last_request_time = 0.0
            self.update()
            return
        self._video_preview_suppressed = False
        if path != self._video_preview_path and not Path(path).is_file():
            self._video_preview_suppressed = True
            self._video_timeline_preview_active = False
            self._video_pending_seek_ms = None
            self._video_last_requested_target_ms = None
            self._video_last_request_time = 0.0
            if self._video_player is not None:
                self._video_player.stop()
            self.update()
            return
        self._video_timeline_preview_active = True
        self._video_editor_poster_pending = False
        self._ensure_video_decoder()
        self._set_video_loops(QMediaPlayer.Loops.Infinite)
        self._set_video_playback_rate(self.source.video_speed)
        target = max(0, round(seconds * 1000))
        request_time = monotonic()
        previous_target = self._video_last_requested_target_ms
        previous_request_time = self._video_last_request_time
        self._video_last_requested_target_ms = target
        self._video_last_request_time = request_time
        if path != self._video_preview_path:
            self._video_filter_generation += 1
            self._video_pending_frame = None
            self._video_preview_path = path
            self._video_last_frame_accepted = 0.0
            self._video_source_switches += 1
            self._video_pending_seek_ms = target
            self._video_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
            self._video_player.play()
            if self._video_player.mediaStatus() in {
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            }:
                self._video_media_status_changed(self._video_player.mediaStatus())
            return
        if self._video_pending_seek_ms is not None:
            # While loading, retain only the latest playhead request.
            self._video_pending_seek_ms = target
            return
        current_position = self._video_player.position()
        seek_applied = False
        if not self._video_should_play:
            if video_position_needs_seek(
                current_position, target, self._video_preview_fps,
            ):
                self._apply_video_seek(target, request_time=request_time)
                seek_applied = True
        else:
            # QMediaPlayer's position can update much less frequently than the
            # 30 FPS preview timer. Re-seeking for each temporary discrepancy
            # prevents a busy decoder from ever catching up, producing a
            # positive feedback loop of stalls and more seeks. During ordinary
            # playback, correct only substantial drift at a bounded cadence.
            rate = max(0.05, self._video_applied_playback_rate or self.source.video_speed)
            requested_advance = (
                target - previous_target if previous_target is not None else 0
            )
            expected_advance = (
                max(0.0, request_time - previous_request_time) * 1000.0 * rate
                if previous_target is not None and previous_request_time > 0.0 else 0.0
            )
            discontinuity_threshold = max(
                600,
                video_seek_tolerance_ms(self._video_preview_fps) * 3,
            )
            discontinuous_request = (
                force_seek
                or (
                    previous_target is not None
                    and abs(requested_advance - expected_advance)
                    > discontinuity_threshold
                )
            )
            drift = abs(int(current_position) - target) if current_position >= 0 else 0
            cooldown_elapsed = request_time - self._video_last_seek_time
            drift_correction_due = (
                drift > discontinuity_threshold and cooldown_elapsed >= 1.0
            )
            if discontinuous_request or drift_correction_due:
                self._apply_video_seek(target, request_time=request_time)
                seek_applied = True
        if (not seek_applied and self._video_should_play
                and self._video_player.playbackState()
                is not QMediaPlayer.PlaybackState.PlayingState):
            self._video_player.play()

    def video_decoder_stats(self) -> VideoDecoderStats:
        return VideoDecoderStats(
            accepted_frames=self._video_accepted_frames,
            dropped_frames=self._video_dropped_frames,
            seek_count=self._video_seek_count,
            source_switches=self._video_source_switches,
            frame_handle=self._video_frame_handle,
            gpu_backed_frame=self._video_gpu_backed_frame,
            throttled_frames=self._video_throttled_frames,
            pressure_drops=self._video_pressure_drops,
            filter_pending=self._video_pending_frame is not None,
        )

    def reset_video_preview(self) -> None:
        """Return a video item to its static editor-poster presentation."""
        if self.source.source_type is SourceType.VIDEO:
            self.set_video_gpu_color_filter(False)
            self.set_video_direct_gpu_frame(False)
            self._video_preview_suppressed = False
            self._video_timeline_preview_active = False
            self._video_editor_poster_pending = False
            self._video_display_scale = 1.0
            self._video_preview_fps = 30
            self._video_preview_path = ""
            self._video_pending_seek_ms = None
            self._video_last_seek_time = 0.0
            self._video_last_requested_target_ms = None
            self._video_last_request_time = 0.0
            self._video_should_play = False
            self._video_pause_after_frame = False
            self._configure_video_preview()
            self.update()

    def suspend_video_preview(self) -> None:
        """Release decoder work while an element is hidden for deletion or replacement."""
        self._video_filter_generation += 1
        self._video_pending_frame = None
        self._video_preview_path = ""
        self._video_timeline_preview_active = False
        self._video_editor_poster_pending = False
        self._video_pending_seek_ms = None
        self._video_last_seek_time = 0.0
        self._video_last_requested_target_ms = None
        self._video_last_request_time = 0.0
        self._video_should_play = False
        self._video_pause_after_frame = False
        self._video_applied_playback_rate = None
        self._video_loop_mode = None
        self._video_gpu_color_filter = False
        self._video_applied_gpu_color_filter = False
        self._video_last_raw_frame = QImage()
        self._video_presented_frame = QImage()
        self._video_presented_gpu_frame = QVideoFrame()
        self._video_presented_gpu_serial = 0
        self._video_presented_revision = 0
        self._video_direct_gpu_frame = False
        if self._video_player is not None:
            self._video_player.stop()
            self._video_player.setSource(QUrl())

    def release_video_decoder(self) -> None:
        """Destroy multimedia objects while keeping this item reusable by Undo."""
        self._video_filter_generation += 1
        self._video_pending_frame = None
        self._video_preview_path = ""
        self._video_preview_suppressed = False
        self._video_timeline_preview_active = False
        self._video_editor_poster_pending = False
        self._video_last_frame_accepted = 0.0
        self._video_pending_seek_ms = None
        self._video_last_seek_time = 0.0
        self._video_last_requested_target_ms = None
        self._video_last_request_time = 0.0
        self._video_should_play = False
        self._video_pause_after_frame = False
        self._video_applied_playback_rate = None
        self._video_loop_mode = None
        self._video_gpu_color_filter = False
        self._video_applied_gpu_color_filter = False
        self._video_last_raw_frame = QImage()
        self._video_presented_frame = QImage()
        self._video_presented_gpu_frame = QVideoFrame()
        self._video_presented_gpu_serial = 0
        self._video_presented_revision = 0
        self._video_direct_gpu_frame = False
        player = self._video_player
        sink = self._video_sink
        had_decoder = player is not None or sink is not None
        if had_decoder or self.source.source_type is SourceType.VIDEO:
            self._pixmap = QPixmap()
        if had_decoder and self.source.source_type is not SourceType.VIDEO:
            # A source changed from video to an image-backed type. Force the
            # ordinary image path to rebuild after the decoder frame is cleared.
            self._image_filter_key = None
        self._video_player = None
        self._video_sink = None
        if sink is not None:
            try:
                sink.videoFrameChanged.disconnect(self._video_frame_changed)
            except (RuntimeError, TypeError):
                pass
        if player is not None:
            player.stop()
            try:
                player.setVideoOutput(None)
            except RuntimeError:
                pass
            player.setSource(QUrl())
            player.deleteLater()
        if sink is not None:
            sink.deleteLater()
        self.update()

    def _rebuild_lyric_resources(self) -> None:
        """Create lyric fonts once per source edit instead of once per paint call."""
        base_size = max(10, min(96, int(self.source.font_size)))
        style_sizes = [
            max(8.0, min(120.0, float(style.get(
                "font_size",
                self.source.font_size + float(style.get("font_size_offset", 0.0)),
            ))))
            for style in self.source.subtitle_line_styles if style
        ]
        largest_size = max([float(base_size), *style_sizes])
        role_scale = max(
            [float(self.source.subtitle_role_styles.get("current", {}).get("scale", self.source.subtitle_current_scale)),
             *(float(style.get("scale", 1.0)) for style in self.source.subtitle_role_styles.values())]
        )
        role_sizes = [
            max(8.0, min(120.0, float(style.get("font_size", largest_size)) + float(style.get("font_size_offset", 0))))
            for style in self.source.subtitle_role_styles.values()
        ]
        current_size = max(base_size, round(max([largest_size, *role_sizes]) * role_scale))
        line_styles_key = tuple(
            tuple(sorted(style.items())) for style in self.source.subtitle_line_styles
        )
        resource_key = (
            self.source.font_family, base_size, current_size,
            self.source.subtitle_line_spacing,
            round(self.source.subtitle_previous_blur),
            line_styles_key,
            tuple((role, tuple(sorted(style.items()))) for role, style in sorted(self.source.subtitle_role_styles.items())),
            *self._typography_key(),
        )
        if resource_key == self._lyric_resource_key:
            return
        # These two fonts define the tallest row; each line gets its own font
        # during painting while all rows retain one stable vertical rhythm.
        self._lyric_fonts = {
            "regular": self.text_font(base_size, self.source.font_weight),
            "current": self.text_font(current_size, self.source.font_weight),
        }
        self._lyric_ghost_cache.clear()
        self._lyric_coverage_cache.clear()
        self._lyric_font_cache.clear()
        self._lyric_advance_cache.clear()
        self._lyric_wrap_cache.clear()
        self._lyric_height_cache = None
        self._lyric_resource_key = resource_key

    def _lyric_font(self, size: int, weight: int, italic: bool) -> QFont:
        """Return a detached font; painting must not mutate a cached instance."""
        key = (self.source.font_family, size, weight, italic, self._typography_key())
        font = self._lyric_font_cache.pop(key, None)
        if font is None:
            font = self.text_font(size, weight)
            font.setItalic(italic)
            if len(self._lyric_font_cache) >= 64:
                self._lyric_font_cache.pop(next(iter(self._lyric_font_cache)))
        self._lyric_font_cache[key] = font
        return QFont(font)

    def _lyric_text_advance(self, font: QFont, text: str) -> float:
        key = (font.key(), text)
        advance = self._lyric_advance_cache.pop(key, None)
        if advance is None:
            advance = QFontMetricsF(font).horizontalAdvance(text)
            if len(self._lyric_advance_cache) >= 256:
                self._lyric_advance_cache.pop(next(iter(self._lyric_advance_cache)))
        self._lyric_advance_cache[key] = advance
        return advance

    def text_font(self, size: int, weight: int) -> QFont:
        """A font for this source's text with its shared typography applied."""
        font = QFont(self.source.font_family, size)
        font.setWeight(QFont.Weight(weight))
        font.setItalic(self.source.text_italic)
        if self.source.text_letter_spacing:
            font.setLetterSpacing(
                QFont.SpacingType.AbsoluteSpacing, self.source.text_letter_spacing,
            )
        font.setCapitalization(_CAPITALIZATION.get(
            self.source.text_case, QFont.Capitalization.MixedCase,
        ))
        return font

    def _typography_key(self) -> tuple[object, ...]:
        return (
            self.source.font_weight, self.source.text_italic,
            self.source.text_letter_spacing, self.source.text_case,
        )

    @staticmethod
    def _blurred_raster(
        paint: Callable[[QPainter], None], size: QSizeF, radius: float, pixel_ratio: float,
        *, solid_color: QColor | None = None,
        coverage_cache: dict[tuple, tuple[np.ndarray, int, int]] | None = None, coverage_key: tuple = (),
    ) -> tuple[QPixmap, float]:
        """Rasterise ``paint`` (logical coordinates within ``size``) and blur it.

        Returns the pixmap and its logical margin. It is rendered at device
        resolution, so callers cache it and only blit it with an opacity.
        """
        pixel_radius = max(0.0, round(radius * pixel_ratio, 2))
        margin = ceil(pixel_radius * 3) + 2
        image = QImage(
            ceil(size.width() * pixel_ratio) + margin * 2,
            ceil(size.height() * pixel_ratio) + margin * 2,
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        cached = coverage_cache.get(coverage_key) if coverage_cache is not None and solid_color is not None else None
        coverage, top, left = cached if cached is not None else (None, 0, 0)
        image.fill(Qt.GlobalColor.transparent)
        if coverage is None:
            raster_painter = QPainter(image)
            raster_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            raster_painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            raster_painter.translate(margin, margin)
            raster_painter.scale(pixel_ratio, pixel_ratio)
            paint(raster_painter)
            raster_painter.end()
        pixels = _rgb_pixels(image)
        if solid_color is not None:
            # Single-colour glyphs need only one Gaussian pass. Reconstruct
            # premultiplied BGR from coverage, keeping transparent edges clean.
            if coverage is None:
                alpha = pixels[:, :, 3]
                columns = np.flatnonzero(alpha.any(axis=0))
                rows = np.flatnonzero(alpha.any(axis=1))
                coverage = np.zeros((0, 0), dtype=np.float32)
                if columns.size and rows.size:
                    padding = ceil(pixel_radius * 3)
                    left, right = max(0, columns[0] - padding), min(alpha.shape[1], columns[-1] + padding + 1)
                    top, bottom = max(0, rows[0] - padding), min(alpha.shape[0], rows[-1] + padding + 1)
                    # Outside this rectangle the finite Gaussian kernel sees only zeros.
                    coverage = gaussian_filter(
                        alpha[top:bottom, left:right].astype(np.float32), sigma=pixel_radius,
                        mode="constant", truncate=3.0,
                    )
                if coverage_cache is not None:
                    while coverage_cache and (len(coverage_cache) >= 48 or
                            sum(value[0].nbytes for value in coverage_cache.values()) + coverage.nbytes > 16 * 1024 * 1024):
                        coverage_cache.pop(next(iter(coverage_cache)))
                    if coverage.nbytes <= 16 * 1024 * 1024:
                        coverage_cache[coverage_key] = coverage, top, left
            channels = np.array([
                solid_color.blueF(), solid_color.greenF(), solid_color.redF(), 1.0,
            ], dtype=np.float32)
            values = coverage[:, :, None] * channels
            target = pixels[top:top + coverage.shape[0], left:left + coverage.shape[1]]
        else:
            # General artwork can contain multiple colours.
            values = gaussian_filter(
                pixels.astype(np.float32), sigma=(pixel_radius, pixel_radius, 0),
                mode="constant", truncate=3.0,
            )
            target = pixels
        values += 0.5
        np.clip(values, 0.0, 255.0, out=values)
        np.copyto(target, values, casting="unsafe")
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(pixel_ratio)
        return pixmap, margin / pixel_ratio

    def _lyric_blur_pixmap(self, line: str, color: QColor, radius: float,
                           content_width: float, line_height: float,
                           pixel_ratio: float, flags: int,
                           font: QFont | None = None) -> tuple[QPixmap, float]:
        """Return a Gaussian-blurred lyric row and its logical margin."""
        font = font or self._lyric_fonts["current"]
        key = (line, color.rgba(), round(radius * pixel_ratio, 2), round(content_width),
               round(line_height), pixel_ratio, int(flags), font.toString())
        cached = self._lyric_ghost_cache.pop(key, None)
        if cached is None:
            if len(self._lyric_ghost_cache) >= 96:
                self._lyric_ghost_cache.pop(next(iter(self._lyric_ghost_cache)))

            def paint(blur_painter: QPainter) -> None:
                blur_painter.setPen(color)
                blur_painter.setFont(font)
                blur_painter.drawText(
                    QRectF(0.0, 0.0, content_width, line_height), flags, line,
                )

            cached = self._blurred_raster(
                paint, QSizeF(content_width, line_height), radius, pixel_ratio, solid_color=color,
                coverage_cache=self._lyric_coverage_cache,
                coverage_key=(line, color.alpha(), round(radius * pixel_ratio, 2),
                              content_width, line_height, pixel_ratio, int(flags), font.toString()),
            )
        self._lyric_ghost_cache[key] = cached
        return cached

    def _lyric_line_height(self) -> float:
        """Return a font-metric row that preserves Latin/Korean descenders."""
        key = (self._lyric_fonts["regular"].key(), self._lyric_fonts["current"].key(),
               float(self.source.subtitle_line_spacing))
        if self._lyric_height_cache is not None and self._lyric_height_cache[0] == key:
            return self._lyric_height_cache[1]
        regular = QFontMetricsF(self._lyric_fonts["regular"])
        current = QFontMetricsF(self._lyric_fonts["current"])
        glyph_height = max(regular.height(), current.height())
        # Qt's aligned text rectangle can otherwise land the last antialiased
        # row directly on its lower edge. Keep a small guard proportional to
        # the real font descent for g, j, p, q and y, plus configured spacing.
        descender_guard = max(2.0, max(regular.descent(), current.descent()) * 0.35)
        value = max(
            16.0,
            glyph_height
            + max(0.0, float(self.source.subtitle_line_spacing))
            + descender_guard,
        )
        self._lyric_height_cache = (key, value)
        return value

    def lyric_line_capacity(self) -> int:
        """Return the number of physical lyric rows that fit in this source."""
        return max(
            1,
            min(15, int(self.source.height / self._lyric_line_height())),
        )

    def effective_track_list_count(self) -> int:
        """Return the configured track count or a readable automatic count."""
        configured = int(self.source.track_list_count)
        if configured > 0:
            return max(1, min(15, configured))
        padding = max(0.0, min(40.0, self.source.track_list_item_padding))
        available = max(1.0, self.source.height - padding * 2.0)
        spacing = max(0.0, min(40.0, self.source.track_list_row_spacing))
        regular = QFont(
            self.source.font_family,
            max(8, min(120, round(self.source.font_size))),
        )
        regular.setWeight(QFont.Weight(self.source.font_weight))
        active = QFont(
            self.source.font_family,
            max(8, min(120, round(
                self.source.font_size
                * max(0.8, min(1.5, self.source.track_list_current_scale))
            ))),
        )
        active.setWeight(QFont.Weight.Bold)
        glyph_height = max(
            QFontMetricsF(regular).height(), QFontMetricsF(active).height(),
        )
        style_padding = (
            8.0
            if self.source.track_list_style in {"cards", "glass", "pills"}
            else 3.0
        )
        readable_row_height = max(18.0, glyph_height + style_padding)
        count = int(
            (available + spacing) / (readable_row_height + spacing)
        )
        return max(1, min(15, count))

    def _apply_image_filters(
        self, pixmap: QPixmap, *, include_blur: bool = True,
    ) -> QPixmap:
        """Apply non-destructive brightness, contrast, and soft blur to an image source.

        ``include_blur`` is disabled for the album-art ambient background, which
        already produces its own large blur while it is built.
        """
        if pixmap.isNull():
            return pixmap
        maximum_dimension = max(pixmap.width(), pixmap.height())
        if maximum_dimension > 4096:
            pixmap = pixmap.scaled(
                round(pixmap.width() * 4096 / maximum_dimension),
                round(pixmap.height() * 4096 / maximum_dimension),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            )
        blur = float(self.source.blur) if include_blur else 0.0
        if self.source.brightness == 0 and self.source.contrast == 0 and blur <= 0:
            return pixmap
        image = apply_color_filters(
            pixmap.toImage(),
            brightness=float(self.source.brightness),
            contrast=float(self.source.contrast),
            blur=blur,
        )
        return QPixmap.fromImage(image)

    def _adjust_fill_color(self, color: QColor) -> QColor:
        """Apply this source's brightness/contrast to a solid or gradient fill.

        Colour and gradient backgrounds have no bitmap for
        :meth:`_apply_image_filters` to touch, so the same math is applied to
        the paint colour directly. This keeps "darken the background" working
        identically in the editor, preview, and export.
        """
        brightness = self.source.brightness * 2.55
        contrast = 1.0 + self.source.contrast / 100.0
        if brightness == 0.0 and contrast == 1.0:
            return color
        adjusted = QColor(color)
        adjusted.setRed(max(0, min(255, round((color.red() - 128) * contrast + 128 + brightness))))
        adjusted.setGreen(max(0, min(255, round((color.green() - 128) * contrast + 128 + brightness))))
        adjusted.setBlue(max(0, min(255, round((color.blue() - 128) * contrast + 128 + brightness))))
        return adjusted

    def _sync_transform_origin(self) -> None:
        """Keep Qt rotation and scaling anchored at the visual object centre."""
        self.setTransformOriginPoint(self.source.width / 2, self.source.height / 2)

    def _paint_track_list(self, painter: QPainter, rect: QRectF) -> None:
        """Paint a semantic playlist with a distinct active-track row."""
        source = self.source
        style = source.track_list_style
        background = QColor(source.fill_color)
        if style == "glass":
            background.setAlpha(min(190, max(72, background.alpha())))
        background_brush = QBrush(background)
        if source.gradient.enabled and not source.text_gradient:
            gradient = linear_gradient(rect, source.gradient.angle)
            start = QColor(source.gradient.start_color)
            end = QColor(source.gradient.end_color)
            if style == "glass":
                start.setAlpha(min(190, max(72, start.alpha())))
                end.setAlpha(min(190, max(72, end.alpha())))
            gradient.setColorAt(0, start)
            gradient.setColorAt(1, end)
            background_brush = QBrush(gradient)
        if style != "minimal":
            painter.setBrush(background_brush)
            painter.setPen(
                QPen(QColor(source.outline_color), source.outline_width)
                if source.outline_width > 0 else QPen(Qt.PenStyle.NoPen)
            )
            painter.drawRoundedRect(rect, source.border_radius, source.border_radius)

        padding = max(0.0, min(40.0, source.track_list_item_padding))
        inner = rect.adjusted(padding, padding, -padding, -padding)
        lines = self.track_list_display_lines()
        if not lines or inner.isEmpty():
            return
        current_row = source.track_list_current_row
        if not 0 <= current_row < len(lines):
            current_row = next(
                (index for index, line in enumerate(lines)
                 if line.lstrip().startswith(("▶", "●", "▌"))),
                0,
            )
        spacing = max(0.0, min(40.0, source.track_list_row_spacing))
        row_height = max(12.0, (inner.height() - spacing * (len(lines) - 1)) / len(lines))
        alignment = {
            "center": Qt.AlignmentFlag.AlignHCenter,
            "right": Qt.AlignmentFlag.AlignRight,
        }.get(source.text_alignment, Qt.AlignmentFlag.AlignLeft)

        for index, raw_line in enumerate(lines):
            row = QRectF(
                inner.left(), inner.top() + index * (row_height + spacing),
                inner.width(), row_height,
            )
            is_current = index == current_row
            if style in {"cards", "glass", "pills"}:
                row_fill = QColor(source.track_list_current_background)
                if not is_current:
                    row_fill = QColor(source.track_list_inactive_color)
                    row_fill.setAlpha(28 if style != "glass" else 42)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(row_fill)
                radius = min(source.border_radius or 8.0, row.height() / 2)
                if style == "pills":
                    radius = row.height() / 2
                painter.drawRoundedRect(row, radius, radius)
            elif style == "queue" and is_current:
                accent = QColor(source.track_list_current_background)
                painter.fillRect(QRectF(row.left(), row.top(), 4.0, row.height()), accent)

            if source.track_list_show_dividers and index < len(lines) - 1:
                divider = QColor(source.track_list_inactive_color)
                divider.setAlpha(58)
                painter.setPen(QPen(divider, 1.0))
                painter.drawLine(row.bottomLeft() + QPointF(0, spacing / 2),
                                 row.bottomRight() + QPointF(0, spacing / 2))

            font_size = source.font_size * (
                max(0.8, min(1.5, source.track_list_current_scale)) if is_current else 1.0
            )
            painter.setFont(self.text_font(
                max(8, min(120, round(font_size))),
                QFont.Weight.Bold if is_current else source.font_weight,
            ))
            color = QColor(
                source.track_list_current_color if is_current
                else source.track_list_inactive_color
            )
            if not is_current:
                opacity = max(0.05, min(1.0, source.track_list_inactive_opacity))
                if style == "scroll":
                    distance = abs(index - current_row)
                    opacity *= max(0.2, 1.0 - distance * 0.22)
                color.setAlphaF(opacity)
            painter.setPen(color)
            text_rect = row.adjusted(10 if style in {"cards", "glass", "pills", "queue"} else 2,
                                     0, -8, 0)
            line = raw_line.lstrip()
            if line.startswith(("▶", "●", "▌")):
                line = line[1:].lstrip()
            configured_marker = {
                "play": "▶", "dot": "●", "line": "▌", "none": "",
            }.get(source.track_list_marker, "▶")
            marker_prefix = f"{configured_marker} " if is_current and configured_marker else ""
            line = f"{marker_prefix}{line}"
            if source.text_overflow == "ellipsis":
                line = painter.fontMetrics().elidedText(
                    line, Qt.TextElideMode.ElideRight, max(1, round(text_rect.width()))
                )
            painter.save()
            painter.setClipRect(text_rect, Qt.ClipOperation.IntersectClip)
            flags = alignment | Qt.AlignmentFlag.AlignVCenter
            if source.text_overflow != "wrap":
                flags |= Qt.TextFlag.TextSingleLine
            else:
                flags |= Qt.TextFlag.TextWordWrap
            self._draw_text(painter, text_rect, flags, line)
            painter.restore()

    def track_list_display_lines(self) -> list[str]:
        """Return real or editor-placeholder rows limited by effective count."""
        text = self._render_text() or self.source.name
        legacy_placeholder = "▶ 01. Current track\n  02. Next track"
        if text.strip() == legacy_placeholder.strip():
            korean = bool(getattr(self.scene(), "placeholder_labels_korean", False))
            current = "현재 곡" if korean else "Current track"
            upcoming = "다음 곡" if korean else "Next track"
            track = "플레이리스트 곡" if korean else "Playlist track"
            sample_lines = [f"▶ 01. {current}", f"  02. {upcoming}"]
            sample_lines.extend(
                f"  {index:02d}. {track} {index}" for index in range(3, 16)
            )
            text = "\n".join(sample_lines)
        return text.splitlines()[:self.effective_track_list_count()]

    _SAMPLE_TEXT_TYPES = frozenset({
        SourceType.TEXT, SourceType.TIME, SourceType.TRACK_LIST,
        SourceType.NOW_PLAYING, SourceType.LYRICS,
    })

    def _render_text(self) -> str:
        """Return the text to paint, showing known tokens as ``(label)`` on canvas.

        Preview and export set ``source.text`` to the resolved value before
        painting, so any ``%token%`` reaching here belongs to the live editing
        canvas and is displayed as a readable placeholder instead.
        """
        text = self.source.text
        if self.source.source_type not in self._SAMPLE_TEXT_TYPES:
            return text
        template = text
        if self.source.source_type is SourceType.TIME and "%" not in text:
            template = "%track_current_time%"
        if "%" not in template:
            return text
        korean = bool(getattr(self.scene(), "placeholder_labels_korean", False))
        return expand_placeholder_labels(template, korean)

    def _draw_text(
        self, painter: QPainter, rect: QRectF, flags: int, text: str,
    ) -> None:
        """Draw ``text`` with the configured glyph outline behind its fill.

        A vector path stroker produces one continuous, anti-aliased contour.
        Repainting glyphs at many integer offsets created scalloped edges,
        especially on diagonals and rounded Hangul/Latin glyphs.
        """
        source = self.source
        is_text = source.source_type in TEXT_SOURCE_TYPES
        width = float(source.text_stroke_width)
        if source.shadow.enabled and source.text_shadow_glyph and is_text:
            self._draw_glyph_shadow(painter, rect, flags, text, width)
        if width > 0.0:
            path = self._text_layout_path(painter, rect, flags, text)
            if not path.isEmpty():
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
                painter.fillPath(
                    self._glyph_stroke(path, width),
                    QBrush(QColor(source.text_stroke_color)),
                )
                painter.restore()
        brush = painter.pen().brush()
        if (source.text_gradient and source.gradient.enabled and is_text
                and not (source.source_type == SourceType.LYRICS and source.subtitle_karaoke_enabled)):
            # The caller's pen alpha (dimmed rows) carries into both stops.
            pen_alpha = painter.pen().color().alphaF()
            gradient = linear_gradient(self.content_rect(), source.gradient.angle)
            for position, value in ((0.0, source.gradient.start_color),
                                    (1.0, source.gradient.end_color)):
                color = QColor(value)
                color.setAlphaF(color.alphaF() * pen_alpha)
                gradient.setColorAt(position, color)
            brush = QBrush(gradient)
        painter.save()
        if self._line_spacing(flags):
            # drawText has no line-spacing control; fill the spaced glyph path.
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.fillPath(self._text_layout_path(painter, rect, flags, text), brush)
        else:
            painter.setPen(QPen(brush, 1.0))
            painter.drawText(rect, flags, text)
        painter.restore()

    def _line_spacing(self, flags: int) -> float:
        """Extra space between the rows of a multi-line text draw."""
        if (self.source.source_type not in TEXT_SOURCE_TYPES
                or flags & Qt.TextFlag.TextSingleLine):
            return 0.0
        return float(self.source.text_line_gap)

    @staticmethod
    def _glyph_stroke(path: QPainterPath, width: float) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(width * 2.0)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        return stroker.createStroke(path)

    def _draw_glyph_shadow(
        self, painter: QPainter, rect: QRectF, flags: int, text: str, stroke: float,
    ) -> None:
        """Soft shadow/glow cast by the glyphs (and their outline) themselves."""
        path = self._text_layout_path(painter, rect, flags, text)
        if path.isEmpty():
            return
        shadow = self.source.shadow
        bounds = path.boundingRect().adjusted(-stroke, -stroke, stroke, stroke)
        pixel_ratio = device_pixel_ratio(painter)
        radius = max(0.5, float(shadow.blur_radius) * 0.5)
        color = QColor(shadow.color)
        shadow_alpha = color.alphaF()
        color.setAlpha(255)
        # Position-independent: the raster is drawn at the path's own bounds.
        key = (painter.font().key(), text, round(rect.width(), 3), round(rect.height(), 3),
               int(flags), color.rgba(), round(radius * pixel_ratio, 2), pixel_ratio, stroke,
               self._line_spacing(flags))
        cached = self._text_shadow_cache.get(key)
        if cached is None:
            if len(self._text_shadow_cache) >= 64:
                self._text_shadow_cache.clear()
            def paint(shadow_painter: QPainter) -> None:
                shadow_painter.translate(-bounds.topLeft())
                shadow_painter.fillPath(path, color)
                if stroke > 0.0:
                    shadow_painter.fillPath(self._glyph_stroke(path, stroke), color)

            cached = self._blurred_raster(paint, bounds.size(), radius, pixel_ratio)
            self._text_shadow_cache[key] = cached
        pixmap, margin = cached
        painter.save()
        painter.setOpacity(
            painter.opacity() * max(0.0, min(1.0, shadow.opacity))
            * shadow_alpha * painter.pen().color().alphaF()
        )
        painter.drawPixmap(
            bounds.topLeft() + QPointF(shadow.offset_x - margin, shadow.offset_y - margin),
            pixmap,
        )
        painter.restore()

    def _text_layout_path(
        self, painter: QPainter, rect: QRectF, flags: int, text: str,
    ) -> QPainterPath:
        """Build a vector glyph path matching the surrounding drawText layout."""
        if not text or rect.width() <= 0.0 or rect.height() <= 0.0:
            return QPainterPath()
        spacing = self._line_spacing(flags)
        cache_key = (
            painter.font().key(), text, round(rect.left(), 3), round(rect.top(), 3),
            round(rect.width(), 3), round(rect.height(), 3), str(flags), spacing,
        )
        if cache_key == self._text_outline_path_key:
            return QPainterPath(self._text_outline_path)
        single_line = bool(flags & Qt.TextFlag.TextSingleLine)
        wrap = bool(flags & Qt.TextFlag.TextWordWrap) and not single_line
        paragraphs = [text.replace("\n", " ")] if single_line else text.split("\n")
        horizontal = Qt.AlignmentFlag.AlignLeft
        if flags & Qt.AlignmentFlag.AlignHCenter:
            horizontal = Qt.AlignmentFlag.AlignHCenter
        elif flags & Qt.AlignmentFlag.AlignRight:
            horizontal = Qt.AlignmentFlag.AlignRight

        runs: list[object] = []
        y_cursor = 0.0
        for paragraph in paragraphs:
            # A space preserves the vertical advance of intentionally blank
            # lines without contributing a visible glyph to the path.
            layout = QTextLayout(paragraph or " ", painter.font())
            option = QTextOption()
            option.setAlignment(horizontal)
            option.setWrapMode(
                QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
                if wrap else QTextOption.WrapMode.NoWrap
            )
            layout.setTextOption(option)
            layout.beginLayout()
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(rect.width())
                if runs or y_cursor > 0.0:
                    y_cursor += spacing
                line.setPosition(QPointF(0.0, y_cursor))
                y_cursor += line.height()
                runs.extend(line.glyphRuns())
                if not wrap:
                    break
            layout.endLayout()

        vertical_offset = 0.0
        if flags & Qt.AlignmentFlag.AlignVCenter:
            vertical_offset = max(0.0, (rect.height() - y_cursor) / 2.0)
        elif flags & Qt.AlignmentFlag.AlignBottom:
            vertical_offset = max(0.0, rect.height() - y_cursor)

        path = QPainterPath()
        for glyph_run in runs:
            raw_font = glyph_run.rawFont()
            for glyph_index, position in zip(
                glyph_run.glyphIndexes(), glyph_run.positions(), strict=False,
            ):
                glyph_path = raw_font.pathForGlyph(glyph_index)
                glyph_path.translate(
                    rect.left() + position.x(),
                    rect.top() + vertical_offset + position.y(),
                )
                path.addPath(glyph_path)
        self._text_outline_path_key = cache_key
        self._text_outline_path = QPainterPath(path)
        return path

    @contextmanager
    def lyric_settings(self):
        original = self.source
        self.source = original.resolved_lyrics()
        try:
            yield self.source
        finally:
            self.source = original

    def paint(
        self,
        painter: QPainter,
        option: object,
        widget: object | None = None,
    ) -> None:
        """Dispatch through the source definition without changing legacy pixels."""
        renderer = source_registry.get(self.source.source_type).renderer
        if renderer is None:
            raise RuntimeError(f"No renderer registered for {self.source.source_type.value}")
        with self.lyric_settings():
            renderer(self, painter, option, widget)

    def _paint_legacy(
        self,
        painter: QPainter,
        option: object,
        widget: object | None = None,
    ) -> None:
        """Paint source content plus a compact selection bounding box."""
        if self.source.source_type is SourceType.VIDEO and self._video_preview_suppressed:
            return
        rect = self.content_rect()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.save()
        # Multiply, never replace: the scene sets the painter opacity to this
        # item's animated QGraphicsItem.opacity() before paint(), and the entrance
        # / exit fades rely on that being carried through.
        painter.setOpacity(painter.opacity() * self.source.opacity)
        # Colour / gradient backgrounds carry no bitmap, so brightness and
        # contrast are folded into the paint colour here instead.
        tint = (
            self._adjust_fill_color
            if self.source.source_type is SourceType.BACKGROUND and self._pixmap.isNull()
            else (lambda color: color)
        )
        fill = QBrush(tint(QColor(self.source.fill_color)))
        if self.source.gradient.enabled:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0, tint(QColor(self.source.gradient.start_color)))
            gradient.setColorAt(1, tint(QColor(self.source.gradient.end_color)))
            fill = QBrush(gradient)
        pen = (
            QPen(QColor(self.source.outline_color), self.source.outline_width)
            if self.source.outline_width > 0
            else QPen(Qt.PenStyle.NoPen)
        )
        painter.setPen(pen)
        painter.setBrush(fill)

        if self.source.shadow.enabled:
            shadow_color = QColor(self.source.shadow.color)
            shadow_color.setAlphaF(shadow_color.alphaF() * max(0.0, min(1.0, self.source.shadow.opacity)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(shadow_color)
            shadow_rect = rect.translated(self.source.shadow.offset_x, self.source.shadow.offset_y)
            spread = max(0.0, self.source.shadow.blur_radius * 0.18)
            painter.drawRoundedRect(shadow_rect.adjusted(-spread, -spread, spread, spread),
                                    self.source.border_radius + spread,
                                    self.source.border_radius + spread)
            painter.setPen(pen)
            painter.setBrush(fill)

        image_backed_types = {
            SourceType.IMAGE,
            SourceType.BACKGROUND,
            SourceType.ALBUM_COVER,
            SourceType.LOGO,
            SourceType.WATERMARK,
            SourceType.VIDEO,
        }
        if self.source.source_type in image_backed_types and not self._pixmap.isNull():
            display_rect = rect
            clip_path = QPainterPath()
            frame_style = self.source.album_frame_style if self.source.source_type is SourceType.ALBUM_COVER else "rounded"
            if frame_style == "circle":
                painter.drawEllipse(rect)
                clip_path.addEllipse(rect)
            elif frame_style == "polaroid":
                painter.setPen(QPen(QColor("#FFFFFF"), max(1.0, self.source.outline_width)))
                painter.setBrush(QColor("#F8FAFC"))
                painter.drawRoundedRect(rect, 5, 5)
                display_rect = rect.adjusted(14, 14, -14, -46)
                clip_path.addRect(display_rect)
            else:
                painter.drawRoundedRect(rect, self.source.border_radius, self.source.border_radius)
                clip_path.addRoundedRect(rect, self.source.border_radius, self.source.border_radius)
            painter.setClipPath(clip_path)
            if self.source.image_fit_mode == "stretch":
                target = display_rect
            else:
                ratio = self._pixmap.width() / max(1, self._pixmap.height())
                rect_ratio = display_rect.width() / max(1, display_rect.height())
                contain = self.source.image_fit_mode == "contain"
                width_limited = ratio > rect_ratio
                if contain == width_limited:
                    target = QRectF(display_rect.left(), display_rect.center().y() - display_rect.width() / ratio / 2,
                                    display_rect.width(), display_rect.width() / ratio)
                else:
                    target = QRectF(display_rect.center().x() - display_rect.height() * ratio / 2, display_rect.top(),
                                    display_rect.height() * ratio, display_rect.height())
            painter.drawPixmap(target, self._pixmap, self._pixmap.rect())
            painter.setClipping(False)
            if frame_style == "glass":
                painter.setBrush(QColor(255, 255, 255, 40))
                painter.setPen(QPen(QColor(255, 255, 255, 180), 1.5))
                painter.drawRoundedRect(rect, self.source.border_radius, self.source.border_radius)
        elif self.source.source_type is SourceType.VIDEO:
            painter.setClipping(True)
            clip_path = QPainterPath()
            clip_path.addRoundedRect(rect, self.source.border_radius, self.source.border_radius)
            painter.setClipPath(clip_path)
            painter.fillRect(rect, QColor("#10151D"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 210))
            size = min(rect.width(), rect.height()) * 0.22
            center = rect.center()
            play = QPainterPath()
            play.moveTo(center.x() - size * 0.35, center.y() - size * 0.55)
            play.lineTo(center.x() + size * 0.55, center.y())
            play.lineTo(center.x() - size * 0.35, center.y() + size * 0.55)
            play.closeSubpath()
            painter.drawPath(play)
            painter.setClipping(False)
            paths = self.source.video_paths
            filename = Path(paths[0]).name if paths else "No video selected"
            suffix = f"  +{len(paths) - 1}" if len(paths) > 1 else ""
            painter.setPen(QColor("#D8E1EE"))
            painter.drawText(
                rect.adjusted(10, 10, -10, -10),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                filename + suffix,
            )
        elif self.source.source_type is SourceType.BACKGROUND:
            # Color, gradient, and unavailable-image backgrounds are visual-only.
            # Never fall through to the generic text renderer with the name "Background".
            painter.drawRoundedRect(rect, self.source.border_radius, self.source.border_radius)
        elif self.source.source_type is SourceType.SHAPE:
            if self.source.shape_kind == "circle":
                painter.drawEllipse(rect)
            elif self.source.shape_kind == "line":
                painter.setPen(QPen(QColor(self.source.fill_color),
                                    max(1.0, self.source.height)))
                painter.drawLine(rect.left(), rect.center().y(), rect.right(),
                                 rect.center().y())
            else:
                painter.drawRoundedRect(rect, self.source.border_radius,
                                        self.source.border_radius)
        elif self.source.source_type is SourceType.PROGRESS_BAR:
            style = self.source.progress_style
            radius = 0.0 if style == "youtube" else rect.height() / 2
            track_color = QColor(self.source.progress_track_color)
            if style == "apple":
                track_color = QColor(self.source.fill_color)
                track_color.setAlpha(80)
            elif style == "spotify":
                track_color = QColor("#1B2530")
            painter.setBrush(track_color)
            painter.drawRoundedRect(rect, radius, radius)
            painter.setBrush(fill)
            progress_width = rect.width() * max(0.0, min(1.0, self.source.progress_value))
            if style == "apple":
                progress_width = rect.width() * max(0.0, min(1.0, self.source.progress_value))
            painter.drawRoundedRect(
                QRectF(0, 0, progress_width, rect.height()),
                radius,
                radius,
            )
            if style == "spotify":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QPointF(progress_width, rect.center().y()), rect.height() * 0.34,
                                    rect.height() * 0.34)
        elif self.source.source_type is SourceType.ALBUM_COVER:
            painter.drawRoundedRect(rect, self.source.border_radius,
                                    self.source.border_radius)
            painter.setPen(QPen(QColor("#FFFFFF"), 2))
            painter.drawEllipse(rect.center(), rect.width() * 0.16,
                                rect.width() * 0.16)
        elif self.source.source_type is SourceType.AUDIO_VISUALIZER:
            painter.setPen(Qt.PenStyle.NoPen)
            bar_count = max(4, min(96, self.source.visualizer_bars))
            gap = max(1.0, rect.width() * 0.012 / bar_count)
            bar_width = max(1.0, (rect.width() - gap * (bar_count - 1)) / bar_count)
            minimum = max(0.0, min(0.5, self.source.visualizer_min_level))
            maximum = max(minimum, min(1.0, self.source.visualizer_max_level))
            curve = max(0.25, min(3.0, self.source.visualizer_curve))
            # The editor has no playback signal, so show a representative design
            # sample.  Real preview/export frames use the audio analysis and are
            # completely flat at silence when Minimum level is zero.
            levels = [
                minimum + (maximum - minimum)
                * (0.16 + 0.76 * abs(sin(index * 0.61 + 0.8))) ** curve
                for index in range(bar_count)
            ]
            style = self.source.visualizer_style
            if style in {"line", "wave"}:
                painter.setPen(QPen(QColor(self.source.fill_color), max(
                    self.source.visualizer_line_width, rect.height() * 0.025
                )))
                path = QPainterPath(QPointF(rect.left(), rect.center().y()))
                for index, level in enumerate(levels):
                    x = rect.left() + index * rect.width() / max(1, bar_count - 1)
                    y = rect.center().y() - (level - 0.5) * rect.height() * 0.82
                    if style == "wave":
                        y = rect.center().y() - sin(index * 0.42) * level * rect.height() * 0.36
                    path.lineTo(x, y)
                painter.drawPath(path)
            elif style == "arc":
                painter.setBrush(Qt.BrushStyle.NoBrush)
                for index, level in enumerate(levels):
                    inset = index * min(rect.width(), rect.height()) / max(1, bar_count * 3.2)
                    arc_rect = rect.adjusted(inset, inset, -inset, -inset)
                    arc_color = QColor(self.source.fill_color)
                    arc_color.setAlpha(max(45, int(255 * level)))
                    painter.setPen(QPen(arc_color, max(1.0, self.source.visualizer_line_width * 0.75)))
                    painter.drawArc(arc_rect, 210 * 16, int(120 * 16 * level))
            else:
                for index, level in enumerate(levels):
                    bar_height = rect.height() * level
                    x = rect.left() + index * (bar_width + gap)
                    if style == "led":
                        segments = 8
                        segment_gap = max(1.0, rect.height() * 0.025)
                        segment_height = (rect.height() - segment_gap * (segments - 1)) / segments
                        active = max(1, round(level * segments))
                        for segment in range(active):
                            y = rect.bottom() - (segment + 1) * segment_height - segment * segment_gap
                            painter.setBrush(fill)
                            painter.drawRoundedRect(QRectF(x, y, bar_width, segment_height), 2, 2)
                        continue
                    if style == "center":
                        bar_height = rect.height() * level
                        painter.setBrush(fill)
                        painter.drawRoundedRect(QRectF(x, rect.bottom() - bar_height, bar_width, bar_height), bar_width / 2, bar_width / 2)
                        continue
                    if style == "mirror":
                        bar_height *= 0.48
                        y = rect.center().y() - bar_height
                        painter.setBrush(fill)
                        painter.drawRoundedRect(QRectF(x, y, bar_width, bar_height), bar_width / 2, bar_width / 2)
                        painter.drawRoundedRect(QRectF(x, rect.center().y(), bar_width, bar_height), bar_width / 2, bar_width / 2)
                        continue
                    if style == "dots":
                        dot_size = max(3.0, min(bar_width * 1.35, rect.height() * 0.16))
                        dot_count = max(1, int(level * 7))
                        for dot_index in range(dot_count):
                            y = rect.bottom() - dot_size - dot_index * (dot_size + 3)
                            painter.setBrush(fill)
                            painter.drawEllipse(QRectF(x, y, dot_size, dot_size))
                        continue
                    y = rect.center().y() - bar_height / 2
                    if style == "spectrum":
                        spectrum_color = QColor.fromHsv(int(300 * index / max(1, bar_count - 1)), 210, 245)
                        painter.setBrush(spectrum_color)
                    else:
                        painter.setBrush(fill)
                    draw_width = bar_width * 0.62 if style == "capsule" else bar_width
                    radius = draw_width / 2
                    painter.drawRoundedRect(QRectF(x + (bar_width - draw_width) / 2, y, draw_width, bar_height), radius, radius)
        elif self.source.source_type is SourceType.AUDIO_WAVEFORM:
            painter.setPen(QPen(QColor(self.source.fill_color), max(1.0, self.source.visualizer_line_width)))
            path = QPainterPath(QPointF(rect.left(), rect.center().y()))
            points = max(24, min(128, self.source.visualizer_bars))
            for index in range(points):
                x = rect.left() + index * rect.width() / max(1, points - 1)
                level = 0.22 + 0.64 * abs(sin(index * 0.38 + 0.8))
                y = rect.center().y() - sin(index * 0.72) * level * rect.height() * 0.36
                path.lineTo(x, y)
            painter.drawPath(path)
        elif self.source.source_type is SourceType.AUDIO_LEVEL_METER:
            legacy_led = self.source.level_meter_mode == "led"
            channels = 1 if self.source.level_meter_mode == "mono" else 2
            sample_levels = (0.72,) if channels == 1 else (0.72, 0.54)
            sample_peaks = (0.82,) if channels == 1 else (0.82, 0.66)
            paint_level_meter(
                painter,
                rect,
                sample_levels,
                sample_peaks,
                style="led" if legacy_led else self.source.level_meter_style,
                orientation=self.source.level_meter_orientation,
                segments=self.source.level_meter_segments,
                gap=self.source.level_meter_gap,
                track_color=self.source.level_meter_track_color,
                low_color=self.source.level_meter_low_color,
                mid_color=self.source.level_meter_mid_color,
                high_color=self.source.level_meter_high_color,
                show_peak=self.source.level_meter_show_peak,
            )
        elif self.source.source_type is SourceType.PARTICLE_OVERLAY:
            paint_particles(
                painter,
                rect,
                style=self.source.particle_style,
                color=self.source.fill_color,
                secondary_color=self.source.particle_secondary_color,
                density=self.source.particle_density,
                speed=self.source.particle_speed,
                minimum_size=self.source.particle_min_size,
                maximum_size=self.source.particle_max_size,
                particle_opacity=self.source.particle_opacity,
                direction=self.source.particle_direction,
                drift=self.source.particle_drift,
                twinkle=self.source.particle_twinkle,
                glow=self.source.particle_glow,
                seed=self.source.particle_seed,
            )
        elif self.source.source_type is SourceType.LYRICS:
            painter.drawRoundedRect(rect, self.source.border_radius, self.source.border_radius)
            lyrics_renderer.paint_lines(self, painter, rect)
        elif self.source.source_type is SourceType.TRACK_LIST:
            self._paint_track_list(painter, rect)
        elif self.source.source_type is SourceType.NOW_PLAYING:
            now_playing_renderer.paint_card(self, painter, rect)
        else:
            painter.drawRoundedRect(rect, self.source.border_radius,
                                    self.source.border_radius)
            text_color = (
                self.source.outline_color
                if self.source.source_type in {SourceType.TEXT, SourceType.TIME, SourceType.LYRICS,
                                               SourceType.TRACK_LIST}
                else "#FFFFFF"
            )
            painter.setPen(QColor(text_color))
            font = QFont(self.source.font_family,
                         max(8, min(120, int(self.source.font_size))))
            font.setWeight(QFont.Weight(self.source.font_weight))
            painter.setFont(font)
            alignment = {
                "left": Qt.AlignmentFlag.AlignLeft,
                "right": Qt.AlignmentFlag.AlignRight,
            }.get(self.source.text_alignment, Qt.AlignmentFlag.AlignHCenter)
            text_rect = rect.adjusted(12, 6, -12, -6)
            text = self._render_text() or self.source.name
            flags = alignment | Qt.AlignmentFlag.AlignVCenter
            overflow_types = {SourceType.TEXT, SourceType.TRACK_LIST}
            if self.source.source_type in overflow_types and self.source.text_overflow != "wrap":
                lines = (
                    text.splitlines() or [""]
                    if self.source.source_type is SourceType.TRACK_LIST else
                    [" ".join(text.splitlines())]
                )
                metrics = painter.fontMetrics()
                line_height = max(1, metrics.height())
                block_height = line_height * len(lines)
                top = max(text_rect.top(), text_rect.center().y() - block_height / 2)
                painter.save()
                painter.setClipRect(text_rect)
                for index, line in enumerate(lines):
                    if self.source.text_overflow == "ellipsis":
                        line = metrics.elidedText(
                            line,
                            Qt.TextElideMode.ElideRight,
                            max(1, int(text_rect.width())),
                        )
                    line_rect = QRectF(
                        text_rect.left(), top + index * line_height,
                        text_rect.width(), line_height,
                    )
                    self._draw_text(
                        painter,
                        line_rect,
                        alignment | Qt.AlignmentFlag.AlignVCenter
                        | Qt.TextFlag.TextSingleLine,
                        line,
                    )
                painter.restore()
            else:
                self._draw_text(
                    painter,
                    text_rect,
                    flags | Qt.TextFlag.TextWordWrap,
                    text,
                )
        painter.restore()

        scene = self.scene()
        multi_selected = bool(
            scene is not None and hasattr(scene, "has_multi_selection")
            and scene.has_multi_selection()  # type: ignore[attr-defined]
        )
        if self.isSelected() and not multi_selected:
            guide_color = QColor("#55B8FF")
            if self.source.opacity <= 0.25:
                guide_color = QColor(255, 255, 255, 190)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(guide_color, 1.5,
                                Qt.PenStyle.DashLine))
            painter.drawRect(rect)
            painter.setPen(QPen(QColor("#FFFFFF"), 1.0))
            painter.setBrush(guide_color)
            for handle_rect in self.resize_handle_rects().values():
                painter.drawRect(handle_rect)
            painter.setPen(QPen(guide_color, 1.5))
            painter.drawLine(QPointF(self.source.width / 2, 0),
                             QPointF(self.source.width / 2, -23))
            painter.setBrush(guide_color)
            painter.drawEllipse(self.rotation_handle_rect())

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Begin a resize or rotation from one of the selection handles."""
        position = event.pos()
        edit_handle = self._next_press_edit_handle
        self._next_press_edit_handle = None
        if edit_handle is None:
            edit_handle = self.edit_handle_at(position)
        if edit_handle == "rotate":
            self.setCursor(self.cursor_for_edit_handle(edit_handle, self.rotation()))
            self._rotating = True
            self._begin_user_interaction()
            scene = self.scene()
            if scene is not None and hasattr(scene, "begin_item_interaction"):
                scene.begin_item_interaction(self)  # type: ignore[attr-defined]
            self._rotation_center_scene = self.mapToScene(self.transformOriginPoint())
            self._last_rotation_angle = self._scene_angle(event.scenePos())
            self._raw_rotation = self.source.rotation
            event.accept()
            return
        if edit_handle is not None:
            self.setCursor(self.cursor_for_edit_handle(edit_handle, self.rotation()))
            self._resizing = True
            self._begin_user_interaction()
            scene = self.scene()
            if scene is not None and hasattr(scene, "begin_item_interaction"):
                scene.begin_item_interaction(self)  # type: ignore[attr-defined]
            self._resize_origin_scene = event.scenePos()
            self._resize_size = (self.source.width, self.source.height)
            self._resize_handle = edit_handle
            self._resize_anchor_scene = self.mapToScene(
                self._anchor_point_for_size(
                    edit_handle, self.source.width, self.source.height,
                )
            )
            event.accept()
            return
        if (not self.source.locked and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._duplicate_on_release = True
            self._duplicate_dragged = False
            self._duplicate_origin = QPointF(self.pos())
        super().mousePressEvent(event)
        if (not self.source.locked and event.button() == Qt.MouseButton.LeftButton
                and not self._rotating and not self._resizing):
            scene = self.scene()
            if scene is not None and hasattr(scene, "begin_item_interaction"):
                scene.begin_item_interaction(  # type: ignore[attr-defined]
                    self, include_selection=not self._duplicate_on_release,
                )

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Resize while dragging the handle, otherwise use normal item movement."""
        if self._rotating:
            current_angle = self._scene_angle(event.scenePos())
            self._raw_rotation += self._normalized_angle_delta(
                current_angle - self._last_rotation_angle
            )
            self._last_rotation_angle = current_angle
            rotation = self._raw_rotation
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                rotation = round(rotation / 15.0) * 15.0
            self.source.rotation = rotation % 360.0
            self.setRotation(self.source.rotation)
            self._queue_user_changes({"rotation": self.source.rotation})
            self.interaction_hint.emit(f"{self.source.rotation:.0f}°")
            event.accept()
            return
        if self._resizing:
            delta = self._resize_delta(event.scenePos())
            handle = self._resize_handle or "se"
            width = self._resize_size[0]
            height = self._resize_size[1]
            if "e" in handle:
                width += delta.x()
            elif "w" in handle:
                width -= delta.x()
            if "s" in handle:
                height += delta.y()
            elif "n" in handle:
                height -= delta.y()
            width = max(32.0, width)
            height = max(24.0, height)
            aspect_ratio = self._resize_aspect_ratio(event.modifiers())
            aspect_driver = (
                self._resize_aspect_driver(width, height, handle)
                if aspect_ratio is not None else None
            )
            scene = self.scene()
            if scene is not None and hasattr(scene, "snap_resize"):
                width, height = scene.snap_resize(  # type: ignore[no-any-return]
                    self, width, height, handle, event.modifiers(),
                )
            if aspect_ratio is not None and aspect_driver is not None:
                snapped_width, snapped_height = width, height
                width, height = self._constrain_resize_aspect(
                    width, height, aspect_ratio, aspect_driver,
                )
                # Aspect/minimum limits can move an edge away from its guide.
                if scene is not None:
                    if hasattr(scene, "guide_y") and (
                        aspect_driver == "width" or abs(height - snapped_height) > 1e-6
                    ):
                        scene.guide_y = None  # type: ignore[attr-defined]
                    if hasattr(scene, "guide_x") and (
                        aspect_driver == "height" or abs(width - snapped_width) > 1e-6
                    ):
                        scene.guide_x = None  # type: ignore[attr-defined]
            self._resize_to(width, height, handle)
            if scene is not None and hasattr(scene, "update_alignment_guides"):
                scene.update_alignment_guides(self)  # type: ignore[attr-defined]
            self.interaction_hint.emit(
                f"{self.source.width:.0f} × {self.source.height:.0f}"
            )
            event.accept()
            return
        scene = self.scene()
        if (scene is not None and hasattr(scene, "move_interactive_items")
                and scene.is_item_interactive(self)):
            scene.move_interactive_items(
                event.scenePos() - event.buttonDownScenePos(Qt.MouseButton.LeftButton),
                event.modifiers(),
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Finish an active resize."""
        if self._rotating:
            self._rotating = False
            self.unsetCursor()
            self.interaction_hint.emit("")
            scene = self.scene()
            if scene is not None and hasattr(scene, "finish_item_interaction"):
                scene.finish_item_interaction()  # type: ignore[attr-defined]
            else:
                self._commit_user_interaction()
            event.accept()
            return
        if self._resizing:
            self._resizing = False
            self._resize_handle = None
            self.unsetCursor()
            self.interaction_hint.emit("")
            scene = self.scene()
            if scene is not None and hasattr(scene, "finish_item_interaction"):
                scene.finish_item_interaction()  # type: ignore[attr-defined]
            else:
                self._commit_user_interaction()
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if self._duplicate_on_release:
            self._duplicate_on_release = False
            if self._duplicate_dragged:
                dropped_position = QPointF(self.pos())
                self._suppress_position_sync = True
                try:
                    self.setPos(self._duplicate_origin)
                finally:
                    self._suppress_position_sync = False
                self.source.x = self._duplicate_origin.x()
                self.source.y = self._duplicate_origin.y()
                self._queue_user_changes({"x": self.source.x, "y": self.source.y})
                self.duplicate_requested.emit(
                    self.source.id, dropped_position.x(), dropped_position.y()
                )
        scene = self.scene()
        if scene is not None and hasattr(scene, "finish_item_interaction"):
            scene.finish_item_interaction()  # type: ignore[attr-defined]
        else:
            self._commit_user_interaction()

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Open the matching editor for this source on a left double-click."""
        if event.button() == Qt.MouseButton.LeftButton and not self.source.locked:
            self.edit_requested.emit(self.source.id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _scene_angle(self, scene_position: QPointF) -> float:
        """Return a stable mouse angle around the fixed scene-space centre."""
        return degrees(atan2(
            scene_position.y() - self._rotation_center_scene.y(),
            scene_position.x() - self._rotation_center_scene.x(),
        ))

    @staticmethod
    def _normalized_angle_delta(delta: float) -> float:
        """Normalize an angular change to the shortest path across ±180 degrees."""
        return (delta + 180.0) % 360.0 - 180.0

    def _resize_to(self, width: float, height: float, handle: str = "se") -> None:
        """Resize while keeping the handle's opposite edge or corner fixed."""
        anchor_before = self._resize_anchor_scene
        if anchor_before.isNull():
            anchor_before = self.mapToScene(
                self._anchor_point_for_size(handle, self.source.width, self.source.height)
            )
        self.prepareGeometryChange()
        self.source.width = width
        self.source.height = height
        self._sync_transform_origin()
        anchor_after = self.mapToScene(self._anchor_point_for_size(handle, width, height))
        self._suppress_position_sync = True
        try:
            self.setPos(self.pos() + anchor_before - anchor_after)
        finally:
            self._suppress_position_sync = False
        position = self.pos()
        self.source.x = position.x()
        self.source.y = position.y()
        self.update()
        self._queue_user_changes({
            "width": width, "height": height,
            "x": position.x(), "y": position.y(),
        })

    def _begin_user_interaction(self) -> None:
        self._pending_user_changes.clear()

    def _queue_user_changes(self, changes: dict[str, object]) -> None:
        self._pending_user_changes.update(changes)

    def _commit_user_interaction(self) -> None:
        if not self._pending_user_changes:
            return
        changes = dict(self._pending_user_changes)
        self._pending_user_changes.clear()
        self.changed_by_user.emit(self.source.id, changes)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: object) -> object:
        """Send source positions back to the store after user movement."""
        if change is QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            if self._suppress_position_sync:
                return super().itemChange(change, value)
            position = value
            if isinstance(position, QPointF):
                if self._duplicate_on_release:
                    self._duplicate_dragged = True
                scene = self.scene()
                changes = {"x": position.x(), "y": position.y()}
                self.source.x = position.x()
                self.source.y = position.y()
                if (scene is not None and hasattr(scene, "is_item_interactive")
                        and scene.is_item_interactive(self)):  # type: ignore[attr-defined]
                    self._queue_user_changes(changes)
                else:
                    self.changed_by_user.emit(self.source.id, changes)
                if scene is not None and hasattr(scene, "update_alignment_guides"):
                    scene.update_alignment_guides(self)
        if change is QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            self.prepareGeometryChange()
        if change is QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)


def _render_legacy_source(
    item: SourceItem, painter: QPainter, option: object, widget: object | None,
) -> None:
    item._paint_legacy(painter, option, widget)


# Bind in the UI layer: core model imports must not initialize Qt multimedia.
# Phase 7 split every SourceType onto its own dedicated renderer below;
# _paint_legacy keeps every type's code so tests/test_source_registry.py can
# keep comparing registered output against the legacy reference pixel-for-
# pixel, but _render_legacy_source is no longer bound to any type here.
source_registry.get(SourceType.SHAPE).renderer = shape_renderer.render
source_registry.get(SourceType.PROGRESS_BAR).renderer = progress_renderer.render
source_registry.get(SourceType.BACKGROUND).renderer = background_renderer.render
source_registry.get(SourceType.ALBUM_COVER).renderer = album_cover_renderer.render
source_registry.get(SourceType.IMAGE).renderer = image_renderer.render
source_registry.get(SourceType.LOGO).renderer = logo_renderer.render
source_registry.get(SourceType.WATERMARK).renderer = watermark_renderer.render
source_registry.get(SourceType.VIDEO).renderer = video_renderer.render
source_registry.get(SourceType.TEXT).renderer = text_renderer.render
source_registry.get(SourceType.TIME).renderer = time_renderer.render
source_registry.get(SourceType.AUDIO_VISUALIZER).renderer = audio_visualizer_renderer.render
source_registry.get(SourceType.AUDIO_WAVEFORM).renderer = audio_waveform_renderer.render
source_registry.get(SourceType.AUDIO_LEVEL_METER).renderer = audio_level_meter_renderer.render
source_registry.get(SourceType.PARTICLE_OVERLAY).renderer = particle_overlay_renderer.render
source_registry.get(SourceType.LYRICS).renderer = lyrics_renderer.render
source_registry.get(SourceType.TRACK_LIST).renderer = track_list_renderer.render
source_registry.get(SourceType.NOW_PLAYING).renderer = now_playing_renderer.render
