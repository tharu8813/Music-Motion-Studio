"""Render translation windows with fixtures, without network or real credentials.

    python scripts/capture_translation_workspace.py
    QT_SCALE_FACTOR=1.5 python scripts/capture_translation_workspace.py --suffix dpi150
"""
import argparse
import os
from pathlib import Path
import sys
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from app.dialogs.lyrics_translation_dialog import LyricsTranslationDialog
from app.dialogs.local_llm_manager_dialog import LocalLLMManagerDialog
from app.dialogs.lyrics_translation_style_dialog import LyricsTranslationStyleDialog
from app.models.source import Source, SourceType
from app.services.lyrics_translation_service import TranslationConfig, apply_translations
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Translator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", default="")
    args = parser.parse_args()
    output = ROOT / "work/translation-workspace" / (args.suffix or "default")
    output.mkdir(parents=True, exist_ok=True)
    for previous in ("review.png", "review-compact.png"):
        (output / previous).unlink(missing_ok=True)
    app = QApplication([])
    apply_studio_style(app)
    settings = QSettings(str(output / "fixture.ini"), QSettings.Format.IniFormat)
    settings.clear()
    settings.setValue("translation/provider", "openai")
    settings.setValue("translation/config/openai/model", "example-model")
    cues = [{"start": i * 5 + 1, "text": text} for i, text in enumerate([
        "A quiet street beneath the morning sky", "I keep your words beside the open door",
        "The rain will pass\nAnd we will sing again", "A thousand lights across the sleeping town",
        "We take our time and let the evening stay", "The road remembers every step we made",
        "A quiet street beneath the morning sky", "And we will sing again"])]
    cues = apply_translations(cues, ["아침 하늘 아래 조용한 거리", "열린 문 곁에 네 말을 간직해",
        "비는 지나가고\n우리는 다시 노래할 거야", "잠든 마을을 가로지르는 수많은 불빛",
        "서두르지 않고 저녁을 머물게 해", "길은 우리가 내디딘 모든 걸음을 기억해",
        "아침 하늘 아래 조용한 거리", "우리는 다시 노래할 거야"], TranslationConfig("openai", model="example-model"))
    dialog = LyricsTranslationDialog(cues, Translator(), settings=settings, style_callback=lambda _parent: None)
    dialog.key_edit.setText("fixture-only")
    dialog.review_panel.table.item(1, 3).setText("열린 문 곁에 네 말을 소중히 간직해")
    windows = [("main", dialog), ("service", dialog.service_dialog),
               ("advanced", dialog.advanced_dialog)]
    local = Mock(provider="ollama", base="http://localhost:11434")
    local.discover.return_value = [
        {"id": "qwen3:4b", "size": 2_500_000_000, "parameters": "4B", "quantization": "Q4_K_M"},
        {"id": "qwen3:8b", "size": 5_200_000_000, "parameters": "8B", "quantization": "Q4_K_M"}]
    manager = LocalLLMManagerDialog(Translator(), service=local)
    windows.append(("models", manager))
    style = LyricsTranslationStyleDialog(Source(SourceType.LYRICS, "Lyrics"), Translator())
    windows.append(("style", style))
    for name, window in windows:
        window.show()
        for _ in range(40):
            app.processEvents()
            if name == "models" and manager.worker:
                manager.worker.wait(10)
        window.grab().save(str(output / f"{name}.png"))
        if name in {"main", "service", "advanced", "models"}:
            original = window.size()
            window.resize(window.minimumSize())
            QTest.qWait(100)
            window.grab().save(str(output / f"{name}-compact.png"))
            window.resize(original)
        window.hide()
    if manager.worker:
        manager.worker.cancel.set()
        manager.worker.wait()
        app.processEvents()
    for _, window in windows:
        window.deleteLater()
    app.processEvents()
    print(output)


if __name__ == "__main__":
    main()
