"""Compare Rust and NumPy export PCM-window kernels, not full export speed.

Usage: python tools/benchmark_rust_export_pcm.py
Run after installing the optional Rust extension.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from statistics import median
from time import perf_counter

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.renderer.native_export import ENABLE_ENV, analyze_pcm_windows
from app.renderer.python_visualizer import PythonVisualizerRenderer


def timed_call(call, repetitions: int = 3) -> tuple[np.ndarray, float]:
    elapsed = []
    result = None
    for _ in range(repetitions):
        start = perf_counter()
        result = call()
        elapsed.append(perf_counter() - start)
    assert result is not None
    return result, median(elapsed)


def main() -> None:
    import music_motion_native  # noqa: F401

    previous = os.environ.get(ENABLE_ENV)
    try:
        rng = np.random.default_rng(20261109)
        samples = rng.normal(0, 0.12, size=8_000 * 20).astype(np.float32)
        renderer = PythonVisualizerRenderer(Path("ffmpeg"))
        tasks = {
            "RMS": lambda: renderer._analyze_rms(samples, 60, threading.Event()),
            "Waveform/96": lambda: renderer._analyze_waveform(samples, 60, 96, threading.Event()),
        }
        os.environ[ENABLE_ENV] = "1"
        if analyze_pcm_windows(samples, 60, 8000) is None:
            raise SystemExit("Native RMS function failed; benchmark would fall back")
        if analyze_pcm_windows(samples, 60, 8000, points=96) is None:
            raise SystemExit("Native waveform function failed; benchmark would fall back")

        for task_name, task in tasks.items():
            os.environ[ENABLE_ENV] = "0"
            original, numpy_time = timed_call(task)
            os.environ[ENABLE_ENV] = "1"
            accelerated, rust_time = timed_call(task)
            np.testing.assert_allclose(accelerated, original, rtol=0, atol=2e-6)
            print(
                f"{task_name}: NumPy {numpy_time:.5f} s | Rust {rust_time:.5f} s | "
                f"{numpy_time / max(1e-9, rust_time):.2f}x | "
                f"max difference {np.max(np.abs(accelerated - original)):.9g}"
            )
    finally:
        if previous is None:
            os.environ.pop(ENABLE_ENV, None)
        else:
            os.environ[ENABLE_ENV] = previous


if __name__ == "__main__":
    main()
