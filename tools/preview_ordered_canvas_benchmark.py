"""Compare old full-scene Canvas raster and the ordered GPU preview raster.

This intentionally measures CPU Canvas preparation only, not display FPS or
the actual NVIDIA OpenGL queue. GitHub Actions publishes the JSON result.
A visual result must still be tested on a real GPU after merging.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import mean
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.export_preview_dialog import ExportPreviewDialog, TIMELINE_SCALE
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.utils.i18n import Translator


def run() -> dict:
    app = QApplication.instance() or QApplication([])
    scene = CanvasScene()
    scene.set_artboard_size(1280, 720)
    sources = [
        Source(SourceType.SHAPE, "static background", x=0, y=0,
               width=1280, height=720, fill_color="#15212b", z_index=0),
        Source(SourceType.LYRICS, "moving bilingual lyrics",
               x=515, y=85, width=680, height=515, font_size=38,
               fill_color="#00000000", subtitle_animation="glow",
               subtitle_context_lines=2, subtitle_next_lines=2,
               subtitle_glow_radius=9, subtitle_previous_blur=4,
               z_index=1),
        Source(SourceType.TEXT, "foreground text", x=125, y=45,
               width=350, height=85, font_size=35, text="Music Motion Studio",
               z_index=2, fill_color="#ffffff"),
    ]
    for source in sources:
        scene.addItem(SourceItem(source))
    track = PlaylistTrack(
        "", "Benchmark", duration_seconds=45,
        lyrics=[
            {"start": 0.0, "end": 2.0, "text": "따뜻한 밤의 노래\nThe night feels warm"},
            {"start": 2.0, "end": 4.0, "text": "함께 걷는 시간\nWe walk together"},
            {"start": 4.0, "end": 6.0, "text": "우리가 만든 이야기\nA story we made"},
            {"start": 6.0, "end": 8.0, "text": "조용히 흐르는 시간\nTime moves quietly"},
        ],
    )
    preview = ExportPreviewDialog(
        scene, [track], Translator(), preferred_backend="cpu",
    )
    preview._refresh_source_partitions()
    # Use identical playhead values for the old and new raster preparations.
    positions = (1.0, 1.1, 1.2, 2.2, 2.3, 3.2, 4.2, 5.2)
    reference_ms: list[float] = []
    layered_ms: list[float] = []
    try:
        preview._adaptive_quality.reset(1.0)
        for seconds in positions:
            preview.timeline.blockSignals(True)
            preview.timeline.setValue(round(seconds * TIMELINE_SCALE))
            preview.timeline.blockSignals(False)
            start = perf_counter()
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0,
                elapsed_seconds=seconds,
                timeline_seconds=seconds,
                output_scale=1.0,
                playlist_duration_seconds=45.0,
                playlist_tracks=[track],
                compiled_plan=preview._compiled_plan,
            )
            reference_ms.append((perf_counter() - start) * 1000)
            start = perf_counter()
            preview._ordered_canvas_gpu_layers(
                track, 0, seconds, 0.0, frozenset(),
            )
            layered_ms.append((perf_counter() - start) * 1000)
    finally:
        preview._stop_preview()
        preview.close()
        preview.deleteLater()
        app.processEvents()
    # First sample warms resources, which would skew both algorithms.
    original = reference_ms[1:]
    improved = layered_ms[1:]
    return {
        "measurement": "CPU raster preparation, not full GPU presentation FPS",
        "fixture": "1280x720 static background + dynamic bilingual lyrics + higher Z static text",
        "samples": len(improved),
        "full_canvas_mean_ms": round(mean(original), 3),
        "ordered_zbands_mean_ms": round(mean(improved), 3),
        "ratio_full_over_ordered": round(mean(original) / max(0.001, mean(improved)), 3),
        "ordered_static_cache_hits": preview._ordered_cache_hits,
        "ordered_band_renders": preview._ordered_cache_renders,
        "full_canvas_samples_ms": [round(v, 3) for v in original],
        "ordered_zbands_samples_ms": [round(v, 3) for v in improved],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    result = run()
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
