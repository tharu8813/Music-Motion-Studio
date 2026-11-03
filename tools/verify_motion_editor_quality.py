"""Native UI captures, mixed-project timing and resource checks for motion editing.

Run each language/DPI in a separate process; do not run alongside run_tests.py.
Writes evidence under work/ and uses isolated INI settings.
"""
import argparse
import gc
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, QRect, QSettings
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.animation.custom_motion import CustomMotion, MotionPoint
from app.animation.keyframes import Keyframe
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.custom_motion_dialog import CustomMotionDialog
from app.dialogs.motion_editor_guide import MOTION_GUIDE_KEY
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Translator
from app.utils.subprocess_utils import hidden_process_kwargs
from tests.test_real_video_preview_performance import _resident_set_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("ko", "en"), default="ko")
    parser.add_argument("--samples", type=int, default=80)
    parser.add_argument("--cycles", type=int, default=5)
    args = parser.parse_args()
    output = ROOT / "work" / f"motion-quality-{args.language}"
    output.mkdir(parents=True, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(output / "settings"))
    app = QApplication([])
    app.setOrganizationName("MotionEditorQualityCheck")
    app.setApplicationName(args.language)
    apply_studio_style(app)
    QSettings().setValue(MOTION_GUIDE_KEY, True)
    translator = Translator()
    translator.set_language(args.language)
    image = QImage(256, 256, QImage.Format.Format_RGB32)
    image.fill(QColor("#527A6D"))
    image.save(str(output / "image.png"))
    ffmpeg = shutil.which("ffmpeg")
    video = output / "video.mp4"
    if ffmpeg:
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=20:d=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-y", str(video)],
            check=True, capture_output=True, timeout=20, **hidden_process_kwargs())
    scene = CanvasScene()
    scene.set_artboard_size(3840, 2160)
    models = []
    for i in range(50):
        models.append(Source(SourceType.IMAGE if i < 30 else SourceType.TEXT, f"Layer {i}",
            x=80+(i % 10)*350, y=80+(i//10)*330, width=240, height=180,
            content_path=str(output / "image.png") if i < 30 else "", text=f"Music Motion {i}", font_size=32))
    models.append(Source(SourceType.AUDIO_VISUALIZER, "Audio sample", x=100, y=1900, width=1200, height=160))
    if video.is_file():
        models.append(Source(SourceType.VIDEO, "Video", x=1600, y=1800, width=480, height=270,
            video_paths=[str(video)], video_repeat_mode="loop_one"))
    target = Source(SourceType.SHAPE, "Motion / 모션", x=1900, y=800, width=400, height=250,
        fill_color="#B7D0C4", animation_in="custom", animation_out="custom",
        custom_intro_keyframes=CustomMotion(tuple(MotionPoint(i/199, dx=i*.5, scale=1+i/1000) for i in range(200))),
        custom_outro_keyframes=CustomMotion.default(False))
    target.keyframes.global_curves = {"x": [Keyframe(0, 900)]}
    models.append(target)
    for model in models:
        scene.addItem(SourceItem(model))
    saved = deepcopy([model.to_dict() for model in models])
    tracks = [PlaylistTrack("", "Quality check", duration_seconds=10)]
    timings, cycles, layouts = [], [], []
    for cycle in range(args.cycles):
        begin = perf_counter()
        dialog = CustomMotionDialog(target, scene, tracks, translator)
        dialog.show()
        app.processEvents()
        opened = (perf_counter()-begin)*1000
        for i in range(args.samples if cycle == 0 else 10):
            begin = perf_counter()
            dialog.seek((i % 100)/100)
            dialog.render_now()
            app.processEvents()
            if cycle == 0:
                timings.append((perf_counter()-begin)*1000)
        if cycle == 0:
            for advanced in (False, True):
                dialog.set_advanced(advanced)
                dialog.adapt_to_screen()
                dialog.resize(1180, 880)
                app.processEvents()
                mode = "advanced" if advanced else "simple"
                dialog.grab().save(str(output / f"{mode}.png"))
                with patch.object(dialog, "screen", return_value=SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1024, 768))):
                    dialog.adapt_to_screen()
                    dialog.resize(840, 620)
                    app.processEvents()
                    dialog.render_now()
                    app.processEvents()
                    dialog.grab().save(str(output / f"{mode}-small.png"))
                    assert dialog.rect().contains(dialog.apply_button.mapTo(dialog, dialog.apply_button.rect().bottomRight()))
                    assert dialog.canvas.viewport().height() >= 200
                    assert dialog.frameGeometry().width() <= 1024
                    assert dialog.frameGeometry().height() <= 768
                    for spin in dialog.fields.values():
                        assert dialog.property_scroll.viewport().rect().contains(spin.mapTo(dialog.property_scroll.viewport(), spin.rect().bottomRight()))
                    layouts.append({"mode": mode, "client": [dialog.width(), dialog.height()],
                        "frame": [dialog.frameGeometry().width(), dialog.frameGeometry().height()],
                        "viewport": [dialog.canvas.viewport().width(), dialog.canvas.viewport().height()]})
        decoders = tuple(dialog.canvas.media.items)
        dialog.reject()
        assert all(item._video_player is None and item._video_sink is None for item in decoders)
        assert not dialog.timer.isActive() and not dialog.render_timer.isActive()
        assert saved == [model.to_dict() for model in models]
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(50)
        gc.collect()
        cycles.append({"open_ms": round(opened, 2), "rss_bytes_after_close": _resident_set_bytes()})
    result = {"language": args.language, "dpi_scale": os.environ.get("QT_SCALE_FACTOR", "1"),
        "canvas": [3840, 2160], "sources": len(models), "motion_points": 200, "samples": len(timings),
        "seek_and_render_ms": {"median": round(float(np.median(timings)), 2),
            "p95": round(float(np.percentile(timings, 95)), 2), "max": round(max(timings), 2)},
        "cycles": cycles, "small_screen_layouts": layouts, "source_data_preserved": True, "decoders_released": True}
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    for item in scene.items():
        if isinstance(item, SourceItem):
            item.release_video_decoder()
    scene.clear()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    main()
