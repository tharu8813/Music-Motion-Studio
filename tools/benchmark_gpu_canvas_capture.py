"""A/B inspect CPU QImage vs experimental OpenGL Canvas capture on THIS GPU.

Run directly on the target Windows desktop, not through Remote Desktop or a
headless CI runner, using the application's normal Python environment:

  python tools/benchmark_gpu_canvas_capture.py --width 1920 --height 1080

Compares capture wall time AND pixel quality. Does not measure NVENC or full
MP4 exports. GPU capture still performs a costly GPU->CPU framebuffer readback.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication, QGraphicsScene

from app.preview.gpu_canvas_capture import (
    ENABLE_ENV, capture_canvas_opengl, reset_gpu_capture_for_tests,
)


def build_scene(width: int, height: int) -> QGraphicsScene:
    scene = QGraphicsScene(0, 0, width, height)
    # Include fill, alpha blending, text, and small shapes to exercise
    # the same QPainter primitives used by source renderers.
    scene.addRect(
        QRectF(0, 0, width, height),
        brush=QBrush(QColor("#101926")),
    )
    for i in range(24):
        x = (i * 83) % max(1, width - 90)
        y = (i * 61) % max(1, height - 48)
        color = QColor.fromHsv((i * 31) % 360, 180, 210, 220)
        scene.addEllipse(QRectF(x, y, 72, 40), brush=QBrush(color))
    for i in range(5):
        item = scene.addText(f"Export frame {i} • ABC 가사 Lyrics")
        font = QFont("Arial", 24)
        item.setFont(font)
        item.setDefaultTextColor(QColor("#FFFFFF"))
        item.setPos(24, min(height - 70, height * (i + 1) / 7))
    return scene


def cpu_capture(scene: QGraphicsScene, target: QRectF, transparent: bool) -> QImage:
    fmt = QImage.Format.Format_ARGB32_Premultiplied if transparent else QImage.Format.Format_RGB32
    image = QImage(round(target.width()), round(target.height()), fmt)
    image.fill(Qt.GlobalColor.transparent if transparent else Qt.GlobalColor.black)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scene.render(painter, target, target)
    finally:
        painter.end()
    return image


def pixels(image: QImage) -> np.ndarray:
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    arr = np.frombuffer(converted.constBits(), dtype=np.uint8)
    return arr.reshape(converted.height(), converted.bytesPerLine())[
        :, :converted.width() * 4
    ].reshape(converted.height(), converted.width(), 4).copy()


def run(width: int, height: int, frames: int, repeats: int) -> dict:
    app = QApplication.instance() or QApplication([])
    scene = build_scene(width, height)
    bounds = QRectF(0, 0, width, height)
    original = os.environ.get(ENABLE_ENV)
    results = {"cpu": [], "opengl": []}
    statuses = []
    try:
        # The first OpenGL allocation and Qt font-glyph upload can take much
        # longer than later frames; exclude both modes' warmup from measurement.
        cpu_capture(scene, bounds, False)
        os.environ[ENABLE_ENV] = "1"
        reset_gpu_capture_for_tests()
        accelerated = capture_canvas_opengl(scene, bounds, bounds, False)
        if accelerated is None:
            return {
                "schema": 1, "status": "opengl_unavailable",
                "explanation": "Cannot create an offscreen Qt OpenGL surface here; CPU mode remains active.",
                "width": width, "height": height,
            }
        # Compare an identical static scene (not a different animation time).
        cpu_pixels = pixels(cpu_capture(scene, bounds, False))
        gl_pixels = pixels(accelerated)
        diff = np.abs(cpu_pixels.astype(np.int16) - gl_pixels.astype(np.int16))
        quality = {
            "mean_absolute_channel_difference": round(float(diff.mean()), 5),
            "max_absolute_channel_difference": int(diff.max()),
            "channels_differing_over_4_percent": round(
                float(np.mean(diff > 4) * 100), 5,
            ),
            "cpu_sha256": sha256(cpu_pixels.tobytes()).hexdigest(),
            "opengl_sha256": sha256(gl_pixels.tobytes()).hexdigest(),
            "byte_identical": bool(np.array_equal(cpu_pixels, gl_pixels)),
        }
        for rep in range(repeats):
            order = ("cpu", "opengl") if rep % 2 == 0 else ("opengl", "cpu")
            for mode in order:
                started = perf_counter()
                for _ in range(frames):
                    if mode == "cpu":
                        result = cpu_capture(scene, bounds, False)
                    else:
                        result = capture_canvas_opengl(scene, bounds, bounds, False)
                        if result is None:
                            raise RuntimeError("OpenGL context was lost during benchmark")
                    if result.isNull():
                        raise RuntimeError("A Canvas frame was null")
                elapsed = perf_counter() - started
                results[mode].append(round(elapsed, 5))
        cpu_time = median(results["cpu"])
        gpu_time = median(results["opengl"])
        return {
            "schema": 1, "status": "measured",
            "scope": "QGraphicsScene paint + GPU->CPU readback; no FFmpeg/NVENC",
            "width": width, "height": height, "frames": frames,
            "repeats": repeats,
            "cpu_median_seconds": round(cpu_time, 5),
            "opengl_median_seconds": round(gpu_time, 5),
            "speedup": round(cpu_time / gpu_time, 4) if gpu_time else None,
            "quality": quality, "samples": results,
        }
    finally:
        if original is None:
            os.environ.pop(ENABLE_ENV, None)
        else:
            os.environ[ENABLE_ENV] = original
        reset_gpu_capture_for_tests()
        scene.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (not 128 <= args.width <= 3840 or not 128 <= args.height <= 2160
            or not 1 <= args.frames <= 120 or not 1 <= args.repeats <= 5):
        parser.error("Dimensions or repetitions out of range")
    report = run(args.width, args.height, args.frames, args.repeats)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
