"""Remux the experimental native NVENC Annex-B H.264 into a verified MP4.

Video remains H.264 bit-for-bit (FFmpeg -c:v copy); only the container
timestamps are synthesized from the specified constant source FPS.
An optional audio file is AAC-encoded, padded/trimmed to the video
duration. This is not the Music Motion Studio production export path.

Example:
    python -m tools.nvenc_probe_mux --input d3d11-nvenc-h264-sync-local.h264 \
        --output nvenc-sample.mp4 --fps 60 --expected-frames 30 \
        --validate-color-cycle --report nvenc-mp4-validation.json
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

COLOR_FIELDS = {
    "color_range": "pc",
    "color_space": "bt709",
    "color_transfer": "bt709",
    "color_primaries": "bt709",
}
# The D3D11 RTX patch test measured these GPU NV12 values. These only
# validate the CENTER of a diagnostic solid-color frame, not pixel parity.
TEST_PATTERN_YUV = (
    (0, 128, 128),      # black
    (255, 128, 128),    # white
    (54, 99, 255),     # red
    (182, 30, 12),     # green
    (18, 255, 116),    # blue
    (237, 0, 140),     # yellow
)


class VerificationError(RuntimeError):
    """An encoder output or mux validation step failed."""


def parse_fps(value: str) -> Fraction:
    try:
        fps = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise VerificationError(f"Invalid frame rate {value!r}") from exc
    if fps < 1 or fps > 240:
        raise VerificationError("Frame rate must be between 1 and 240 fps")
    return fps


def run_tool(args: list[str], *, timeout: int = 180) -> bytes:
    try:
        result = subprocess.run(
            args, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError(f"Media tool failed to launch or timed out: {args[0]}: {exc}") from exc
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace")[-3000:]
        raise VerificationError(f"{args[0]} exited {result.returncode}: {message}")
    return result.stdout


def stream_info(
    ffprobe: str, media: Path, *, raw_h264: bool,
) -> dict[str, Any]:
    args = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-count_frames", "-show_entries",
        "stream=codec_name,width,height,nb_read_frames,nb_frames,duration,"
        "r_frame_rate,avg_frame_rate,color_range,color_space,"
        "color_transfer,color_primaries",
        "-of", "json",
    ]
    if raw_h264:
        args += ["-f", "h264"]
    args.append(str(media))
    try:
        data = json.loads(run_tool(args))
        streams = data.get("streams", [])
        if len(streams) != 1:
            raise VerificationError("Expected exactly one video stream")
        return streams[0]
    except (ValueError, TypeError) as exc:
        raise VerificationError("FFprobe returned malformed stream JSON") from exc


def frame_count(stream: dict[str, Any]) -> int:
    try:
        frames = int(stream["nb_read_frames"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VerificationError("FFprobe could not count decoded frames") from exc
    if frames < 1:
        raise VerificationError("No decodable video frames")
    return frames


def check_video(stream: dict[str, Any], *, frames: int, width: int, height: int) -> None:
    if stream.get("codec_name") != "h264":
        raise VerificationError("Expected H.264 video stream")
    if stream.get("width") != width or stream.get("height") != height:
        raise VerificationError("Video resolution changed during remux")
    if frame_count(stream) != frames:
        raise VerificationError(
            f"Decoded frame count changed: {frame_count(stream)} instead of {frames}"
        )
    for key, value in COLOR_FIELDS.items():
        if stream.get(key) != value:
            raise VerificationError(
                f"Missing or incorrect {key}: {stream.get(key)!r} (expected {value!r})"
            )


def timestamps(ffprobe: str, media: Path) -> list[float]:
    try:
        data = json.loads(run_tool([
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_frames", "-show_entries",
            "frame=best_effort_timestamp_time", "-of", "json", str(media),
        ]))
        return [float(frame["best_effort_timestamp_time"]) for frame in data["frames"]]
    except (KeyError, ValueError, TypeError) as exc:
        raise VerificationError("FFprobe did not return usable decoded frame PTS") from exc


def check_cfr_pts(values: list[float], fps: Fraction, count: int) -> None:
    if len(values) != count:
        raise VerificationError("MP4 frame timestamps do not match decoded frame count")
    step = 1.0 / float(fps)
    # Allows rounding to common MP4 timescales without permitting
    # duplicated or skipped presentation timestamps.
    tolerance = min(0.001, step * 0.10)
    for i, pts in enumerate(values):
        if abs(pts - values[0] - i * step) > tolerance:
            raise VerificationError(
                f"MP4 presentation timestamp mismatch at frame {i}: {pts}"
            )


def check_test_color_cycle(
    ffmpeg: str, media: Path, *, width: int, height: int, frames: int,
) -> int:
    if width % 2 or height % 2 or width < 4 or height < 4:
        raise VerificationError("NV12 test color sampling requires even dimensions >=4")
    # Decode only a 2x2 center sample from each frame.
    # NV12 is 4 luma bytes + 1 U byte + 1 V byte per 2x2 sample.
    x = (width // 2) & ~1
    y = (height // 2) & ~1
    raw = run_tool([
        ffmpeg, "-hide_banner", "-v", "error", "-i", str(media),
        "-vf", f"crop=2:2:{x}:{y},format=nv12",
        "-pix_fmt", "nv12", "-f", "rawvideo", "pipe:1",
    ])
    if len(raw) != frames * 6:
        raise VerificationError(
            f"Color sample data length mismatch: {len(raw)} vs {frames * 6}"
        )
    max_delta = 0
    for i in range(frames):
        sample = raw[i * 6:(i + 1) * 6]
        yuv = (sample[0], sample[4], sample[5])
        expected = TEST_PATTERN_YUV[i % len(TEST_PATTERN_YUV)]
        delta = max(abs(v - ref) for v, ref in zip(yuv, expected))
        max_delta = max(max_delta, delta)
        if delta > 4:
            raise VerificationError(
                f"Encoded frame {i} center color drifted: {yuv} "
                f"vs reference {expected} (max channel delta {delta})"
            )
    return max_delta


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def remux(
    input_h264: Path, output_mp4: Path, *,
    ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
    fps: Fraction = Fraction(60), expected_frames: int | None = None,
    audio: Path | None = None, validate_color_cycle: bool = False,
) -> dict[str, Any]:
    input_h264 = Path(input_h264)
    output_mp4 = Path(output_mp4)
    if not input_h264.is_file() or input_h264.stat().st_size == 0:
        raise VerificationError(f"H.264 source file does not exist or is empty: {input_h264}")
    if input_h264.resolve() == output_mp4.resolve():
        raise VerificationError("Input and output paths must be different")
    if output_mp4.exists():
        raise VerificationError(f"Refusing to overwrite existing MP4: {output_mp4}")
    if audio is not None and not Path(audio).is_file():
        raise VerificationError(f"Audio source does not exist: {audio}")
    if expected_frames is not None and not (1 <= expected_frames <= 1_000_000):
        raise VerificationError("--expected-frames is outside the supported range")

    source = stream_info(ffprobe, input_h264, raw_h264=True)
    count = frame_count(source)
    check_video(source, frames=count, width=source["width"], height=source["height"])
    if expected_frames is not None and count != expected_frames:
        raise VerificationError(f"Expected {expected_frames} source frames, got {count}")

    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".mms-nvenc-", suffix=".mp4", dir=output_mp4.parent,
    )
    os.close(fd)
    tmp = Path(temporary_name)
    try:
        # -r placed BEFORE the raw H264 input synthesizes correct CFR PTS.
        # Never transcode the NVENC H.264 video, only repackage it.
        command = [
            ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-y",
            "-r", str(fps), "-f", "h264", "-i", str(input_h264),
        ]
        if audio is not None:
            command += ["-i", str(audio)]
        command += ["-map", "0:v:0", "-c:v", "copy"]
        if audio is not None:
            # No audio ever requires access to raw GPU video surfaces.
            # Padding short audio avoids truncating the video stream.
            command += [
                "-map", "1:a:0", "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-af", "apad",
                "-t", f"{count / float(fps):.9f}",
            ]
        else:
            command += ["-an"]
        command += ["-movflags", "+faststart", "-f", "mp4", str(tmp)]
        run_tool(command)

        result = stream_info(ffprobe, tmp, raw_h264=False)
        check_video(result, frames=count, width=source["width"], height=source["height"])
        if Fraction(result.get("avg_frame_rate", "0")) != fps:
            raise VerificationError(
                f"MP4 frame rate mismatch: {result.get('avg_frame_rate')} vs {fps}"
            )
        pts = timestamps(ffprobe, tmp)
        check_cfr_pts(pts, fps, count)
        duration = float(result.get("duration", "nan"))
        if abs(duration - count / float(fps)) > 0.01:
            raise VerificationError(f"MP4 duration mismatch: {duration} seconds")

        if audio is not None:
            info = json.loads(run_tool([
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "json", str(tmp),
            ]))
            if len(info.get("streams", [])) != 1 or \
                info["streams"][0].get("codec_name") != "aac":
                raise VerificationError("Audio input did not produce an AAC stream")

        color_delta = (
            check_test_color_cycle(
                ffmpeg, tmp, width=source["width"], height=source["height"], frames=count,
            ) if validate_color_cycle else None
        )

        # Same-directory NTFS / POSIX hard link is an atomic create-if-absent
        # operation. Never replace a user's existing target on a race.
        try:
            os.link(tmp, output_mp4)
        except FileExistsError as exc:
            raise VerificationError(f"Refusing to overwrite existing MP4: {output_mp4}") from exc

        return {
            "schema": 1,
            "status": "verified",
            "video_codec": "h264",
            "video_reencoded": False,
            "audio_codec": "aac" if audio is not None else None,
            "resolution": [source["width"], source["height"]],
            "frames_decoded": count,
            "fps": str(fps),
            "duration_seconds": duration,
            "color": {key: result.get(key) for key in COLOR_FIELDS},
            "center_color_cycle_checked": validate_color_cycle,
            "center_color_max_channel_delta": color_delta,
            "full_frame_color_parity_verified": False,
            "input_h264_sha256": sha256(input_h264),
            "output_mp4_sha256": sha256(output_mp4),
            "output_mp4": str(output_mp4),
            "production_export_changed": False,
        }
    finally:
        tmp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="NVENC Annex-B H.264 file")
    parser.add_argument("--output", required=True, type=Path, help="New verified MP4 path")
    parser.add_argument("--fps", default="60", help="CFR input FPS, e.g. 60 or 30000/1001")
    parser.add_argument("--expected-frames", type=int)
    parser.add_argument("--audio", type=Path, help="Optional soundtrack, AAC encoded")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--validate-color-cycle", action="store_true",
                        help="Compare the 6-color native diagnostic center samples (not whole frames)")
    parser.add_argument("--report", type=Path, help="Optional new verification JSON")
    args = parser.parse_args(argv)
    try:
        fps = parse_fps(args.fps)
        if args.report is not None and args.report.exists():
            raise VerificationError(f"Refusing to overwrite report: {args.report}")
        result = remux(
            args.input, args.output, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe,
            fps=fps, expected_frames=args.expected_frames, audio=args.audio,
            validate_color_cycle=args.validate_color_cycle,
        )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (VerificationError, OSError, ValueError) as exc:
        print(json.dumps({
            "schema": 1, "status": "verification_failed",
            "reason": str(exc), "production_export_changed": False,
        }, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
