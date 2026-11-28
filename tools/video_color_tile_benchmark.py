"""Same-input ABBA comparison for tiled CPU color filtering.

Synthetic RGBA images only; measure wall time and tracemalloc peak in separate
passes. No actual decoder, display FPS, RSS, GPU or whole-pipeline claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tracemalloc
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtGui import QImage
from app.video.frame_filter import _rgb_pixels, apply_color_filters


def reference(image, *, brightness, contrast, saturation, grayscale):
    """Unmodified pre-tile float expression, including conversion and alpha."""
    result = image.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = _rgb_pixels(result)
    rgb = pixels[:, :, :3].astype(np.float32)
    rgb = (rgb - 128.0) * (1.0 + float(contrast) / 100.0) + 128.0 + float(brightness) * 2.55
    effective = 0.0 if grayscale else max(0.0, min(3.0, saturation))
    if effective != 1.0:
        luminance = (
            rgb[:, :, 0:1] * 0.2126 + rgb[:, :, 1:2] * 0.7152
            + rgb[:, :, 2:3] * 0.0722
        )
        rgb = luminance + (rgb - luminance) * effective
    pixels[:, :, :3] = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    return result


def digest(image):
    return hashlib.sha256(bytes(image.constBits())).hexdigest()


def run(rounds):
    rng = np.random.default_rng(270)
    methods = {"reference": reference, "tiled": apply_color_filters}
    cases = {}
    for width, height in ((640, 360), (1920, 1080), (3840, 2160)):
        source = QImage(width, height, QImage.Format.Format_RGBA8888)
        pixels = _rgb_pixels(source)
        pixels[:] = rng.integers(0, 256, pixels.shape, dtype=np.uint8)
        del pixels
        original_hash = digest(source)
        for case, options in (
            ("saturation", dict(brightness=17.5, contrast=-35.25, saturation=2.2, grayscale=False)),
            ("grayscale", dict(brightness=10.0, contrast=20.0, saturation=1.0, grayscale=True)),
        ):
            expected = digest(reference(source, **options))
            if digest(apply_color_filters(source, **options)) != expected:
                raise RuntimeError("Warmup output differs")
            timings = {name: [] for name in methods}
            for repeat in range(rounds):
                order = ("reference", "tiled", "tiled", "reference")
                if repeat % 2:
                    order = ("tiled", "reference", "reference", "tiled")
                for name in order:
                    started = perf_counter()
                    output = methods[name](source, **options)
                    timings[name].append((perf_counter() - started) * 1000)
                    if digest(output) != expected or digest(source) != original_hash:
                        raise RuntimeError(f"Output or input changed: {width}x{height} {case} {name}")
                    del output
            summary = {}
            for name, method in methods.items():
                tracemalloc.start()
                try:
                    output = method(source, **options)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                if digest(output) != expected:
                    raise RuntimeError("Traced pass changed output")
                del output
                ordered = sorted(timings[name])
                summary[name] = {
                    "median_ms": statistics.median(ordered),
                    "p95_ms": ordered[math.ceil(.95 * len(ordered)) - 1],
                    "traced_peak_bytes": peak,
                    "samples_ms": timings[name],
                }
            key = f"{width}x{height}-{case}"
            cases[key] = {"sha256": expected, "input_unchanged": True, "methods": summary}
            print(json.dumps({key: cases[key]}), flush=True)
    return {"numpy": np.__version__, "rounds": rounds, "logical_cpus": os.cpu_count(),
            "scope": "CPU QImage conversion and color math; tracemalloc tracks Python/NumPy allocations, not Qt/native allocations, RSS or VRAM.",
            "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("Positive round count required")
    result = run(args.rounds)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
