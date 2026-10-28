"""Timed LRC and SRT lyric parsing utilities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import re
from pathlib import Path
from unicodedata import east_asian_width

LYRICS_EXTENSIONS = (".lrc", ".srt", ".vtt", ".ttml")
_SIMILAR_RATIO = 0.72


class LyricsError(ValueError):
    """Raised when a lyric file cannot be read as LRC or SRT."""


@dataclass(frozen=True, slots=True)
class LyricsSidecar:
    """A lyric file discovered next to an audio file during import."""

    path: Path
    exact: bool


def _normalized_stem(stem: str) -> str:
    """Reduce a file stem so track-number prefixes and copy suffixes don't matter."""
    text = stem.casefold()
    text = re.sub(r"^\s*\d{1,3}\s*[-_.)\]]*\s*", "", text)  # "01 - ", "12."
    text = re.sub(r"\s*[(\[][^)\]]*[)\]]\s*$", "", text)  # " (1)", " [live]"
    text = re.sub(r"\s*-\s*copy\s*$", "", text)
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def find_sidecar_lyrics(
    audio_path: str | Path,
    *, entries: Iterable[Path] | None = None,
) -> tuple[list[LyricsSidecar], list[LyricsSidecar]]:
    """Return (exact-stem matches, similar-stem matches) beside an audio file.

    Exact matches share the file stem (case-insensitively); ``.lrc`` is listed
    first. Similar matches survive normalization of track numbers and copy
    markers, or score above a fuzzy-ratio threshold, and never include an
    exact match.
    """
    audio = Path(audio_path)
    folder = audio.parent
    if entries is None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return [], []
    exact_stem = audio.stem.casefold()
    target = _normalized_stem(audio.stem)
    exact: list[LyricsSidecar] = []
    scored_similar: list[tuple[float, LyricsSidecar]] = []
    for entry in entries:
        if entry.suffix.lower() not in LYRICS_EXTENSIONS or not entry.is_file():
            continue
        if entry.stem.casefold() == exact_stem:
            exact.append(LyricsSidecar(entry, True))
            continue
        candidate = _normalized_stem(entry.stem)
        if not candidate or not target:
            continue
        if candidate == target:
            scored_similar.append((1.0, LyricsSidecar(entry, False)))
            continue
        ratio = SequenceMatcher(None, target, candidate).ratio()
        if ratio >= _SIMILAR_RATIO:
            scored_similar.append((ratio, LyricsSidecar(entry, False)))
    exact.sort(
        key=lambda sidecar: LYRICS_EXTENSIONS.index(sidecar.path.suffix.lower())
    )
    scored_similar.sort(key=lambda item: item[0], reverse=True)
    return exact, [sidecar for _ratio, sidecar in scored_similar]


class LyricsService:
    """Load and resolve timed lyric cues for the active playlist track."""

    _lrc_time = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
    _srt_time = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{1,3})")
    _word_time = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>")

    @staticmethod
    @lru_cache(maxsize=512)
    def _automatic_words(text: str) -> tuple[tuple[int, int], ...]:
        spans = []
        for match in re.finditer(r"\S+", text):
            # No-space CJK lyrics still advance one character at a time.
            if all(east_asian_width(char) in {"W", "F"} for char in match[0]):
                spans.extend((index, index + 1) for index in range(match.start(), match.end()))
            else:
                spans.append(match.span())
        return tuple(spans)

    @classmethod
    @lru_cache(maxsize=512)
    def _reveal_spans(cls, text, start, end, words, line_positions):
        """Cache timing preparation; fill missing text between existing anchors."""
        def estimate(a, b, first, last):
            indices = cls._automatic_words(text[a:b])
            total = sum(right - left for left, right in indices)
            consumed, spans = 0, []
            # ponytail: character-weighted gaps; timed LRC supplies vocal accuracy.
            for left, right in indices:
                word_start = first + (last - first) * consumed / max(1, total)
                consumed += right - left
                word_end = first + (last - first) * consumed / max(1, total)
                spans.append((a + left, a + right, word_start, word_end))
            return spans

        automatic = estimate(0, len(text), start, end) if not words and line_positions is None else []
        result, offset = [], 0
        for segment in text.splitlines(keepends=True):
            line = segment.rstrip("\r\n")
            if line.strip():
                limit = offset + len(line)
                anchors = [word for word in words if word[1] > offset and word[0] < limit]
                selected = not line_positions or len(result) in line_positions
                spans = []
                if selected and automatic:
                    spans = [word for word in automatic if word[1] > offset and word[0] < limit]
                elif selected and (anchors or line_positions is not None):
                    cursor, previous_end = offset, start
                    for a, b, first, last in anchors:
                        spans.extend(estimate(cursor, a, previous_end, first))
                        spans.append((a, b, first, last))
                        cursor, previous_end = b, last
                    spans.extend(estimate(cursor, limit, previous_end, end))
                result.append(tuple((max(0, a - offset), min(len(line), b - offset), first, last)
                                    for a, b, first, last in spans))
            offset += len(segment)
        return tuple(result)

    @classmethod
    def word_reveal(cls, cue: dict[str, object], elapsed: float, *,
                    line_positions: tuple[int, ...] | None = None) -> tuple[float, ...]:
        """Revealed characters, with independent fallback for selected sung lines."""
        raw_text = cls.decode_line_breaks(cue.get("text", ""))
        text = raw_text.strip()
        leading = len(raw_text) - len(raw_text.lstrip())
        words = tuple((int(word["offset"]) - leading,
                       int(word["offset"]) + len(word["text"]) - leading,
                       float(word["start"]), float(word["end"])) for word in cue.get("words", []))
        lines = cls._reveal_spans(text, float(cue.get("start", 0)), float(cue.get("end", 0)),
                                  words, line_positions)
        result = []
        for spans in lines:
            revealed = 0.0
            for a, b, start, end in spans:
                if elapsed < start:
                    break
                progress = 1.0 if end <= start else min(1.0, max(0.0, (elapsed - start) / (end - start)))
                revealed = max(revealed, a + (b - a) * progress)
            result.append(revealed)
        return tuple(result)

    @staticmethod
    def decode_line_breaks(value: object) -> str:
        """Interpret the two LRC characters ``\\n`` as an in-app line break."""
        return str(value).replace("\\n", "\n")

    @staticmethod
    def encode_line_breaks(value: object) -> str:
        """Store in-app line breaks as the two literal LRC characters ``\\n``."""
        normalized = str(value).replace("\r\n", "\n").replace("\r", "\n")
        return normalized.replace("\n", "\\n")

    @staticmethod
    def lrc_timestamp(seconds: float) -> str:
        """Format seconds as a conventional hundredth-resolution LRC timestamp."""
        total_hundredths = max(0, round(float(seconds) * 100))
        minutes, remainder = divmod(total_hundredths, 6_000)
        whole_seconds, hundredths = divmod(remainder, 100)
        return f"{minutes:02d}:{whole_seconds:02d}.{hundredths:02d}"

    @classmethod
    def format_lrc(
        cls, cues: list[dict[str, object]], *, title: str = "",
        artist: str = "", creator: str = "Playlist Canvas",
    ) -> str:
        """Serialize timed lyric cues into a UTF-8 LRC document."""
        lines: list[str] = []
        for tag, value in (("ti", title), ("ar", artist), ("by", creator)):
            cleaned = str(value).replace("\r", " ").replace("\n", " ").strip()
            if cleaned:
                lines.append(f"[{tag}:{cleaned}]")
        if lines:
            lines.append("")
        ordered = sorted(
            cues, key=lambda cue: (float(cue.get("start", 0.0)), str(cue.get("text", "")))
        )
        for cue in ordered:
            text = cls.decode_line_breaks(cue.get("text", ""))
            if not cue.get("words"):
                text = text.strip()
            markers: dict[int, list[str]] = {}
            for word in cue.get("words", []):
                offset = int(word["offset"])
                markers.setdefault(offset, []).append(f"<{cls.lrc_timestamp(float(word['start']))}>")
                markers.setdefault(offset + len(word["text"]), []).append(
                    f"<{cls.lrc_timestamp(float(word['end']))}>"
                )
            for offset in sorted(markers, reverse=True):
                text = text[:offset] + "".join(markers[offset]) + text[offset:]
            text = cls.encode_line_breaks(text)
            lines.append(f"[{cls.lrc_timestamp(float(cue.get('start', 0.0)))}]{text}")
        return "\n".join(lines) + ("\n" if lines else "")

    @classmethod
    def save_lrc(
        cls, path: str | Path, cues: list[dict[str, object]], *,
        title: str = "", artist: str = "",
    ) -> Path:
        """Write a generated LRC file atomically using UTF-8."""
        target = Path(path).expanduser()
        if target.suffix.lower() != ".lrc":
            target = target.with_suffix(".lrc")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_text(
                cls.format_lrc(cues, title=title, artist=artist),
                encoding="utf-8",
            )
            temporary.replace(target)
            return target.resolve()
        except OSError as error:
            raise LyricsError(f"Could not save LRC file: {error}") from error

    @classmethod
    def load(cls, path: str | Path) -> list[dict[str, object]]:
        lyric_path = Path(path)
        try:
            data = lyric_path.read_bytes()
            encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            content = data.decode(encoding)
        except OSError as error:
            raise LyricsError(str(error)) from error
        except UnicodeError as error:
            raise LyricsError("가사 파일의 문자 인코딩을 읽을 수 없습니다. UTF-8 또는 UTF-16으로 저장한 뒤 다시 불러오세요.\n"
                              "Cannot decode lyrics. Save the file as UTF-8 or UTF-16 and try again.") from error
        return cls.parse(content, lyric_path.suffix)

    @classmethod
    def parse(cls, content: str, suffix: str) -> list[dict[str, object]]:
        """Parse file or downloaded content without writing a temporary lyric file."""
        suffix = suffix.lower()
        if suffix == ".lrc":
            return cls._parse_lrc(content)
        if suffix in {".srt", ".vtt"}:
            return cls._parse_srt(content)
        if suffix == ".ttml":
            from app.services.ttml_parser import TTMLParseError, parse_ttml
            try:
                return parse_ttml(content)
            except TTMLParseError as error:
                raise LyricsError(str(error)) from error
        raise LyricsError("Only .lrc, .srt, .vtt, and .ttml lyric files are supported.")

    @classmethod
    def current_text(cls, cues: list[dict[str, object]], elapsed: float) -> str:
        """Return the cue active at elapsed seconds, or an empty string."""
        cue = cls.current_cue(cues, elapsed)
        return cls.decode_line_breaks(cue.get("text", "")) if cue else ""

    @classmethod
    def current_cue(cls, cues: list[dict[str, object]], elapsed: float) -> dict[str, object] | None:
        """Return the exact active cue, with a tiny tolerance for frame rounding."""
        index = cls.current_cue_index(cues, elapsed)
        return cues[index] if index is not None else None

    @classmethod
    def current_cue_index(cls, cues: list[dict[str, object]], elapsed: float) -> int | None:
        """Return the active cue index in O(log n) with safe frame boundaries."""
        if not cues:
            return None
        precision = 0.0005
        # Cues are normalized into start-time order by parsers and project
        # validation. Binary search avoids scanning a long subtitle file for
        # every preview/export frame.
        low = 0
        high = len(cues)
        target = elapsed + precision
        while low < high:
            middle = (low + high) // 2
            if float(cues[middle].get("start", 0.0)) <= target:
                low = middle + 1
            else:
                high = middle
        index = low - 1
        if index >= 0:
            cue = cues[index]
            start = float(cue.get("start", 0.0))
            end = float(cue.get("end", start + 8.0))
            if start - precision <= elapsed < end - precision:
                return index
        return None

    @classmethod
    def display_cue_index(cls, cues: list[dict[str, object]], elapsed: float) -> int | None:
        """Return a lyric to display continuously whenever the track has lyrics.

        Timed subtitle files can leave silence before the first cue or gaps between
        cues.  A Lyrics source should not fall back to its editor placeholder during
        those gaps: show the first cue from track start, then retain the most recent
        cue until the next one begins.
        """
        if not cues:
            return None
        active_index = cls.current_cue_index(cues, elapsed)
        if active_index is not None:
            return active_index
        precision = 0.0005
        low = 0
        high = len(cues)
        target = elapsed + precision
        while low < high:
            middle = (low + high) // 2
            if float(cues[middle].get("start", 0.0)) <= target:
                low = middle + 1
            else:
                high = middle
        return max(0, low - 1)

    @classmethod
    def _parse_lrc(cls, content: str) -> list[dict[str, object]]:
        cues: list[dict[str, object]] = []
        previous: list[dict[str, object]] = []
        for line in content.splitlines():
            text = cls.decode_line_breaks(cls._lrc_time.sub("", line).strip())
            timestamps = cls._lrc_time.findall(line)
            if timestamps:
                words = []
                plain = cls._word_time.sub("", text)
                line_offset = 0
                for physical_line in text.splitlines(keepends=True):
                    tagged = physical_line.rstrip("\r\n")
                    markers = list(cls._word_time.finditer(tagged))
                    offset = line_offset + (markers[0].start() if markers else 0)
                    for index, marker in enumerate(markers):
                        following = markers[index + 1] if index + 1 < len(markers) else None
                        segment = tagged[marker.end():following.start() if following else len(tagged)]
                        word_start = int(marker[1]) * 60 + float(marker[2])
                        word_end = int(following[1]) * 60 + float(following[2]) if following else None
                        if word_end is not None and word_end < word_start:
                            raise LyricsError("Word timestamps must be in ascending order within a line.")
                        if segment:
                            words.append({"start": word_start, "end": word_end,
                                          "text": segment, "offset": offset})
                        offset += len(segment)
                    line_offset += len(cls._word_time.sub("", physical_line))
                previous = []
                for minute, second in timestamps:
                    cue = {"start": int(minute) * 60 + float(second), "end": 0.0, "text": plain}
                    if words:
                        shift = float(cue["start"]) - (int(timestamps[0][0]) * 60 + float(timestamps[0][1]))
                        cue["words"] = [word | {"start": word["start"] + shift,
                            "end": word["end"] + shift if word["end"] is not None else None} for word in words]
                    cues.append(cue)
                    previous.append(cue)
            elif line.strip().startswith("[") or not line.strip():
                previous = []
            elif previous:
                for cue in previous:
                    cue["text"] = f"{cue['text']}\n{text}"
        cues.sort(key=lambda cue: float(cue["start"]))
        for index, cue in enumerate(cues):
            cue["end"] = float(cues[index + 1]["start"]) if index + 1 < len(cues) else float(cue["start"]) + 8.0
            if index + 1 == len(cues) and cue.get("words"):
                cue["end"] = max(float(cue["end"]), max(word["end"] if word["end"] is not None
                                 else word["start"] + 8.0 for word in cue["words"]))
            for word in cue.get("words", []):
                word["end"] = float(cue["end"]) if word["end"] is None else word["end"]
                if not float(cue["start"]) <= word["start"] <= word["end"] <= float(cue["end"]):
                    raise LyricsError("Word timestamps must fall inside their lyric cue.")
        return cues

    @classmethod
    def _parse_srt(cls, content: str) -> list[dict[str, object]]:
        cues: list[dict[str, object]] = []
        for block in re.split(r"\r?\n\s*\r?\n", content.strip()):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if len(lines) < 2:
                continue
            if lines[0].split(maxsplit=1)[0] in {"WEBVTT", "NOTE", "STYLE", "REGION"}:
                continue
            timing_index = 0 if "-->" in lines[0] else 1
            if "-->" not in lines[timing_index]:
                continue
            start_raw, end_raw = (part.strip() for part in lines[timing_index].split("-->", 1))
            start, end = cls._srt_seconds(start_raw), cls._srt_seconds(end_raw)
            if end < start:
                raise LyricsError("Subtitle cue ends before it starts.")
            cues.append({"start": start, "end": end, "text": "\n".join(lines[timing_index + 1:])})
        cues.sort(key=lambda cue: (float(cue["start"]), float(cue["end"])))
        return cues

    @classmethod
    def _srt_seconds(cls, value: str) -> float:
        match = cls._srt_time.fullmatch(value.split(maxsplit=1)[0]) if value.strip() else None
        if not match:
            raise LyricsError(f"Invalid subtitle timestamp: {value}")
        hours, minutes, seconds, milliseconds = match.groups()
        if int(minutes) >= 60 or int(seconds) >= 60:
            raise LyricsError(f"Invalid subtitle timestamp: {value}")
        try:
            return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds.ljust(3, "0")) / 1000
        except (ValueError, OverflowError) as error:
            raise LyricsError("Subtitle timestamp is too large.") from error
