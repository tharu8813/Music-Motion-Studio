"""Direct lyric provider search with recording-specific downloads."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
from app.services.krc_lyrics import KRCError, krc_to_ttml

KUGOU_SEARCH_API = "https://lyrics.kugou.com/search"
KUGOU_DOWNLOAD_API = "https://lyrics.kugou.com/download"
KUGOU_CATALOG_API = "https://mobileservice.kugou.com/api/v3/search/song"
AMLL_SEARCH_API = "https://api.amll.dev/v1/lyrics/search"
AMLL_GET_API = "https://api.amll.dev/v1/lyrics/get"
LRCLIB_SEARCH_API = "https://lrclib.net/api/search"
# Historical source labels are retained for already-loaded documents only.
PROVIDERS = {
    "kugou": "KuGou", "amll": "AMLL TTML DB",
    "apple_music_liriqo": "Apple Music", "unison_community": "Unison",
    "qq_portato": "QQ Music", "kugou_legato": "KuGou",
    "lrclib_wordsync": "LRCLib (estimated word sync)", "lrclib": "LRCLIB",
    "ytm_line": "YouTube Music", "netease": "NetEase Cloud Music",
}
SEARCH_PROVIDERS = ("kugou", "amll", "lrclib")
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
            "lyrics.kugou.com": .5, "mobileservice.kugou.com": .5, "api.amll.dev": .2,
            "lrclib.net": .5,
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

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


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
        if self.provider not in {"all", *SEARCH_PROVIDERS}:
            raise LyricsSearchError("지원하지 않는 가사 제공자입니다. / Unsupported lyric search provider.")
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
    provider_counts: dict[str, int] = field(default_factory=dict)


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
                        raise LyricsSearchError(f"제공자의 요청 한도 또는 일시적 오류입니다. 약 {int(retry) + 1}초 후 다시 시도하세요. / Provider rate limit or temporary failure; retry in {int(retry) + 1}s. (HTTP {error.code})", status=error.code) from error
                    if error.code == 404:
                        raise LyricsSearchError("가사를 찾을 수 없습니다. / Lyrics were not found.", status=404) from error
                    raise LyricsSearchError(f"제공자 응답 오류 / Provider HTTP error: {error.code}") from error
                finally:
                    error.close()
            self.request_state.remember(key, raw, 600)
            return value
        finally:
            host.lock.release()

    def _request_json(self, url, params, cancel):
        request = Request(url + "?" + urlencode(params), headers={
            "User-Agent": f"PlaylistCanvas/{__version__}",
            "Accept": "application/json",
        })
        try:
            with urlopen(request, timeout=10) as response:
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
        results, warnings, counts = [], [], {}
        succeeded = 0
        for provider in SEARCH_PROVIDERS if query.provider == "all" else (query.provider,):
            _cancelled(cancel)
            try:
                search = {"kugou": self._search_kugou, "amll": self._search_amll,
                          "lrclib": self._search_lrclib}[provider]
                batch = search(query, cancel)
            except LyricsSearchError as error:
                warnings.append(f"{PROVIDERS[provider]}: {error}")
            else:
                succeeded += 1
                counts[provider] = len(batch.results)
                results.extend(batch.results)
                warnings.extend(batch.warnings)
        _cancelled(cancel)
        if not succeeded:
            raise LyricsSearchError("\n".join(warnings))
        results.sort(key=lambda result: self._score(query, result), reverse=True)
        return LyricsSearchBatch(results, warnings, counts)

    def _search_lrclib(self, query, cancel):
        # Album and duration rank recordings locally; neither constrains discovery.
        params = {"track_name": query.title.strip()}
        if query.artist.strip():
            params["artist_name"] = query.artist.strip()
        data = self._get_json(LRCLIB_SEARCH_API, params, cancel)
        if not isinstance(data, list):
            raise LyricsSearchError("LRCLIB 검색 응답을 읽을 수 없습니다. / Invalid LRCLIB search response.")
        results, seen = [], set()
        for row in data[:100]:
            _cancelled(cancel)
            if not isinstance(row, dict) or row.get("instrumental") is True:
                continue
            identifier, title = row.get("id"), row.get("trackName")
            if (isinstance(identifier, bool) or not isinstance(identifier, (str, int))
                    or not str(identifier).isdigit() or str(identifier) in seen
                    or not isinstance(title, str) or not title.strip()):
                continue
            synced, plain = row.get("syncedLyrics"), row.get("plainLyrics")
            synced = synced if isinstance(synced, str) and synced.strip() else ""
            plain = plain if isinstance(plain, str) and plain.strip() else ""
            if not (synced or plain) or (query.synced_only and not synced):
                continue
            seen.add(str(identifier))
            results.append(LyricsSearchResult("lrclib", str(identifier), title,
                row.get("artistName") if isinstance(row.get("artistName"), str) else "",
                row.get("albumName") if isinstance(row.get("albumName"), str) else "",
                self._duration(row.get("duration")), "line" if synced else "plain", synced, plain))
        # Keep the selected record's response: fetching never searches for a different version.
        return LyricsSearchBatch(results)

    def _search_kugou(self, query, cancel):
        params = {"ver": 1, "man": "yes", "client": "pc",
                  "keyword": " - ".join(part.strip() for part in (query.artist, query.title) if part.strip())}
        # User/playlist durations rank results; they must not constrain discovery.
        data = self._get_json(KUGOU_SEARCH_API, params, cancel)
        batch = self._kugou_results(data, cancel)
        if batch.results:
            return batch
        # The lyric endpoint expects catalog spellings, unlike song search.
        # Resolve aliases/localized artist tags before declaring no results.
        keyword = " ".join(part.strip() for part in (query.title, query.artist) if part.strip())
        data = self._get_json(KUGOU_CATALOG_API, {"keyword": keyword, "page": 1, "pagesize": 20}, cancel)
        if (not isinstance(data, dict) or data.get("status") != 1 or not isinstance(data.get("data"), dict)
                or not isinstance(data["data"].get("info"), list)):
            raise LyricsSearchError("KuGou 곡 검색 응답을 읽을 수 없습니다. / Invalid KuGou catalog response.")
        recordings = []
        seen = set()
        for row in data["data"]["info"][:100]:
            _cancelled(cancel)
            if not isinstance(row, dict):
                continue
            title, artist = row.get("songname"), row.get("singername")
            if not isinstance(title, str) or not title.strip() or not isinstance(artist, str) or not artist.strip():
                continue
            signature = _normal(title), _normal(artist)
            if signature in seen:
                continue
            seen.add(signature)
            recording = LyricsSearchResult("kugou", "", title, artist,
                duration=self._duration(row.get("duration")))
            recordings.append((recording, row))
        recordings.sort(key=lambda pair: self._score(query, pair[0]), reverse=True)
        # Bound catalog expansion: no unbounded fan-out or empty-result retry loop.
        for recording, row in recordings[:3]:
            _cancelled(cancel)
            resolved = {"ver": 1, "man": "yes", "client": "pc",
                        "keyword": f"{recording.artist} - {recording.title}"}
            if recording.duration:
                resolved["duration"] = round(recording.duration * 1000)
            if isinstance(row.get("hash"), str) and re.fullmatch(r"[a-fA-F0-9]{32}", row["hash"]):
                resolved["hash"] = row["hash"]
            # Do not issue the same empty request again if metadata was unchanged.
            if resolved == params:
                continue
            batch = self._kugou_results(self._get_json(KUGOU_SEARCH_API, resolved, cancel), cancel)
            if batch.results:
                return LyricsSearchBatch(batch.results, [
                    "KuGou: 곡 검색으로 제목·아티스트 표기를 확인해 가사를 찾았습니다. 곡과 길이를 확인하세요. / "
                    "Resolved catalog metadata to find lyrics; check the recording and duration."])
        return LyricsSearchBatch([], [
            "KuGou: 곡 검색까지 확인했지만 제공되는 가사를 찾지 못했습니다. 제목 또는 아티스트 표기를 바꿔 보세요. / "
            "No lyrics found after catalog search; try another title or artist spelling."])

    @staticmethod
    def _kugou_results(data, cancel):
        if not isinstance(data, dict) or data.get("status") != 200 or not isinstance(data.get("candidates"), list):
            raise LyricsSearchError("KuGou 검색 응답을 읽을 수 없습니다. / Invalid KuGou search response.")
        results, seen = [], set()
        for row in data["candidates"][:100]:
            _cancelled(cancel)
            if not isinstance(row, dict):
                continue
            identifier, accesskey = row.get("id"), row.get("accesskey")
            title, artist = row.get("song"), row.get("singer")
            if (isinstance(identifier, bool) or not isinstance(identifier, (str, int))
                    or not str(identifier).isdigit() or str(identifier) in seen
                    or not isinstance(accesskey, str) or not accesskey
                    or not isinstance(title, str) or not title.strip()):
                continue
            seen.add(str(identifier))
            results.append(LyricsSearchResult("kugou", str(identifier), title,
                artist if isinstance(artist, str) else "", duration=LyricsSearchService._duration(row.get("duration"), 1000),
                payload={"accesskey": accesskey}))
        return LyricsSearchBatch(results)

    @staticmethod
    def _amll_data(value):
        if not isinstance(value, dict) or value.get("status") != 200 or not isinstance(value.get("data"), dict):
            raise LyricsSearchError("AMLL 응답을 읽을 수 없습니다. / Invalid AMLL response.")
        return value["data"]

    def _search_amll(self, query, cancel):
        params = {"musicName": query.title.strip(), "pageSize": 50}
        if query.artist.strip():
            params["artistName"] = query.artist.strip()
        data = self._amll_data(self._get_json(AMLL_SEARCH_API, params, cancel))
        if not isinstance(data.get("items"), list):
            raise LyricsSearchError("Invalid AMLL search results.")
        warnings = []
        if not data["items"] and "artistName" in params:
            params = {key: value for key, value in params.items() if key != "artistName"}
            data = self._amll_data(self._get_json(AMLL_SEARCH_API, params, cancel))
            if not isinstance(data.get("items"), list):
                raise LyricsSearchError("Invalid AMLL search results.")
            if data["items"]:
                warnings.append("AMLL: 제목으로 검색 범위를 넓혔습니다. 아티스트를 확인하세요. / "
                                "Broadened to title search; check the artist.")

        def names(row, key):
            values = row.get(key)
            return [value for value in values if isinstance(value, str) and value.strip()] if isinstance(values, list) else []

        results, seen = [], set()
        for row in data["items"][:100]:
            _cancelled(cancel)
            if not isinstance(row, dict):
                continue
            identifier, filename = row.get("id"), row.get("filename")
            titles = names(row, "musicNames")
            if (isinstance(identifier, bool) or not isinstance(identifier, (str, int))
                    or not str(identifier).isdigit() or str(identifier) in seen or not titles
                    or not isinstance(filename, str) or not filename.endswith(".ttml")):
                continue
            seen.add(str(identifier))
            title = max(titles, key=lambda value: SequenceMatcher(None, _normal(query.title), _normal(value)).ratio())
            results.append(LyricsSearchResult("amll", str(identifier), title,
                ", ".join(names(row, "artistNames")), ", ".join(names(row, "albumNames")),
                payload={"filename": filename}))
        pagination = data.get("pagination", {})
        if not isinstance(pagination, dict):
            raise LyricsSearchError("Invalid AMLL pagination.")
        if pagination.get("hasMore") is True:
            warnings.append("AMLL: 검색 결과 일부를 표시합니다. 제목·아티스트를 더 구체적으로 입력하세요. / "
                            "Showing partial results; refine the title or artist.")
        return LyricsSearchBatch(results, warnings)

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
            score += max(-1, 1 - abs(query.duration - result.duration) / 15)
        return score

    def fetch(self, result: LyricsSearchResult, cancel: Event | None = None) -> FoundLyrics:
        _cancelled(cancel)
        if result.provider not in PROVIDERS:
            raise LyricsSearchError("Unknown lyric provider.")
        payload = result.payload
        if result.provider == "kugou" and "accesskey" in payload and "ttml" not in payload:
            data = self._get_json(KUGOU_DOWNLOAD_API, {"id": result.identifier, "accesskey": payload["accesskey"],
                "fmt": "krc", "charset": "utf8", "client": "pc", "ver": 1}, cancel)
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                raise LyricsSearchError("Invalid KuGou lyric response.")
            try:
                payload = {"ttml": krc_to_ttml(data["content"], max_bytes=MAX_RESPONSE_BYTES,
                                              check_cancel=lambda: _cancelled(cancel))}
            except KRCError as error:
                raise LyricsSearchError(f"KRC 가사를 읽을 수 없습니다. / Invalid KRC lyrics: {error}") from error
        elif result.provider == "amll" and "filename" in payload and "ttml" not in payload:
            data = self._amll_data(self._get_json(AMLL_GET_API, {"id": result.identifier}, cancel))
            if str(data.get("id")) != result.identifier or data.get("filename") != payload["filename"]:
                raise LyricsSearchError("선택한 가사와 응답이 다릅니다. 다시 검색하세요. / Lyric identity mismatch; search again.")
            payload = {"ttml": data.get("lyrics")}
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
            raise LyricsSearchError("이 결과에는 제공되는 가사가 없습니다. 다른 결과를 선택하세요. / No lyrics are available for this recording; select another result.")
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
        if result.provider in SEARCH_PROVIDERS:
            result = replace(result, timing="word" if any(cue.get("words") for cue in cues) else "line" if cues else "plain")
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
