"""Bounded overlay frames and fallback selection, independent of playback and widgets."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtGui import QImage

OverlaySignature = tuple[int, ...]


class OverlayFrameCache:
    def __init__(self) -> None:
        self._frames: dict[tuple[str, OverlaySignature, int], tuple[QImage, ...]] = {}
        self._last_layers: tuple[QImage, ...] = ()
        self._last_track_id = ""
        self._last_signature: OverlaySignature = ()
        self.revision = 0

    def images(self) -> Iterator[QImage]:
        """Expose retained images for memory accounting without exposing cache keys."""
        for layers in self._frames.values():
            yield from layers

    def first_missing(self, track_id: str, signature: OverlaySignature, start: int, count: int) -> int | None:
        return next((index for index in range(start, start + count)
                     if (track_id, signature, index) not in self._frames), None)

    def store(self, track_id: str, signature: OverlaySignature, frames: list) -> None:
        for frame_index, layers in frames:
            if isinstance(frame_index, int) and isinstance(layers, tuple):
                self._frames[(track_id, signature, frame_index)] = layers
        self.revision += 1

    def layers_for_frame(self, track_id: str, frame_index: int, signature: OverlaySignature) -> tuple[QImage, ...]:
        """Prefer the exact frame, then the last displayed frame, then the newest prior frame."""
        layers = self._frames.get((track_id, signature, frame_index))
        if layers is None or len(layers) != len(signature):
            if (self._last_track_id == track_id and self._last_signature == signature
                    and len(self._last_layers) == len(signature)):
                return self._last_layers
            prior = [
                (index, cached) for (track, cached_signature, index), cached in self._frames.items()
                if track == track_id and cached_signature == signature and index < frame_index
                and len(cached) == len(signature)
            ]
            if not prior:
                return ()
            _index, layers = max(prior, key=lambda entry: entry[0])
        self._last_layers = layers
        self._last_track_id = track_id
        self._last_signature = signature
        return layers

    def trim(self, active_track_id: str, current_frame: int, prefetch_count: int) -> None:
        minimum = max(0, current_frame - 3)
        maximum = current_frame + prefetch_count * 3
        for key in list(self._frames):
            track_id, _signature, frame_index = key
            if track_id != active_track_id or frame_index < minimum or frame_index > maximum:
                self._frames.pop(key, None)

    def clear(self, track_id: str | None = None) -> None:
        self.revision += 1
        if track_id is None:
            self._frames.clear()
        else:
            for key in [key for key in self._frames if key[0] == track_id]:
                self._frames.pop(key, None)
        if track_id is None or self._last_track_id == track_id:
            self._last_layers = ()
            self._last_track_id = ""
            self._last_signature = ()
