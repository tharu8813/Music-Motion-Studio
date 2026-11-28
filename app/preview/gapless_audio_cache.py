"""Bounded session-local LRU cache of immutable gapless preview soundtracks.

A preview owns its own hard link, so closing it or evicting the cache cannot
remove another preview's soundtrack. No persistent cache or source mutation.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from app.timeline.render_plan import CompiledRenderPlan

_MAX_BYTES = 1024 * 1024 * 1024
_MAX_ENTRIES = 3


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    directory: TemporaryDirectory
    plan: CompiledRenderPlan
    size: int


def _fingerprint(path) -> tuple:
    resolved = Path(path).resolve(strict=True)
    stat = resolved.stat()
    if not resolved.is_file():
        raise OSError("Not a file")
    return (str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
            stat.st_dev, stat.st_ino)


def gapless_preview_key(executable, tracks, audio_effects) -> tuple | None:
    """Include the audio inputs and timeline identity, not visual edits."""
    try:
        binary = _fingerprint(executable)
        probe = Path(binary[0]).with_name(
            "ffprobe.exe" if Path(binary[0]).suffix.lower() == ".exe" else "ffprobe"
        )
        # A missing adjacent probe may cause the renderer to use PATH.
        import shutil
        probe_path = probe if probe.is_file() else shutil.which("ffprobe")
        probe_key = _fingerprint(probe_path) if probe_path else None
        inputs = tuple(
            (_fingerprint(track.file_path), track.id, track.duration_seconds,
             track.start_time_seconds, track.enabled, track.audio_filter)
            for track in tracks
        )
        effects = json.dumps(audio_effects or {}, sort_keys=True, separators=(",", ":"))
        return binary, probe_key, inputs, effects
    except (OSError, ValueError, TypeError):
        return None


class GaplessPreviewCache:
    """Retain recent completed mixes, bounded by aggregate bytes and count."""

    def __init__(self, max_bytes: int = _MAX_BYTES, max_entries: int = _MAX_ENTRIES):
        self._max_bytes = max(0, max_bytes)
        self._max_entries = max(0, max_entries)
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, _CacheEntry] = OrderedDict()
        self._bytes = 0

    def _remove(self, key: tuple) -> None:
        entry = self._entries.pop(key)
        self._bytes -= entry.size
        entry.directory.cleanup()

    def restore(self, key: tuple | None, destination: Path) -> CompiledRenderPlan | None:
        if key is None:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            source = Path(entry.directory.name) / "audio.flac"
            try:
                if source.stat().st_size != entry.size:
                    self._remove(key)
                    return None
            except OSError:
                self._remove(key)
                return None
            try:
                # A preview owns its own hard link, even after eviction.
                # Cross-volume and existing-destination failures are misses.
                os.link(source, destination)
            except OSError:
                return None
            self._entries.move_to_end(key)
            return entry.plan

    def remember(self, key: tuple | None, source: Path, plan: CompiledRenderPlan) -> None:
        if key is None or not self._max_entries:
            return
        with self._lock:
            directory = None
            try:
                size = source.stat().st_size
                if not 0 < size <= self._max_bytes:
                    return
                directory = TemporaryDirectory(prefix="playlist-gapless-cache-", ignore_cleanup_errors=True)
                os.link(source, Path(directory.name) / "audio.flac")
            except OSError:
                if directory is not None:
                    directory.cleanup()
                return
            # Publish only after linking succeeds, so a failed replacement
            # cannot evict valid existing cache entries.
            if key in self._entries:
                self._remove(key)
            self._entries[key] = _CacheEntry(directory, plan, size)
            self._bytes += size
            while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
                self._remove(next(iter(self._entries)))

    def clear(self) -> None:
        with self._lock:
            for key in tuple(self._entries):
                self._remove(key)


GAPLESS_PREVIEW_CACHE = GaplessPreviewCache()
