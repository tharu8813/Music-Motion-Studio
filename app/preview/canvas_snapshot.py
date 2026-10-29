"""Rasterize the current Qt canvas artboard for the FFmpeg renderer."""

from __future__ import annotations

from app.utils.performance import timed

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from functools import wraps
from math import ceil, floor
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform

from app.canvas.live_canvas import CanvasScene
from app.animation.lyrics import lyric_progress
from app.canvas.source_item import SourceItem
from app.canvas.renderers.lyrics_renderer import lyric_wrapped_rows
from app.animation.curves import (
    AnimationPose, animation_pose, ease_in_out_cubic, loop_pose, motion_padding, music_reactive_pose,
)
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.album_art import (
    AMBIENT_FLOW_HZ,
    adjust_personal_color,
    create_cached_ambient_background,
    extract_track_cover,
    extract_track_personal_color,
)
from app.preview.frame_state import (
    MixJunction, resolve_edge_animation, resolve_lyrics_cue_state, resolve_lyrics_intro_state,
    resolve_lyrics_word_reveal,
    lyrics_display_groups,
    resolve_now_playing_exit_state, resolve_timeline_window_phase,
)
from app.preview.text_template import (
    TEXT_TEMPLATE_TOKEN_NAMES,
    expand_track_template,
)
from app.services.lyrics_service import LyricsService
from app.services.lyrics_intro_service import instrumental_spans

# Text template tokens whose resolved value changes during an export: every
# token the renderer substitutes except ``track_total``, which is constant for
# the whole playlist.  Unknown ``%tokens%`` are preserved verbatim, so they are
# constant too and do not appear here.
_VOLATILE_TEXT_TOKENS: frozenset[str] = frozenset(TEXT_TEMPLATE_TOKEN_NAMES) - {
    "track_total",
}

# Editor preview never asks for more than the artboard resolution, but export
# renders the vector scene at the target output resolution so text and shapes
# stay crisp instead of being bicubic-upscaled by FFmpeg afterwards. Custom
# projects may be as small as 64 px while export can reach 4K, which needs a
# 60x raster scale; the cap leaves a small margin for the supported outputs.
_MAX_CAPTURE_SCALE = 64.0


# Track-driven album covers and ambient backgrounds are re-injected on every
# captured frame. Filtering them there would re-run the colour/blur pass in the
# playback loop, so the filtered result is memoised per (artwork, filter) key.
_FILTERED_TRACK_PIXMAP_CACHE: dict[tuple, QPixmap] = {}


def _lyric_cue_line_count(cue: dict[str, object], graphics_item: SourceItem | None = None) -> int:
    """Count the physical display rows occupied by one lyric cue."""
    text = LyricsService.decode_line_breaks(cue.get("text", ""))
    lines = [line for line in text.splitlines() if line.strip()]
    if graphics_item is not None and graphics_item.source.subtitle_auto_wrap:
        positions = cue.get("display_line_positions", tuple(range(len(lines))))
        return max(1, sum(len(lyric_wrapped_rows(graphics_item, line, positions[index]))
                          for index, line in enumerate(lines)))
    return max(1, len(lines))


def _effective_lyric_context(
    graphics_item: SourceItem,
    cues: Sequence[dict[str, object]],
    cue_index: int,
) -> tuple[int, int]:
    """Resolve automatic context; horizontal flow keeps one cue on each side."""
    source = graphics_item.source
    previous_setting = int(source.subtitle_context_lines)
    next_setting = int(source.subtitle_next_lines)
    if source.subtitle_flow_direction in {"left", "right"}:
        return max(0, previous_setting) if previous_setting >= 0 else 1, max(0, next_setting) if next_setting >= 0 else 1
    if previous_setting >= 0 and next_setting >= 0:
        return previous_setting, next_setting

    previous = min(cue_index, max(0, previous_setting))
    following_available = max(0, len(cues) - cue_index - 1)
    following = min(following_available, max(0, next_setting))
    used_rows = _lyric_cue_line_count(cues[cue_index], graphics_item)
    used_rows += int(graphics_item._subtitle_intro_state is not None)
    used_rows += sum(
        _lyric_cue_line_count(cues[index], graphics_item)
        for index in range(cue_index - previous, cue_index)
    )
    used_rows += sum(
        _lyric_cue_line_count(cues[index], graphics_item)
        for index in range(cue_index + 1, cue_index + following + 1)
    )
    remaining = max(0, graphics_item.lyric_line_capacity() - used_rows)
    automatic_previous = previous_setting < 0
    automatic_next = next_setting < 0
    blocked: set[str] = set()
    while remaining > 0:
        progressed = False
        for side in ("previous", "next"):
            if side in blocked:
                continue
            if side == "previous":
                if not automatic_previous or previous >= cue_index or previous >= 15:
                    blocked.add(side)
                    continue
                candidate = cue_index - previous - 1
            else:
                if not automatic_next or following >= following_available or following >= 15:
                    blocked.add(side)
                    continue
                candidate = cue_index + following + 1
            row_cost = _lyric_cue_line_count(cues[candidate], graphics_item)
            if row_cost > remaining:
                blocked.add(side)
                continue
            if side == "previous":
                previous += 1
            else:
                following += 1
            remaining -= row_cost
            progressed = True
        if not progressed:
            break
    return previous, following


def _filtered_track_pixmap(
    graphics_item: SourceItem,
    base: QPixmap | Callable[[], QPixmap],
    key: tuple,
    *,
    include_blur: bool = True,
    prefiltered: bool = False,
) -> QPixmap:
    """Apply the source's brightness/contrast/blur to a track pixmap, memoised.

    ``base`` may be a callable so an expensive source pixmap (a re-scaled
    ambient background, a decoded cover) is only built on a cache miss;
    ``prefiltered``: it already carries the filters, only memoise it.
    """
    cached = _FILTERED_TRACK_PIXMAP_CACHE.get(key)
    if cached is not None:
        return cached
    source_pixmap = base() if callable(base) else base
    result = source_pixmap if prefiltered else graphics_item._apply_image_filters(
        source_pixmap, include_blur=include_blur,
    )
    if len(_FILTERED_TRACK_PIXMAP_CACHE) > 96:
        _FILTERED_TRACK_PIXMAP_CACHE.clear()
    _FILTERED_TRACK_PIXMAP_CACHE[key] = result
    return result


def _crossfade_pixmaps(
    previous: QPixmap, current: QPixmap, progress: float,
) -> QPixmap:
    """Return ``previous`` dissolving into ``current`` at ``progress`` in [0, 1]."""
    if previous.isNull() or current.isNull() or progress >= 1.0:
        return current
    if previous.size() != current.size():
        previous = previous.scaled(
            current.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    merged = QPixmap(current.size())
    merged.fill(Qt.GlobalColor.transparent)
    painter = QPainter(merged)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawPixmap(0, 0, previous)
    painter.setOpacity(max(0.0, min(1.0, progress)))
    painter.drawPixmap(0, 0, current)
    painter.end()
    return merged


def _with_lyric_settings(capture):
    @wraps(capture)
    def resolved(scene, *args, **kwargs):
        # Cover state evaluation as well as painting: even a failed evaluation
        # must return each item to its original editable basic model.
        with ExitStack() as settings:
            items = kwargs.get("band_source_items")
            for item in scene.items() if items is None else items:
                if isinstance(item, SourceItem) and item.source.subtitle_advanced_categories:
                    settings.enter_context(item.lyric_settings())
            return capture(scene, *args, **kwargs)
    return resolved


class CanvasSnapshot:
    """Captures only the export artboard, without editor handles or workspace chrome."""

    @staticmethod
    @timed("canvas.scene_render_seconds")
    def capture(scene: CanvasScene, output_scale: float = 1.0,
                z_min: float | None = None, z_max: float | None = None,
                transparent: bool = False, image_buffer: QImage | None = None,
                capture_rect: QRectF | None = None) -> QImage:
        """Render all or one Z-index band of the artboard at project resolution.

        Transparent Z bands are the basis for interleaving static Canvas content
        with audio-reactive video layers during export.
        """
        artboard = scene.artboard_rect
        requested_rect = capture_rect if capture_rect is not None else artboard
        source_rect = requested_rect.intersected(artboard)
        if source_rect.isEmpty():
            source_rect = QRectF(artboard.left(), artboard.top(), 1.0, 1.0)
        scale = max(0.25, min(_MAX_CAPTURE_SCALE, output_scale))
        width = max(1, round(source_rect.width() * scale))
        height = max(1, round(source_rect.height() * scale))
        image_format = (
            QImage.Format.Format_ARGB32_Premultiplied
            if transparent else QImage.Format.Format_RGB32
        )
        if (image_buffer is not None and image_buffer.size().width() == width
                and image_buffer.size().height() == height
                and image_buffer.format() == image_format):
            image = image_buffer
        else:
            image = QImage(width, height, image_format)
        image.fill(Qt.GlobalColor.transparent if transparent else Qt.GlobalColor.black)
        selected_items = scene.selectedItems()
        grid_visible = scene.show_grid
        guide_x, guide_y = scene.guide_x, scene.guide_y
        background_suppressed = scene.suppress_render_background
        hidden_items: list[SourceItem] = []
        scene.blockSignals(True)
        try:
            scene.clearSelection()
            scene.show_grid = False
            scene.guide_x = None
            scene.guide_y = None
            scene.suppress_render_background = transparent
            if z_min is not None or z_max is not None:
                for item in scene.items():
                    if not isinstance(item, SourceItem) or not item.isVisible():
                        continue
                    z_value = item.source.z_index
                    if ((z_min is not None and z_value < z_min)
                            or (z_max is not None and z_value > z_max)):
                        hidden_items.append(item)
                        item.setVisible(False)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            scene.render(
                painter,
                QRectF(0, 0, image.width(), image.height()),
                source_rect,
                Qt.AspectRatioMode.IgnoreAspectRatio,
            )
            painter.end()
            for item in selected_items:
                item.setSelected(True)
        finally:
            for item in hidden_items:
                item.setVisible(True)
            scene.show_grid = grid_visible
            scene.guide_x = guide_x
            scene.guide_y = guide_y
            scene.suppress_render_background = background_suppressed
            scene.blockSignals(False)
        return image

    @staticmethod
    def _dynamic_band_capture_rect(
        scene: CanvasScene,
        z_min: float | None,
        z_max: float | None,
    ) -> QRectF | None:
        """Return pixel-aligned bounds for the currently visible band items."""
        artboard = scene.artboard_rect
        bounds = QRectF()
        found = False
        for item in scene.items():
            if not isinstance(item, SourceItem) or not item.isVisible():
                continue
            source = item.source
            if (
                not source.visible
                or (z_min is not None and source.z_index < z_min)
                or (z_max is not None and source.z_index > z_max)
            ):
                continue
            item_bounds = item.mapRectToScene(item.content_rect())
            # SourceItem paints a few effects beyond its nominal content.  The
            # item transform already includes current animation movement,
            # scale, and rotation; this scene-space padding covers antialiasing,
            # outlines, shadows, and lyric reveal blur without clipping them.
            shadow_padding = 0.0
            if source.shadow.enabled:
                shadow_padding = (
                    abs(source.shadow.offset_x)
                    + abs(source.shadow.offset_y)
                    + source.shadow.blur_radius * 0.25
                )
            visual_padding = max(
                3.0,
                source.outline_width + 2.0,
                shadow_padding + 2.0,
                source.subtitle_previous_blur * 2.0 + 2.0
                if source.source_type is SourceType.LYRICS else 0.0,
            ) * max(1.0, abs(item.scale()))
            item_bounds = item_bounds.adjusted(
                -visual_padding,
                -visual_padding,
                visual_padding,
                visual_padding,
            )
            bounds = item_bounds if not found else bounds.united(item_bounds)
            found = True
        if not found:
            return QRectF(artboard.left(), artboard.top(), 1.0, 1.0)
        bounds = bounds.intersected(artboard)
        if bounds.isEmpty():
            return QRectF(artboard.left(), artboard.top(), 1.0, 1.0)
        left = max(artboard.left(), floor(bounds.left()))
        top = max(artboard.top(), floor(bounds.top()))
        right = min(artboard.right(), ceil(bounds.right()))
        bottom = min(artboard.bottom(), ceil(bounds.bottom()))
        return QRectF(
            left,
            top,
            max(1.0, right - left),
            max(1.0, bottom - top),
        )

    @staticmethod
    def band_capture_envelope(
        scene: CanvasScene,
        z_min: float | None,
        z_max: float | None,
        *,
        maximum_area_ratio: float = 0.9,
    ) -> QRectF | None:
        """Return one safe fixed crop for a complete transparent Z stream."""
        artboard = scene.artboard_rect
        bounds = QRectF()
        found = False
        slide_styles = {
            "slide_left", "slide_right", "slide_up", "slide_down",
        }
        for item in scene.items():
            if not isinstance(item, SourceItem) or not item.isVisible():
                continue
            source = item.source
            if (
                not source.visible
                or (z_min is not None and source.z_index < z_min)
                or (z_max is not None and source.z_index > z_max)
            ):
                continue
            item_bounds = item.mapRectToScene(item.content_rect())
            scale = max(1.0, abs(item.scale()))
            shadow_padding = 0.0
            if source.shadow.enabled:
                shadow_padding = (
                    abs(source.shadow.offset_x)
                    + abs(source.shadow.offset_y)
                    + source.shadow.blur_radius * 0.25
                )
            padding = max(
                3.0,
                source.outline_width + 2.0,
                shadow_padding + 2.0,
                source.subtitle_previous_blur * 2.0 + 2.0
                if source.source_type is SourceType.LYRICS else 0.0,
            ) * scale
            padding += motion_padding(
                {source.animation_in, source.animation_out},
                source.loop_motion, source.loop_motion_amount,
                source.width, source.height,
            ) * scale
            if source.source_type is SourceType.TEXT and source.uses_bass_reaction:
                padding += max(source.width, source.height, source.font_size) * source.music_reactive_strength * scale
            if source.source_type is SourceType.NOW_PLAYING:
                if source.now_playing_exit_animation in slide_styles:
                    padding += 24.0 * scale
                elif source.now_playing_exit_animation == "zoom":
                    padding += max(source.width, source.height) * 0.04 * scale
            item_bounds = item_bounds.adjusted(
                -padding, -padding, padding, padding,
            )
            bounds = item_bounds if not found else bounds.united(item_bounds)
            found = True
        if not found:
            return None
        bounds = bounds.intersected(artboard)
        if bounds.isEmpty():
            return None
        left = max(artboard.left(), floor(bounds.left()))
        top = max(artboard.top(), floor(bounds.top()))
        right = min(artboard.right(), ceil(bounds.right()))
        bottom = min(artboard.bottom(), ceil(bounds.bottom()))
        envelope = QRectF(
            left,
            top,
            max(1.0, right - left),
            max(1.0, bottom - top),
        )
        artboard_area = max(1.0, artboard.width() * artboard.height())
        if envelope.width() * envelope.height() >= (
            artboard_area * max(0.1, min(1.0, maximum_area_ratio))
        ):
            return None
        return envelope

    @staticmethod
    def _expand_partial_capture(
        partial_image: QImage,
        capture_rect: QRectF,
        artboard: QRectF,
        output_scale: float,
    ) -> QImage:
        """Place a cropped transparent render into a full-size export frame."""
        scale = max(0.25, min(_MAX_CAPTURE_SCALE, output_scale))
        image = QImage(
            max(1, round(artboard.width() * scale)),
            max(1, round(artboard.height() * scale)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        target_x = round((capture_rect.left() - artboard.left()) * scale)
        target_y = round((capture_rect.top() - artboard.top()) * scale)
        painter = QPainter(image)
        painter.drawImage(target_x, target_y, partial_image)
        painter.end()
        return image

    @staticmethod
    def z_bands(scene: CanvasScene, dynamic_source_ids: set[str]) -> list[tuple[float | None, float | None]]:
        """Return static Z-index intervals separated by reactive source layers."""
        dynamic_z = sorted({item.source.z_index for item in scene.items()
                            if isinstance(item, SourceItem) and item.isVisible()
                            and item.source.visible and item.source.id in dynamic_source_ids})
        if not dynamic_z:
            return [(None, None)]
        bands: list[tuple[float | None, float | None]] = []
        lower: float | None = None
        for z_value in dynamic_z:
            bands.append((lower, z_value - 1e-6))
            # Put a static source sharing the same legacy Z value into the
            # foreground band. The former +epsilon lower bound excluded it
            # from every band, so it vanished only in the exported video.
            lower = z_value
        bands.append((lower, None))
        static_z = [
            item.source.z_index for item in scene.items()
            if isinstance(item, SourceItem) and item.isVisible() and item.source.visible
            and item.source.id not in dynamic_source_ids
        ]

        def contains_static(band: tuple[float | None, float | None]) -> bool:
            minimum, maximum = band
            return any(
                (minimum is None or z_value >= minimum)
                and (maximum is None or z_value <= maximum)
                for z_value in static_z
            )

        # The first band is always the opaque base video.  Later transparent
        # bands are useful only when they contain an actual static source.
        return [bands[0], *(band for band in bands[1:] if contains_static(band))]

    @staticmethod
    def source_is_capture_invariant(
        source: Source, playlist_duration_seconds: float,
    ) -> bool:
        """Return whether a source is identical for every export sample.

        This deliberately accepts only a small allow-list.  A false negative
        merely keeps the established sequential capture path, while a false
        positive could freeze a time-dependent element in the final video.
        """
        if (source.animation_in != "none" or source.animation_out != "none"
                or source.loop_motion != "none" or source.uses_bass_reaction):
            return False
        if source.uses_personal_color:
            return False
        if source.timeline_start > 0.0:
            return False
        if (
            source.timeline_duration > 0.0
            and source.timeline_start + source.timeline_duration
            < playlist_duration_seconds
        ):
            return False
        if source.source_type is SourceType.TEXT:
            # Identical for the whole playlist only when every substituted token
            # is playlist-constant.  ``track_total`` and unknown tokens qualify;
            # title/artist/album/track/filename follow the current track and the
            # time tokens follow the clock.
            return not any(
                f"%{token}%" in source.text for token in _VOLATILE_TEXT_TOKENS
            )
        if source.source_type is SourceType.ALBUM_COVER:
            return bool(source.content_path)
        if source.source_type is SourceType.BACKGROUND:
            return source.background_mode != "album_art"
        return source.source_type in {
            SourceType.IMAGE,
            SourceType.SHAPE,
            SourceType.LOGO,
            SourceType.WATERMARK,
        }

    @staticmethod
    def split_mixed_capture_bands(
        scene: CanvasScene,
        dynamic_source_ids: set[str],
        z_bands: list[tuple[float | None, float | None]],
        playlist_duration_seconds: float,
        *,
        max_streams: int = 12,
    ) -> list[tuple[float | None, float | None]]:
        """Split broad Z bands into consecutive invariant/dynamic sublayers.

        Sources sharing one Z value remain together so their QGraphicsScene
        stacking order cannot change.  A global cap prevents an alternating
        project from creating an unbounded number of lossless video streams.
        """
        visible_sources = [
            item.source for item in scene.items()
            if isinstance(item, SourceItem)
            and item.isVisible()
            and item.source.visible
            and item.source.id not in dynamic_source_ids
        ]
        split_by_original: list[list[dict[str, object]]] = []
        for original_min, original_max in z_bands:
            band_sources = [
                source for source in visible_sources
                if (original_min is None or source.z_index >= original_min)
                and (original_max is None or source.z_index <= original_max)
            ]
            grouped: dict[float, list[Source]] = {}
            for source in band_sources:
                grouped.setdefault(float(source.z_index), []).append(source)
            if not grouped:
                split_by_original.append([{
                    "minimum": original_min,
                    "maximum": original_max,
                    "invariant": True,
                    "source_count": 0,
                }])
                continue
            z_values = sorted(grouped)
            group_states = [
                all(
                    CanvasSnapshot.source_is_capture_invariant(
                        source, playlist_duration_seconds,
                    )
                    for source in grouped[z_value]
                )
                for z_value in z_values
            ]
            runs: list[dict[str, object]] = []
            run_start = 0
            for group_index in range(1, len(z_values) + 1):
                at_end = group_index == len(z_values)
                if not at_end and group_states[group_index] == group_states[run_start]:
                    continue
                runs.append({
                    "minimum": (
                        original_min if run_start == 0 else z_values[run_start]
                    ),
                    "maximum": (
                        original_max if at_end else z_values[group_index - 1]
                    ),
                    "invariant": group_states[run_start],
                    "source_count": sum(
                        len(grouped[z_value])
                        for z_value in z_values[run_start:group_index]
                    ),
                })
                run_start = group_index
            split_by_original.append(runs)

        stream_limit = max(len(z_bands), max(1, int(max_streams)))

        def coalesce_equal_states(runs: list[dict[str, object]]) -> None:
            index = 0
            while index + 1 < len(runs):
                left, right = runs[index], runs[index + 1]
                if bool(left["invariant"]) != bool(right["invariant"]):
                    index += 1
                    continue
                left["maximum"] = right["maximum"]
                left["source_count"] = (
                    int(left["source_count"]) + int(right["source_count"])
                )
                runs.pop(index + 1)

        while sum(len(runs) for runs in split_by_original) > stream_limit:
            candidates: list[tuple[int, int, int, int, int]] = []
            for band_index, runs in enumerate(split_by_original):
                for run_index, (left, right) in enumerate(zip(runs, runs[1:])):
                    lost_invariant_sources = (
                        int(left["source_count"]) if bool(left["invariant"]) else 0
                    ) + (
                        int(right["source_count"]) if bool(right["invariant"]) else 0
                    )
                    candidates.append((
                        lost_invariant_sources,
                        int(left["source_count"]) + int(right["source_count"]),
                        band_index,
                        run_index,
                        len(runs),
                    ))
            if not candidates:
                break
            _lost, _size, band_index, run_index, _run_count = min(candidates)
            runs = split_by_original[band_index]
            left, right = runs[run_index], runs[run_index + 1]
            runs[run_index:run_index + 2] = [{
                "minimum": left["minimum"],
                "maximum": right["maximum"],
                "invariant": bool(left["invariant"]) and bool(right["invariant"]),
                "source_count": (
                    int(left["source_count"]) + int(right["source_count"])
                ),
            }]
            coalesce_equal_states(runs)

        return [
            (run["minimum"], run["maximum"])
            for runs in split_by_original
            for run in runs
        ]

    @staticmethod
    def invariant_stream_keys(
        scene: CanvasScene,
        dynamic_source_ids: set[str],
        z_bands: list[tuple[float | None, float | None]],
        playlist_duration_seconds: float,
    ) -> set[str]:
        """Find complete Z bands that are safe to capture only once.

        The scene itself always remains on the GUI thread.  Only bands whose
        every visible Canvas source is immutable are admitted to this cache;
        dynamic FFmpeg overlays are excluded because they are hidden during
        Canvas capture and composed later.
        """
        visible_sources = [
            item.source for item in scene.items()
            if isinstance(item, SourceItem)
            and item.isVisible()
            and item.source.visible
            and item.source.id not in dynamic_source_ids
        ]
        invariant: set[str] = set()
        for index, (z_min, z_max) in enumerate(z_bands):
            sources = [
                source for source in visible_sources
                if (z_min is None or source.z_index >= z_min)
                and (z_max is None or source.z_index <= z_max)
            ]
            if all(
                CanvasSnapshot.source_is_capture_invariant(
                    source, playlist_duration_seconds,
                )
                for source in sources
            ):
                invariant.add("base" if index == 0 else f"layer:{index - 1}")
        return invariant

    @staticmethod
    @timed("canvas.capture_seconds")
    @_with_lyric_settings
    def capture_track(scene: CanvasScene, track: PlaylistTrack, track_number: int,
                      track_total: int, start_seconds: float, animation_phase: str | None = None,
                      animation_progress: float = 1.0, elapsed_seconds: float = 0.0,
                      hide_visualizers: set[str] | None = None,
                      playlist_duration_seconds: float | None = None,
                      playlist_tracks: list[PlaylistTrack] | None = None,
                      output_scale: float = 1.0, z_min: float | None = None,
                      z_max: float | None = None, transparent: bool = False,
                      hide_source_ids: set[str] | None = None,
                      image_buffer: QImage | None = None,
                      capture_rect: QRectF | None = None,
                      timeline_seconds: float | None = None,
                      animation_phase_duration: float | None = None,
                      partial_render: bool = False,
                      render_metrics: dict[str, object] | None = None,
                      band_source_items: Sequence[SourceItem] | None = None,
                      junction: MixJunction | None = None,
                      bass_level: float = 0.0,
                      music_levels: dict[str, float] | None = None,
                      lyric_instrumental_spans: tuple | None = None,
                      lyric_instrumental_spans_by_source: dict[str, tuple] | None = None) -> QImage:
        """Capture one track state with metadata, cover art, and an optional Z band.

        ``junction``: the frame sits at a crossfade/AutoMix handover of the
        Canvas (see render_plan.visual_segments): sources play their mix
        entrance/exit, and on-screen clocks that start with a track (Now
        Playing, the album-art background cross-fade) start at the handover.
        """
        screen_elapsed = (
            junction.visual_elapsed
            if junction is not None and junction.visual_elapsed is not None
            else elapsed_seconds
        )
        original_text: list[tuple[SourceItem, str]] = []
        original_music_levels: list[tuple[SourceItem, float, int | None]] = []
        original_transforms: list[
            tuple[SourceItem, object, float, float, float, QTransform]
        ] = []
        original_progress: list[tuple[SourceItem, float]] = []
        original_covers: list[tuple[SourceItem, QPixmap]] = []
        original_backgrounds: list[tuple[SourceItem, QPixmap]] = []
        original_visibility: list[tuple[SourceItem, bool]] = []
        original_subtitle_lines: list[tuple[SourceItem, int, int]] = []
        original_subtitle_offsets: list[tuple[SourceItem, float]] = []
        original_subtitle_transitions: list[tuple[SourceItem, float]] = []
        original_subtitle_style_indices: list[tuple[SourceItem, tuple[int, ...]]] = []
        original_subtitle_motion = []
        original_subtitle_anchors: list[
            tuple[SourceItem, int, int, int, int]
        ] = []
        original_track_list_rows: list[tuple[SourceItem, int]] = []
        original_personal_colors: list[
            tuple[SourceItem, dict[str, str], tuple[str, str] | None]
        ] = []
        original_shadow_colors: list[tuple[SourceItem, str]] = []
        # Removed sources are deliberately retained as hidden Qt items for safe Undo.
        # They must never participate in preview/export captures after a preset swap.
        source_items = [
            item for item in (
                band_source_items
                if band_source_items is not None else scene.items()
            )
            if isinstance(item, SourceItem) and item.isVisible() and item.source.visible
            and (z_min is None or item.source.z_index >= z_min)
            and (z_max is None or item.source.z_index <= z_max)
        ]
        hidden_source_ids = set(hide_visualizers or ()) | set(hide_source_ids or ())
        global_seconds = (
            max(0.0, timeline_seconds)
            if timeline_seconds is not None
            else max(0.0, start_seconds + elapsed_seconds)
        )
        needs_embedded_cover = any(
            item.source.id not in hidden_source_ids
            and (
                (item.source.source_type is SourceType.ALBUM_COVER and not item.source.content_path)
                or (item.source.source_type is SourceType.BACKGROUND
                    and item.source.background_mode == "album_art")
                or item.source.uses_personal_color
            )
            for item in source_items
        )
        track_cover = (
            extract_track_cover(track.file_path, track.cover_path)
            if needs_embedded_cover else QPixmap()
        )
        personal_color = (
            extract_track_personal_color(track.file_path, track.cover_path)
            if any(item.source.uses_personal_color for item in source_items)
            else QColor()
        )
        for graphics_item in source_items:
            if not isinstance(graphics_item, SourceItem):
                continue
            source = graphics_item.source
            hidden_for_capture = source.id in hidden_source_ids
            timing_end = source.timeline_start + source.timeline_duration
            outside_timing = (
                global_seconds < source.timeline_start
                or (source.timeline_duration > 0.0 and global_seconds >= timing_end)
            )
            if hidden_for_capture or outside_timing:
                original_visibility.append((graphics_item, graphics_item.isVisible()))
                graphics_item.setVisible(False)
                # A split preview pass deliberately excludes this source.  Do
                # not also expand templates, recreate cover backgrounds, or
                # mutate its animation state only to restore it immediately.
                continue
            if (source.source_type is SourceType.TIME
                    or (source.source_type is SourceType.TEXT and "%" in source.text)):
                original_text.append((graphics_item, source.text))
                template = (
                    source.text if "%" in source.text else "%current_time%"
                )
                source.text = expand_track_template(
                    template, track, track_number, track_total,
                    global_seconds - elapsed_seconds,
                    elapsed_seconds, playlist_duration_seconds,
                )
                graphics_item.update()
            if source.source_type is SourceType.LYRICS:
                original_text.append((graphics_item, source.text))
                original_subtitle_lines.append((
                    graphics_item, source.subtitle_current_line,
                    source.subtitle_current_line_count,
                ))
                original_subtitle_offsets.append((graphics_item, source.subtitle_scroll_offset))
                original_subtitle_transitions.append((
                    graphics_item, graphics_item._subtitle_transition_progress,
                ))
                original_subtitle_style_indices.append((
                    graphics_item, graphics_item._subtitle_line_style_indices,
                ))
                original_subtitle_motion.append((
                    graphics_item, graphics_item._subtitle_transition_raw, graphics_item._subtitle_cue_layout,
                    graphics_item._subtitle_intro_state,
                    graphics_item._subtitle_word_reveal,
                    graphics_item._subtitle_gap_unblurred,
                    graphics_item._subtitle_before_first,
                    graphics_item._subtitle_blur_restore_lines,
                    graphics_item._subtitle_blur_restore_progress,
                    graphics_item._subtitle_prefetch_line_count,
                    graphics_item._subtitle_prefetch_progress,
                    graphics_item._subtitle_prefetch_entry_line_count,
                ))
                graphics_item._subtitle_intro_state = resolve_lyrics_intro_state(
                    track, source, elapsed_seconds,
                    (lyric_instrumental_spans_by_source[source.id]
                     if lyric_instrumental_spans_by_source is not None else
                     instrumental_spans(track, source) if lyric_instrumental_spans is None else lyric_instrumental_spans),
                )
                original_subtitle_anchors.append((
                    graphics_item, graphics_item._subtitle_anchor_line,
                    graphics_item._subtitle_anchor_line_count,
                    graphics_item._subtitle_previous_line_count,
                    graphics_item._subtitle_leaving_line_count,
                    graphics_item._subtitle_entering_line_count,
                    graphics_item._subtitle_emphasis,
                    graphics_item._subtitle_previous_emphasis,
                    graphics_item._subtitle_incoming_visible,
                ))
                graphics_item._subtitle_transition_progress = 1.0
                graphics_item._subtitle_transition_raw = 1.0
                graphics_item._subtitle_cue_layout = ()
                graphics_item._subtitle_previous_line_count = 0
                graphics_item._subtitle_leaving_line_count = 0
                graphics_item._subtitle_entering_line_count = 0
                graphics_item._subtitle_prefetch_line_count = 0
                graphics_item._subtitle_prefetch_progress = 0.0
                graphics_item._subtitle_prefetch_entry_line_count = 0
                graphics_item._subtitle_emphasis = 1.0
                graphics_item._subtitle_previous_emphasis = 1.0
                graphics_item._subtitle_incoming_visible = True
                cue_state = resolve_lyrics_cue_state(
                    track, elapsed_seconds,
                    track.lyrics_timing_offset_seconds, source.subtitle_timing_offset,
                    source.subtitle_animation, source.subtitle_animation_duration,
                    whole_lines=source.subtitle_karaoke_enabled,
                )
                active_cue_index = cue_state.active_cue_index
                display_cues, cue_index = lyrics_display_groups(track, cue_state)
                lyric_cue = display_cues[cue_index] if cue_index is not None else None
                graphics_item._subtitle_gap_unblurred = cue_state.gap_unblurred
                graphics_item._subtitle_before_first = cue_state.before_first
                graphics_item._subtitle_blur_restore_lines = frozenset()
                graphics_item._subtitle_blur_restore_progress = lyric_progress(source, cue_state.blur_restore_progress)
                graphics_item._subtitle_word_reveal = resolve_lyrics_word_reveal(
                    track, source, elapsed_seconds, cue_state, graphics_item._subtitle_intro_state,
                )
                lyric = LyricsService.decode_line_breaks(
                    lyric_cue.get("text", "") if lyric_cue else ""
                )
                transitioning = cue_state.transitioning
                eased = 1.0
                if transitioning:
                    eased = lyric_progress(source, cue_state.transition_progress)
                    graphics_item._subtitle_transition_raw = cue_state.transition_progress
                if cue_index is not None:
                    context, next_context = _effective_lyric_context(
                        graphics_item, display_cues, cue_index,
                    )
                else:
                    context = max(0, source.subtitle_context_lines)
                    next_context = max(0, source.subtitle_next_lines)
                # Keep the cue leaving the top of the visible context for one
                # extra transition slot. Otherwise the oldest displayed row is
                # removed in one frame when the lyric window advances.
                extra_leading = (
                    1 if (
                        transitioning and eased < 1.0
                        and cue_index is not None and cue_index > context
                    ) else 0
                )
                if cue_index is not None:
                    first = max(0, cue_index - context - extra_leading)
                    visible_last = min(len(display_cues), cue_index + next_context + 1)
                    # Prepare two future cue blocks before either becomes a
                    # displayed upcoming cue. They do not consume context slots.
                    last = min(len(display_cues), visible_last + (2 if next_context > 0 else 0))
                    blocks = [
                        LyricsService.decode_line_breaks(cue.get("text", "")).strip()
                        for cue in display_cues[first:last]
                    ]
                    block_line_counts = [
                        max(1, len([line for line in block.splitlines() if line.strip()]))
                        for block in blocks
                    ]
                    graphics_item._subtitle_prefetch_line_count = sum(block_line_counts[visible_last - first:])
                    # Bring the nearest staged cue in before the next clock
                    # boundary, on every cue, rather than restarting its entry
                    # after the current lyric changes. Derive from audio time
                    # so seeking and export follow the same path as playback.
                    if (last > visible_last and cue_index + 1 < len(display_cues)
                            and source.subtitle_animation != "none" and not cue_state.before_first):
                        def cue_start(index):
                            cue = display_cues[index]
                            originals = cue.get("display_cue_indices")
                            return min(float(track.lyrics[position]["start"]) for position in originals) if originals else float(cue["start"])
                        start, following_start = cue_start(cue_index), cue_start(cue_index + 1)
                        _, following_context = _effective_lyric_context(graphics_item, display_cues, cue_index + 1)
                        following_last = min(len(display_cues), cue_index + following_context + 2)
                        prepared_last = min(last, following_last)
                        graphics_item._subtitle_prefetch_entry_line_count = sum(
                            block_line_counts[visible_last - first:prepared_last - first])
                        duration = min(source.subtitle_animation_duration, max(.05, (following_start - start) / 2))
                        clock = elapsed_seconds + track.lyrics_timing_offset_seconds + source.subtitle_timing_offset
                        graphics_item._subtitle_prefetch_progress = lyric_progress(
                            source, max(0.0, min(1.0, (clock - following_start + duration) / duration)))
                    restored_rows = set()
                    if cue_state.blur_restore_cue_indices:
                        row_offset = 0
                        for display_index in range(first, last):
                            original_indices = display_cues[display_index].get("display_cue_indices", (display_index,))
                            for original_index in original_indices:
                                rows = max(1, len([line for line in LyricsService.decode_line_breaks(
                                    track.lyrics[original_index].get("text", "")).strip().splitlines() if line.strip()]))
                                if original_index in cue_state.blur_restore_cue_indices:
                                    restored_rows.update(range(row_offset, row_offset + rows))
                                row_offset += rows
                    graphics_item._subtitle_blur_restore_lines = frozenset(restored_rows)
                    graphics_item._subtitle_line_style_indices = tuple(
                        line_index
                        for cue, line_count in zip(display_cues[first:last], block_line_counts)
                        for line_index in cue.get("display_line_positions", tuple(range(line_count)))
                    )
                    graphics_item._subtitle_cue_layout = tuple(
                        (slot, line_index, line_count)
                        for slot, line_count in enumerate(block_line_counts)
                        for line_index in range(line_count)
                    )
                    source.text = "\n".join(block for block in blocks if block) or source.subtitle_fallback
                    relative_index = max(0, cue_index - first)
                    intro_state = graphics_item._subtitle_intro_state
                    if intro_state is not None and intro_state.held_cue_index is not None:
                        held_display_index = (display_cues.index_for_cue(intro_state.held_cue_index)
                            if hasattr(display_cues, "index_for_cue") else intro_state.held_cue_index)
                        graphics_item._subtitle_intro_state = replace(
                            intro_state, held_slot=held_display_index - first,
                        )
                    anchor_line = sum(block_line_counts[:relative_index])
                    graphics_item._subtitle_anchor_line = anchor_line
                    graphics_item._subtitle_anchor_line_count = block_line_counts[relative_index]
                    graphics_item._subtitle_leaving_line_count = (
                        block_line_counts[0] if extra_leading else 0
                    )
                    releasing = (
                        cue_state.release_progress is not None
                        and cue_state.release_progress < 1.0
                    )
                    if active_cue_index == cue_state.cue_index or releasing:
                        source.subtitle_current_line = anchor_line
                        source.subtitle_current_line_count = block_line_counts[relative_index]
                        if releasing:
                            # A gap follows: dim the held cue instead of cutting
                            # its highlight on the frame it ends.
                            graphics_item._subtitle_emphasis = 1.0 - ease_in_out_cubic(
                                cue_state.release_progress,
                            )
                    else:
                        source.subtitle_current_line = -1
                        source.subtitle_current_line_count = 1
                else:
                    source.text = lyric or source.text or source.subtitle_fallback
                    graphics_item._subtitle_line_style_indices = tuple(
                        range(max(1, len([
                            line for line in source.text.splitlines() if line.strip()
                        ])))
                    )
                    source.subtitle_current_line = -1
                    source.subtitle_current_line_count = 1
                    graphics_item._subtitle_anchor_line = -1
                    graphics_item._subtitle_anchor_line_count = 1
                    graphics_item._subtitle_leaving_line_count = 0
                if transitioning:
                    graphics_item._subtitle_transition_progress = eased
                    # Keep the lyric card/background stable. Only its text layout
                    # moves. Measure between cue centers so differing multiline
                    # cue heights stay continuous across the boundary.
                    previous_line_count = 0
                    if cue_index and cue_index > 0 and (context > 0 or extra_leading):
                        previous_text = LyricsService.decode_line_breaks(
                            display_cues[cue_index - 1].get("text", "")
                        )
                        previous_line_count = max(
                            1, len([line for line in previous_text.splitlines() if line.strip()])
                        )
                    graphics_item._subtitle_previous_line_count = previous_line_count
                    graphics_item._subtitle_previous_emphasis = cue_state.previous_emphasis
                    if cue_index is not None:
                        # Cue 0 is shown (dimmed) before it starts; later cues
                        # depend on the previous window, not on how many cues
                        # remain after the one that just became current.
                        previous_next = 0
                        if cue_index > 0:
                            _, previous_next = _effective_lyric_context(
                                graphics_item, display_cues, cue_index - 1,
                            )
                        graphics_item._subtitle_incoming_visible = (
                            cue_index == 0 or previous_next > 0
                        )
                        # Automatic context shrinks near the end. A tail cue
                        # already in the previous window must not enter again.
                        if cue_index > 0 and next_context > 0:
                            previous_last = min(len(display_cues), cue_index + previous_next)
                            # Up to two staged cues completed their entrances
                            # before this boundary. Keep their ordinary scroll
                            # instead of replaying an entrance from below.
                            entering_first = max(cue_index + 1, previous_last + 2)
                            if entering_first < visible_last:
                                graphics_item._subtitle_entering_line_count = sum(
                                    block_line_counts[entering_first - first:visible_last - first]
                                )
                    line_height = graphics_item._lyric_line_height()
                    source.subtitle_scroll_offset = (
                        (previous_line_count + graphics_item._subtitle_anchor_line_count) / 2
                        * line_height * (1.0 - eased) if previous_line_count else 0.0
                    )
                graphics_item.update()
            if source.source_type is SourceType.TRACK_LIST:
                original_text.append((graphics_item, source.text))
                original_track_list_rows.append(
                    (graphics_item, source.track_list_current_row)
                )
                tracks = playlist_tracks or [track]
                current_index = max(0, min(len(tracks) - 1, track_number - 1))
                count = graphics_item.effective_track_list_count()
                if source.track_list_window == "upcoming":
                    first = current_index
                elif source.track_list_window == "history":
                    first = max(0, current_index - count + 1)
                else:
                    first = max(0, current_index - count // 2)
                last = min(len(tracks), first + count)
                first = max(0, last - count)
                lines: list[str] = []
                marker = {
                    "play": "▶", "dot": "●", "line": "▌", "none": "",
                }.get(source.track_list_marker, "▶")
                for list_index in range(first, last):
                    entry = tracks[list_index]
                    title = entry.title or Path(entry.file_path).stem
                    prefix = f"{list_index + 1:02d}. " if source.track_list_show_number else ""
                    active_marker = marker if list_index == current_index else " " * len(marker)
                    details: list[str] = []
                    if source.track_list_show_artist and entry.artist:
                        details.append(entry.artist)
                    if source.track_list_show_album and entry.album:
                        details.append(entry.album)
                    suffix = f" — {' · '.join(details)}" if details else ""
                    lines.append(f"{active_marker} {prefix}{title}{suffix}".strip())
                source.track_list_current_row = max(0, current_index - first)
                source.text = "\n".join(lines)
                graphics_item.update()
            if source.source_type is SourceType.NOW_PLAYING:
                original_text.append((graphics_item, source.text))
                original_visibility.append((graphics_item, graphics_item.isVisible()))
                title = track.title or Path(track.file_path).stem
                artist = track.artist or "Unknown artist"
                album = f"\n{track.album}" if track.album else ""
                source.text = f"NOW PLAYING\n{title}\n{artist}{album}"
                exit_state = resolve_now_playing_exit_state(
                    screen_elapsed, source.now_playing_duration, source.now_playing_exit_duration,
                )
                graphics_item.setVisible(exit_state.visible)
                if exit_state.exit_progress is not None:
                    exit_progress = exit_state.exit_progress
                    # The previous quintic exit stayed almost fully opaque
                    # until the last few frames, which made moving cards look
                    # as if they popped out. Couple position/scale and opacity
                    # to a balanced curve so every exit style visibly fades
                    # while its configured motion continues.
                    exit_motion = ease_in_out_cubic(exit_progress)
                    exit_opacity = 1.0 - exit_motion
                    original_transforms.append((
                        graphics_item, graphics_item.pos(), graphics_item.scale(),
                        graphics_item.rotation(), graphics_item.opacity(),
                        graphics_item.transform(),
                    ))
                    graphics_item._suppress_position_sync = True
                    if source.now_playing_exit_animation == "fade":
                        graphics_item.setOpacity(exit_opacity)
                    elif source.now_playing_exit_animation == "slide_up":
                        graphics_item.setPos(graphics_item.pos().x(), graphics_item.pos().y() - 24.0 * exit_motion)
                        graphics_item.setOpacity(exit_opacity)
                    elif source.now_playing_exit_animation == "slide_down":
                        graphics_item.setPos(graphics_item.pos().x(), graphics_item.pos().y() + 24.0 * exit_motion)
                        graphics_item.setOpacity(exit_opacity)
                    elif source.now_playing_exit_animation == "zoom":
                        graphics_item.setScale(source.scale * (1.0 - exit_motion * 0.08))
                        graphics_item.setOpacity(exit_opacity)
                graphics_item.update()
            if source.source_type is SourceType.PROGRESS_BAR:
                original_progress.append((graphics_item, source.progress_value))
                if source.progress_mode == "video":
                    source.progress_value = max(0.0, min(
                        1.0,
                        global_seconds
                        / max(0.01, playlist_duration_seconds or track.duration_seconds),
                    ))
                else:
                    source.progress_value = max(0.0, min(
                        1.0, elapsed_seconds / max(0.01, track.duration_seconds)
                    ))
                graphics_item.update()
            filter_key = (
                track.file_path, track.cover_path,
                source.brightness, source.contrast, source.blur,
            )
            if source.source_type is SourceType.ALBUM_COVER and not source.content_path:
                original_covers.append((graphics_item, QPixmap(graphics_item._pixmap)))
                graphics_item._pixmap = _filtered_track_pixmap(
                    graphics_item, lambda: QPixmap(track_cover), ("cover", *filter_key),
                )
                graphics_item.update()
            if source.source_type is SourceType.BACKGROUND and source.background_mode == "album_art":
                original_backgrounds.append((graphics_item, QPixmap(graphics_item._pixmap)))
                if source.background_ambient:
                    ambient_motion = max(0.0, min(3.0, source.background_ambient_motion))
                    flow_step = round(global_seconds * ambient_motion * AMBIENT_FLOW_HZ)
                    graphics_item._pixmap = _filtered_track_pixmap(
                        graphics_item,
                        lambda: create_cached_ambient_background(
                            track.file_path, max(1, round(source.width)),
                            max(1, round(source.height)), source.background_ambient_blur,
                            track.cover_path, phase=global_seconds,
                            motion=ambient_motion,
                            brightness=source.brightness, contrast=source.contrast,
                        ),
                        ("ambient", round(source.width), round(source.height),
                         round(source.background_ambient_blur, 2), flow_step,
                         *filter_key),
                        prefiltered=True,
                    )
                else:
                    graphics_item._pixmap = _filtered_track_pixmap(
                        graphics_item, lambda: QPixmap(track_cover),
                        ("bgcover", *filter_key),
                    )
                previous_track = (
                    playlist_tracks[track_number - 2]
                    if (source.background_track_transition
                        and playlist_tracks is not None
                        and 2 <= track_number <= len(playlist_tracks))
                    else None
                )
                fade_seconds = max(0.05, source.background_track_transition_seconds)
                if previous_track is not None and 0.0 <= screen_elapsed < fade_seconds:
                    blend = ease_in_out_cubic(
                        max(0.0, min(1.0, screen_elapsed / fade_seconds))
                    )
                    filters = (source.brightness, source.contrast, source.blur)
                    if source.background_ambient:
                        previous_background = _filtered_track_pixmap(
                            graphics_item,
                            lambda: create_cached_ambient_background(
                                previous_track.file_path,
                                max(1, round(source.width)),
                                max(1, round(source.height)),
                                source.background_ambient_blur,
                                previous_track.cover_path, phase=global_seconds,
                                motion=ambient_motion,
                                brightness=source.brightness, contrast=source.contrast,
                            ),
                            ("ambient-prev", round(source.width),
                             round(source.height),
                             round(source.background_ambient_blur, 2), flow_step,
                             previous_track.file_path,
                             previous_track.cover_path, *filters),
                            prefiltered=True,
                        )
                    else:
                        previous_background = _filtered_track_pixmap(
                            graphics_item,
                            lambda: QPixmap(extract_track_cover(
                                previous_track.file_path,
                                previous_track.cover_path,
                            )),
                            ("bgcover-prev", previous_track.file_path,
                             previous_track.cover_path, *filters),
                        )
                    graphics_item._pixmap = _crossfade_pixmaps(
                        previous_background, graphics_item._pixmap, blend,
                    )
                graphics_item.update()
            if source.personal_color_enabled and personal_color.isValid():
                fields = source.personal_color_fields
                original = {field: str(getattr(source, field)) for field in fields}
                gradient_original = (
                    (source.gradient.start_color, source.gradient.end_color)
                    if "fill_color" in fields and source.gradient.enabled else None
                )
                for field, fallback in original.items():
                    setattr(source, field, adjust_personal_color(
                        personal_color,
                        fallback,
                        brightness=source.personal_color_brightness,
                        saturation=source.personal_color_saturation,
                        hue_shift=source.personal_color_hue_shift,
                        strength=source.personal_color_strength,
                    ))
                if gradient_original is not None:
                    source.gradient.start_color = adjust_personal_color(
                        personal_color, gradient_original[0],
                        brightness=source.personal_color_brightness,
                        saturation=source.personal_color_saturation,
                        hue_shift=source.personal_color_hue_shift,
                        strength=source.personal_color_strength,
                    )
                    source.gradient.end_color = adjust_personal_color(
                        personal_color, gradient_original[1],
                        brightness=source.personal_color_brightness,
                        saturation=source.personal_color_saturation,
                        hue_shift=source.personal_color_hue_shift,
                        strength=source.personal_color_strength,
                    )
                original_personal_colors.append((
                    graphics_item, original, gradient_original,
                ))
                graphics_item.update()
            shadow = source.shadow
            if shadow.enabled and shadow.personal_color_enabled and personal_color.isValid():
                original_shadow_colors.append((graphics_item, shadow.color))
                shadow.color = adjust_personal_color(
                    personal_color, shadow.color,
                    brightness=shadow.personal_color_brightness,
                    saturation=shadow.personal_color_saturation,
                    hue_shift=shadow.personal_color_hue_shift,
                    strength=shadow.personal_color_strength,
                )
                graphics_item.update()
            window_phase, window_progress = resolve_timeline_window_phase(
                source, global_seconds,
            )
            source_has_window = source.timeline_start > 0.0 or source.timeline_duration > 0.0
            if source_has_window:
                edge = (
                    (window_phase, source.animation_in if window_phase == "in" else source.animation_out,
                     max(0.0, min(1.0, window_progress)))
                    if window_phase is not None else None
                )
            else:
                edge = resolve_edge_animation(
                    source, animation_phase, elapsed_seconds=elapsed_seconds,
                    track_duration=track.duration_seconds, phase_progress=animation_progress,
                    phase_duration=animation_phase_duration, junction=junction,
                )
            pose = loop_pose(
                source.loop_motion, global_seconds, source.loop_motion_period,
                source.loop_motion_amount, source.width, source.height,
            )
            if (source.source_type is SourceType.BACKGROUND
                    and source.background_bass_reactive):
                level = max(0.0, min(1.0, float(bass_level)))
                bass_scale = 1.0 + level * source.background_bass_strength
                pose = pose.combined(AnimationPose(
                    dx=-source.width * (bass_scale - 1.0) * 0.5,
                    dy=-source.height * (bass_scale - 1.0) * 0.5,
                    scale=bass_scale,
                ))
            music_level = music_levels.get(source.id, 0.0) if music_levels is not None else bass_level
            if source.source_type is SourceType.LYRICS:
                original_music_levels.append((graphics_item, graphics_item._music_reaction_level,
                                              graphics_item._music_preview_current_line))
                graphics_item._music_preview_current_line = None
                graphics_item._music_reaction_level = music_level if source.uses_bass_reaction else 0.0
            elif source.source_type is SourceType.TEXT and source.uses_bass_reaction:
                pose = pose.combined(music_reactive_pose(
                    source.music_reactive_effect, music_level,
                    source.music_reactive_strength, source.font_size,
                ))
            if edge is not None and edge[1] != "none":
                phase, style, local_progress = edge
                pose = animation_pose(
                    style, local_progress, phase == "in", source.width, source.height,
                ).combined(pose)
            if not pose.is_identity:
                original_transforms.append((
                    graphics_item, graphics_item.pos(), graphics_item.scale(),
                    graphics_item.rotation(), graphics_item.opacity(),
                    graphics_item.transform(),
                ))
                graphics_item._suppress_position_sync = True
                # Relative to the current placement, so a Now Playing exit
                # (applied above) and this pose compose instead of replacing.
                graphics_item.setOpacity(graphics_item.opacity() * pose.opacity)
                graphics_item.setScale(graphics_item.scale() * pose.scale)
                graphics_item.setRotation(graphics_item.rotation() + pose.rotation)
                graphics_item.setPos(
                    graphics_item.pos().x() + pose.dx, graphics_item.pos().y() + pose.dy,
                )
                if pose.scale_x != 1.0:
                    center_x = source.width / 2.0
                    graphics_item.setTransform(
                        QTransform().translate(center_x, 0.0).scale(pose.scale_x, 1.0)
                        .translate(-center_x, 0.0),
                    )
        try:
            effective_capture_rect = capture_rect
            used_partial_render = False
            if partial_render and transparent and capture_rect is None:
                candidate = CanvasSnapshot._dynamic_band_capture_rect(
                    scene, z_min, z_max,
                )
                artboard = scene.artboard_rect
                if candidate is not None:
                    artboard_area = max(1.0, artboard.width() * artboard.height())
                    candidate_area = candidate.width() * candidate.height()
                    # A near-full crop adds a blit without materially reducing
                    # scene painting. Keep the established full render there.
                    if candidate_area < artboard_area * 0.9:
                        effective_capture_rect = candidate
                        used_partial_render = True
            captured = CanvasSnapshot.capture(
                scene, output_scale, z_min=z_min, z_max=z_max, transparent=transparent,
                image_buffer=(None if used_partial_render else image_buffer),
                capture_rect=effective_capture_rect,
            )
            if used_partial_render and effective_capture_rect is not None:
                captured = CanvasSnapshot._expand_partial_capture(
                    captured,
                    effective_capture_rect,
                    scene.artboard_rect,
                    output_scale,
                )
            if render_metrics is not None:
                render_metrics["partial_render"] = used_partial_render
                render_metrics["capture_rect"] = effective_capture_rect
            return captured
        finally:
            for graphics_item, color in original_shadow_colors:
                graphics_item.source.shadow.color = color
                graphics_item.update()
            for graphics_item, level, current_line in original_music_levels:
                graphics_item._music_reaction_level = level
                graphics_item._music_preview_current_line = current_line
                graphics_item.update()
            for graphics_item, colors, gradient_colors in original_personal_colors:
                for field, value in colors.items():
                    setattr(graphics_item.source, field, value)
                if gradient_colors is not None:
                    graphics_item.source.gradient.start_color = gradient_colors[0]
                    graphics_item.source.gradient.end_color = gradient_colors[1]
                graphics_item.update()
            for graphics_item, text in original_text:
                graphics_item.source.text = text
                graphics_item.update()
            for graphics_item, current_line, line_count in original_subtitle_lines:
                graphics_item.source.subtitle_current_line = current_line
                graphics_item.source.subtitle_current_line_count = line_count
                graphics_item.update()
            for graphics_item, scroll_offset in original_subtitle_offsets:
                graphics_item.source.subtitle_scroll_offset = scroll_offset
                graphics_item.update()
            for graphics_item, progress in original_subtitle_transitions:
                graphics_item._subtitle_transition_progress = progress
                graphics_item.update()
            for graphics_item, indices in original_subtitle_style_indices:
                graphics_item._subtitle_line_style_indices = indices
                graphics_item.update()
            for (graphics_item, raw, layout, intro_state, word_reveal, gap_unblurred, before_first,
                 blur_restore_lines, blur_restore_progress, prefetch_line_count, prefetch_progress,
                 prefetch_entry_line_count) in original_subtitle_motion:
                graphics_item._subtitle_transition_raw = raw
                graphics_item._subtitle_cue_layout = layout
                graphics_item._subtitle_intro_state = intro_state
                graphics_item._subtitle_word_reveal = word_reveal
                graphics_item._subtitle_gap_unblurred = gap_unblurred
                graphics_item._subtitle_before_first = before_first
                graphics_item._subtitle_blur_restore_lines = blur_restore_lines
                graphics_item._subtitle_blur_restore_progress = blur_restore_progress
                graphics_item._subtitle_prefetch_line_count = prefetch_line_count
                graphics_item._subtitle_prefetch_progress = prefetch_progress
                graphics_item._subtitle_prefetch_entry_line_count = prefetch_entry_line_count
                graphics_item.update()
            for (
                graphics_item, anchor_line, anchor_count, previous_line_count,
                leaving_line_count, entering_line_count, emphasis,
                previous_emphasis, incoming_visible,
            ) in original_subtitle_anchors:
                graphics_item._subtitle_anchor_line = anchor_line
                graphics_item._subtitle_anchor_line_count = anchor_count
                graphics_item._subtitle_previous_line_count = previous_line_count
                graphics_item._subtitle_leaving_line_count = leaving_line_count
                graphics_item._subtitle_entering_line_count = entering_line_count
                graphics_item._subtitle_emphasis = emphasis
                graphics_item._subtitle_previous_emphasis = previous_emphasis
                graphics_item._subtitle_incoming_visible = incoming_visible
                graphics_item.update()
            for graphics_item, current_row in original_track_list_rows:
                graphics_item.source.track_list_current_row = current_row
                graphics_item.update()
            for graphics_item, progress in original_progress:
                graphics_item.source.progress_value = progress
                graphics_item.update()
            for graphics_item, pixmap in original_covers:
                graphics_item._pixmap = pixmap
                graphics_item.update()
            for graphics_item, pixmap in original_backgrounds:
                graphics_item._pixmap = pixmap
                graphics_item.update()
            for graphics_item, visible in original_visibility:
                graphics_item.setVisible(visible)
            # Newest first: an item changed twice ends on its oldest state.
            for (
                graphics_item, position, scale, rotation, opacity, transform,
            ) in reversed(original_transforms):
                graphics_item.setPos(position)
                graphics_item.setScale(scale)
                graphics_item.setRotation(rotation)
                graphics_item.setOpacity(opacity)
                graphics_item.setTransform(transform)
                graphics_item._suppress_position_sync = False
