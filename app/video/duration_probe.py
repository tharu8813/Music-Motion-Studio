"""Bounded, allocation-light MP4 timing reader for previews and audio decoding.

Inspect container metadata without reading or decoding media samples.
The movie duration also works with video-only MP4s that report zero
duration to audio tag readers.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path


_MP4_EXTENSIONS = frozenset({".mp4", ".m4v", ".mov", ".m4a"})


def _boxes(stream, first: int, end: int) -> list[tuple[bytes, int, int]]:
    """Read a bounded list of box headers without loading their payloads."""
    boxes = []
    cursor = first
    for _ in range(8192):
        if cursor + 8 > end:
            break
        stream.seek(cursor)
        header = stream.read(8)
        if len(header) != 8:
            break
        box_size, kind = struct.unpack(">I4s", header)
        data_start = cursor + 8
        if box_size == 1:
            extended = stream.read(8)
            if len(extended) != 8:
                break
            box_size = struct.unpack(">Q", extended)[0]
            data_start += 8
        elif box_size == 0:
            box_size = end - cursor
        box_end = cursor + box_size
        if box_size < data_start - cursor or box_end > end:
            break
        boxes.append((kind, data_start, box_end))
        cursor = box_end
    return boxes


def mp4_container_duration(path: Path) -> float | None:
    """Return movie duration in seconds if available, or None for FFprobe."""
    if path.suffix.lower() not in _MP4_EXTENSIONS:
        return None
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            end = stream.tell()
            if end < 16:
                return None
            for kind, data_start, box_end in _boxes(stream, 0, end):
                if kind != b"moov":
                    continue
                for nested, start, stop in _boxes(stream, data_start, box_end):
                    if nested != b"mvhd":
                        continue
                    stream.seek(start)
                    payload = stream.read(min(32, stop - start))
                    if len(payload) < 20:
                        return None
                    version = payload[0]
                    if version == 0:
                        timescale = struct.unpack_from(">I", payload, 12)[0]
                        ticks = struct.unpack_from(">I", payload, 16)[0]
                        if ticks == 0xFFFFFFFF:
                            return None
                    elif version == 1 and len(payload) >= 32:
                        timescale = struct.unpack_from(">I", payload, 20)[0]
                        ticks = struct.unpack_from(">Q", payload, 24)[0]
                        if ticks == 0xFFFFFFFFFFFFFFFF:
                            return None
                    else:
                        return None
                    seconds = ticks / timescale if timescale else 0.0
                    if math.isfinite(seconds) and seconds > 0:
                        return seconds
                return None
    except (OSError, OverflowError, struct.error):
        return None
    return None


def m4a_audio_edit(path: Path) -> tuple[float, float | None]:
    """Return the start and length of the first audio track's simple edit."""
    with path.open("rb") as stream:
        file_end = stream.seek(0, 2)
        movie = _find_box(_boxes(stream, 0, file_end), b"moov")
        if movie is None:
            return 0.0, None
        movie_boxes = _boxes(stream, *movie)
        for kind, start, end in movie_boxes:
            if kind != b"trak":
                continue
            track = {name: (first, stop) for name, first, stop in _boxes(stream, start, end)}
            if b"mdia" not in track:
                continue
            media = {name: (first, stop) for name, first, stop in _boxes(stream, *track[b"mdia"])}
            if b"hdlr" not in media or _box_prefix(stream, media[b"hdlr"], 12)[8:] != b"soun":
                continue
            if b"edts" not in track:
                return 0.0, None
            edit = _find_box(_boxes(stream, *track[b"edts"]), b"elst")
            if edit is None:
                return 0.0, None
            payload = _box_prefix(stream, edit, 28)
            if len(payload) < 8:
                raise ValueError("Incomplete M4A edit list.")
            edits = struct.unpack_from(">I", payload, 4)[0]
            if edits == 0:
                return 0.0, None
            if edits != 1:
                raise ValueError("The M4A file uses a complex audio edit list.")
            version = payload[0]
            if version not in (0, 1):
                raise ValueError("Unsupported M4A edit version.")
            length, offset, speed = struct.unpack_from(">IiI" if version == 0 else ">QqI", payload, 8)
            if speed != 65536 or offset < 0:
                raise ValueError("Unsupported M4A audio edit.")
            movie_header = _find_box(movie_boxes, b"mvhd")
            if movie_header is None or b"mdhd" not in media:
                raise ValueError("Missing M4A time scale.")
            duration = length / _timescale(stream, movie_header)
            return offset / _timescale(stream, media[b"mdhd"]), duration or None
    return 0.0, None


def _find_box(boxes: list[tuple[bytes, int, int]], name: bytes) -> tuple[int, int] | None:
    for kind, start, end in boxes:
        if kind == name:
            return start, end
    return None


def _box_prefix(stream, box: tuple[int, int], count: int) -> bytes:
    first, end = box
    stream.seek(first)
    return stream.read(min(count, end - first))


def _timescale(stream, box: tuple[int, int]) -> int:
    payload = _box_prefix(stream, box, 24)
    if len(payload) < 16 or payload[0] not in (0, 1):
        raise ValueError("Invalid M4A time scale.")
    value = struct.unpack_from(">I", payload, 20 if payload[0] == 1 else 12)[0]
    if not value:
        raise ValueError("Invalid M4A time scale.")
    return value
