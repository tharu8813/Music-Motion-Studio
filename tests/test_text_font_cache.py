"""Qt font-template cache preserves glyph geometry, pixels and source edits."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType


def original_font(item: SourceItem, size: int, weight: int) -> QFont:
    """Former uncached text_font implementation, for exact reference."""
    source = item.source
    font = QFont(source.font_family, size)
    font.setWeight(QFont.Weight(weight))
    font.setItalic(source.text_italic)
    if source.text_letter_spacing:
        font.setLetterSpacing(
            QFont.SpacingType.AbsoluteSpacing, source.text_letter_spacing,
        )
    capitalization = {
        "upper": QFont.Capitalization.AllUppercase,
        "lower": QFont.Capitalization.AllLowercase,
        "capitalize": QFont.Capitalization.Capitalize,
        "small_caps": QFont.Capitalization.SmallCaps,
    }
    font.setCapitalization(capitalization.get(
        source.text_case, QFont.Capitalization.MixedCase,
    ))
    return font


def text_pixels(font: QFont) -> bytes:
    image = QImage(280, 96, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(font)
        painter.setPen(QColor("#fafafa"))
        painter.drawText(QRectF(6.0, 3.0, 270.0, 84.0),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         "Hello 한글 123")
    finally:
        painter.end()
    return bytes(image.constBits())


class SourceFontCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt = QApplication.instance() or QApplication([])

    def test_source_typography_matches_old_keys_metrics_and_pixels(self) -> None:
        source = Source(SourceType.TEXT, "Sample")
        item = SourceItem(source)
        for family, weight, spacing, case, italic in (
            ("Segoe UI", 400, 0.0, "", False),
            ("Segoe UI", 600, 1.5, "upper", True),
            ("Arial", 300, 2.0, "small_caps", False),
        ):
            with self.subTest(family=family, weight=weight, case=case):
                source.font_family = family
                source.text_letter_spacing = spacing
                source.text_case = case
                source.text_italic = italic
                actual = item.text_font(32, weight)
                baseline = original_font(item, 32, weight)
                self.assertEqual(actual.key(), baseline.key())
                self.assertEqual(
                    QFontMetricsF(actual).horizontalAdvance("Hello 한글 123"),
                    QFontMetricsF(baseline).horizontalAdvance("Hello 한글 123"),
                )
                self.assertEqual(text_pixels(actual), text_pixels(baseline))
                self.assertEqual(item.text_font(32, weight).key(), baseline.key())

    def test_mutating_return_value_does_not_mutate_cached_template(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Sample"))
        baseline = original_font(item, 36, 500)
        font = item.text_font(36, 500)
        font.setFamily("Courier New")
        font.setWeight(QFont.Weight.Bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 8.0)
        new_font = item.text_font(36, 500)
        self.assertEqual(new_font.key(), baseline.key())
        self.assertEqual(text_pixels(new_font), text_pixels(baseline))

    def test_animation_changes_are_bounded_to_64_fonts(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Animated"))
        for size in range(10, 220):
            item.text_font(size, 500)
        self.assertLessEqual(len(item._text_font_cache), 64)
        self.assertEqual(item.text_font(36, 500).key(),
                         original_font(item, 36, 500).key())

    def test_apply_source_invalidates_existing_cached_family(self) -> None:
        source = Source(SourceType.TEXT, "Editable")
        item = SourceItem(source)
        source.font_family = "OldFamily"
        item.text_font(42, 500)
        self.assertTrue(any(key[0] == "OldFamily" for key in item._text_font_cache))
        source.font_family = "Arial"
        item.apply_source()
        self.assertFalse(any(key[0] == "OldFamily" for key in item._text_font_cache))
        self.assertEqual(item.text_font(42, 500).key(),
                         original_font(item, 42, 500).key())


if __name__ == "__main__":
    unittest.main()
