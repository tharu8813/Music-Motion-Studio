"""Export playlist lyrics on the resolved audio timeline, including overlapping clips."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import os
from pathlib import Path
from tempfile import mkstemp
from typing import Iterable
from xml.etree import ElementTree as ET

from app.models.playlist import PlaylistTrack
from app.services.lyrics_service import LyricsError, LyricsService
from app.services.export_formats import SUBTITLE_FORMATS
from app.timeline.compiler import compile_playlist
from app.timeline.render_plan import CompiledRenderPlan


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    start: float
    end: float
    text: str


class SubtitleExportService:
    """Resolve lyric boundaries against source trims, tempo changes, and audio overlaps."""

    @staticmethod
    def cues(tracks: Iterable[PlaylistTrack], plan: CompiledRenderPlan, *, timing_offset: float = 0.0) -> list[SubtitleCue]:
        if not isfinite(timing_offset):
            raise LyricsError("Subtitle timing offset must be finite.")
        track_by_id = {track.id: track for track in tracks if track.enabled}
        # All independently timed cues remain visible until their actual end,
        # including overlapping lines within the same TTML/LRC track.
        events: dict[int, list[tuple[bool, int, int, str]]] = {}
        clip_order = sorted(enumerate(plan.audio.clips), key=lambda item: (item[1].timeline_start, item[0]))
        for clip_index, (_original_index, clip) in enumerate(clip_order):
            track = track_by_id.get(clip.track_id)
            if track is None:
                continue
            offset = float(track.lyrics_timing_offset_seconds) + timing_offset
            ordered_cues = sorted(track.lyrics, key=lambda item: (float(item.get("start", 0)), float(item.get("end", 0))))
            for cue_index, cue in enumerate(ordered_cues):
                source_start = float(cue.get("start", 0)) - offset
                source_end = float(cue.get("end", float(cue.get("start", 0)) + 8)) - offset
                text = "\n".join(
                    line.strip() for line in LyricsService.decode_line_breaks(cue.get("text", "")).splitlines()
                    if line.strip()
                )
                if not text or not isfinite(source_start) or not isfinite(source_end):
                    continue
                start = max(clip.source_in, source_start)
                end = min(clip.source_out, source_end)
                if end <= start:
                    continue
                start_ms = max(0, round(clip.timeline_at(start) * 1000))
                end_ms = min(round(plan.duration_seconds * 1000), round(clip.timeline_at(end) * 1000))
                if end_ms <= start_ms:
                    continue
                events.setdefault(start_ms, []).append((True, clip_index, cue_index, text))
                events.setdefault(end_ms, []).append((False, clip_index, cue_index, text))
        times = sorted(events)
        active: dict[int, dict[int, str]] = {}
        result: list[SubtitleCue] = []
        for index, time_ms in enumerate(times[:-1]):
            for entering, clip_index, cue_index, text in events[time_ms]:
                clip_cues = active.setdefault(clip_index, {})
                if entering:
                    clip_cues[cue_index] = text
                else:
                    clip_cues.pop(cue_index, None)
                if not clip_cues:
                    active.pop(clip_index, None)
            lines = [
                text
                for _clip_index, clip_cues in sorted(active.items())
                for _cue_index, text in sorted(clip_cues.items())
            ]
            if not lines:
                continue
            text = "\n".join(f"- {line}" for line in lines) if len(lines) > 1 else lines[0]
            start, end = time_ms / 1000, times[index + 1] / 1000
            if result and result[-1].text == text and result[-1].end == start:
                previous = result[-1]
                result[-1] = SubtitleCue(previous.start, end, text)
            else:
                result.append(SubtitleCue(start, end, text))
        return result

    @staticmethod
    def _srt_timestamp(milliseconds: int) -> str:
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, fraction = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{fraction:03d}"

    @classmethod
    def format_srt(cls, cues: list[SubtitleCue]) -> str:
        blocks = []
        for cue in cues:
            start, end = round(cue.start * 1000), round(cue.end * 1000)
            if end <= start:
                continue
            blocks.append(f"{len(blocks) + 1}\n{cls._srt_timestamp(start)} --> {cls._srt_timestamp(end)}\n{cue.text}")
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    @staticmethod
    def format_lrc(cues: list[SubtitleCue], *, title: str = "", artist: str = "") -> str:
        # LRC has no end timestamp: clear the text at cue ends, including gaps
        # and the final silence. At a shared boundary the next text wins.
        entries: dict[int, str] = {0: ""}
        for cue in cues:
            start, end = round(cue.start * 100), round(cue.end * 100)
            if end <= start:
                continue
            entries[start] = cue.text
            entries[end] = ""
        header = LyricsService.format_lrc([], title=title, artist=artist)
        lines = [f"[{LyricsService.lrc_timestamp(time / 100)}]{text}" for time, text in sorted(entries.items())]
        # Multiline cues contain physical line breaks, including the two rows
        # used for an outgoing and incoming track in a mix.
        return header + "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def track_formats(cues: list[dict]) -> tuple[str, ...]:
        """Offer TTML only when the lyrics already carry inline timing."""
        if any(cue.get("words") for cue in cues):
            return ("ttml", "lrc", "srt")
        return ("lrc", "srt")

    @staticmethod
    def _format_ttml(cues: list[dict]) -> str:
        root = ET.Element("tt", {"xmlns": "http://www.w3.org/ns/ttml", "xml:space": "preserve"})
        division = ET.SubElement(ET.SubElement(root, "body"), "div")

        def append_text(element: ET.Element, text: str) -> None:
            for index, line in enumerate(text.split("\n")):
                if index:
                    ET.SubElement(element, "br").tail = line
                elif len(element):
                    element[-1].tail = (element[-1].tail or "") + line
                else:
                    element.text = (element.text or "") + line

        for cue in cues:
            start, end = cue["start"], cue["end"]
            paragraph = ET.SubElement(division, "p", {"begin": f"{start:.9f}s", "end": f"{end:.9f}s"})
            text = cue["text"]
            offset = 0
            for word in cue["words"]:
                if word["end"] <= word["start"]:
                    continue
                position = word["offset"]
                append_text(paragraph, text[offset:position])
                span = ET.SubElement(paragraph, "span", {
                    "begin": f"{word['start'] - start:.9f}s",
                    "end": f"{word['end'] - start:.9f}s",
                })
                append_text(span, word["text"])
                offset = position + len(word["text"])
            append_text(paragraph, text[offset:])
        return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"

    @classmethod
    def export_track_lyrics(
        cls, cues: list[dict], output_path: str | Path, *, timing_offset: float = 0.0,
        title: str = "", artist: str = "",
    ) -> Path:
        """Export the current track's cues without consulting the original sidecar."""
        target = Path(output_path).expanduser().resolve()
        suffix = target.suffix.lower().lstrip(".")
        if suffix not in cls.track_formats(cues):
            raise LyricsError("The selected format is unavailable for these lyrics.")
        if not isfinite(timing_offset):
            raise LyricsError("Subtitle timing offset must be finite.")
        adjusted = []
        for cue in cues:
            source_start = float(cue.get("start", 0.0))
            start = max(0.0, source_start - timing_offset)
            end = max(start, float(cue.get("end", source_start + 8.0)) - timing_offset)
            text = LyricsService.decode_line_breaks(cue.get("text", ""))
            if not text.strip() or (suffix != "lrc" and end <= start):
                continue
            words = [
                {**word, "start": max(start, min(end, word["start"] - timing_offset)),
                 "end": max(start, min(end, word["end"] - timing_offset))}
                for word in cue.get("words", []) if word["end"] - timing_offset > start
            ]
            adjusted.append({"start": start, "end": end, "text": text, "words": words})
        if not adjusted:
            raise LyricsError("No timed lyrics remain after applying the track offset.")
        if suffix == "lrc":
            content = LyricsService.format_lrc(adjusted, title=title, artist=artist)
        elif suffix == "srt":
            content = cls.format_srt([
                SubtitleCue(cue["start"], cue["end"],
                            "\n".join(line for line in cue["text"].splitlines() if line.strip()))
                for cue in adjusted
            ])
        else:
            content = cls._format_ttml(adjusted)
        if not content:
            raise LyricsError("No timed lyrics remain at this format's time resolution.")
        return cls._save_text(target, content)

    def export(
        self, tracks: Iterable[PlaylistTrack], output_path: str | Path,
        compiled_plan: CompiledRenderPlan | None = None, *, title: str = "", artist: str = "",
        timing_offset: float = 0.0,
    ) -> Path:
        selected = [track for track in tracks if track.enabled]
        target = Path(output_path).expanduser().resolve()
        if target.suffix.lower().lstrip(".") not in SUBTITLE_FORMATS:
            raise LyricsError("Choose an LRC or SRT subtitle file.")
        cues = self.cues(selected, compiled_plan or compile_playlist(selected, enabled_only=True), timing_offset=timing_offset)
        if not cues:
            raise LyricsError("No timed lyrics were found in the selected audio timeline.")
        content = self.format_srt(cues) if target.suffix.lower() == ".srt" else self.format_lrc(cues, title=title, artist=artist)
        return self._save_text(target, content)

    @staticmethod
    def _save_text(target: Path, content: str) -> Path:
        staging: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = mkstemp(prefix=".playlist-subtitles-", suffix=".tmp", dir=target.parent)
            staging = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            staging.replace(target)
        except OSError as error:
            raise LyricsError(f"Could not save subtitles: {error}") from error
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
        return target
