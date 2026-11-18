from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import _effective_lyric_context


class AutomaticLineCountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    @staticmethod
    def _track_list(height: float, count: int = 0) -> SourceItem:
        return SourceItem(Source(
            SourceType.TRACK_LIST,
            "Track list",
            width=460,
            height=height,
            font_size=20,
            text="▶ 01. Current track\n  02. Next track",
            track_list_count=count,
        ))

    def test_automatic_track_count_expands_with_source_height(self) -> None:
        short = self._track_list(100)
        tall = self._track_list(420)

        self.assertGreater(
            tall.effective_track_list_count(),
            short.effective_track_list_count(),
        )
        self.assertGreater(len(tall.track_list_display_lines()), 2)
        self.assertEqual(
            len(tall.track_list_display_lines()),
            tall.effective_track_list_count(),
        )

    def test_explicit_track_count_is_not_overridden_by_height(self) -> None:
        item = self._track_list(500, count=2)
        self.assertEqual(item.effective_track_list_count(), 2)
        self.assertEqual(len(item.track_list_display_lines()), 2)

    def test_manual_track_count_is_capped_by_available_height(self) -> None:
        item = self._track_list(95, count=15)
        self.assertGreaterEqual(item.effective_track_list_count(), 1)
        self.assertLess(item.effective_track_list_count(), 15)
        self.assertEqual(
            len(item.track_list_display_lines()), item.effective_track_list_count(),
        )

    def test_new_row_designs_use_card_spacing_in_auto_count(self) -> None:
        item = self._track_list(210)
        item.source.track_list_style = "cards"
        card_rows = item.effective_track_list_count()
        for style in ("spotlight", "outline", "stripes"):
            with self.subTest(style=style):
                item.source.track_list_style = style
                self.assertEqual(item.effective_track_list_count(), card_rows)

    def test_tiny_track_list_never_paints_outside_the_source(self) -> None:
        for style in ("cards", "spotlight", "outline", "stripes"):
            with self.subTest(style=style):
                source = Source(
                    SourceType.TRACK_LIST, "Many rows",
                    width=160, height=90, font_size=20,
                    track_list_style=style, track_list_count=15,
                    track_list_row_spacing=40, track_list_item_padding=10,
                    text="\n".join(f"{index:02d}. Track" for index in range(15)),
                    fill_color="#213347",
                )
                item = SourceItem(source)
                image = QImage(180, 260, QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(0)
                painter = QPainter(image)
                item.paint(painter, None)
                painter.end()
                self.assertTrue(
                    all(image.pixelColor(x, y).alpha() == 0
                        for x in range(0, 160, 4) for y in range(91, 255, 4))
                )

    def test_automatic_lyric_context_uses_height_and_multiline_cost(self) -> None:
        cues = [
            {"start": float(index), "end": float(index + 1), "text": f"Line {index}"}
            for index in range(12)
        ]
        short = SourceItem(Source(
            SourceType.LYRICS, "Lyrics", height=110,
            subtitle_context_lines=-1, subtitle_next_lines=-1,
        ))
        tall = SourceItem(Source(
            SourceType.LYRICS, "Lyrics", height=420,
            subtitle_context_lines=-1, subtitle_next_lines=-1,
        ))
        short_context = _effective_lyric_context(short, cues, 6)
        tall_context = _effective_lyric_context(tall, cues, 6)
        self.assertGreater(sum(tall_context), sum(short_context))

        multiline = list(cues)
        multiline[5] = {
            "start": 5.0, "end": 6.0, "text": "First\\nSecond\\nThird",
        }
        self.assertLessEqual(
            sum(_effective_lyric_context(tall, multiline, 6)),
            sum(tall_context),
        )

    def test_automatic_values_round_trip_through_project_data(self) -> None:
        source = Source(
            SourceType.LYRICS, "Lyrics",
            subtitle_context_lines=-1,
            subtitle_next_lines=-1,
        )
        restored = Source.from_dict(source.to_dict())
        self.assertEqual(restored.subtitle_context_lines, -1)
        self.assertEqual(restored.subtitle_next_lines, -1)

        tracks = Source(SourceType.TRACK_LIST, "Tracks", track_list_count=0)
        self.assertEqual(Source.from_dict(tracks.to_dict()).track_list_count, 0)


if __name__ == "__main__":
    unittest.main()
