"""A/B measure the *actual* FFmpeg Canvas overlay graph across thread counts.

Builds deterministic testsrc2 + two translucent RGBA overlays in FFmpeg and
invokes the same layered filter-graph builder as the export renderer.
Measures both filter-only and real libx264 encode time; validates frame hashes.
This tests FFmpeg filter workers, NOT any whole-project export speedup.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.renderer.ffmpeg.filter_graph import layered_filter_graph


def command(
    ffmpeg: str, width: int, height: int, fps: int,
    frames: int, workers: int, *, mode: str,
) -> list[str]:
    overlay_width, overlay_height = max(8, width // 3), max(8, height // 3)
    static = [
        (1.0, Path("overlay-a"), "pipe", width // 6, height // 8),
        (2.0, Path("overlay-b"), "pipe", width // 3, height // 4),
    ]
    graph = layered_filter_graph(
        [], static, fps, width, height, base_already_sized=True,
        base_is_cfr=True, optimize_constant_overlays=True,
    )
    # Keep the same input order as the application: base, audio, static layers.
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-threads", "0",
        "-filter_threads", str(workers),
        "-filter_complex_threads", str(workers),
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000",
        "-f", "lavfi", "-i",
        f"color=c=red@0.4:size={overlay_width}x{overlay_height}:rate={fps},format=rgba",
        "-f", "lavfi", "-i",
        f"color=c=blue@0.6:size={overlay_width}x{overlay_height}:rate={fps},format=rgba",
        "-filter_complex", graph, "-map", "[vout]",
        "-frames:v", str(frames), "-pix_fmt", "yuv420p",
    ]
    if mode == "encode":
        return [
            *args, "-an", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-f", "null", "-",
        ]
    if mode == "filter":
        return [*args, "-an", "-c:v", "rawvideo", "-f", "null", "-"]
    if mode == "hash":
        return [*args, "-an", "-c:v", "rawvideo", "-f", "framemd5", "-"]
    raise ValueError(mode)


def run(arguments: list[str], *, mode: str) -> tuple[float, str | None]:
    started = perf_counter()
    result = subprocess.run(
        arguments, check=False, capture_output=True, timeout=180,
    )
    elapsed = perf_counter() - started
    if result.returncode:
        raise RuntimeError(
            f"FFmpeg {mode} failed (code {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace')[-4000:]}"
        )
    digest = sha256(result.stdout).hexdigest() if mode == "hash" else None
    return elapsed, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workers", default="1,2,4,8")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if (not 64 <= arguments.width <= 3840 or not 64 <= arguments.height <= 2160
            or not 1 <= arguments.fps <= 120 or not 1 <= arguments.frames <= 300
            or not 1 <= arguments.repeats <= 5):
        parser.error("Invalid dimensions, frame rate, frame count or repetitions")
    try:
        workers = tuple(dict.fromkeys(int(v) for v in arguments.workers.split(",")))
    except ValueError as exc:
        parser.error(f"Invalid --workers: {exc}")
    if not workers or not all(1 <= n <= 16 for n in workers):
        parser.error("All worker counts must be 1..16")

    hashes = {
        n: run(command(
            arguments.ffmpeg, arguments.width, arguments.height,
            arguments.fps, min(arguments.frames, 12), n, mode="hash",
        ), mode="hash")[1]
        for n in workers
    }
    if len(set(hashes.values())) != 1:
        raise SystemExit(f"FAIL: FFmpeg threads changed decoded pixels: {hashes}")

    observations: dict[str, dict[int, list[float]]] = {
        "filter": {n: [] for n in workers},
        "encode": {n: [] for n in workers},
    }
    for repetition in range(arguments.repeats):
        for mode in ("filter", "encode"):
            order = workers if repetition % 2 == 0 else workers[::-1]
            for n in order:
                elapsed, _ = run(command(
                    arguments.ffmpeg, arguments.width, arguments.height,
                    arguments.fps, arguments.frames, n, mode=mode,
                ), mode=mode)
                observations[mode][n].append(elapsed)
    report = {
        "schema": 1,
        "note": "FFmpeg input/filter/encoder throughput on a deterministic synthetic multilayer Canvas, not full MP4 project export.",
        "width": arguments.width, "height": arguments.height,
        "fps": arguments.fps, "frames": arguments.frames,
        "repeats": arguments.repeats,
        "matching_frame_hash": next(iter(hashes.values())),
        "cases": {
            mode: {
                str(n): {
                    "median_seconds": round(median(observations[mode][n]), 4),
                    "runs_seconds": [round(v, 4) for v in observations[mode][n]],
                }
                for n in workers
            } for mode in observations
        },
    }
    print(json.dumps(report, indent=2), flush=True)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
