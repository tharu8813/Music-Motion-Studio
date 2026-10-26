"""Compare Canvas capture work with identical CFR output for a saved project.

python tools/export_capture_benchmark.py project.pvsproj --start 30 --seconds 5
Only captures are measured; this does not encode audio/video or open the editor.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from math import floor
import os
from pathlib import Path
import statistics
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests  # Isolate project extraction, caches and settings from the running app.
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.source import SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportTimelinePlanner
from app.renderer.ffmpeg_renderer import RenderFrame
from app.services.project_service import ProjectService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.seconds <= 0 or args.start < 0 or args.fps <= 0 or args.width <= 0 or args.rounds <= 0:
        parser.error("Seconds, FPS, width and rounds must be positive; start must be nonnegative.")
    application = QApplication.instance() or QApplication([])
    document = ProjectService.load(args.project)
    tracks = [track for track in document.playlist if track.enabled]
    duration = sum(track.duration_seconds for track in tracks)
    dynamic = {source.id for source in document.sources if source.source_type in {
        SourceType.AUDIO_VISUALIZER, SourceType.AUDIO_WAVEFORM,
        SourceType.AUDIO_LEVEL_METER, SourceType.PARTICLE_OVERLAY, SourceType.VIDEO,
    }}
    scene = CanvasScene()
    scene.set_artboard_size(document.canvas.width, document.canvas.height)
    for source in document.sources:
        scene.addItem(SourceItem(source))
    bands = CanvasSnapshot.split_mixed_capture_bands(
        scene, dynamic, CanvasSnapshot.z_bands(scene, dynamic), duration,
    )
    schedules = ExportTimelinePlanner.build_by_z_band(
        tracks, document.sources, dynamic, bands, args.fps, lazy=True,
    )
    reference = None
    timings = defaultdict(list)
    details = {}
    for run in range(args.rounds):
        for optimized in ((False, True) if run % 2 == 0 else (True, False)):
            totals = defaultdict(float)
            counts = defaultdict(int)
            digests = defaultdict(sha256)

            def stage(image, seconds, key):
                totals[key] += seconds
                target = max(1, floor(totals[key] * args.fps + 0.5))
                repeats = max(0, target - counts[key])
                if repeats:
                    signature = sha256(image.constBits()).digest()
                    digests[key].update(signature * repeats)
                counts[key] = target
                return RenderFrame(image, seconds)

            capturer = ExportCanvasCapturer(
                scene, tracks, duration, dynamic, bands, stage, lambda: None, lambda *_args: None,
                retain_static_frames=False, output_scale=args.width / document.canvas.width,
                output_fps=args.fps if optimized else None,
            )
            started = perf_counter()
            states = 0
            for key, samples in schedules.items():
                for sample in capturer.iter_coalesced_samples(samples, key):
                    if sample.timeline_seconds >= args.start + args.seconds:
                        break
                    if sample.timeline_seconds >= args.start:
                        capturer.capture_stream(sample, key)
                        states += 1
            elapsed = perf_counter() - started
            if not states:
                raise ValueError("No samples in the requested project interval.")
            signature = {key: (counts[key], digests[key].hexdigest()) for key in counts}
            if reference is None:
                reference = signature
            assert signature == reference, "CFR pixels or frame counts changed"
            label = "optimized" if optimized else "baseline"
            timings[label].append(elapsed)
            details[label] = {"states": states, "skipped_captures": capturer.skipped_cfr_captures}
            print(f"{label}: {elapsed:.3f}s; skipped={capturer.skipped_cfr_captures}/{states}", flush=True)
    medians = {key: statistics.median(values) for key, values in timings.items()}
    result = {
        "benchmark": "canvas_capture_only", "start": args.start, "seconds": args.seconds,
        "fps": args.fps, "width": args.width, "bands": len(bands),
        "runs_seconds": dict(timings), "median_seconds": medians,
        "improvement_percent": (1 - medians["optimized"] / medians["baseline"]) * 100,
        "details": details, "encoded_pixels_equal": True,
        "frame_signatures": reference,
        "limitation": "Capture-only sample; excludes FFmpeg, audio, visualizer preparation and pipe backpressure.",
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    scene.clear()
    application.processEvents()


if __name__ == "__main__":
    main()
