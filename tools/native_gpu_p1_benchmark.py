"""Reproducible Windows GPU A-B-B-A benchmark with full decoded media parity.

This is an opt-in developer tool, not a production rendering preference.
Default transport study: A disables two developer-only transport features;
B uses the automatic production profile. Optional alpha-trim study: A uses
unmodified automatic and B additionally enables transparent-band trimming.
Optional equal-pixels study: B attempts full immutable-byte equality before
SHA-256, using the previous verified digest only for an exact match.
Every run captures an actual short MP4 and native report.
No GPU speedup can be inferred until both project cases pass on real hardware.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys

ORDER = ("a1", "b1", "b2", "a2")
_SHA256 = re.compile(r"^SHA256=([a-fA-F0-9]{64})$")


def _execute(args: list[str], *, timeout: int = 120) -> str:
    completed = subprocess.run(
        args, capture_output=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode:
        raise RuntimeError(
            f"{Path(args[0]).name} exited {completed.returncode}: "
            + completed.stderr[-1600:]
        )
    return completed.stdout


def _sha_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_stream(ffmpeg: str, path: Path, stream: str) -> str:
    if stream == "video":
        options = ["-map", "0:v:0", "-an", "-sn", "-dn",
                   "-c:v", "rawvideo", "-pix_fmt", "yuv420p"]
    elif stream == "audio":
        options = ["-map", "0:a:0", "-vn", "-sn", "-dn",
                   "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
    else:
        raise ValueError("Unsupported decoded stream")
    line = _execute(
        [ffmpeg, "-hide_banner", "-v", "error", "-i", str(path),
         *options, "-f", "hash", "-hash", "sha256", "pipe:1"],
        timeout=300,
    ).strip()
    match = _SHA256.fullmatch(line)
    if not match:
        raise ValueError(f"Unexpected decoded {stream} SHA-256 result")
    return match.group(1).lower()


def _video_pts(ffprobe: str, path: Path) -> tuple[int, str]:
    # ffprobe's CSV can emit e.g. "0," for H.264 SEI side-data even
    # when selecting only PTS. Read structured frames to avoid false failures.
    payload = json.loads(_execute(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=best_effort_timestamp",
         "-of", "json", str(path)],
        timeout=120,
    ))
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("Cannot verify every presentation timestamp")
    values = [str(frame.get("best_effort_timestamp", ""))
              for frame in frames if isinstance(frame, dict)]
    if (len(values) != len(frames)
            or any(not value.lstrip("-").isdigit() for value in values)):
        raise ValueError("Cannot verify every presentation timestamp")
    # Preserve the entire ordered sequence, not only the first/last PTS.
    digest = sha256(("\n".join(values) + "\n").encode("ascii")).hexdigest()
    return len(values), digest


def _probe_media(ffprobe: str, path: Path, *, width: int, height: int,
                 fps: int, seconds: int) -> dict:
    payload = json.loads(_execute([
        ffprobe, "-v", "error", "-count_frames",
        "-show_entries",
        "stream=codec_type,width,height,avg_frame_rate,nb_read_frames:"
        "format=duration",
        "-of", "json", str(path),
    ]))
    streams = payload.get("streams") or []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError("Expected exactly one video stream and one audio stream")
    video = videos[0]
    if (video.get("width"), video.get("height")) != (width, height):
        raise ValueError("Decoded video dimensions differ from the benchmark contract")
    if Fraction(str(video.get("avg_frame_rate"))) != Fraction(fps, 1):
        raise ValueError("Video FPS differs from the benchmark contract")
    expected = fps * seconds
    if str(video.get("nb_read_frames")) != str(expected):
        raise ValueError("Frame count differs from the expected full CFR timeline")
    duration = float(payload.get("format", {}).get("duration", "nan"))
    if not math.isfinite(duration) or abs(duration - seconds) > 0.25:
        raise ValueError("Output duration differs from the expected smoke clip")
    pts_count, pts_hash = _video_pts(ffprobe, path)
    if pts_count != expected:
        raise ValueError("Decoded video PTS count differs from the expected timeline")
    return {
        "width": width, "height": height, "fps": fps,
        "frames": expected, "duration_seconds": duration,
        "pts_sha256": pts_hash,
    }


def _positive(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Missing benchmark metric: {label}") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"Invalid benchmark metric: {label}")
    return number


def _native_stages(native: dict) -> dict[str, float]:
    raster = native.get("raster_cache") or {}
    # Inclusive/overlapping timings are deliberately NOT summed.
    keys = {
        "frame_production_seconds": native.get("frame_production_seconds"),
        "frame_pipe_write_seconds": native.get("frame_pipe_write_seconds"),
        "frame_ack_wait_seconds": native.get("frame_ack_wait_seconds"),
        "native_stream_elapsed_seconds": native.get("native_stream_elapsed_seconds"),
        "native_stream_wall_fps": native.get("native_stream_wall_fps"),
        "qt_canvas_capture_seconds": raster.get("qt_canvas_capture_seconds"),
        "qt_canvas_paint_stage_seconds": raster.get("qt_canvas_paint_stage_seconds"),
        "qt_canvas_state_setup_seconds": raster.get("qt_canvas_state_setup_seconds"),
        "frame_sha256_wall_seconds": native.get("frame_sha256_wall_seconds"),
        "frame_sha256_equality_seconds": native.get("frame_sha256_equality_seconds"),
    }
    return {key: float(value) for key, value in keys.items()
            if isinstance(value, (float, int)) and math.isfinite(value) and value >= 0}


def _read_trial(root: Path, label: str, *, expected_source_sha256: str,
                ffprobe: str, ffmpeg: str, width: int, height: int,
                fps: int, seconds: int) -> dict:
    report = json.loads((root / f"{label}.json").read_text(encoding="utf-8"))
    native = report.get("native_report") or {}
    if (report.get("status") != "completed"
            or report.get("scope") != "short_clip_gpu_smoke"
            or report.get("source_sha256") != expected_source_sha256
            or native.get("status") != "measured"
            or native.get("production_export_scope") != "production_complete_playlist"
            or native.get("native_optimization_profile") != "automatic"
            or native.get("frames_encoded") != fps * seconds):
        raise ValueError(f"Incomplete or mismatched native export report: {label}")
    video = root / f"{label}.mp4"
    if not video.is_file() or not video.stat().st_size:
        raise ValueError(f"Missing completed MP4: {label}")
    media = _probe_media(ffprobe, video, width=width, height=height,
                         fps=fps, seconds=seconds)
    return {
        "label": label,
        "wall_seconds": _positive(report.get("complete_cli_wall_seconds"), "total wall"),
        "media": media,
        "decoded_video_sha256": _hash_stream(ffmpeg, video, "video"),
        "decoded_pcm_sha256": _hash_stream(ffmpeg, video, "audio"),
        "stage_timings": _native_stages(native),
        "frame_transport_bytes": native.get("frame_transport_bytes"),
        "frame_sha256_bytes": native.get("frame_sha256_bytes"),
        "frame_sha256_reused_bytes": native.get("frame_sha256_reused_bytes"),
        "frame_sha256_equality_checked_bytes": native.get("frame_sha256_equality_checked_bytes"),
        "frame_sha256_equality_reused_bytes": native.get("frame_sha256_equality_reused_bytes"),
        "alpha_trim_band_timing": (
            native.get("raster_cache") or {}
        ).get("alpha_trim_band_timing") or {},
    }


def _summarize(trials: list[dict]) -> dict:
    if len(trials) != 4 or tuple(row["label"] for row in trials) != ORDER:
        raise ValueError("An A-B-B-A comparison requires four ordered completed trials")
    baseline = [trials[0], trials[3]]
    candidate = [trials[1], trials[2]]
    base_seconds = sum(row["wall_seconds"] for row in baseline) / 2
    candidate_seconds = sum(row["wall_seconds"] for row in candidate) / 2
    keys = ("width", "height", "fps", "frames", "pts_sha256")
    first = trials[0]
    mismatches = [
        f"{entry['label']}: {field} differs from A1"
        for entry in trials[1:] for field in keys
        if entry["media"][field] != first["media"][field]
    ]
    for key in ("decoded_video_sha256", "decoded_pcm_sha256"):
        mismatches += [
            f"{entry['label']}: {key} differs from A1"
            for entry in trials[1:] if entry[key] != first[key]
        ]
    # 'verdict' historically reports ONLY decoded video/audio/PTS parity.
    # An all-green media check does not imply a speedup.
    if base_seconds <= 0:
        raise ValueError("Baseline wall time must be positive")
    delta_seconds = candidate_seconds - base_seconds
    performance_verdict = (
        "slower" if delta_seconds > 0 else
        "faster" if delta_seconds < 0 else "equal"
    )
    promotion_decision = (
        "reject" if mismatches or delta_seconds >= 0
        else "requires_second_project_confirmation"
    )
    summary = {
        "schema": 1,
        "verdict": "pass" if not mismatches else "fail",
        "verdict_scope": "decoded_video_audio_pts_parity_only",
        "performance_verdict": performance_verdict,
        "candidate_mean_delta_seconds": round(delta_seconds, 4),
        "automatic_promotion_decision": promotion_decision,
        "baseline_mean_seconds": round(base_seconds, 4),
        "candidate_mean_seconds": round(candidate_seconds, 4),
        "wall_time_reduction_percent": round(
            100 * (base_seconds - candidate_seconds) / base_seconds, 2
        ),
        "mismatches": mismatches,
        "trials": trials,
        "timing_notice": (
            "Qt, transport and encoder stage wall times overlap; never add stage times. "
            "Decoded SHA-256 parity compares the complete 1800-frame video and "
            "48kHz stereo PCM soundtrack, not the compressed MP4 bytes."
        ),
    }
    # Additive optional fields keep existing benchmark reports and test
    # fixtures compatible while quantifying a candidate's traffic savings.
    if all(
        isinstance(row.get("frame_transport_bytes"), (int, float))
        and row["frame_transport_bytes"] >= 0 for row in trials
    ):
        baseline_wire = sum(row["frame_transport_bytes"] for row in baseline) / 2
        candidate_wire = sum(row["frame_transport_bytes"] for row in candidate) / 2
        summary["baseline_mean_transport_bytes"] = round(baseline_wire)
        summary["candidate_mean_transport_bytes"] = round(candidate_wire)
        if baseline_wire > 0:
            summary["transport_reduction_percent"] = round(
                100 * (baseline_wire - candidate_wire) / baseline_wire, 2
            )
    if all(
        type(row.get("frame_sha256_bytes")) is int and row["frame_sha256_bytes"] >= 0
        and type(row.get("frame_sha256_equality_reused_bytes")) is int
        and row["frame_sha256_equality_reused_bytes"] >= 0
        for row in trials
    ):
        summary["baseline_mean_sha256_bytes"] = round(
            sum(row["frame_sha256_bytes"] for row in baseline) / 2
        )
        summary["candidate_mean_sha256_bytes"] = round(
            sum(row["frame_sha256_bytes"] for row in candidate) / 2
        )
        summary["candidate_mean_sha256_equality_reused_bytes"] = round(
            sum(row["frame_sha256_equality_reused_bytes"] for row in candidate) / 2
        )
    if (all(isinstance(row.get("alpha_trim_band_timing"), dict) for row in candidate)
            and any(row["alpha_trim_band_timing"] for row in candidate)):
        summary["candidate_alpha_trim_scan_seconds_mean"] = round(
            sum(sum(bucket.get("scan_seconds", 0.0) for bucket in
                    row["alpha_trim_band_timing"].values())
                for row in candidate) / 2, 4,
        )
        summary["candidate_alpha_trim_copy_seconds_mean"] = round(
            sum(sum(bucket.get("copy_seconds", 0.0) for bucket in
                    row["alpha_trim_band_timing"].values())
                for row in candidate) / 2, 4,
        )
    return summary


def build_command(args: argparse.Namespace, label: str) -> list[str]:
    cmd = [
        sys.executable, "-m", "tools.native_gpu_full_export",
        "--project", str(args.project),
        "--output", str(args.output_dir / f"{label}.mp4"),
        "--native-exe", str(args.native_exe),
        "--fps", str(args.fps), "--scale", str(args.scale),
        "--smoke-seconds", str(args.seconds),
        "--gpu-profile", "automatic",
        "--report", str(args.output_dir / f"{label}.json"),
        "--ffmpeg", str(args.ffmpeg),
    ]
    experiment = getattr(args, "experiment", "transport")
    if experiment == "transport":
        if label.startswith("a"):
            cmd.extend(["--no-gpu-shared-packet-transport",
                        "--no-gpu-parallel-packet-validation"])
    elif experiment == "alpha_trim":
        # The only difference between A and B is one opt-in Qt alpha crop.
        # The regular automatic transport and encoder remain identical.
        if label.startswith("b"):
            cmd.append("--gpu-trim-transparent-bands")
    elif experiment == "equal_pixels":
        # Full byte equality before SHA-256, no changes to Qt capture or Rust.
        if label.startswith("b"):
            cmd.append("--gpu-reuse-equal-pixels")
    else:
        raise ValueError(f"Unknown P1 experiment: {experiment}")
    return cmd


def verify_output_geometry(project_path: Path, scale: float, width: int, height: int) -> tuple[int, int]:
    """Fail before four GPU exports if Canvas scale cannot yield target pixels."""
    from app.services.project_service import ProjectError, ProjectService

    try:
        canvas = ProjectService.load(project_path).canvas
    except ProjectError as error:
        raise ValueError(f"Could not read benchmark Canvas: {error}") from error
    actual = (round(canvas.width * scale), round(canvas.height * scale))
    if actual != (width, height):
        ratio_x = width / canvas.width
        ratio_y = height / canvas.height
        suggestion = (
            f" Use --scale {ratio_x:g}."
            if math.isclose(ratio_x, ratio_y, rel_tol=0, abs_tol=1e-9)
            else " The project aspect ratio differs from the requested target."
        )
        raise ValueError(
            f"Project Canvas {canvas.width:g}x{canvas.height:g} with "
            f"--scale {scale:g} produces {actual[0]}x{actual[1]}, "
            f"not the required {width}x{height}.{suggestion}"
        )
    return actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--native-exe", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--scale", type=float, required=True,
                        help="Canvas scale producing the requested output resolution")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument(
        "--experiment", choices=("transport", "alpha_trim", "equal_pixels"), default="transport",
        help="transport baseline, developer alpha-trim, or exact BGRA equality/SHA-256 reuse",
    )
    args = parser.parse_args(argv)
    if (not args.project.is_file() or args.project.is_symlink()
            or args.project.suffix.lower() not in (".pvsproj", ".json")
            or not args.native_exe.is_file()
            or not math.isfinite(args.scale) or args.scale <= 0
            or args.width < 2 or args.height < 2
            or args.width % 2 or args.height % 2
            or not 1 <= args.fps <= 120 or not 1 <= args.seconds <= 30):
        parser.error("Invalid project, native binary or benchmark frame geometry")
    if args.output_dir.exists():
        parser.error("Output directory must be new: existing results are never overwritten")
    # The GUI Canvas is not assumed to be 1920x1080. Catch mismatches before
    # spending 4 × 30s on GPU exports and before creating any result files.
    try:
        verify_output_geometry(args.project, args.scale, args.width, args.height)
    except (ValueError, OSError) as error:
        parser.error(f"Benchmark geometry preflight failed: {error}")
    for executable in (args.ffmpeg, args.ffprobe):
        if not (Path(executable).is_file() or shutil.which(executable)):
            parser.error(f"Required FFmpeg tool is unavailable: {executable}")
    # Do not compare different code revisions hidden by local tracked changes.
    try:
        revision = _execute(["git", "rev-parse", "HEAD"]).strip()
        dirty = _execute(["git", "status", "--porcelain", "--untracked-files=no"])
    except (RuntimeError, OSError) as error:
        parser.error(f"Git revision cannot be verified: {error}")
    if dirty.strip():
        parser.error("Working tree has tracked changes; use a clean checked-out revision")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    progress: list[str] = []
    try:
        for label in ORDER:
            print(f"[P1 GPU A-B-B-A] {label.upper()}: generating 30s video", flush=True)
            _execute(build_command(args, label), timeout=3600)
            progress.append(label)
        source_sha = _sha_file(args.project)
        trials = [
            _read_trial(args.output_dir, label, expected_source_sha256=source_sha,
                        ffprobe=args.ffprobe, ffmpeg=args.ffmpeg,
                        width=args.width, height=args.height,
                        fps=args.fps, seconds=args.seconds)
            for label in ORDER
        ]
        summary = _summarize(trials)
        summary["git_revision"] = revision
        summary["experiment"] = args.experiment
        summary["native_binary_sha256"] = _sha_file(args.native_exe)
        summary["project_sha256"] = source_sha
        with (args.output_dir / "p1-summary.json").open("x", encoding="utf-8") as target:
            json.dump(summary, target, ensure_ascii=False, indent=2)
        print(json.dumps({
            "verdict": summary["verdict"],
            "verdict_scope": summary["verdict_scope"],
            "performance_verdict": summary["performance_verdict"],
            "automatic_promotion_decision": summary["automatic_promotion_decision"],
            "wall_time_reduction_percent": summary["wall_time_reduction_percent"],
            "mismatches": summary["mismatches"],
            "report": str(args.output_dir / "p1-summary.json"),
        }, ensure_ascii=False))
        return 0 if summary["verdict"] == "pass" else 1
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired,
            json.JSONDecodeError) as error:
        # Keep completed partial artifacts; never promote incomplete trials to PASS.
        with (args.output_dir / "p1-error.json").open("x", encoding="utf-8") as target:
            json.dump({"verdict": "fail", "completed_trials": progress,
                       "reason": str(error), "git_revision": revision},
                      target, ensure_ascii=False, indent=2)
        print(f"P1 benchmark failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
