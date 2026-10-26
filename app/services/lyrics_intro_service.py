"""Measure only long lyric gaps, off the paint thread; reuse the vocal separator/cache."""

from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
import logging
from pathlib import Path
import threading
import time

import numpy as np
from PySide6.QtCore import QCoreApplication, QSettings

from app.automix.analysis.basic import BasicAnalysisProvider
from app.automix.analysis.beat_this_onnx import model_path
from app.automix.analysis.vocals import (
    ONNX_MODEL_NAME, UMX_SAMPLE_RATE, OnnxVocalDetector, activity_spans, merge_spans,
)
from app.automix.cache import AnalysisCache
from app.automix.models import TrackAnalysis
from app.models.playlist import PlaylistTrack
from app.models.source import Source
from app.preview.frame_state import lyric_instrumental_windows
from app.renderer.ffmpeg_renderer import FFmpegRenderer
from app.utils.subprocess_utils import lower_thread_if_background

LOGGER = logging.getLogger(__name__)
_jobs: dict[tuple, Future] = {}
_submitted_at: dict[tuple, float] = {}
_lock = threading.Lock()
_cancel = threading.Event()
_executor = None
_detector = None
_detector_configured = None


def confirmed_instrumental_spans(windows, vocals, minimum):
    """Never turn an unmeasured interval into evidence of absent vocals."""
    quiet = []
    for start, end in windows:
        cursor = start
        for a, b in vocals:
            if b <= cursor or a >= end:
                continue
            left, right = max(start, a - 0.3), min(end, b + 0.3)
            if left - cursor >= minimum:
                quiet.append((cursor, left))
            cursor = max(cursor, right)
        if end - cursor >= minimum:
            quiet.append((cursor, end))
    return tuple(quiet)


def request_instrumental_analysis(track: PlaylistTrack, source: Source) -> Future | None:
    """Nonblocking and deduplicated for every preview/export capture of a track."""
    global _executor
    source = source.resolved_lyrics()
    if not source.subtitle_intro_midtrack:
        return None
    windows = lyric_instrumental_windows(track, source)
    if not windows or not track.file_path:
        return None
    try:
        path = Path(track.file_path).resolve()
        stat = path.stat()
    except OSError:
        return None
    configured = str(QSettings().value("export/ffmpeg_path", "") or "")
    key = (str(path), stat.st_size, stat.st_mtime_ns, windows, configured)
    with _lock:
        previous = _jobs.get(key)
        if (previous is not None and previous.done()
                and (previous.cancelled() or previous.exception() is not None or previous.result() is None)
                and time.monotonic() - _submitted_at.get(key, 0.0) >= 30.0):
            _jobs.pop(key)
        if key not in _jobs:
            if _executor is None:
                _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lyric-interludes",
                                               initializer=lower_thread_if_background)
                app = QCoreApplication.instance()
                if app is not None:
                    app.aboutToQuit.connect(_cancel.set)
            # Only finished entries can be dropped; pending work stays deduplicated.
            for old in list(_jobs):
                if len(_jobs) < 32:
                    break
                if _jobs[old].done():
                    _jobs.pop(old)
                    _submitted_at.pop(old, None)
            _jobs[key] = _executor.submit(_measure, track, windows, configured)
            _submitted_at[key] = time.monotonic()
        return _jobs[key]


def instrumental_spans(track: PlaylistTrack, source: Source):
    return completed_instrumental_spans(source, request_instrumental_analysis(track, source))


def completed_instrumental_spans(source: Source, future: Future | None):
    """Resolve a retained job without re-requesting an evicted analysis cache entry."""
    source = source.resolved_lyrics()
    if future is None or not future.done():
        return ()
    try:
        result = future.result()
    except Exception:
        return ()
    if result is None:
        return ()
    windows, blocked = result
    return confirmed_instrumental_spans(windows, blocked, source.subtitle_intro_gap)


def _measure(track, windows, configured):
    global _detector, _detector_configured
    version = "1-" + sha256(repr(windows).encode()).hexdigest()[:16]
    cache = AnalysisCache(analyzer_id="lyrics-interlude-umxhq", analyzer_version=version)
    try:
        fields = cache.load(track.file_path)
        if fields is not None:
            return tuple(map(tuple, fields["vocal_coverage"] or ())), tuple(map(tuple, fields["vocal_activity"]))
        if not model_path(ONNX_MODEL_NAME).is_file():
            raise RuntimeError("The bundled vocal model is missing")
        if _detector is None or _detector_configured != configured:
            decode = BasicAnalysisProvider(FFmpegRenderer.find_executable(configured or None))._decode_mono_pcm
            _detector = OnnxVocalDetector(decode, model_path(ONNX_MODEL_NAME))
            _detector_configured = configured
        blocked, measured = [], []
        # ponytail: gap-only separation, 12-second chunks bound memory/cancel latency.
        # Analyze full songs only if a future feature needs their vocal timeline.
        for start, end in windows:
            cursor = start
            while cursor < end:
                if _cancel.is_set():
                    return None
                length = min(12.0, end - cursor)
                pcm = _detector._decode(Path(track.file_path), _cancel, sample_rate=UMX_SAMPLE_RATE,
                                        channels=2, start=cursor, duration=length).reshape(-1, 2)
                if len(pcm) < UMX_SAMPLE_RATE * length - UMX_SAMPLE_RATE * 0.15:
                    raise RuntimeError("Incomplete audio in a lyric gap")
                stem = _detector._separate_vocals(pcm)
                blocked.extend(activity_spans(stem, pcm, UMX_SAMPLE_RATE, offset=cursor))
                # Silence is not instrumental music. Reuse the activity gate to mark it.
                frame = UMX_SAMPLE_RATE // 10
                for index in range(len(pcm) // frame):
                    block = pcm[index * frame:(index + 1) * frame]
                    if np.mean(np.square(block)) < 10 ** (-55 / 10):
                        a = cursor + index * 0.1
                        blocked.append((a, a + 0.1))
                cursor += length
            measured.append((start, end))
        coverage = merge_spans(measured, track.duration_seconds)
        vocals = merge_spans(blocked, track.duration_seconds)
        result = TrackAnalysis(track.id, track.file_path, track.duration_seconds,
                               vocal_activity=vocals, vocal_coverage=coverage,
                               analyzer_id="lyrics-interlude-umxhq", analyzer_version=version)
        try:
            cache.store(track.file_path, result)
        except OSError as error:
            LOGGER.info("Could not cache lyric interludes: %s", error)
        return coverage, vocals
    except Exception as error:
        LOGGER.warning("Lyric interlude detection unavailable for %s: %s", track.file_path, error)
        return None  # A failed measurement can be retried; it is never evidence of silence.
