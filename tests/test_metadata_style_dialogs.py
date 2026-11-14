"""Regression checks for the two editors with the real studio stylesheet."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mutagen.id3 import APIC, ID3, TIT2, TXXX
from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFormLayout, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTextEdit, QWidget,
)

from app.dialogs.lyrics_line_style_dialog import LyricsLineStyleDialog
from app.dialogs.mp3_metadata_editor_dialog import Mp3MetadataEditorDialog
from app.models.source import Source, SourceType
from app.ui.design_system import COLORS, apply_studio_style, studio_stylesheet


class TranslatorStub(QObject):
    language_changed = Signal()
    is_korean = True


class MetadataStyleDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def test_disabled_settings_use_distinct_text_and_background(self):
        window = QWidget()
        self.addCleanup(window.close)
        form = QFormLayout(window)
        pairs = []
        for cls in (QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QTextEdit, QPlainTextEdit):
            enabled, disabled = cls(), cls()
            disabled.setEnabled(False)
            form.addRow(enabled, disabled)
            pairs.append((enabled, disabled))
        # Color swatches have local styles; disabling must still use the shared skin.
        swatch = QPushButton("#FFFFFF")
        swatch.setStyleSheet("QPushButton:enabled { background: white; color: black; }")
        swatch.setEnabled(False)
        form.addRow(swatch)
        window.show()
        self.app.processEvents()
        for enabled, disabled in pairs:
            with self.subTest(widget=type(disabled).__name__):
                self.assertEqual(enabled.palette().color(QPalette.ColorGroup.Active, QPalette.ColorRole.Text),
                                 QColor(COLORS["text"]))
                self.assertEqual(disabled.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text),
                                 QColor(COLORS["disabled_text"]))
                self.assertEqual(disabled.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base),
                                 QColor(COLORS["disabled"]))
        self.assertEqual(swatch.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText),
                         QColor(COLORS["disabled_text"]))
        # Qt spin boxes create their own child line edits, which also must dim.
        for _, disabled in pairs[1:3]:
            self.assertEqual(disabled.findChild(QLineEdit).palette().color(
                QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text), QColor(COLORS["disabled_text"]))
        sheet = studio_stylesheet()
        self.assertNotIn("@", sheet)
        for name in ("spin_up_disabled.svg", "spin_down_disabled.svg"):
            self.assertIn(name, sheet)
            self.assertTrue((Path(__file__).resolve().parents[1] / "app/assets/icons" / name).is_file())

    def test_metadata_layout_and_tag_roundtrip(self):
        translator = TranslatorStub()
        dialog = Mp3MetadataEditorDialog(translator)
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()
        self.assertTrue(dialog._cover_preview.text())
        self.assertFalse(dialog._remove_cover_button.isEnabled())
        for korean in (True, False):
            translator.is_korean = korean
            translator.language_changed.emit()
            self.app.processEvents()
            fields = [dialog._edits[key] for key in ("title", "artist", "album", "album_artist")]
            for before, after in zip(fields, fields[1:]):
                self.assertLess(before.geometry().bottom(), after.geometry().top())
            scroll = dialog.findChild(QScrollArea)
            self.assertEqual(scroll.verticalScrollBar().maximum(), 0)
        dialog.resize(dialog.minimumSize())
        self.app.processEvents()
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        self.assertTrue(dialog.rect().contains(dialog._buttons.geometry()))

        with TemporaryDirectory() as directory:
            path = Path(directory) / "track.mp3"
            image = QImage(16, 16, QImage.Format.Format_RGB32)
            image.fill(QColor("#79C7B4"))
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            tags = ID3()
            tags.add(TIT2(encoding=3, text="Original"))
            tags.add(TXXX(encoding=3, desc="untouched", text="Keep me"))
            tags.add(APIC(encoding=3, mime="image/png", type=3, data=bytes(buffer.data())))
            tags.save(path)
            dialog.load_file(path)
            self.assertTrue(dialog._remove_cover_button.isEnabled())
            self.assertFalse(dialog._cover_preview.pixmap().isNull())
            dialog._edits["title"].setText("Edited")
            QTest.mouseClick(dialog._save_button, Qt.MouseButton.LeftButton)
            saved = ID3(path)
            self.assertEqual(saved["TIT2"].text, ["Edited"])
            self.assertEqual(saved["TXXX:untouched"].text, ["Keep me"])
            self.assertEqual(len(saved.getall("APIC")), 1)
            QTest.mouseClick(dialog._remove_cover_button, Qt.MouseButton.LeftButton)
            self.assertTrue(dialog._cover_preview.text())
            self.assertTrue(dialog._cover_preview.pixmap().isNull())
            self.assertFalse(dialog._remove_cover_button.isEnabled())
            QTest.mouseClick(dialog._save_button, Qt.MouseButton.LeftButton)
            self.assertEqual(ID3(path).getall("APIC"), [])

    def test_line_preview_changes_and_restores_inheritance(self):
        source = Source(SourceType.LYRICS, "Lyrics", font_size=24, font_weight=500,
                        text_italic=True, outline_color="#E6E8E7")
        original = source.to_dict()
        dialog = LyricsLineStyleDialog(source, TranslatorStub())
        self.addCleanup(dialog.close)
        dialog.show()
        self.app.processEvents()
        self.assertGreaterEqual(dialog.line_list.visualItemRect(dialog.line_list.item(0)).height(), 36)
        self.assertEqual(dialog.line_title.text(), "2번째 줄")
        self.assertEqual(dialog.line_list.count(), 11)
        self.assertEqual(sum(not dialog.line_list.item(row).isHidden() for row in range(11)), 3)
        self.assertEqual(dialog.expand_lines.text(), "목록 펼치기")
        self.assertTrue(dialog.preview_before.isVisible())
        self.assertEqual(dialog.preview_before.font().pixelSize(), 24)
        self.assertEqual(dialog.preview.font().pixelSize(), 24)
        dialog.enabled.setChecked(True)
        dialog._set_color("#79C7B4")
        dialog.font_size.setValue(12)
        dialog.font_weight.setCurrentIndex(dialog.font_weight.findData(800))
        dialog.italic.setChecked(False)
        self.app.processEvents()
        self.assertEqual(dialog.preview.font().pixelSize(), 36)
        self.assertEqual(dialog.preview_before.font().pixelSize(), 24)
        self.assertEqual(int(dialog.preview.font().weight()), 800)
        self.assertFalse(dialog.preview.font().italic())
        dialog.size_mode.setCurrentIndex(dialog.size_mode.findData("absolute"))
        self.assertEqual(dialog.styles()[0], {})
        self.assertEqual(dialog.styles()[1]["font_size"], 36)
        dialog.size_mode.setCurrentIndex(dialog.size_mode.findData("relative"))
        self.assertEqual(dialog.styles()[1]["font_size_offset"], 12)
        dialog.enabled.setChecked(False)
        self.app.processEvents()
        self.assertEqual(dialog.styles(), [])
        self.assertEqual(dialog._color, source.outline_color)
        self.assertEqual(dialog.preview.font().pixelSize(), 24)
        self.assertEqual(int(dialog.preview.font().weight()), 500)
        self.assertTrue(dialog.preview.font().italic())
        self.assertFalse(dialog.color_button.isEnabled())
        QTest.mouseClick(dialog.expand_lines, Qt.MouseButton.LeftButton)
        self.assertEqual(sum(not dialog.line_list.item(row).isHidden() for row in range(11)), 11)
        dialog.line_list.setCurrentRow(10)
        self.assertEqual(dialog.line_title.text(), "12번째 줄")
        self.assertFalse(dialog.preview_before.isVisible())
        dialog.enabled.setChecked(True)
        dialog.font_size.setValue(76)
        self.app.processEvents()
        self.assertEqual(dialog.preview.font().pixelSize(), 48)
        self.assertIn("48", dialog.preview_detail.text())
        self.assertEqual(dialog.styles()[11]["font_size_offset"], 76)
        QTest.mouseClick(dialog.expand_lines, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.line_list.currentRow(), 2)
        self.assertEqual(sum(not dialog.line_list.item(row).isHidden() for row in range(11)), 3)
        QTest.mouseClick(dialog.expand_lines, Qt.MouseButton.LeftButton)
        dialog.line_list.setCurrentRow(10)
        self.assertEqual(dialog.font_size.value(), 76)
        dialog._clear_all()
        self.assertEqual(dialog.styles(), [])
        self.assertEqual(source.to_dict(), original)

    def test_hidden_first_line_keeps_existing_style_and_preview(self):
        first = {"color": "#79C7B4", "font_size": 20, "italic": True}
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_line_styles=[first, {"font_size": 32}])
        dialog = LyricsLineStyleDialog(source, TranslatorStub())
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.preview_before.font().pixelSize(), 20)
        self.assertTrue(dialog.preview_before.font().italic())
        dialog.font_size.setValue(40)
        self.assertEqual(dialog.styles()[0], first)
        self.assertEqual(dialog.styles()[1]["font_size"], 40)
        dialog._clear_all()
        self.assertEqual(dialog.styles(), [first])

    def test_advanced_roles_keep_per_line_typography_editable_and_ignore_old_overrides(self):
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_advanced_categories=["styles"],
                        subtitle_advanced_settings={"subtitle_role_styles": {
                            "current": {"color": "#79C7B4", "font_weight": 800, "italic": True, "scale": 1.2},
                        }})
        dialog = LyricsLineStyleDialog(source, TranslatorStub())
        self.addCleanup(dialog.close)
        dialog.enabled.setChecked(True)
        self.assertTrue(dialog.font_size.isEnabled())
        self.assertTrue(dialog.color_button.isEnabled())
        self.assertTrue(dialog.font_weight.isEnabled())
        self.assertTrue(dialog.italic.isEnabled())
        dialog._set_color("#0000FF")
        dialog.font_weight.setCurrentIndex(dialog.font_weight.findData(700))
        dialog.italic.setChecked(True)
        dialog.font_size.setValue(10)
        self.assertEqual(dialog.styles()[1]["font_size_offset"], 10)
        self.assertEqual(dialog.styles()[1]["color"], "#0000FF")
        self.assertEqual(int(dialog.preview.font().weight()), 700)
        self.assertTrue(dialog.preview.font().italic())
        resolved = source.resolved_lyrics()
        self.assertEqual(resolved.font_weight, source.font_weight)
        self.assertEqual(resolved.text_italic, source.text_italic)
        self.assertEqual(resolved.subtitle_role_styles["current"], {"scale": 1.2})

        from app.inspector.source_inspector import SourceInspector
        from app.services.source_store import SourceStore
        from app.utils.i18n import Translator

        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.close)
        self.addCleanup(inspector.deleteLater)
        store.add(source)
        for key in ("text_color", "font_weight", "text_italic", "subtitle_accent_enabled"):
            self.assertTrue(inspector._field_widgets[key].isEnabled(), key)
            self.assertNotIn(key, inspector._lyrics_locked_fields)
        self.assertEqual(inspector._lyrics_locked_fields,
                         {"subtitle_previous_opacity", "subtitle_current_scale"})


if __name__ == "__main__":
    unittest.main()
