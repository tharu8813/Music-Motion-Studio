"""Renderer/thumbnail contracts for every waveform style and popup invalidation."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.canvas.source_item import SourceItem  # noqa: E402
from app.models.source import Source, SourceType  # noqa: E402
from app.renderer.python_visualizer import PythonVisualizerRenderer  # noqa: E402
from app.widgets.visual_preset_combo import (  # noqa: E402
    VisualPresetComboBox, _preview_color, _preview_icon,
)


class VisualPresetPreviewParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def raster(source: Source) -> QImage:
        output = QImage(300, 150, QImage.Format.Format_ARGB32_Premultiplied)
        output.fill(Qt.GlobalColor.transparent)
        painter = QPainter(output)
        SourceItem(source).paint(painter, None)
        painter.end()
        return output

    @staticmethod
    def thumbnail(kind: str, style: str, source: Source | None = None) -> QImage:
        return _preview_icon(kind, style, 0.45, source).pixmap(112, 72).toImage()

    def test_waveform_styles_are_distinct_in_popup_canvas_and_python_export(self) -> None:
        source = Source(
            SourceType.AUDIO_WAVEFORM, "Wave",
            width=240, height=85, visualizer_bars=48,
            visualizer_line_width=3, fill_color="#33DDEE",
        )
        popup, canvas, export = [], [], []
        samples = np.array([0.15 + 0.8 * abs(np.sin(i * 0.43)) for i in range(48)],
                           dtype=np.float32)
        for style in ("line", "filled", "mirror"):
            source.waveform_style = style
            popup.append(self.thumbnail("waveform", style, source))
            canvas.append(self.raster(source))
            overlay = SimpleNamespace(
                kind="waveform", style=style, effect_style=style,
                color=source.fill_color, line_width=source.visualizer_line_width,
            )
            export.append(PythonVisualizerRenderer._draw_frame(
                240, 85, overlay, samples,
            ))
        for title, images in (("popup", popup), ("canvas", canvas), ("export", export)):
            with self.subTest(view=title):
                self.assertNotEqual(images[0], images[1])
                self.assertNotEqual(images[0], images[2])
                self.assertNotEqual(images[1], images[2])

    def test_text_and_lyrics_animations_sample_their_real_glyph_colors(self) -> None:
        source = Source(
            SourceType.TEXT, "Text", fill_color="#00000000",
            outline_color="#38EE17",
        )
        color = _preview_color("entrance", source, QColor("#7C3AED"))
        self.assertEqual(color.name(), "#38ee17")
        dark = Source(
            SourceType.TEXT, "Dark", fill_color="#00000000",
            outline_color="#00000000",
        )
        self.assertEqual(
            _preview_color("entrance", dark, QColor("#7C3AED")).name(),
            "#7c3aed",
        )
        lyrics = Source(
            SourceType.LYRICS, "Lyrics", fill_color="#00000000",
            outline_color="#FFFFFF", subtitle_accent_enabled=True,
            subtitle_accent_color="#FFCC22",
        )
        self.assertEqual(
            _preview_color("lyric", lyrics, QColor("#7C3AED")).name(),
            "#ffcc22",
        )

    def test_progress_gradient_is_distinct_in_popup_and_canvas(self) -> None:
        source = Source(
            SourceType.PROGRESS_BAR, "Progress",
            width=240, height=40, fill_color="#399BDA",
        )
        popup = [self.thumbnail("progress", value, source)
                 for value in ("rounded", "gradient")]
        self.assertNotEqual(*popup)
        canvas = []
        for value in ("rounded", "gradient"):
            source.progress_style = value
            canvas.append(self.raster(source))
        self.assertNotEqual(*canvas)

    def test_mutated_source_rebuilds_open_popup_thumbnail_without_closing(self) -> None:
        source = Source(
            SourceType.TEXT, "Text",
            outline_color="#44FF00", fill_color="#00000000",
        )
        combo = VisualPresetComboBox("entrance")
        self.addCleanup(combo.close)
        self.addCleanup(combo.deleteLater)
        combo.addItem("Slide", "slide_left")
        combo.set_preview_source(source)
        combo.showPopup()
        view = combo._list
        self.assertIsNotNone(view)
        delegate = view.itemDelegate()
        before = delegate._icon(combo, "slide_left", 1.0, False).pixmap(112, 72).toImage()
        self.assertTrue(delegate._still_icons)
        source.outline_color = "#FF22DD"
        combo.set_preview_source(source)
        self.assertIs(combo._list, view)
        self.assertEqual(delegate._still_icons, {})
        after = delegate._icon(combo, "slide_left", 1.0, False).pixmap(112, 72).toImage()
        self.assertNotEqual(before, after)
        combo.hidePopup()

    def test_popup_reopen_disposes_timer_and_view_repeatedly(self) -> None:
        combo = VisualPresetComboBox("entrance")
        self.addCleanup(combo.close)
        self.addCleanup(combo.deleteLater)
        for label in ("Fade", "Slide", "Zoom"):
            combo.addItem(label, label.lower())
        for _ in range(12):
            combo.showPopup()
            self.assertIsNotNone(combo._list)
            combo.hidePopup()
            self.assertIsNone(combo._list)
            self.assertIsNone(combo._popup)
            self.assertFalse(combo._timer.isActive())


    def test_entrance_cards_remain_visible_even_when_duration_divides_old_sample_time(self) -> None:
        source = Source(
            SourceType.TEXT, "Fade", fill_color="#00000000",
            outline_color="#FFFFFF", animation_in_duration=0.4,
        )
        combo = VisualPresetComboBox("entrance")
        self.addCleanup(combo.close)
        self.addCleanup(combo.deleteLater)
        combo.addItem("Fade", "fade")
        combo.set_preview_source(source)
        combo.showPopup()
        delegate = combo._list.itemDelegate()
        for duration in (0.2, 0.4, 0.8, 1.6):
            with self.subTest(duration=duration):
                source.animation_in_duration = duration
                combo.set_preview_source(source)
                image = delegate._icon(combo, "fade", 1.0, False).pixmap(112, 72).toImage()
                self.assertGreater(
                    image.pixelColor(56, 27).lightness()
                    - image.pixelColor(12, 12).lightness(), 45,
                )
        combo.hidePopup()

    def test_inspector_source_change_refreshes_open_gallery_without_dismissal(self) -> None:
        from app.inspector.source_inspector import SourceInspector
        from app.services.source_store import SourceStore
        from app.utils.i18n import Translator

        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.close)
        self.addCleanup(inspector.deleteLater)
        source = Source(SourceType.TEXT, "Live", outline_color="#44FF00")
        store.add(source)
        combo = inspector.animation_in_combo
        combo.showPopup()
        delegate = combo._list.itemDelegate()
        original = delegate._icon(combo, "slide_left", 1.0, False).pixmap(112, 72).toImage()
        store.update(source.id, outline_color="#CC33FF")
        self.assertIsNotNone(combo._list)
        updated = delegate._icon(combo, "slide_left", 1.0, False).pixmap(112, 72).toImage()
        self.assertNotEqual(original, updated)
        combo.hidePopup()


if __name__ == "__main__":
    unittest.main()
