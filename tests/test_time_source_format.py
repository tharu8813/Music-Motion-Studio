"""Time-source formatting preserves existing text templates and exports."""

from __future__ import annotations

import unittest

from app.models.playlist import PlaylistTrack
from app.preview.text_template import expand_track_template, format_timestamp


class TimeSourceFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.track = PlaylistTrack(
            file_path="song.mp3", title="Song", duration_seconds=4505,
        )
        self.template = "%track_current_time% / %track_total_time% · %video_total_time%"

    def expand(self, time_format: str = "HH:mm") -> str:
        return expand_track_template(
            self.template, self.track, 1, 1, 0,
            track_elapsed_seconds=65, playlist_duration_seconds=4505,
            time_format=time_format,
        )

    def test_legacy_default_keeps_adaptive_clock(self) -> None:
        self.assertEqual(self.expand(), "01:05 / 01:15:05 · 01:15:05")
        self.assertEqual(format_timestamp(4505), "01:15:05")

    def test_minutes_mode_keeps_counting_after_an_hour(self) -> None:
        self.assertEqual(self.expand("mm:ss"), "01:05 / 75:05 · 75:05")

    def test_fixed_hours_mode_keeps_leading_zero_hour(self) -> None:
        self.assertEqual(self.expand("HH:mm:ss"), "00:01:05 / 01:15:05 · 01:15:05")
        self.assertEqual(format_timestamp(-1, time_format="HH:mm:ss"), "00:00:00")

    def test_unknown_tokens_and_regular_text_remain_unchanged(self) -> None:
        self.assertEqual(
            expand_track_template(
                "%title% %unknown% %current_time%",
                self.track, 1, 1, 0, 5,
            ),
            "Song %unknown% 00:05",
        )


if __name__ == "__main__":
    unittest.main()
