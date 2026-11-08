"""Track details: modern responsive player and actual TTML word progress."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication

from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.models.playlist import PlaylistTrack
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Translator
from app.widgets.track_lyrics_preview import KaraokeLyricLabel, fit_context_lyrics


def word_ttml():
    return [
        {
            "format": "ttml", "start": 1.0, "end": 4.0,
            "text": "Hello world",
            "words": [
                {"offset": 0, "text": "Hello", "start": 1.0, "end": 2.5},
                {"offset": 6, "text": "world", "start": 2.5, "end": 4.0},
            ],
        },
        {
            "format": "ttml", "start": 5.0, "end": 7.0,
            "text": "Next phrase",
            "words": [
                {"offset": 0, "text": "Next", "start": 5.0, "end": 6.0},
                {"offset": 5, "text": "phrase", "start": 6.0, "end": 7.0},
            ],
        },
    ]


class ResponsiveTrackLyricPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def test_context_rows_increase_with_available_height(self) -> None:
        font = QFont()
        font.setPointSize(12)
        metrics = QFontMetrics(font)
        previous = ["past 1", "past 2", "past 3", "past 4", "past 5"]
        following = ["next 1", "next 2", "next 3", "next 4", "next 5"]
        small = fit_context_lyrics(
            previous, following, available_height=70,
            available_width=250, metrics=metrics,
        )
        large = fit_context_lyrics(
            previous, following, available_height=280,
            available_width=250, metrics=metrics,
        )
        self.assertLess(sum(map(len, small)), sum(map(len, large)))
        self.assertEqual(large[0], list(reversed(previous)))
        self.assertEqual(large[1], following)
        self.assertTrue(all(row in previous for row in small[0]))
        self.assertEqual(small[1], following[:len(small[1])])

    def test_long_multiline_cues_consume_more_vertical_space(self) -> None:
        metrics = QFontMetrics(QFont("Segoe UI", 11))
        short = fit_context_lyrics(
            ["short 1", "short 2"], ["short 3", "short 4"],
            available_height=105, available_width=155, metrics=metrics,
        )
        long = fit_context_lyrics(
            ["Long phrase with many more words wrapping onto another line\ntranslated"],
            ["Another long phrase with even more words that wrap"],
            available_height=105, available_width=155, metrics=metrics,
        )
        self.assertLessEqual(sum(map(len, long)), sum(map(len, short)))

    def test_ttml_word_timing_is_independent_and_partial(self) -> None:
        cue = word_ttml()[0]
        before = KaraokeLyricLabel.ranges_for_cues([cue], 1.0)
        during = KaraokeLyricLabel.ranges_for_cues([cue], 2.0)
        later = KaraokeLyricLabel.ranges_for_cues([cue], 3.1)
        self.assertIsNotNone(before)
        self.assertEqual(before[0], ())
        self.assertTrue(during[0])
        self.assertLess(during[0][0][1], 5.0)
        self.assertTrue(any(a >= 6 for a, _ in later[0]))
        self.assertIsNone(KaraokeLyricLabel.ranges_for_cues([
            {"format": "lrc", "start": 1, "end": 3, "text": "Plain"},
        ], 2.0))

    def test_word_highlighting_changes_actual_painted_pixels(self) -> None:
        label = KaraokeLyricLabel()
        label.resize(300, 85)
        label.setText("Hello world")
        label.show()
        self.app.processEvents()
        try:
            label.set_word_ranges(
                KaraokeLyricLabel.ranges_for_cues(word_ttml()[:1], 1.0),
            )
            before = bytes(label.grab().toImage().constBits())
            label.set_word_ranges(
                KaraokeLyricLabel.ranges_for_cues(word_ttml()[:1], 3.2),
            )
            after = bytes(label.grab().toImage().constBits())
            self.assertNotEqual(before, after)
        finally:
            label.close()

    def test_dialog_uses_ttml_timing_and_preserves_overlap(self) -> None:
        lyrics = word_ttml()
        lyrics.append({
            "format": "ttml", "start": 2.0, "end": 5.0, "text": "Overlap",
            "words": [{"offset": 0, "text": "Overlap", "start": 2.0, "end": 5.0}],
        })
        lyrics.sort(key=lambda cue: cue["start"])
        dialog = TrackDetailsDialog(
            PlaylistTrack("not-on-disk.wav", "Track title",
                          artist="Artist name", duration_seconds=9, lyrics=lyrics),
            Translator(),
        )
        try:
            self.assertEqual(dialog.playback_track_title.text(), "Track title")
            self.assertEqual(dialog.playback_track_artist.text(), "Artist name")
            self.assertIn("TTML", dialog.lyrics_mode_badge.text())
            self.assertEqual(dialog.lyrics_preview_layout.indexOf(dialog.current_lyric), 1)
            dialog._update_live_lyrics(2500)
            self.assertEqual(dialog.current_lyric.text(), "Hello world\nOverlap")
            self.assertTrue(dialog.current_lyric.has_word_timing)
            ranges = dialog.current_lyric._timed_lines
            self.assertTrue(ranges and ranges[0])
            dialog.timing_offset_spin.setValue(1.0)
            dialog._update_live_lyrics(2500)
            self.assertNotEqual(dialog.current_lyric._timed_lines, ranges)
        finally:
            dialog.reject()

    def test_dialog_shows_more_neighbors_when_viewport_is_taller(self) -> None:
        cues = [
            {"start": i * 2.0, "end": i * 2.0 + 1.5, "text": f"Cue {i}"}
            for i in range(30)
        ]
        dialog = TrackDetailsDialog(
            PlaylistTrack("not-on-disk.wav", "Track", duration_seconds=60, lyrics=cues),
            Translator(),
        )
        try:
            dialog._update_live_lyrics(25000)
            dialog.lyrics_preview_host.resize(290, 165)
            dialog._update_live_lyrics(25000)
            small = len(dialog.previous_lyric.text().splitlines()) + len(
                dialog.next_lyric.text().splitlines(),
            )
            dialog.lyrics_preview_host.resize(290, 450)
            dialog._update_live_lyrics(25000)
            large = len(dialog.previous_lyric.text().splitlines()) + len(
                dialog.next_lyric.text().splitlines(),
            )
            self.assertGreater(large, small)
        finally:
            dialog.reject()


if __name__ == "__main__":
    unittest.main()
