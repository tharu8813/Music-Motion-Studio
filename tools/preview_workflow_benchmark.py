"""Simulate a burst of timeline drag events with a real Qt lyric preview."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests  # Isolate settings, caches and temporary audio from the user's app.

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.export_preview_dialog import ExportPreviewDialog, TIMELINE_SCALE
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Language, Translator


def run(screenshot=None):
    app = QApplication.instance() or QApplication([])
    apply_studio_style(app)
    translator = Translator()
    translator.set_language(Language.KOREAN)
    with tempfile.TemporaryDirectory() as directory:
        audio = Path(directory) / "preview.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(8000)
            stream.writeframes(b"\0\0" * (8000 * 120))
        scene = CanvasScene()
        scene.set_artboard_size(1280, 720)
        item = SourceItem(Source(SourceType.LYRICS, "Lyrics", x=70, y=20,
                                 width=1140, height=680, font_size=32,
                                 fill_color="#00000000", subtitle_animation="glow",
                                 subtitle_context_lines=3, subtitle_next_lines=3,
                                 subtitle_glow_radius=12, subtitle_accent_enabled=True,
                                 subtitle_accent_color="#ffda65"))
        scene.addItem(item)
        track = PlaylistTrack(str(audio), "미리보기 응답성 예시", duration_seconds=120,
                              lyrics=[{"start": i * 0.06, "end": (i + 1) * 0.06,
                                       "text": f"{i}: 음악이 흐르는 밤\nMusic brings us together"}
                                      for i in range(2000)])
        preview = ExportPreviewDialog(scene, [track], translator, preferred_backend="cpu")
        preview.resize(1200, 800)
        preview.show()
        preview.audio_output.setMuted(True)
        preview.play_button.setChecked(True)
        QTest.qWait(150)
        timeline = preview.timeline
        final_x = round((timeline.width() - 18) * 0.8 + 9)
        expected = round(0.8 * timeline.maximum())
        started = perf_counter()
        with patch.object(preview, "refresh_preview", wraps=preview.refresh_preview) as render, \
             patch.object(preview, "_start_audio_at_playhead", wraps=preview._start_audio_at_playhead) as seek:
            QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
            for index in range(60):
                x = 20 + (final_x - 20) * index / 59
                position = QPointF(x, 20)
                event = QMouseEvent(QEvent.Type.MouseMove, position, QPointF(timeline.mapToGlobal(position.toPoint())),
                                    Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                    Qt.KeyboardModifier.NoModifier)
                QApplication.sendEvent(timeline, event)
            QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=QPoint(final_x, 20))
            burst_ms = (perf_counter() - started) * 1000
            result = {"fixture": "CPU Qt preview, 1280x720, 2000 bilingual cues, 60 drag moves",
                      "drag_burst_ms": burst_ms, "preview_refresh_calls": render.call_count,
                      "audio_seek_requests": seek.call_count,
                      "final_seconds": timeline.value() / TIMELINE_SCALE,
                      "expected_seconds_approx": expected / TIMELINE_SCALE,
                      "playing_after_release": preview._playing}
        preview.play_button.setChecked(False)
        app.processEvents()
        if screenshot:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            preview.grab().save(str(screenshot))
        preview._stop_preview()
        preview.close()
        preview.deleteLater()
        scene.clear()
        app.processEvents()
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    result = run(args.screenshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
