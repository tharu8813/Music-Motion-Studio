"""Optional Rust fast path for export-only FFT frequency-band reduction.

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
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as error:
        if not _warned:
            LOGGER.warning("Rust export analysis unavailable; using NumPy: %s", error)
            _warned = True
        return None
