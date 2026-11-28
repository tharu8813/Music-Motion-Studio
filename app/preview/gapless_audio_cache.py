"""One bounded session-local, immutable gapless preview audio cache.

A preview owns its own hard link, so closing it or evicting the cache cannot
remove another preview's soundtrack. No persistent cache or source mutation.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from app.timeline.render_plan import CompiledRenderPlan

_MAX_BYTES = 1024 * 1024 * 1024


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
    def __init__(self, max_bytes: int = _MAX_BYTES):
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._entry = None

    def restore(self, key: tuple | None, destination: Path) -> CompiledRenderPlan | None:
        if key is None:
            return None
        with self._lock:
            if self._entry is None or self._entry[0] != key:
                return None
            _, directory, plan = self._entry
            try:
                # Avoid copying potentially hundreds of MB; on a filesystem
                # without hard links, simply render normally.
                os.link(Path(directory.name) / "audio.flac", destination)
            except OSError:
                return None
            return plan

    def remember(self, key: tuple | None, source: Path, plan: CompiledRenderPlan) -> None:
        if key is None:
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
            previous, self._entry = self._entry, (key, directory, plan)
            if previous is not None:
                previous[1].cleanup()

    def clear(self) -> None:
        with self._lock:
            previous, self._entry = self._entry, None
            if previous is not None:
                previous[1].cleanup()


GAPLESS_PREVIEW_CACHE = GaplessPreviewCache()
