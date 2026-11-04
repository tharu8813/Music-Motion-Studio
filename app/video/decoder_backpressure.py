"""Independent resolution and cadence backpressure for preview video decoders."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DecoderBackpressureState:
    resolution_factor: float
    fps_limit: int
    resolution_level: int
    fps_level: int
    changed: bool


class VideoDecoderBackpressure:
    """Reduce decoder work under sustained pressure without quality flapping."""

    RESOLUTION_FACTORS = (1.0, 0.82, 0.67, 0.52)
    FPS_FACTORS = (1.0, 0.80, 0.60, 0.40)

    def __init__(
        self, base_fps: int = 30, *,
        prefer_resolution: bool = False,
        keep_resolution: bool = False,
    ) -> None:
        self.base_fps = max(1, int(base_fps))
        self.prefer_resolution = bool(prefer_resolution)
        self.keep_resolution = bool(keep_resolution)
        self.resolution_level = 0
        self.fps_level = 0
        self._resolution_pressure = 0
        self._resolution_stable = 0
        self._fps_pressure = 0
        self._fps_stable = 0

    @property
    def resolution_factor(self) -> float:
        return self.RESOLUTION_FACTORS[self.resolution_level]

    @property
    def fps_limit(self) -> int:
        return min(self.base_fps, max(10, round(self.base_fps * self.FPS_FACTORS[self.fps_level])))

    def reset(self, base_fps: int | None = None) -> DecoderBackpressureState:
        previous = (self.resolution_factor, self.fps_limit)
        if base_fps is not None:
            self.base_fps = max(1, int(base_fps))
        self.resolution_level = 0
        self.fps_level = 0
        self._resolution_pressure = 0
        self._resolution_stable = 0
        self._fps_pressure = 0
        self._fps_stable = 0
        return self._state(previous != (self.resolution_factor, self.fps_limit))

    def observe(
        self, *, accepted_frames: int, pressure_drops: int,
        pending_decoders: int, active_decoders: int,
    ) -> DecoderBackpressureState:
        """Consume one rolling sample of decoder and presentation pressure."""
        previous = (self.resolution_factor, self.fps_limit)
        active = max(0, int(active_decoders))
        # Occasional mailbox replacement is expected at vsync, particularly
        # when a decoder publishes two frames in one GUI event-loop turn.
        pressure = (
            pressure_drops >= max(2, (accepted_frames + 9) // 10)
            or pending_decoders > 0
        )

        # Resolution-oriented modes give the decoder time to reduce cadence
        # before sacrificing pixel detail. Quality mode never shrinks video.
        reduce_resolution = (
            pressure and not self.keep_resolution
            and (not self.prefer_resolution or self.fps_level >= 2)
        )
        if reduce_resolution:
            self._resolution_pressure += 1
            self._resolution_stable = 0
            if (
                self._resolution_pressure >= 2
                and self.resolution_level < len(self.RESOLUTION_FACTORS) - 1
            ):
                self.resolution_level += 1
                self._resolution_pressure = 0
        else:
            self._resolution_pressure = 0
            self._resolution_stable += 1
            if self._resolution_stable >= 8 and self.resolution_level > 0:
                self.resolution_level -= 1
                self._resolution_stable = 0

        # Cadence backs off more slowly than resolution. Persistent queue
        # pressure can lower FPS too, but only after resolution already yielded.
        fps_overloaded = (
            pressure and active > 0
            and (self.prefer_resolution or self.resolution_level >= 1)
        )
        if fps_overloaded:
            self._fps_pressure += 1
            self._fps_stable = 0
            if (
                self._fps_pressure >= 3
                and self.fps_level < len(self.FPS_FACTORS) - 1
            ):
                self.fps_level += 1
                self._fps_pressure = 0
        elif not pressure:
            self._fps_pressure = 0
            self._fps_stable += 1
            if self._fps_stable >= 12 and self.fps_level > 0:
                self.fps_level -= 1
                self._fps_stable = 0
        else:
            self._fps_pressure = 0
            self._fps_stable = 0
        return self._state(previous != (self.resolution_factor, self.fps_limit))

    def _state(self, changed: bool) -> DecoderBackpressureState:
        return DecoderBackpressureState(
            resolution_factor=self.resolution_factor,
            fps_limit=self.fps_limit,
            resolution_level=self.resolution_level,
            fps_level=self.fps_level,
            changed=changed,
        )
