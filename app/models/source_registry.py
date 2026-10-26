"""Source definitions shared by the model and UI adapter layers.

The core registers typed components and the legacy Source factory/serializer. Canvas and Inspector
bind their legacy callbacks when imported, so model-only use never imports Qt.
Type-specific implementations can replace these bindings in later phases.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from app.models.source import Source, SourceType
from app.models.source_components import (
    AlbumCoverComponent, BackgroundComponent, ImageComponent, LevelMeterComponent,
    LyricsComponent, NowPlayingComponent, ParticleComponent, ProgressComponent,
    ShapeComponent, SourceComponent, TextComponent, TimeComponent,
    TrackListComponent, VideoComponent, VisualizerComponent, WaveformComponent,
)


@dataclass(slots=True)
class SourceDefinition:
    """One source type's model, codec and optional UI adapter callbacks."""

    type: SourceType
    component: type[SourceComponent]
    source_factory: type[Source] = Source
    serializer: type[Source] = Source
    renderer: Callable[..., None] | None = None
    inspector: Callable[..., None] | None = None


class SourceRegistry:
    """Explicit lookup; unknown types and duplicate registrations are errors."""

    def __init__(self) -> None:
        self._definitions: dict[SourceType, SourceDefinition] = {}

    def register(self, definition: SourceDefinition) -> None:
        if not isinstance(definition.type, SourceType):
            raise ValueError("Source definitions require a supported SourceType.")
        if not isinstance(definition.component, type) or not issubclass(definition.component, SourceComponent):
            raise ValueError("Source definitions require a SourceComponent class.")
        if definition.type in self._definitions:
            raise ValueError(f"Source type is already registered: {definition.type.value}")
        self._definitions[definition.type] = definition

    def get(self, source_type: SourceType | str) -> SourceDefinition:
        try:
            return self._definitions[SourceType(source_type)]
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError(f"Unregistered source type: {source_type!r}") from error

    def serialize(self, source: Source) -> dict[str, Any]:
        return self.get(source.source_type).serializer.to_dict(source)

    def deserialize(self, data: dict[str, Any]) -> Source:
        if not isinstance(data, dict):
            raise ValueError("Project sources must be objects.")
        return self.get(data.get("source_type")).serializer.from_dict(data)

    def component_for(self, source: Source) -> SourceComponent:
        """Read a fresh, detached snapshot of this source's specific properties."""
        return self.get(source.source_type).component.from_source(source)

    def component_changes(self, source: Source, **changes: object) -> dict[str, object]:
        """Validate an edit without mutating the source or normalizing unrelated fields."""
        component = replace(self.component_for(source), **changes)
        legacy = {
            name: value for name, value in component.to_source_changes().items()
            if value != getattr(source, name)
        }
        if not legacy:
            return {}
        validated = self.deserialize(self.serialize(source) | legacy)
        return {
            name: getattr(validated, name) for name in legacy
            if getattr(validated, name) != getattr(source, name)
        }


source_registry = SourceRegistry()
for _source_type, _component in {
    SourceType.IMAGE: ImageComponent,
    SourceType.VIDEO: VideoComponent,
    SourceType.TEXT: TextComponent,
    SourceType.SHAPE: ShapeComponent,
    SourceType.PROGRESS_BAR: ProgressComponent,
    SourceType.TIME: TimeComponent,
    SourceType.ALBUM_COVER: AlbumCoverComponent,
    SourceType.LOGO: ImageComponent,
    SourceType.WATERMARK: ImageComponent,
    SourceType.BACKGROUND: BackgroundComponent,
    SourceType.AUDIO_VISUALIZER: VisualizerComponent,
    SourceType.LYRICS: LyricsComponent,
    SourceType.TRACK_LIST: TrackListComponent,
    SourceType.NOW_PLAYING: NowPlayingComponent,
    SourceType.AUDIO_WAVEFORM: WaveformComponent,
    SourceType.AUDIO_LEVEL_METER: LevelMeterComponent,
    SourceType.PARTICLE_OVERLAY: ParticleComponent,
}.items():
    source_registry.register(SourceDefinition(_source_type, _component))
