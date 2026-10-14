"""On-demand, CPU-only word alignment with a small native Whisper runtime."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import file_digest, sha256
import json
import logging
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import unicodedata
from urllib.request import Request, urlopen
from zipfile import ZipFile

from app.automix.cache import VersionedAnalysisCache
from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import FFmpegRenderer
from app.utils.subprocess_utils import hidden_process_kwargs

RUNTIME_URL = "https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.3/whisper-bin-x64.zip"
RUNTIME_SHA256 = "d824b1e37599f882b396e73f1ee0bfd5d0529f700314c48311dcbd00b803321d"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/fb5ed9369c26eaf08392504d8446da431e0fffb9/ggml-tiny-q5_1.bin"
MODEL_SHA256 = "818710568da3ca15689e31a743197b520007872ff9576237bda97bd1b469c3d7"
RUNTIME_FILES = {"whisper-cli.exe", "whisper.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu.dll"}
LOGGER = logging.getLogger(__name__)


class AlignmentCancelled(RuntimeError):
    """The user cancelled; no partial result is applied."""


@dataclass
class AlignmentResult:
    cues: list[dict[str, object]]
    matched: int
    total: int
    transcript: str = ""

    def to_cache_fields(self):
        return {"cues": self.cues, "matched": self.matched, "total": self.total, "transcript": self.transcript}

    @classmethod
    def from_cache_fields(cls, track_id, path, fields):
        track = PlaylistTrack.from_dict({"id": track_id, "file_path": path, "title": "Lyrics",
                                         "lyrics": deepcopy(fields["cues"])})
        matched, total = fields["matched"], fields["total"]
        if (not isinstance(matched, int) or isinstance(matched, bool)
                or not isinstance(total, int) or isinstance(total, bool)
                or not 0 <= matched <= total <= sum(len(cue_lines(cue)) for cue in track.lyrics)):
            raise ValueError("Invalid alignment result counts")
        transcript = fields.get("transcript", "")
        if not isinstance(transcript, str) or len(transcript) > 12000:
            raise ValueError("Invalid alignment transcript")
        return cls(track.lyrics, matched, total, transcript)


class _AlignmentCache(VersionedAnalysisCache):
    SCHEMA_VERSION = 1
    ANALYSIS_TYPE = AlignmentResult
    DIRECTORY_NAME = "lyrics-word-cache"
    LOG_LABEL = "Lyrics words"
    LOGGER = LOGGER


def _check_cancel(cancel):
    if cancel.is_set():
        raise AlignmentCancelled("Cancelled")


def _download(url, target, digest, limit, cancel, progress):
    _check_cancel(cancel)
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        request = Request(url, headers={"User-Agent": "PlaylistCanvas-Lyrics"})
        with urlopen(request, timeout=10) as response, temporary.open("wb") as output:
            size = 0
            total = int(response.headers.get("Content-Length", 0))
            checksum = sha256()
            while chunk := response.read(256 * 1024):
                _check_cancel(cancel)
                size += len(chunk)
                if size > limit:
                    raise RuntimeError("Recognition download is unexpectedly large")
                output.write(chunk)
                checksum.update(chunk)
                progress(min(1.0, size / max(1, total)), "download")
            _check_cancel(cancel)
            if checksum.hexdigest() != digest:
                raise RuntimeError("Recognition download failed SHA-256 verification")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_runtime(root: Path, cancel, progress):
    """Download only the verified CPU CLI, its four DLLs and 31 MiB model."""
    _check_cancel(cancel)
    if sys.platform != "win32":
        raise RuntimeError("Lightweight recognition currently supports Windows x64")
    root.mkdir(parents=True, exist_ok=True)
    executable, model = root / "whisper-cli.exe", root / "ggml-tiny-q5_1.bin"
    ready = root / "runtime.ready"
    if not ready.is_file() or not all((root / name).is_file() for name in RUNTIME_FILES):
        archive = root / "runtime.zip"
        _download(RUNTIME_URL, archive, RUNTIME_SHA256, 8 * 1024 * 1024, cancel,
                  lambda value, _stage: progress(value * 0.1, "download"))
        try:
            with ZipFile(archive) as package:
                for name in RUNTIME_FILES:
                    _check_cancel(cancel)
                    # Exact members, never extract arbitrary archive paths.
                    (root / name).write_bytes(package.read("Release/" + name))
            ready.write_text(RUNTIME_SHA256, encoding="ascii")
        finally:
            archive.unlink(missing_ok=True)
    _check_cancel(cancel)
    if model.is_file():
        with model.open("rb") as handle:
            verified = file_digest(handle, "sha256").hexdigest() == MODEL_SHA256
    else:
        verified = False
    if not verified:
        _download(MODEL_URL, model, MODEL_SHA256, 40 * 1024 * 1024, cancel,
                  lambda value, _stage: progress(0.1 + value * 0.9, "download"))
    return executable, model


def _run(command, cancel, timeout=180):
    """Drain pipes while polling cancellation; reap every child before returning."""
    _check_cancel(cancel)
    options = hidden_process_kwargs()
    if sys.platform == "win32":
        options["creationflags"] = options.get("creationflags", 0) | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **options) as process:
        deadline = monotonic() + timeout
        try:
            while True:
                _check_cancel(cancel)
                if monotonic() >= deadline:
                    raise RuntimeError("Audio recognition timed out")
                try:
                    _stdout, stderr = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                detail = stderr.decode("utf-8", errors="replace")[-1500:]
                raise RuntimeError(f"Audio recognition failed: {detail}")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def _normalized(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def _recognized_chars(payload, audio_start, timing_offset):
    """Preserve UTF-8 tokens; DTW output times close intervals rather than open them."""
    tokens = []
    for segment in payload["transcription"]:
        segment_end = float(segment["offsets"]["to"]) / 1000
        for token in segment.get("tokens", []):
            if int(token["id"]) >= 50257 or not token["text"]:
                continue
            anchor, probability = float(token["t_dtw"]) / 100, float(token["p"])
            onset = float(token.get("offsets", {}).get("from", segment["offsets"].get("from", 0))) / 1000
            if (not all(isfinite(value) for value in (anchor, onset, probability, segment_end))
                    or anchor < 0 or onset < 0 or not 0 <= probability <= 1):
                raise ValueError("Invalid acoustic token timing")
            tokens.append((token["text"].encode("utf-8", errors="surrogateescape"), anchor, onset, probability))
    raw, byte_times = bytearray(), []
    previous, group_start = 0.0, 0.0
    for index, (token, end, onset, probability) in enumerate(tokens):
        if index and end < tokens[index - 1][1]:
            raise ValueError("Acoustic timestamps run backwards")
        if index == 0 or end > tokens[index - 1][1]:
            # ponytail: infer onsets from token output boundaries; forced alignment for vocal precision.
            # https://github.com/ggml-org/whisper.cpp/blob/v1.8.3/include/whisper.h#L136-L139
            group_start = max(previous, onset) if onset < end else previous
            previous = end
        raw.extend(token)
        byte_times.extend([(group_start + audio_start + timing_offset, end + audio_start + timing_offset,
                            probability)] * len(token))
    text = raw.decode("utf-8", errors="surrogateescape")
    characters, timing = [], []
    cursor = 0
    for char in text:
        size = len(char.encode("utf-8", errors="surrogateescape"))
        pieces = byte_times[cursor:cursor + size]
        cursor += size
        for normalized in _normalized(char):
            characters.append(normalized)
            timing.append((min(piece[0] for piece in pieces), max(piece[1] for piece in pieces),
                           min(piece[2] for piece in pieces)))
    # Equal DTW anchors occur across adjacent words; share their acoustic interval.
    index = 0
    while index < len(timing):
        following = index + 1
        a, b, _probability = timing[index]
        while following < len(timing) and timing[following][:2] == (a, b):
            following += 1
        count = following - index
        for step in range(count):
            timing[index + step] = (a + (b - a) * step / count, a + (b - a) * (step + 1) / count,
                                    timing[index + step][2])
        index = following
    return "".join(characters), timing


def cue_lines(cue):
    """Visible physical lines with offsets into the original, unchanged cue text."""
    result, offset = [], 0
    for segment in str(cue["text"]).splitlines(keepends=True):
        text = segment.rstrip("\r\n")
        if text.strip():
            result.append((len(result), offset, text))
        offset += len(segment)
    return result


def line_words(cue, offset, text):
    """Include imported spans that overlap a line, so they are never overwritten."""
    return [word for word in cue.get("words", [])
            if word["offset"] < offset + len(text) and word["offset"] + len(word["text"]) > offset]


def align_recognition(cues, payload, audio_start=0.0, timing_offset=0.0, line_positions=(0,)):
    """Match each selected physical line independently; translations cannot block originals."""
    recognized, timing = _recognized_chars(payload, audio_start, timing_offset)
    result, matched = deepcopy(cues), 0
    for position in sorted(set(line_positions)):
        entries = [(index, offset, text) for index, cue in enumerate(cues)
                   for line, offset, text in cue_lines(cue) if line == position]
        count = _align_lines(cues, result, entries, recognized, timing)
        matched += count
    return result, matched


def _align_lines(cues, result, entries, recognized, timing):
    target = "".join(_normalized(text) for _index, _offset, text in entries)
    matcher = SequenceMatcher(None, target, recognized, autojunk=False)
    correspondence = {a + index: b + index for a, b, size in matcher.get_matching_blocks()
                      for index in range(size)}
    cursor, matched = 0, 0
    for index, offset, text in entries:
        original, cue = cues[index], result[index]
        words = []
        start, end = float(cue["start"]), float(cue["end"])
        previous_end = 0.0
        tokens = [word for word in re.finditer(r"\S+", text) if _normalized(word[0])]
        matched_characters = 0
        for word in re.finditer(r"\S+", text):
            spelling = _normalized(word[0])
            positions = [correspondence.get(cursor + index) for index in range(len(spelling))]
            cursor += len(spelling)
            if not spelling:
                continue
            if any(position is None for position in positions):
                continue
            evidence = [timing[position] for position in positions]
            a, b = evidence[0][0], evidence[-1][1]
            # Allow small existing cue errors, but never match a distant repeated chorus.
            if (min(piece[2] for piece in evidence) < 0.35 or a + 0.000001 < previous_end or b <= a
                    or a < max(0, start - 1.5) or b > end + 1.5):
                continue
            # Keep cue order and manual line timing stable even when recognition drifts.
            a, b = max(start, previous_end, a), min(end, b)
            if b <= a:
                continue
            words.append({"offset": offset + word.start(), "text": word[0], "start": a, "end": b})
            matched_characters += len(spelling)
            previous_end = b
        if line_words(original, offset, text):
            continue
        coverage = matched_characters / max(1, len(_normalized(text)))
        # ponytail: exact-word anchors only; keep partial lines once a majority matches.
        if words and coverage >= 0.55 and (len(words) >= 2 or len(tokens) == 1):
            cue["words"] = sorted(cue.get("words", []) + words, key=lambda word: word["offset"])
            matched += 1
    return matched


class LyricsAlignmentService:
    def __init__(self, root: Path | None = None):
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        self.root = root or local / "PlaylistCanvas" / "tools" / "lyrics-whisper-v1.8.3"

    def analyze(self, track: PlaylistTrack, ffmpeg_path, cancel, progress, line_positions=(0,), *, use_cache=True):
        _check_cancel(cancel)
        track = PlaylistTrack.from_dict(track.to_dict())
        cues = track.lyrics
        line_positions = tuple(sorted(set(line_positions)))
        if any(not isinstance(position, int) or isinstance(position, bool) or position < 0 for position in line_positions):
            raise ValueError("Invalid lyric line selection")
        if not use_cache:
            for cue in cues:
                replaced = [word for position, offset, text in cue_lines(cue) if position in line_positions
                            for word in line_words(cue, offset, text)]
                if replaced:
                    cue["words"] = [word for word in cue["words"] if word not in replaced]
                    if not cue["words"]:
                        cue.pop("words")
        pending_lines = [(index, position) for index, cue in enumerate(cues)
                         for position, offset, text in cue_lines(cue)
                         if position in line_positions and _normalized(text) and not line_words(cue, offset, text)]
        pending = sorted({index for index, _position in pending_lines})
        total = len(pending_lines)
        if not pending:
            return AlignmentResult(cues, 0, 0)
        original_stat = Path(track.file_path).stat()
        if track.duration_seconds <= 0:
            from app.services.playlist_service import PlaylistService
            track.duration_seconds = PlaylistService._probe_duration(Path(track.file_path))
            if track.duration_seconds <= 0:
                raise RuntimeError("오디오 길이를 읽지 못했습니다. 곡 파일을 다시 연결하세요. / Cannot read audio duration.")
        identity = json.dumps([cues, line_positions, track.lyrics_timing_offset_seconds], ensure_ascii=False, sort_keys=True)
        version = "6-dtw-output-boundaries-" + sha256(identity.encode()).hexdigest()
        cache = _AlignmentCache(analyzer_id="whisper-cpp-1.8.3", analyzer_version=version)
        cached = cache.load(track.file_path) if use_cache else None
        if cached is not None and cached.get("matched", 0) > 0:
            result = AlignmentResult.from_cache_fields(track.id, track.file_path, cached)
            return result
        offset = track.lyrics_timing_offset_seconds
        groups = []
        for index in pending:
            a, b = max(0, float(cues[index]["start"]) - offset - 0.3), min(track.duration_seconds, float(cues[index]["end"]) - offset + 0.3)
            # ponytail: bounded 24-second windows; split longer cues before recognition.
            if not 0 < b - a <= 24:
                continue
            if groups and b - groups[-1][0] <= 24 and a <= groups[-1][1] + 1:
                groups[-1][1] = max(b, groups[-1][1])
                groups[-1][2].append(index)
            else:
                groups.append([a, b, [index]])
        if not groups:
            return AlignmentResult(cues, 0, total)
        ffmpeg = FFmpegRenderer.find_executable(ffmpeg_path)
        executable, model = ensure_runtime(self.root, cancel, progress)
        matched, transcripts = 0, []
        with TemporaryDirectory(prefix="lyrics-words-") as directory:
            wav, output = Path(directory) / "audio.wav", Path(directory) / "recognized"
            for number, (a, b, indices) in enumerate(groups):
                _check_cancel(cancel)
                progress(number / max(1, len(groups)), "recognize")
                _run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1", "-ss", str(a),
                      "-i", track.file_path, "-t", str(b - a), "-vn", "-threads", "1", "-ac", "1", "-ar", "16000",
                      "-c:a", "pcm_s16le", str(wav)], cancel)
                # Smart quotes and punctuation do not turn English lyrics into another language.
                original_text = " ".join(text for index in indices for position, _offset, text in cue_lines(cues[index]) if position == line_positions[0])
                letters = "".join(char for char in original_text if char.isalpha())
                language = "ko" if re.search(r"[가-힣]", letters) else "en" if letters.isascii() else "auto"
                try:
                    output.with_suffix(".json").unlink(missing_ok=True)
                    _run([str(executable), "-m", str(model), "-f", str(wav), "-of", str(output), "-ojf", "-ml", "1", "-sow",
                          "-dtw", "tiny", "-nfa", "-ng", "-t", "2", "-bs", "3", "-bo", "3", "-nf", "-sns", "-l", language,
                          "--prompt", original_text[:700]], cancel)
                    payload = json.loads(output.with_suffix(".json").read_bytes().decode("utf-8", errors="surrogateescape"), strict=False)
                    transcripts.append(" ".join(str(segment.get("text", "")) for segment in payload["transcription"]))
                    aligned, count = align_recognition([cues[index] for index in indices], payload, a, offset, line_positions)
                except AlignmentCancelled:
                    raise
                except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                    LOGGER.info("Unusable lyric recognition window: %s", error)
                    continue
                for index, cue in zip(indices, aligned):
                    cues[index] = cue
                matched += count
        _check_cancel(cancel)
        final_stat = Path(track.file_path).stat()
        if (original_stat.st_size, original_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
            raise RuntimeError("The audio changed during recognition; no result was applied")
        transcript = "\n".join(transcripts).encode("utf-8", errors="replace").decode("utf-8")[:12000]
        result = AlignmentResult(cues, matched, total, transcript)
        try:
            cache.store(track.file_path, result)
        except OSError as error:
            LOGGER.info("Could not cache word timings: %s", error)
        progress(1.0, "recognize")
        return result
