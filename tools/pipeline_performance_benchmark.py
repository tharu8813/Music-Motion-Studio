"""Reproducible pipeline diagnostics; JSON contains measured values only.

Run before and after runtime edits with the same interpreter and arguments.
These are component measurements, not end-to-end export or display FPS.
"""
from __future__ import annotations

import argparse
from dataclasses import fields
from hashlib import sha256
import json
import os
from pathlib import Path
import pickle
import platform
import statistics
import sys
import threading
from time import perf_counter, process_time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests  # Disposable settings and caches, including portable-project extraction.
import numpy as np
import PySide6
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication
from app.models.playlist import PlaylistTrack
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.utils.performance import resident_set_bytes


def measured(work, rounds):
    runs = []
    result = None
    for _ in range(rounds):
        initial = resident_set_bytes()
        samples = [initial or 0]
        stop = threading.Event()

        def poll():
            while not stop.wait(.01):
                samples.append(resident_set_bytes() or 0)

        worker = threading.Thread(target=poll)
        worker.start()
        cpu = process_time()
        started = perf_counter()
        try:
            result = work()
        finally:
            elapsed = perf_counter() - started
            cpu = process_time() - cpu
            stop.set()
            worker.join()
        runs.append(dict(wall_seconds=elapsed, process_cpu_seconds=cpu,
                         rss_initial=initial, rss_sampled_peak=max(samples),
                         rss_after=resident_set_bytes()))
    return dict(runs=runs, median_seconds=statistics.median(r["wall_seconds"] for r in runs),
                result=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("rounds must be positive")
    application = QApplication.instance() or QApplication([])
    results = dict(python=platform.python_version(), qt=PySide6.__version__,
                   numpy=np.__version__, platform=platform.platform(), rounds=args.rounds)
    analyzer = PythonVisualizerRenderer(Path("unused-ffmpeg"))
    times = np.arange(30 * analyzer.sample_rate) / analyzer.sample_rate
    audio = (.2 * np.sin(2 * np.pi * 80 * times) + .1 * np.sin(2 * np.pi * 330 * times)).astype(np.float32)

    def fft():
        values = analyzer._analyze_levels(audio, 60, 48, threading.Event())
        return dict(shape=list(values.shape), sha256=sha256(values.tobytes()).hexdigest())

    results["fft_30_seconds_60fps_48bands"] = measured(fft, args.rounds)
    track = PlaylistTrack("unused.wav", "IPC fixture", duration_seconds=240,
                          lyrics=[dict(start=i * .1, end=(i + 1) * .1,
                                       text=f"{i}: 음악이 흐르는 밤 / Music brings us together") for i in range(2000)])
    sample = ExportFrameSample(track, 1, 0, 1 / 60, 1.0, 1.0)
    from app.preview import canvas_process_stream as ipc
    pack = getattr(ipc, "_pack_sample", None)
    indexes = {id(track): 0}

    def serialize():
        count = 0
        for _ in range(1000):
            message = pack(sample, indexes) if pack else sample
            count += len(pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL))
        return dict(messages=1000, serialized_bytes=count,
                    sample_fields=[field.name for field in fields(sample)])

    results["canvas_ipc_2000_cues"] = measured(serialize, args.rounds)
    from app.preview import canvas_snapshot as snapshot
    from types import SimpleNamespace
    item = SimpleNamespace(_apply_image_filters=lambda pixmap, **_: pixmap)

    def cache():
        snapshot._FILTERED_TRACK_PIXMAP_CACHE.clear()
        for index in range(16):
            image = QImage(1920, 1080, QImage.Format.Format_ARGB32)
            image.fill(index * 1234567 | 0xff000000)
            pixmap = QPixmap.fromImage(image)
            snapshot._filtered_track_pixmap(item, pixmap, ("benchmark", index))
        entries = snapshot._FILTERED_TRACK_PIXMAP_CACHE
        return dict(entries=len(entries), logical_pixel_bytes=sum(
            pixmap.width() * pixmap.height() * ((pixmap.depth() + 7) // 8)
            for pixmap in entries.values()))

    results["filtered_cache_16_1080p_images"] = measured(cache, args.rounds)
    snapshot._FILTERED_TRACK_PIXMAP_CACHE.clear()
    from app.canvas.live_canvas import CanvasScene
    from app.canvas.source_item import SourceItem
    from app.models.source import Source, SourceType
    from app.preview.export_canvas_capture import ExportCanvasCapturer
    from app.renderer.ffmpeg_renderer import RenderFrame
    scene = CanvasScene()
    scene.set_artboard_size(1280, 720)
    scene.addItem(SourceItem(Source(SourceType.TEXT, "Title", text="Music Motion / 음악",
                                   x=40, y=40, width=800, height=120, animation_in="fade")))
    for width in (1920, 3840):
        def capture():
            digest = sha256()
            def stage(image, seconds, _):
                digest.update(image.constBits())
                return RenderFrame(image, seconds)
            capturer = ExportCanvasCapturer(scene, [track], 240, set(), [(None, None)],
                                            stage, lambda: None, lambda *_: None,
                                            output_scale=width / 1280, output_fps=60)
            for index in range(60):
                capturer.capture_stream(ExportFrameSample(track, 1, 0, 1/60, index/60,
                    index/60, animation_phase="in", animation_progress=index/60,
                    animation_phase_duration=1), "base")
            return dict(frames=60, width=width, sha256=digest.hexdigest())
        results[f"capture_hash_{width}_60frames"] = measured(capture, args.rounds)
    scene.clear()
    application.processEvents()
    results["limitations"] = ["CPU times and RSS cover this Python process only; exclude child processes and GPU.",
                              "Capture includes SHA256 reads; IPC measures serialization, not queue or rendering.",
                              "No real audio decode, end-to-end export, display latency or GPU timing in this tool."]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: value["median_seconds"] for key, value in results.items()
                      if isinstance(value, dict) and "median_seconds" in value}, indent=2))


if __name__ == "__main__":
    main()
