"""Selectable multi-provider lyrics through https://github.com/AlFarrizi-Studio/LiriQo."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import json
from math import isfinite
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from time import monotonic, time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from app import __version__
from app.models.playlist import PlaylistTrack
from app.services.lyrics_service import LyricsError, LyricsService

LIRIQO_API = "https://api.liriqo-alfarrizi.my.id/v1/lyrics"
PROVIDERS = {
    "apple_music_liriqo": "Apple Music", "unison_community": "Unison",
    "qq_portato": "QQ Music", "kugou_legato": "KuGou",
    "lrclib_wordsync": "LRCLib (estimated word sync)", "lrclib": "LRCLib",
    "ytm_line": "YouTube Music", "netease": "NetEase Cloud Music",
}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_REQUEST_WAIT = 30


@dataclass
class _HostRequests:
    lock: object = field(default_factory=Lock)
    next_request: float = 0


class LyricsRequestState:
    """Shared across dialogs: paced requests, server cooldowns and bounded LRU."""

    def __init__(self, *, intervals=None, cooldown_delay=5):
        self.intervals = intervals if intervals is not None else {
            "api.liriqo-alfarrizi.my.id": 1.0,
        }
        self.cooldown_delay = cooldown_delay
        self.lock = Lock()
        self.hosts = {}
        self.responses = OrderedDict()
        self.cache_bytes = 0

    def cached(self, key):
        with self.lock:
            entry = self.responses.get(key)
            if entry is None:
                return None
            expires, raw = entry
            if expires <= monotonic():
                self.cache_bytes -= len(raw)
                del self.responses[key]
                return None
            self.responses.move_to_end(key)
            return json.loads(raw.decode("utf-8-sig"))

    def remember(self, key, raw, ttl):
        with self.lock:
            previous = self.responses.pop(key, None)
            if previous is not None:
                self.cache_bytes -= len(previous[1])
            self.responses[key] = monotonic() + ttl, raw
            self.cache_bytes += len(raw)
            while len(self.responses) > 64 or self.cache_bytes > MAX_CACHE_BYTES:
                _key, (_expires, removed) = self.responses.popitem(last=False)
                self.cache_bytes -= len(removed)

    def host(self, name):
        with self.lock:
            return self.hosts.setdefault(name, _HostRequests())


_REQUEST_STATE = LyricsRequestState()


class LyricsSearchError(ValueError):
    """An actionable search, provider, or download failure."""


class LyricsSearchCancelled(Exception):
    pass


@dataclass(frozen=True)
class LyricsSearchQuery:
    title: str
    artist: str = ""
    album: str = ""
    duration: float = 0
    provider: str = "all"
    synced_only: bool = True

    def validate(self):
        if not self.title.strip():
            raise LyricsSearchError("제목은 필수입니다. / A song title is required.")
        if self.provider not in {"all", *PROVIDERS}:
            raise LyricsSearchError("Unknown lyric provider.")
        if not isfinite(self.duration) or not 0 <= self.duration <= 86400:
            raise LyricsSearchError("Invalid track duration.")


@dataclass(frozen=True)
class LyricsSearchResult:
    provider: str
    identifier: str
    title: str
    artist: str
    album: str = ""
    duration: float = 0
    # Payload is kept so selecting a result never resolves a different recording.
    timing: str = "unknown"
    synced_lyrics: str = field(default="", repr=False)
    plain_lyrics: str = field(default="", repr=False)
    payload: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def key(self):
        return self.provider, self.identifier


@dataclass(frozen=True)
class LyricsSearchBatch:
    results: list[LyricsSearchResult]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FoundLyrics:
    result: LyricsSearchResult
    format: str
    content: str = field(repr=False)
    cues: list[dict[str, object]] = field(default_factory=list, repr=False)

    @property
    def filename(self):
        name = " - ".join(part for part in (self.result.artist, self.result.title) if part)
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:160] or "lyrics"
        if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            name = "_" + name
        return name + "." + self.format


def _cancelled(cancel: Event | None):
    if cancel is not None and cancel.is_set():
        raise LyricsSearchCancelled()


def _normal(value):
    return re.sub(r"[\W_]+", "", str(value).casefold())


class LyricsSearchService:
    """Bounded HTTP calls; invoked by a worker, never by the GUI paint thread."""

    def __init__(self, request_state: LyricsRequestState | None = None):
        self.request_state = request_state or _REQUEST_STATE
        self.on_wait = None

    def _wait(self, seconds: float, cancel: Event | None):
        _cancelled(cancel)
        if seconds <= 0:
            return
        if self.on_wait is not None:
            self.on_wait(seconds)
        if (cancel or Event()).wait(seconds):
            raise LyricsSearchCancelled()

    @staticmethod
    def _retry_after(headers, fallback):
        value = headers.get("Retry-After", "") if headers is not None else ""
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time()
            except (ValueError, TypeError, OverflowError):
                return fallback
        return max(0.0, seconds) if isfinite(seconds) else fallback

    def _get_json(self, url: str, params: dict, cancel: Event | None):
        _cancelled(cancel)
        key = url, tuple(sorted((name, str(value)) for name, value in params.items()))
        cached = self.request_state.cached(key)
        if cached is not None:
            return cached
        host_name = urlsplit(url).hostname
        host = self.request_state.host(host_name)
        while not host.lock.acquire(timeout=.1):
            _cancelled(cancel)
        try:
            _cancelled(cancel)
            cached = self.request_state.cached(key)
            if cached is not None:
                return cached
            delay = max(0, host.next_request - monotonic())
            if delay > MAX_REQUEST_WAIT:
                raise LyricsSearchError(f"제공자의 요청 한도 대기 중입니다. 약 {int(delay) + 1}초 후 다시 시도하세요. / Provider rate limit: retry in {int(delay) + 1}s.")
            self._wait(delay, cancel)
            host.next_request = monotonic() + self.request_state.intervals.get(host_name, .5)
            try:
                value, raw = self._request_json(url, params, cancel)
            except HTTPError as error:
                try:
                    limited = error.code == 429 or error.code in {500, 502, 503, 504}
                    if limited:
                        retry = self._retry_after(error.headers, self.request_state.cooldown_delay)
                        host.next_request = max(host.next_request, monotonic() + retry)
                        raise LyricsSearchError(f"제공자의 요청 한도 또는 일시적 오류입니다. 약 {int(retry) + 1}초 후 다시 시도하거나 다른 제공자를 선택하세요. / Provider rate limit or temporary failure; retry in {int(retry) + 1}s or use another provider. (HTTP {error.code})") from error
                    if error.code == 404:
                        raise LyricsSearchError("가사를 찾을 수 없습니다. / Lyrics were not found.") from error
                    raise LyricsSearchError(f"제공자 응답 오류 / Provider HTTP error: {error.code}") from error
                finally:
                    error.close()
            self.request_state.remember(key, raw, 600)
            return value
        finally:
            host.lock.release()

    def _request_json(self, url, params, cancel):
        request = Request(url + "?" + urlencode(params), headers={
            "User-Agent": f"PlaylistCanvas/{__version__} (https://github.com/AlFarrizi-Studio/LiriQo)",
            "Accept": "application/json",
        })
        try:
            with urlopen(request, timeout=30 if url == LIRIQO_API else 10) as response:
                chunks, size = [], 0
                while chunk := response.read(64 * 1024):
                    _cancelled(cancel)
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise LyricsSearchError("가사 응답이 너무 큽니다. / Lyric response is too large.")
                    chunks.append(chunk)
            _cancelled(cancel)
            raw = b"".join(chunks)
            return json.loads(raw.decode("utf-8-sig")), raw
        except HTTPError:
            raise  # Retry-After and cooldown are handled by the host request gate.
        except (URLError, TimeoutError, OSError) as error:
            raise LyricsSearchError("연결할 수 없습니다. 인터넷 연결을 확인하고 다시 시도하세요. / Cannot connect; check your connection and retry.") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise LyricsSearchError("제공자의 응답을 읽을 수 없습니다. / Invalid provider response.") from error

    def search(self, query: LyricsSearchQuery, cancel: Event | None = None) -> LyricsSearchBatch:
        query.validate()
        _cancelled(cancel)
        params = {"title": query.title.strip()}
        if query.artist.strip():
            params["artist"] = query.artist.strip()
        if query.duration:
            params["duration"] = query.duration
        data = self._get_json(LIRIQO_API, params, cancel)
        _cancelled(cancel)
        if not isinstance(data, dict) or not isinstance(data.get("tracks"), list):
            raise LyricsSearchError("LiriQo 응답을 읽을 수 없습니다. / Invalid LiriQo response.")
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise LyricsSearchError("Invalid LiriQo metadata.")
        results = []
        for index, row in enumerate(data["tracks"][:100]):
            _cancelled(cancel)
            if not isinstance(row, dict):
                continue
            provider, timing = row.get("provider"), row.get("syncLevel")
            if provider not in PROVIDERS or timing not in {"syllable", "word", "line", "plain"}:
                continue
            if query.provider != "all" and provider != query.provider:
                continue
            if query.synced_only and timing == "plain":
                continue
            if not any(row.get(key) for key in ("ttml", "lrc", "timed", "plain")):
                continue
            # Metadata belongs to the recording resolved by LiriQo, shared by all variants.
            result = LyricsSearchResult(
                provider, f"{data.get('videoId', '')}:{index}",
                str(metadata.get("title") or query.title),
                str(metadata.get("artist") or query.artist), str(metadata.get("album") or ""),
                self._duration(metadata.get("duration")), timing, payload=row)
            # Album is a local filter: the API has no documented album parameter.
            if query.album.strip() and _normal(query.album) not in _normal(result.album):
                continue
            results.append(result)
        quality = {"syllable": 4, "word": 3, "line": 2, "plain": 1}
        results.sort(key=lambda result: (quality[result.timing], self._score(query, result)), reverse=True)
        _cancelled(cancel)
        return LyricsSearchBatch(results)

    @staticmethod
    def _duration(value, divisor=1):
        try:
            duration = float(value) / divisor
            return duration if isfinite(duration) and 0 <= duration <= 86400 else 0
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _score(query, result):
        score = 4 * SequenceMatcher(None, _normal(query.title), _normal(result.title)).ratio()
        for value, candidate, weight in ((query.artist, result.artist, 2), (query.album, result.album, 1)):
            if value.strip():
                score += weight * SequenceMatcher(None, _normal(value), _normal(candidate)).ratio()
        if query.duration and result.duration:
            score += max(0, 1 - abs(query.duration - result.duration) / 15)
        return score

    def fetch(self, result: LyricsSearchResult, cancel: Event | None = None) -> FoundLyrics:
        _cancelled(cancel)
        if result.provider not in PROVIDERS:
            raise LyricsSearchError("Unknown lyric provider.")
        payload = result.payload
        if payload:
            if payload.get("ttml"):
                content, format_name = payload["ttml"], "ttml"
            elif payload.get("timed") and result.timing in {"word", "syllable"}:
                content, format_name = self._timed_ttml(payload, cancel), "ttml"
            elif payload.get("lrc"):
                content, format_name = payload["lrc"], "lrc"
            elif payload.get("timed"):
                content, format_name = self._timed_ttml(payload, cancel), "ttml"
            else:
                content, format_name = payload.get("plain"), "txt"
        else:
            content = result.synced_lyrics or result.plain_lyrics
            format_name = "lrc" if result.synced_lyrics else "txt"
        if not isinstance(content, str) or not content.strip():
            raise LyricsSearchError("이 결과에는 제공되는 가사가 없습니다. 다른 곡이나 제공자를 선택하세요. / No lyrics are available for this recording; select another result or provider.")
        if len(content.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise LyricsSearchError("Lyric response is too large.")
        try:
            cues = LyricsService.parse(content, "." + format_name) if format_name != "txt" else []
            if format_name != "txt" and not cues:
                raise LyricsError("No timed lyric cues were found.")
            # Catch invalid timings before they can make a project unsaveable.
            if cues:
                PlaylistTrack.from_dict(PlaylistTrack("", result.title, lyrics=cues).to_dict())
        except (LyricsError, TypeError, ValueError) as error:
            raise LyricsSearchError(f"가사 시간 정보를 읽을 수 없습니다. / Invalid lyric timings: {error}") from error
        _cancelled(cancel)
        return FoundLyrics(result, format_name, content, cues)

    @staticmethod
    def _timed_ttml(payload, cancel):
        """Serialize LiriQo's millisecond timings without estimating word times."""
        lines = payload.get("timed")
        if not isinstance(lines, list) or len(lines) > 20000:
            raise LyricsSearchError("Invalid LiriQo timed lyrics.")
        root = ET.Element("tt", {"xmlns": "http://www.w3.org/ns/ttml"})
        body = ET.SubElement(root, "body")
        global_words = payload.get("words")

        def timestamp(value):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise LyricsSearchError("Invalid LiriQo timestamp.")
            return f"{value / 1000:.6f}s"

        for index, line in enumerate(lines):
            _cancelled(cancel)
            if not isinstance(line, dict) or not isinstance(line.get("text"), str):
                raise LyricsSearchError("Invalid LiriQo lyric line.")
            text = line["text"]
            if not text.strip():
                continue
            element = ET.SubElement(body, "p", {
                "begin": timestamp(line.get("start")), "end": timestamp(line.get("end"))})
            if line["end"] < line["start"]:
                raise LyricsSearchError("Invalid LiriQo lyric interval.")
            words = line.get("words")
            if words is None and isinstance(global_words, list):
                line_id = line.get("id", index)
                if isinstance(line_id, int) and not isinstance(line_id, bool) and 0 <= line_id < len(global_words):
                    words = global_words[line_id]
            if words is None:
                words = []
            if not isinstance(words, list):
                raise LyricsSearchError("Invalid LiriQo word timings.")
            offset, previous, previous_end = 0, None, line["start"]
            for word in words:
                if not isinstance(word, dict) or not isinstance(word.get("text"), str) or not word["text"]:
                    raise LyricsSearchError("Invalid LiriQo lyric word.")
                position = text.find(word["text"], offset)
                if position < 0:
                    raise LyricsSearchError("LiriQo word does not match lyric text.")
                timestamp(word.get("start"))
                timestamp(word.get("end"))
                if not previous_end <= word["start"] <= word["end"] <= line["end"]:
                    raise LyricsSearchError("Invalid LiriQo word interval.")
                gap = text[offset:position]
                if previous is None:
                    element.text = gap
                else:
                    previous.tail = gap
                previous = ET.SubElement(element, "span", {
                    "begin": timestamp(word["start"] - line["start"]),
                    "end": timestamp(word["end"] - line["start"])})
                previous.text = word["text"]
                offset = position + len(word["text"])
                previous_end = word["end"]
            if previous is None:
                element.text = text
            else:
                previous.tail = text[offset:]
        return ET.tostring(root, encoding="unicode")

    @staticmethod
    def save(document: FoundLyrics, path: str | Path) -> Path:
        """Preserve the provider's original format and replace the target atomically."""
        target = Path(path).expanduser()
        if target.suffix.lower() != "." + document.format:
            raise LyricsSearchError("선택한 가사 형식의 확장자를 사용하세요. / Keep the original lyric file extension.")
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=target.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(document.content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
            return target.resolve()
        except OSError as error:
            raise LyricsSearchError(f"가사를 저장할 수 없습니다. / Could not save lyrics: {error}") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
