"""Verify FFmpeg D3D11 hardware video frames can reach NVENC without hwdownload.

This capability test is NOT connected to the Music Motion Studio GPU
compositor: input frames come from FFmpeg's CPU testsrc2 generator, then
hwupload places them on the GPU. The D3D11 path never requests hwdownload
between upload and h264_nvenc.

On the user's Windows RTX 5060:
 python tools/benchmark_nvenc_d3d11_hwframes.py --width 1920 --height 1080 \
   --frames 120 --repeats 3 --output nvenc-d3d11-hwframes-local.json

Runs use isolated temporary MP4s and verify decoded H.264 frame counts with
ffprobe. No project files, export settings or permanent MP4s are modified.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import tempfile
from time import perf_counter


def build_encode_command(
    ffmpeg: str, width: int, height: int, fps: int, frames: int,
    output: Path, *, d3d11: bool, adapter_index: int,
) -> list[str]:
    """Use the same source/encoder; only D3D11 hardware-frame input differs."""
    command = [ffmpeg, "-hide_banner", "-loglevel", "verbose", "-nostdin", "-n"]
    if d3d11:
        command += [
            "-init_hw_device", f"d3d11va=mms:{adapter_index}",
            "-filter_hw_device", "mms",
        ]
    command += [
        "-f", "lavfi", "-i",
        f"testsrc2=size={width}x{height}:rate={fps}",
        "-frames:v", str(frames),
        "-vf", "format=nv12,hwupload" if d3d11 else "format=nv12",
        "-an", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "18",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-f", "mp4", str(output),
    ]
    return command


def build_probe_command(ffprobe: str, output: Path) -> list[str]:
    return [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-count_frames",
        "-show_entries",
        "stream=codec_name,nb_read_frames,width,height,pix_fmt,avg_frame_rate,color_primaries,color_transfer,color_space",
        "-of", "json", str(output),
    ]


def inspect_video(payload: str, *, width: int, height: int, frames: int, fps: int) -> dict:
    """Treat missing metadata or a truncated stream as a failed probe."""
    data = json.loads(payload)
    streams = data.get("streams", [])
    if len(streams) != 1:
        raise ValueError(f"Expected one video stream, found {len(streams)}")
    stream = streams[0]
    if stream.get("codec_name") != "h264":
        raise ValueError(f"Video codec is not H.264: {stream.get('codec_name')}")
    if stream.get("width") != width or stream.get("height") != height:
        raise ValueError("Encoded dimensions differ from requested dimensions")
    if int(stream.get("nb_read_frames", -1)) != frames:
        raise ValueError(
            f"Decoded frame count {stream.get('nb_read_frames')} != {frames}"
        )
    rate = stream.get("avg_frame_rate", "0/1")
    numerator, _, denominator = rate.partition("/")
    if not denominator or int(denominator) <= 0 or int(numerator) != fps * int(denominator):
        raise ValueError(f"Unexpected video frame rate: {rate} (expected {fps} fps)")
    return {
        "codec": stream["codec_name"],
        "width": stream["width"],
        "height": stream["height"],
        "decoded_frames": frames,
        "fps": fps,
        "decoded_pix_fmt": stream.get("pix_fmt"),
        "color_primaries": stream.get("color_primaries"),
        "color_space": stream.get("color_space"),
    }


def execute(command: list[str], timeout: float) -> dict:
    started = perf_counter()
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
        return {
            "ok": False,
            "seconds": round(perf_counter() - started, 6),
            "error": f"{type(error).__name__}: {error}",
            "stderr_excerpt": "",
        }
    stderr = result.stderr.decode("utf-8", "replace")
    return {
        "ok": result.returncode == 0,
        "seconds": round(perf_counter() - started, 6),
        "returncode": result.returncode,
        "error": "" if result.returncode == 0 else stderr[-3000:],
        # In verbose FFmpeg logs, actual hwupload and pixel format can be
        # inspected later without storing huge build logs or absolute paths.
        "stderr_excerpt": stderr[-4000:],
        "stdout": result.stdout.decode("utf-8", "replace"),
    }


def measure(
    ffmpeg: str, ffprobe: str, width: int, height: int, fps: int,
    frames: int, repeats: int, adapter_index: int,
) -> dict:
    if os.name != "nt":
        return {
            "schema": 1, "status": "requires_windows",
            "reason": "The D3D11VA hardware frames device is Windows-only",
            "production_export_changed": False,
        }
    samples: dict[str, list[dict]] = {"software_input_nvenc": [], "d3d11_frames_nvenc": []}
    with tempfile.TemporaryDirectory(prefix="mms-nvenc-d3d11-") as folder:
        directory = Path(folder)
        for iteration in range(repeats):
            order = (False, True) if iteration % 2 == 0 else (True, False)
            for d3d11 in order:
                label = "d3d11_frames_nvenc" if d3d11 else "software_input_nvenc"
                output = directory / f"{label}-{iteration}.mp4"
                result = execute(
                    build_encode_command(
                        ffmpeg, width, height, fps, frames, output,
                        d3d11=d3d11, adapter_index=adapter_index,
                    ),
                    timeout=120,
                )
                if not result["ok"]:
                    missing_ffmpeg = (
                        result["error"].startswith("FileNotFoundError:")
                    )
                    return {
                        "schema": 1,
                        "status": (
                            "ffmpeg_unavailable" if missing_ffmpeg else
                            "d3d11_hardware_frames_unavailable" if d3d11
                            else "nvenc_unavailable"
                        ),
                        "failed_mode": label,
                        "failure": result["error"],
                        "samples": samples,
                        "production_export_changed": False,
                        "direct_rust_gpu_interop": False,
                    }
                inspection = execute(build_probe_command(ffprobe, output), timeout=30)
                if not inspection["ok"]:
                    return {
                        "schema": 1,
                        "status": (
                            "ffprobe_unavailable"
                            if inspection["error"].startswith("FileNotFoundError:")
                            else "decode_validation_failed"
                        ),
                        "failed_mode": label,
                        "failure": inspection["error"],
                        "samples": samples,
                        "production_export_changed": False,
                        "direct_rust_gpu_interop": False,
                    }
                try:
                    video = inspect_video(
                        inspection["stdout"], width=width, height=height,
                        fps=fps, frames=frames,
                    )
                except (ValueError, KeyError, TypeError) as error:
                    return {
                        "schema": 1,
                        "status": "decode_validation_failed",
                        "failed_mode": label,
                        "failure": str(error),
                        "samples": samples,
                        "production_export_changed": False,
                        "direct_rust_gpu_interop": False,
                    }
                samples[label].append({
                    "seconds": result["seconds"],
                    "video": video,
                    "encoded_file_bytes": output.stat().st_size,
                    "ffmpeg_verbose_excerpt": result["stderr_excerpt"][-1800:],
                })
        baseline = median(x["seconds"] for x in samples["software_input_nvenc"])
        hardware = median(x["seconds"] for x in samples["d3d11_frames_nvenc"])
        return {
            "schema": 1,
            "status": "measured",
            "scope": "FFmpeg CPU lavfi input to NVENC; with/without D3D11VA hardware-frame upload",
            "resolution": [width, height],
            "fps": fps,
            "frames": frames,
            "repeats": repeats,
            "d3d11_adapter_index": adapter_index,
            "software_input_nvenc_median_seconds": baseline,
            "d3d11_frames_nvenc_median_seconds": hardware,
            "d3d11_vs_software_speedup": round(baseline / hardware, 4) if hardware else None,
            "both_h264_decoded_frame_counts_verified": True,
            "d3d11_pipeline_filter": "format=nv12,hwupload",
            "d3d11_gpu_to_cpu_readback_requested": False,
            "direct_rust_gpu_interop": False,
            "encoder_consumes_d3d11_hwframes": True,
            "production_export_changed": False,
            "samples": samples,
            "interpretation": (
                "D3D11 hardware frames were passed from FFmpeg hwupload to NVENC. "
                "Rust wgpu buffer-to-D3D11 sharing has NOT been implemented; "
                "the FFmpeg lavfi source still starts on CPU."
            ),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--adapter-index", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        args.width < 64 or args.width > 3840 or args.width % 2
        or args.height < 64 or args.height > 2160 or args.height % 2
        or args.fps < 1 or args.fps > 120
        or args.frames < 4 or args.frames > 600
        or args.repeats < 1 or args.repeats > 5
        or args.adapter_index < 0 or args.adapter_index > 15
    ):
        parser.error("Invalid dimensions, frame count, FPS, repetitions, or adapter index")
    report = measure(
        args.ffmpeg, args.ffprobe, args.width, args.height, args.fps,
        args.frames, args.repeats, args.adapter_index,
    )
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered, flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
