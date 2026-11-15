"""Time-resolved vocal activity for AutoMix, from a separated vocal stem.

AutoMix never mixes over a singer (app/automix/candidates.py rejects any
window containing TrackAnalysis.vocal_activity), so it needs to know *when*
each track sings. Spectral heuristics were tried and removed: on real music
the "voice band" share was higher in instrumental seconds than sung ones
(basic.py's module docstring), and Sonara's vocalness scored AUC 0.55 on
3 s windows. Separating the vocal stem and measuring its level is the
reliable signal. Only the regions a transition can use are separated -- the
first and last MIX_REGION_SECONDS of each track, once; results are cached
with the rest of the analysis.

Two separators share activity_spans:

- OnnxVocalDetector (shipped, the default analyzer's): Open-Unmix ``umxhq``
  (MIT) vocals network as int8 ONNX, ~9 MB, ~1.7 s per track. Against
  htdemucs on 31 pop tracks' mix regions it agrees on 95 % of 100 ms frames
  and finds 98 % of the sung ones.
- DemucsVocalDetector: htdemucs (Meta, MIT, ~84 MB) through PyTorch, about
  0.5x real time on a 14-thread CPU. Optional, for tooling only.
"""

from __future__ import annotations

from app.utils.performance import timed

import importlib.util
import logging
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

LOGGER = logging.getLogger(__name__)

ENGINE_ID = "htdemucs"
DEMUCS_SAMPLE_RATE = 44100
MIX_REGION_SECONDS = 45.0
"""Head and tail span analyzed: covers the longest preset window (32 s) plus snapping room."""
FRAME_SECONDS = 0.1
VOCAL_RELATIVE_DB = -20.0
"""A frame sings when the vocal stem is within this many dB of the full mix..."""
VOCAL_FLOOR_DBFS = -45.0
"""...and above this absolute level (separation leakage in quiet passages stays below it)."""
MERGE_GAP_SECONDS = 0.8
"""Breaths and short pauses inside a phrase are still singing: never mix into them."""
MIN_SPAN_SECONDS = 0.3


def vocal_detection_available() -> bool:
    """Whether the optional engine is installed (never imports torch)."""
    return importlib.util.find_spec("torch") is not None and importlib.util.find_spec("demucs") is not None


def activity_spans(vocals: np.ndarray, mix: np.ndarray, sample_rate: int, offset: float = 0.0) -> list[tuple[float, float]]:
    """Sung ``(start, end)`` seconds from a separated vocal stem and its mix (mono or (n, channels))."""
    frame = int(FRAME_SECONDS * sample_rate)
    count = min(len(vocals), len(mix)) // frame
    if count == 0:
        return []

    def level_db(signal: np.ndarray) -> np.ndarray:
        blocks = np.asarray(signal[:count * frame], dtype=np.float64).reshape(count, frame, -1)
        return 10.0 * np.log10(np.mean(np.square(blocks), axis=(1, 2)) + 1e-12)

    vocal_db, mix_db = level_db(vocals), level_db(mix)
    active = (vocal_db > VOCAL_FLOOR_DBFS) & (vocal_db > mix_db + VOCAL_RELATIVE_DB)
    spans: list[list[float]] = []
    for index in np.flatnonzero(active):
        start = offset + index * FRAME_SECONDS
        if spans and start - spans[-1][1] <= MERGE_GAP_SECONDS:
            spans[-1][1] = start + FRAME_SECONDS
        else:
            spans.append([start, start + FRAME_SECONDS])
    return [(round(a, 3), round(b, 3)) for a, b in spans if b - a >= MIN_SPAN_SECONDS]


def merge_spans(spans: list[tuple[float, float]], duration: float) -> tuple[tuple[float, float], ...]:
    """Sorted, non-overlapping spans clipped to ``duration`` (TrackAnalysis's contract)."""
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        start, end = max(0.0, start), min(duration, end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((a, b) for a, b in merged)


def mix_regions(duration: float) -> list[tuple[float, float]]:
    """(start, length) of the head and tail regions, merged for a short track."""
    if duration <= 2 * MIX_REGION_SECONDS:
        return [(0.0, duration)]
    return [(0.0, MIX_REGION_SECONDS), (duration - MIX_REGION_SECONDS, MIX_REGION_SECONDS)]


def measured_regions(duration: float) -> tuple[tuple[float, float], ...]:
    """(start, end) of the spans ``detect`` separates -- TrackAnalysis.vocal_coverage."""
    return tuple((start, min(duration, start + length)) for start, length in mix_regions(duration) if length > 0.0)


class DemucsVocalDetector:
    """Loads htdemucs once (lazily, thread-safe) and separates one region at a time.

    Separation is serialized: AnalysisService analyzes tracks concurrently,
    and parallel torch passes only oversubscribe the CPU.
    """

    def __init__(self, decode: Callable[..., np.ndarray]) -> None:
        self._decode = decode
        self._model = None
        self._lock = threading.Lock()

    @timed("automix.vocal_seconds")
    def detect(self, path: Path, duration: float, cancel_event: threading.Event) -> tuple[tuple[float, float], ...]:
        spans: list[tuple[float, float]] = []
        for start, length in mix_regions(duration):
            if cancel_event.is_set():
                break
            pcm = self._decode(path, cancel_event, sample_rate=DEMUCS_SAMPLE_RATE, channels=2,
                               start=start, duration=length)
            mix = pcm.reshape(-1, 2)
            spans.extend(activity_spans(self._separate_vocals(mix), mix, DEMUCS_SAMPLE_RATE, offset=start))
        return merge_spans(spans, duration)

    def _separate_vocals(self, mix: np.ndarray) -> np.ndarray:
        import torch
        from demucs.apply import apply_model

        with self._lock:
            model = self._load()
            tensor = torch.from_numpy(np.ascontiguousarray(mix.T, dtype=np.float32))[None]
            with torch.no_grad():
                sources = apply_model(model, tensor, device="cpu", shifts=0, overlap=0.1, progress=False)
        return sources[0, model.sources.index("vocals")].numpy().T

    def _load(self):
        if self._model is None:
            from demucs.pretrained import get_model

            self._model = get_model(ENGINE_ID)
            self._model.eval()
            LOGGER.info("Loaded Demucs %s for AutoMix vocal detection", ENGINE_ID)
        return self._model


ONNX_ENGINE_ID = "umxhq"
ONNX_MODEL_NAME = "umxhq_vocals_int8.onnx"
UMX_SAMPLE_RATE = 44100
UMX_N_FFT = 4096
UMX_HOP = 1024


class OnnxVocalDetector:
    """Open-Unmix vocals (magnitude in, magnitude out) via onnxruntime; the stem keeps the mix phase.

    Same call as DemucsVocalDetector. The session loads lazily and separation is
    serialized, like Demucs: parallel passes only oversubscribe the CPU.
    """

    def __init__(self, decode: Callable[..., np.ndarray], model: Path) -> None:
        self._decode = decode
        self._model = model
        self._session = None
        self._lock = threading.Lock()

    @timed("automix.vocal_seconds")
    def detect(self, path: Path, duration: float, cancel_event: threading.Event) -> tuple[tuple[float, float], ...]:
        spans: list[tuple[float, float]] = []
        for start, length in mix_regions(duration):
            if cancel_event.is_set():
                break
            pcm = self._decode(path, cancel_event, sample_rate=UMX_SAMPLE_RATE, channels=2,
                               start=start, duration=length)
            mix = pcm.reshape(-1, 2)
            spans.extend(activity_spans(self._separate_vocals(mix), mix, UMX_SAMPLE_RATE, offset=start))
        return merge_spans(spans, duration)

    def _separate_vocals(self, mix: np.ndarray) -> np.ndarray:
        import librosa

        if len(mix) < UMX_N_FFT:
            return np.zeros_like(mix)
        spectrum = np.stack([librosa.stft(np.ascontiguousarray(mix[:, channel]), n_fft=UMX_N_FFT,
                                          hop_length=UMX_HOP, window="hann", center=False) for channel in (0, 1)])
        with self._lock:
            magnitude = self._load().run(None, {"magnitude": np.abs(spectrum).astype(np.float32)[None]})[0][0]
        stem = magnitude * np.exp(1j * np.angle(spectrum))
        return np.stack([librosa.istft(stem[channel], hop_length=UMX_HOP, window="hann", center=False,
                                       length=len(mix)) for channel in (0, 1)], axis=1)

    def _load(self):
        if self._session is None:
            import os

            import onnxruntime

            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
            # Sleep, not busy-wait, between runs: keeps cores free for Preview.
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
            self._session = onnxruntime.InferenceSession(str(self._model), options, providers=["CPUExecutionProvider"])
            LOGGER.info("Loaded Open-Unmix %s for AutoMix vocal detection", ONNX_ENGINE_ID)
        return self._session


if __name__ == "__main__":
    rate = 1000
    mix = np.full(10 * rate, 0.3)
    vocals = np.zeros_like(mix)
    vocals[2 * rate:4 * rate] = 0.2       # sung
    vocals[4 * rate + 300:6 * rate] = 0.2  # after a 0.3 s breath: same phrase
    vocals[8 * rate:8 * rate + 100] = 0.2  # 0.1 s blip: ignored
    assert activity_spans(vocals, mix, rate) == [(2.0, 6.0)], activity_spans(vocals, mix, rate)
    assert merge_spans([(5.0, 9.0), (1.0, 6.0), (9.5, 12.0)], 10.0) == ((1.0, 9.0), (9.5, 10.0))
    assert mix_regions(60.0) == [(0.0, 60.0)] and mix_regions(200.0) == [(0.0, 45.0), (155.0, 45.0)]
    print("vocals ok")
