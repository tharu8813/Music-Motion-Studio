"""Per-project export defaults, durable even before the project is saved again."""

from __future__ import annotations

from dataclasses import fields, replace
from hashlib import sha256
import json
from typing import Any

from PySide6.QtCore import QSettings

from app.models.project import ProjectSettings
from app.services.app_settings_service import AppSettings

# Do not persist UI-only render dimensions: each project derives those from its canvas.
_RENDER_FIELDS = (
    "output_directory", "resolution_name", "fps", "video_codec", "crf",
    "preset", "audio_bitrate", "native_nvenc_cq", "native_nvenc_preset",
)
_EXPORT_FIELDS = ("engine", "kind", "format")


def _storage_key(project: ProjectSettings) -> str:
    """Use stable project creation identity, never the mutable title or filename."""
    identity = project.created_at
    return "project_exports/" + sha256(identity.encode("utf-8")).hexdigest()


def load_export_preferences(project: ProjectSettings) -> dict[str, Any]:
    """Prefer this machine's latest export over the last packaged snapshot."""
    if not isinstance(project, ProjectSettings):
        return {}
    stored = QSettings().value(_storage_key(project), "")
    if isinstance(stored, str) and stored:
        try:
            decoded = json.loads(stored)
            if isinstance(decoded, dict):
                return decoded
        except (ValueError, TypeError):
            pass
    return dict(project.export_preferences)


def settings_for_project(
    defaults: AppSettings, project: ProjectSettings,
) -> tuple[AppSettings, dict[str, Any]]:
    """A project's first export uses app settings; later exports use its own."""
    preference = load_export_preferences(project)
    allowed = {field.name for field in fields(AppSettings)}
    updates = {
        key: preference[key]
        for key in _RENDER_FIELDS
        if key in preference and key in allowed
        and isinstance(preference[key], type(getattr(defaults, key)))
        and not isinstance(preference[key], bool)
    }
    return replace(defaults, **updates), preference


def remember_export_preferences(
    project: ProjectSettings, settings: AppSettings,
    *, engine: str, kind: str, file_format: str,
) -> None:
    """Store confirmed export values for this project, not global defaults."""
    if not isinstance(project, ProjectSettings):
        return
    data: dict[str, Any] = {
        key: getattr(settings, key) for key in _RENDER_FIELDS
    }
    data.update(engine=engine, kind=kind, format=file_format)
    project.export_preferences = data
    QSettings().setValue(_storage_key(project), json.dumps(data, ensure_ascii=False))
