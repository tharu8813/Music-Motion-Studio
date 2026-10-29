"""Serializable document model for a Music Motion Studio project."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Any
from uuid import uuid4

from app import __version__
from app.automix.overrides import parse_overrides, serialize_overrides
from app.models.playlist import PlaylistTrack
from app.models.layer import LayerGroup
from app.models.source import Source
from app.models.source_registry import source_registry
from app.timeline.models import Timeline, timeline_from_playlist


@dataclass(slots=True)
class CanvasSettings:
    """Persisted canvas state relevant to Phase 1 editing."""

    width: float = 1280.0
    height: float = 720.0
    show_grid: bool = True
    snap_enabled: bool = True
    zoom: float = 1.0


TRANSITION_MODES = ("none", "crossfade", "automix")
DEFAULT_CROSSFADE_SECONDS = 3.0
MIN_CROSSFADE_SECONDS = 0.5
MAX_CROSSFADE_SECONDS = 30.0


@dataclass(slots=True)
class ProjectSettings:
    """Project identity and portable-content policy."""

    title: str = "Untitled Project"
    description: str = ""
    author: str = ""
    content_mode: str = "embed"
    thumbnail_mode: str = "canvas"
    thumbnail_path: str = ""
    # Per-project, not per-user: how export blends between tracks.
    # "none" (legacy instant cut) is the default so existing projects keep
    # their exact legacy sequential audio.
    transition_mode: str = "none"
    # Only meaningful when transition_mode == "crossfade": how many seconds
    # before each track ends the next one starts fading in.
    crossfade_seconds: float = DEFAULT_CROSSFADE_SECONDS
    # Only meaningful when transition_mode == "automix": junctions the user
    # set by hand, keyed "<outgoing track id>><incoming track id>" (see
    # app.automix.overrides). Every other junction is automatic.
    automix_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    modified_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        if self.content_mode not in {"embed", "reference"}:
            self.content_mode = "embed"
        if self.thumbnail_mode not in {"canvas", "custom"}:
            self.thumbnail_mode = "canvas"
        if self.transition_mode not in TRANSITION_MODES:
            self.transition_mode = "none"
        if (not isinstance(self.crossfade_seconds, (int, float))
                or isinstance(self.crossfade_seconds, bool)
                or not isfinite(float(self.crossfade_seconds))):
            self.crossfade_seconds = DEFAULT_CROSSFADE_SECONDS
        else:
            self.crossfade_seconds = max(
                MIN_CROSSFADE_SECONDS, min(MAX_CROSSFADE_SECONDS, float(self.crossfade_seconds)),
            )
        # A broken entry (hand-edited file, older build) drops back to automatic.
        self.automix_overrides = serialize_overrides(parse_overrides(self.automix_overrides))


@dataclass(slots=True)
class ProjectContent:
    """Reusable content registered in one project's content library."""

    path: str
    media_type: str
    name: str = ""
    id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if not self.name:
            from pathlib import Path
            self.name = Path(self.path).stem


SUPPORTED_VERSIONS = (1, 2)
CURRENT_VERSION = 2


def migrate_project_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Bring a saved project of any supported version to the current layout.

    Pure: returns a new dict and never mutates ``data``. Only renamed/removed
    fields are handled here; ``ProjectDocument.from_dict`` validates the
    result. v1 and v2 share one layout, and the settings fields below were
    retired while files were already written as v2, so these steps run for
    every version. A future layout change bumps CURRENT_VERSION and adds a
    step keyed on the incoming version here.
    """
    if not isinstance(data, dict):
        raise ValueError("Project root must be an object.")
    raw_version = data.get("version", 1)
    if isinstance(raw_version, bool) or not isinstance(raw_version, (int, str)):
        raise ValueError("Project version must be an integer.")
    try:
        version = int(raw_version)
    except ValueError as error:
        raise ValueError("Project version must be an integer.") from error
    if version not in SUPPORTED_VERSIONS:
        raise ValueError("This project version is not supported.")
    migrated = {**data, "version": CURRENT_VERSION}
    settings = data.get("settings")
    if isinstance(settings, dict):
        settings = dict(settings)
        if "transition_mode" not in settings and "automix_enabled" in settings:
            # Before transition_mode, AutoMix was a plain on/off switch.
            settings["transition_mode"] = "automix" if settings.get("automix_enabled") else "none"
        settings.pop("automix_enabled", None)
        # AutoMix's style is always automatic now; a saved listening preset is dropped.
        settings.pop("automix_preset", None)
        migrated["settings"] = settings
    return migrated


@dataclass(slots=True)
class ProjectDocument:
    """Complete Music Motion Studio project document."""

    sources: list[Source] = field(default_factory=list)
    groups: list[LayerGroup] = field(default_factory=list)
    playlist: list[PlaylistTrack] = field(default_factory=list)
    canvas: CanvasSettings = field(default_factory=CanvasSettings)
    theme: str = "dark"
    language: str = "ko"
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    content_library: list[ProjectContent] = field(default_factory=list)
    version: int = CURRENT_VERSION
    app_version: str = __version__

    @property
    def timeline(self) -> Timeline:
        """Derive the legacy editing timeline without persisting duplicate state."""
        return timeline_from_playlist(self.playlist)

    def to_dict(self) -> dict[str, Any]:
        """Convert the document into JSON-compatible data."""
        return {
            "version": self.version,
            "app_version": self.app_version,
            "canvas": asdict(self.canvas),
            "theme": self.theme,
            "language": self.language,
            "settings": asdict(self.settings),
            "content_library": [asdict(item) for item in self.content_library],
            "sources": [source_registry.serialize(source) for source in self.sources],
            "groups": [group.to_dict() for group in self.groups],
            "playlist": [track.to_dict() for track in self.playlist],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectDocument":
        """Create a validated document from parsed JSON data.

        Policy: a value read from a file that is out of range is rejected
        here (the file is damaged or from an unknown build), while
        ``ProjectSettings.__post_init__`` clamps values the app itself
        constructs. ``theme``/``language`` are legacy per-project metadata:
        still validated and kept for round trips, but the app-wide
        preference decides the UI.
        """
        data = migrate_project_payload(data)
        app_version = data.get("app_version", "")
        if not isinstance(app_version, str) or len(app_version) > 64:
            raise ValueError("Project application version must be a short string.")
        sources = data.get("sources", [])
        groups = data.get("groups", [])
        playlist = data.get("playlist", [])
        canvas = data.get("canvas", {})
        content_library = data.get("content_library", [])
        if not all(isinstance(entries, list) for entries in (
            sources, groups, playlist, content_library,
        )):
            raise ValueError("Project collections must be arrays.")
        if not all(isinstance(entry, dict) for entry in sources):
            raise ValueError("Project sources must be objects.")
        if not all(isinstance(entry, dict) for entry in groups):
            raise ValueError("Project groups must be objects.")
        if not all(isinstance(entry, dict) for entry in playlist):
            raise ValueError("Project playlist tracks must be objects.")
        if not isinstance(canvas, dict):
            raise ValueError("Project canvas settings must be an object.")
        if not all(isinstance(entry, dict) for entry in content_library):
            raise ValueError("Project content entries must be objects.")
        if len(sources) > 20_000 or len(groups) > 20_000 or len(playlist) > 20_000:
            raise ValueError("The project contains too many sources, groups, or tracks.")
        settings_data = data.get("settings", {})
        if not isinstance(settings_data, dict):
            settings_data = {}
        canvas_model = CanvasSettings(**canvas)
        for name in ("width", "height", "zoom"):
            value = getattr(canvas_model, name)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not isfinite(float(value))):
                raise ValueError(f"Canvas '{name}' must be a finite number.")
        if not 64 <= canvas_model.width <= 16_384 or not 64 <= canvas_model.height <= 16_384:
            raise ValueError("Canvas dimensions must be between 64 and 16384 pixels.")
        if not 0.05 <= canvas_model.zoom <= 20:
            raise ValueError("Canvas zoom must be between 0.05 and 20.")
        if not isinstance(canvas_model.show_grid, bool) or not isinstance(canvas_model.snap_enabled, bool):
            raise ValueError("Canvas grid and snap settings must be booleans.")

        if settings_data.get("content_mode", "embed") not in {"embed", "reference"}:
            raise ValueError("Project content mode must be 'embed' or 'reference'.")
        if settings_data.get("thumbnail_mode", "canvas") not in {"canvas", "custom"}:
            raise ValueError("Project thumbnail mode must be 'canvas' or 'custom'.")
        if settings_data.get("transition_mode", "none") not in TRANSITION_MODES:
            raise ValueError("Project transition mode must be 'none', 'crossfade', or 'automix'.")
        crossfade_value = settings_data.get("crossfade_seconds", DEFAULT_CROSSFADE_SECONDS)
        if (not isinstance(crossfade_value, (int, float)) or isinstance(crossfade_value, bool)
                or not isfinite(float(crossfade_value))):
            raise ValueError("Project crossfade_seconds must be a finite number.")
        settings_model = ProjectSettings(**settings_data)
        if not all(isinstance(getattr(settings_model, name), str) for name in (
            "title", "description", "author", "thumbnail_path", "created_at", "modified_at",
        )):
            raise ValueError("Project identity and timestamp fields must be strings.")

        source_models = [source_registry.deserialize(entry) for entry in sources]
        group_models = [LayerGroup.from_dict(entry) for entry in groups]
        track_models = [PlaylistTrack.from_dict(entry) for entry in playlist]
        content_models = [ProjectContent(**entry) for entry in content_library]

        def require_unique_ids(items: list[object], label: str) -> set[str]:
            identifiers: list[str] = []
            for item in items:
                identifier = getattr(item, "id", None)
                if not isinstance(identifier, str) or not identifier.strip():
                    raise ValueError(f"Every {label} must have a non-empty string ID.")
                identifiers.append(identifier)
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"Project {label} IDs must be unique.")
            return set(identifiers)

        source_ids = require_unique_ids(source_models, "source")
        group_ids = require_unique_ids(group_models, "group")
        require_unique_ids(track_models, "playlist track")
        require_unique_ids(content_models, "content item")
        if len(source_ids) != len(source_models):
            raise ValueError("Project source IDs must be unique.")
        for source in source_models:
            if source.group_id is not None and source.group_id not in group_ids:
                raise ValueError(f"Source '{source.name}' references an unknown group.")
        for content in content_models:
            if not isinstance(content.path, str) or not isinstance(content.name, str):
                raise ValueError("Project content paths and names must be strings.")
            if content.media_type not in {"audio", "video", "image", "font", "lyrics"}:
                raise ValueError("Project content media type is not supported.")

        theme = data.get("theme", "dark")
        language = data.get("language", "ko")
        if theme not in {"light", "dark", "auto"}:
            raise ValueError("Project theme is not supported.")
        if language not in {"ko", "en"}:
            raise ValueError("Project language is not supported.")

        return cls(
            version=CURRENT_VERSION,
            app_version=app_version,
            sources=source_models,
            groups=group_models,
            playlist=track_models,
            canvas=canvas_model,
            theme=theme,
            language=language,
            settings=settings_model,
            content_library=content_models,
        )
