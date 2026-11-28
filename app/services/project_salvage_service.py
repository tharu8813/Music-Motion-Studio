"""Explicit, non-destructive salvage of readable project package entries.

Normal project loading remains strict. Salvage writes into a new user-selected
directory, never the original package or the temporary playback cache.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import shutil
import threading
import zipfile
import zlib

from app.models.project import ProjectDocument
from app.services.project_service import ProjectError, ProjectLoadCancelled, ProjectService


@dataclass(frozen=True, slots=True)
class SalvageProblem:
    entry: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProjectSalvageResult:
    directory: Path
    project_path: Path | None
    recovered_assets: tuple[Path, ...]
    problems: tuple[SalvageProblem, ...]


class ProjectSalvageService:
    """Recover independently readable entries without weakening the normal loader."""

    CHUNK_BYTES = 1024 * 1024
    DISK_RESERVE = 512 * 1024 * 1024

    @staticmethod
    def _check_cancel(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ProjectLoadCancelled("Project salvage was cancelled.")

    @staticmethod
    def _safe_internal_name(name: str) -> bool:
        # Archive names are never used directly as output paths.
        parts = name.split("/")
        return (len(parts) >= 2 and parts[0] == "assets"
                and not any(part in {"", ".", ".."} for part in parts)
                and "\\" not in name and ":" not in name and "\x00" not in name)

    @classmethod
    def salvage(
        cls, source: str | Path, directory: str | Path, *,
        cancel_event: threading.Event | None = None,
    ) -> ProjectSalvageResult:
        """Export CRC-valid assets and, when possible, a new reference-mode project.

        The new directory must not already exist. A valid manifest is required
        for a project copy, but not for extracting independently readable media.
        Missing assets remain explicit missing-file references for later relinking.
        """
        root: Path | None = None
        try:
            source_path = ProjectService._project_file(source)
            before = ProjectService.revision(source_path)
            if before is None:
                raise ProjectError("Project package does not exist.")
            cls._check_cancel(cancel_event)
            output = Path(directory).expanduser().resolve()
            output.mkdir(parents=False, exist_ok=False)
            root = output
            result = cls._salvage_package(source_path, root, cancel_event)
            cls._check_cancel(cancel_event)
            if ProjectService.revision(source_path) != before:
                raise ProjectError("Project package changed during salvage. Retry with a stable copy.")
            return result
        except (OSError, ValueError, TypeError, RuntimeError, zipfile.BadZipFile) as error:
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
            raise ProjectError(f"Could not salvage project: {error}") from error
        except BaseException:
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
            raise

    @classmethod
    def _salvage_package(
        cls, source: Path, root: Path, cancel_event: threading.Event | None,
    ) -> ProjectSalvageResult:
        problems: list[SalvageProblem] = []
        recovered: dict[str, Path] = {}
        document: ProjectDocument | None = None
        project_path: Path | None = None
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            names = Counter(info.filename.casefold() for info in infos)
            assets = [info for info in infos if not info.is_dir() and
                      info.filename.replace("\\", "/").split("/", 1)[0].casefold() == "assets"]
            if len(assets) > ProjectService.MAX_ASSETS:
                raise ProjectError("Project package contains too many assets.")
            cls._check_cancel(cancel_event)
            try:
                if names[ProjectService.MANIFEST_NAME.casefold()] != 1:
                    raise ValueError("Missing or ambiguous project manifest.")
                info = archive.getinfo(ProjectService.MANIFEST_NAME)
                if info.file_size > ProjectService.MAX_MANIFEST_BYTES:
                    raise ValueError("Project manifest exceeds the size limit.")
                document = ProjectDocument.from_dict(json.loads(
                    archive.read(info).decode("utf-8-sig")
                ))
            except (KeyError, TypeError, ValueError, RecursionError, OverflowError,
                    zipfile.BadZipFile, RuntimeError, zlib.error, EOFError, NotImplementedError) as error:
                problems.append(SalvageProblem(ProjectService.MANIFEST_NAME, str(error)))

            available = max(0, shutil.disk_usage(root).free - cls.DISK_RESERVE)
            if sum(info.file_size for info in assets) > available:
                raise ProjectError("Not enough disk space to salvage project assets safely.")
            asset_root = root / "assets"
            asset_root.mkdir()
            for info in assets:
                cls._check_cancel(cancel_event)
                name = info.filename
                if not cls._safe_internal_name(name) or names[name.casefold()] != 1:
                    problems.append(SalvageProblem(name, "Unsafe or duplicate asset entry."))
                    continue
                # Hash plus bounded basename keeps extraction inside the new folder.
                destination = root / ProjectService._asset_archive_path(name, PurePosixPath(name).name)
                if destination.exists():
                    problems.append(SalvageProblem(name, "Asset output name collision."))
                    continue
                try:
                    with archive.open(info, "r") as stream, destination.open("xb") as output:
                        remaining = info.file_size
                        while chunk := stream.read(cls.CHUNK_BYTES):
                            cls._check_cancel(cancel_event)
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise zipfile.BadZipFile("Asset exceeds declared size.")
                            output.write(chunk)
                        if remaining:
                            raise zipfile.BadZipFile("Asset is shorter than declared size.")
                except (zipfile.BadZipFile, RuntimeError, zlib.error, EOFError, NotImplementedError) as error:
                    destination.unlink(missing_ok=True)
                    problems.append(SalvageProblem(name, str(error)))
                    continue
                recovered[name] = destination

            thumbnail: Path | None = None
            if names[ProjectService.THUMBNAIL_NAME.casefold()]:
                try:
                    if names[ProjectService.THUMBNAIL_NAME.casefold()] != 1:
                        raise ValueError("Duplicate thumbnail.")
                    info = archive.getinfo(ProjectService.THUMBNAIL_NAME)
                    if info.file_size > ProjectService.MAX_THUMBNAIL_BYTES:
                        raise ValueError("Thumbnail exceeds the size limit.")
                    data = archive.read(info)
                    thumbnail = root / ProjectService.THUMBNAIL_NAME
                    thumbnail.write_bytes(data)
                except (KeyError, ValueError, zipfile.BadZipFile, RuntimeError,
                        zlib.error, EOFError, NotImplementedError) as error:
                    problems.append(SalvageProblem(ProjectService.THUMBNAIL_NAME, str(error)))

        cls._check_cancel(cancel_event)
        if document is not None:
            missing: set[str] = set()

            def resolve(raw: str) -> str:
                if not raw:
                    return raw
                if raw in recovered:
                    return str(recovered[raw])
                if raw.replace("\\", "/").split("/", 1)[0].casefold() == "assets":
                    if raw not in missing:
                        missing.add(raw)
                        problems.append(SalvageProblem(raw, "Referenced asset was not recovered."))
                    # A safe, absent path, not an archive-controlled traversal.
                    return str(root / "missing" / ProjectService._asset_archive_path(
                        raw, PurePosixPath(raw.replace("\\", "/")).name
                    ))
                path = Path(raw).expanduser()
                return str(path if path.is_absolute() else source.parent / path)

            ProjectService._map_media_paths(document, resolve)
            document.settings.content_mode = "reference"
            if document.settings.thumbnail_mode == "custom" and thumbnail is not None:
                document.settings.thumbnail_path = str(thumbnail)
            project_path = ProjectService.save(
                root / "recovered.pvsproj", document, expected_revision=None,
            )

        report = {
            "source": str(source),
            "project": str(project_path) if project_path else None,
            "recovered_assets": {name: str(path) for name, path in recovered.items()},
            "problems": [{"entry": item.entry, "reason": item.reason} for item in problems],
            "content_mode": "reference" if project_path else None,
        }
        (root / "recovery-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return ProjectSalvageResult(root, project_path, tuple(recovered.values()), tuple(problems))
