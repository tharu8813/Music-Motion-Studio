"""Project media path validation and safe relinking helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.models.project import ProjectDocument
from app.models.source import SourceType


@dataclass(slots=True)
class MissingMedia:
    """One project-linked file that is unavailable at its saved path."""

    kind: str
    identifier: str
    display_name: str
    original_path: str
    replacement_path: str = ""

    @property
    def is_audio(self) -> bool:
        return self.kind in {"audio", "library:audio"}

    @property
    def is_font(self) -> bool:
        return self.kind in {"font", "library:font"}

    @property
    def is_lyrics(self) -> bool:
        return self.kind == "lyrics" or self.kind == "library:lyrics"

    @property
    def is_video(self) -> bool:
        return self.kind in {"video", "track_video", "library:video"}

    @property
    def library_type(self) -> str:
        return self.kind.partition(":")[2] if self.kind.startswith("library:") else ""


class ProjectMediaService:
    """Finds absent image/audio assets before a ProjectDocument is applied."""

    @classmethod
    def validate(cls, document: ProjectDocument, project_path: Path) -> list[MissingMedia]:
        """Relink simple project-local matches and return remaining unavailable assets."""
        directory = project_path.parent
        missing: list[MissingMedia] = []
        for source in document.sources:
            if source.content_path and source.source_type is not SourceType.VIDEO:
                resolved = cls._resolve_existing_path(source.content_path, directory)
                if resolved:
                    source.content_path = str(resolved)
                else:
                    missing.append(MissingMedia(
                        kind="image", identifier=source.id, display_name=source.name,
                        original_path=source.content_path,
                    ))
            for index, video_path in enumerate(source.video_paths):
                resolved_video = cls._resolve_existing_path(video_path, directory)
                if resolved_video:
                    source.video_paths[index] = str(resolved_video)
                else:
                    missing.append(MissingMedia(
                        kind="video", identifier=f"{source.id}:{index}",
                        display_name=f"{source.name} video {index + 1}",
                        original_path=video_path,
                    ))
            if source.font_path:
                resolved_font = cls._resolve_existing_path(source.font_path, directory)
                if resolved_font:
                    source.font_path = str(resolved_font)
                else:
                    missing.append(MissingMedia(
                        kind="font", identifier=source.id,
                        display_name=f"{source.name} ({source.font_family})",
                        original_path=source.font_path,
                    ))
        for track in document.playlist:
            resolved = cls._resolve_existing_path(track.file_path, directory)
            if resolved:
                track.file_path = str(resolved)
            else:
                missing.append(MissingMedia(
                    kind="audio", identifier=track.id,
                    display_name=f"{track.artist} - {track.title}", original_path=track.file_path,
                ))
            if track.cover_path:
                cover = cls._resolve_existing_path(track.cover_path, directory)
                if cover:
                    track.cover_path = str(cover)
                else:
                    missing.append(MissingMedia(
                        kind="cover", identifier=track.id,
                        display_name=f"{track.title} cover",
                        original_path=track.cover_path,
                    ))
            if track.lyrics_path:
                lyrics = cls._resolve_existing_path(track.lyrics_path, directory)
                if lyrics:
                    track.lyrics_path = str(lyrics)
                else:
                    missing.append(MissingMedia(
                        kind="lyrics", identifier=track.id,
                        display_name=f"{track.title} lyrics",
                        original_path=track.lyrics_path,
                    ))
            for index, video_path in enumerate(track.video_paths):
                resolved_video = cls._resolve_existing_path(video_path, directory)
                if resolved_video:
                    track.video_paths[index] = str(resolved_video)
                else:
                    missing.append(MissingMedia(
                        kind="track_video", identifier=f"{track.id}:{index}",
                        display_name=f"{track.title} video {index + 1}",
                        original_path=video_path,
                    ))
        referenced_missing = {entry.original_path.casefold() for entry in missing}
        for content in document.content_library:
            if not content.path:
                continue
            resolved_content = cls._resolve_existing_path(content.path, directory)
            if resolved_content:
                content.path = str(resolved_content)
            elif content.path.casefold() not in referenced_missing:
                missing.append(MissingMedia(
                    kind=f"library:{content.media_type}", identifier=content.id,
                    display_name=content.name, original_path=content.path,
                ))
        return missing

    @staticmethod
    def apply_replacements(document: ProjectDocument, media: list[MissingMedia]) -> None:
        """Update selected paths; clear skipped images and disable skipped audio tracks."""
        sources = {source.id: source for source in document.sources}
        tracks = {track.id: track for track in document.playlist}
        contents = {content.id: content for content in document.content_library}
        for entry in media:
            replacement = Path(entry.replacement_path).expanduser() if entry.replacement_path else None
            if replacement and replacement.is_file():
                replacement_value = str(replacement.resolve())
                if entry.kind == "image" and entry.identifier in sources:
                    sources[entry.identifier].content_path = replacement_value
                elif entry.kind == "font" and entry.identifier in sources:
                    sources[entry.identifier].font_path = replacement_value
                elif entry.kind == "audio" and entry.identifier in tracks:
                    tracks[entry.identifier].file_path = replacement_value
                elif entry.kind == "cover" and entry.identifier in tracks:
                    tracks[entry.identifier].cover_path = replacement_value
                elif entry.kind == "lyrics" and entry.identifier in tracks:
                    tracks[entry.identifier].lyrics_path = replacement_value
                elif entry.kind in {"video", "track_video"}:
                    owner_id, _, index_text = entry.identifier.rpartition(":")
                    owner = sources.get(owner_id) if entry.kind == "video" else tracks.get(owner_id)
                    if owner is not None and index_text.isdigit():
                        paths = owner.video_paths
                        index = int(index_text)
                        if 0 <= index < len(paths):
                            paths[index] = replacement_value
                elif entry.kind.startswith("library:") and entry.identifier in contents:
                    contents[entry.identifier].path = replacement_value
                for content in document.content_library:
                    if content.path.casefold() == entry.original_path.casefold():
                        content.path = replacement_value
            elif entry.kind == "image" and entry.identifier in sources:
                sources[entry.identifier].content_path = ""
            elif entry.kind == "font" and entry.identifier in sources:
                sources[entry.identifier].font_path = ""
            elif entry.kind == "audio" and entry.identifier in tracks:
                tracks[entry.identifier].enabled = False
            elif entry.kind == "cover" and entry.identifier in tracks:
                tracks[entry.identifier].cover_path = ""
            elif entry.kind == "lyrics" and entry.identifier in tracks:
                tracks[entry.identifier].lyrics_path = ""
            elif entry.kind in {"video", "track_video"}:
                owner_id, _, index_text = entry.identifier.rpartition(":")
                owner = sources.get(owner_id) if entry.kind == "video" else tracks.get(owner_id)
                if owner is not None and index_text.isdigit():
                    index = int(index_text)
                    if 0 <= index < len(owner.video_paths):
                        owner.video_paths[index] = ""
            elif entry.kind.startswith("library:") and entry.identifier in contents:
                contents[entry.identifier].path = ""
            if not replacement or not replacement.is_file():
                for content in document.content_library:
                    if content.path.casefold() == entry.original_path.casefold():
                        content.path = ""
        document.content_library[:] = [
            content for content in document.content_library if content.path
        ]
        for source in document.sources:
            source.video_paths[:] = [path for path in source.video_paths if path]
            if source.source_type is SourceType.VIDEO:
                source.content_path = source.video_paths[0] if source.video_paths else ""
        for track in document.playlist:
            track.video_paths[:] = [path for path in track.video_paths if path]

    @staticmethod
    def _resolve_existing_path(raw_path: str, project_directory: Path) -> Path | None:
        """Try the saved path, then project-relative and same-name local alternatives."""
        candidate = Path(raw_path).expanduser()
        alternatives = [candidate]
        if not candidate.is_absolute():
            alternatives.insert(0, project_directory / candidate)
        alternatives.append(project_directory / candidate.name)
        for alternative in alternatives:
            if alternative.is_file():
                return alternative.resolve()
        return None
