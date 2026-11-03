"""Saved-animation loading and rendering measurements; no user project or preferences."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests  # Match the suite's disposable Qt settings and data directories.
from PySide6.QtWidgets import QApplication
from app.animation.keyframes import Keyframe
from app.animation.keyframe_render import KeyframeRenderBinding, ffmpeg_transform
from app.models.source import Source, SourceType
from app.models.playlist import PlaylistTrack
from app.models.project import ProjectDocument
from app.services.project_service import ProjectService
from app.timeline.compiler import compile_playlist
from app.renderer.ffmpeg_renderer import FFmpegRenderer


def milliseconds(action, repeat=1):
    begin = perf_counter()
    for _ in range(repeat):
        action()
    return (perf_counter() - begin) * 1000 / repeat


class MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                *[(name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]


def rss_mb(handle=None):
    if sys.platform != "win32":
        return None
    process = ctypes.WinDLL("kernel32").GetCurrentProcess
    process.restype = ctypes.c_void_p
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    read = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if read(int(handle) if handle is not None else process(), ctypes.byref(counters), counters.cb):
        return round(counters.WorkingSetSize / 1024 ** 2, 2)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "work/keyframe-benchmark.json")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    ffmpeg = FFmpegRenderer()
    rows = []
    for label, track_count, point_count, source_count in (("A", 3, 20, 5), ("B", 30, 300, 20),
                                                        ("C", 100, 1000, 50), ("dense", 100, 1000, 1)):
        tracks = [PlaylistTrack("", f"Track {i}", duration_seconds=10) for i in range(track_count)]
        sources = []
        for i in range(source_count):
            source = Source(SourceType.SHAPE, f"Layer {i}", x=(i % 10) * 14, y=(i // 10) * 14, width=8, height=8)
            count = point_count // source_count + (i < point_count % source_count)
            source.keyframes.global_curves = {"x": [Keyframe(j * track_count * 10 / max(1, count - 1),
                source.x + j % 3, "linear") for j in range(count)]}
            sources.append(source)
        plan = compile_playlist(tracks)
        document = ProjectDocument(sources=sources, playlist=tracks)
        row = {"case": label, "tracks": track_count, "points": point_count, "sources": source_count,
               "resolution": "160x90", "fps": 30, "rendered_seconds": 1}
        with TemporaryDirectory() as raw:
            temporary = Path(raw)
            project = ProjectService.save(temporary / "audit.pvsproj", document)
            row["project_load_ms"] = milliseconds(lambda: ProjectService.load(project), 3)
            # The actual production transform on 5/20/50 layers. Shared white
            # input isolates filter cost from media decoding and audio analysis.
            graph = [f"[1:v]format=rgba,split={source_count}" + "".join(f"[src{i}]" for i in range(source_count))]
            current = "[0:v]"
            for i, source in enumerate(sources):
                binding = KeyframeRenderBinding.build(source, plan)
                layer, x, y = ffmpeg_transform(graph, f"[src{i}]", binding, 8, 8, str(i))
                output = "[out]" if i == source_count - 1 else f"[base{i}]"
                graph.append(f"{current}{layer}overlay=x='{x}':y='{y}':format=rgb{output}")
                current = output
            graph_text = ";".join(graph)
            row["filter_graph_characters"] = len(graph_text)
            graph_args = ffmpeg._filter_graph_arguments(graph_text, temporary)
            command = [str(ffmpeg.executable), "-v", "error", "-filter_complex_threads", "1",
                "-f", "lavfi", "-i", "color=black:s=160x90:r=30:d=1", "-f", "lavfi", "-i",
                "color=white:s=8x8:r=30:d=1", *graph_args, "-map", "[out]", "-an"]
            start = perf_counter()
            init = subprocess.run([*command, "-frames:v", "1", "-f", "null", "-"], capture_output=True, timeout=60)
            row["first_frame_process_ms"] = (perf_counter() - start) * 1000
            row["first_frame_exit"] = init.returncode
            start = perf_counter()
            process = subprocess.Popen([*command, "-c:v", "libx264", "-preset", "ultrafast", "-y",
                str(temporary / "out.mp4")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            peak = 0
            while process.poll() is None:
                peak = max(peak, rss_mb(process._handle) or 0)
                sleep(.02)
            _, stderr = process.communicate()
            row["render_process_ms"] = (perf_counter() - start) * 1000
            row["render_exit"] = process.returncode
            row["ffmpeg_sampled_peak_rss_mb"] = peak
            if process.returncode or init.returncode:
                row["error"] = (stderr + init.stderr).decode("utf-8", "replace")[-2000:]
            row["python_rss_mb"] = rss_mb()
        rows.append(row)
        print(json.dumps(row), flush=True)
    args.output.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return int(any(r["render_exit"] or r["first_frame_exit"] for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
