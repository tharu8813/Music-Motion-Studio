"""Read and write M3U8 (and legacy M3U) playlists of local audio files.

The playlist file itself is only a list of paths: importing it adds the songs it
points to, never the ``.m3u8`` file, to the project.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import locale
from itertools import count
from pathlib import Path
import shutil
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from app.models.playlist import PlaylistTrack
from app.services.export_file_naming import project_export_stem
from app.services.playlist_service import AUDIO_EXTENSIONS
from app.services.project_service import ProjectService

PLAYLIST_FILE_EXTENSIONS = {".m3u8", ".m3u"}


class M3uPlaylistError(Exception):
    """Raised when a playlist file cannot be read or written."""


@dataclass(slots=True)
class M3uEntries:
    """Playable local audio files in playlist order, plus the entries left out."""

    audio_paths: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    """Missing files, web streams and non-audio entries, as written in the playlist."""


def read_m3u(path: str | Path) -> M3uEntries:
    """Return the local audio files a playlist lists, relative entries resolved to its folder."""
    playlist = Path(path)
    try:
        raw = playlist.read_bytes()
    except OSError as error:
        raise M3uPlaylistError(f"Could not read the playlist file: {playlist}") from error
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # M3U8 is UTF-8 by definition; a legacy .m3u uses the system code page.
        encoding = "utf-8" if playlist.suffix.lower() == ".m3u8" else locale.getpreferredencoding(False)
        text = raw.decode(encoding, errors="replace")
    entries = M3uEntries()
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        location = _local_path(entry, playlist.parent)
        if location is None or location.suffix.lower() not in AUDIO_EXTENSIONS or not location.is_file():
            entries.skipped.append(entry)
            continue
        entries.audio_paths.append(location.resolve())
    return entries


def export_m3u8_folder(
    tracks: Iterable[PlaylistTrack], directory: Path, name: str,
    library_names: Mapping[str, str] | None = None,
) -> tuple[Path, int]:
    """Create a standalone playlist folder with copies of its enabled tracks."""
    tracks = [track for track in tracks if track.enabled]
    library_names = library_names or {}
    if not tracks:
        raise M3uPlaylistError("There are no songs to export.")
    base = project_export_stem(name)
    folder: Path | None = None
    try:
        directory = directory.resolve()
        for number in count(1):
            candidate = directory / (base if number == 1 else f"{base} ({number})")
            try:
                candidate.mkdir()
            except FileExistsError:
                continue
            folder = candidate
            break
        music = folder / "music"
        music.mkdir()
        lines = ["#EXTM3U"]
        copied: dict[Path, str] = {}
        used_names: set[str] = set()
        for track in tracks:
            source = Path(track.file_path).expanduser()
            if source.suffix.casefold() not in AUDIO_EXTENSIONS or not source.is_file():
                raise OSError(f"Audio file is missing or unsupported: {track.title} ({source})")
            resolved = source.resolve()
            filename = copied.get(resolved)
            if filename is None:
                original = Path(ProjectService.media_export_filename(
                    source, library_names.get(track.file_path, ""),
                ))
                stem = original.stem.replace("\r", "_").replace("\n", "_")
                filename = f"{stem}{original.suffix}"
                for number in count(2):
                    if filename.casefold() not in used_names:
                        break
                    filename = f"{stem} ({number}){original.suffix}"
                used_names.add(filename.casefold())
                copied[resolved] = filename
            seconds = round(track.duration_seconds) if track.duration_seconds > 0 else -1
            label = f"{track.artist} - {track.title}" if track.artist else track.title
            lines.append(f"#EXTINF:{seconds},{' '.join(label.splitlines())}")
            lines.append(f"music/{filename}")
        for source, filename in copied.items():
            shutil.copyfile(source, music / filename)
        playlist = folder / f"{folder.name}.m3u8"
        draft = folder / "playlist.tmp"
        draft.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        draft.replace(playlist)
    except (OSError, ValueError) as error:
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        raise M3uPlaylistError(str(error)) from error
    return playlist, len(tracks)


def _local_path(entry: str, base: Path) -> Path | None:
    if entry.lower().startswith("file:"):
        parsed = urlparse(entry)
        # file://server/share/x.mp3 keeps its UNC host; file:///C:/x.mp3 has none.
        location = f"//{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path
        return Path(url2pathname(unquote(location)))
    if "://" in entry:
        return None  # http(s) streams and other remote entries
    location = Path(entry)
    return location if location.is_absolute() else base / location
