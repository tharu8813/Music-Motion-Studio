"""Reproduce lyric transitions and rapid advanced-setting edits offscreen."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests  # Isolate Qt settings, drafts and temporary data from the user's app.

from PySide6.QtWidgets import QApplication
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.utils.i18n import Translator
import app.canvas.source_item as source_module


def timing_benchmark():
    from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog
    application = QApplication.instance() or QApplication([])
    dialog = LrcGeneratorDialog([], Translator(), initial_cues=[
        {"start": i * 2, "end": i * 2 + 2, "text": f"{i}: 가사 타이밍 편집"}
        for i in range(2000)
    ])
    timings = []
    for position in range(100000, 104000, 100):
        started = perf_counter()
        dialog._position_changed(position)
        timings.append((perf_counter() - started) * 1000)
    first_item = dialog.timeline_table.item(0, 2)
    started = perf_counter()
    dialog.timestamps[51] = 102.5
    dialog._refresh_table(51)
    result = {"playback_median_ms": statistics.median(timings),
              "playback_p95_ms": sorted(timings)[37],
              "single_time_edit_ms": (perf_counter() - started) * 1000,
              "unchanged_cell_reused": first_item is dialog.timeline_table.item(0, 2)}
    dialog.close()
    application.processEvents()
    return result


def run():
    application = QApplication.instance() or QApplication([])
    cases = {}
    for effect in ("rise", "glow", "karaoke"):
        scene = CanvasScene()
        scene.set_artboard_size(1280, 720)
        source = Source(SourceType.LYRICS, "Lyrics", width=1000, height=680,
                        font_size=32, fill_color="#00000000",
                        subtitle_context_lines=3, subtitle_next_lines=3,
                        subtitle_animation="glow" if effect == "glow" else "rise",
                        subtitle_animation_duration=0.8, subtitle_accent_enabled=True,
                        subtitle_accent_color="#ffc85a", subtitle_glow_radius=12,
                        subtitle_karaoke_enabled=effect == "karaoke",
                        subtitle_role_styles={role: {"blur": 5, "opacity": 0.4}
                                              for role in ("previous", "next")})
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("", "Long multilingual lyrics", duration_seconds=4000,
                              lyrics=[{"start": i * 2, "end": (i + 1) * 2,
                                       "text": f"{i}: 음악이 흐르는 밤 🌙\nMusic brings us together"}
                                      for i in range(2000)])
        timings, digest = [], hashlib.sha256()
        with patch.object(source_module, "gaussian_filter", wraps=source_module.gaussian_filter) as blur:
            for frame in range(90):
                started = perf_counter()
                image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                                                     elapsed_seconds=100 + frame / 30,
                                                     transparent=True)
                timings.append((perf_counter() - started) * 1000)
                digest.update(bytes(image.constBits()))
        cases[effect] = {"median_ms": statistics.median(timings),
                         "p95_ms": sorted(timings)[85], "gaussian_calls": blur.call_count,
                         "all_frames_sha256": digest.hexdigest()}
        scene.clear()
    dialog = LyricsAnimationDialog(Source(SourceType.LYRICS, "Lyrics"), Translator())
    dialog.resize(1200, 800)
    dialog.show()
    application.processEvents()
    for toggle in dialog.category_toggles.values():
        toggle.setChecked(True)
    application.processEvents()
    with patch.object(dialog, "_render_preview", wraps=dialog._render_preview) as render:
        started = perf_counter()
        dialog.preview_count.setValue(13)
        direction = dialog.controls["subtitle_flow_direction"]
        direction.setCurrentIndex(direction.findData("left"))
        control = dialog.controls["subtitle_motion_distance"]
        for value in range(21, 41):
            control.setValue(value)
        application.processEvents()
        cases["rapid_settings"] = {"elapsed_ms": (perf_counter() - started) * 1000,
                                   "preview_renders": render.call_count,
                                   "final_distance": dialog._preview_source.subtitle_motion_distance,
                                   "preview_image_width": dialog.preview.pixmap().width()}
    dialog.close()
    return {"fixture": "90 frames, 1280x720, 2000 bilingual cues; 22 rapid edits, 13 horizontal cues",
            "cases": cases}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timing-only", action="store_true")
    args = parser.parse_args()
    result = {"timing_editor": timing_benchmark()} if args.timing_only else run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
