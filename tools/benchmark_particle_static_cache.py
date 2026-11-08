"""Measure Qt particle paint speed with and without cached static geometry.

Includes the same QPainter draw operations in each branch and verifies the
resulting image bytes. This is a per-Canvas-layer benchmark, NOT a full video
export benchmark. Run both modes with the same seed, size and frame times.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from app.utils import particle_painter as particle


def paint_sequence(*, cached: bool, frames: int, density: int, style: str) -> tuple[float, str]:
    particle._DISABLE_PARTICLE_STATIC_CACHE = not cached
    image = QImage(512, 288, QImage.Format.Format_ARGB32_Premultiplied)
    rect = QRectF(0, 0, 512, 288)
    # Warm up outside the measured timer, including the cached particle table.
    if cached:
        particle._particle_static_values(density, 139)
    start = perf_counter()
    for frame in range(frames):
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            particle.paint_particles(
                painter, rect, style=style, color="#00aaff",
                secondary_color="#ff44aa", density=density,
                speed=1.6, minimum_size=1.5, maximum_size=11,
                particle_opacity=0.72, direction=33, drift=0.75,
                twinkle=0.8, glow=0.55, seed=139,
                time_seconds=frame / 60,
            )
        finally:
            painter.end()
    elapsed = perf_counter() - start
    checksum = sha256(memoryview(image.constBits()).cast("B")).hexdigest()
    return elapsed, checksum


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--density", type=int, default=300)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.frames <= 5000 or not 4 <= args.density <= 500 or not 1 <= args.repeats <= 10:
        raise SystemExit("Invalid frame count, particle density or repeat count")
    cases = {}
    original = particle._DISABLE_PARTICLE_STATIC_CACHE
    try:
        for style in ("stars", "noise"):
            observations: dict[str, list[float]] = {"uncached": [], "cached": []}
            hashes = {}
            for i in range(args.repeats):
                # Alternate order so warm caches don't systematically bias one mode.
                order = (False, True) if i % 2 == 0 else (True, False)
                for cached in order:
                    elapsed, checksum = paint_sequence(
                        cached=cached, frames=args.frames,
                        density=args.density, style=style,
                    )
                    label = "cached" if cached else "uncached"
                    observations[label].append(elapsed)
                    hashes[label] = checksum
            if hashes["uncached"] != hashes["cached"]:
                raise SystemExit(f"Particle image parity failed for style {style}")
            before = median(observations["uncached"])
            after = median(observations["cached"])
            cases[style] = {
                "uncached_median_seconds": round(before, 5),
                "cached_median_seconds": round(after, 5),
                "speedup": round(before / after, 3) if after else None,
                "samples": {k: [round(v, 5) for v in vs] for k, vs in observations.items()},
                "output_sha256": hashes["cached"],
            }
    finally:
        particle._DISABLE_PARTICLE_STATIC_CACHE = original
    report = {
        "schema": 1,
        "note": "QPainter particle layer microbenchmark, not full MP4 export time.",
        "frames": args.frames,
        "density": args.density,
        "repeats": args.repeats,
        "cases": cases,
    }
    print(json.dumps(report, indent=2))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
