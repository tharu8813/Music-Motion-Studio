"""Native Korean/English timing dialog layout and keyboard checks.

Run with QT_SCALE_FACTOR=1/1.5/2 in separate processes.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
from PySide6.QtCore import Qt, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
from app.timeline.precision_time_dialog import PrecisionTimeDialog
from app.ui.design_system import apply_studio_style


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("ko", "en"), default="ko")
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "work" / f"precise-ui-{args.language}-{os.environ.get('QT_SCALE_FACTOR', '1')}"
    output.mkdir(parents=True, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(output / "settings"))
    app = QApplication([])
    apply_studio_style(app)
    results = []
    for track, full in ((True, False), (False, False), (False, True)):
        dialog = PrecisionTimeDialog("Music / 음악" if track else "Title / 제목", 83.45, 0 if full else 12.345,
            track=track, full=full, playlist_end=180, korean=args.language == "ko")
        dialog.show()
        app.processEvents()
        assert dialog.screen().availableGeometry().contains(dialog.frameGeometry())
        assert dialog.rect().contains(dialog.apply_button.mapTo(dialog, dialog.apply_button.rect().bottomRight()))
        for field in dialog.fields.values():
            assert field.fontMetrics().horizontalAdvance(field.text()) < field.contentsRect().width()-8
        name = "track" if track else "full" if full else "source"
        dialog.grab().save(str(output / f"{name}.png"))
        field = dialog.fields["start"]
        screen = dialog.screen().availableGeometry()
        dialog.move(screen.right()-dialog.width()+1, screen.bottom()-dialog.height()+1)
        field.selectAll()
        QTest.keyClicks(field, "-1")
        QTest.keyClick(field, Qt.Key.Key_Tab)
        app.processEvents()
        assert dialog.error.text()
        assert screen.contains(dialog.frameGeometry())
        field.setFocus()
        field.selectAll()
        QTest.keyClicks(field, "01:23.451")
        QTest.keyClick(field, Qt.Key.Key_Tab)
        assert dialog.start == 83.451
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.timing == (83.451, 0 if full else 12.345)
        results.append({"mode": name, "size": [dialog.width(), dialog.height()], "keyboard": "passed", "in_screen": True})
        dialog.deleteLater()
        app.processEvents()
    (output / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results))


if __name__ == "__main__":
    main()
