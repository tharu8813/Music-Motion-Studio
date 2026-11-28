"""Same mapped-plane data: old gather + bytes vs owned view + cached indices.

CPU packing only. No Qt decoder mapping, GL transfer or presentation timing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tracemalloc
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from app.preview.gpu_texture_surface import GpuTexturePreviewSurface, _cached_plane_indices


class MappedPlane:
    def __init__(self, width, height, channels, rng):
        self.stride = ((width * channels + 63) // 64) * 64
        self.raw = rng.integers(0, 256, self.stride * height, dtype=np.uint8)

    def bytesPerLine(self, _plane):
        return self.stride

    def bits(self, _plane):
        return memoryview(self.raw)


def reference(frame, plane, width, height, out_width, out_height, *, channels=1):
    source = np.frombuffer(frame.bits(plane), dtype=np.uint8,
                           count=frame.stride * height).reshape(height, frame.stride)
    pixels = source[:, :width * channels].view(np.dtype((np.void, channels)))
    y = np.linspace(0, height - 1, out_height).astype(np.intp)
    x = np.linspace(0, width - 1, out_width).astype(np.intp)
    return pixels[y[:, None], x[None, :]].tobytes()


def run(rounds):
    rng = np.random.default_rng(274)
    methods = {"reference": reference, "owned_view": GpuTexturePreviewSurface._copy_scaled_video_plane}
    cases = {}
    for width, height, out_width, out_height in ((1920,1080,1056,594), (3840,2160,1920,1080),
                                                   (1919,1079,1055,593)):
        for channels in (1, 2):
            dims = ((width, height, out_width, out_height) if channels == 1 else
                    tuple((value + 1) // 2 for value in (width, height, out_width, out_height)))
            frame = MappedPlane(dims[0], dims[1], channels, rng)
            args = (frame, 0, *dims)
            kw = {"channels": channels}
            original_hash = hashlib.sha256(frame.raw).hexdigest()
            expected = hashlib.sha256(reference(*args, **kw)).hexdigest()
            _cached_plane_indices.cache_clear()
            for method in methods.values():
                if hashlib.sha256(method(*args, **kw)).hexdigest() != expected:
                    raise RuntimeError("Warmup pixels differ")
            timings = {name: [] for name in methods}
            for repeat in range(rounds):
                order = (("reference", "owned_view", "owned_view", "reference") if repeat % 2 == 0 else
                         ("owned_view", "reference", "reference", "owned_view"))
                for name in order:
                    start = perf_counter()
                    result = methods[name](*args, **kw)
                    timings[name].append((perf_counter() - start) * 1000)
                    if hashlib.sha256(result).hexdigest() != expected:
                        raise RuntimeError("Packed plane differs")
                    del result
            summary = {}
            for name, method in methods.items():
                tracemalloc.start()
                try:
                    result = method(*args, **kw)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                if hashlib.sha256(result).hexdigest() != expected:
                    raise RuntimeError("Traced packed plane differs")
                del result
                summary[name] = {"median_ms": statistics.median(timings[name]),
                                 "samples_ms": timings[name], "traced_peak_bytes": peak}
            if hashlib.sha256(frame.raw).hexdigest() != original_hash:
                raise RuntimeError("Source plane changed")
            owned = methods["owned_view"](*args, **kw)
            frame.raw.fill(0)
            if hashlib.sha256(owned).hexdigest() != expected:
                raise RuntimeError("Output depends on original mapped storage")
            key = f"{width}x{height}-to-{out_width}x{out_height}-channels-{channels}"
            cases[key] = {"sha256": expected, "payload_bytes": len(owned), "methods": summary}
            print(json.dumps({key: cases[key]}), flush=True)
    return {"rounds": rounds, "numpy": np.__version__,
            "scope": "Synthetic padded mapped plane packing; warmed index cache, no physical GL or decoder. Traced peak excludes retained warm cache and native allocations.",
            "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("Positive round count required")
    args.output.write_text(json.dumps(run(args.rounds), indent=2), encoding="utf-8")
