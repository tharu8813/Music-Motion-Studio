"""User-defined design presets stored as portable JSON files."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from app.models.source import Source
from app.presets.preset_service import PresetDefinition, PresetService

PRESET_SUFFIX = ".pcpreset.json"
_FORMAT = "playlist-canvas-preset"
_ID_PREFIX = "user:"


class UserPresetError(Exception):
    """Raised when a preset file cannot be read, written, or validated."""


def preset_directory() -> Path:
    """Return the writable folder that holds user preset files."""
    override = os.environ.get("PLAYLIST_CANVAS_PRESET_DIR")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "PlaylistCanvas" / "presets"


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w-]+", "-", text.strip().lower()).strip("-")
    return slug or "preset"


def _definition_from_payload(identifier: str, payload: dict) -> PresetDefinition:
    name = (str(payload.get("name") or "").strip() or "Untitled preset")
    width = float(payload.get("source_width") or 1280.0) or 1280.0
    height = float(payload.get("source_height") or 720.0) or 720.0
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise UserPresetError("Preset file contains no canvas sources.")
    # Validate eagerly so a corrupt file is skipped at load time, never at apply.
    for entry in raw_sources:
        if not isinstance(entry, dict):
            raise UserPresetError("Preset source entries must be objects.")
        Source.from_dict(dict(entry))

    def builder(entries: list = raw_sources) -> list[Source]:
        sources: list[Source] = []
        for entry in entries:
            data = dict(entry)
            data.pop("id", None)  # fresh ids so repeated applies do not collide
            sources.append(Source.from_dict(data))
        return sources

    return PresetDefinition(
        identifier, name, name,
        "저장한 사용자 프리셋입니다.", "A preset you saved.",
        builder, source_width=width, source_height=height, editable=True,
    )


class UserPresetService:
    """List, save, delete, import, and export user design presets."""

    @staticmethod
    def directory() -> Path:
        return preset_directory()

    @classmethod
    def all(cls) -> list[PresetDefinition]:
        """Return every valid user preset, newest last, skipping unreadable files."""
        directory = cls.directory()
        if not directory.is_dir():
            return []
        presets: list[PresetDefinition] = []
        for path in sorted(directory.glob(f"*{PRESET_SUFFIX}"),
                           key=lambda item: item.stat().st_mtime):
            try:
                payload = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("format") != _FORMAT:
                continue
            stem = path.name[: -len(PRESET_SUFFIX)]
            try:
                presets.append(_definition_from_payload(_ID_PREFIX + stem, payload))
            except (UserPresetError, ValueError):
                continue
        return presets

    @classmethod
    def save(cls, name: str, sources: Iterable[Source],
             source_width: float, source_height: float) -> PresetDefinition:
        """Write the given sources to a new preset file and return its definition."""
        cleaned = name.strip() or "Untitled preset"
        source_list = list(sources)
        if not source_list:
            raise UserPresetError("Cannot save a preset with no canvas sources.")
        directory = cls.directory()
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{_slug(cleaned)}-{uuid.uuid4().hex[:8]}"
        payload = {
            "format": _FORMAT,
            "version": 1,
            "name": cleaned,
            "source_width": float(source_width) or 1280.0,
            "source_height": float(source_height) or 720.0,
            "created": datetime.now(timezone.utc).isoformat(),
            "sources": [source.to_dict() for source in source_list],
        }
        (directory / f"{stem}{PRESET_SUFFIX}").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), "utf-8",
        )
        return _definition_from_payload(_ID_PREFIX + stem, payload)

    @classmethod
    def delete(cls, identifier: str) -> bool:
        path = cls._path_for(identifier)
        if path is not None and path.is_file():
            path.unlink()
            return True
        return False

    @classmethod
    def export_to(cls, identifier: str, destination: str | Path) -> None:
        path = cls._path_for(identifier)
        if path is None or not path.is_file():
            raise UserPresetError("Preset file not found.")
        Path(destination).write_text(path.read_text("utf-8"), "utf-8")

    @classmethod
    def import_from(cls, source_path: str | Path) -> PresetDefinition:
        try:
            payload = json.loads(Path(source_path).read_text("utf-8"))
        except (OSError, ValueError) as error:
            raise UserPresetError("The file could not be read as a preset.") from error
        if not isinstance(payload, dict) or payload.get("format") != _FORMAT:
            raise UserPresetError("This is not a Music Motion Studio preset file.")
        name = (str(payload.get("name") or "").strip()
                or Path(source_path).stem or "Imported preset")
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise UserPresetError("Preset file contains no canvas sources.")
        sources = [Source.from_dict(dict(entry)) for entry in raw_sources]
        return cls.save(
            name, sources,
            float(payload.get("source_width") or 1280.0),
            float(payload.get("source_height") or 720.0),
        )

    @classmethod
    def _path_for(cls, identifier: str) -> Path | None:
        if not identifier.startswith(_ID_PREFIX):
            return None
        stem = identifier[len(_ID_PREFIX):]
        if not stem or "/" in stem or "\\" in stem or ".." in stem:
            return None
        return cls.directory() / f"{stem}{PRESET_SUFFIX}"


def all_presets() -> list[PresetDefinition]:
    """Return built-in presets followed by the user's saved presets."""
    return [*PresetService.all(), *UserPresetService.all()]
