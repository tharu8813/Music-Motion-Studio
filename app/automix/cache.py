"""Versioned, on-disk cache for AutoMix track analysis.

Keyed by file *content* (SHA-256 + size), not by path, track ID or analyzer
identity alone: the same song is analyzed once however its path changes
(.pvsproj media is re-extracted to a new temp path on every open), a media
replacement is still detected, and switching analyzers never returns a stale
result under a new meaning (the analyzer identity is folded into the key).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
import json
import logging
import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
import threading
import time
from typing import Any, ClassVar

from app.automix.models import TrackAnalysis

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 2  # 2: content-keyed (was path + mtime)


_CACHE_FILE_PATTERNS = ("*.json", "*.tmp")
"""Entries, plus temp files an interrupted atomic write can leave behind."""


def cache_directories() -> tuple[Path, ...]:
    """Every managed audio-analysis cache (rhythm, structure, lyric words)."""
    from app.automix.structure.cache import StructureAnalysisCache

    from app.services.lyrics_alignment_service import _AlignmentCache

    return AnalysisCache.default_root(), StructureAnalysisCache.default_root(), _AlignmentCache.default_root()


def _cache_files(directories: tuple[Path, ...]) -> list[Path]:
    return [path for directory in directories if directory.is_dir()
            for pattern in _CACHE_FILE_PATTERNS for path in directory.glob(pattern)]


def cache_usage(directories: tuple[Path, ...] | None = None) -> tuple[int, int]:
    """(entry count, total bytes) of the AutoMix analysis caches.

    A changed file or analyzer version just stops matching, so old entries
    linger until cleared or aged out by ``prune_caches``.
    """
    entries = total = 0
    for path in _cache_files(directories or cache_directories()):
        try:
            total += path.stat().st_size
        except OSError:
            continue
        if path.suffix == ".json":
            entries += 1
    return entries, total


def clear_caches(directories: tuple[Path, ...] | None = None) -> int:
    """Delete every cached analysis; returns how many files went.

    Safe at any time: results are recomputed on demand, and a write racing
    this simply lands as a fresh entry (writes are atomic replaces).
    """
    removed = 0
    for path in _cache_files(directories or cache_directories()):
        try:
            path.unlink()
            removed += 1
        except OSError as error:  # in use by a concurrent reader on Windows: leave it
            LOGGER.info("AutoMix cache file not removed (%s): %s", error, path)
    return removed


PRUNE_MAX_AGE_DAYS = 180
_STALE_TEMP_SECONDS = 86_400


def prune_caches(
    directories: tuple[Path, ...] | None = None, max_age_days: float = PRUNE_MAX_AGE_DAYS,
) -> int:
    """Delete entries unused for ``max_age_days`` and leftover temp files.

    A cache hit refreshes its entry's mtime, so this removes what no current
    file or analyzer version reads -- the entries that otherwise accumulate
    forever. Meant for a background thread; returns how many files went.
    ponytail: age only, add a total-size cap if a huge library outgrows this.
    """
    now = time.time()
    removed = 0
    for path in _cache_files(directories or cache_directories()):
        limit = max_age_days * 86_400 if path.suffix == ".json" else _STALE_TEMP_SECONDS
        try:
            if now - path.stat().st_mtime > limit:
                path.unlink()
                removed += 1
        except OSError:  # removed or in use by a concurrent reader: skip it
            continue
    if removed:
        LOGGER.info("AutoMix cache pruned %d unused file(s)", removed)
    return removed


def canonical_media_path(path: str) -> str:
    """Normalize a media path so the same file always hashes identically."""
    return os.path.normcase(str(Path(path).expanduser().resolve()))


class BoundedMemo:
    """Thread-safe LRU memo keyed by file version, e.g. (path, size, mtime_ns).

    A changed file gets a new key, so stale values are never returned; they
    just age out. The capacity must stay above the largest playlist: a pass
    over more files than it holds evicts each one before it is reused.
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._items: OrderedDict[Any, Any] = OrderedDict()
        self._inflight: dict[Any, threading.Lock] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: Any) -> Any:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def get_or_compute(self, key: Any, compute: Callable[[], Any]) -> Any:
        """Return the memoized value, computing it once even under concurrent
        callers; the global lock is never held while ``compute`` runs."""
        value = self.get(key)
        if value is not None:
            return value
        with self._lock:
            key_lock = self._inflight.setdefault(key, threading.Lock())
        try:
            with key_lock:
                value = self.get(key)
                if value is None:
                    value = compute()
                    self.put(key, value)
                return value
        finally:
            with self._lock:
                self._inflight.pop(key, None)


_HASH_CHUNK_BYTES = 1024 * 1024
# ~0.5 KB per entry, so 4096 files cost ~2 MB while covering any real playlist.
MEMO_CAPACITY = 4096
# (canonical path, size, mtime_ns) -> content digest, so a session hashes each
# file once -- and the rhythm and structure threads, which open the same file
# at the same moment, share one hash instead of each reading it in full.
_digest_memo = BoundedMemo(MEMO_CAPACITY)


def _content_digest(canonical: str, size: int, mtime_ns: int) -> str:
    def compute() -> str:
        hasher = sha256()
        with open(canonical, "rb") as file:
            while chunk := file.read(_HASH_CHUNK_BYTES):
                hasher.update(chunk)
        return hasher.hexdigest()

    return _digest_memo.get_or_compute((canonical, size, mtime_ns), compute)


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    """Identifies media by *content*: .pvsproj projects re-extract their media to
    a new temp path/mtime on every open, and a path-keyed cache re-analyzed every
    track each time. ``canonical_path``/``mtime_ns`` are kept for logs only."""

    canonical_path: str
    size: int
    mtime_ns: int
    content_sha256: str

    @classmethod
    def of(cls, path: str) -> "_FileFingerprint":
        canonical = canonical_media_path(path)
        stat = Path(canonical).stat()
        return cls(canonical, stat.st_size, stat.st_mtime_ns,
                   _content_digest(canonical, stat.st_size, stat.st_mtime_ns))

    def entry_name(self, *identity: str) -> str:
        """Cache file name for this content plus the cache's own identity parts."""
        key = "\0".join((self.content_sha256, str(self.size), *identity))
        return f"{sha256(key.encode('utf-8')).hexdigest()}.json"

    def record(self) -> dict[str, Any]:
        return {"path": self.canonical_path, "size": self.size, "sha256": self.content_sha256}

    def matches(self, record: object) -> bool:
        return (isinstance(record, dict) and record.get("sha256") == self.content_sha256
                and record.get("size") == self.size)


class VersionedAnalysisCache:
    """Reads and writes one JSON envelope per (file, analyzer) combination.

    Shared by the rhythm cache (``AnalysisCache``) and the structure cache
    (``app.automix.structure.cache.StructureAnalysisCache``). They stay
    separate caches -- own root directory, schema version and logger -- so
    invalidating one never touches the other; only the envelope handling is
    shared. A subclass sets the class attributes below.
    """

    SCHEMA_VERSION: ClassVar[int]
    ANALYSIS_TYPE: ClassVar[Any]
    """Result type; needs ``from_cache_fields`` (validation) and ``to_cache_fields``."""
    DIRECTORY_NAME: ClassVar[str]
    """Folder under ``%LOCALAPPDATA%/PlaylistCanvas`` holding the entries."""
    LOG_LABEL: ClassVar[str]
    """Prefix of every log message, e.g. ``"AutoMix structure"``."""
    LOGGER: ClassVar[logging.Logger]

    def __init__(
        self, root: Path | None = None, *,
        analyzer_id: str = "", analyzer_version: str = "",
    ) -> None:
        self.root = root or self.default_root()
        self.analyzer_id = analyzer_id
        self.analyzer_version = analyzer_version

    @classmethod
    def default_root(cls) -> Path:
        """Return the per-user folder this cache's entries live in."""
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "PlaylistCanvas" / cls.DIRECTORY_NAME

    def _entry_path(self, fingerprint: _FileFingerprint) -> Path:
        return self.root / fingerprint.entry_name(
            self.analyzer_id, self.analyzer_version, str(self.SCHEMA_VERSION),
        )

    def load(self, source_path: str) -> dict[str, Any] | None:
        """Return cached analysis fields for ``source_path``, or None on any miss.

        A missing file, a stale entry, a malformed one, or an entry from a
        different analyzer/schema are all treated the same way: a cache
        miss to be recomputed, never a crash.
        """
        label, logger = self.LOG_LABEL, self.LOGGER
        try:
            fingerprint = _FileFingerprint.of(source_path)
        except OSError:
            return None
        entry_path = self._entry_path(fingerprint)
        try:
            envelope = json.loads(entry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.debug("%s cache miss: %s", label, fingerprint.canonical_path)
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.info("%s cache invalidated: malformed entry for %s", label, fingerprint.canonical_path)
            return None
        if not self._envelope_matches(envelope, fingerprint):
            logger.info("%s cache invalidated: stale entry for %s", label, fingerprint.canonical_path)
            return None
        try:
            fields = envelope["analysis"]
            if not isinstance(fields, dict):
                raise TypeError("analysis field must be an object")
            # Round-trip through the result type to reject a cache entry whose
            # fields no longer pass validation (e.g. hand-edited or from a
            # future schema variant this version does not fully understand).
            self.ANALYSIS_TYPE.from_cache_fields("cache-check", fingerprint.canonical_path, fields)
        except (KeyError, TypeError, ValueError) as error:
            logger.info(
                "%s cache invalidated: invalid fields for %s (%s)",
                label, fingerprint.canonical_path, error,
            )
            return None
        logger.debug("%s cache hit: %s", label, fingerprint.canonical_path)
        try:
            os.utime(entry_path)  # marks the entry as in use for prune_caches()
        except OSError:
            pass
        return fields

    def store(self, source_path: str, analysis: Any) -> None:
        """Atomically write ``analysis`` for ``source_path``, replacing any prior entry."""
        fingerprint = _FileFingerprint.of(source_path)
        entry_path = self._entry_path(fingerprint)
        envelope = {
            "schema_version": self.SCHEMA_VERSION,
            "analyzer_id": self.analyzer_id,
            "analyzer_version": self.analyzer_version,
            "file": fingerprint.record(),
            "analysis": analysis.to_cache_fields(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".tmp", dir=self.root, delete=False,
            ) as temporary:
                json.dump(envelope, temporary, ensure_ascii=False)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.replace(entry_path)
        except OSError:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        else:
            self.LOGGER.debug("%s analysis cached: %s", self.LOG_LABEL, fingerprint.canonical_path)

    def _envelope_matches(self, envelope: object, fingerprint: _FileFingerprint) -> bool:
        if not isinstance(envelope, dict) or envelope.get("schema_version") != self.SCHEMA_VERSION:
            return False
        if (envelope.get("analyzer_id") != self.analyzer_id
                or envelope.get("analyzer_version") != self.analyzer_version):
            return False
        return fingerprint.matches(envelope.get("file"))


class AnalysisCache(VersionedAnalysisCache):
    """The rhythm (BPM/beat/key/vocal) analysis cache: ``TrackAnalysis`` entries."""

    SCHEMA_VERSION = SCHEMA_VERSION
    ANALYSIS_TYPE = TrackAnalysis
    DIRECTORY_NAME = "automix-cache"
    LOG_LABEL = "AutoMix"
    LOGGER = LOGGER
