"""A/B benchmark of the real per-frame FFT band reduction during export.

Run after installing the optional Rust wheel:
    python tools/benchmark_rust_export_analysis.py

This reports only the analysis kernel. It is NOT an end-to-end export speed claim.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from time import perf_counter

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.renderer.native_export import ENABLE_ENV
from app.renderer.python_visualizer import PythonVisualizerRenderer


def main() -> None:
    import music_motion_native  # noqa: F401; fail clearly if not installed

    renderer = PythonVisualizerRenderer(Path("ffmpeg"))
    rng = np.random.default_rng(42)
    samples = rng.normal(0, 0.12, size=8_000 * 20).astype(np.float32)
    times: dict[str, float] = {}
    outputs: dict[str, np.ndarray] = {}
    previous = os.environ.get(ENABLE_ENV)
    try:
        for label, enabled in (("numpy", False), ("rust", True)):
            os.environ[ENABLE_ENV] = "1" if enabled else "0"
            started = perf_counter()
            outputs[label] = renderer._analyze_levels(
                samples, 60, 96, threading.Event(),
            )
            times[label] = perf_counter() - started
    finally:
        if previous is None:
            os.environ.pop(ENABLE_ENV, None)
        else:
            os.environ[ENABLE_ENV] = previous

    error = float(np.max(np.abs(outputs["numpy"] - outputs["rust"])))
    print(f"NumPy analysis: {times['numpy']:.4f} s")
    print(f"Rust analysis:  {times['rust']:.4f} s")
    print(f"Kernel speedup: {times['numpy'] / max(times['rust'], 1e-9):.2f}x")
    print(f"Maximum absolute level difference: {error:.9g}")
    if error > 1e-6:
        raise SystemExit("Rust output differs from NumPy reference")


if __name__ == "__main__":
    main()
