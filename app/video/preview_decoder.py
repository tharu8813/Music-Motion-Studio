"""Shared scheduling rules and diagnostics for live video decoders."""

from __future__ import annotations

from dataclasses import dataclass


MAPPED_YUV420_FORMATS = frozenset({"Format_NV12", "Format_NV21", "Format_YUV420P"})


def video_seek_tolerance_ms(fps: int) -> int:
    """Allow normal decoder clock granularity without repeated corrective seeks."""
    bounded_fps = max(10, min(60, int(fps)))
    return max(160, min(300, round(4000 / bounded_fps)))


def video_position_needs_seek(current_ms: int, target_ms: int, fps: int) -> bool:
    if current_ms < 0:
        return True
    return abs(int(current_ms) - int(target_ms)) > video_seek_tolerance_ms(fps)


@dataclass(frozen=True, slots=True)
class VideoDecoderStats:
    accepted_frames: int = 0
    dropped_frames: int = 0
    seek_count: int = 0
    source_switches: int = 0
    frame_handle: str = ""
    gpu_backed_frame: bool = False
    throttled_frames: int = 0
    pressure_drops: int = 0
    filter_pending: bool = False
    received_frames: int = 0
    mailbox_drops: int = 0
    stream_fps: float = 0.0
    pixel_format: str = ""
