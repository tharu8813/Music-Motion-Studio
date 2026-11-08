"""Compare two real export profiles and optionally validate produced MP4 files.

This tool runs on the user's PC after two exports; it cannot launch the editor
or infer speedups from unchanged GitHub code. It uses only the standard library
plus optional ffprobe/ffmpeg binaries for media checks.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess
from typing import Any


STAGE_KEYS = (
    "export.total_wall_seconds",
    "export.completed_wall_seconds",
    "export.incomplete_wall_seconds",
    "export.audio_prepare_seconds",
    "export.piped_capture_session_seconds",
    "export.capture_session_seconds",
    "export.frame_generation_seconds",
    "canvas.scene_render_seconds",
    "canvas.qt_scene_paint_seconds",
    "export.frame_queue_seconds",
    "export.frame_delivery_seconds",
    "export.frame_pixel_prepare_seconds",
    "export.frame_pipe_write_seconds",
    "export.renderer_seconds",
    "ffmpeg.process_wall_seconds",
)
CPU_KEYS = ("editor", "canvas_workers", "ffmpeg", "other")


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _delta(before: float | None, after: float | None) -> dict[str, float | None]:
    return {
        "baseline": before,
        "optimized": after,
        "change_percent": (
            round(100 * (after - before) / before, 2)
            if before is not None and after is not None and before > 0 else None
        ),
    }


def _read_profile(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError(f"Not a supported export profiler report: {path}")
    if data.get("capture_status") != "export_observed":
        raise ValueError(
            f"Export not verified in {path}: {data.get('capture_status', 'missing')}"
        )
    if not isinstance(data.get("summary"), dict):
        raise ValueError(f"Missing summary in export profiler report: {path}")
    stages = data["summary"].get("stage_timings", {})
    if ("export.total_wall_seconds" in stages
            and "export.completed_wall_seconds" not in stages):
        raise ValueError(
            f"No successfully completed video export recorded in {path}; "
            "a failed/cancelled export cannot be used as a benchmark."
        )
    return data


def compare_canvas_streams(baseline: dict, optimized: dict) -> dict[str, dict]:
    """Compare same-named Canvas bands; never infer causality from I/O waits."""
    old_streams = baseline.get("canvas_streams") or {}
    new_streams = optimized.get("canvas_streams") or {}
    results: dict[str, dict] = {}
    for key in sorted(set(old_streams) | set(new_streams)):
        old, new = old_streams.get(key, {}), new_streams.get(key, {})
        old_io, new_io = old.get("writer_io") or {}, new.get("writer_io") or {}
        old_queue, new_queue = old.get("parent_job_queue") or {}, new.get("parent_job_queue") or {}
        def stage(entry: dict, name: str) -> float | None:
            return _number(
                (entry.get("stage_timings") or {}).get(name, {}).get("inclusive_wall_seconds")
            )
        results[key] = {
            "size_before": [old.get("width"), old.get("height")],
            "size_after": [new.get("width"), new.get("height")],
            "written_frames": _delta(_number(old.get("written_frames")),
                                     _number(new.get("written_frames"))),
            "pipe_write_wall_seconds": _delta(
                stage(old, "export.frame_pipe_write_seconds"),
                stage(new, "export.frame_pipe_write_seconds"),
            ),
            "pipe_pending_wait_seconds": _delta(
                _number(old_io.get("pending_wait_seconds")),
                _number(new_io.get("pending_wait_seconds")),
            ),
            "pipe_write_api_seconds": _delta(
                _number(old_io.get("write_api_seconds")),
                _number(new_io.get("write_api_seconds")),
            ),
            "pipe_write_calls": _delta(
                _number(old_io.get("write_calls")), _number(new_io.get("write_calls")),
            ),
            "parent_job_queue_put_seconds": _delta(
                _number(old_queue.get("put_wall_seconds")),
                _number(new_queue.get("put_wall_seconds")),
            ),
            "parent_job_queue_full_events": _delta(
                _number(old_queue.get("full_events")),
                _number(new_queue.get("full_events")),
            ),
            "written_mib": _delta(
                (_number(old_io.get("bytes_written")) or 0) / (1024 ** 2)
                if old_io.get("bytes_written") is not None else None,
                (_number(new_io.get("bytes_written")) or 0) / (1024 ** 2)
                if new_io.get("bytes_written") is not None else None,
            ),
        }
    return results


def compare_profiles(baseline: dict, optimized: dict) -> dict:
    """Compare measured numbers without adding overlapped/parallel wall stages."""
    first = baseline["summary"]
    second = optimized["summary"]
    stages: dict[str, dict] = {}
    for name in STAGE_KEYS:
        old_stage = first.get("stage_timings", {}).get(name, {})
        new_stage = second.get("stage_timings", {}).get(name, {})
        old = _number(old_stage.get("inclusive_wall_seconds"))
        new = _number(new_stage.get("inclusive_wall_seconds"))
        if old is not None or new is not None:
            stages[name] = {
                **_delta(old, new),
                "baseline_calls": old_stage.get("calls"),
                "optimized_calls": new_stage.get("calls"),
                "inclusive_or_parallel": True,
            }
    cpu: dict[str, dict] = {}
    for name in CPU_KEYS:
        old = _number(first.get("observed_cpu_core_seconds", {}).get(name))
        new = _number(second.get("observed_cpu_core_seconds", {}).get(name))
        if old is not None or new is not None:
            cpu[name] = _delta(old, new)
    environment_before = baseline.get("run_configuration")
    environment_after = optimized.get("run_configuration")
    warnings: list[str] = [
        "Stage timings are inclusive and may overlap; do not sum them to calculate export duration.",
        "CPU core-seconds and peak RSS cover the observed editor process lifetime, including idle periods.",
        "NVIDIA GPU/NVENC percentages describe the entire GPU, not the application.",
    ]
    if environment_before is None or environment_after is None:
        warnings.append("One profile lacks optimization settings; identical A/B conditions cannot be verified.")
    else:
        before = environment_before.get("optimization_environment", {})
        after = environment_after.get("optimization_environment", {})
        if before == after:
            warnings.append("Both runs recorded the same optimization flags; check which code/flags were tested.")
        if environment_before.get("python_version") != environment_after.get("python_version"):
            warnings.append("Python versions differ between runs.")
    if not stages.get("export.completed_wall_seconds"):
        warnings.append(
            "No successfully completed full export wall timer was recorded; "
            "only renderer time or process-lifetime samples can be compared."
        )
    if stages.get("export.incomplete_wall_seconds"):
        warnings.append("One of the runs included an incomplete video export attempt.")
    # A successful program exit does not mean the first live Canvas encoder
    # succeeded. A 12-FPS real project stalled, then transparently retried
    # with intermediate PNG files, producing two renderer_seconds attempts.
    for label, summary in (("Baseline", first), ("Optimized", second)):
        attempts = _number(
            summary.get("stage_timings", {}).get("export.renderer_seconds", {}).get("calls")
        )
        if attempts is not None and attempts > 1:
            warnings.append(
                f"{label} run contains {int(attempts)} final renderer attempts "
                "(possible live-pipe failure and intermediate-file fallback); "
                "do not interpret this as a successful single-pass performance result."
            )
    if not stages.get("export.renderer_seconds") and not stages.get("ffmpeg.process_wall_seconds"):
        warnings.append("No renderer wall-time stage was recorded; speed improvement cannot be estimated.")
    return {
        "total_export_wall_seconds": stages.get("export.completed_wall_seconds"),
        "renderer_wall_seconds": stages.get("export.renderer_seconds")
        or stages.get("ffmpeg.process_wall_seconds"),
        "stage_timings_seconds": stages,
        "canvas_streams": compare_canvas_streams(first, second),
        "observed_cpu_core_seconds": cpu,
        "peak_process_tree_rss_mib": _delta(
            _number(first.get("peak_process_tree_rss_mib")),
            _number(second.get("peak_process_tree_rss_mib")),
        ),
        "device_nvenc_mean_percent": _delta(
            _number(first.get("device_nvenc_mean_percent")),
            _number(second.get("device_nvenc_mean_percent")),
        ),
        "warnings": warnings,
        "baseline_configuration": environment_before,
        "optimized_configuration": environment_after,
    }


def _run(args: list[str], timeout: int = 120) -> bytes:
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        details = result.stderr.decode("utf-8", errors="replace")[-1600:]
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}): {details}")
    return result.stdout


def _fraction(value: object) -> Fraction | None:
    try:
        result = Fraction(str(value))
        return result if result.denominator and result > 0 else None
    except (ZeroDivisionError, ValueError, TypeError):
        return None


def inspect_video(path: Path, ffprobe: str) -> dict:
    """Read container/stream metadata without decoding hours of video."""
    payload = _run([
        ffprobe, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ])
    parsed = json.loads(payload.decode("utf-8"))
    streams = parsed.get("streams", [])
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if not videos:
        raise ValueError(f"No video stream in {path}")
    video = videos[0]
    return {
        "video_codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "pix_fmt": video.get("pix_fmt"),
        "sample_aspect_ratio": video.get("sample_aspect_ratio"),
        "fps": video.get("avg_frame_rate") or video.get("r_frame_rate"),
        "frame_count": int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else None,
        "duration_seconds": _number(parsed.get("format", {}).get("duration")) or _number(video.get("duration")),
        "video_duration_seconds": _number(video.get("duration")),
        "audio_stream_count": len(audios),
        "audio": [
            {
                "codec": stream.get("codec_name"),
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
                "duration_seconds": _number(stream.get("duration")),
            } for stream in audios
        ],
        "bytes": path.stat().st_size,
    }


def compare_media_metadata(baseline: dict, optimized: dict) -> dict:
    """Hard failures are structural regressions, not codec bitstream changes."""
    failures: list[str] = []
    warnings: list[str] = []
    for key in ("width", "height", "fps", "audio_stream_count"):
        lhs, rhs = baseline.get(key), optimized.get(key)
        if key == "fps":
            lhs, rhs = _fraction(lhs), _fraction(rhs)
        if lhs != rhs:
            failures.append(f"{key} differs: {baseline.get(key)!r} vs {optimized.get(key)!r}")
    old_frames, new_frames = baseline.get("frame_count"), optimized.get("frame_count")
    if old_frames is not None and new_frames is not None:
        if old_frames != new_frames:
            failures.append(f"Video frame count differs: {old_frames} vs {new_frames}")
    else:
        warnings.append("Container does not provide both video frame counts; exact frame count was not verified.")
    rate = _fraction(baseline.get("fps"))
    tolerance = max(0.10, float(2 / rate)) if rate else 0.10
    old_duration = _number(baseline.get("duration_seconds"))
    new_duration = _number(optimized.get("duration_seconds"))
    if old_duration is None or new_duration is None:
        warnings.append("Missing duration metadata; video length was not verified.")
    elif abs(old_duration - new_duration) > tolerance:
        failures.append(
            f"Container duration differs by {abs(old_duration - new_duration):.3f}s "
            f"(allowed {tolerance:.3f}s)"
        )
    for i, (old, new) in enumerate(zip(baseline.get("audio", []), optimized.get("audio", []))):
        for key in ("sample_rate", "channels"):
            if old.get(key) != new.get(key):
                failures.append(f"Audio stream {i} {key} differs.")
        for key in ("codec",):
            if old.get(key) != new.get(key):
                warnings.append(f"Audio stream {i} codec differs; check output settings.")
        lhs, rhs = _number(old.get("duration_seconds")), _number(new.get("duration_seconds"))
        if lhs is not None and rhs is not None and abs(lhs - rhs) > 0.10:
            failures.append(f"Audio stream {i} duration differs by {abs(lhs-rhs):.3f}s")
    for key in ("video_codec", "pix_fmt", "sample_aspect_ratio"):
        if baseline.get(key) != optimized.get(key):
            warnings.append(f"{key} differs; compare identical export settings.")
    return {
        "passed": not failures,
        "failures": failures,
        "warnings": warnings,
        "baseline": baseline,
        "optimized": optimized,
    }


def sample_frame_hash(path: Path, ffmpeg: str, seconds: float) -> str:
    """Hash a decoded frame; mismatches merit review with lossy codecs."""
    pixels = _run([
        ffmpeg, "-hide_banner", "-v", "error",
        "-ss", f"{seconds:.3f}", "-i", str(path),
        "-map", "0:v:0", "-frames:v", "1", "-an", "-sn",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ])
    if not pixels:
        raise RuntimeError(f"No decoded frame at {seconds:.3f}s in {path}")
    return sha256(pixels).hexdigest()


def compare_frame_samples(before_path: Path, after_path: Path, ffmpeg: str,
                          duration: float) -> dict:
    if duration <= 0:
        return {"status": "not_checked", "warning": "No valid video duration."}
    # Sample early, middle, and late positions. No full 36-minute decode.
    candidates = (max(0.0, min(2.0, duration * 0.02)), duration * 0.50,
                  max(0.0, duration - min(3.0, duration * 0.05)))
    timestamps = list(dict.fromkeys(round(t, 3) for t in candidates))
    pairs: list[dict] = []
    for sec in timestamps:
        old_hash = sample_frame_hash(before_path, ffmpeg, sec)
        new_hash = sample_frame_hash(after_path, ffmpeg, sec)
        pairs.append({"seconds": sec, "pixel_hash_equal": old_hash == new_hash,
                      "baseline_sha256": old_hash, "optimized_sha256": new_hash})
    matches = all(item["pixel_hash_equal"] for item in pairs)
    return {
        "status": "identical_samples" if matches else "review_samples",
        "samples": pairs,
        "warning": None if matches else (
            "Decoded frame hashes differ. This is NOT automatic proof of a visual bug: "
            "lossy encoding and frame seeking may differ. Inspect matched frames visually."
        ),
    }


def render_markdown(report: dict) -> str:
    profile = report["profile_comparison"]
    def cell(value: float | None) -> str:
        return f"{value:,.2f}" if value is not None else "N/A"
    def row(label: str, data: dict) -> str:
        change = data.get("change_percent")
        percent = f"{change:+.2f}%" if change is not None else "N/A"
        return f"| {label} | {cell(data['baseline'])} | {cell(data['optimized'])} | {percent} |"
    out = [
        "# Music Motion Studio export A/B verification",
        "",
        f"**Result: {report['verdict'].upper()}**",
        "",
        "Negative percent means a reduction; for time or CPU this is usually an improvement.",
        "Measurements were collected over real editor runs, not reproduced by this script.",
        "",
        "| Metric | Baseline | Optimized | Change |",
        "|---|---:|---:|---:|",
    ]
    if profile.get("total_export_wall_seconds"):
        out.append(row("Completed video export wall time (s)", profile["total_export_wall_seconds"]))
    if profile.get("renderer_wall_seconds"):
        out.append(row("FFmpeg renderer wall time (s)", profile["renderer_wall_seconds"]))
    for name in ("canvas.scene_render_seconds", "export.frame_pipe_write_seconds",
                 "export.frame_delivery_seconds"):
        value = profile["stage_timings_seconds"].get(name)
        if value:
            out.append(row(name + " (inclusive s)", value))
    for name in ("ffmpeg", "canvas_workers", "editor"):
        value = profile["observed_cpu_core_seconds"].get(name)
        if value:
            out.append(row(name + " observed CPU-core seconds", value))
    out.append(row("Peak process-tree RSS (MiB)", profile["peak_process_tree_rss_mib"]))
    streams = profile.get("canvas_streams") or {}
    if streams:
        out.extend([
            "", "## Canvas stream I/O and backpressure", "",
            "The pending wait is time waiting for a Win32 overlapped write to "
            "complete. It includes FFmpeg reader backpressure, not CPU time.",
            "",
            "| Stream | Before size | After size | Pipe write before (s) | "
            "Pipe write after (s) | Pending wait before (s) | Pending wait after (s) | "
            "Parent queue before (s) | Parent queue after (s) |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ])
        for name, item in sorted(streams.items()):
            lhs = item["pipe_write_wall_seconds"]["baseline"]
            rhs = item["pipe_write_wall_seconds"]["optimized"]
            lwait = item["pipe_pending_wait_seconds"]["baseline"]
            rwait = item["pipe_pending_wait_seconds"]["optimized"]
            lqueue = item["parent_job_queue_put_seconds"]["baseline"]
            rqueue = item["parent_job_queue_put_seconds"]["optimized"]
            before_size = "×".join(str(x) for x in item["size_before"])
            after_size = "×".join(str(x) for x in item["size_after"])
            out.append(
                f"| {name} | {before_size} | {after_size} | "
                f"{cell(lhs)} | {cell(rhs)} | {cell(lwait)} | {cell(rwait)} | "
                f"{cell(lqueue)} | {cell(rqueue)} |"
            )
    out.extend(["", "## Checks", ""])
    if report.get("media_comparison"):
        media = report["media_comparison"]
        out.append(f"- Stream metadata: {'PASS' if media['passed'] else 'FAIL'}")
        out.extend(f"- FAIL: {message}" for message in media["failures"])
        out.extend(f"- Warning: {message}" for message in media["warnings"])
    else:
        out.append("- Output media: not checked (provide both MP4 paths)")
    sample = report.get("frame_samples")
    if sample:
        out.append(f"- Decoded frame sample comparison: {sample['status']}")
        if sample.get("warning"):
            out.append(f"- Warning: {sample['warning']}")
    out.extend(["", "## Measurement limitations", ""])
    out.extend(f"- {warning}" for warning in profile["warnings"])
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-profile", type=Path, required=True)
    parser.add_argument("--optimized-profile", type=Path, required=True)
    parser.add_argument("--baseline-video", type=Path)
    parser.add_argument("--optimized-video", type=Path)
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe.exe path or PATH command")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg.exe path or PATH command")
    parser.add_argument("--no-frame-samples", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("build/export-ab-report.json"))
    parser.add_argument("--markdown", type=Path, default=Path("build/export-ab-report.md"))
    args = parser.parse_args(argv)
    if bool(args.baseline_video) != bool(args.optimized_video):
        parser.error("Provide both --baseline-video and --optimized-video, or neither.")
    if args.baseline_video and args.baseline_video.resolve() == args.optimized_video.resolve():
        parser.error("Baseline and optimized MP4 paths must be distinct.")
    try:
        before = _read_profile(args.baseline_profile)
        after = _read_profile(args.optimized_profile)
        comparison = compare_profiles(before, after)
        report: dict[str, Any] = {
            "schema": 1, "verdict": "measurement_only",
            "profile_comparison": comparison,
            "media_comparison": None,
            "frame_samples": None,
        }
        if args.baseline_video:
            old_media = inspect_video(args.baseline_video, args.ffprobe)
            new_media = inspect_video(args.optimized_video, args.ffprobe)
            media = compare_media_metadata(old_media, new_media)
            report["media_comparison"] = media
            report["verdict"] = "pass" if media["passed"] else "fail"
            if media["passed"] and not args.no_frame_samples:
                try:
                    report["frame_samples"] = compare_frame_samples(
                        args.baseline_video, args.optimized_video, args.ffmpeg,
                        min(old_media["duration_seconds"] or 0, new_media["duration_seconds"] or 0),
                    )
                    if report["frame_samples"]["status"] == "review_samples":
                        report["verdict"] = "review"
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                    report["frame_samples"] = {"status": "not_checked", "warning": str(error)}
                    report["verdict"] = "review"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
        print(render_markdown(report))
        print(f"JSON: {args.output}\nMarkdown: {args.markdown}")
        return 1 if report["verdict"] == "fail" else 0
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError,
            subprocess.TimeoutExpired) as error:
        parser.exit(2, f"Export A/B verification failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
