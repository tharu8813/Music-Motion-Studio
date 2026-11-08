"""Measure Windows named-pipe write chunk size with a real FFmpeg reader.

This is a controlled live-pipe producer + FFmpeg benchmark, NOT an entire MP4
export. The raw Bayer-less RGB32 input and output frames are identical for each
candidate. The QImage producer, queue and FFmpeg output are included in timing.

Run on Windows (offscreen Qt):
 python tools/benchmark_windows_pipe_chunks.py --ffmpeg C:\\ffmpeg\\ffmpeg.exe
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QColor, QImage

from app.renderer.canvas_pipe import (
    PipedCanvasStream, _WindowsNamedPipeEndpoint, _windows_pipe_write_chunk_bytes,
)


def _frame(width: int, height: int, index: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor((index * 37) % 256, (index * 83) % 256, (index * 13) % 256))
    return image


@contextmanager
def _pipe_chunk_override(mib: int):
    # The endpoint reads its chunk size only at construction.
    with patch.dict(os.environ, {"PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB": str(mib)}):
        yield


def _run_one(
    ffmpeg: str, width: int, height: int, fps: int, frames: int,
    chunk_mib: int, mode: str,
) -> dict:
    with TemporaryDirectory(prefix="mms-pipe-chunk-") as temp, _pipe_chunk_override(chunk_mib):
        stream = PipedCanvasStream(
            Path(temp), "base", fps,
            preserve_alpha=False, queue_capacity=3,
        )
        image = _frame(width, height, 0)
        stream.submit(image, 1 / fps, target_frame_count=1)
        if mode == "hash":
            output = ["-an", "-c:v", "rawvideo", "-pix_fmt", "bgr0",
                      "-f", "framemd5", "pipe:1"]
        elif mode == "encode":
            output = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                      "-pix_fmt", "yuv420p", "-f", "null", "-"]
        else:
            raise ValueError(mode)
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            *stream.input_arguments(),
            "-frames:v", str(frames), *output,
        ]
        started = monotonic()
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            for index in range(1, frames):
                stream.submit(
                    _frame(width, height, index),
                    1 / fps, target_frame_count=index + 1,
                )
            result = stream.finish()
            stdout, stderr = process.communicate(timeout=90)
            if process.returncode != 0:
                raise RuntimeError(
                    f"FFmpeg exit {process.returncode}: "
                    f"{stderr.decode('utf-8', 'replace')[-2500:]}"
                )
            if result.frame_count != frames:
                raise RuntimeError(
                    f"Expected {frames} piped frames, wrote {result.frame_count}"
                )
            lines = [
                line.strip() for line in stdout.decode("utf-8", "replace").splitlines()
                if line.strip() and not line.startswith("#")
            ]
            if mode == "hash" and len(lines) != frames:
                raise RuntimeError(
                    f"Expected {frames} decoded frame hashes, got {len(lines)}"
                )
            diagnostics = stream.io_diagnostics()
            return {
                "seconds": round(monotonic() - started, 5),
                "frames": result.frame_count,
                "raw_mib": round(frames * width * height * 4 / (1 << 20), 2),
                "write_api_calls": diagnostics.get("write_calls"),
                "wait_seconds": round(float(diagnostics.get("pending_wait_seconds", 0)), 5),
                "data_sha256": sha256("\n".join(lines).encode()).hexdigest() if mode == "hash" else None,
            }
        finally:
            if process.poll() is None:
                stream.cancel()
                process.kill()
                process.communicate(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default=os.getenv("PLAYLIST_CANVAS_TEST_FFMPEG", "ffmpeg"))
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--frames", type=int, default=36)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--chunks", default="2,4,8")
    parser.add_argument("--output", type=Path)
    a = parser.parse_args()
    if os.name != "nt":
        parser.error("This benchmark needs Windows named pipes")
    if not 1 <= a.repeats <= 5 or not 2 <= a.frames <= 150 or not 64 <= a.width <= 3840 or not 64 <= a.height <= 2160:
        parser.error("Unsupported benchmark dimensions or repetitions")
    chunks = tuple(dict.fromkeys(int(c) for c in a.chunks.split(",")))
    if len(chunks) < 2 or any(c not in (1, 2, 4, 8) for c in chunks):
        parser.error("Select at least two of 1,2,4,8 MiB")

    # Validate FFmpeg reads the same number of *exact* frames for all modes.
    hashes = {}
    for mib in chunks:
        hashes[mib] = _run_one(a.ffmpeg, a.width, a.height, a.fps, min(a.frames, 8), mib, "hash")["data_sha256"]
    if len(set(hashes.values())) != 1:
        raise SystemExit(f"ERROR: decoded frame data differs between chunks: {hashes}")

    samples: dict[int, list[dict]] = {mib: [] for mib in chunks}
    for rep in range(a.repeats):
        order = chunks if rep % 2 == 0 else chunks[::-1]
        for mib in order:
            samples[mib].append(
                _run_one(a.ffmpeg, a.width, a.height, a.fps, a.frames, mib, "encode")
            )
    medians = {str(mib): round(statistics.median(s["seconds"] for s in results), 5)
               for mib, results in samples.items()}
    report = {
        "schema": 1, "scope": "Windows PipedCanvasStream + FFmpeg synthetic H.264; not full project",
        "width": a.width, "height": a.height, "fps": a.fps,
        "frames": a.frames, "repeats": a.repeats,
        "same_frames": True, "decoded_frame_hash": next(iter(hashes.values())),
        "median_seconds": medians,
        "runs": {str(mib): results for mib, results in samples.items()},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
