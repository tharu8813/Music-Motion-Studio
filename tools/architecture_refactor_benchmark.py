"""Compare HEAD implementations with the working refactor without changing the checkout."""
from __future__ import annotations

import ast
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
from time import perf_counter
import types
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests

from PySide6.QtGui import QColor, QImage
from app.models.source import Source, SourceType
import app.renderer.ffmpeg_renderer as renderer_module
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderSettings
from app.models.playlist import PlaylistTrack


def head(path):
    return subprocess.check_output(["git", "show", f"HEAD:{path}"], cwd=ROOT).decode("utf-8")


def legacy_run():
    tree = ast.parse(head("app/renderer/ffmpeg_renderer.py"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FFmpegRenderer")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_run")
    method.decorator_list = []
    namespace = vars(renderer_module).copy() | {"deque": deque}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "HEAD:_run", "exec"), namespace)
    return namespace["_run"]


def measure_pair(calls, repeats):
    for call in calls.values():
        call()
    samples = {name: [] for name in calls}
    names = list(calls)
    for index in range(repeats):
        for name in names if index % 2 == 0 else names[::-1]:
            start = perf_counter()
            calls[name]()
            samples[name].append(perf_counter() - start)
    return {name: {"samples_seconds": values, "median_seconds": statistics.median(values)}
            for name, values in samples.items()}


def main():
    legacy = types.ModuleType("architecture_baseline_source")
    sys.modules[legacy.__name__] = legacy
    exec(compile(head("app/models/source.py"), "HEAD:source.py", "exec"), vars(legacy))
    payloads = [Source(kind, kind.value).to_dict() for kind in SourceType]
    for payload in payloads:
        assert legacy.Source.from_dict(payload).to_dict() == Source.from_dict(payload).to_dict()

    def codec(cls):
        for _ in range(20):
            for payload in payloads:
                cls.from_dict(payload)

    report = {
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "python": sys.version,
        "scope": "HEAD vs working tree: 17 source types; identical FFmpeg static video command path, CPU H.264 320x180/12fps/2s. Warm-up excluded; paired alternating order, 9 codec runs and 7 renders per variant. No GPU performance claim.",
        "source_round_trip_types": len(payloads),
    }
    report["source_decode_340"] = measure_pair({
        "baseline": lambda: codec(legacy.Source), "refactored": lambda: codec(Source),
    }, 9)
    directory = ROOT / "work" / "architecture-render-check"
    directory.mkdir(exist_ok=True)
    audio = directory / "tone.wav"
    import numpy as np
    samples = (np.sin(np.arange(96000) * 2 * np.pi * 440 / 48000) * 8000).astype("<i2")
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(samples.tobytes())
    track = PlaylistTrack(str(audio), "Tone", duration_seconds=2)
    image = QImage(320, 180, QImage.Format.Format_ARGB32)
    image.fill(QColor("#174A75"))
    old = FFmpegRenderer()
    old._run = types.MethodType(legacy_run(), old)
    new = FFmpegRenderer()
    process_args = ["-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.2", "-f", "null", "-",
                    "-progress", "pipe:1"]
    report["ffmpeg_process"] = measure_pair({
        "baseline": lambda: old._run(process_args), "refactored": lambda: new._run(process_args),
    }, 12)
    settings = RenderSettings(fps=12, output_width=320, output_height=180, preset="ultrafast")
    report["render"] = measure_pair({
        "baseline": lambda: old.render(image, [track], directory / "baseline.mp4", settings),
        "refactored": lambda: new.render(image, [track], directory / "refactored.mp4", settings),
    }, 7)
    outputs = {}
    for name, renderer in (("baseline", old), ("refactored", new)):
        output = directory / f"{name}.mp4"
        decoded = {}
        for kind, args in (("video", ["-an", "-f", "rawvideo", "-pix_fmt", "rgb24"]),
                           ("audio", ["-vn", "-f", "s16le", "-acodec", "pcm_s16le"])):
            raw = subprocess.check_output([str(renderer.executable), "-v", "error", "-i", str(output), *args, "pipe:1"])
            decoded[kind] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        outputs[name] = decoded
    assert outputs["baseline"] == outputs["refactored"], outputs
    report["decoded_outputs"] = outputs
    report["decoded_video_and_audio_equal"] = True
    # A real hung encoder must stop even when it emits no more progress.
    from app.renderer.ffmpeg_renderer import RenderCancelledError
    cancel = threading.Event()
    timer = threading.Timer(0.25, cancel.set)
    started = perf_counter()
    timer.start()
    try:
        try:
            new._run(["-re", "-f", "lavfi", "-i", "sine=frequency=440", "-f", "null", "-"], cancel_event=cancel)
        except RenderCancelledError:
            report["real_ffmpeg_cancel_seconds"] = perf_counter() - started
        else:
            raise AssertionError("cancelled render unexpectedly succeeded")
    finally:
        timer.cancel()
    target = ROOT / "docs" / "architecture" / "benchmarks" / "2026-10-26-refactor-equivalence.json"
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
