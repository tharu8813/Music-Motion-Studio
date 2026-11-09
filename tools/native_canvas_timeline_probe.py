"""Deterministic moving Qt Canvas/lyric-style frames for native GPU parity.

This is an opt-in *diagnostic*, NOT the Music Motion Studio production
export timeline or karaoke scheduler. Real SourceItem text and shape
painters generate a new scene at each explicitly scheduled PTS.
The original application's video/lyrics export path stays unchanged.

Capture (Qt, no NVIDIA required):
    python -m tools.native_canvas_timeline_probe capture --output-dir qt-motion --frames 6 --scale 0.5

Compare actual RTX H.264 to the CPU reference:
    python -m tools.native_canvas_timeline_probe verify \
        --timeline qt-motion/timeline.json --video qt-motion/motion.h264 \
        --report qt-motion/parity.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import numpy as np


FORMAT = "mms_qt_canvas_timeline_v1"
MAX_FRAMES = 30
MAX_DECODED_BYTES = 256 * 1024 * 1024


class TimelineValidationError(ValueError):
    """A malformed timeline, a mismatched frame, or broken encoded output."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_local_file(directory: Path, name: object, expected: str) -> Path:
    if name != expected:
        raise TimelineValidationError(f"Unexpected timeline filename: {name!r}")
    path = directory / expected
    if not path.is_file() or path.is_symlink():
        raise TimelineValidationError(f"Missing or nonregular timeline file: {path}")
    return path


def _verify_manifest(directory: Path, name: str, width: int, height: int) -> None:
    path = _safe_local_file(directory, name, name)
    if path.stat().st_size > 64 * 1024:
        raise TimelineValidationError("Qt BGRA manifest exceeds 64 KiB")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TimelineValidationError(f"Invalid Qt frame manifest {path}: {exc}") from exc
    if (doc.get("format") != "mms_canvas_bgra_layers_v1"
            or doc.get("pixel_format") != "BGRA8_STRAIGHT"
            or doc.get("width") != width or doc.get("height") != height
            or doc.get("production_export_changed") is not False):
        raise TimelineValidationError(f"Qt frame manifest dimensions or format invalid: {path}")
    layers = doc.get("layers")
    if not isinstance(layers, list) or len(layers) != 2:
        raise TimelineValidationError("Expected background plus one ordered text/shape layer")
    for i, layer in enumerate(layers):
        _safe_local_file(directory, layer.get("file"), name.removesuffix(".json") + f"-layer-{i:02d}.bgra")
        raw = (directory / layer["file"]).read_bytes()
        if len(raw) != width * height * 4:
            raise TimelineValidationError("Canvas BGRA layer byte count changed")
        if _sha256(directory / layer["file"]) != layer.get("sha256"):
            raise TimelineValidationError("Canvas BGRA checksum mismatch")


def capture_timeline(output_dir: Path, *, frames: int = 6, fps: int = 60, scale: float = 0.5) -> Path:
    """Capture changing Qt SourceItem scenes and straight-alpha BGRA packets.

    Different text cues and panel positions are deliberately scheduled per
    frame. This tests transport and temporal sequencing, not karaoke timing.
    """
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from app.canvas.live_canvas import CanvasScene
    from app.canvas.source_item import SourceItem
    from app.models.source import Source, SourceType
    from app.preview.native_canvas_layer_packets import (
        capture_native_canvas_frame, save_native_canvas_frame,
    )

    if not 3 <= frames <= MAX_FRAMES or not 1 <= fps <= 120 or not 0.25 <= scale <= 1.0:
        raise TimelineValidationError("Requires frames 3..30, FPS 1..120, scale 0.25..1.0")
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise TimelineValidationError(f"Refusing nonempty timeline output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    sequence: list[dict[str, object]] = []
    try:
        for i in range(frames):
            cue = i // 2
            # New static scene per PTS keeps the Qt reference deterministic.
            # The actual production code must resolve source/keyframe state.
            scene = CanvasScene()
            try:
                scene.addItem(SourceItem(Source(
                    SourceType.SHAPE, "Background", x=0, y=0, width=1280,
                    height=720, z_index=0, fill_color="#15283F",
                )))
                scene.addItem(SourceItem(Source(
                    SourceType.SHAPE, "Moving lyric backdrop",
                    x=210 + 24 * i, y=286, width=850, height=148,
                    z_index=12, opacity=0.28 + 0.08 * (i % 3),
                    fill_color="#3772B4",
                )))
                scene.addItem(SourceItem(Source(
                    SourceType.TEXT, "Timed diagnostic text",
                    x=200 + 12 * i, y=308, width=880, height=110,
                    z_index=13, text=f"LYRIC CUE {cue + 1} | {i:02d}",
                    font_size=51, fill_color="#FFE5AE",
                    text_stroke_width=0.0,
                )))
                packet = capture_native_canvas_frame(
                    scene, [(None, 9), (10, None)], output_scale=scale,
                )
                stem = f"frame-{i:04d}"
                manifest = save_native_canvas_frame(packet, output_dir, stem=stem)
                # QPainter provides the Qt CPU baseline for straight-alpha
                # GPU blend. Do not use a synthetic mathematical overlay as
                # the 'actual Qt' baseline.
                width, height = packet.width, packet.height
                base = packet.layers[0]
                reference = QImage(
                    base.pixels, width, height, width * 4,
                    QImage.Format.Format_ARGB32,
                ).copy()
                painter = QPainter(reference)
                try:
                    for layer in packet.layers[1:]:
                        image = QImage(
                            layer.pixels, width, height, width * 4,
                            QImage.Format.Format_ARGB32,
                        )
                        painter.setOpacity(layer.opacity)
                        painter.drawImage(0, 0, image)
                finally:
                    painter.end()
                expected = output_dir / f"reference-{i:04d}.png"
                if expected.exists() or not reference.save(str(expected), "PNG"):
                    raise TimelineValidationError("Cannot save exclusive Qt CPU reference PNG")
                sequence.append({
                    "pts": i,
                    "time_seconds": round(i / fps, 9),
                    "manifest": manifest.name,
                    "reference_png": expected.name,
                    "reference_sha256": _sha256(expected),
                    "cue_index": cue,
                })
            finally:
                scene.clear()
        first = json.loads((output_dir / sequence[0]["manifest"]).read_text(encoding="utf-8"))
        timeline = {
            "schema": 1,
            "format": FORMAT,
            "width": first["width"],
            "height": first["height"],
            "fps": fps,
            "frames": sequence,
            "qt_canvas_cpu_rasterized": True,
            "native_gpu_text_painting_implemented": False,
            "production_export_changed": False,
        }
        path = output_dir / "timeline.json"
        with path.open("x", encoding="utf-8") as writer:
            json.dump(timeline, writer, indent=2, ensure_ascii=False)
            writer.write("\n")
        return path
    except BaseException:
        # Only clean the isolated directory we created above; never touch
        # existing user files because we refuse nonempty paths up front.
        for entry in output_dir.iterdir():
            if entry.is_file() and not entry.is_symlink():
                entry.unlink()
        raise
    finally:
        _ = app


def load_timeline(path: Path) -> tuple[dict, list[Path]]:
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024:
        raise TimelineValidationError("Missing or oversized Qt timeline manifest")
    try:
        timeline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TimelineValidationError(f"Invalid timeline JSON: {exc}") from exc
    frames = timeline.get("frames")
    width, height, fps = timeline.get("width"), timeline.get("height"), timeline.get("fps")
    if (timeline.get("format") != FORMAT or timeline.get("schema") != 1
            or not isinstance(width, int) or not isinstance(height, int)
            or not 64 <= width <= 3840 or not 64 <= height <= 2160
            or width % 2 or height % 2 or not isinstance(fps, int)
            or not 1 <= fps <= 120 or not isinstance(frames, list)
            or not 3 <= len(frames) <= MAX_FRAMES
            or timeline.get("production_export_changed") is not False):
        raise TimelineValidationError("Invalid Qt timeline schema, size, FPS or frame count")
    kind = timeline.get("kind", "source_motion")
    if kind not in {"source_motion", "track_lyrics", "project_timeline"}:
        raise TimelineValidationError("Unsupported Qt Canvas timeline mode")
    start = timeline.get("start_seconds", 0.0)
    if kind == "track_lyrics" and (
        timeline.get("uses_actual_lyrics_cue_resolver") is not True
        or not isinstance(start, (int, float)) or isinstance(start, bool)
        or not math.isfinite(start) or start < 0
    ):
        raise TimelineValidationError("Invalid actual track lyric timeline header")
    if kind == "project_timeline":
        digest = timeline.get("source_project_sha256")
        duration = timeline.get("compiled_plan_duration_seconds")
        excluded = timeline.get("excluded_sources")
        excluded_count = timeline.get("excluded_source_count")
        capture_scope = timeline.get("capture_scope")
        if (not isinstance(excluded, list)
                or type(excluded_count) is not int
                or excluded_count != len(excluded)
                or not 0 <= excluded_count <= 64
                or capture_scope not in ("partial_canvas_only", "all_eligible_canvas_sources",
                                         "dynamic_overlay_audio_diagnostic")
                or (capture_scope == "partial_canvas_only") != bool(excluded)
                or timeline.get("full_project_visual_parity_verified") is not False):
            raise TimelineValidationError("Invalid project excluded dynamic source disclosure")
        dynamic = timeline.get("dynamic_sources", [])
        dynamic_origin = timeline.get("dynamic_audio_origin")
        if (not isinstance(dynamic, list)
                or ((capture_scope == "dynamic_overlay_audio_diagnostic") != bool(dynamic))
                or (dynamic_origin in ("provided_prepared_audio", "application_export_mixed_audio"))
                   != (capture_scope == "dynamic_overlay_audio_diagnostic")):
            raise TimelineValidationError("Invalid project dynamic overlay disclosure")
        for rendered in dynamic:
            if (not isinstance(rendered, dict)
                    or not isinstance(rendered.get("source_id"), str)
                    or not rendered["source_id"].strip()
                    or rendered.get("source_type") not in ("audio_visualizer", "particle_overlay")):
                raise TimelineValidationError("Invalid rendered dynamic source metadata")
        if len({x["source_id"] for x in dynamic}) != len(dynamic):
            raise TimelineValidationError("Duplicate rendered dynamic source ID")
        for omitted in excluded:
            if (not isinstance(omitted, dict)
                    or not isinstance(omitted.get("source_id"), str)
                    or not omitted["source_id"].strip()
                    or not isinstance(omitted.get("source_type"), str)
                    or not omitted["source_type"].strip()
                    or omitted.get("reason") not in (
                        "requires_separate_export_overlay", "requires_audio_analysis",
                    )):
                raise TimelineValidationError("Invalid excluded source metadata")
        if len({x["source_id"] for x in excluded}) != excluded_count:
            raise TimelineValidationError("Duplicate excluded dynamic source ID")
        if (not isinstance(start, (float, int)) or isinstance(start, bool)
                or not math.isfinite(start) or start < 0
                or type(timeline.get("source_count")) is not int
                or not 1 <= timeline["source_count"] <= 64
                or len(dynamic) > timeline["source_count"]
                or timeline["source_count"] + excluded_count > 64
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration <= 0
                or not isinstance(digest, str) or len(digest) != 64
                or not all(c in "0123456789abcdef" for c in digest)):
            raise TimelineValidationError("Invalid project-backed GPU timeline header")
    directory = path.parent
    references = []
    seen = set()
    for i, item in enumerate(frames):
        expected_manifest = f"frame-{i:04d}.json"
        expected_reference = f"reference-{i:04d}.png"
        if (not isinstance(item, dict) or item.get("pts") != i
                or not isinstance(item.get("time_seconds"), (int, float))
                or not math.isclose(item["time_seconds"], i / fps, abs_tol=1e-8)):
            raise TimelineValidationError(f"Qt timeline PTS or cue discontinuity at {i}")
        valid_indices = lambda values: (
            isinstance(values, list)
            and all(type(n) is int and 0 <= n <= 100000 for n in values)
            and values == sorted(set(values))
        )
        if kind == "source_motion":
            if item.get("cue_index") != i // 2:
                raise TimelineValidationError(f"Source-motion cue discontinuity at {i}")
        elif kind == "track_lyrics":
            cue = item.get("cue_index")
            active = item.get("active_cue_indices")
            held = item.get("held_cue_indices")
            if (not (cue is None or type(cue) is int and 0 <= cue <= 100000)
                    or not valid_indices(active) or not valid_indices(held)
                    or type(item.get("before_first")) is not bool
                    or not isinstance(item.get("track_elapsed_seconds"), (int, float))
                    or not math.isclose(
                        item["track_elapsed_seconds"], start + i / fps,
                        abs_tol=1e-8,
                    )):
                raise TimelineValidationError(f"Actual lyric cue state or clock invalid at {i}")
        else:
            absolute = item.get("timeline_seconds")
            local = item.get("track_elapsed_seconds")
            window_start = item.get("window_start_seconds")
            source_start = item.get("source_time_at_start")
            track_id = item.get("track_id")
            lyric_states = item.get("lyric_states")
            if (any(type(v) not in {float, int} or not math.isfinite(v)
                    for v in (absolute, local, window_start, source_start))
                    or not math.isclose(absolute, start + i / fps, abs_tol=1e-8)
                    or not math.isclose(local, absolute - window_start + source_start, abs_tol=1e-8)
                    or local < 0 or absolute < window_start
                    or not isinstance(track_id, str) or not track_id.strip()
                    or type(item.get("track_number")) is not int
                    or not 1 <= item["track_number"] <= 20000
                    or item.get("animation_phase") not in (None, "in", "out")
                    or not isinstance(lyric_states, list)
                    or len(lyric_states) > timeline["source_count"]):
                raise TimelineValidationError(f"Compiled project timeline/PTS invalid at {i}")
            for lyric in lyric_states:
                if (not isinstance(lyric, dict)
                        or not isinstance(lyric.get("source_id"), str)
                        or not lyric["source_id"].strip()
                        or not (lyric.get("cue_index") is None
                                or type(lyric.get("cue_index")) is int
                                and 0 <= lyric["cue_index"] <= 100000)
                        or not valid_indices(lyric.get("active_cue_indices"))
                        or not valid_indices(lyric.get("held_cue_indices"))
                        or type(lyric.get("before_first")) is not bool):
                    raise TimelineValidationError(f"Invalid project lyric state at PTS {i}")
        name = item.get("manifest")
        if name != expected_manifest or name in seen:
            raise TimelineValidationError("Qt timeline frame manifest path or order invalid")
        seen.add(name)
        _verify_manifest(directory, name, width, height)
        reference = _safe_local_file(directory, item.get("reference_png"), expected_reference)
        if _sha256(reference) != item.get("reference_sha256"):
            raise TimelineValidationError(f"Qt CPU frame {i} reference SHA-256 mismatch")
        references.append(reference)
    return timeline, references


def _run_media(argv: list[str], *, timeout: int = 120) -> bytes:
    try:
        process = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TimelineValidationError(f"Could not run media tool: {exc}") from exc
    if process.returncode:
        raise TimelineValidationError(
            f"Media tool failed: {process.stderr.decode('utf-8', 'replace')[-1200:]}"
        )
    return process.stdout


def verify_video_parity(
    timeline_path: Path, h264_path: Path, *,
    ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
    max_mae: float = 5.0, max_p95: float = 20.0,
) -> dict:
    """Compare every decoded H264 frame with its CPU Qt SourceItem reference.

    A bounded test: strictly ≤30 frames, ≤256MiB decoded RGB. This checks
    full-frame *lossy* parity, not per-pixel identity or GPU-native painting.
    """
    from PySide6.QtGui import QImage

    timeline, references = load_timeline(timeline_path)
    width, height = timeline["width"], timeline["height"]
    size = width * height * 3 * len(references)
    if size > MAX_DECODED_BYTES:
        raise TimelineValidationError("Qt RGB parity decode exceeds 256 MiB budget")
    if not 0 < max_mae <= 30 or not 0 < max_p95 <= 100:
        raise TimelineValidationError("Invalid parity thresholds")
    video = Path(h264_path)
    if not video.is_file() or video.stat().st_size < 64:
        raise TimelineValidationError("Missing or empty native GPU H264 video")
    try:
        probe = json.loads(_run_media([
            ffprobe, "-v", "error", "-f", "h264", "-count_frames",
            "-show_entries",
            "stream=codec_name,width,height,nb_read_frames,color_range,"
            "color_space,color_transfer,color_primaries",
            "-of", "json", str(video),
        ]))
        streams = probe["streams"]
        if len(streams) != 1:
            raise TimelineValidationError("Expected one H264 stream")
        stream = streams[0]
    except (ValueError, KeyError, TypeError) as exc:
        raise TimelineValidationError("FFprobe video stream metadata malformed") from exc
    if (stream.get("codec_name") != "h264" or stream.get("width") != width
            or stream.get("height") != height
            or stream.get("nb_read_frames") != str(len(references))
            or any(stream.get(field) != value for field, value in (
                ("color_range", "pc"), ("color_space", "bt709"),
                ("color_transfer", "bt709"), ("color_primaries", "bt709"),
            ))):
        raise TimelineValidationError(f"H264 frame/dimension/BT709 metadata mismatch: {stream}")
    decoded = _run_media([
        ffmpeg, "-v", "error", "-f", "h264", "-i", str(video),
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    if len(decoded) != size:
        raise TimelineValidationError(
            f"Decoded RGB byte count incorrect: {len(decoded)} != {size}"
        )
    frames = np.frombuffer(decoded, dtype=np.uint8).reshape(
        len(references), height, width, 3,
    )
    compared: list[dict] = []
    previous_reference: bytes | None = None
    previous_decoded: bytes | None = None
    for i, file in enumerate(references):
        img = QImage(str(file))
        if img.isNull() or img.width() != width or img.height() != height:
            raise TimelineValidationError(f"Invalid Qt RGB reference at frame {i}")
        rgb = img.convertToFormat(QImage.Format.Format_RGB888)
        stride = rgb.bytesPerLine()
        raw = bytes(rgb.constBits())
        reference = np.frombuffer(raw, dtype=np.uint8).reshape(height, stride)[
            :, :width*3,
        ].reshape(height, width, 3)
        a = frames[i].astype(np.int16)
        b = reference.astype(np.int16)
        delta = np.abs(a - b)
        mae = float(delta.mean())
        p95 = float(np.percentile(delta, 95))
        mse = float(np.square(a.astype(np.float64) - b.astype(np.float64)).mean())
        if mae > max_mae or p95 > max_p95:
            raise TimelineValidationError(
                f"Qt GPU/CPU parity failed at PTS {i}: MAE {mae:.3f}, p95 {p95:.1f}"
            )
        reference_bytes = reference.tobytes()
        decoded_bytes = frames[i].tobytes()
        if previous_reference == reference_bytes or previous_decoded == decoded_bytes:
            raise TimelineValidationError(f"Missing Qt source change or frozen decoded frame at PTS {i}")
        previous_reference, previous_decoded = reference_bytes, decoded_bytes
        compared.append({
            "pts": i,
            "rgb_mae_codes": round(mae, 4),
            "rgb_abs_error_p95_codes": round(p95, 2),
            "psnr_db": round(10 * math.log10(255**2 / mse), 3) if mse else None,
        })
    return {
        "schema": 1, "status": "verified",
        "frames_compared": len(compared),
        "resolution": [width, height],
        "fps_from_timeline": timeline["fps"],
        "source_frame_changes_verified": True,
        "decoded_frame_changes_verified": True,
        "full_frame_lossy_parity_verified": True,
        "exact_pixel_equality_claimed": False,
        "mean_mae_codes": round(sum(x["rgb_mae_codes"] for x in compared) / len(compared), 4),
        "worst_mae_codes": max(x["rgb_mae_codes"] for x in compared),
        "worst_p95_codes": max(x["rgb_abs_error_p95_codes"] for x in compared),
        "frame_metrics": compared,
        "qt_cpu_rasterized": True,
        "native_gpu_text_painting_implemented": False,
        "production_export_changed": False,
        **({
            "capture_scope": timeline["capture_scope"],
            "excluded_source_count": timeline["excluded_source_count"],
            "excluded_sources": timeline["excluded_sources"],
            "dynamic_sources": timeline.get("dynamic_sources", []),
            "dynamic_audio_origin": timeline.get("dynamic_audio_origin"),
            "full_project_visual_parity_verified": False,
        } if timeline.get("kind") == "project_timeline" else {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--output-dir", type=Path, required=True)
    cap.add_argument("--frames", type=int, default=6)
    cap.add_argument("--fps", type=int, default=60)
    cap.add_argument("--scale", type=float, default=0.5)
    ver = sub.add_parser("verify")
    ver.add_argument("--timeline", type=Path, required=True)
    ver.add_argument("--video", type=Path, required=True)
    ver.add_argument("--ffmpeg", default="ffmpeg")
    ver.add_argument("--ffprobe", default="ffprobe")
    ver.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture":
            path = capture_timeline(
                args.output_dir, frames=args.frames, fps=args.fps, scale=args.scale,
            )
            timeline, _ = load_timeline(path)
            result = {
                "status": "captured", "timeline": str(path),
                "frames": len(timeline["frames"]),
                "resolution": [timeline["width"], timeline["height"]],
                "fps": timeline["fps"],
                "production_export_changed": False,
            }
        else:
            if args.report and args.report.exists():
                raise TimelineValidationError("Refusing to overwrite parity report")
            result = verify_video_parity(
                args.timeline, args.video, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe,
            )
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                with args.report.open("x", encoding="utf-8") as out:
                    json.dump(result, out, indent=2, ensure_ascii=False)
                    out.write("\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TimelineValidationError, OSError, ValueError) as exc:
        print(json.dumps({
            "status": "validation_failed", "reason": str(exc),
            "production_export_changed": False,
        }, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
