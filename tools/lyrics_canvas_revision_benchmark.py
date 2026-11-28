"""Compare identical Canvas captures across revisions on one machine.

Three isolated checkouts: before lyric grouping/search changes, after those
changes, and after raster cache changes. Forward/reverse order balances drift.
No hardware presentation FPS or process-wide memory claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter
from tempfile import TemporaryDirectory


def summarize(samples):
    ordered = sorted(samples)
    return {"median_ms": statistics.median(ordered),
            "p95_ms": ordered[max(0, math.ceil(len(ordered) * .95) - 1)]}


def capture_cases(repo_root: Path, frames: int):
    # Explicit checkout, not the checkout containing this benchmark script.
    sys.path.insert(0, str(repo_root.resolve()))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import tests  # noqa: F401 -- disposable Qt/app state
    from PySide6.QtWidgets import QApplication
    from app.canvas.live_canvas import CanvasScene
    from app.canvas.source_item import SourceItem
    from app.models.playlist import PlaylistTrack
    from app.models.source import Source, SourceType
    from app.preview.canvas_snapshot import CanvasSnapshot

    app = QApplication.instance() or QApplication([])
    cases = {}
    for kind in ("sequential", "overlap"):
        # Evaluate late in a long track, including real overlaps and gaps.
        cues = []
        for i in range(2000):
            start = i * 2.0
            end = start + (5.5 if kind == "overlap" and i % 3 == 0 else 1.8)
            text = f"{i}: 음악이 흐르는 밤 / Music"
            cues.append({"start": start, "end": end, "text": text, "format": "ttml",
                         "words": [{"start": start, "end": end, "text": text, "offset": 0}]})
        track = PlaylistTrack("", "Synthetic lyrics", duration_seconds=4005.0, lyrics=cues)
        for scale in (0.55, 1.0):
            scene = CanvasScene()
            scene.set_artboard_size(1280, 720)
            source = Source(
                SourceType.LYRICS, "Lyrics", x=90, y=80, width=1100, height=560,
                font_size=36, subtitle_animation="glow", subtitle_animation_duration=.8,
                subtitle_karaoke_enabled=True, subtitle_context_lines=3,
                subtitle_next_lines=3, subtitle_previous_blur=5,
                subtitle_glow_radius=12, subtitle_intro_enabled=False,
                subtitle_intro_midtrack=False, fill_color="#00000000",
            )
            item = SourceItem(source)
            scene.addItem(item)
            total, state, paint, hashes = [], [], [], []
            peak_ghost_bytes = peak_coverage_bytes = peak_entries = 0
            try:
                # Identical warmup outside timing; no cache clearing mid-run.
                for step in range(-6, frames):
                    seconds = 3000.0 + step / 12.0
                    metrics = {}
                    started = perf_counter()
                    image = CanvasSnapshot.capture_track(
                        scene, track, 1, 1, 0.0, elapsed_seconds=seconds,
                        timeline_seconds=seconds, output_scale=scale,
                        transparent=True, render_metrics=metrics,
                    )
                    elapsed = (perf_counter() - started) * 1000.0
                    if image.isNull():
                        raise RuntimeError("Null Canvas capture")
                    if step < 0:
                        continue
                    total.append(elapsed)
                    state.append(float(metrics["state_setup_seconds"]) * 1000)
                    paint.append(float(metrics["paint_stage_seconds"]) * 1000)
                    # Hash and memory accounting outside timed capture.
                    hashes.append(hashlib.sha256(bytes(image.constBits())).hexdigest())
                    peak_entries = max(peak_entries, len(item._lyric_ghost_cache))
                    peak_ghost_bytes = max(peak_ghost_bytes, sum(
                        pixmap.width() * pixmap.height() * max(4, (pixmap.depth() + 7) // 8)
                        for pixmap, _ in item._lyric_ghost_cache.values()
                    ))
                    peak_coverage_bytes = max(peak_coverage_bytes, sum(
                        value[0].nbytes for value in item._lyric_coverage_cache.values()
                    ))
                cases[f"{kind}-{scale}"] = {
                    "capture": summarize(total), "state_setup": summarize(state),
                    "paint": summarize(paint), "frame_hashes": hashes,
                    "peak_ghost_entries": peak_entries,
                    "peak_ghost_estimated_bytes": peak_ghost_bytes,
                    "peak_coverage_array_bytes": peak_coverage_bytes,
                }
            finally:
                scene.clear()
                app.processEvents()
    return cases


def compare(roots, frames: int, report_path: Path):
    names = ("before", "search", "raster")
    rows = {name: [] for name in names}
    script = Path(__file__).resolve()
    with TemporaryDirectory(prefix="lyrics-revision-bench-") as directory:
        for repeat, order in enumerate((names, names[::-1]), start=1):
            for name in order:
                output = Path(directory) / f"{repeat}-{name}.json"
                subprocess.run(
                    [sys.executable, str(script), "--repo-root", str(roots[name]),
                     "--frames", str(frames), "--output", str(output)],
                    cwd=roots[name], check=True, timeout=600,
                )
                result = json.loads(output.read_text(encoding="utf-8"))
                rows[name].append(result)
                print(f"completed round={repeat} revision={name}", flush=True)
    summary = {}
    errors = []
    for case in rows["before"][0]:
        reference = rows["before"][0][case]["frame_hashes"]
        variants = {}
        for name in names:
            runs = [row[case] for row in rows[name]]
            if runs[0]["frame_hashes"] != runs[1]["frame_hashes"]:
                errors.append(f"{case}: {name} pixel hashes changed across identical runs")
            changed = sum(a != b for a, b in zip(reference, runs[0]["frame_hashes"], strict=True))
            # Search/grouping optimizations promise exact pixels. Raster cache
            # geometry correction intentionally can change previously aliased pixels.
            if name == "search" and changed:
                errors.append(f"{case}: search/grouping changed {changed} frame(s)")
            variants[name] = {
                "capture_median_ms": statistics.median(r["capture"]["median_ms"] for r in runs),
                "capture_p95_ms": statistics.median(r["capture"]["p95_ms"] for r in runs),
                "state_median_ms": statistics.median(r["state_setup"]["median_ms"] for r in runs),
                "paint_median_ms": statistics.median(r["paint"]["median_ms"] for r in runs),
                "peak_ghost_estimated_bytes": max(r["peak_ghost_estimated_bytes"] for r in runs),
                "peak_coverage_array_bytes": max(r["peak_coverage_array_bytes"] for r in runs),
                "changed_frames_vs_before": changed,
            }
            if name == "raster" and variants[name]["peak_ghost_estimated_bytes"] > 32 * 1024 * 1024:
                errors.append(f"{case}: raster cache exceeded estimated byte budget")
        summary[case] = variants
    report = {
        "revisions": {name: subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
            for name, root in roots.items()},
        "frames_per_case": frames, "logical_cpus": os.cpu_count(),
        "order": [list(names), list(reversed(names))],
        "scope": "Offscreen Canvas CPU capture, not live display FPS. Per-source retained pixmap/coverage estimates, not total RAM or VRAM. Timings are medians of two run summaries; stages can overlap.",
        "summary": summary, "runs": rows, "errors": errors,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "errors": errors}, indent=2), flush=True)
    if errors:
        raise RuntimeError("; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--search", type=Path)
    parser.add_argument("--raster", type=Path)
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("Positive frame count required")
    if args.repo_root is not None:
        result = capture_cases(args.repo_root, args.frames)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    elif all((args.before, args.search, args.raster)):
        compare({name: getattr(args, name).resolve() for name in ("before", "search", "raster")},
                args.frames, args.output)
    else:
        parser.error("Supply --repo-root or all three comparison checkouts")


if __name__ == "__main__":
    main()
