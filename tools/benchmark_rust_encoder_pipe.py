"""Compare real FFmpeg encode time with 32-bit vs Rust-packed 24-bit pipe input.

This microbenchmark includes producer packing, BGR24-to-BGR0 FFmpeg\nnormalization and FFmpeg processing; it is NOT
a benchmark of Qt Canvas rendering, alpha compositing, or a whole project.
Results vary by CPU, video codec, frame content, resolution and hardware.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from statistics import median
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QColor, QImage

from app.renderer.native_export import RUST_BGR24_PIPE_ENV, native_bgr24_packer


def encode(ffmpeg: str, images: list[QImage], *, native) -> dict:
    width, height = images[0].width(), images[0].height()
    fmt = "bgr24" if native is not None else "bgr0"
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", fmt,
        "-video_size", f"{width}x{height}", "-framerate", "30",
        # Normalize the experimental 24-bit input inside FFmpeg before
        # colorspace conversion, exactly as the production filter graph does.
        "-i", "pipe:0",
        *(["-vf", "format=bgr0"] if native is not None else []),
        "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "18", "-pix_fmt", "yuv420p", "-f", "null", "-",
    ]
    source_bytes = 0
    started = perf_counter()
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None
        for image in images:
            source = memoryview(image.constBits()).cast("B")
            pixels = (
                native(source, width, height) if native is not None else source
            )
            source_bytes += len(pixels)
            process.stdin.write(pixels)
        process.stdin.close()
        error = process.stderr.read() if process.stderr is not None else b""
        result = process.wait(timeout=120)
        if result:
            raise RuntimeError(f"FFmpeg encoding failed: {error.decode('utf-8', 'replace')}")
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        if process.stderr is not None:
            process.stderr.close()
    return {
        "input_format": fmt,
        "seconds": round(perf_counter() - started, 4),
        "raw_pipe_mib": round(source_bytes / (1024 * 1024), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=os.getenv("PLAYLIST_CANVAS_TEST_FFMPEG", "ffmpeg"))
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--frames", type=int, default=90)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.width <= 0 or args.height <= 0
            or not 1 <= args.frames <= 300 or not 1 <= args.repeats <= 5):
        raise SystemExit("Invalid benchmark size, frame count or repetitions")
    if importlib.util.find_spec("music_motion_native") is None:
        raise SystemExit("Install the v0.3+ native extension before benchmarking.")
    os.environ[RUST_BGR24_PIPE_ENV] = "1"
    native = native_bgr24_packer()
    if native is None:
        raise SystemExit("Rust BGR24 packer is not available.")
    images = []
    for i in range(args.frames):
        image = QImage(args.width, args.height, QImage.Format.Format_RGB32)
        image.fill(QColor((i * 17) % 256, (i * 13) % 256, (i * 19) % 256))
        images.append(image)
    records: dict[str, list[dict]] = {"baseline": [], "rust_bgr24": []}
    # Alternate order to reduce the effect of warm caches/CPU scheduling.
    for iteration in range(args.repeats):
        labels = ("baseline", "rust_bgr24") if iteration % 2 == 0 else (
            "rust_bgr24", "baseline"
        )
        for label in labels:
            records[label].append(encode(
                args.ffmpeg, images,
                native=None if label == "baseline" else native,
            ))
    baseline = {
        **records["baseline"][0],
        "seconds": round(median(x["seconds"] for x in records["baseline"]), 4),
        "all_times_seconds": [x["seconds"] for x in records["baseline"]],
    }
    optimized = {
        **records["rust_bgr24"][0],
        "seconds": round(median(x["seconds"] for x in records["rust_bgr24"]), 4),
        "all_times_seconds": [x["seconds"] for x in records["rust_bgr24"]],
    }
    result = {
        "schema": 1,
        "note": "Measured FFmpeg encoder, raw pipe producer and normalized BGR24 input; not a whole project.",
        "width": args.width,
        "height": args.height,
        "frames": args.frames,
        "repeats": args.repeats,
        "baseline": baseline,
        "rust_bgr24": optimized,
        "speedup": round(baseline["seconds"] / optimized["seconds"], 3) if optimized["seconds"] else None,
    }
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
