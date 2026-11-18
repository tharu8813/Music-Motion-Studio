"""Inspector/catalog, Canvas and thumbnail coverage for the new design collection."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.canvas.source_item import SourceItem  # noqa: E402
from app.inspector.source_inspector import SourceInspector  # noqa: E402
from app.models.source import Source, SourceType  # noqa: E402
from app.services.source_store import SourceStore  # noqa: E402
from app.utils.i18n import Translator  # noqa: E402
from app.widgets.visual_preset_combo import _preview_icon  # noqa: E402


DESIGN_FAMILIES = {
    "progress_style": (SourceType.PROGRESS_BAR, "progress", "rounded",
                       ("segments", "rail", "neon")),
    "now_playing_style": (SourceType.NOW_PLAYING, "now_playing", "card",
                          ("outline", "accent", "gradient")),
    "track_list_style": (SourceType.TRACK_LIST, "track_list", "compact",
                          ("spotlight", "outline", "stripes")),
    "album_frame_style": (SourceType.ALBUM_COVER, "album_frame", "rounded",
                           ("vinyl", "filmstrip")),
}


class ElementDesignCollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def render(source: Source, *, use_artwork: bool = False) -> QImage:
        item = SourceItem(source)
        if use_artwork:
            art = QImage(100, 100, QImage.Format.Format_ARGB32_Premultiplied)
            for y in range(100):
                for x in range(100):
                    art.setPixelColor(x, y, "#43D6C5" if (x + y) % 11 < 6 else "#315E9C")
            item._pixmap = QPixmap.fromImage(art)
        result = QImage(330, 190, QImage.Format.Format_ARGB32_Premultiplied)
        result.fill(0)
        painter = QPainter(result)
        item.paint(painter, None)
        painter.end()
        return result

    @staticmethod
    def source(kind: SourceType) -> Source:
        params = {
            "width": 300, "height": 110,
            "fill_color": "#286FB0", "outline_color": "#F1F4F5",
            "border_radius": 12,
        }
        if kind is SourceType.PROGRESS_BAR:
            params.update(height=30, progress_value=0.56, progress_track_color="#24313D")
        elif kind is SourceType.NOW_PLAYING:
            params.update(height=125, text="NOW PLAYING\\nTrack title\\nArtist name")
        elif kind is SourceType.TRACK_LIST:
            params.update(height=160, text="▶ 01. Song\\n02. Another song\\n03. More music",
                          track_list_current_row=1, track_list_current_background="#1676CA")
        elif kind is SourceType.ALBUM_COVER:
            params.update(width=160, height=160, image_fit_mode="cover")
        return Source(kind, "Design sample", **params)

    def test_all_new_designs_are_selectable_and_round_trip_without_schema_changes(self) -> None:
        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.deleteLater)
        families = {
            "progress_style": inspector.progress_style_combo,
            "now_playing_style": inspector.now_playing.widgets["now_playing_style"],
            "track_list_style": inspector.track_list.widgets["track_list_style"],
            "album_frame_style": inspector.album_frame_combo,
        }
        for field, (kind, _gallery_kind, _legacy, variants) in DESIGN_FAMILIES.items():
            with self.subTest(field=field):
                source = self.source(kind)
                store.add(source)
                combo = families[field]
                for variant in variants:
                    index = combo.findData(variant)
                    self.assertGreaterEqual(index, 0, (field, variant))
                    combo.setCurrentIndex(index)
                    self.assertEqual(getattr(source, field), variant)
                    restored = Source.from_dict(source.to_dict())
                    self.assertEqual(getattr(restored, field), variant)
                self.assertTrue(inspector._field_visibility.get(
                    "album_frame" if field == "album_frame_style" else field
                ))

    def test_each_new_style_has_own_canvas_and_gallery_appearance(self) -> None:
        for field, (kind, gallery_kind, original, variants) in DESIGN_FAMILIES.items():
            source = self.source(kind)
            use_artwork = kind is SourceType.ALBUM_COVER
            images = {}
            cards = {}
            for style in (original, *variants):
                setattr(source, field, style)
                canvas = self.render(source, use_artwork=use_artwork)
                card = _preview_icon(gallery_kind, style, 0, source).pixmap(112, 72).toImage()
                self.assertFalse(canvas.isNull())
                self.assertFalse(card.isNull())
                images[style] = canvas
                cards[style] = card
            for style in variants:
                with self.subTest(field=field, style=style):
                    self.assertNotEqual(images[original], images[style])
                    self.assertNotEqual(cards[original], cards[style])
            for index, first in enumerate(variants):
                for second in variants[index + 1:]:
                    with self.subTest(field=field, first=first, second=second):
                        self.assertNotEqual(images[first], images[second])
                        self.assertNotEqual(cards[first], cards[second])

    def test_progress_designs_respect_zero_and_full_progress(self) -> None:
        source = self.source(SourceType.PROGRESS_BAR)
        for style in ("segments", "rail", "neon"):
            with self.subTest(style=style):
                source.progress_style = style
                source.progress_value = 0.0
                empty = self.render(source)
                source.progress_value = 1.0
                full = self.render(source)
                self.assertNotEqual(empty, full)

    def test_album_framing_preserves_square_source_and_cover_content(self) -> None:
        source = self.source(SourceType.ALBUM_COVER)
        for style in ("vinyl", "filmstrip"):
            with self.subTest(style=style):
                source.album_frame_style = style
                image = self.render(source, use_artwork=True)
                self.assertGreater(image.pixelColor(80, 80).alpha(), 0)
                self.assertGreater(image.pixelColor(22, 22).alpha(), 0)
                self.assertEqual(Source.from_dict(source.to_dict()).album_frame_style, style)


if __name__ == "__main__":
    unittest.main()
