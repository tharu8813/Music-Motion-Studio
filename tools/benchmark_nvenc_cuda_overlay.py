"""NVIDIA hardware-compositing feasibility probe for GPU-first export.

Compare CPU FFmpeg 'overlay' + NVIDIA NVENC against CUDA 'overlay_cuda' +
NVIDIA NVENC on the user's own Windows RTX GPU. The graphics workload is
synthetic, but FFmpeg really runs both chains and times each complete encode.
This probe does not alter the application's export pipeline.

  python tools/benchmark_nvenc_cuda_overlay.py --ffmpeg ffmpeg --output cuda-probe.json

Both variants use the same synthetic input frames and final NVENC encoder.
GPU mode performs RGB->YUVA420P preprocessing before uploading overlays.
THIS IS NOT A PIXEL-IDENTICAL REPLACEMENT FOR RGB/alpha Canvas composition.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
import subprocess
from time import perf_counter


def ffmpeg_chain(
    ffmpeg: str, width: int, height: int, frames: int, fps: int, *, gpu: bool,
) -> list[str]:
    overlay_width = width // 3 // 2 * 2
    overlay_height = height // 3 // 2 * 2
    graph_cpu = (
        "[0:v][1:v]overlay=64:48:eof_action=pass:eval=init,"
        "format=yuv420p[vout]"
    )
    graph_gpu = (
        "[0:v]format=yuv420p,hwupload[b];"
        "[1:v]format=yuva420p,hwupload[o];"
        "[b][o]overlay_cuda=64:48:eof_action=pass:eval=init[vout]"
    )
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        *(
            ["-init_hw_device", "cuda=mms:0", "-filter_hw_device", "mms"]
            if gpu else []
        ),
        "-f", "lavfi", "-i",
        f"testsrc2=size={width}x{height}:rate={fps}",
        "-f", "lavfi", "-i",
        f"color=c=red@0.45:size={overlay_width}x{overlay_height}:rate={fps},format=rgba",
        "-filter_complex", graph_gpu if gpu else graph_cpu,
        "-map", "[vout]", "-frames:v", str(frames), "-an",
        "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18",
        "-f", "null", "-",
    ]


def run_once(cmd: list[str]) -> dict:
    started = perf_counter()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "seconds": None, "error": str(error)}
    return {
        "ok": r.returncode == 0,
        "seconds": round(perf_counter() - started, 5),
        "error": r.stderr.decode("utf-8", "replace")[-4000:],
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ffmpeg", default="ffmpeg")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--frames", type=int, default=120)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if (
        args.width % 2 or args.height % 2
        or not 128 <= args.width <= 3840 or not 128 <= args.height <= 2160
        or not 1 <= args.fps <= 120 or not 1 <= args.frames <= 300
        or not 1 <= args.repeats <= 5
    ):
        p.error("Invalid (or odd) video geometry, frame rate or repetitions")
    cpu_cmd = ffmpeg_chain(args.ffmpeg, args.width, args.height, 5, args.fps, gpu=False)
    gpu_cmd = ffmpeg_chain(args.ffmpeg, args.width, args.height, 5, args.fps, gpu=True)
    cpu_probe, gpu_probe = run_once(cpu_cmd), run_once(gpu_cmd)
    if not cpu_probe["ok"] or not gpu_probe["ok"]:
        report = {
            "schema": 1, "status": "unavailable",
            "cpu_probe": cpu_probe, "cuda_probe": gpu_probe,
            "note": "No GPU performance result. Check FFmpeg overlay_cuda, CUDA device and NVENC availability.",
        }
    else:
        samples = {"cpu_overlay": [], "cuda_overlay": []}
        for rep in range(args.repeats):
            order = (False, True) if rep % 2 == 0 else (True, False)
            for gpu in order:
                result = run_once(ffmpeg_chain(
                    args.ffmpeg, args.width, args.height, args.frames, args.fps,
                    gpu=gpu,
                ))
                if not result["ok"]:
                    raise SystemExit(
                        "A CUDA/CPU composition encode failed: " + result["error"]
                    )
                samples["cuda_overlay" if gpu else "cpu_overlay"].append(
                    result["seconds"],
                )
        cpu_time = median(samples["cpu_overlay"])
        gpu_time = median(samples["cuda_overlay"])
        report = {
            "schema": 1, "status": "measured",
            "scope": "Synthetic FFmpeg overlay and NVENC only; not full Canvas export",
            "resolution": [args.width, args.height], "fps": args.fps,
            "frames": args.frames, "repeats": args.repeats,
            "cpu_overlay_median_seconds": round(cpu_time, 5),
            "cuda_overlay_median_seconds": round(gpu_time, 5),
            "speedup": round(cpu_time / gpu_time, 3) if gpu_time else None,
            "quality_warning": (
                "CUDA path converts overlay to YUVA420P before composition; "
                "color/alpha may differ from the existing CPU RGBA compositor."
            ),
            "samples": samples,
        }
    print(json.dumps(report, indent=2), flush=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
