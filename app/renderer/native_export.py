"""Optional Rust fast paths for export FFT and PCM analysis.

The accelerator is deliberately opt-in and is not a runtime dependency.
A missing/failed native extension always falls back to the existing NumPy
implementation. This preserves older installations and export reliability.
"""

from __future__ import annotations

import importlib
import logging
import os

import numpy as np

LOGGER = logging.getLogger(__name__)
ENABLE_ENV = "PLAYLIST_CANVAS_ENABLE_RUST_EXPORT_ANALYSIS"
_TRUE = {"1", "true", "yes", "on"}
_warned = False


def rust_export_enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() in _TRUE


def reduce_frequency_bands(
    spectra: np.ndarray,
    frequencies: np.ndarray,
    bins: tuple[np.ndarray, ...],
    probes: np.ndarray,
) -> np.ndarray | None:
    """Return rows x bands float32 values, or None for Python fallback.

    The ABI moves only contiguous float64 FFT magnitudes. It does not touch Qt,
    FFmpeg, audio timing or frame buffers. Native output is copied before the
    temporary Rust-owned bytes are released.
    """
    global _warned
    if not rust_export_enabled():
        return None
    try:
        native = importlib.import_module("music_motion_native")
        magnitude_rows = np.ascontiguousarray(spectra, dtype="<f8")
        if magnitude_rows.ndim != 2:
            raise ValueError("Expected 2D spectrum rows")
        reduced = native.reduce_spectra(
            magnitude_rows.tobytes(),
            int(magnitude_rows.shape[0]),
            int(magnitude_rows.shape[1]),
            np.asarray(frequencies, dtype=np.float64).tolist(),
            np.asarray(probes, dtype=np.float64).reshape(-1).tolist(),
            [np.asarray(band, dtype=np.int64).tolist() for band in bins],
        )
        return np.frombuffer(reduced, dtype="<f4").reshape(
            magnitude_rows.shape[0], len(bins),
        ).copy()
    except (ImportError, AttributeError, OSError, RuntimeError, ValueError, TypeError) as error:
        if not _warned:
            LOGGER.warning("Rust export analysis unavailable; using NumPy: %s", error)
            _warned = True
        return None


def analyze_pcm_windows(
    samples: np.ndarray,
    fps: int,
    sample_rate: int,
    *,
    points: int | None = None,
) -> np.ndarray | None:
    """Try native RMS or waveform analysis, returning None on any native error.

    For RMS, returns (frames,) float32; for waveforms, (frames, points) float32.
    The opt-in gate and fallback guarantee that shipping without a native wheel
    preserves the existing NumPy export behavior.
    """
    global _warned
    if not rust_export_enabled():
        return None
    try:
        if fps <= 0 or sample_rate <= 0:
            raise ValueError("Invalid audio frame rate")
        values = np.ascontiguousarray(samples, dtype="<f4")
        if values.ndim != 1:
            raise ValueError("Expected one channel of PCM samples")
        frames = max(1, (int(values.size) * int(fps) + int(sample_rate) - 1) // int(sample_rate))
        native = importlib.import_module("music_motion_native")
        if points is None:
            raw = native.pcm_rms(values.tobytes(), int(fps), int(sample_rate))
            shape = (frames,)
        else:
            if not 1 <= int(points) <= 8192:
                raise ValueError("Invalid waveform point count")
            raw = native.pcm_waveform(
                values.tobytes(), int(fps), int(sample_rate), int(points)
            )
            shape = (frames, int(points))
        result = np.frombuffer(raw, dtype="<f4")
        if result.size != int(np.prod(shape, dtype=np.int64)):
            raise ValueError("Native PCM result has unexpected shape")
        return result.reshape(shape).copy()
    except (ImportError, AttributeError, OSError, RuntimeError, ValueError, TypeError) as error:
        if not _warned:
            LOGGER.warning("Rust export PCM analysis unavailable; using NumPy: %s", error)
            _warned = True
        return None
