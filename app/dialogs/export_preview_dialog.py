"""Full-playlist, audio-aware pre-export preview dialog."""

from __future__ import annotations

import logging
from bisect import bisect_right
import math
from math import ceil
import os
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
from time import monotonic
from dataclasses import replace
from weakref import WeakSet

import numpy as np

from PySide6.QtCore import QElapsedTimer, QPointF, QRect, QRectF, QSize, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QResizeEvent, QShortcut,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedLayout,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from app.animation.curves import motion_padding
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.adaptive_quality import AdaptivePreviewQuality
from app.preview.gpu_texture_surface import (
    GPU_TEXTURE_SURFACE_AVAILABLE, GpuBackendInfo, GpuColorFilter, GpuPreviewLayer,
    GpuTexturePreviewSurface,
)
from app.preview.gpu_health import GpuPreviewHealth
from app.preview.text_template import format_timestamp
from app.controllers.preview_audio_controller import PreviewAudioController
from app.widgets.audio_sync_warning import AudioSyncWarning
from app.renderer.ffmpeg_renderer import FFmpegRenderer, VisualizerOverlay
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.services.source_store import SourceStore
from app.services.preview_audio_settings import preview_volume, save_preview_volume
from app.services.playlist_service import PlaylistService
from app.timeline.compiler import compile_playlist
from app.timeline.crossfade import compile_crossfade
from app.preview.live_mix_player import LiveMixPlayer
from app.timeline.models import TransitionType
from app.timeline.render_plan import (
    AudioRenderTransition, CompiledRenderPlan, reactive_layer_windows, visual_segment_at,
    visual_segments,
)
from app.preview.frame_state import (
    MixJunction, resolve_mix_phase, segment_junction,
)
from app.preview.album_art import prewarm_cover_thumbnail
from app.video.timeline import resolve_video_position, source_video_paths
from app.video.frame_filter import VideoFrameFilterSettings, filter_video_frame
from app.video.decoder_backpressure import VideoDecoderBackpressure
from app.video.preview_proxy import PreviewProxyCache, PreviewProxyWorker
from app.utils.i18n import Translator
from app.utils.performance import ENABLED as PROFILE_ENABLED, PROFILE, timed
from app.widgets.activity_progress import activity_for
from app.widgets.automix_details_panel import AutoMixDetailsPanel, ready_through
from app.widgets.transition_inspector import TransitionInspectorWindow

TIMELINE_SCALE = 100
VIDEO_PROXY_ACTIVITY = "preview_video_proxy"
AUDIO_ANALYSIS_ACTIVITY = "preview_audio_analysis"
_BLENDED_AUDIO_TRACK_INDEX = -2
_LIVE_MIX_TRACK_INDEX = -3
"""Sentinel for `_active_track_index` when playing the pre-rendered AutoMix/crossfade mix."""


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
LOGGER = logging.getLogger(__name__)
OverlaySignature = tuple[int, ...]


class PreviewCoverWorker(QThread):
    """Read artwork off the GUI thread; only the receiver creates icons."""

    ready = Signal(int, QImage)

    def __init__(self, tracks, parent=None):
        super().__init__(parent)
        self.paths = tuple((track.file_path, track.cover_path) for track in tracks)

    def run(self) -> None:
        for index, (audio_path, cover_path) in enumerate(self.paths):
            if self.isInterruptionRequested():
                return
            try:
                image = prewarm_cover_thumbnail(audio_path, cover_path)
            except Exception as error:
                LOGGER.warning("Preview cover could not load for %s: %s", audio_path, error)
                continue
            if self.isInterruptionRequested():
                return
            if not image.isNull():
                self.ready.emit(index, image)


class CpuPreviewSurface(QWidget):
    """CPU preview surface that scales during paint instead of copying a pixmap."""

    frame_presented = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.image = QImage()
        self._target_key: tuple[int, int, int, int] | None = None
        self._target_rect = QRect()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def set_image(self, image: QImage) -> None:
        """Present the latest frame without constructing a scaled QPixmap copy."""
        self.image = image
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111820"))
        if not self.image.isNull():
            target_key = (self.image.width(), self.image.height(), self.width(), self.height())
            if self._target_key != target_key:
                size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
                x = (self.width() - size.width()) // 2
                y = (self.height() - size.height()) // 2
                self._target_rect = QRect(x, y, size.width(), size.height())
                self._target_key = target_key
            needs_scaling = self._target_rect.size() != self.image.size()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, needs_scaling)
            painter.drawImage(self._target_rect, self.image)
        painter.end()
        self.frame_presented.emit()


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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.schedule = schedule
        self._transition_regions = _transition_display_regions(transitions)
        self._dragging = False
        self.setMinimumHeight(46)

    def set_schedule(
        self, schedule: tuple[tuple[int, PlaylistTrack, float, float], ...],
        transitions: tuple[AudioRenderTransition, ...] = (),
    ) -> None:
        """Replace track boundaries and the transition overlay, then repaint."""
        self.schedule = schedule
        self._transition_regions = _transition_display_regions(transitions)
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        """One block per track (the playing one outlined, the part heard filled),
        transitions in amber across the joins, and the playhead on top."""
        total = max(0.01, self.maximum() / TIMELINE_SCALE)
        current_seconds = self.value() / TIMELINE_SCALE
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        accent = palette.highlight().color()
        usable_width = max(1, self.width() - 18)

        def x_at(seconds: float) -> float:
            return 9 + seconds / total * usable_width

        top, height = 8.0, max(16.0, self.height() - 18.0)
        played_x = x_at(current_seconds)
        for index, (_schedule_index, track, start, end) in enumerate(self.schedule, start=1):
            active = start <= current_seconds < end or (
                index == len(self.schedule) and current_seconds >= start
            )
            block = QRectF(x_at(start) + 1, top, max(2.0, x_at(end) - x_at(start) - 2), height)
            painter.setPen(QPen(accent, 1.2) if active else Qt.PenStyle.NoPen)
            painter.setBrush(palette.alternateBase())
            painter.drawRoundedRect(block, 4, 4)
            if played_x > block.left():
                heard = QColor(accent)
                heard.setAlpha(70 if active else 38)
                painter.save()
                painter.setClipRect(QRectF(block.left(), top, played_x - block.left(), height))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(heard)
                painter.drawRoundedRect(block, 4, 4)
                painter.restore()
            if block.width() >= 28:
                label = f"{index:02d}  {track.title or Path(track.file_path).stem}"
                painter.setPen(palette.text().color() if active else palette.placeholderText().color())
                text_rect = block.adjusted(7, 0, -5, 0)
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight,
                                                                  int(text_rect.width())))
        amber = QColor("#F5C66B")
        for start, end, _transition_type in self._transition_regions:
            x0, x1 = x_at(start), x_at(end)
            if x1 - x0 < 1:
                continue
            painter.setPen(Qt.PenStyle.NoPen)
            amber.setAlpha(46)
            painter.setBrush(amber)
            painter.drawRect(QRectF(x0, top, x1 - x0, height))
            amber.setAlpha(220)
            painter.setBrush(amber)
            painter.drawRoundedRect(QRectF(x0, top + height + 3, x1 - x0, 3), 1.5, 1.5)
        painter.setPen(QPen(palette.text().color(), 1.6))
        painter.drawLine(QPointF(played_x, top - 5), QPointF(played_x, top + height + 4))
        painter.setPen(QPen(palette.window().color(), 1.5))
        painter.setBrush(palette.text().color())
        painter.drawEllipse(QPointF(played_x, top - 3), 4.5, 4.5)
        painter.end()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.setSliderDown(True)
            self._set_value_from_position(event.position().x())
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
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _set_value_from_position(self, x_position: float) -> None:
        ratio = max(0.0, min(1.0, (x_position - 9) / max(1, self.width() - 18)))
        self.setValue(round(self.minimum() + ratio * (self.maximum() - self.minimum())))


def _track_details(track: PlaylistTrack, separator: str) -> str:
    """Artist and album, else the file name: the "Unknown ..." placeholders are stored defaults, not information."""
    return separator.join(
        value for value, placeholder in ((track.artist, "Unknown Artist"), (track.album, "Unknown Album"))
        if value and value != placeholder
    ) or Path(track.file_path).name


TRACK_ROW_ROLE = Qt.ItemDataRole.UserRole + 2
"""(detail, duration) text of a Preview track-list row; its title is the item text."""


class PreviewTrackDelegate(QStyledItemDelegate):
    """A Preview track row: cover or number tile, title over details, duration on the right."""

    def paint(self, painter: QPainter, option, index) -> None:  # type: ignore[override]
        palette = option.palette
        accent = palette.highlight().color()
        rect = QRectF(option.rect).adjusted(0, 1, -2, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected or hovered:
            fill = QColor(accent) if selected else palette.alternateBase().color()
            if selected:
                fill.setAlpha(46)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 5, 5)
        if selected:
            painter.setBrush(accent)
            painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 8, 3, rect.height() - 16), 1.5, 1.5)
        tile = QRectF(rect.left() + 10, rect.center().y() - 18, 36, 36)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        number = f"{index.row() + 1:02d}"
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, tile.toRect())
        else:
            painter.setBrush(QColor("#304B44") if selected else palette.alternateBase())
            painter.drawRoundedRect(tile, 5, 5)
            painter.setPen(accent if selected else palette.placeholderText().color())
            painter.drawText(tile, Qt.AlignmentFlag.AlignCenter, number)
        detail, duration = index.data(TRACK_ROW_ROLE) or ("", "")
        metrics = painter.fontMetrics()
        duration_width = metrics.horizontalAdvance(duration) + 4
        text_left = tile.right() + 10
        title_rect = QRectF(text_left, rect.top() + 8, rect.right() - text_left - duration_width - 10, 18)
        bold = QFont(option.font)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(palette.text().color())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(bold).elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "",
                                                       Qt.TextElideMode.ElideRight, int(title_rect.width())))
        painter.setFont(option.font)
        painter.setPen(palette.placeholderText().color())
        painter.drawText(QRectF(rect.right() - duration_width - 8, title_rect.top(), duration_width, 18),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, duration)
        detail_rect = QRectF(text_left, title_rect.bottom() + 1, rect.right() - text_left - 8, 16)
        painter.drawText(detail_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(detail, Qt.TextElideMode.ElideRight, int(detail_rect.width())))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # type: ignore[override]
        return QSize(0, 54)


class AudioAnalysisWorker(QThread):
    """Build audio-reactive preview frames without blocking the playback UI."""

    ready = Signal(str, int, object)
    failed = Signal(str)

    def __init__(self, renderer: PythonVisualizerRenderer, track: PlaylistTrack,
                 bands: int, fps: int = 30, needs_waveform: bool = False,
                 overlays: tuple[VisualizerOverlay, ...] = (),
                 parent: QWidget | None = None, *, music_profiles: tuple = (),
                 cached_analysis: dict | None = None) -> None:
        super().__init__(parent)
        self.renderer = renderer
        self.track = track
        self.bands = bands
        self.fps = fps
        self.needs_waveform = needs_waveform
        self.overlays = overlays
        self.cancel_event = threading.Event()
        self.music_profiles = music_profiles
        self.cached_analysis = cached_analysis

    def cancel(self) -> None:
        """Request cancellation before the next analysis block."""
        self.cancel_event.set()

    def run(self) -> None:
        try:
            payload = dict(self.cached_analysis) if self.cached_analysis is not None else self._analyze_audio()
            payload["music_envelopes"] = {
                profile: self.renderer.music_envelope(payload["levels"], self.fps, profile, self.cancel_event)
                for profile in self.music_profiles
            }
        except Exception as error:
            if not self.cancel_event.is_set():
                self.failed.emit(str(error))
            return
        if not self.cancel_event.is_set():
            self.ready.emit(self.track.id, self.fps, payload)

    def _analyze_audio(self) -> dict:
        needs_meter = any(overlay.kind == "level_meter" for overlay in self.overlays)
        stereo = (
            self.renderer._decode_stereo_audio(
                Path(self.track.file_path), self.cancel_event,
            )
            if needs_meter else None
        )
        samples = (
            np.mean(stereo, axis=1, dtype=np.float32)
            if stereo is not None else
            self.renderer._decode_mono_audio(Path(self.track.file_path), self.cancel_event)
        )
        levels = self.renderer._analyze_levels(samples, self.fps, self.bands, self.cancel_event)
        waveform = (
            self.renderer._analyze_waveform(samples, self.fps, max(32, self.bands), self.cancel_event)
            if self.needs_waveform else np.zeros((1, max(32, self.bands)), dtype=np.float32)
        )
        processed = tuple(
            (
                np.zeros((1, 1), dtype=np.float32)
                if overlay.kind in {"particles", "level_meter"} else
                self.renderer.process_level_sequence(
                    waveform if overlay.kind == "waveform" else levels,
                    overlay,
                    max(4, min(96, overlay.bar_count)),
                )
            )
            for overlay in self.overlays
        )
        raw_meter = (
            np.column_stack((
                self.renderer._analyze_rms(stereo[:, 0], self.fps, self.cancel_event),
                self.renderer._analyze_rms(stereo[:, 1], self.fps, self.cancel_event),
            ))
            if stereo is not None else None
        )
        meters = tuple(
            self.renderer.process_meter_sequence(raw_meter, overlay, self.fps)
            if overlay.kind == "level_meter" and raw_meter is not None else None
            for overlay in self.overlays
        )
        return {"levels": levels, "waveform": waveform,
                "bass": self.renderer.bass_envelope(levels, self.fps),
                "processed": processed, "meters": meters}


class VideoDurationProbeWorker(QThread):
    """Resolve one media duration without blocking preview interaction."""

    ready = Signal(str, float)

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path

    def run(self) -> None:
        duration = PlaylistService._probe_duration(Path(self.path))
        if not self.isInterruptionRequested():
            self.ready.emit(self.path, duration)


class OverlayFrameWorker(QThread):
    """Pre-render a small run of pure-QImage overlay frames off the UI thread."""

    ready = Signal(str, int, int, object, object)
    failed = Signal(str)

    def __init__(self, track_id: str, fps: int, generation: int, start_frame: int, frame_count: int,
                 overlays: tuple[VisualizerOverlay, ...],
                 analysis_indices: OverlaySignature,
                 analysis: dict[str, np.ndarray],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self.prepare(track_id, fps, generation, start_frame, frame_count,
                     overlays, analysis_indices, analysis)

    def prepare(self, track_id: str, fps: int, generation: int, start_frame: int, frame_count: int,
                overlays: tuple[VisualizerOverlay, ...], analysis_indices: OverlaySignature,
                analysis: dict[str, np.ndarray]) -> None:
        """Reuse the worker only after its previous finished signal was joined."""
        self.track_id = track_id
        self.fps = fps
        self.generation = generation
        self.start_frame = start_frame
        self.frame_count = frame_count
        self.overlays = overlays
        if len(analysis_indices) != len(overlays):
            raise ValueError("Every preview overlay requires its original analysis index.")
        self.analysis_indices = analysis_indices
        self.overlay_signature = analysis_indices
        self.analysis = analysis
        self.cancel_event.clear()
        self.busy = True

    def cancel(self) -> None:
        """Discard this short prefetch batch at the next frame boundary."""
        self.cancel_event.set()

    def run(self) -> None:
        try:
            rendered: list[tuple[int, tuple[QImage, ...]]] = []
            spectrum = self.analysis["levels"]
            waveform = self.analysis["waveform"]
            for frame_index in range(self.start_frame, self.start_frame + self.frame_count):
                if self.cancel_event.is_set():
                    return
                layers: list[QImage] = []
                processed = self.analysis.get("processed", ())
                meters = self.analysis.get("meters", ())
                for overlay_index, overlay in enumerate(self.overlays):
                    analysis_index = self.analysis_indices[overlay_index]
                    values = waveform if overlay.kind == "waveform" else spectrum
                    is_processed = analysis_index < len(processed)
                    if is_processed:
                        values = processed[analysis_index]
                    values_for_frame = values[min(frame_index, len(values) - 1)]
                    meter_values = None
                    peak_values = None
                    if analysis_index < len(meters) and meters[analysis_index] is not None:
                        meter_levels, meter_peaks = meters[analysis_index]
                        meter_index = min(frame_index, len(meter_levels) - 1)
                        meter_values = tuple(float(value) for value in meter_levels[meter_index])
                        peak_values = tuple(float(value) for value in meter_peaks[meter_index])
                    layers.append(PythonVisualizerRenderer.preview_image(
                        overlay.width, overlay.height, overlay, values_for_frame, frame_index,
                        processed=is_processed,
                        frame_rate=self.fps,
                        channel_values=meter_values,
                        peak_values=peak_values,
                    ))
                rendered.append((frame_index, tuple(layers)))
            if not self.cancel_event.is_set():
                self.ready.emit(
                    self.track_id, self.fps, self.generation,
                    self.overlay_signature, rendered,
                )
        except Exception as error:
            if not self.cancel_event.is_set():
                self.failed.emit(str(error))


class ExportPreviewDialog(QDialog):
    """Play and inspect the complete playlist using export-equivalent visuals."""

    def __init__(self, scene: CanvasScene, tracks: list[PlaylistTrack], translator: Translator,
                 overlays: list[VisualizerOverlay] | None = None,
                 ffmpeg_executable: Path | None = None,
                 parent: QWidget | None = None,
                 source_store: SourceStore | None = None,
                 embedded: bool = False,
                 preferred_backend: str = "gpu_layers",
                 transition_mode: str = "none",
                 crossfade_seconds: float = 3.0,
                 preloaded_blended_audio: tuple[Path, CompiledRenderPlan] | None = None,
                 blended_audio_controller: PreviewAudioController | None = None,
                 blended_audio_temp_dir: TemporaryDirectory | None = None,
                 automix_settings=None) -> None:
        super().__init__(parent)
        self.embedded = embedded
        self.setObjectName("playlistPreview")
        self.preferred_backend = (
            preferred_backend if preferred_backend in {"gpu_layers", "cpu"}
            else "gpu_layers"
        )
        if embedded:
            # Reuse the complete preview controller as the editor's central
            # preview page instead of creating a second top-level window.
            self.setWindowFlags(Qt.WindowType.Widget)
        self.scene = scene
        self.tracks = tracks
        # A caller that already ran the AutoMix/crossfade preparation dialog
        # to completion hands over the final plan directly, so the timeline
        # and track boundaries below are final from the very first frame
        # instead of starting sequential and jumping once blended audio
        # arrives later.
        # An adopted progressive controller may already hold a partial mix
        # (the preparation dialog closes on the first one): start on it too.
        latest_partial = (
            getattr(blended_audio_controller, "latest_partial", None)
            if preloaded_blended_audio is None else None
        )
        self._compiled_plan = (
            preloaded_blended_audio[1] if preloaded_blended_audio is not None
            else latest_partial[1] if latest_partial is not None
            else compile_crossfade(tracks, crossfade_seconds) if transition_mode in {"crossfade", "automix"}
            else compile_playlist(tracks)
        )
        self._track_schedule = self._build_track_schedule()
        self._track_schedule_starts = tuple(
            start for _index, _track, start, _end in self._track_schedule
        )
        self._playlist_duration_cache: float | None = None
        self.translator = translator
        self.overlays = list(overlays or [])
        self.source_store = source_store
        self._visualizer_ffmpeg_executable = ffmpeg_executable
        self._bass_reactive = any(
            isinstance(item, SourceItem)
            and item.source.visible
            and item.source.uses_bass_reaction
            for item in self.scene.items()
        )
        self.visualizer_renderer = (
            PythonVisualizerRenderer(ffmpeg_executable)
            if ffmpeg_executable and (self.overlays or self._bass_reactive) else None
        )
        self._preview_proxy_ffmpeg = (
            Path(ffmpeg_executable)
            if ffmpeg_executable is not None and Path(ffmpeg_executable).is_file()
            else None
        )
        self._preview_proxy_cache = (
            PreviewProxyCache() if self._preview_proxy_ffmpeg is not None else None
        )
        self._transition_mode = transition_mode
        self._crossfade_seconds = crossfade_seconds
        self._automix_settings = automix_settings
        self._remix_count = 0
        self._blended_audio_path: Path | None = (
            preloaded_blended_audio[0] if preloaded_blended_audio is not None else None
        )
        self._blended_audio_temp_dir: TemporaryDirectory | None = blended_audio_temp_dir
        self._blended_audio_controller: PreviewAudioController | None = None
        # Progressive AutoMix (ProgressiveAutoMixController): a partial mix only
        # covers the timeline up to _blended_audio_until; per-track audio plays
        # after it at _per_track_gain so the level matches the mix. New mixes
        # wait in _pending_swap until progressive.swap_is_safe() allows them.
        self._blended_audio_until = math.inf
        # Before any mix lands too: per-track audio at 1.0 was the originals'
        # level, and every song then dropped by the normalization gain the
        # moment the mix swapped in (~7 dB on loud masters).
        self._per_track_gain = getattr(blended_audio_controller, "playback_gain", None) or 1.0
        if latest_partial is not None:
            path_str, _plan, self._blended_audio_until, self._per_track_gain = latest_partial
            self._blended_audio_path = Path(path_str)
        self._pending_swap: tuple[Path, CompiledRenderPlan, float, float | None] | None = None
        self._progressive = False
        self._last_playhead_report = -math.inf
        self.progressive_swap_count = 0
        if preloaded_blended_audio is not None:
            # Already the final render (see above) -- nothing left to prepare.
            pass
        elif blended_audio_controller is not None:
            # The caller's preparation dialog was skipped ("Start Without
            # Waiting") while this controller's render was still in flight.
            # Adopt the same running worker instead of starting a second,
            # duplicate render: reparenting and reconnecting its signals
            # here means _stop_preview()'s existing shutdown() call is the
            # only lifecycle code this path needs.
            self._blended_audio_controller = blended_audio_controller
            blended_audio_controller.setParent(self)
            blended_audio_controller.audio_ready.connect(self._on_blended_audio_ready)
            blended_audio_controller.audio_failed.connect(self._on_blended_audio_failed)
            blended_audio_controller.progress.connect(self._on_blended_audio_progress)
            progressive_ready = getattr(blended_audio_controller, "progressive_ready", None)
            if progressive_ready is not None:
                self._progressive = True
                progressive_ready.connect(self._on_progressive_audio_ready)
                level_ready = getattr(blended_audio_controller, "level_ready", None)
                if level_ready is not None:
                    level_ready.connect(self._on_playback_level)
                # Attach right away (paused at 0), so the first transition renders before Play.
                QTimer.singleShot(0, self, lambda: self._report_playhead(force=True))
        elif ((self._transition_mode != "none" or any(track.audio_filter for track in self.tracks))
              and self._preview_proxy_ffmpeg is not None and self.tracks):
            self._blended_audio_temp_dir = TemporaryDirectory(prefix="playlist-preview-audio-", ignore_cleanup_errors=True)
            self._blended_audio_controller = PreviewAudioController(
                FFmpegRenderer(self._preview_proxy_ffmpeg), self,
            )
            self._blended_audio_controller.audio_ready.connect(self._on_blended_audio_ready)
            self._blended_audio_controller.audio_failed.connect(self._on_blended_audio_failed)
            self._blended_audio_controller.progress.connect(self._on_blended_audio_progress)
            self._blended_audio_controller.start(
                self.tracks, Path(self._blended_audio_temp_dir.name),
                self._transition_mode, self._crossfade_seconds, automix_settings=automix_settings,
            )
        self._image = QImage()
        self._base_image = QImage()
        self._base_track_id = ""
        self._base_elapsed = -1.0
        self._base_hidden_source_ids: frozenset[str] = frozenset()
        self._source_partition_dirty = True
        self._cached_audio_dynamic_ids: frozenset[str] = frozenset()
        self._cached_always_dynamic_ids: frozenset[str] = frozenset()
        self._cached_track_transition_backgrounds: tuple[
            tuple[str, float], ...
        ] = ()
        self._cached_animated_source_ids: frozenset[str] = frozenset()
        self._cached_visible_source_ids: frozenset[str] = frozenset()
        self._cached_source_items: tuple[SourceItem, ...] = ()
        self._cached_source_z_by_id: dict[str, float] = {}
        self._cached_animation_in_duration = 0.0
        self._cached_animation_out_duration = 0.0
        self._cached_video_items: tuple[SourceItem, ...] = ()
        self._dynamic_region_plans: dict[frozenset[str], tuple[tuple[frozenset[str], QRectF], ...]] = {}
        self._dynamic_region_buffers: dict[frozenset[str], QImage] = {}
        self._foreground_bands: list[tuple[float, QImage]] = []
        self._video_z_band_key: tuple[object, ...] | None = None
        self._video_z_band_layers: list[tuple[float, int, GpuPreviewLayer]] = []
        self._last_composition_key: tuple[object, ...] | None = None
        self._active_overlay_cache_seconds: float | None = None
        self._active_overlay_cache: tuple[tuple[int, VisualizerOverlay], ...] = ()
        self._scaled_overlay_cache: dict[
            tuple[OverlaySignature, float, int], tuple[VisualizerOverlay, ...]
        ] = {}
        self._playing = False
        self._scrubbing = False
        self._advancing_playhead = False
        self._force_video_seek = False
        self._playhead_seconds = 0.0
        self._active_track_index = -1
        self._last_media_position_ms = -1
        self._media_source_ready = False
        self._media_source_generation = 0
        self._pending_media_seek_ms: int | None = None
        self._pending_media_generation = -1
        self._preview_levels = None
        self._preview_levels_track_id = ""
        self._last_analysis_second = -1.0
        self._track_levels: dict[str, object] = {}
        self._analysis_worker: AudioAnalysisWorker | None = None
        self._analysis_track_id = ""
        self._overlay_worker: OverlayFrameWorker | None = None
        self._overlay_frame_cache: dict[
            tuple[str, OverlaySignature, int], tuple[QImage, ...]
        ] = {}
        self._last_overlay_layers: tuple[QImage, ...] = ()
        self._last_overlay_track_id = ""
        self._last_overlay_signature: OverlaySignature = ()
        self._overlay_prefetch_count = 6
        self._overlay_generation = 0
        self._overlay_content_revision = 0
        self._idle_overlay_analyses: dict[int, dict[str, np.ndarray]] = {}
        self._refresh_queued = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._run_scheduled_refresh)
        self._gpu_refresh_deferred = False
        self._video_duration_cache: dict[str, float] = {}
        self._video_probe_queue: list[str] = []
        self._video_probe_queued: set[str] = set()
        self._video_probe_worker: VideoDurationProbeWorker | None = None
        self._last_video_prefetch_track_index = -1
        self._video_proxy_paths: dict[str, str] = {}
        self._video_proxy_queue: list[str] = []
        self._video_proxy_total = 0  # queued this run, for the status-bar progress
        self._video_proxy_queued: set[str] = set()
        self._video_proxy_failures: set[str] = set()
        self._video_proxy_worker: PreviewProxyWorker | None = None
        self._observed_video_items: WeakSet[SourceItem] = WeakSet()
        self._closing = False
        self._overlay_error_reported = False
        self._preview_error_signatures: list[tuple[str, str]] = []
        self._last_preview_error_title = ""
        self._last_preview_error_detail = ""
        self.preview_fps = 30
        self.preview_render_scale = 0.65
        self.audio_output = QAudioOutput(self)
        saved_volume = preview_volume()
        self.audio_output.setVolume(saved_volume / 100.0)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.media_player.seekableChanged.connect(self._on_media_seekable_changed)
        self._live_mix = LiveMixPlayer(tracks, self) if transition_mode != "none" else None
        if self._live_mix is not None:
            self._live_mix.failed.connect(lambda message: self._show_preview_error(
                "오디오를 재생할 수 없습니다" if self.translator.is_korean else "Audio could not play",
                message, warning=True,
            ) if not self._closing else None)
        self.play_timer = QTimer(self)
        self.play_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.play_timer.setInterval(33)
        self.play_timer.timeout.connect(self._advance_playback)
        self._gpu_health = GpuPreviewHealth(fallback_after_stalls=2)
        self._gpu_watchdog = QTimer(self)
        self._gpu_watchdog.setSingleShot(True)
        self._gpu_watchdog.setInterval(1500)
        self._gpu_watchdog.timeout.connect(self._gpu_watchdog_timeout)
        self.play_clock = QElapsedTimer()
        self._frame_stats_clock = QElapsedTimer()
        self._frame_stats_clock.start()
        self._presented_frames = 0
        self._actual_preview_fps = 0.0

        if embedded:
            self.setMinimumSize(0, 0)
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
            )
        else:
            self.setMinimumSize(1040, 720)
            self.resize(1240, 820)
        # The application preference selects the initial backend.  Runtime GPU
        # failures still fall back to CPU for this preview without rewriting the
        # user's preference, so the next launch can retry after a driver update.
        self.gpu_preview_enabled = False
        self.preview_label = CpuPreviewSurface()
        self.preview_label.setStyleSheet("background: #121416; border-radius: 4px;")
        self.preview_label.frame_presented.connect(self._record_presented_frame)
        self.preview_stack_host = QWidget()
        self.preview_stack = QStackedLayout(self.preview_stack_host)
        self.preview_stack.setContentsMargins(0, 0, 0, 0)
        self.preview_stack.addWidget(self.preview_label)
        self.gpu_surface = None
        self.hint_label = QLabel()
        self.hint_label.setObjectName("mutedLabel")
        self.hint_label.setWordWrap(True)
        self.dialog_title_label = QLabel()
        self.dialog_title_label.setObjectName("previewDialogTitle")
        self.preview_mode_label = QLabel()
        self.preview_mode_label.setObjectName("previewStatusChip")
        self.frame_rate_label = QLabel()
        self.frame_rate_label.setObjectName("previewStatusChip")
        self.frame_rate_label.setMinimumWidth(170)
        self.frame_rate_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tracks_toggle = QPushButton()
        self.tracks_toggle.setCheckable(True)
        self.tracks_toggle.setChecked(True)
        self.performance_toggle = QPushButton()
        self.performance_toggle.setCheckable(True)
        self.performance_bar = QFrame()
        self.performance_bar.setObjectName("previewPerformanceBar")
        self.performance_bar.hide()
        self.performance_toggle.toggled.connect(self.performance_bar.setVisible)
        self.performance_title_label = QLabel()
        self.performance_title_label.setObjectName("previewPerformanceTitle")
        self.performance_scale_label = QLabel()
        self.performance_gpu_label = QLabel()
        self.performance_video_label = QLabel()
        self.performance_latency_label = QLabel()
        for label in (
            self.performance_scale_label, self.performance_gpu_label,
            self.performance_video_label, self.performance_latency_label,
        ):
            label.setObjectName("previewPerformanceMetric")
        self.error_banner = QFrame()
        self.error_banner.setObjectName("previewErrorBanner")
        self.error_banner.setProperty("severity", "error")
        error_layout = QHBoxLayout(self.error_banner)
        error_layout.setContentsMargins(10, 7, 8, 7)
        error_layout.setSpacing(8)
        self.error_icon_label = QLabel("!")
        self.error_icon_label.setObjectName("previewErrorIcon")
        self.error_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_icon_label.setFixedSize(22, 22)
        self.error_title_label = QLabel()
        self.error_title_label.setObjectName("previewErrorTitle")
        self.error_message_label = QLabel()
        self.error_message_label.setObjectName("previewErrorMessage")
        self.error_message_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        self.error_details_button = QPushButton()
        self.error_details_button.setObjectName("previewErrorButton")
        self.error_dismiss_button = QPushButton("×")
        self.error_dismiss_button.setObjectName("previewErrorDismissButton")
        self.error_dismiss_button.setFixedWidth(28)
        error_layout.addWidget(self.error_icon_label)
        error_layout.addWidget(self.error_title_label)
        error_layout.addWidget(self.error_message_label, 1)
        error_layout.addWidget(self.error_details_button)
        error_layout.addWidget(self.error_dismiss_button)
        self.error_details_button.clicked.connect(self._show_preview_error_details)
        self.error_dismiss_button.clicked.connect(self.error_banner.hide)
        self.error_banner.hide()
        self.play_button = QPushButton()
        self.play_button.setObjectName("previewPlayButton")
        self.play_button.setCheckable(True)
        self.previous_button = QPushButton()
        self.rewind_button = QPushButton()
        self.forward_button = QPushButton("+5s")
        self.next_button = QPushButton()
        for button in (
            self.previous_button, self.rewind_button,
            self.forward_button, self.next_button,
        ):
            button.setObjectName("previewTransportButton")
        self.volume_label = QLabel()
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(saved_volume)
        self.volume_slider.setFixedWidth(150)
        self.volume_value_label = QLabel(f"{saved_volume}%")
        self.volume_value_label.setObjectName("previewValueLabel")
        self.volume_value_label.setMinimumWidth(42)
        self._adaptive_quality = AdaptivePreviewQuality(self.preview_render_scale)
        self._decoder_backpressure = VideoDecoderBackpressure(self.preview_fps)
        self._last_decoder_accepted_frames = 0
        self._last_decoder_pressure_drops = 0
        self._last_active_decoder_count = 0
        self._last_gpu_dropped_frames = 0
        self.play_timer.setInterval(max(8, round(1000 / self.preview_fps)))
        self.timeline = PlaylistTimeline(self._track_schedule, self._compiled_plan.audio.transitions)
        self.timeline.setObjectName("previewTimeline")
        self.timeline.setRange(0, max(1, ceil(self._playlist_duration() * TIMELINE_SCALE)))
        self.time_label = QLabel()
        self.time_label.setObjectName("previewTimeLabel")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.track_time_label = QLabel("00:00 / 00:00")
        self.track_time_label.setObjectName("previewTimeLabel")
        self.track_title_label = QLabel("-")
        self.track_title_label.setObjectName("previewTrackTitle")
        self.track_title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.track_title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.track_meta_label = QLabel("-")
        self.track_meta_label.setObjectName("mutedLabel")
        self.track_meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.track_meta_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.track_counter_label = QLabel("0 / 0")
        self.track_counter_label.setObjectName("previewStatusChip")
        self.track_badge_label = QLabel("01")
        self.track_badge_label.setObjectName("previewTrackBadge")
        self.track_badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.track_badge_label.setFixedSize(36, 36)
        self.timeline_title_label = QLabel()
        self.timeline_title_label.setObjectName("panelTitle")
        self.track_list_panel = QFrame()
        self.track_list_panel.setObjectName("previewTrackPanel")
        self.tracks_toggle.toggled.connect(self.track_list_panel.setVisible)
        self.track_list_panel.setMinimumWidth(210)
        self.track_list_panel.setMaximumWidth(300)
        self.track_list_panel.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding,
        )
        self.track_list_title_label = QLabel()
        self.track_list_title_label.setObjectName("previewTrackListTitle")
        self.track_list_count_label = QLabel()
        self.track_list_count_label.setObjectName("mutedLabel")
        self.track_list = QListWidget()
        self.track_list.setObjectName("previewTrackList")
        self.track_list.setUniformItemSizes(True)
        self.track_list.setItemDelegate(PreviewTrackDelegate(self.track_list))
        self.track_list.setMouseTracking(True)
        self.track_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self._highlighted_track_index = -1
        track_panel_layout = QVBoxLayout(self.track_list_panel)
        track_panel_layout.setContentsMargins(10, 10, 10, 10)
        track_panel_layout.setSpacing(8)
        track_panel_header = QHBoxLayout()
        track_panel_header.addWidget(self.track_list_title_label)
        track_panel_header.addStretch(1)
        track_panel_header.addWidget(self.track_list_count_label)
        track_panel_layout.addLayout(track_panel_header)
        track_panel_layout.addWidget(self.track_list, 1)
        # Mix status, and the "Transition details" window for blended previews.
        self.automix_details: AutoMixDetailsPanel | None = None
        if self._transition_mode != "none":
            self.automix_details = AutoMixDetailsPanel(
                translator, automix=self._transition_mode == "automix",
            )
            track_panel_layout.addWidget(self.automix_details)
            self.automix_details.open_requested.connect(self._open_transition_window)
            self._refresh_automix_details(self._blended_audio_until if self._blended_audio_path else None)
            last = getattr(self._blended_audio_controller, "last_progress", None)
            if last is not None:  # continue from where the preparation popup was
                self._on_blended_audio_progress(*last)
        self.shortcut_hint_label = QLabel()
        self.shortcut_hint_label.setObjectName("mutedLabel")
        self.shortcut_hint_label.setWordWrap(True)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.preview_close_button = self.button_box.button(
            QDialogButtonBox.StandardButton.Close,
        )
        self.button_box.rejected.connect(self.reject)
        self.button_box.accepted.connect(self.accept)
        self.rejected.connect(self._stop_preview)
        self.accepted.connect(self._stop_preview)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header_text = QVBoxLayout()
        header_text.setSpacing(2)
        header_text.addWidget(self.dialog_title_label)
        header_text.addWidget(self.hint_label)
        header.addLayout(header_text, 1)
        header.addWidget(self.tracks_toggle)
        header.addWidget(self.performance_toggle)
        header.addWidget(self.button_box)
        layout.addLayout(header)
        self.audio_sync_warning = AudioSyncWarning(self, audio_output=self.audio_output,
                                                  media_player=self.media_player, translator=translator)
        layout.addWidget(self.audio_sync_warning)

        performance_layout = QGridLayout(self.performance_bar)
        performance_layout.setContentsMargins(12, 8, 12, 8)
        performance_layout.setSpacing(6)
        performance_layout.addWidget(self.performance_title_label, 0, 0)
        for index, metric in enumerate((self.preview_mode_label, self.frame_rate_label,
                                       self.performance_scale_label, self.performance_gpu_label,
                                       self.performance_video_label, self.performance_latency_label)):
            metric.setWordWrap(True)
            performance_layout.addWidget(metric, 1 + index // 2, index % 2)
        performance_layout.setColumnStretch(0, 1)
        performance_layout.setColumnStretch(1, 1)
        layout.addWidget(self.performance_bar)
        layout.addWidget(self.error_banner)

        self.preview_stage = QFrame()
        self.preview_stage.setObjectName("previewStage")
        stage_layout = QVBoxLayout(self.preview_stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_content_layout = QHBoxLayout()
        stage_content_layout.setContentsMargins(0, 0, 0, 0)
        stage_content_layout.setSpacing(8)
        stage_content_layout.addWidget(self.preview_stack_host, 1)
        stage_content_layout.addWidget(self.track_list_panel)
        stage_layout.addLayout(stage_content_layout, 1)
        layout.addWidget(self.preview_stage, 1)

        self.now_playing_card = QFrame()
        self.now_playing_card.setObjectName("previewInfoCard")
        now_layout = QHBoxLayout(self.now_playing_card)
        now_layout.setContentsMargins(14, 8, 14, 8)
        now_layout.setSpacing(12)
        now_layout.addWidget(self.track_badge_label)
        track_text = QVBoxLayout()
        track_text.setSpacing(1)
        track_text.addWidget(self.track_title_label)
        track_text.addWidget(self.track_meta_label)
        now_layout.addLayout(track_text, 1)
        now_layout.addWidget(self.track_counter_label)
        now_layout.addWidget(self.track_time_label)
        layout.addWidget(self.now_playing_card)

        self.timeline_card = QFrame()
        self.timeline_card.setObjectName("previewControlCard")
        timeline_layout = QVBoxLayout(self.timeline_card)
        timeline_layout.setContentsMargins(14, 6, 14, 4)
        timeline_header = QHBoxLayout()
        timeline_header.addWidget(self.timeline_title_label)
        timeline_header.addStretch()
        timeline_header.addWidget(self.time_label)
        timeline_layout.addLayout(timeline_header)
        timeline_layout.addWidget(self.timeline)
        layout.addWidget(self.timeline_card)

        self.transport_card = QFrame()
        self.transport_card.setObjectName("previewControlCard")
        controls = QGridLayout(self.transport_card)
        controls.setContentsMargins(14, 10, 14, 10)
        controls.setHorizontalSpacing(10)
        controls.setVerticalSpacing(8)
        controls.addWidget(self.previous_button, 0, 0)
        controls.addWidget(self.rewind_button, 0, 1)
        controls.addWidget(self.play_button, 0, 2)
        controls.addWidget(self.forward_button, 0, 3)
        controls.addWidget(self.next_button, 0, 4)
        controls.setColumnStretch(5, 1)
        controls.addWidget(self.volume_label, 0, 6)
        controls.addWidget(self.volume_slider, 0, 7)
        controls.addWidget(self.volume_value_label, 0, 8)
        self.shortcut_hint_label.hide()
        layout.addWidget(self.transport_card)

        self.timeline.valueChanged.connect(self._on_seeked)
        self.timeline.sliderPressed.connect(self._start_scrubbing)
        self.timeline.sliderReleased.connect(self._finish_scrubbing)
        self.play_button.toggled.connect(self._toggle_playback)
        self.previous_button.clicked.connect(lambda: self._skip_track(-1))
        self.rewind_button.clicked.connect(lambda: self._seek_relative(-5.0))
        self.forward_button.clicked.connect(lambda: self._seek_relative(5.0))
        self.next_button.clicked.connect(lambda: self._skip_track(1))
        self.volume_slider.valueChanged.connect(self._set_volume)
        self.track_list.itemClicked.connect(self._restore_track_highlight)
        self.track_list.itemDoubleClicked.connect(self._jump_to_track_item)
        self._install_shortcuts()
        initial_gpu = (
            self.preferred_backend == "gpu_layers"
            and GPU_TEXTURE_SURFACE_AVAILABLE
        )
        self._set_gpu_preview(initial_gpu)
        self._apply_preview_style()
        self.translator.language_changed.connect(self.retranslate)
        if self.source_store is not None:
            self.source_store.source_added.connect(self._invalidate_source_partitions)
            self.source_store.source_removed.connect(self._invalidate_source_partitions)
            self.source_store.source_changed.connect(self._invalidate_source_partitions)
            self.source_store.sources_replaced.connect(self._invalidate_source_partitions)
        self.retranslate()
        self.refresh_preview()
        self._cover_worker = PreviewCoverWorker(self.tracks, self)
        self._cover_worker.ready.connect(self._store_track_cover)
        QTimer.singleShot(0, self, self._start_cover_loading)

    def build_embedded_controls_page(self) -> QWidget:
        """Build editor-native Canvas stage and bottom transport arrangements."""
        if not self.embedded:
            raise RuntimeError("Bottom controls are available only in embedded preview mode.")
        existing = getattr(self, "_embedded_controls_page", None)
        if existing is not None:
            return existing
        page = QFrame()
        page.setObjectName("embeddedPreviewControls")
        controls_layout = QVBoxLayout(page)
        controls_layout.setContentsMargins(12, 8, 12, 8)
        controls_layout.setSpacing(5)
        source_layout = self.layout()
        for widget in (
            self.now_playing_card, self.timeline_card, self.transport_card,
        ):
            if source_layout is not None:
                source_layout.removeWidget(widget)
            controls_layout.addWidget(widget)

        self.setObjectName("embeddedCanvasPreview")
        self.dialog_title_label.setObjectName("embeddedPreviewTitle")
        self.hint_label.hide()
        if source_layout is not None:
            source_layout.setContentsMargins(0, 0, 0, 0)
            source_layout.setSpacing(6)
        stage_layout = self.preview_stage.layout()
        if stage_layout is not None:
            stage_layout.setContentsMargins(0, 0, 0, 0)
        self.now_playing_card.setObjectName("embeddedNowPlaying")
        self.timeline_card.setObjectName("embeddedTimeline")
        self.transport_card.setObjectName("embeddedTransport")
        self.track_badge_label.setFixedSize(38, 38)
        self.now_playing_card.setMaximumHeight(58)
        self.timeline_card.setMaximumHeight(82)
        self.transport_card.setMaximumHeight(56)
        self.shortcut_hint_label.hide()

        # Recompose the dialog's two-row control card into the same one-line
        # transport strip used by desktop editors.
        transport_layout = self.transport_card.layout()
        transport_widgets = (
            self.previous_button, self.rewind_button, self.play_button,
            self.forward_button, self.next_button, self.volume_label,
            self.volume_slider, self.volume_value_label,
            self.shortcut_hint_label,
        )
        for widget in transport_widgets:
            transport_layout.removeWidget(widget)
        # The original two-row grid stretches column 5 for the shortcut hint.
        # Clear those stretches before reusing the columns for the one-line strip.
        for column in range(transport_layout.columnCount()):
            transport_layout.setColumnStretch(column, 0)
        transport_layout.addWidget(self.volume_label, 0, 0)
        transport_layout.addWidget(self.volume_slider, 0, 1)
        transport_layout.addWidget(self.volume_value_label, 0, 2)
        transport_layout.setColumnStretch(3, 1)
        transport_layout.addWidget(self.previous_button, 0, 4)
        transport_layout.addWidget(self.rewind_button, 0, 5)
        transport_layout.addWidget(self.play_button, 0, 6)
        transport_layout.addWidget(self.forward_button, 0, 7)
        transport_layout.addWidget(self.next_button, 0, 8)
        transport_layout.setColumnStretch(9, 1)
        if self.preview_close_button is not None:
            self.button_box.removeButton(self.preview_close_button)
            self.preview_close_button.setObjectName("embeddedPreviewCloseButton")
            self.preview_close_button.clicked.connect(self.reject)
            transport_layout.addWidget(self.preview_close_button, 0, 10)
        self.button_box.hide()
        self._apply_embedded_preview_style(page)
        self.retranslate()
        self._embedded_controls_page = page
        return page

    def _apply_embedded_preview_style(self, page: QWidget) -> None:
        """Embedded playback uses the same chrome as the rest of the editor."""
        self.setStyleSheet("")
        page.setStyleSheet("")

    def _build_track_schedule(
        self,
    ) -> tuple[tuple[int, PlaylistTrack, float, float], ...]:
        """Build immutable, non-overlapping track-ownership boundaries for fast lookup.

        Sourced from CompiledRenderPlan.metadata.chapters -- the same
        chapter/timestamp boundaries Export and previous/next navigation
        already use -- rather than PresentationWindow.timeline_end directly.
        A window's own timeline_end is its clip's actual audio end, which
        AutoMix deliberately extends past the next window's start so two
        clips overlap during a transition; a chapter's end is instead
        clamped to the next chapter's start (or the plan duration for the
        last one), so track-ownership regions here never overlap even when
        the underlying audio does. PresentationWindow only carries
        track_id, so the PlaylistTrack object is resolved back by id.
        """
        track_by_id = {track.id: track for track in self.tracks}
        plan = getattr(self, "_compiled_plan", None) or compile_playlist(self.tracks)
        return tuple(
            (index, track_by_id[chapter.track_id], chapter.start, chapter.end)
            for index, chapter in enumerate(plan.metadata.chapters)
        )

    def _track_at(self, playlist_seconds: float) -> tuple[int, PlaylistTrack, float, float] | None:
        """Return the active track plus local time and global start position."""
        plan = getattr(self, "_compiled_plan", None)
        if plan is not None and plan.presentation.windows:
            window = plan.presentation._window_at(playlist_seconds)
            index = next(i for i, track in enumerate(self.tracks) if track.id == window.track_id)
            return index, self.tracks[index], plan.presentation.local_time(playlist_seconds), window.timeline_start
        schedule = getattr(self, "_track_schedule", None)
        starts = getattr(self, "_track_schedule_starts", None)
        if schedule is None or starts is None:
            schedule = ExportPreviewDialog._build_track_schedule(self)
            starts = tuple(start for _index, _track, start, _end in schedule)
        if not schedule:
            return None
        schedule_index = bisect_right(starts, playlist_seconds) - 1
        if schedule_index < 0:
            index, track, start, _end = schedule[0]
            return index, track, 0.0, start
        index, track, start, end = schedule[schedule_index]
        if playlist_seconds < end:
            elapsed = max(
                0.0,
                min(track.duration_seconds, playlist_seconds - start),
            )
            return index, track, elapsed, start
        # In a requested gap, retain the previous track's metadata while audio
        # remains inactive. This matches the established Canvas behavior.
        return index, track, track.duration_seconds, start

    def _visual_track_at(
        self, playlist_seconds: float,
    ) -> tuple[tuple[int, PlaylistTrack, float, float] | None, MixJunction | None]:
        """``_track_at`` for what the Canvas draws, plus its mix handover state.

        Across a crossfade/AutoMix overlap the next track takes the Canvas over in
        the overlap's middle (render_plan.visual_segments), exactly like export.
        Audio transport keeps ``_track_at``: the incoming track starts playing
        at its own start.
        """
        plan = getattr(self, "_compiled_plan", None)
        if plan is None or not plan.presentation.windows:
            return self._track_at(playlist_seconds), None
        cached = getattr(self, "_visual_segments_cache", None)
        if cached is None or cached[0] is not plan:
            cached = self._visual_segments_cache = (plan, visual_segments(plan))
        segment = visual_segment_at(cached[1], playlist_seconds)
        window = plan.presentation.windows[segment.window_index]
        index = next(i for i, track in enumerate(self.tracks) if track.id == window.track_id)
        clamped = min(max(playlist_seconds, window.timeline_start), window.timeline_end)
        elapsed = window.source_time_at_start + (clamped - window.timeline_start) * window.playback_rate
        return (
            (index, self.tracks[index], elapsed, window.timeline_start),
            segment_junction(segment, playlist_seconds),
        )

    def _frame_animation_state(
        self, track: PlaylistTrack, elapsed: float, junction: MixJunction | None,
    ) -> tuple[str | None, float, float]:
        """``_animation_state``, except right around a mix handover."""
        plain = self._animation_state(track, elapsed)
        mix = resolve_mix_phase(
            [item.source for item in getattr(self, "_cached_source_items", ())], junction,
        )
        return mix if mix[0] is not None else plain

    def _track_start_seconds(self, target_index: int) -> float:
        if 0 <= target_index < len(self._track_schedule):
            return self._track_schedule[target_index][2]
        return 0.0

    def _populate_track_list(self) -> None:
        """Build a read-only navigator for the complete preview playlist."""
        current = self._highlighted_track_index
        korean = self.translator.is_korean
        for index, track in enumerate(self.tracks):
            title = track.title or Path(track.file_path).stem or (
                "제목 없음" if korean else "Untitled track"
            )
            detail = _track_details(track, " · ")
            item = self.track_list.item(index)
            if item is None:
                item = QListWidgetItem()
                self.track_list.addItem(item)
            item.setText(title)
            item.setData(TRACK_ROW_ROLE, (detail, format_timestamp(track.duration_seconds)))
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setData(
                Qt.ItemDataRole.UserRole + 1,
                self._track_start_seconds(index),
            )
            item.setToolTip(
                f"{title}\n{detail}\n{Path(track.file_path)}"
            )
        self.track_list_count_label.setText(str(len(self.tracks)))
        if 0 <= current < self.track_list.count():
            self.track_list.setCurrentRow(current)

    def _start_cover_loading(self) -> None:
        if not self._closing:
            self._cover_worker.start()

    def _store_track_cover(self, index: int, image: QImage) -> None:
        if not self._closing:
            item = self.track_list.item(index)
            if item is not None:
                item.setIcon(QIcon(QPixmap.fromImage(image)))

    def _highlight_track(self, index: int) -> None:
        if index == self._highlighted_track_index:
            return
        self._highlighted_track_index = index
        self.previous_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < len(self.tracks) - 1)
        if 0 <= index < self.track_list.count():
            self.track_list.setCurrentRow(index)
            item = self.track_list.item(index)
            if item is not None:
                self.track_list.scrollToItem(item)

    def _jump_to_track_item(self, item: QListWidgetItem) -> None:
        index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(index, int) or not 0 <= index < len(self.tracks):
            return
        start = float(item.data(Qt.ItemDataRole.UserRole + 1) or 0.0)
        self.timeline.setValue(round(start * TIMELINE_SCALE))
        self._highlight_track(index)

    def _restore_track_highlight(self, _item: QListWidgetItem) -> None:
        """Keep single clicks informational; navigation requires a double click."""
        if 0 <= self._highlighted_track_index < self.track_list.count():
            self.track_list.setCurrentRow(self._highlighted_track_index)

    @property
    def _active_render_scale(self) -> float:
        """Return adaptive preview scale without changing export quality."""
        return self._adaptive_quality.scale

    @staticmethod
    def _selection_has_audio(
        selected: tuple[int, PlaylistTrack, float, float] | None,
        playlist_seconds: float,
    ) -> bool:
        """Return whether a preview selection is inside audible track time."""
        if selected is None:
            return False
        _index, track, _elapsed, start = selected
        return start <= playlist_seconds < start + track.duration_seconds

    def _has_audio(self, selected, playlist_seconds: float) -> bool:
        plan = getattr(self, "_compiled_plan", None)
        if plan is not None:
            return any(clip.timeline_start <= playlist_seconds < clip.timeline_end
                       for clip in plan.audio.clips)
        return self._selection_has_audio(selected, playlist_seconds)

    def _refresh_automix_details(self, covered_until: float | None = None) -> None:
        """Show the playing plan's transitions: waiting (sequential), provisional or final."""
        panel = getattr(self, "automix_details", None)
        if panel is None:
            return
        if covered_until is None:
            state, through = ("final" if self._blended_audio_path is not None else "waiting"), None
        elif math.isinf(covered_until):
            state, through = "final", None
        else:
            state, through = "provisional", ready_through(self._compiled_plan, covered_until)
        panel.set_plan(self._compiled_plan, self.tracks, state=state, ready_through=through)
        panel.set_playhead(self._playhead_seconds if hasattr(self, "_playhead_seconds") else 0.0)

    def _open_transition_window(self) -> None:
        """Show the larger, drawn transition view; one window, reused and brought to front."""
        window = getattr(self, "_transition_window", None)
        if window is None:
            window = self._transition_window = TransitionInspectorWindow(self.automix_details, self)
            window.seek_requested.connect(self._seek_to_seconds)
            window.play_requested.connect(self._play_from_seconds)
            # Its transport drives, and follows, Preview's own play button and volume.
            window.playing_toggled.connect(self.play_button.setChecked)
            window.volume_changed.connect(self.volume_slider.setValue)
            self.play_button.toggled.connect(window.set_playing)
            self.volume_slider.valueChanged.connect(window.set_volume)
            window.set_playing(self.play_button.isChecked())
            window.set_volume(self.volume_slider.value())
            self.finished.connect(window.close)
            window.set_playhead(self.timeline.value() / TIMELINE_SCALE)
        window.show()
        window.raise_()
        window.activateWindow()

    def remix_automix(self) -> None:
        """Re-plan and re-render the AutoMix with the current settings; the new mix swaps in when ready.

        Reuses (or creates) a progressive controller: cached analysis makes the
        re-plan immediate, and the existing swap logic keeps the music going
        (``swap_playhead``) while the new mix renders into a fresh directory,
        never over the file that is playing.
        """
        if self._closing or self._transition_mode != "automix" or not self.tracks:
            return
        controller = self._blended_audio_controller
        if getattr(controller, "progressive_ready", None) is None:
            if self._preview_proxy_ffmpeg is None:
                return
            from app.controllers.progressive_automix_controller import ProgressiveAutoMixController

            if controller is not None:
                controller.shutdown()
            controller = ProgressiveAutoMixController(
                FFmpegRenderer(self._preview_proxy_ffmpeg), self, korean=self.translator.is_korean,
            )
            self._blended_audio_controller = controller
            controller.audio_ready.connect(self._on_blended_audio_ready)
            controller.audio_failed.connect(self._on_blended_audio_failed)
            controller.progress.connect(self._on_blended_audio_progress)
            controller.progressive_ready.connect(self._on_progressive_audio_ready)
            controller.level_ready.connect(self._on_playback_level)
            self._progressive = True
        if self._blended_audio_temp_dir is None:
            self._blended_audio_temp_dir = TemporaryDirectory(
                prefix="playlist-preview-audio-", ignore_cleanup_errors=True,
            )
        self._remix_count += 1
        self._pending_swap = None
        controller.start(
            self.tracks, Path(self._blended_audio_temp_dir.name) / f"remix-{self._remix_count}",
            "automix", self._crossfade_seconds, automix_settings=self._automix_settings,
        )
        self._report_playhead(force=True)

    def _seek_to_seconds(self, seconds: float) -> None:
        self.timeline.setValue(round(max(0.0, seconds) * TIMELINE_SCALE))

    def _play_from_seconds(self, seconds: float) -> None:
        self._seek_to_seconds(seconds)
        if not self.play_button.isChecked():
            self.play_button.setChecked(True)

    def _on_seeked(self, _value: int) -> None:
        panel = getattr(self, "automix_details", None)
        if panel is not None:
            panel.set_playhead(self.timeline.value() / TIMELINE_SCALE)
        if not self._advancing_playhead:
            self._playhead_seconds = self.timeline.value() / TIMELINE_SCALE
            self._force_video_seek = True
            if self._scrubbing:
                self._schedule_refresh(max(8, round(1000 / self.preview_fps)))
                return
            if getattr(self, "_progressive", False):
                # A user seek restarts audio anyway: the natural moment to take
                # a waiting mix, and to re-prioritize rendering around the new spot.
                self._try_apply_pending_swap(force=True)
                self._report_playhead(force=True)
            elif self._pending_swap is not None:
                self._try_apply_pending_swap(force=True)
        self.refresh_preview()
        if self._playing and not self._advancing_playhead:
            self._start_audio_at_playhead()

    def _start_scrubbing(self) -> None:
        if self._closing:
            return
        self._scrubbing = True
        self.play_timer.stop()
        self.media_player.pause()
        if self._live_mix is not None:
            self._live_mix.pause()
        self._refresh_source_partitions()
        for item in self._cached_video_items:
            item.set_video_preview_playback(False, item.source.video_speed)

    def _finish_scrubbing(self) -> None:
        if self._closing:
            return
        self._scrubbing = False
        self._refresh_timer.stop()
        self._refresh_queued = False
        self._on_seeked(self.timeline.value())
        self._reset_frame_statistics()
        if self._playing:
            self.play_clock.start()
            self.play_timer.start()

    @timed("preview.refresh_seconds")
    def refresh_preview(self) -> None:
        """Render the global playhead with the same dynamic-overlay drawing as export."""
        if self._closing:
            return
        if (
            self.gpu_preview_enabled
            and self.gpu_surface is not None
            and getattr(self.gpu_surface, "frame_pending", False)
        ):
            # The playhead can advance faster than vsync on a busy scene. Keep
            # only the newest requested state instead of rendering a queue of
            # frames that will already be obsolete when presented.
            if PROFILE_ENABLED:
                PROFILE.observe("preview.deferred_refresh", 1.0)
            self._gpu_refresh_deferred = True
            return
        self._gpu_refresh_deferred = False
        selected, junction = self._visual_track_at(self.timeline.value() / TIMELINE_SCALE)
        if selected is None:
            return
        track_index, track, elapsed, start = selected
        self._frame_junction = junction
        self._highlight_track(track_index)
        playlist_seconds = self.timeline.value() / TIMELINE_SCALE
        phase, phase_progress, phase_duration = self._frame_animation_state(
            track, elapsed, junction,
        )
        self._refresh_source_partitions()
        bass_level = self._bass_level(track, elapsed)
        audio_dynamic_ids = self._cached_audio_dynamic_ids
        canvas_dynamic_ids = self._canvas_dynamic_source_ids(
            track_index,
            junction.visual_elapsed
            if junction is not None and junction.visual_elapsed is not None else elapsed,
            phase,
        )
        static_hidden_ids = frozenset(audio_dynamic_ids | canvas_dynamic_ids)
        ordered_canvas_required = self._canvas_dynamic_requires_z_composition(
            canvas_dynamic_ids, playlist_seconds,
        )
        gpu_video_filters = self._configure_gpu_video_color_filters(
            canvas_dynamic_ids, ordered_canvas_required, playlist_seconds,
        )
        self._sync_video_sources(
            track, elapsed,
            track_start=start,
            track_active=ExportPreviewDialog._has_audio(self, selected, playlist_seconds),
            force_seek=self._force_video_seek,
        )
        self._force_video_seek = False
        self._prefetch_upcoming_video_durations(track_index)
        direct_video_ids, direct_video_layers = self._direct_gpu_video_layers(
            canvas_dynamic_ids, ordered_canvas_required, playlist_seconds,
            gpu_video_filters,
        )
        raster_dynamic_ids = frozenset(canvas_dynamic_ids - direct_video_ids)
        video_z_banded = (
            ordered_canvas_required
            and bool(direct_video_ids)
            and not self._canvas_dynamic_requires_z_composition(
                raster_dynamic_ids, playlist_seconds,
            )
        )
        full_canvas_ordered_required = ordered_canvas_required and not video_z_banded
        if video_z_banded:
            self._ensure_video_z_band_cache(
                track, track_index, start, static_hidden_ids,
                direct_video_ids,
            )
            self._foreground_bands = []
        elif full_canvas_ordered_required:
            # A dynamic source below a static source cannot be drawn as a final
            # dirty-region layer without reversing their stack. Capture the
            # Canvas in its native scene order for this frame. Audio-reactive
            # overlays remain separate and use their existing Z-band path.
            self._base_image = CanvasSnapshot.capture_track(
                self.scene, track, track_index + 1, len(self.tracks), start,
                phase, phase_progress, elapsed, audio_dynamic_ids,
                self._playlist_duration(), self.tracks, self._active_render_scale,
                timeline_seconds=self.timeline.value() / TIMELINE_SCALE,
                animation_phase_duration=phase_duration,
                junction=junction,
                bass_level=bass_level,
                music_levels=self._music_reaction_levels(track, elapsed),
            )
            self._base_track_id = track.id
            self._base_elapsed = elapsed
            self._base_hidden_source_ids = frozenset(audio_dynamic_ids)
            self._foreground_bands = []
        else:
            refresh_base = (
                self._base_image.isNull()
                or self._base_track_id != track.id
                or self._base_hidden_source_ids != static_hidden_ids
            )
            if refresh_base:
                self._base_image = CanvasSnapshot.capture_track(
                    self.scene, track, track_index + 1, len(self.tracks), start,
                    None, 1.0, 0.0, audio_dynamic_ids,
                    self._playlist_duration(), self.tracks, self._active_render_scale,
                hide_source_ids=static_hidden_ids,
                    timeline_seconds=self.timeline.value() / TIMELINE_SCALE,
                )
                self._base_track_id = track.id
                self._base_elapsed = 0.0
                self._base_hidden_source_ids = static_hidden_ids
                self._foreground_bands = []
        use_gpu_layers = self.gpu_preview_enabled and self.gpu_surface is not None
        composition_key = self._preview_composition_key(
            track, phase, raster_dynamic_ids, direct_video_ids,
            playlist_seconds,
            ordered_canvas=full_canvas_ordered_required,
            video_z_banded=video_z_banded,
        )
        composition_needed = composition_key != self._last_composition_key
        gpu_submitted = False
        if composition_needed:
            ordered_dynamic_layers: list[tuple[float, int, GpuPreviewLayer]] = [
                (z_index, 0, layer) for z_index, layer in direct_video_layers
            ]
            if video_z_banded:
                ordered_dynamic_layers.extend(self._video_z_band_layers)
            if not use_gpu_layers:
                # QImage is implicitly shared, but painting would detach it.
                # A static frame can therefore reuse the base without a copy.
                self._image = (
                    self._base_image
                    if not raster_dynamic_ids
                    and not self._active_overlay_entries(playlist_seconds)
                    else self._base_image.copy()
                )
            if raster_dynamic_ids and not full_canvas_ordered_required:
                painter = None if use_gpu_layers else QPainter(self._image)
                for source_ids, capture_rect in self._dynamic_capture_regions(
                    raster_dynamic_ids,
                ):
                    buffer_key = frozenset(source_ids)
                    buffer = self._dynamic_region_buffers.get(buffer_key, QImage())
                    buffer = CanvasSnapshot.capture_track(
                        self.scene, track, track_index + 1, len(self.tracks), start,
                        phase, phase_progress, elapsed, audio_dynamic_ids,
                        self._playlist_duration(), self.tracks,
                        self._active_render_scale,
                        transparent=True,
                        hide_source_ids=self._cached_visible_source_ids - source_ids,
                        image_buffer=buffer,
                        capture_rect=capture_rect,
                        timeline_seconds=playlist_seconds,
                        animation_phase_duration=phase_duration,
                        junction=junction,
                        bass_level=bass_level,
                        music_levels=self._music_reaction_levels(track, elapsed),
                    )
                    self._dynamic_region_buffers[buffer_key] = buffer
                    target = QRectF(
                        round(capture_rect.left() * self._active_render_scale),
                        round(capture_rect.top() * self._active_render_scale),
                        buffer.width(), buffer.height(),
                    )
                    if use_gpu_layers:
                        color_filter = (
                            gpu_video_filters.get(next(iter(source_ids)))
                            if len(source_ids) == 1 else None
                        )
                        ordered_dynamic_layers.append((
                            self._source_group_z(source_ids),
                            0,
                            GpuPreviewLayer(
                                ("canvas-dynamic", buffer_key), buffer, target,
                                color_filter=color_filter,
                            ),
                        ))
                    elif painter is not None:
                        painter.drawImage(
                            round(target.x()), round(target.y()), buffer,
                        )
                if painter is not None:
                    painter.end()
            dynamic_layers = [
                layer for _z_index, _kind, layer in sorted(
                    ordered_dynamic_layers,
                    key=lambda entry: (entry[0], entry[1]),
                )
            ]
            # Dynamic audio layers are normally a cheap final composition.
            gpu_submitted = use_gpu_layers and self._submit_gpu_composition(
                track, track_index, elapsed, start, phase, phase_progress,
                phase_duration, dynamic_layers,
            )
            if not gpu_submitted:
                if use_gpu_layers:
                    self._image = self._base_image.copy()
                    painter = QPainter(self._image)
                    for layer in dynamic_layers:
                        self._paint_cpu_fallback_layer(painter, layer)
                    painter.end()
                self._composite_export_overlays(track, elapsed)
            self._last_composition_key = composition_key
        time_text = (
            f"{format_timestamp(self.timeline.value() / TIMELINE_SCALE)} / "
            f"{format_timestamp(self._playlist_duration())}"
        )
        if self.time_label.text() != time_text:
            self.time_label.setText(time_text)
        track_time_text = (
            f"{format_timestamp(elapsed)} / {format_timestamp(track.duration_seconds)}"
        )
        if self.track_time_label.text() != track_time_text:
            self.track_time_label.setText(track_time_text)
        track_title = track.title or Path(track.file_path).stem
        if self.track_title_label.text() != track_title:
            self.track_title_label.setText(track_title)
            self.track_title_label.setToolTip(track_title)
        metadata_text = _track_details(track, " / ")
        if self.track_meta_label.text() != metadata_text:
            self.track_meta_label.setText(metadata_text)
            self.track_meta_label.setToolTip(str(Path(track.file_path)))
        counter_text = f"{track_index + 1} / {len(self.tracks)}"
        if self.track_counter_label.text() != counter_text:
            self.track_counter_label.setText(counter_text)
        badge_text = f"{track_index + 1:02d}"
        if self.track_badge_label.text() != badge_text:
            self.track_badge_label.setText(badge_text)
        if composition_needed and not gpu_submitted:
            self._update_pixmap()

    def _invalidate_source_partitions(self, *_args: object) -> None:
        """Invalidate preview caches only when the editor's source model changes."""
        self._source_partition_dirty = True
        self._base_image = QImage()
        self._base_track_id = ""
        self._dynamic_region_plans.clear()
        self._dynamic_region_buffers.clear()
        self._video_z_band_key = None
        self._video_z_band_layers.clear()
        self._last_composition_key = None

    def _sync_video_sources(
        self, track: PlaylistTrack, elapsed: float, *, track_start: float | None = None,
        track_active: bool = True, force_seek: bool = False,
    ) -> None:
        """Seek Canvas video items with the same deterministic rules as export."""
        timeline_seconds = self.timeline.value() / TIMELINE_SCALE
        # Decoder pixels have their own feedback loop. Canvas GPU pressure must
        # not silently alter decoder resolution, or the two controls oscillate.
        display_scale = getattr(self, "preview_render_scale", 0.65)
        decoder_backpressure = getattr(self, "_decoder_backpressure", None)
        if decoder_backpressure is not None:
            display_scale *= decoder_backpressure.resolution_factor
            decoder_fps = decoder_backpressure.fps_limit
        else:
            decoder_fps = getattr(self, "preview_fps", 30)
        unresolved_paths: list[str] = []
        video_items = getattr(self, "_cached_video_items", None)
        if video_items is None:
            video_items = tuple(
                item for item in self.scene.items()
                if isinstance(item, SourceItem)
                and item.source.source_type is SourceType.VIDEO
            )
        for item in video_items:
            observed_items = getattr(self, "_observed_video_items", None)
            if observed_items is not None and item not in observed_items:
                item.video_frame_ready.connect(self._video_frame_ready)
                observed_items.add(item)
            item.set_video_preview_budget(
                display_scale,
                decoder_fps,
            )
            source = item.source
            if not source.visible or not item.isVisible():
                item.set_video_preview_position(None)
                continue
            timing_end = source.timeline_start + source.timeline_duration
            if (timeline_seconds < source.timeline_start
                    or (source.timeline_duration > 0.0
                        and timeline_seconds >= timing_end)):
                item.set_video_preview_position(None)
                continue
            if source.video_timing_mode == "track" and not track_active:
                # `_track_at` retains nearby metadata during silence. Per-track
                # video must not inherit that retained track into the gap;
                # export occurrences end at the real track boundary as well.
                item.set_video_preview_position(None)
                continue
            paths = source_video_paths(source, track.video_paths)
            request_proxies = getattr(self, "_request_video_proxies", None)
            if callable(request_proxies):
                request_proxies(paths)
            durations: dict[str, float] = {}
            for path in paths:
                if path not in self._video_duration_cache:
                    media_path = Path(path)
                    if media_path.is_file():
                        unresolved_paths.append(path)
                    else:
                        self._video_duration_cache[path] = 0.0
                durations[path] = self._video_duration_cache.get(path, 0.0)
            if source.video_timing_mode == "track":
                actual_track_start = (
                    track_start if track_start is not None
                    else timeline_seconds - elapsed
                )
                source_elapsed = timeline_seconds - max(
                    actual_track_start, source.timeline_start,
                )
            else:
                source_elapsed = timeline_seconds - source.timeline_start
            position = resolve_video_position(source, paths, durations, source_elapsed)
            item.set_video_preview_playback(
                getattr(self, "_playing", False) and not getattr(self, "_scrubbing", False), source.video_speed,
            )
            if position is not None:
                proxy_resolver = getattr(self, "_preview_video_path", None)
                position_path = (
                    proxy_resolver(position.path)
                    if callable(proxy_resolver) else position.path
                )
            else:
                position_path = None
            position_seconds = position.seconds if position is not None else 0.0
            if force_seek:
                item.set_video_preview_position(
                    position_path, position_seconds, force_seek=True,
                )
            else:
                item.set_video_preview_position(position_path, position_seconds)
        if unresolved_paths:
            self._request_video_durations(unresolved_paths)

    def _request_video_durations(self, paths: list[str]) -> None:
        """Queue uncached FFprobe requests and run at most one subprocess at once."""
        active_path = (
            self._video_probe_worker.path
            if self._video_probe_worker is not None else ""
        )
        for path in paths:
            if (path in self._video_duration_cache or path == active_path
                    or path in self._video_probe_queued):
                continue
            self._video_probe_queue.append(path)
            self._video_probe_queued.add(path)
        self._start_next_video_probe()

    def _prefetch_upcoming_video_durations(self, track_index: int) -> None:
        """Probe the next tracks before a transport jump needs their first frame."""
        if track_index == self._last_video_prefetch_track_index:
            return
        self._last_video_prefetch_track_index = track_index
        paths: list[str] = []
        for item in self._cached_video_items:
            source = item.source
            if not source.visible:
                continue
            if source.video_timing_mode == "timeline":
                paths.extend(source.video_paths)
                continue
            for track in self.tracks[track_index:track_index + 3]:
                paths.extend(track.video_paths)
        if paths:
            unique_paths = list(dict.fromkeys(paths))
            self._request_video_durations(unique_paths)
            request_proxies = getattr(self, "_request_video_proxies", None)
            if callable(request_proxies):
                request_proxies(unique_paths)

    def _start_next_video_probe(self) -> None:
        if (self._closing or self._video_probe_worker is not None
                or not self._video_probe_queue):
            return
        path = self._video_probe_queue.pop(0)
        self._video_probe_queued.discard(path)
        worker = VideoDurationProbeWorker(path, self)
        self._video_probe_worker = worker
        worker.ready.connect(self._store_video_duration)
        worker.finished.connect(self._video_probe_finished)
        worker.start()

    def _store_video_duration(self, path: str, duration: float) -> None:
        """Cache successful and failed probes so neither is repeated per frame."""
        self._video_duration_cache[path] = max(0.0, float(duration))
        if duration <= 0.0:
            korean = self.translator.is_korean
            self._show_preview_error(
                "영상 정보를 확인할 수 없습니다" if korean else "Video information unavailable",
                (f"영상 길이를 읽지 못했습니다: {path}" if korean else
                 f"Could not read the video duration: {path}"),
            )
        self._schedule_refresh()

    def _video_probe_finished(self) -> None:
        worker = self.sender()
        if worker is self._video_probe_worker:
            self._video_probe_worker = None
        if isinstance(worker, VideoDurationProbeWorker):
            worker.deleteLater()
        self._start_next_video_probe()

    @staticmethod
    def _video_proxy_key(path: str) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(Path(path))

    def _preview_video_path(self, original_path: str) -> str | None:
        """Return a ready proxy, or hold the poster while one is being built."""
        key = self._video_proxy_key(original_path)
        resolved = self._video_proxy_paths.get(key)
        if resolved is not None:
            return resolved
        worker_is_preparing = (
            self._video_proxy_worker is not None
            and str(self._video_proxy_worker.source_path) == key
        )
        if key in self._video_proxy_queued or worker_is_preparing:
            return None
        return original_path

    def _request_video_proxies(self, paths: list[str]) -> None:
        """Queue each high-resolution preview input at most once per dialog."""
        cache = self._preview_proxy_cache
        if cache is None or self._preview_proxy_ffmpeg is None or self._closing:
            return
        changed = False
        for path in paths:
            source = Path(path)
            if not path or not source.is_file():
                continue
            key = self._video_proxy_key(path)
            if (
                key in self._video_proxy_paths
                or key in self._video_proxy_failures
                or key in self._video_proxy_queued
                or (
                    self._video_proxy_worker is not None
                    and str(self._video_proxy_worker.source_path) == key
                )
            ):
                continue
            cached = cache.cached_path(Path(key))
            if cached is not None:
                self._video_proxy_paths[key] = str(cached)
                changed = True
                continue
            self._video_proxy_queue.append(key)
            self._video_proxy_queued.add(key)
            self._video_proxy_total += 1
            changed = True
        if changed:
            self._start_next_video_proxy()
            self._update_frame_rate_label()
            self._sync_background_activity()

    def _start_next_video_proxy(self) -> None:
        if (
            self._closing
            or self._preview_proxy_cache is None
            or self._preview_proxy_ffmpeg is None
            or self._video_proxy_worker is not None
            or not self._video_proxy_queue
        ):
            return
        path = self._video_proxy_queue.pop(0)
        self._video_proxy_queued.discard(path)
        worker = PreviewProxyWorker(
            self._preview_proxy_ffmpeg, Path(path),
            self._preview_proxy_cache, self,
        )
        self._video_proxy_worker = worker
        worker.ready.connect(self._video_proxy_ready)
        worker.failed.connect(self._video_proxy_failed)
        worker.finished.connect(self._video_proxy_finished)
        worker.start()
        self._sync_background_activity()

    def _video_proxy_ready(
        self, original_path: str, preview_path: str, _proxied: bool,
    ) -> None:
        key = self._video_proxy_key(original_path)
        self._video_proxy_paths[key] = preview_path
        self._update_frame_rate_label()
        self._schedule_refresh()

    def _video_proxy_failed(self, original_path: str, message: str) -> None:
        """Keep original playback available after a nonfatal proxy failure."""
        key = self._video_proxy_key(original_path)
        self._video_proxy_failures.add(key)
        self._video_proxy_paths[key] = original_path
        korean = self.translator.is_korean
        self._show_preview_error(
            "저화질 미리보기를 만들지 못했습니다"
            if korean else "Could not create low-resolution preview",
            (
                f"원본 영상으로 재생합니다: {original_path}\n{message}"
                if korean else
                f"Playing the original video instead: {original_path}\n{message}"
            ),
            warning=True,
        )
        self._update_frame_rate_label()

    def _video_proxy_finished(self) -> None:
        worker = self.sender()
        if worker is self._video_proxy_worker:
            self._video_proxy_worker = None
        if isinstance(worker, PreviewProxyWorker):
            worker.deleteLater()
        self._start_next_video_proxy()
        self._update_frame_rate_label()
        self._sync_background_activity()

    def _sync_background_activity(self) -> None:
        """Mirror Preview's own background work in the main window's status-bar activity line."""
        bar = activity_for(self)
        if bar is None:
            return
        korean = self.translator.is_korean
        pending = len(self._video_proxy_queue) + (self._video_proxy_worker is not None)
        total = self._video_proxy_total
        if self._closing or not pending:
            self._video_proxy_total = 0
            bar.finish(VIDEO_PROXY_ACTIVITY)
        else:
            current = (Path(str(self._video_proxy_worker.source_path)).name
                       if self._video_proxy_worker is not None else "")
            done = max(0, total - pending)
            bar.update(
                VIDEO_PROXY_ACTIVITY, done / total if total else None,
                label="미리보기용 영상 준비" if korean else "Preparing preview video",
                detail=(f"{done + 1} / {total}개 · {current}" if korean else f"{done + 1} / {total} · {current}"),
            )
        analysis = self._analysis_worker
        track = next((track for track in self.tracks if track.id == self._analysis_track_id), None)
        if self._closing or analysis is None or track is None:
            bar.finish(AUDIO_ANALYSIS_ACTIVITY)
        else:
            bar.update(
                AUDIO_ANALYSIS_ACTIVITY, None,
                label="오디오 반응 효과 분석" if korean else "Analyzing audio for visualizers",
                detail=track.title or Path(track.file_path).name,
            )

    def _video_frame_ready(self) -> None:
        """Present an asynchronously decoded seek frame while paused or dragging."""
        if not self._playing:
            self._schedule_refresh()
        elif self._scrubbing:
            self._schedule_refresh(max(8, round(1000 / self.preview_fps)))

    def _refresh_source_partitions(self) -> None:
        """Cache source partitions; rebuilding them per playback frame is wasteful."""
        if not self._source_partition_dirty:
            return
        time_tokens = (
            "%current_time%", "%track_current_time%", "%video_current_time%",
        )
        dynamic_types = {
            SourceType.LYRICS, SourceType.PROGRESS_BAR, SourceType.NOW_PLAYING,
            SourceType.VIDEO,
        }
        audio_dynamic_ids: set[str] = set()
        bass_reactive = False
        always_dynamic_ids: set[str] = set()
        transition_backgrounds: list[tuple[str, float]] = []
        animated_source_ids: set[str] = set()
        visible_source_ids: set[str] = set()
        source_items: list[SourceItem] = []
        source_z_by_id: dict[str, float] = {}
        video_items: list[SourceItem] = []
        for item in self.scene.items():
            if (not isinstance(item, SourceItem) or not item.isVisible()
                    or not item.source.visible):
                continue
            source = item.source
            source_items.append(item)
            if source.source_type in {SourceType.TEXT, SourceType.LYRICS} and source.uses_bass_reaction:
                always_dynamic_ids.add(source.id)
                bass_reactive = True
            source_z_by_id[source.id] = float(source.z_index)
            visible_source_ids.add(source.id)
            if source.source_type is SourceType.VIDEO:
                video_items.append(item)
            if source.source_type in {
                SourceType.AUDIO_VISUALIZER, SourceType.AUDIO_WAVEFORM,
                SourceType.AUDIO_LEVEL_METER, SourceType.PARTICLE_OVERLAY,
            }:
                audio_dynamic_ids.add(source.id)
                continue
            if (source.source_type is SourceType.BACKGROUND
                    and source.background_mode == "album_art"):
                if source.background_ambient:
                    always_dynamic_ids.add(source.id)
                if source.background_bass_reactive:
                    always_dynamic_ids.add(source.id)
                    bass_reactive = True
                if source.background_track_transition:
                    transition_backgrounds.append((
                        source.id,
                        max(0.05, source.background_track_transition_seconds),
                    ))
            if (source.source_type in dynamic_types
                    or source.source_type is SourceType.TIME
                    or source.loop_motion != "none"
                    or source.timeline_start > 0.0
                    or source.timeline_duration > 0.0
                    or any(token in source.text.lower() for token in time_tokens)):
                always_dynamic_ids.add(source.id)
            if source.animation_in != "none" or source.animation_out != "none":
                animated_source_ids.add(source.id)
        self._cached_audio_dynamic_ids = frozenset(audio_dynamic_ids)
        self._bass_reactive = bass_reactive
        if (
            bass_reactive and self.visualizer_renderer is None
            and self._visualizer_ffmpeg_executable is not None
        ):
            self.visualizer_renderer = PythonVisualizerRenderer(
                self._visualizer_ffmpeg_executable
            )
        self._cached_always_dynamic_ids = frozenset(always_dynamic_ids)
        self._cached_track_transition_backgrounds = tuple(
            transition_backgrounds
        )
        self._cached_animated_source_ids = frozenset(animated_source_ids)
        self._cached_visible_source_ids = frozenset(visible_source_ids)
        self._cached_source_items = tuple(source_items)
        self._cached_source_z_by_id = source_z_by_id
        self._cached_animation_in_duration = max(
            (
                item.source.animation_in_duration for item in source_items
                if item.source.animation_in != "none"
            ),
            default=0.0,
        )
        self._cached_animation_out_duration = max(
            (
                item.source.animation_out_duration for item in source_items
                if item.source.animation_out != "none"
            ),
            default=0.0,
        )
        self._cached_video_items = tuple(video_items)
        self._dynamic_region_plans.clear()
        self._dynamic_region_buffers.clear()
        self._source_partition_dirty = False

    def _canvas_dynamic_source_ids(
        self, track_index: int, elapsed: float, phase: str | None,
    ) -> frozenset[str]:
        """Return only sources whose pixels can change at this playhead time."""
        dynamic_ids = set(self._cached_always_dynamic_ids)
        if track_index > 0:
            dynamic_ids.update(
                source_id
                for source_id, duration in self._cached_track_transition_backgrounds
                if 0.0 <= elapsed < duration
            )
        if phase is not None:
            dynamic_ids.update(self._cached_animated_source_ids)
        return frozenset(dynamic_ids)

    def _active_overlay_entries(
        self, timeline_seconds: float,
    ) -> tuple[tuple[int, VisualizerOverlay], ...]:
        """Resolve one timeline's active overlays once for all compositors."""
        if self._active_overlay_cache_seconds == timeline_seconds:
            return self._active_overlay_cache
        entries = tuple(
            (index, overlay) for index, overlay in enumerate(self.overlays)
            if timeline_seconds >= overlay.timeline_start
            and (
                overlay.timeline_duration <= 0.0
                or timeline_seconds
                < overlay.timeline_start + overlay.timeline_duration
            )
        )
        self._active_overlay_cache_seconds = timeline_seconds
        self._active_overlay_cache = entries
        return entries

    def _scaled_active_overlays(
        self, entries: tuple[tuple[int, VisualizerOverlay], ...],
        track_index: int,
    ) -> tuple[VisualizerOverlay, ...]:
        """Reuse immutable render-size overlays instead of replacing per frame."""
        signature = tuple(index for index, _overlay in entries)
        key = (signature, self._active_render_scale, track_index)
        cached = self._scaled_overlay_cache.get(key)
        if cached is not None:
            return cached
        cached = tuple(
            replace(
                overlay,
                width=max(1, round(overlay.width * self._active_render_scale)),
                height=max(1, round(overlay.height * self._active_render_scale)),
                **self._personal_overlay_changes(overlay, track_index),
            )
            for _index, overlay in entries
        )
        self._scaled_overlay_cache[key] = cached
        while len(self._scaled_overlay_cache) > 32:
            self._scaled_overlay_cache.pop(next(iter(self._scaled_overlay_cache)))
        return cached

    @staticmethod
    def _personal_overlay_changes(
        overlay: VisualizerOverlay, track_index: int,
    ) -> dict[str, str]:
        colors = overlay.personal_colors
        if not (0 <= track_index < len(colors)):
            return {}
        color = colors[track_index]
        return {
            "color": color,
            "particle_secondary_color": color,
            "level_meter_low_color": color,
            "level_meter_mid_color": color,
            "level_meter_high_color": color,
        }

    def _preview_composition_key(
        self, track: PlaylistTrack, phase: str | None,
        raster_dynamic_ids: frozenset[str], direct_video_ids: frozenset[str],
        timeline_seconds: float, *, ordered_canvas: bool, video_z_banded: bool,
    ) -> tuple[object, ...]:
        """Describe pixels that can change, without copying any frame images."""
        dynamic_video_ids = frozenset(
            item.source.id for item in self._cached_video_items
            if item.source.id in raster_dynamic_ids
            or item.source.id in direct_video_ids
        )
        video_revisions = tuple(
            (
                item.source.id,
                item.video_preview_revision,
                item.video_preview_active,
            )
            for item in self._cached_video_items
            if item.source.id in dynamic_video_ids
        )
        active_overlay_signature = tuple(
            index for index, _overlay
            in self._active_overlay_entries(timeline_seconds)
        )
        timeline_driven = bool(
            phase is not None
            or active_overlay_signature
            or raster_dynamic_ids - dynamic_video_ids
        )
        cadence_key = (
            round(timeline_seconds * self.preview_fps)
            if timeline_driven else -1
        )
        return (
            track.id,
            self._base_image.cacheKey(),
            self._active_render_scale,
            self.gpu_preview_enabled,
            ordered_canvas,
            video_z_banded,
            raster_dynamic_ids,
            direct_video_ids,
            video_revisions,
            active_overlay_signature,
            self._overlay_content_revision if active_overlay_signature else 0,
            cadence_key,
        )

    def _configure_gpu_video_color_filters(
        self, dynamic_ids: set[str], _ordered_canvas_required: bool,
        timeline_seconds: float,
    ) -> dict[str, GpuColorFilter]:
        """Configure direct frame retention and shader-owned color effects."""
        direct_gpu_available = (
            self.gpu_preview_enabled
            and self.gpu_surface is not None
        )
        if direct_gpu_available:
            active_overlays = [
                overlay for _index, overlay
                in self._active_overlay_entries(timeline_seconds)
            ]
            if active_overlays and self._requires_z_band_composition(active_overlays):
                direct_gpu_available = False
        result: dict[str, GpuColorFilter] = {}
        for item in self._cached_video_items:
            source = item.source
            effects_active = (
                source.brightness != 0.0
                or source.contrast != 0.0
                or source.video_saturation != 1.0
                or source.video_grayscale
            )
            simple_raster_style = (
                source.image_fit_mode == "stretch"
                and source.border_radius <= 0.0
                and source.outline_width <= 0.0
                and not source.shadow.enabled
            )
            direct_frame_enabled = (
                direct_gpu_available
                and source.id in dynamic_ids
                and simple_raster_style
                and source.blur <= 0.0
                and source.animation_in == "none"
                and source.animation_out == "none"
            )
            item.set_video_direct_gpu_frame(direct_frame_enabled)
            enabled = direct_frame_enabled and effects_active
            item.set_video_gpu_color_filter(enabled)
            if enabled and item.video_gpu_color_filter_ready:
                result[source.id] = GpuColorFilter(
                    brightness=source.brightness,
                    contrast=source.contrast,
                    saturation=source.video_saturation,
                    grayscale=source.video_grayscale,
                )
        return result

    def _direct_gpu_video_layers(
        self, dynamic_ids: set[str], _ordered_canvas_required: bool,
        timeline_seconds: float, color_filters: dict[str, GpuColorFilter],
    ) -> tuple[frozenset[str], list[tuple[float, GpuPreviewLayer]]]:
        """Build direct texture layers for videos that need no scene styling.

        Styled, animated, or inactive videos deliberately remain in the
        CanvasSnapshot path so preview appearance stays unchanged.
        """
        if (
            not self.gpu_preview_enabled
            or self.gpu_surface is None
        ):
            return frozenset(), []
        active_overlays = [
            overlay for _index, overlay
            in self._active_overlay_entries(timeline_seconds)
        ]
        if active_overlays and self._requires_z_band_composition(active_overlays):
            # The audio Z-band compositor currently owns the complete Canvas
            # stack. Preserve that established path when both systems overlap.
            return frozenset(), []
        scale = self._active_render_scale
        direct_ids: set[str] = set()
        layers: list[tuple[float, GpuPreviewLayer]] = []
        for item in self._cached_video_items:
            source = item.source
            timing_end = source.timeline_start + source.timeline_duration
            timeline_active = (
                timeline_seconds >= source.timeline_start
                and (
                    source.timeline_duration <= 0.0
                    or timeline_seconds < timing_end
                )
            )
            simple_raster_style = (
                source.image_fit_mode == "stretch"
                and source.border_radius <= 0.0
                and source.outline_width <= 0.0
                and not source.shadow.enabled
                and source.animation_in == "none"
                and source.animation_out == "none"
            )
            if (
                source.id not in dynamic_ids
                or not source.visible
                or not timeline_active
                or not simple_raster_style
                or not item.video_preview_active
            ):
                continue
            frame = item.video_preview_frame()
            gpu_frame, gpu_serial = item.video_preview_gpu_frame()
            if frame.isNull() and not gpu_frame.isValid():
                continue
            target = QRectF(
                round(source.x * scale),
                round(source.y * scale),
                max(1, round(source.width * source.scale * scale)),
                max(1, round(source.height * source.scale * scale)),
            )
            direct_ids.add(source.id)
            layers.append((
                source.z_index,
                GpuPreviewLayer(
                    ("video-direct", source.id), frame, target,
                    opacity=source.opacity,
                    rotation=source.rotation,
                    color_filter=color_filters.get(source.id),
                    video_frame=gpu_frame if gpu_frame.isValid() else None,
                    native_serial=gpu_serial,
                    native_scale=item.video_preview_scale,
                ),
            ))
        return frozenset(direct_ids), layers

    def _ensure_video_z_band_cache(
        self, track: PlaylistTrack, track_index: int, start: float,
        hidden_source_ids: frozenset[str], direct_video_ids: frozenset[str],
    ) -> None:
        """Cache static Canvas bands surrounding direct video texture layers."""
        z_bands = CanvasSnapshot.z_bands(self.scene, set(direct_video_ids))
        cache_key = (
            track.id,
            hidden_source_ids,
            direct_video_ids,
            tuple(z_bands),
            self._active_render_scale,
        )
        if (
            self._video_z_band_key == cache_key
            and not self._base_image.isNull()
        ):
            return
        common = dict(
            elapsed_seconds=0.0,
            hide_visualizers=set(self._cached_audio_dynamic_ids),
            playlist_duration_seconds=self._playlist_duration(),
            playlist_tracks=self.tracks,
            output_scale=self._active_render_scale,
            hide_source_ids=hidden_source_ids,
            timeline_seconds=start,
        )
        first_min, first_max = z_bands[0]
        self._base_image = CanvasSnapshot.capture_track(
            self.scene, track, track_index + 1, len(self.tracks), start,
            None, 1.0, z_min=first_min, z_max=first_max, **common,
        )
        canvas_rect = QRectF(
            0.0, 0.0, self._base_image.width(), self._base_image.height(),
        )
        foreground_layers: list[tuple[float, int, GpuPreviewLayer]] = []
        for index, (z_min, z_max) in enumerate(z_bands[1:]):
            foreground = CanvasSnapshot.capture_track(
                self.scene, track, track_index + 1, len(self.tracks), start,
                None, 1.0, z_min=z_min, z_max=z_max,
                transparent=True, **common,
            )
            foreground_layers.append((
                z_min if z_min is not None else -10_000.0,
                1,
                GpuPreviewLayer(
                    ("video-z-foreground", index, z_min, z_max, track.id),
                    foreground, canvas_rect,
                ),
            ))
        self._video_z_band_key = cache_key
        self._video_z_band_layers = foreground_layers
        self._base_track_id = track.id
        self._base_elapsed = 0.0
        self._base_hidden_source_ids = hidden_source_ids

    def _source_group_z(self, source_ids: set[str] | frozenset[str]) -> float:
        """Return the lowest scene Z used by one captured dynamic group."""
        z_values = [
            self._cached_source_z_by_id[source_id]
            for source_id in source_ids
            if source_id in self._cached_source_z_by_id
        ]
        return min(z_values, default=0.0)

    @staticmethod
    def _cpu_fallback_layer_image(layer: GpuPreviewLayer) -> QImage:
        """Preserve filtered output if one GPU submission falls back to CPU."""
        color_filter = layer.color_filter
        source_image = layer.image
        if source_image.isNull() and layer.video_frame is not None:
            # Exceptional GPU submission fallback. Normal RHI frames never take
            # this conversion path; retaining correctness is more important
            # after a context or shader failure.
            source_image = layer.video_frame.toImage()
        if color_filter is None or not color_filter.active:
            return source_image
        return filter_video_frame(
            source_image,
            VideoFrameFilterSettings(
                source_image.width(), source_image.height(),
                brightness=color_filter.brightness,
                contrast=color_filter.contrast,
                saturation=color_filter.saturation,
                grayscale=color_filter.grayscale,
            ),
        )

    @classmethod
    def _paint_cpu_fallback_layer(
        cls, painter: QPainter, layer: GpuPreviewLayer,
    ) -> None:
        """Paint a GPU layer equivalently when submission falls back to CPU."""
        image = cls._cpu_fallback_layer_image(layer)
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, layer.opacity)))
        if layer.rotation:
            center = layer.target.center()
            painter.translate(center)
            painter.rotate(layer.rotation)
            target = QRectF(
                -layer.target.width() / 2.0,
                -layer.target.height() / 2.0,
                layer.target.width(), layer.target.height(),
            )
        else:
            target = layer.target
        painter.drawImage(target, image)
        painter.restore()

    def _canvas_dynamic_requires_z_composition(
        self, dynamic_ids: set[str], timeline_seconds: float,
    ) -> bool:
        """Return whether dirty-region layering would reverse Canvas Z order."""
        if not dynamic_ids:
            return False
        audio_ids = set(self._cached_audio_dynamic_ids)
        active_dynamic_z: list[float] = []
        static_z: list[float] = []
        for item in self._cached_source_items:
            source = item.source
            timing_end = source.timeline_start + source.timeline_duration
            active = (
                timeline_seconds >= source.timeline_start
                and (
                    source.timeline_duration <= 0.0
                    or timeline_seconds < timing_end
                )
            )
            if source.id in dynamic_ids:
                if active:
                    active_dynamic_z.append(source.z_index)
            elif source.id not in audio_ids and active:
                static_z.append(source.z_index)
        if not active_dynamic_z:
            return False
        lowest_dynamic_z = min(active_dynamic_z)
        return any(z_index >= lowest_dynamic_z for z_index in static_z)

    def _dynamic_capture_regions(self, dynamic_ids: set[str]) -> tuple[tuple[frozenset[str], QRectF], ...]:
        """Return non-overlapping dirty regions for the visible dynamic sources."""
        key = frozenset(dynamic_ids)
        cached = self._dynamic_region_plans.get(key)
        if cached is not None:
            return cached
        source_items = {
            item.source.id: item for item in self._cached_source_items
            if item.source.id in dynamic_ids
        }
        groups: list[tuple[set[str], QRectF, float]] = []
        for source_id, item in source_items.items():
            source = item.source
            padding = max(8.0, source.outline_width)
            if source.shadow.enabled:
                padding += source.shadow.blur_radius + abs(source.shadow.offset_x) + abs(source.shadow.offset_y)
            if source.source_type is SourceType.LYRICS:
                padding += source.font_size + source.subtitle_line_spacing + source.subtitle_previous_blur + 8.0
            if source.source_type is SourceType.NOW_PLAYING:
                padding += 28.0
            if (source.animation_in != "none" or source.animation_out != "none"
                    or source.loop_motion != "none"):
                padding += max(
                    min(180.0, max(72.0, max(source.width, source.height) * 0.22)),
                    motion_padding(
                        {source.animation_in, source.animation_out},
                        source.loop_motion, source.loop_motion_amount,
                        source.width, source.height,
                    ),
                ) + 8.0
            rect = item.sceneBoundingRect().adjusted(-padding, -padding, padding, padding)
            rect = rect.intersected(self.scene.artboard_rect)
            if rect.isEmpty():
                continue
            overlapping = [index for index, (_ids, group_rect, _z) in enumerate(groups) if group_rect.intersects(rect)]
            if not overlapping:
                groups.append(({source_id}, rect, source.z_index))
                continue
            merged_ids = {source_id}
            merged_rect = QRectF(rect)
            merged_z = source.z_index
            for index in reversed(overlapping):
                existing_ids, existing_rect, existing_z = groups.pop(index)
                merged_ids.update(existing_ids)
                merged_rect = merged_rect.united(existing_rect)
                merged_z = min(merged_z, existing_z)
            groups.append((merged_ids, merged_rect, merged_z))
        result = tuple(
            (frozenset(source_ids), QRectF(rect.toAlignedRect()))
            for source_ids, rect, _z in sorted(groups, key=lambda entry: entry[2])
        )
        self._dynamic_region_plans[key] = result
        return result

    def _submit_gpu_composition(
        self, track: PlaylistTrack, track_index: int, elapsed: float, start: float,
        phase: str | None, phase_progress: float, phase_duration: float,
        dynamic_layers: list[GpuPreviewLayer],
    ) -> bool:
        """Submit ordered Canvas layers without flattening them through QPainter."""
        if self.gpu_surface is None or self._base_image.isNull():
            return False
        try:
            active_overlays, overlay_images = self._prepared_overlay_images(
                track, elapsed,
            )
            if active_overlays and self._requires_z_band_composition(active_overlays):
                layers = self._gpu_z_band_layers(
                    track, track_index, elapsed, start, phase, phase_progress,
                    phase_duration, active_overlays, overlay_images,
                )
            else:
                canvas_rect = QRectF(
                    0.0, 0.0,
                    float(self._base_image.width()),
                    float(self._base_image.height()),
                )
                layers = [
                    GpuPreviewLayer(
                        ("canvas-base", track.id, self._base_hidden_source_ids),
                        self._base_image, canvas_rect,
                    ),
                    *dynamic_layers,
                ]
                layers.extend(
                    self._gpu_audio_layers(active_overlays, overlay_images)
                )
            self.gpu_surface.set_layers(self._base_image.size(), layers)
            self._gpu_health.frame_queued(monotonic())
            self._gpu_watchdog.start()
            return True
        except Exception as error:
            LOGGER.warning(
                "GPU preview layer preparation failed; using flattened CPU frame",
                exc_info=True,
            )
            korean = self.translator.is_korean
            self._show_preview_error(
                "GPU 합성을 일부 사용할 수 없습니다" if korean else "GPU composition partially unavailable",
                str(error) or (
                    "GPU 레이어 준비에 실패해 현재 프레임을 CPU에서 합성했습니다."
                    if korean else
                    "GPU layer preparation failed, so the current frame was composed on the CPU."
                ),
                warning=True,
            )
            return False

    def _prepared_overlay_images(
        self, track: PlaylistTrack, elapsed: float,
    ) -> tuple[list[VisualizerOverlay], tuple[QImage, ...]]:
        """Return the current audio-reactive layers for either compositor."""
        if self.visualizer_renderer is None:
            return [], ()
        timeline_seconds = self.timeline.value() / TIMELINE_SCALE
        active_entries = self._active_overlay_entries(timeline_seconds)
        if not active_entries:
            return [], ()
        overlay_signature = tuple(index for index, _overlay in active_entries)
        active_overlays = [overlay for _index, overlay in active_entries]
        bands = max(max(4, overlay.bar_count) for overlay in self.overlays)
        self._ensure_track_analysis(track, bands)
        analyzed = self._track_levels.get(track.id)
        analysis = analyzed if analyzed is not None else self._idle_overlay_analysis(bands)
        frame_index = max(0, round(elapsed * self.preview_fps))
        track_index = next(
            (index for index, candidate in enumerate(self.tracks)
             if candidate.id == track.id),
            -1,
        )
        scaled_overlays = self._scaled_active_overlays(
            active_entries, track_index,
        )
        self._request_overlay_frames(
            track.id, frame_index, scaled_overlays, overlay_signature, analysis,
        )
        images = self._overlay_layers_for_frame(
            track.id, frame_index, overlay_signature,
        )
        # Overlay rendering runs asynchronously.  During its first frame the
        # active overlay list is already known while no matching images exist
        # yet.  Submit the set only when it is complete so layer indices and Z
        # positions cannot be paired with the wrong image.
        if len(images) != len(active_overlays):
            return [], ()
        return active_overlays, self._animated_overlay_images(
            active_overlays, images, timeline_seconds,
        )

    def _animated_overlay_images(
        self, overlays: list[VisualizerOverlay], images: tuple[QImage, ...],
        timeline_seconds: float,
    ) -> tuple[QImage, ...]:
        """Apply export's per-track entrance/exit to audio-reactive layers.

        Same state function and windows as PythonVisualizerRenderer in export,
        so visualizers/meters enter and exit at every track change, and at a
        crossfade/AutoMix handover, exactly as the rendered video does.
        """
        plan = getattr(self, "_compiled_plan", None)
        windows: tuple = ()
        if plan is not None and plan.presentation.windows:
            cached = getattr(self, "_reactive_windows_cache", None)
            if cached is None or cached[0] is not plan:
                cached = self._reactive_windows_cache = (plan, reactive_layer_windows(plan))
            windows = cached[1]
        elif all(getattr(overlay, "loop_motion", "none") == "none" for overlay in overlays):
            return images
        animated = []
        for overlay, image in zip(overlays, images, strict=True):
            if not image.isNull():
                # The preview layer may be downscaled; size the motion to it.
                image = PythonVisualizerRenderer.transform_layer(
                    image, overlay, timeline_seconds, windows,
                    (float(image.width()), float(image.height())),
                )
            animated.append(image)
        return tuple(animated)

    def _gpu_audio_layers(
        self, overlays: list[VisualizerOverlay], images: tuple[QImage, ...],
    ) -> list[GpuPreviewLayer]:
        """Map visualizer images into scaled Canvas coordinates."""
        if len(overlays) != len(images):
            return []
        return [
            GpuPreviewLayer(
                ("audio-overlay", index), image,
                QRectF(
                    round(overlay.x * self._active_render_scale),
                    round(overlay.y * self._active_render_scale),
                    image.width(), image.height(),
                ),
                rotation=overlay.rotation,
            )
            for index, (overlay, image) in enumerate(
                zip(overlays, images, strict=True)
            )
        ]

    def _gpu_z_band_layers(
        self, track: PlaylistTrack, track_index: int, elapsed: float, start: float,
        phase: str | None, phase_progress: float, phase_duration: float,
        overlays: list[VisualizerOverlay], images: tuple[QImage, ...],
    ) -> list[GpuPreviewLayer]:
        """Preserve export Z ordering while leaving final blending on the GPU."""
        audio_ids = set(self._cached_audio_dynamic_ids)
        z_bands = CanvasSnapshot.z_bands(self.scene, audio_ids)
        if len(z_bands) < 2:
            canvas_rect = QRectF(
                0.0, 0.0, self._base_image.width(), self._base_image.height(),
            )
            return [
                GpuPreviewLayer("canvas-base", self._base_image, canvas_rect),
                *self._gpu_audio_layers(overlays, images),
            ]
        common = dict(
            elapsed_seconds=elapsed,
            hide_visualizers=audio_ids,
            playlist_duration_seconds=self._playlist_duration(),
            playlist_tracks=self.tracks,
            output_scale=self._active_render_scale,
            timeline_seconds=self.timeline.value() / TIMELINE_SCALE,
            animation_phase_duration=phase_duration,
            junction=getattr(self, "_frame_junction", None),
            bass_level=self._bass_level(track, elapsed),
            music_levels=self._music_reaction_levels(track, elapsed),
        )
        base = CanvasSnapshot.capture_track(
            self.scene, track, track_index + 1, len(self.tracks), start,
            phase, phase_progress, z_max=z_bands[0][1], **common,
        )
        canvas_rect = QRectF(0.0, 0.0, base.width(), base.height())
        layers: list[GpuPreviewLayer] = [
            GpuPreviewLayer(("z-base", z_bands[0][1]), base, canvas_rect),
        ]
        ordered: list[tuple[float, int, GpuPreviewLayer]] = []
        for index, layer in enumerate(self._gpu_audio_layers(overlays, images)):
            ordered.append((overlays[index].z_index, 0, layer))
        for index, (z_min, z_max) in enumerate(z_bands[1:]):
            foreground = CanvasSnapshot.capture_track(
                self.scene, track, track_index + 1, len(self.tracks), start,
                phase, phase_progress, z_min=z_min, z_max=z_max,
                transparent=True, **common,
            )
            ordered.append((
                z_min if z_min is not None else -10_000.0,
                1,
                GpuPreviewLayer(
                    ("z-foreground", index, z_min, z_max), foreground,
                    QRectF(0.0, 0.0, foreground.width(), foreground.height()),
                ),
            ))
        layers.extend(layer for _z, _kind, layer in sorted(ordered, key=lambda value: (value[0], value[1])))
        return layers

    def _composite_export_overlays(
        self, track: PlaylistTrack, elapsed: float,
    ) -> None:
        """Composite pre-rendered Python overlay pixels without blocking the UI."""
        if self.visualizer_renderer is None or self._image.isNull():
            return
        try:
            timeline_seconds = self.timeline.value() / TIMELINE_SCALE
            active_entries = self._active_overlay_entries(timeline_seconds)
            if not active_entries:
                return
            overlay_signature = tuple(index for index, _overlay in active_entries)
            active_overlays = [overlay for _index, overlay in active_entries]
            bands = max(max(4, overlay.bar_count) for overlay in self.overlays)
            self._ensure_track_analysis(track, bands)
            analyzed = self._track_levels.get(track.id)
            analysis = analyzed if analyzed is not None else self._idle_overlay_analysis(bands)
            frame_index = max(0, round(elapsed * self.preview_fps))
            track_index = next(
                (index for index, candidate in enumerate(self.tracks)
                 if candidate.id == track.id),
                -1,
            )
            scaled_overlays = self._scaled_active_overlays(
                active_entries, track_index,
            )
            self._request_overlay_frames(
                track.id, frame_index, scaled_overlays,
                overlay_signature, analysis,
            )
            layers = self._overlay_layers_for_frame(
                track.id, frame_index, overlay_signature,
            )
            if not layers:
                return
            layers = self._animated_overlay_images(active_overlays, layers, timeline_seconds)
            if self._requires_z_band_composition(active_overlays):
                if self._composite_overlays_in_canvas_order(
                    track, elapsed, active_overlays, layers,
                ):
                    return
            painter = QPainter(self._image)
            for overlay, layer in zip(active_overlays, layers, strict=True):
                self._paint_overlay_layer(painter, overlay, layer)
            painter.end()
        except Exception as error:
            if not self._overlay_error_reported:
                LOGGER.warning("Preview overlay compositing failed", exc_info=True)
                self._overlay_error_reported = True
                korean = self.translator.is_korean
                self._show_preview_error(
                    "비주얼라이저를 표시하지 못했습니다" if korean else "Visualizer could not be displayed",
                    str(error) or (
                        "비주얼라이저 프레임 합성 중 오류가 발생했습니다."
                        if korean else
                        "An error occurred while compositing the visualizer frame."
                    ),
                )
            return

    def _requires_z_band_composition(
        self, active_overlays: list[VisualizerOverlay],
    ) -> bool:
        """Return whether a Canvas source must be painted over an audio layer."""
        if not active_overlays:
            return False
        lowest_dynamic_z = min(overlay.z_index for overlay in active_overlays)
        audio_ids = set(self._cached_audio_dynamic_ids)
        return any(
            item.source.id not in audio_ids
            and item.source.z_index >= lowest_dynamic_z
            for item in self._cached_source_items
        )

    def _composite_overlays_in_canvas_order(
        self, track: PlaylistTrack, elapsed: float,
        active_overlays: list[VisualizerOverlay], layers: tuple[QImage, ...],
    ) -> bool:
        """Interleave preview overlays and Canvas bands exactly like export."""
        selected, junction = self._visual_track_at(self.timeline.value() / TIMELINE_SCALE)
        if selected is None:
            return False
        track_index, _selected_track, _selected_elapsed, start = selected
        phase, phase_progress, phase_duration = self._frame_animation_state(
            track, elapsed, junction,
        )
        audio_ids = set(self._cached_audio_dynamic_ids)
        z_bands = CanvasSnapshot.z_bands(self.scene, audio_ids)
        if len(z_bands) < 2:
            return False
        common = dict(
            elapsed_seconds=elapsed,
            hide_visualizers=audio_ids,
            playlist_duration_seconds=self._playlist_duration(),
            playlist_tracks=self.tracks,
            output_scale=self._active_render_scale,
            timeline_seconds=self.timeline.value() / TIMELINE_SCALE,
            animation_phase_duration=phase_duration,
            junction=junction,
            bass_level=self._bass_level(track, elapsed),
            music_levels=self._music_reaction_levels(track, elapsed),
        )
        base = CanvasSnapshot.capture_track(
            self.scene, track, track_index + 1, len(self.tracks), start,
            phase, phase_progress, z_max=z_bands[0][1], **common,
        )
        foreground_bands: list[tuple[float, QImage]] = []
        for z_min, z_max in z_bands[1:]:
            foreground = CanvasSnapshot.capture_track(
                self.scene, track, track_index + 1, len(self.tracks), start,
                phase, phase_progress, z_min=z_min, z_max=z_max,
                transparent=True, **common,
            )
            foreground_bands.append((
                z_min if z_min is not None else -10_000.0, foreground,
            ))
        self._foreground_bands = foreground_bands
        self._image = base
        entries: list[tuple[float, int, int]] = [
            (overlay.z_index, 0, index)
            for index, overlay in enumerate(active_overlays)
        ]
        entries.extend(
            (z_index, 1, index)
            for index, (z_index, _image) in enumerate(foreground_bands)
        )
        painter = QPainter(self._image)
        for _z_index, kind, index in sorted(entries):
            if kind == 0:
                self._paint_overlay_layer(
                    painter, active_overlays[index], layers[index],
                )
            else:
                painter.drawImage(0, 0, foreground_bands[index][1])
        painter.end()
        return True

    def _paint_overlay_layer(
        self, painter: QPainter, overlay: VisualizerOverlay, layer: QImage,
    ) -> None:
        """Paint one already-scaled audio-reactive layer at its Canvas transform."""
        scaled_width = layer.width()
        scaled_height = layer.height()
        if overlay.rotation:
            center_x = round(
                (overlay.x + overlay.width / 2) * self._active_render_scale
            )
            center_y = round(
                (overlay.y + overlay.height / 2) * self._active_render_scale
            )
            painter.save()
            painter.translate(center_x, center_y)
            painter.rotate(overlay.rotation)
            painter.drawImage(-scaled_width // 2, -scaled_height // 2, layer)
            painter.restore()
        else:
            painter.drawImage(
                round(overlay.x * self._active_render_scale),
                round(overlay.y * self._active_render_scale), layer,
            )

    def _schedule_refresh(self, delay_ms: int = 0) -> None:
        """Coalesce worker completions into one GUI-thread preview composition."""
        if self._closing or self._refresh_queued:
            return
        self._refresh_queued = True
        self._refresh_timer.start(delay_ms)

    def _run_scheduled_refresh(self) -> None:
        self._refresh_queued = False
        if not self._closing:
            self.refresh_preview()

    def _preview_worker_failed(self, message: str) -> None:
        """Retain a nonfatal worker error for diagnostics without closing playback."""
        LOGGER.warning("Preview background worker failed: %s", message)
        korean = self.translator.is_korean
        self._show_preview_error(
            "미리보기 일부를 처리하지 못했습니다" if korean else "Part of the preview could not be processed",
            message,
        )

    def _show_preview_error(
        self, title: str, detail: str, *, warning: bool = False,
    ) -> None:
        """Show one bounded, non-modal preview issue with optional full details."""
        normalized_detail = str(detail or title).strip()
        signature = (title, normalized_detail)
        if signature in self._preview_error_signatures:
            return
        self._preview_error_signatures.append(signature)
        if len(self._preview_error_signatures) > 20:
            self._preview_error_signatures.pop(0)
        self._last_preview_error_title = title
        self._last_preview_error_detail = normalized_detail
        one_line = " ".join(normalized_detail.splitlines())
        if len(one_line) > 180:
            one_line = one_line[:177].rstrip() + "…"
        self.error_title_label.setText(title)
        self.error_message_label.setText(one_line)
        self.error_message_label.setToolTip(normalized_detail)
        self.error_banner.setProperty("severity", "warning" if warning else "error")
        self.error_banner.style().unpolish(self.error_banner)
        self.error_banner.style().polish(self.error_banner)
        self.error_banner.show()

    def _show_preview_error_details(self) -> None:
        if not self._last_preview_error_detail:
            return
        QMessageBox.warning(
            self,
            self._last_preview_error_title,
            self._last_preview_error_detail,
        )

    def _idle_overlay_analysis(self, bands: int) -> dict[str, np.ndarray]:
        """Return a harmless placeholder until the real track analysis is ready."""
        normalized_bands = max(4, bands)
        cached = self._idle_overlay_analyses.get(normalized_bands)
        if cached is not None:
            return cached
        cached = {
            "levels": np.zeros((1, normalized_bands), dtype=np.float32),
            "waveform": np.zeros((1, max(32, normalized_bands)), dtype=np.float32),
        }
        self._idle_overlay_analyses[normalized_bands] = cached
        return cached

    def _request_overlay_frames(self, track_id: str, frame_index: int,
                                overlays: tuple[VisualizerOverlay, ...],
                                overlay_signature: OverlaySignature,
                                analysis: dict[str, np.ndarray]) -> None:
        """Keep a short, newest-first visualizer frame queue ready for presentation."""
        if self._closing:
            return
        if self._overlay_worker is not None and self._overlay_worker.busy:
            if (
                self._overlay_worker.track_id != track_id
                or self._overlay_worker.overlay_signature != overlay_signature
            ):
                self._overlay_worker.cancel()
            return
        end_frame = frame_index + self._overlay_prefetch_count
        missing_frame = next(
            (index for index in range(frame_index, end_frame)
             if (track_id, overlay_signature, index)
             not in self._overlay_frame_cache),
            None,
        )
        if missing_frame is None:
            return
        request = (
            track_id, self.preview_fps, self._overlay_generation, missing_frame,
            min(self._overlay_prefetch_count, end_frame - missing_frame),
            overlays, overlay_signature, analysis,
        )
        if self._overlay_worker is None:
            self._overlay_worker = OverlayFrameWorker(*request, parent=self)
            self._overlay_worker.ready.connect(self._store_overlay_frames)
            self._overlay_worker.failed.connect(self._preview_worker_failed)
            self._overlay_worker.finished.connect(self._overlay_worker_finished)
        else:
            self._overlay_worker.prepare(*request)
        self._overlay_worker.start()

    def _store_overlay_frames(
        self, track_id: str, fps: int, generation: int,
        overlay_signature: object, frames: object,
    ) -> None:
        """Receive completed QImage layers; no Canvas/QWidget access occurred in the worker."""
        if (fps != self.preview_fps or generation != self._overlay_generation
                or not isinstance(overlay_signature, tuple)
                or not all(isinstance(index, int) for index in overlay_signature)
                or not isinstance(frames, list)):
            return
        for frame_index, layers in frames:
            if isinstance(frame_index, int) and isinstance(layers, tuple):
                self._overlay_frame_cache[
                    (track_id, overlay_signature, frame_index)
                ] = layers
        self._overlay_content_revision += 1
        self._trim_overlay_frame_cache(track_id)
        self._schedule_refresh()

    def _overlay_worker_finished(self) -> None:
        """Join native thread teardown before reusing the retained worker."""
        worker = self.sender()
        if isinstance(worker, OverlayFrameWorker):
            worker.wait()
            worker.busy = False
            if self._closing:
                if worker is self._overlay_worker:
                    self._overlay_worker = None
                worker.deleteLater()
        self._schedule_refresh()

    def _overlay_layers_for_frame(
        self, track_id: str, frame_index: int,
        overlay_signature: OverlaySignature,
    ) -> tuple[QImage, ...]:
        """Use the exact frame when ready, otherwise keep the newest completed frame."""
        overlay_count = len(overlay_signature)
        layers = self._overlay_frame_cache.get(
            (track_id, overlay_signature, frame_index)
        )
        if layers is not None and len(layers) == overlay_count:
            self._last_overlay_layers = layers
            self._last_overlay_track_id = track_id
            self._last_overlay_signature = overlay_signature
            return layers
        if (
            self._last_overlay_track_id == track_id
            and self._last_overlay_signature == overlay_signature
            and len(self._last_overlay_layers) == overlay_count
        ):
            return self._last_overlay_layers
        prior_frames = [
            (cached_index, cached_layers)
            for (
                cached_track_id, cached_signature, cached_index
            ), cached_layers in self._overlay_frame_cache.items()
            if cached_track_id == track_id and cached_index < frame_index
            and cached_signature == overlay_signature
            and len(cached_layers) == overlay_count
        ]
        if prior_frames:
            _cached_index, cached_layers = max(prior_frames, key=lambda entry: entry[0])
            self._last_overlay_layers = cached_layers
            self._last_overlay_track_id = track_id
            self._last_overlay_signature = overlay_signature
            return cached_layers
        return ()

    def _trim_overlay_frame_cache(self, active_track_id: str) -> None:
        """Bound cached QImages so a long playlist never accumulates frame memory."""
        # The drawn track, not the audible one: across a mix overlap they differ,
        # and trimming around the other track's time discarded every frame just
        # stored, freezing visualizers/meters until the handover.
        selected, _junction = self._visual_track_at(self.timeline.value() / TIMELINE_SCALE)
        current_frame = round(selected[2] * self.preview_fps) if selected else 0
        minimum = max(0, current_frame - 3)
        maximum = current_frame + self._overlay_prefetch_count * 3
        for key in list(self._overlay_frame_cache):
            track_id, _overlay_signature, frame_index = key
            if track_id != active_track_id or frame_index < minimum or frame_index > maximum:
                self._overlay_frame_cache.pop(key, None)

    def _clear_overlay_frames(self, track_id: str | None = None) -> None:
        """Discard stale prefetch output after a seek, track analysis, or quality change."""
        self._overlay_content_revision += 1
        if (track_id is None or self._overlay_worker is None
                or self._overlay_worker.track_id == track_id):
            self._overlay_generation += 1
        if (self._overlay_worker is not None and self._overlay_worker.isRunning()
                and (track_id is None or self._overlay_worker.track_id == track_id)):
            self._overlay_worker.cancel()
        if track_id is None:
            self._overlay_frame_cache.clear()
            self._last_overlay_layers = ()
            self._last_overlay_track_id = ""
            self._last_overlay_signature = ()
            return
        for key in [key for key in self._overlay_frame_cache if key[0] == track_id]:
            self._overlay_frame_cache.pop(key, None)
        if self._last_overlay_track_id == track_id:
            self._last_overlay_layers = ()
            self._last_overlay_track_id = ""
            self._last_overlay_signature = ()

    def _ensure_track_analysis(self, track: PlaylistTrack, bands: int) -> None:
        """Analyze each selected track once, then reuse its moving FFT frames."""
        profiles = tuple({item.source.music_reaction_profile for item in self._cached_source_items
                          if item.source.source_type in {SourceType.TEXT, SourceType.LYRICS}
                          and item.source.uses_bass_reaction})
        cached = self._track_levels.get(track.id)
        if self.visualizer_renderer is None or (
            isinstance(cached, dict) and all(profile in cached.get("music_envelopes", {}) for profile in profiles)
        ):
            return
        if self._closing or self._analysis_worker is not None:
            return
        self._analysis_track_id = track.id
        self._analysis_worker = AudioAnalysisWorker(
            self.visualizer_renderer, track, bands, self.preview_fps,
            needs_waveform=any(overlay.kind == "waveform" for overlay in self.overlays),
            overlays=tuple(self.overlays),
            parent=self, music_profiles=profiles, cached_analysis=cached,
        )
        self._analysis_worker.ready.connect(self._store_track_levels)
        self._analysis_worker.failed.connect(self._preview_worker_failed)
        self._analysis_worker.finished.connect(self._analysis_finished)
        self._analysis_worker.start()
        self._sync_background_activity()

    def _music_reaction_levels(self, track: PlaylistTrack, elapsed: float) -> dict[str, float]:
        if not self._bass_reactive:
            return {}
        self._ensure_track_analysis(track, 24)
        analysis = self._track_levels.get(track.id, {})
        envelopes = analysis.get("music_envelopes", {}) if isinstance(analysis, dict) else {}
        return {
            item.source.id: PythonVisualizerRenderer.music_reaction_level(
                envelopes.get(item.source.music_reaction_profile), self.preview_fps,
                elapsed, item.source.music_reactive_offset,
            )
            for item in self._cached_source_items
            if item.source.source_type in {SourceType.TEXT, SourceType.LYRICS}
            and item.source.uses_bass_reaction
        }

    def _bass_level(self, track: PlaylistTrack, elapsed: float) -> float:
        if not self._bass_reactive:
            return 0.0
        self._ensure_track_analysis(track, 24)
        analysis = self._track_levels.get(track.id)
        if not isinstance(analysis, dict):
            return 0.0
        envelope = analysis.get("bass")
        if envelope is None:
            return 0.0
        try:
            frame = max(0, round(elapsed * self.preview_fps))
            return float(envelope[min(frame, len(envelope) - 1)])
        except (IndexError, TypeError, ValueError):
            return 0.0

    def _store_track_levels(self, track_id: str, fps: int, levels: object) -> None:
        """Receive full-track FFT levels and refresh the current preview image."""
        if fps != self.preview_fps:
            return
        # Full-track FFT arrays are sizeable.  Retain only the two most recently
        # used tracks so long playlists cannot steadily exhaust memory.
        self._track_levels.pop(track_id, None)
        self._track_levels[track_id] = levels
        while len(self._track_levels) > 2:
            oldest_track_id = next(iter(self._track_levels))
            self._track_levels.pop(oldest_track_id, None)
        self._clear_overlay_frames(track_id)
        self._schedule_refresh()

    def _analysis_finished(self) -> None:
        worker = self.sender()
        if worker is self._analysis_worker:
            self._analysis_worker = None
            self._analysis_track_id = ""
        if isinstance(worker, AudioAnalysisWorker):
            worker.deleteLater()
        self._sync_background_activity()
        self._schedule_refresh()

    def _toggle_playback(self, playing: bool) -> None:
        if playing and self.timeline.value() >= self.timeline.maximum():
            self.timeline.setValue(self.timeline.minimum())
        self._playing = playing
        self._refresh_source_partitions()
        for item in self._cached_video_items:
            item.set_video_preview_playback(playing and not self._scrubbing, item.source.video_speed)
        if playing and not self._scrubbing:
            self._reset_decoder_backpressure()
            self._playhead_seconds = self.timeline.value() / TIMELINE_SCALE
            self._reset_frame_statistics()
            self._start_audio_at_playhead()
            self.play_clock.start()
            self.play_timer.start()
        else:
            self.play_timer.stop()
            self.media_player.pause()
            if self._live_mix is not None:
                self._live_mix.pause()
        if getattr(self, "_progressive", False):
            self._report_playhead(force=True)
            self._try_apply_pending_swap()
        elif self._pending_swap is not None:
            self._try_apply_pending_swap()
        self._set_play_text()

    def _install_shortcuts(self) -> None:
        """Install familiar transport shortcuts for the preview window."""
        bindings = (
            ("Space", self.play_button.toggle),
            ("Left", lambda: self._seek_relative(-5.0)),
            ("Right", lambda: self._seek_relative(5.0)),
            ("Shift+Left", lambda: self._skip_track(-1)),
            ("Shift+Right", lambda: self._skip_track(1)),
            ("Up", lambda: self._adjust_volume(5)),
            ("Down", lambda: self._adjust_volume(-5)),
        )
        self._shortcuts: list[QShortcut] = []
        for sequence, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    def _seek_relative(self, seconds: float) -> None:
        value = self.timeline.value() + round(seconds * TIMELINE_SCALE)
        self.timeline.setValue(max(self.timeline.minimum(), min(self.timeline.maximum(), value)))

    def _skip_track(self, offset: int) -> None:
        selected = self._track_at(self.timeline.value() / TIMELINE_SCALE)
        if selected is None:
            return
        index = max(0, min(len(self.tracks) - 1, selected[0] + offset))
        self.timeline.setValue(round(self._track_schedule[index][2] * TIMELINE_SCALE))

    def _adjust_volume(self, change: int) -> None:
        self.volume_slider.setValue(max(0, min(100, self.volume_slider.value() + change)))

    def _set_volume(self, value: int) -> None:
        value = save_preview_volume(value)
        self._apply_output_volume()
        self.volume_value_label.setText(f"{value}%")

    def _set_gpu_preview(self, enabled: bool) -> None:
        """Switch to the persistent texture backend with automatic CPU fallback."""
        was_enabled = self.gpu_preview_enabled
        if enabled and self.gpu_surface is None and GpuTexturePreviewSurface is not None:
            surface = GpuTexturePreviewSurface()
            surface.backend_ready.connect(self._gpu_backend_ready)
            surface.backend_failed.connect(self._gpu_backend_failed)
            surface.frame_presented.connect(self._gpu_frame_presented)
            self.gpu_surface = surface
            self.preview_stack.addWidget(surface)
        self.gpu_preview_enabled = enabled and self.gpu_surface is not None
        if was_enabled != self.gpu_preview_enabled:
            self._gpu_watchdog.stop()
            self._gpu_health.reset()
            self._adaptive_quality.reset(self.preview_render_scale)
            self._last_gpu_dropped_frames = 0
            self._invalidate_preview_render_cache()
        if not self.gpu_preview_enabled:
            self._gpu_refresh_deferred = False
        self.preview_stack.setCurrentIndex(1 if self.gpu_preview_enabled else 0)
        korean = self.translator.is_korean
        self.preview_mode_label.setText(
            ("GPU 레이어 · 시작 중" if korean else "GPU layers · Starting")
            if self.gpu_preview_enabled else ("CPU 모드" if korean else "CPU mode")
        )
        if not self._image.isNull():
            self.refresh_preview()

    def _gpu_backend_ready(self, info: object) -> None:
        if not isinstance(info, GpuBackendInfo) or not self.gpu_preview_enabled:
            return
        korean = self.translator.is_korean
        self.preview_mode_label.setText(
            f"GPU 레이어 · {info.label}" if korean else
            f"GPU layers · {info.label}"
        )
        self.preview_mode_label.setToolTip(
            ("정적 캔버스와 동적 요소를 영구 OpenGL 텍스처로 유지하고 "
             "레이어 순서대로 GPU에서 합성합니다. Canvas 렌더 해상도와 영상 "
             "디코더 해상도·FPS가 각 부하에 따라 독립적으로 조절됩니다.\n" + info.label)
            if korean else
            ("Keeps static Canvas and dynamic elements in persistent OpenGL "
             "textures and composites them in layer order on the GPU. Canvas "
             "resolution and video decoder resolution/FPS adapt independently.\n" + info.label)
        )

    def _gpu_backend_failed(self, message: str) -> None:
        """Fail closed to CPU presentation without interrupting playback."""
        LOGGER.warning("GPU texture preview failed; using CPU fallback: %s", message)
        self.gpu_preview_enabled = False
        self._gpu_watchdog.stop()
        self._gpu_health.cancel_wait()
        self._adaptive_quality.reset(self.preview_render_scale)
        self._last_gpu_dropped_frames = 0
        self._invalidate_preview_render_cache()
        self._gpu_refresh_deferred = False
        self.preview_stack.setCurrentIndex(0)
        self.refresh_preview()
        korean = self.translator.is_korean
        self.preview_mode_label.setText(
            "CPU 폴백" if korean else "CPU fallback"
        )
        self.preview_mode_label.setToolTip(
            ("GPU 텍스처 백엔드를 시작하지 못해 CPU 모드로 전환했습니다.\n"
             if korean else
             "The GPU texture backend could not start, so preview returned to CPU mode.\n")
            + message
        )
        self._show_preview_error(
            "GPU 미리보기를 시작하지 못했습니다" if korean else "GPU preview could not start",
            (("CPU 모드로 자동 전환했습니다.\n" if korean else
              "Preview automatically switched to CPU mode.\n") + message),
            warning=True,
        )

    def _gpu_frame_presented(self) -> None:
        """Resume one coalesced render after the previous GL frame is visible."""
        self._gpu_watchdog.stop()
        self._gpu_health.frame_presented(monotonic())
        self._record_presented_frame()
        if self._gpu_refresh_deferred and not self._closing:
            self._gpu_refresh_deferred = False
            self._schedule_refresh()

    def _gpu_watchdog_timeout(self) -> None:
        """Retry one stalled swap, then fail closed to the current CPU scene."""
        if not self.gpu_preview_enabled or self.gpu_surface is None:
            self._gpu_health.cancel_wait()
            return
        window = self.window()
        if not self.isVisible() or (window is not None and window.isMinimized()):
            # Hidden/minimized OpenGL widgets are intentionally not swapped.
            self._gpu_health.cancel_wait()
            return
        if self._gpu_health.frame_timed_out():
            self._gpu_backend_failed(
                "GPU frame presentation timed out twice; the graphics driver "
                "or OpenGL context stopped responding.",
            )
            return
        # A single delayed swap can happen while Windows moves/resizes a window.
        # Request one repaint and arm a fresh wait before declaring the backend bad.
        self.gpu_surface.update()
        self._gpu_health.frame_queued(monotonic())
        self._gpu_watchdog.start()

    def _start_audio_at_playhead(self) -> None:
        """Seek/play the active source at the current playhead, swapping sources safely.

        ``QMediaPlayer.setSource()`` returns before the new source finishes
        loading, so a ``setPosition()``/``play()`` issued right after it can be
        silently dropped or reset to 0 once loading completes. When the target
        source differs from what is already loaded, this defers the seek until
        ``_on_media_status_changed``/``_on_media_seekable_changed`` confirm the
        new source is actually seekable, always applying the freshest playhead
        (not a value captured before the wait) and respecting whatever
        play/pause state is current when it fires.
        """
        if self._closing or self._scrubbing:
            return
        playlist_seconds = self.timeline.value() / TIMELINE_SCALE
        selected = self._track_at(playlist_seconds)
        if selected is None:
            return
        self._playhead_seconds = playlist_seconds
        if self._live_mix is not None and (
            self._blended_audio_path is None or playlist_seconds >= self._blended_audio_until
        ):
            self.media_player.stop()
            self._pending_media_seek_ms = None
            self._active_track_index = _LIVE_MIX_TRACK_INDEX
            self._live_mix.sync(self._compiled_plan, playlist_seconds, self._playing,
                                self.volume_slider.value() / 100.0 * self._per_track_gain, seek=True)
            return
        if self._live_mix is not None:
            self._live_mix.stop()
        if not ExportPreviewDialog._has_audio(self, selected, playlist_seconds):
            # `_track_at` intentionally retains the nearest track so Canvas text
            # and artwork remain meaningful in a silent gap. Audio transport must
            # nevertheless remain stopped until the real start boundary.
            self.media_player.stop()
            self._active_track_index = -1
            self._last_media_position_ms = 0
            self._pending_media_seek_ms = None
            return
        if self._blended_audio_path is not None and playlist_seconds < getattr(self, "_blended_audio_until", math.inf):
            # One continuous, pre-rendered AutoMix/crossfade mix already has
            # correct silence baked in at gaps and the same total duration as
            # the legacy sequential timeline, so the playhead position IS the
            # source position -- no per-track source switching needed.
            target_index = _BLENDED_AUDIO_TRACK_INDEX
            url = QUrl.fromLocalFile(str(self._blended_audio_path))
            target_ms = round(playlist_seconds * 1000)
        else:
            index, track, elapsed, _start = selected
            target_index = index
            # Until the mix lands, the raw file plays: its volume applies here, its EQ cannot.
            self._active_track_gain = 10 ** (track.volume_db / 20)
            url = QUrl.fromLocalFile(str(Path(track.file_path).resolve()))
            target_ms = round(elapsed * 1000)

        if target_index != self._active_track_index:
            source_changed = self.media_player.source() != url
            self._active_track_index = target_index
            self._media_source_generation += 1
            # stop()/the playlist end clears our active index but keeps Qt's
            # source. setSource(the same URL) emits no loading/ready signals;
            # waiting for them makes replay (and return from a gap) silent.
            self._media_source_ready = not source_changed and self.media_player.isSeekable()
            if source_changed:
                self.media_player.setSource(url)
            self._apply_output_volume()

        if not self._media_source_ready:
            self._pending_media_seek_ms = target_ms
            self._pending_media_generation = self._media_source_generation
            return

        self._pending_media_seek_ms = None
        self.media_player.setPosition(target_ms)
        self._last_media_position_ms = target_ms
        if self._playing:
            self.media_player.play()
        else:
            self.media_player.pause()

    def _on_media_status_changed(self, _status: QMediaPlayer.MediaStatus) -> None:
        self._resume_pending_media_seek()

    def _on_media_seekable_changed(self, _seekable: bool) -> None:
        self._resume_pending_media_seek()

    def _resume_pending_media_seek(self) -> None:
        """Apply a deferred source-swap seek once the new source is actually seekable.

        Different Qt backends reach ``LoadedMedia``/``BufferedMedia`` and
        ``isSeekable() == True`` in different orders, so both signals funnel
        here and both conditions are checked together. The generation check
        discards a late callback from a source that has since been replaced
        by another seek/track change.
        """
        if self._pending_media_seek_ms is None:
            return
        if self._pending_media_generation != self._media_source_generation:
            return
        ready_statuses = (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        )
        if self.media_player.mediaStatus() not in ready_statuses or not self.media_player.isSeekable():
            return
        self._media_source_ready = True
        self._start_audio_at_playhead()
        self._try_apply_pending_swap()  # a mix that arrived mid-swap waited for this

    def _advance_playback(self) -> None:
        if not self._playing or self._scrubbing:
            return
        elapsed_milliseconds = max(1, self.play_clock.restart())
        previous_seconds = self._playhead_seconds
        selected_before = self._track_at(previous_seconds)
        audio_was_active = ExportPreviewDialog._has_audio(self,
            selected_before, previous_seconds
        )
        predicted_seconds = self._playhead_seconds + elapsed_milliseconds / 1000.0
        player_milliseconds = self.media_player.position()
        blended_audio_active = self._active_track_index == _BLENDED_AUDIO_TRACK_INDEX
        if (self._active_track_index != _LIVE_MIX_TRACK_INDEX and audio_was_active and (selected_before is not None or blended_audio_active)
                and player_milliseconds > 0
                and player_milliseconds != self._last_media_position_ms):
            self._last_media_position_ms = player_milliseconds
            # The blended mix's own position IS the playlist position; a
            # per-track source's position must be offset by that track's
            # start-of-clip elapsed time instead.
            audio_seconds = (
                player_milliseconds / 1000.0 if blended_audio_active
                else selected_before[3] + player_milliseconds / 1000.0
            )
            # QMediaPlayer commonly reports position at roughly 10 Hz.  Use it
            # only to correct meaningful drift; the precise timer supplies the
            # intermediate 30/60 FPS playhead positions.
            if abs(audio_seconds - predicted_seconds) > 0.18:
                predicted_seconds = audio_seconds
        self._playhead_seconds = max(0.0, predicted_seconds)
        next_value = round(self._playhead_seconds * TIMELINE_SCALE)
        if next_value > self.timeline.maximum():
            self.play_button.setChecked(False)
            self.timeline.setValue(self.timeline.maximum())
            return
        old_index = self._track_at(self.timeline.value() / TIMELINE_SCALE)
        self._advancing_playhead = True
        try:
            self.timeline.setValue(next_value)
        finally:
            self._advancing_playhead = False
        new_index = self._track_at(next_value / TIMELINE_SCALE)
        if self._active_track_index == _LIVE_MIX_TRACK_INDEX and self._live_mix is not None:
            self._live_mix.sync(self._compiled_plan, self._playhead_seconds, True,
                                self.volume_slider.value() / 100.0 * self._per_track_gain)
        audio_is_active = ExportPreviewDialog._has_audio(self,
            new_index, next_value / TIMELINE_SCALE
        )
        if (
            not blended_audio_active and self._active_track_index != _LIVE_MIX_TRACK_INDEX
            and old_index and new_index
            and (old_index[0] != new_index[0] or audio_was_active != audio_is_active)
        ) or (blended_audio_active and self._playhead_seconds >= getattr(self, "_blended_audio_until", math.inf)):
            # The second case: a partial AutoMix mix ends here; per-track audio
            # carries on through the not-yet-analyzed sequential tail.
            self._start_audio_at_playhead()
        if getattr(self, "_progressive", False):
            self._report_playhead()
            self._try_apply_pending_swap()
        elif self._pending_swap is not None:
            self._try_apply_pending_swap()

    def _animation_state(
        self, track: PlaylistTrack, elapsed: float,
    ) -> tuple[str | None, float, float]:
        """Use the same bounded track-animation windows as final export."""
        refresh_partitions = getattr(self, "_refresh_source_partitions", None)
        if callable(refresh_partitions):
            refresh_partitions()
        if hasattr(self, "_cached_animation_in_duration"):
            maximum_intro = self._cached_animation_in_duration
            maximum_outro = self._cached_animation_out_duration
        else:
            sources = [
                item.source for item in self.scene.items()
                if isinstance(item, SourceItem)
            ]
            maximum_intro = max(
                (
                    source.animation_in_duration for source in sources
                    if source.animation_in != "none"
                ),
                default=0.0,
            )
            maximum_outro = max(
                (
                    source.animation_out_duration for source in sources
                    if source.animation_out != "none"
                ),
                default=0.0,
            )
        intro = min(
            track.duration_seconds / 2,
            maximum_intro,
        )
        outro = min(
            max(0.0, track.duration_seconds - intro) / 2,
            maximum_outro,
        )
        if intro > 0 and elapsed < intro:
            return "in", elapsed / intro, intro
        if outro > 0 and elapsed > max(0.0, track.duration_seconds - outro):
            return (
                "out", (elapsed - (track.duration_seconds - outro)) / outro,
                outro,
            )
        return None, 1.0, 0.0

    def _playlist_duration(self) -> float:
        plan = getattr(self, "_compiled_plan", None)
        if plan is not None:
            return plan.duration_seconds
        if self._playlist_duration_cache is not None:
            return self._playlist_duration_cache
        if self._track_schedule:
            self._playlist_duration_cache = self._track_schedule[-1][3]
            return self._playlist_duration_cache
        self._playlist_duration_cache = compile_playlist(self.tracks).duration_seconds
        return self._playlist_duration_cache

    def _update_pixmap(self) -> None:
        if self._image.isNull():
            return
        if self.gpu_preview_enabled and self.gpu_surface is not None:
            self.gpu_surface.set_image(self._image)
            return
        self.preview_label.set_image(self._image)

    def _reset_frame_statistics(self) -> None:
        """Restart the rolling frame-rate measurement after a quality change."""
        self._frame_stats_clock.restart()
        self._presented_frames = 0
        self._actual_preview_fps = 0.0
        if PROFILE_ENABLED:
            self._profile_presented_at = None
        self._update_frame_rate_label()

    def _record_presented_frame(self) -> None:
        """Show the cadence actually achieved by CPU/GPU frame presentation."""
        if not self._playing or self._scrubbing:
            return
        if PROFILE_ENABLED:
            now = monotonic()
            previous = getattr(self, "_profile_presented_at", None)
            self._profile_presented_at = now
            if previous is not None:
                interval = now - previous
                PROFILE.observe("preview.presentation_interval_seconds", interval)
                PROFILE.observe("preview.late_interval", float(interval > 1.5 / self.preview_fps))
        self._presented_frames += 1
        elapsed = self._frame_stats_clock.elapsed()
        if elapsed < 500:
            return
        self._actual_preview_fps = self._presented_frames * 1000.0 / elapsed
        self._frame_stats_clock.restart()
        self._presented_frames = 0
        dropped = 0
        if self.gpu_preview_enabled and self.gpu_surface is not None:
            stats = getattr(self.gpu_surface, "upload_stats", None)
            if stats is not None:
                dropped = max(
                    0, stats.dropped_pending_frames - self._last_gpu_dropped_frames,
                )
                self._last_gpu_dropped_frames = stats.dropped_pending_frames
        state = self._adaptive_quality.observe(
            self._actual_preview_fps, self.preview_fps,
            dropped_frames=dropped,
        )
        if state.changed:
            self._invalidate_preview_render_cache()
            self._gpu_refresh_deferred = self.gpu_preview_enabled
        self._observe_decoder_backpressure()
        self._update_frame_rate_label()

    def _reset_decoder_backpressure(self) -> None:
        """Reset decoder budgets and cumulative-stat baselines."""
        self._decoder_backpressure.reset(self.preview_fps)
        active_stats = [
            item.video_decoder_stats()
            for item in self._cached_video_items
            if item._video_timeline_preview_active
        ]
        self._last_decoder_accepted_frames = sum(
            stat.accepted_frames for stat in active_stats
        )
        self._last_decoder_pressure_drops = sum(
            stat.pressure_drops for stat in active_stats
        )
        self._last_active_decoder_count = len(active_stats)

    def _observe_decoder_backpressure(self) -> None:
        """Adapt video resolution and cadence independently from Canvas quality."""
        active_stats = [
            item.video_decoder_stats()
            for item in self._cached_video_items
            if item._video_timeline_preview_active
        ]
        active_count = len(active_stats)
        accepted_total = sum(stat.accepted_frames for stat in active_stats)
        pressure_total = sum(stat.pressure_drops for stat in active_stats)
        if (
            active_count != self._last_active_decoder_count
            or accepted_total < self._last_decoder_accepted_frames
            or pressure_total < self._last_decoder_pressure_drops
        ):
            accepted_delta = 0
            pressure_delta = 0
        else:
            accepted_delta = accepted_total - self._last_decoder_accepted_frames
            pressure_delta = pressure_total - self._last_decoder_pressure_drops
        self._last_decoder_accepted_frames = accepted_total
        self._last_decoder_pressure_drops = pressure_total
        self._last_active_decoder_count = active_count
        state = self._decoder_backpressure.observe(
            accepted_frames=accepted_delta,
            pressure_drops=pressure_delta,
            pending_decoders=sum(stat.filter_pending for stat in active_stats),
            active_decoders=active_count,
        )
        if state.changed:
            self._gpu_refresh_deferred = self.gpu_preview_enabled
            self._schedule_refresh()

    def _invalidate_preview_render_cache(self) -> None:
        """Discard only resolution-dependent preview images and layer buffers."""
        self._base_image = QImage()
        self._base_track_id = ""
        self._base_hidden_source_ids = frozenset()
        self._last_composition_key = None
        self._foreground_bands.clear()
        self._dynamic_region_buffers.clear()
        self._video_z_band_key = None
        self._video_z_band_layers.clear()
        self._clear_overlay_frames()

    def _update_frame_rate_label(self) -> None:
        korean = self.translator.is_korean
        actual = "--" if self._actual_preview_fps <= 0 else f"{self._actual_preview_fps:.0f}"
        self.frame_rate_label.setText(
            f"FPS {actual} / {self.preview_fps}"
        )
        self.frame_rate_label.setToolTip("")
        factor = round(self._adaptive_quality.factor * 100)
        self.performance_scale_label.setText(
            f"렌더 {factor}% · ×{self._active_render_scale:.3f}"
            if korean else
            f"Render {factor}% · ×{self._active_render_scale:.3f}"
        )
        self.performance_scale_label.setToolTip(
            "부드러운 재생을 위해 미리보기는 최종 해상도보다 낮게 렌더링됩니다. "
            "내보낸 영상은 선택한 출력 해상도로 선명하게 렌더링되므로 실제 "
            "결과물은 이 미리보기보다 또렷합니다."
            if korean else
            "The preview renders below the final resolution for smooth playback. "
            "The exported video is rendered at the selected output resolution, so "
            "the finished file is sharper than this preview."
        )
        stats = (
            getattr(self.gpu_surface, "upload_stats", None)
            if self.gpu_preview_enabled and self.gpu_surface is not None else None
        )
        if stats is not None:
            allocated_mb = stats.allocated_bytes / (1024 * 1024)
            budget_mb = stats.texture_budget_bytes / (1024 * 1024)
            filtered_layers = int(getattr(stats, "filtered_layers", 0))
            self.performance_gpu_label.setText(
                f"GPU {allocated_mb:.1f}/{budget_mb:.0f} MiB · 텍스처 {stats.cached_textures} · 필터 {filtered_layers}"
                if korean else
                f"GPU {allocated_mb:.1f}/{budget_mb:.0f} MiB · Textures {stats.cached_textures} · Filters {filtered_layers}"
            )
            health = self._gpu_health.stats
            self.performance_latency_label.setText(
                f"표시 {health.last_latency_ms:.1f} ms · 정지 {health.total_stalls}"
                if korean else
                f"Present {health.last_latency_ms:.1f} ms · Stalls {health.total_stalls}"
            )
        else:
            self.performance_gpu_label.setText(
                "CPU 프레임 합성" if korean else "CPU frame composition"
            )
            self.performance_latency_label.setText(
                "표시 지연 --" if korean else "Present latency --"
            )
        decoder_stats = [
            item.video_decoder_stats()
            for item in self._cached_video_items
            if item._video_timeline_preview_active
        ]
        if decoder_stats:
            dropped = sum(stat.dropped_frames for stat in decoder_stats)
            pressure = sum(stat.pressure_drops for stat in decoder_stats)
            seeks = sum(stat.seek_count for stat in decoder_stats)
            gpu_backed = sum(stat.gpu_backed_frame for stat in decoder_stats)
            decoder_scale = round(
                self._decoder_backpressure.resolution_factor * 100
            )
            decoder_fps = self._decoder_backpressure.fps_limit
            proxy_suffix = self._video_proxy_status_text(korean)
            self.performance_video_label.setText(
                f"영상 {len(decoder_stats)} · 압력 {pressure} · 드롭 {dropped} · 탐색 {seeks} · 제한 {decoder_scale}%/{decoder_fps} FPS · GPU {gpu_backed}"
                if korean else
                f"Video {len(decoder_stats)} · Pressure {pressure} · Drop {dropped} · Seek {seeks} · Limit {decoder_scale}%/{decoder_fps} FPS · GPU {gpu_backed}"
            )
            if proxy_suffix:
                self.performance_video_label.setText(
                    self.performance_video_label.text() + proxy_suffix
                )
        else:
            self.performance_video_label.setText(
                "영상 없음" if korean else "No video decoders"
            )
            proxy_suffix = self._video_proxy_status_text(korean)
            if proxy_suffix:
                self.performance_video_label.setText(
                    self.performance_video_label.text() + proxy_suffix
                )

    def _video_proxy_status_text(self, korean: bool) -> str:
        active = sum(
            1 for original, preview in self._video_proxy_paths.items()
            if os.path.normcase(original) != os.path.normcase(preview)
        )
        pending = len(self._video_proxy_queue) + int(
            self._video_proxy_worker is not None
        )
        if pending:
            return (
                f" · 프록시 준비 {pending}"
                if korean else f" · Preparing proxies {pending}"
            )
        if active:
            return (
                f" · 프록시 {active}"
                if korean else f" · Proxies {active}"
            )
        if self._video_proxy_failures:
            return " · 원본 사용" if korean else " · Original media"
        return ""

    def _set_play_text(self) -> None:
        korean = self.translator.is_korean
        self.play_button.setText(
            "Ⅱ  일시정지" if self._playing and korean else
            "Ⅱ  Pause" if self._playing else
            "▶  재생" if korean else "▶  Play"
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not self.gpu_preview_enabled:
            self._update_pixmap()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_preview()
        super().closeEvent(event)

    def _stop_preview(self) -> None:
        """Stop local media and wait briefly for cancellable frame workers to finish."""
        if self._closing:
            return
        self._closing = True
        self._cover_worker.requestInterruption()
        if self._cover_worker.isRunning():
            self._finish_or_detach_worker(self._cover_worker)
        else:
            self._cover_worker.deleteLater()
        self._refresh_queued = False
        self._refresh_timer.stop()
        self._scrubbing = False
        self._gpu_refresh_deferred = False
        self._gpu_watchdog.stop()
        self._gpu_health.cancel_wait()
        if self.gpu_surface is not None:
            surface = self.gpu_surface
            self.preview_stack.removeWidget(surface)
            surface.close()
            surface.setParent(None)
            surface.deleteLater()
            self.gpu_surface = None
            self.gpu_preview_enabled = False
        self._playing = False
        self.play_timer.stop()
        self._pending_media_seek_ms = None
        self._pending_media_generation = -1
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        if self._live_mix is not None:
            self._live_mix.stop()
        for item in self.scene.items():
            if isinstance(item, SourceItem):
                item.reset_video_preview()
        if self._analysis_worker is not None and self._analysis_worker.isRunning():
            self._analysis_worker.cancel()
            self._finish_or_detach_worker(self._analysis_worker)
        if self._overlay_worker is not None and self._overlay_worker.isRunning():
            self._overlay_worker.cancel()
            self._finish_or_detach_worker(self._overlay_worker)
        self._video_probe_queue.clear()
        self._video_probe_queued.clear()
        if self._video_probe_worker is not None and self._video_probe_worker.isRunning():
            self._video_probe_worker.requestInterruption()
            self._finish_or_detach_worker(self._video_probe_worker)
        self._video_probe_worker = None
        self._video_proxy_queue.clear()
        self._video_proxy_queued.clear()
        if self._video_proxy_worker is not None and self._video_proxy_worker.isRunning():
            self._video_proxy_worker.cancel()
            self._finish_or_detach_worker(self._video_proxy_worker)
        self._video_proxy_worker = None
        self._sync_background_activity()  # closing: both lines leave the status bar
        if self._blended_audio_controller is not None:
            self._blended_audio_controller.shutdown()
        if self._blended_audio_temp_dir is not None:
            self._blended_audio_temp_dir.cleanup()
            self._blended_audio_temp_dir = None

    def _on_blended_audio_ready(self, path_str: str, plan) -> None:
        if self._closing:
            return
        if getattr(self, "_progressive", False) or self._transition_mode != "none":
            # The final export-pipeline mix covers everything; still wait for
            # a safe moment so a transition that is already sounding keeps its audio.
            self._pending_swap = (Path(path_str), plan, math.inf, None)
            self._try_apply_pending_swap()
            return
        self._apply_blended_audio(Path(path_str), plan)

    def _on_playback_level(self, gain: float) -> None:
        """The mix's level is known: per-track audio plays at it from now on."""
        if self._closing:
            return
        self._per_track_gain = gain
        self._apply_output_volume()

    def _on_progressive_audio_ready(self, path_str: str, plan, covered_until: float, gain: float) -> None:
        if self._closing:
            return
        final_pending = self._pending_swap is not None and math.isinf(self._pending_swap[2])
        if not final_pending:  # a newer partial never replaces a waiting final mix
            self._pending_swap = (Path(path_str), plan, covered_until, gain)
        self._try_apply_pending_swap()

    def _try_apply_pending_swap(self, *, force: bool = False) -> None:
        """Apply the waiting mix once Preview is between swaps and it is safe.

        ``force`` (a user seek) skips the playhead checks: the audio restarts
        at the new position anyway, so no already-sounding music is replaced.
        A source still loading from the previous swap always blocks: that
        swap's pending seek/generation handshake must finish first.
        """
        pending = self._pending_swap
        if pending is None or self._closing or self._scrubbing:
            return
        if self._active_track_index == _BLENDED_AUDIO_TRACK_INDEX and not self._media_source_ready:
            return  # _resume_pending_media_seek() retries once the previous swap is seekable
        path, plan, covered_until, gain = pending
        playhead = self._playhead_seconds
        if not force:
            from app.automix.progressive import swap_playhead

            playhead = swap_playhead(self._compiled_plan, plan, playhead, self._playing)
            if playhead is None:
                return
        self._pending_swap = None
        self.progressive_swap_count += 1
        if gain is not None:
            self._per_track_gain = gain
        self._apply_blended_audio(path, plan, covered_until, playhead=playhead)

    def _report_playhead(self, *, force: bool = False) -> None:
        """Tell a progressive controller where the listener is (throttled to ~2 Hz)."""
        report = getattr(self._blended_audio_controller, "report_playhead", None)
        if report is None:
            return
        if force or abs(self._playhead_seconds - self._last_playhead_report) >= 0.5:
            self._last_playhead_report = self._playhead_seconds
            report(self._playhead_seconds, self._playing)

    def _apply_output_volume(self) -> None:
        """Slider volume, scaled to the partial mix's level while per-track audio plays."""
        slider = getattr(self, "volume_slider", None)
        if slider is None or not hasattr(self, "audio_output"):
            return
        gain = 1.0 if self._active_track_index == _BLENDED_AUDIO_TRACK_INDEX else (
            getattr(self, "_per_track_gain", 1.0) * getattr(self, "_active_track_gain", 1.0))
        self.audio_output.setVolume(min(1.0, slider.value() / 100.0 * gain))
        if self._active_track_index == _LIVE_MIX_TRACK_INDEX and self._live_mix is not None:
            self._live_mix.sync(self._compiled_plan, self._playhead_seconds,
                                self._playing and not self._scrubbing,
                                slider.value() / 100.0 * self._per_track_gain)

    def _apply_blended_audio(self, path: Path, plan, covered_until: float = math.inf,
                             *, playhead: float | None = None) -> None:
        """``playhead``: where the same music sits in ``plan`` (progressive.swap_playhead); None keeps it."""
        self._blended_audio_until = covered_until
        if playhead is not None:
            self._playhead_seconds = playhead
        if self._active_track_index == _BLENDED_AUDIO_TRACK_INDEX and path != self._blended_audio_path:
            # Mix -> newer mix: force a real source change so the new file goes
            # through the same setSource / pending-seek / generation handshake.
            self._active_track_index = -1
        self._compiled_plan = plan
        self._track_schedule = self._build_track_schedule()
        self._track_schedule_starts = tuple(row[2] for row in self._track_schedule)
        self._playlist_duration_cache = plan.duration_seconds
        self._blended_audio_path = path
        # Beyond the remap above (same music, new timeline), only ever shorten
        # the playhead to fit a shorter plan -- a plan replacement must not
        # reset or jump the music the user is already hearing.
        self._playhead_seconds = min(self._playhead_seconds, plan.duration_seconds)
        self.timeline.blockSignals(True)
        self.timeline.setRange(0, max(1, ceil(plan.duration_seconds * TIMELINE_SCALE)))
        self.timeline.setValue(round(self._playhead_seconds * TIMELINE_SCALE))
        self.timeline.blockSignals(False)
        # The slider widget caches its own copy of the schedule/transition
        # overlay for paintEvent; without this it kept drawing the initial
        # sequential boundaries even after the final AutoMix plan arrived.
        self.timeline.set_schedule(self._track_schedule, plan.audio.transitions)
        self._populate_track_list()
        self._refresh_automix_details(covered_until)
        self._base_track_id = ""
        self._force_video_seek = True
        self._schedule_refresh()
        # Swap the already-playing per-track source for the blended mix at
        # the current playhead so a render that finishes mid-preview takes
        # over without an audible restart-from-zero.
        if self._playing:
            self._start_audio_at_playhead()
        self._apply_output_volume()
        self._report_playhead(force=True)

    def _on_blended_audio_failed(self, message: str) -> None:
        LOGGER.warning("Preview blended-audio render failed, falling back to per-track playback: %s", message)
        panel = getattr(self, "automix_details", None)
        if panel is not None and not self._closing:
            panel.set_failed()

    def _on_blended_audio_progress(self, _stage: str, _fraction: float, message: str) -> None:
        panel = getattr(self, "automix_details", None)
        if panel is not None and not self._closing:
            panel.set_progress_message(message)

    def _apply_preview_style(self) -> None:
        """Use the application's shared playback styling."""
        self.setStyleSheet("")

    def refresh_theme(self) -> None:
        """Rebuild dialog-local cards when the application theme changes."""
        controls_page = getattr(self, "_embedded_controls_page", None)
        if self.embedded and controls_page is not None:
            self._apply_embedded_preview_style(controls_page)
        else:
            self._apply_preview_style()

    @staticmethod
    def _finish_or_detach_worker(worker: QThread) -> None:
        """Detach a still-running worker instead of blocking the UI thread on it.

        The caller already requested cancellation, but that is not always
        enough to stop ``run()`` promptly: ``VideoDurationProbeWorker`` calls
        FFprobe through a blocking ``subprocess.run(..., timeout=15)`` with no
        way to interrupt it early, so a synchronous ``wait()`` here could
        freeze the whole window for up to 15 seconds every time Preview closed
        while a probe was in flight. Reparenting to the application keeps Qt
        from deleting a QThread while it is still running; the worker's own
        ``finished`` signal cleans it up once it actually exits, decoupled
        from the dialog's lifetime.
        """
        application = QApplication.instance()
        if application is not None:
            worker.setParent(application)
        worker.finished.connect(worker.deleteLater)

    def retranslate(self) -> None:
        korean = self.translator.is_korean
        self.setWindowTitle("전체 재생 미리보기" if korean else "Playlist playback preview")
        self.dialog_title_label.setText(
            ("캔버스 미리보기" if korean else "Canvas Preview")
            if self.embedded else
            ("전체 재생 미리보기" if korean else "Playlist Preview")
        )
        self.hint_label.setText(
            "실제 음원과 내보내기 구성을 전체 타임라인에서 확인합니다."
            if korean else
            "Review the complete timeline with actual audio and export-equivalent composition."
        )
        self.performance_title_label.setText("성능" if korean else "Performance")
        self.tracks_toggle.setText("곡 목록" if korean else "Track list")
        self.tracks_toggle.setToolTip("곡 목록 펼치기 / 접기" if korean else "Show or hide the track list")
        self.performance_toggle.setText("성능 정보" if korean else "Performance")
        self.performance_toggle.setToolTip("미리보기 렌더링 상태 보기" if korean else "Show preview rendering status")
        self.track_list_title_label.setText("트랙" if korean else "Tracks")
        self._populate_track_list()
        if getattr(self, "automix_details", None) is not None:
            self.automix_details.retranslate()
        self.timeline_title_label.setText(
            ("재생 타임라인" if korean else "Playback timeline")
            if self.embedded else
            ("전체 플레이리스트" if korean else "Full playlist")
        )
        self.shortcut_hint_label.setText(
            "Space 재생/일시정지  ·  ←/→ 5초 이동  ·  Shift+←/→ 곡 이동  ·  ↑/↓ 볼륨"
            if korean else
            "Space Play/Pause  ·  ←/→ Seek 5s  ·  Shift+←/→ Change track  ·  ↑/↓ Volume"
        )
        self.transport_card.setToolTip(self.shortcut_hint_label.text())
        self.timeline.setAccessibleName("재생 위치" if korean else "Playback position")
        self.timeline.setToolTip(
            "드래그하며 화면을 확인하세요. 오디오는 잠시 멈추고, 재생 중이었다면 놓은 위치에서 이어집니다. ←/→: 5초 이동."
            if korean else
            "Drag to preview a position. Audio pauses during the drag and resumes on release if it was playing. Left/Right: seek 5 seconds."
        )
        self.timeline.setAccessibleDescription(self.timeline.toolTip())
        self.volume_slider.setAccessibleName("음량" if korean else "Volume")
        self.previous_button.setAccessibleName("이전 곡" if korean else "Previous track")
        self.next_button.setAccessibleName("다음 곡" if korean else "Next track")
        if self.preview_close_button is not None:
            self.preview_close_button.setText(
                ("편집으로 돌아가기" if korean else "Back to editing")
                if self.embedded else
                ("닫기" if korean else "Close")
            )
        self.volume_label.setText("볼륨" if korean else "Volume")
        self.error_details_button.setText("자세히" if korean else "Details")
        self.error_details_button.setToolTip(
            "전체 오류 내용 보기" if korean else "Show full error details"
        )
        self.error_dismiss_button.setToolTip(
            "오류 알림 닫기" if korean else "Dismiss error notification"
        )
        self.preview_mode_label.setText(
            ("GPU 레이어" if korean else "GPU layers")
            if self.gpu_preview_enabled else ("CPU 모드" if korean else "CPU mode")
        )
        self.previous_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipBackward))
        self.rewind_button.setText("−5s")
        self.forward_button.setText("+5s")
        self.next_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSkipForward))
        self.previous_button.setToolTip("이전 곡 (Shift+←)" if korean else "Previous track (Shift+←)")
        self.next_button.setToolTip("다음 곡 (Shift+→)" if korean else "Next track (Shift+→)")
        self.rewind_button.setToolTip("5초 뒤로 (←)" if korean else "Back 5 seconds (←)")
        self.forward_button.setToolTip("5초 앞으로 (→)" if korean else "Forward 5 seconds (→)")
        self._update_frame_rate_label()
        self._set_play_text()
