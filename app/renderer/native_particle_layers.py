"""Prepare small deterministic Neon sprite lists for native D3D11 export."""
from collections.abc import Iterator
from struct import Struct
from threading import Event

from PySide6.QtGui import QColor

from app.preview.native_canvas_layer_packets import NativeCanvasParticleLayer
from app.renderer.ffmpeg_renderer import RenderCancelledError, VisualizerOverlay
from app.utils.particle_painter import particle_samples


_SPRITE = Struct("<fffI")


def supports_native_particles(overlay: VisualizerOverlay, width: int, height: int) -> bool:
    return (
        overlay.kind == "particles" and overlay.effect_style == "neon"
        and not (overlay.keyframe_binding or overlay.personal_colors
                 or overlay.flip_horizontal or overlay.flip_vertical or overlay.frame_width)
        and overlay.animation_in == overlay.animation_out == overlay.loop_motion == overlay.mask_shape == "none"
        and overlay.rotation == overlay.timeline_start == overlay.timeline_duration == 0
        and 0 <= overlay.x < overlay.x + overlay.width <= width
        and 0 <= overlay.y < overlay.y + overlay.height <= height
    )


def particle_layers(
    overlay: VisualizerOverlay, start: int, end: int, fps: int, cancel: Event,
) -> Iterator[NativeCanvasParticleLayer]:
    if start < 0 or (start > 0 and start >= end):
        raise ValueError("Requested overlay start frame exceeds the analyzed audio")
    clip = (overlay.x, overlay.y, overlay.x + overlay.width, overlay.y + overlay.height)
    colors = tuple(QColor(value).rgb() & 0xFFFFFF for value in (overlay.color, overlay.particle_secondary_color))
    glow = max(0.0, min(1.0, overlay.particle_glow))
    # Match the Qt painter's 8-bit DestinationIn opacity on the complete layer.
    opacity = round(255 * max(0.0, min(1.0, overlay.opacity))) / 255
    for frame in range(start, end):
        if cancel.is_set():
            raise RenderCancelledError("Native GPU export was cancelled.")
        sprites = bytearray()
        samples = particle_samples(
            overlay.width, overlay.height, density=overlay.density, speed=overlay.speed,
            minimum_size=overlay.particle_min_size, maximum_size=overlay.particle_max_size,
            particle_opacity=overlay.particle_opacity, direction=overlay.particle_direction,
            drift=overlay.particle_drift, twinkle=overlay.particle_twinkle,
            seed=overlay.particle_seed, time_seconds=frame / fps,
        )
        for index, (x, y, size, alpha, _) in enumerate(samples):
            rgb = colors[int(index % 3 == 0)]
            x, y, radius = x + overlay.x, y + overlay.y, size / 2
            if glow > 0:
                halo = max(1, round(alpha * glow * 0.28))
                sprites += _SPRITE.pack(x, y, radius * (1.8 + glow * 2.2), rgb | halo << 24)
            sprites += _SPRITE.pack(x, y, radius, rgb | alpha << 24)
        yield NativeCanvasParticleLayer("pending", 4, len(sprites) // _SPRITE.size,
                                        bytes(sprites), opacity, clip=clip)
