"""Versioned JSON and portable single-file project persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import atexit
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from tempfile import NamedTemporaryFile, gettempdir
import threading
import time
import zipfile
import zlib

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QLockFile
from PySide6.QtGui import QImage

from app.models.project import ProjectDocument


class ProjectError(Exception):
    """Raised when a project document cannot be safely saved or loaded."""


class ProjectLoadCancelled(ProjectError):
    """Raised when the user cancels a package load; the workspace is untouched."""


# (extracted bytes, total asset bytes)
LoadProgress = Callable[[int, int], None]
_EXTRACT_CHUNK_BYTES = 1024 * 1024


@dataclass(slots=True, frozen=True)
class ProjectSummary:
    """Cheap metadata used by the project chooser without loading media."""

    title: str
    description: str
    author: str
    modified_at: str
    thumbnail: bytes = b""


class ProjectService:
    """Read legacy JSON and portable ``.pvsproj`` project packages."""

    PACKAGE_SUFFIX = ".pvsproj"
    MANIFEST_NAME = "project.json"
    THUMBNAIL_NAME = "thumbnail.png"
    MAX_MANIFEST_BYTES = 16 * 1024 * 1024
    MAX_THUMBNAIL_BYTES = 32 * 1024 * 1024
    MAX_ASSETS = 20_000
    _ASSET_HASH_PREFIX = re.compile(r"^(?:[0-9a-fA-F]{12}_)+")
    _MAX_ASSET_BASENAME_LENGTH = 120
    _cache_locks: dict[Path, QLockFile] = {}
    _cache_lock_guard = threading.Lock()

    @classmethod
    def _safe_asset_basename(cls, raw_name: str) -> str:
        """Return a stable Windows-safe basename without accumulated save hashes."""
        name = cls._ASSET_HASH_PREFIX.sub("", Path(raw_name).name)
        safe_name = "".join(
            char if char.isalnum() or char in "._- " else "_"
            for char in name
        ).rstrip(" .")
        if not safe_name:
            safe_name = "asset"
        suffix = Path(safe_name).suffix
        stem = safe_name[:-len(suffix)] if suffix else safe_name
        available = max(1, cls._MAX_ASSET_BASENAME_LENGTH - len(suffix))
        return f"{stem[:available].rstrip(' .') or 'asset'}{suffix}"

    @classmethod
    def _asset_archive_path(cls, identity: str, raw_name: str) -> str:
        digest = sha256(identity.casefold().encode("utf-8")).hexdigest()[:12]
        return f"assets/{digest}_{cls._safe_asset_basename(raw_name)}"

    @classmethod
    def save(
        cls, path: str | Path, document: ProjectDocument,
        thumbnail: QImage | None = None,
    ) -> Path:
        """Atomically save JSON or a portable package based on the file suffix."""
        target = Path(path).expanduser().resolve()
        if target.suffix.lower() not in {".json", cls.PACKAGE_SUFFIX}:
            target = target.with_suffix(cls.PACKAGE_SUFFIX)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix.lower() == ".json":
                return cls._save_json(target, document)
            return cls._save_package(target, document, thumbnail)
        except (OSError, TypeError, ValueError, zipfile.BadZipFile) as error:
            raise ProjectError(f"Could not save project: {error}") from error

    @classmethod
    def load(
        cls, path: str | Path, *,
        progress: LoadProgress | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProjectDocument:
        """Load a package or a backwards-compatible UTF-8 JSON document.

        ``progress``/``cancel_event`` only matter for packages, whose embedded
        media extraction is the slow part; cancelling raises ProjectLoadCancelled.
        """
        target = Path(path).expanduser().resolve()
        try:
            if target.suffix.lower() == cls.PACKAGE_SUFFIX:
                return cls._load_package(target, progress, cancel_event)
            with target.open("r", encoding="utf-8") as file:
                return ProjectDocument.from_dict(json.load(file))
        except (OSError, json.JSONDecodeError, TypeError, ValueError,
                KeyError, zipfile.BadZipFile) as error:
            raise ProjectError(f"Could not open project: {error}") from error

    @classmethod
    def inspect(cls, path: str | Path) -> ProjectSummary:
        """Read only project identity and thumbnail for the startup screen."""
        target = Path(path).expanduser().resolve()
        try:
            thumbnail = b""
            if target.suffix.lower() == cls.PACKAGE_SUFFIX:
                with zipfile.ZipFile(target, "r") as archive:
                    if archive.getinfo(cls.MANIFEST_NAME).file_size > cls.MAX_MANIFEST_BYTES:
                        raise ValueError("Project manifest is unexpectedly large.")
                    data = json.loads(archive.read(cls.MANIFEST_NAME).decode("utf-8"))
                    if cls.THUMBNAIL_NAME in archive.namelist():
                        if archive.getinfo(cls.THUMBNAIL_NAME).file_size > cls.MAX_THUMBNAIL_BYTES:
                            raise ValueError("Project thumbnail is unexpectedly large.")
                        thumbnail = archive.read(cls.THUMBNAIL_NAME)
            else:
                with target.open("r", encoding="utf-8") as file:
                    data = json.load(file)
            settings = data.get("settings", {}) if isinstance(data, dict) else {}
            if not isinstance(settings, dict):
                raise ValueError("Project settings must be an object.")
            return ProjectSummary(
                title=str(settings.get("title") or target.stem.removesuffix(".project")),
                description=str(settings.get("description", "")),
                author=str(settings.get("author", "")),
                modified_at=str(settings.get("modified_at", "")),
                thumbnail=thumbnail,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError,
                zipfile.BadZipFile) as error:
            raise ProjectError(f"Could not inspect project: {error}") from error

    @staticmethod
    def _save_json(target: Path, document: ProjectDocument) -> Path:
        validated = ProjectDocument.from_dict(document.to_dict())
        payload = json.dumps(validated.to_dict(), ensure_ascii=False, indent=2)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".tmp", dir=target.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
            temporary_path.replace(target)
        finally:
            # Antivirus, cloud sync, or a destination lock can make the atomic
            # replace fail. Never leave an accumulating project-sized .tmp file.
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return target

    @classmethod
    def _save_package(
        cls, target: Path, document: ProjectDocument, thumbnail: QImage | None,
    ) -> Path:
        packaged = ProjectDocument.from_dict(document.to_dict())
        packaged.settings.modified_at = datetime.now(timezone.utc).isoformat()
        assets: dict[Path, str] = {}
        missing_assets: set[str] = set()

        def register(raw_path: str) -> str:
            if not raw_path:
                return ""
            source_path = Path(raw_path).expanduser()
            if not source_path.is_file():
                missing_assets.add(raw_path)
                return raw_path
            resolved = source_path.resolve()
            if resolved not in assets:
                assets[resolved] = cls._asset_archive_path(
                    str(resolved), resolved.name,
                )
            return assets[resolved]

        if packaged.settings.content_mode == "embed":
            for source in packaged.sources:
                source.content_path = register(source.content_path)
                source.font_path = register(source.font_path)
                source.video_paths = [register(path) for path in source.video_paths]
            redundant_lyrics: set[str] = set()
            for track in packaged.playlist:
                track.file_path = register(track.file_path)
                if track.lyrics and track.lyrics_path and not Path(track.lyrics_path).expanduser().is_file():
                    redundant_lyrics.add(track.lyrics_path.casefold())
                    track.lyrics_path = ""
                else:
                    track.lyrics_path = register(track.lyrics_path)
                track.cover_path = register(track.cover_path)
                track.video_paths = [register(path) for path in track.video_paths]
            packaged.content_library[:] = [content for content in packaged.content_library
                                            if content.path.casefold() not in redundant_lyrics]
            for content in packaged.content_library:
                content.path = register(content.path)
            if missing_assets:
                raise ProjectError("Cannot embed missing media:\n" + "\n".join(sorted(missing_assets))
                                   + "\nRelink these files or use reference mode.")

        payload = json.dumps(packaged.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        if len(payload) > cls.MAX_MANIFEST_BYTES:
            raise ProjectError("Project manifest is unexpectedly large. Reduce project content before saving.")
        if len(assets) > cls.MAX_ASSETS:
            raise ProjectError("Project package contains too many assets.")
        thumbnail_bytes = cls._thumbnail_bytes(thumbnail)
        if len(thumbnail_bytes) > cls.MAX_THUMBNAIL_BYTES:
            raise ProjectError("Project thumbnail is unexpectedly large.")
        required_bytes = sum(path.stat().st_size for path in assets)
        available_bytes = shutil.disk_usage(target.parent).free
        if required_bytes + len(payload) + len(thumbnail_bytes) > max(0, available_bytes - 512 * 1024 * 1024):
            raise OSError("Not enough free disk space to package project content safely.")
        with NamedTemporaryFile(suffix=".tmp", dir=target.parent, delete=False) as temp:
            temporary_path = Path(temp.name)
        try:
            with zipfile.ZipFile(
                temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
            ) as archive:
                archive.writestr(cls.MANIFEST_NAME, payload)
                for source_path, archive_path in assets.items():
                    # Most media is already compressed. Storing it directly avoids a
                    # long UI-blocking recompression pass and preserves byte quality.
                    archive.write(
                        source_path, archive_path, compress_type=zipfile.ZIP_STORED
                    )
                if thumbnail_bytes:
                    archive.writestr(cls.THUMBNAIL_NAME, thumbnail_bytes)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return target

    @classmethod
    def _lease_cache_root(cls, root: Path) -> Path:
        """Reuse the shared cache, separating only editors that overlap in time."""
        root.parent.mkdir(parents=True, exist_ok=True)
        candidates = (root, root.with_name(f"{root.name}-{os.getpid()}"))
        with cls._cache_lock_guard:
            for candidate in candidates:
                if candidate in cls._cache_locks:
                    return candidate
            for candidate in candidates:
                lease = QLockFile(str(candidate.with_suffix(".lock")))
                lease.setStaleLockTime(0)
                if lease.tryLock(0):
                    cls._cache_locks[candidate] = lease
                    return candidate
        raise OSError("The project media cache is being cleaned. Try opening the project again.")

    @classmethod
    def _load_package(
        cls, target: Path,
        progress: LoadProgress | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProjectDocument:
        fingerprint = sha256(
            f"{target}:{target.stat().st_mtime_ns}:{target.stat().st_size}".encode("utf-8")
        ).hexdigest()[:20]
        cache_root = cls._lease_cache_root(Path(gettempdir()) / "PlaylistCanvas" / "project-cache" / fingerprint)
        cls.cleanup_cache(keep={cache_root.name})
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_root.touch()
        with zipfile.ZipFile(target, "r") as archive:
            manifest_info = archive.getinfo(cls.MANIFEST_NAME)
            if manifest_info.file_size > cls.MAX_MANIFEST_BYTES:
                raise ValueError("Project manifest is unexpectedly large.")
            asset_infos = [
                info for info in archive.infolist()
                if not info.is_dir() and PurePosixPath(info.filename).parts[:1] == ("assets",)
            ]
            if len(asset_infos) > cls.MAX_ASSETS:
                raise ValueError("Project package contains too many assets.")
            data = json.loads(archive.read(cls.MANIFEST_NAME).decode("utf-8"))
            extracted_assets: dict[str, Path] = {}
            extracted_destinations: set[str] = set()
            reusable: set[str] = set()
            required_bytes = sum(info.file_size for info in asset_infos)
            if progress is not None:
                progress(0, required_bytes)
            for info in asset_infos:
                parts = PurePosixPath(info.filename).parts
                if info.is_dir() or not parts or parts[0] != "assets":
                    continue
                if any(part in {"", ".", ".."} for part in parts):
                    raise ValueError("Unsafe asset path in project package.")
                archive_path = PurePosixPath(info.filename).as_posix()
                local_path = cls._asset_archive_path(archive_path, parts[-1])
                destination = cache_root.joinpath(*PurePosixPath(local_path).parts)
                # Duplicate or case-only-different entries map to the same file
                # on Windows; the second would silently overwrite the first.
                destination_key = str(destination).casefold()
                if archive_path in extracted_assets or destination_key in extracted_destinations:
                    raise ValueError("Duplicate asset entry in project package.")
                extracted_destinations.add(destination_key)
                extracted_assets[archive_path] = destination
                if cls._cached_asset_matches(destination, info, cancel_event):
                    reusable.add(archive_path)
            missing_bytes = sum(info.file_size for info in asset_infos
                                if PurePosixPath(info.filename).as_posix() not in reusable)
            if missing_bytes > max(0, shutil.disk_usage(cache_root).free - 512 * 1024 * 1024):
                raise OSError("Not enough temporary disk space to open this project safely.")
            extracted_bytes = 0
            for info in asset_infos:
                archive_path = PurePosixPath(info.filename).as_posix()
                destination = extracted_assets[archive_path]
                if cancel_event is not None and cancel_event.is_set():
                    raise ProjectLoadCancelled("Project loading was cancelled.")
                if archive_path in reusable:
                    extracted_bytes += info.file_size
                    if progress is not None:
                        progress(extracted_bytes, required_bytes)
                    if cancel_event is not None and cancel_event.is_set():
                        raise ProjectLoadCancelled("Project loading was cancelled.")
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary_asset = None
                try:
                    with archive.open(info, "r") as source, NamedTemporaryFile(
                        dir=destination.parent, suffix=".tmp", delete=False,
                    ) as output:
                        temporary_asset = Path(output.name)
                        if cancel_event is not None and cancel_event.is_set():
                            raise ProjectLoadCancelled("Project loading was cancelled.")
                        while chunk := source.read(_EXTRACT_CHUNK_BYTES):
                            if cancel_event is not None and cancel_event.is_set():
                                raise ProjectLoadCancelled("Project loading was cancelled.")
                            output.write(chunk)
                            extracted_bytes += len(chunk)
                            if progress is not None:
                                progress(extracted_bytes, required_bytes)
                    if cancel_event is not None and cancel_event.is_set():
                        raise ProjectLoadCancelled("Project loading was cancelled.")
                    temporary_asset.replace(destination)
                finally:
                    if temporary_asset is not None:
                        temporary_asset.unlink(missing_ok=True)
            thumbnail_cache = cache_root / cls.THUMBNAIL_NAME
            if cls.THUMBNAIL_NAME in archive.namelist():
                if archive.getinfo(cls.THUMBNAIL_NAME).file_size > cls.MAX_THUMBNAIL_BYTES:
                    raise ValueError("Project thumbnail is unexpectedly large.")
                thumbnail_cache.write_bytes(archive.read(cls.THUMBNAIL_NAME))
        document = ProjectDocument.from_dict(data)

        def resolved(raw_path: str) -> str:
            return cls._resolve_package_asset(raw_path, extracted_assets)

        cls._map_media_paths(document, resolved)
        if (document.settings.thumbnail_mode == "custom"
                and thumbnail_cache.is_file()):
            document.settings.thumbnail_path = str(thumbnail_cache.resolve())
        return document

    @staticmethod
    def _cached_asset_matches(
        path: Path, info: zipfile.ZipInfo, cancel_event: threading.Event | None,
    ) -> bool:
        """Verify cached bytes against the archive before skipping extraction."""
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size != info.file_size:
                return False
            checksum = 0
            with path.open("rb") as stream:
                while chunk := stream.read(_EXTRACT_CHUNK_BYTES):
                    if cancel_event is not None and cancel_event.is_set():
                        raise ProjectLoadCancelled("Project loading was cancelled.")
                    checksum = zlib.crc32(chunk, checksum)
            return checksum == info.CRC
        except OSError:
            return False

    @staticmethod
    def _map_media_paths(document: ProjectDocument, resolve: Callable[[str], str]) -> None:
        for source in document.sources:
            source.content_path = resolve(source.content_path)
            source.font_path = resolve(source.font_path)
            source.video_paths = [resolve(path) for path in source.video_paths]
        for track in document.playlist:
            track.file_path = resolve(track.file_path)
            track.lyrics_path = resolve(track.lyrics_path)
            track.cover_path = resolve(track.cover_path)
            track.video_paths = [resolve(path) for path in track.video_paths]
        for content in document.content_library:
            content.path = resolve(content.path)
        document.settings.thumbnail_path = resolve(document.settings.thumbnail_path)

    @classmethod
    def restore_recovery_media(
        cls, document: ProjectDocument, project_path: Path | None,
        load: Callable[[Path], ProjectDocument],
    ) -> None:
        """Rehydrate missing extracted media without replacing recovered edits."""
        if project_path is None or project_path.suffix.lower() != cls.PACKAGE_SUFFIX or not project_path.is_file():
            return
        missing: set[str] = set()

        def cache_key(raw: str) -> str | None:
            path = Path(raw)
            directory = path.parent.parent if path.parent.name == "assets" else path.parent
            if (directory.parent.name == "project-cache"
                    and directory.parent.parent.name in {"PlaylistCanvas", "PlaylistVideoStudio"}):
                return path.name.casefold()
            return None

        def collect(raw: str) -> str:
            if raw and cache_key(raw) is not None and not Path(raw).is_file():
                missing.add(raw)
            return raw

        cls._map_media_paths(document, collect)
        if not missing:
            return
        packaged = load(project_path)
        replacements: dict[str, str] = {}

        def index(raw: str) -> str:
            key = cache_key(raw)
            if key is not None and Path(raw).is_file():
                replacements[key] = raw
            return raw

        cls._map_media_paths(packaged, index)
        cls._map_media_paths(document, lambda raw: replacements.get(cache_key(raw), raw) if raw in missing else raw)

    @staticmethod
    def _resolve_package_asset(raw_path: str, extracted_assets: dict[str, Path]) -> str:
        """Map a manifest path to its extracted file; external paths pass through.

        An internal ``assets/...`` reference is only honoured when it is the
        exact archive path of an entry that was validated and extracted. Anything
        else that claims to be internal (``..``, backslashes, missing entries) is
        rejected instead of being resolved against the cache folder, where it
        could point outside of it.
        """
        if not raw_path:
            return ""
        if raw_path in extracted_assets:
            return str(extracted_assets[raw_path].resolve())
        if raw_path.replace("\\", "/").split("/", 1)[0].casefold() == "assets":
            raise ValueError(f"Project package references a missing or unsafe asset: {raw_path!r}")
        return raw_path

    @classmethod
    def release_cache_locks(cls) -> None:
        """Release process-owned cache leases before shutdown cleanup runs."""
        with cls._cache_lock_guard:
            for lease in cls._cache_locks.values():
                lease.unlock()
            cls._cache_locks.clear()

    @classmethod
    def cleanup_cache(cls, max_age_days: int = 30,
                      keep: set[str] | None = None) -> None:
        """Remove stale extracted project packages from the application temp cache."""
        cutoff = time.time() - max(1, max_age_days) * 86_400
        retained = keep or set()
        roots = (
            Path(gettempdir()) / "PlaylistCanvas" / "project-cache",
            Path(gettempdir()) / "PlaylistVideoStudio" / "project-cache",
        )
        for root in roots:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.name in retained or not entry.is_dir():
                    continue
                lease = QLockFile(str(entry.with_suffix(".lock")))
                lease.setStaleLockTime(0)
                try:
                    if entry.stat().st_mtime < cutoff and lease.tryLock(0):
                        shutil.rmtree(entry)
                except OSError:
                    # A cache may still be in use by another running editor window.
                    continue
                finally:
                    lease.unlock()

    @staticmethod
    def _thumbnail_bytes(image: QImage | None) -> bytes:
        if image is None or image.isNull():
            return b""
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        buffer.close()
        return bytes(data)


atexit.register(ProjectService.release_cache_locks)
