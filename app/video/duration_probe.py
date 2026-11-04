"""Bounded, allocation-light MP4 container duration reader for live previews.

Read only the top-level `moov/mvhd` atom. This works with video-only MP4s
that may report zero duration to audio tag readers. It never reads/decodes
media samples. Unsupported or malformed files defer to the existing FFprobe.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path


_MP4_EXTENSIONS = frozenset({".mp4", ".m4v", ".mov", ".m4a"})


def _boxes(stream, first: int, end: int):
    """Yield validated (box name, data start, box end) with bounded iterations."""
    cursor = first
    for _ in range(8192):
        if cursor + 8 > end:
            return
        stream.seek(cursor)
        header = stream.read(8)
        if len(header) != 8:
            return
        box_size, kind = struct.unpack(">I4s", header)
        data_start = cursor + 8
        if box_size == 1:
            extended = stream.read(8)
            if len(extended) != 8:
                return
            box_size = struct.unpack(">Q", extended)[0]
            data_start += 8
        elif box_size == 0:
            box_size = end - cursor
        box_end = cursor + box_size
        if box_size < data_start - cursor or box_end > end:
            return
        yield kind, data_start, box_end
        cursor = box_end


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
