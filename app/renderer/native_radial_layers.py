"""Prepare compact GPU radial spectrum geometry from the shared CPU audio envelope."""
from collections.abc import Iterator, Sequence
from math import isfinite
from struct import Struct
from threading import Event

import numpy as np
from PySide6.QtGui import QColor

from app.animation.curves import ease_in_out_cubic
from app.preview.native_canvas_layer_packets import NativeCanvasRadialLayer
from app.renderer.ffmpeg_renderer import RenderCancelledError, VisualizerOverlay
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.utils.radial_visualizer import radial_bar_directions, radial_bar_geometry


_SEGMENT = Struct("<fffffI")


def supports_native_radial(overlay: VisualizerOverlay, width: int, height: int) -> bool:
    if not (
        overlay.kind == "visualizer" and overlay.style == "radial"
        and overlay.animation_in in {"none", "fade"} and overlay.animation_out in {"none", "fade"}
        and overlay.loop_motion == overlay.mask_shape == "none"
        and not (overlay.keyframe_binding or overlay.frame_width or overlay.rotation)
        and 8 <= overlay.width <= 3840 and 8 <= overlay.height <= 2160
        and -overlay.width < overlay.x < width
        and -overlay.height < overlay.y < height
        and isfinite(overlay.line_width)
    ):
        return False
    count = max(4, min(96, int(overlay.bar_count)))
    _, _, stroke = radial_bar_geometry(overlay.width, overlay.height, count,
                                       max(1.0, overlay.line_width), overlay.inner_radius)
    return stroke <= 320.0


def radial_layers(overlay: VisualizerOverlay, levels: np.ndarray, start: int, fps: int,
                  windows: Sequence[tuple[float, ...]], cancel: Event, *,
                  output_size: tuple[int, int]) -> Iterator[NativeCanvasRadialLayer]:
    if start < 0 or (start > 0 and start >= len(levels)):
        raise ValueError("Requested overlay start frame exceeds the analyzed audio")
    if cancel.is_set():
        raise RenderCancelledError("Native GPU export was cancelled.")
    count = max(4, min(96, int(overlay.bar_count)))
    processed = PythonVisualizerRenderer.process_level_sequence(levels, overlay, count)
    left, top = overlay.x, overlay.y
    width, height = overlay.width, overlay.height
    clip = (max(0, left), max(0, top),
            min(output_size[0], left + width), min(output_size[1], top + height))
    cx, cy = left + width / 2, top + height / 2
    inner, reach, stroke = radial_bar_geometry(width, height, count, max(1.0, overlay.line_width),
                                               overlay.inner_radius)
    directions = tuple(((-x if overlay.flip_horizontal else x), (-y if overlay.flip_vertical else y))
                       for x, y in radial_bar_directions(count))
    origins = tuple((cx + dx * inner, cy + dy * inner) for dx, dy in directions)
    base_color = QColor(overlay.color).rgba()
    track_colors = tuple(QColor(color).rgba() for color in overlay.personal_colors)
    opacity = round(255 * max(0.0, min(1.0, overlay.opacity))) / 255
    for frame in range(start, len(processed)):
        if cancel.is_set():
            raise RenderCancelledError("Native GPU export was cancelled.")
        seconds = frame / fps
        layer_opacity = opacity
        if (seconds < overlay.timeline_start or (overlay.timeline_duration > 0
                and seconds >= overlay.timeline_start + overlay.timeline_duration)):
            layer_opacity = 0.0
        elif state := PythonVisualizerRenderer._animation_state(seconds, windows, overlay):
            _, progress, entering = state
            eased = ease_in_out_cubic(progress)
            layer_opacity *= eased if entering else 1.0 - eased
        color = base_color
        if track_colors:
            track = PythonVisualizerRenderer._track_index_at(seconds, windows)
            if 0 <= track < len(track_colors):
                color = track_colors[track]
        lengths = [reach * max(0.03, min(1.0, float(level))) for level in processed[frame]]
        pixels = bytearray(len(directions) * _SEGMENT.size)
        for i, ((dx, dy), (x, y)) in enumerate(zip(directions, origins, strict=True)):
            length = lengths[i // 2]
            _SEGMENT.pack_into(pixels, i * _SEGMENT.size, x, y, x + dx * length, y + dy * length,
                               stroke / 2, color)
        yield NativeCanvasRadialLayer("pending", 6, len(directions), bytes(pixels), layer_opacity, clip=clip)
