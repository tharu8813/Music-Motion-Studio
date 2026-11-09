"""Real PlaylistTrack LYRICS timing -> Qt Canvas -> GPU packet diagnostics.

This probe exercises the SAME CanvasSnapshot.capture_track() and
resolve_lyrics_cue_state() as preview/export, including overlapping TTML
cues, but keeps the native GPU encoding experiment isolated from the
normal Music Motion Studio exporter.

It can use a real serialized PlaylistTrack JSON (PlaylistTrack.to_dict())
or a deterministic three-cue overlapping TTML fixture. Source styles
are illustrative; the real project's full source graph and compiled
render plan are not loaded.

    python -m tools.native_canvas_real_lyrics_probe \
        --output-dir qt-lyrics --frames 6 --fps 60 --scale 0.5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.frame_state import resolve_lyrics_cue_state
from app.preview.native_canvas_layer_packets import (
    NativeCanvasFrame, capture_native_canvas_track_frame, save_native_canvas_frame,
)
from tools.native_canvas_timeline_probe import (
    FORMAT, MAX_FRAMES, TimelineValidationError, load_timeline,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def demo_track() -> PlaylistTrack:
    """Three real timed TTML-style lyric cues with deliberate overlap."""
    cues = [
        (0.0, 0.070, "First sustained line"),
        (2 / 60, 0.065, "Second overlapping line"),
        (4 / 60, 0.180, "New incoming line"),
    ]
    return PlaylistTrack(
        "", "Real Qt Lyrics Diagnostic", duration_seconds=0.5,
        lyrics=[
            {
                "start": start, "end": end,
                "text": text, "format": "ttml",
                "words": [{"start": start, "end": end, "text": text, "offset": 0}],
            }
            for start, end, text in cues
        ],
    )


def load_track(track_file: Path | None) -> PlaylistTrack:
    if track_file is None:
        return demo_track()
    file = Path(track_file)
    if not file.is_file() or file.is_symlink() or file.stat().st_size > 4 * 1024 * 1024:
        raise TimelineValidationError("Missing, symlinked or oversized track JSON")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        track = PlaylistTrack.from_dict(data)
    except (ValueError, TypeError, KeyError) as exc:
        raise TimelineValidationError(f"Invalid PlaylistTrack JSON: {exc}") from exc
    if not track.lyrics:
        raise TimelineValidationError("Track JSON has no lyric cues")
    return track


def make_scene(frame_index: int, source: Source) -> CanvasScene:
    scene = CanvasScene()
    scene.addItem(SourceItem(Source(
        SourceType.SHAPE, "Canvas background",
        x=0, y=0, width=1280, height=720, z_index=0,
        fill_color="#17283D",
    )))
    scene.addItem(SourceItem(Source(
        SourceType.SHAPE, "Animated accent panel",
        x=160 + 21 * frame_index, y=270,
        width=940, height=190, z_index=11,
        fill_color="#397BBA", opacity=0.32 + 0.08 * (frame_index % 3),
    )))
    scene.addItem(SourceItem(source))
    return scene


def _make_reference(frame: NativeCanvasFrame) -> QImage:
    base = frame.layers[0]
    reference = QImage(
        base.pixels, frame.width, frame.height, frame.width * 4,
        QImage.Format.Format_ARGB32,
    ).copy()
    painter = QPainter(reference)
    try:
        for layer in frame.layers[1:]:
            raster = QImage(
                layer.pixels, frame.width, frame.height, frame.width * 4,
                QImage.Format.Format_ARGB32,
            )
            painter.setOpacity(layer.opacity)
            painter.drawImage(0, 0, raster)
    finally:
        painter.end()
    return reference


def capture_real_lyrics(
    output_dir: Path, *,
    track_json: Path | None = None,
    frames: int = 6,
    fps: int = 60,
    scale: float = 0.5,
    start_seconds: float = 0.0,
) -> Path:
    if not (3 <= frames <= MAX_FRAMES and 1 <= fps <= 120
            and 0.25 <= scale <= 1.0
            and math.isfinite(start_seconds) and start_seconds >= 0):
        raise TimelineValidationError("Invalid frames, FPS, scale or start time")
    track = load_track(track_json)
    if start_seconds + (frames - 1) / fps >= track.duration_seconds:
        raise TimelineValidationError("Lyric capture interval extends beyond track duration")
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise TimelineValidationError(f"Refusing nonempty lyric capture directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    source = Source(
        SourceType.LYRICS, "Real lyric state", x=160, y=250,
        width=960, height=260, z_index=12,
        font_size=43, font_weight=600,
        fill_color="#FFFFFF", text_stroke_width=0.0,
        subtitle_animation="none", subtitle_intro_enabled=False,
        subtitle_intro_midtrack=False, subtitle_karaoke_enabled=True,
        subtitle_context_lines=0, subtitle_next_lines=0,
        subtitle_current_scale=1.0, subtitle_anchor=0.5,
        subtitle_translation_enabled=False,
    )
    entries: list[dict] = []
    try:
        for i in range(frames):
            elapsed = start_seconds + i / fps
            scene = make_scene(i, source)
            try:
                packets = capture_native_canvas_track_frame(
                    scene, track, [(None, 9), (10, None)],
                    elapsed_seconds=elapsed,
                    output_scale=scale,
                    playlist_duration_seconds=track.duration_seconds,
                )
                name = f"frame-{i:04d}"
                manifest = save_native_canvas_frame(packets, output_dir, stem=name)
                reference = _make_reference(packets)
                png = output_dir / f"reference-{i:04d}.png"
                if png.exists() or not reference.save(str(png), "PNG"):
                    raise TimelineValidationError(f"Cannot write Qt lyric PNG reference at {i}")
                state = resolve_lyrics_cue_state(
                    track, elapsed,
                    track.lyrics_timing_offset_seconds,
                    source.subtitle_timing_offset,
                    source.subtitle_animation,
                    source.subtitle_animation_duration,
                    whole_lines=source.subtitle_karaoke_enabled,
                )
                entries.append({
                    "pts": i,
                    "time_seconds": round(i / fps, 9),
                    "track_elapsed_seconds": round(elapsed, 9),
                    "manifest": manifest.name,
                    "reference_png": png.name,
                    "reference_sha256": _sha256(png),
                    "cue_index": state.cue_index,
                    "active_cue_indices": list(state.active_cue_indices),
                    "held_cue_indices": list(state.held_cue_indices),
                    "before_first": state.before_first,
                })
            finally:
                scene.clear()
        meta = json.loads((output_dir / entries[0]["manifest"]).read_text(encoding="utf8"))
        timeline = {
            "schema": 1, "format": FORMAT, "kind": "track_lyrics",
            "width": meta["width"], "height": meta["height"], "fps": fps,
            "start_seconds": start_seconds,
            "frames": entries,
            "qt_canvas_cpu_rasterized": True,
            "uses_actual_lyrics_cue_resolver": True,
            "native_gpu_text_painting_implemented": False,
            "production_export_changed": False,
        }
        path = output_dir / "timeline.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(timeline, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        load_timeline(path)
        return path
    except BaseException:
        # Only our newly created output directory is eligible for cleanup.
        for item in output_dir.iterdir():
            if item.is_file() and not item.is_symlink():
                item.unlink()
        raise
    finally:
        _ = app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--track-json", type=Path,
                        help="Optional PlaylistTrack.to_dict() JSON with real lyric cues")
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--scale", type=float, default=0.5)
    parser.add_argument("--start-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        path = capture_real_lyrics(
            args.output_dir, track_json=args.track_json,
            frames=args.frames, fps=args.fps, scale=args.scale,
            start_seconds=args.start_seconds,
        )
        timeline, _ = load_timeline(path)
        print(json.dumps({
            "schema": 1, "status": "captured",
            "timeline": str(path),
            "frames": len(timeline["frames"]),
            "resolution": [timeline["width"], timeline["height"]],
            "fps": timeline["fps"],
            "active_cue_indices": [
                frame["active_cue_indices"] for frame in timeline["frames"]
            ],
            "uses_actual_lyrics_cue_resolver": True,
            "production_export_changed": False,
        }, ensure_ascii=False, indent=2))
        return 0
    except (TimelineValidationError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({
            "status": "validation_failed", "reason": str(exc),
            "production_export_changed": False,
        }, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
