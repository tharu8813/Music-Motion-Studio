"""Short production-path resident cache counter check, not an export speed claim.

Run with a freshly built native stream executable and an unused output folder.
No user projects/audio/output are opened or overwritten. Qt still paints dirty
planes on CPU; this probe measures their reuse through actual D3D11/NVENC.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from time import monotonic

from PySide6.QtWidgets import QApplication
from app.models.project import CanvasSettings, ProjectDocument
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.renderer.native_project_frames import produce_complete_project_frames
from app.renderer.native_gpu_stream import stream_native_canvas_h264
from scripts.rust_prerequisites import require_developer_environment


def run(executable: Path, destination: Path):
    require_developer_environment()
    destination.mkdir(parents=True, exist_ok=False)
    app = QApplication.instance() or QApplication([])
    project = ProjectDocument(canvas=CanvasSettings(320, 180), sources=[
        Source(SourceType.SHAPE, "Background", width=320, height=180,
               fill_color="#203040", z_index=0),
        Source(SourceType.PROGRESS_BAR, "Dynamic clock", x=0, y=140,
               width=320, height=24, progress_mode="track", z_index=1),
        Source(SourceType.TEXT, "Static rotated label", text="Resident cache",
               x=20, y=15, width=260, height=80, rotation=12, opacity=.65,
               font_size=24, z_index=2),
    ], playlist=[PlaylistTrack("", "Cache probe", duration_seconds=1)])
    counters = {}
    frames = produce_complete_project_frames(project, fps=60, scale=1,
        prepared_audio=destination / "unused.wav", ffmpeg="ffmpeg", raster_counters=counters)
    start = monotonic()
    report = stream_native_canvas_h264(frames, executable=executable,
        video_path=destination / "cache.h264", report_path=destination / "native.json",
        width=320, height=180, fps=60, duration_seconds=1, expected_frames=60)
    result = {"frames": 60, "layers": 3, "uncached_rasterizations": 180,
              "uncached_uploads": 180, "raster_cache": counters,
              "gpu_cache": report["resident_layer_cache"],
              "measured_stream_seconds": monotonic() - start,
              "full_export_speedup_proven": False,
              "cpu_pixel_painting_remains": True}
    assert counters["rasterizations"] == 62, result
    assert counters["reuse_hits"] == 118, result
    assert report["resident_layer_cache"]["uploads"] <= 62, result
    assert report["resident_layer_cache"]["allocations"] == 3, result
    with (destination / "comparison.json").open("x", encoding="utf-8") as writer:
        json.dump(result, writer, indent=2)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.executable, args.output_dir), indent=2))
