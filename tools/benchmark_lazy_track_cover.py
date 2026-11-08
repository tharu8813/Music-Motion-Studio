"""A/B benchmark of Canvas album-art lazy extraction versus eager extraction.

Measures full QGraphicsScene painting and the per-frame artwork factory cost,
while asserting that every final frame has identical bytes. This is a Canvas
frame benchmark, NOT a measurement of whole MP4 export.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import median
import sys
from time import perf_counter
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview import canvas_snapshot as snapshot


def build_scene() -> CanvasScene:
    scene = CanvasScene()
    scene.addItem(SourceItem(Source(
        SourceType.ALBUM_COVER, "Cover", x=20, y=20,
        width=240, height=240, z_index=1,
    )))
    scene.addItem(SourceItem(Source(
        SourceType.BACKGROUND, "Background", width=480, height=320,
        background_mode="album_art", z_index=0,
    )))
    return scene


def measure(scene: CanvasScene, track: PlaylistTrack, image: QImage, *, lazy: bool, frames: int) -> dict:
    # Mimic extract_track_cover's conversion of its cached QImage to QPixmap.
    calls = [0]

    def extract(*_args):
        calls[0] += 1
        return QPixmap.fromImage(image)

    snapshot._FILTERED_TRACK_PIXMAP_CACHE.clear()
    mode = "0" if lazy else "1"
    with patch.dict(os.environ, {"PLAYLIST_CANVAS_DISABLE_LAZY_TRACK_COVER": mode}), patch.object(
        snapshot, "extract_track_cover", side_effect=extract,
    ):
        started = perf_counter()
        final = None
        for index in range(frames):
            # Repeat the same visual time to ensure only the artwork lookup
            # changes between modes. The scene still paints every QImage.
            final = snapshot.CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0,
                elapsed_seconds=1.0, timeline_seconds=1.0,
            )
        seconds = perf_counter() - started
    assert final is not None
    return {
        "seconds": round(seconds, 5),
        "artwork_pixmap_conversions": calls[0],
        "last_frame_sha256": sha256(memoryview(final.constBits()).cast("B")).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    a = parser.parse_args()
    if not 2 <= a.frames <= 2000 or not 1 <= a.repeats <= 5:
        parser.error("Invalid frame count or repeats")

    app = QApplication.instance() or QApplication([])
    scene = build_scene()
    track = PlaylistTrack("benchmark-track.wav", "Test", duration_seconds=60)
    artwork = QImage(1024, 1024, QImage.Format.Format_RGB32)
    artwork.fill(QColor("#4080C0"))
    observations: dict[str, list[dict]] = {"eager": [], "lazy": []}
    for rep in range(a.repeats):
        order = (False, True) if rep % 2 == 0 else (True, False)
        for lazy in order:
            label = "lazy" if lazy else "eager"
            observations[label].append(
                measure(scene, track, artwork, lazy=lazy, frames=a.frames)
            )
    expected = {x["last_frame_sha256"] for data in observations.values() for x in data}
    if len(expected) != 1:
        raise SystemExit(f"ERROR: album-art frame output is different: {expected}")
    baseline = median(x["seconds"] for x in observations["eager"])
    optimized = median(x["seconds"] for x in observations["lazy"])
    report = {
        "schema": 1,
        "scope": "Full Canvas frame painting of two album-art sources, not total MP4 export",
        "frames": a.frames, "repeats": a.repeats,
        "all_frames_equal": True,
        "eager_median_seconds": round(baseline, 5),
        "lazy_median_seconds": round(optimized, 5),
        "speedup": round(baseline / optimized, 3) if optimized else None,
        "artwork_conversion_counts": {
            "eager": observations["eager"][0]["artwork_pixmap_conversions"],
            "lazy": observations["lazy"][0]["artwork_pixmap_conversions"],
        },
        "runs": observations,
    }
    print(json.dumps(report, indent=2), flush=True)
    if a.output is not None:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    scene.clear()


if __name__ == "__main__":
    main()
