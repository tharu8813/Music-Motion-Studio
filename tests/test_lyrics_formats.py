"""Subtitle imports reject bad timing before it can poison project persistence."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.models.playlist import PlaylistTrack
from app.services.lyrics_service import LyricsError, LyricsService


class SubtitleFormatTests(unittest.TestCase):
    def test_vtt_accepts_optional_hours_identifiers_and_cue_settings(self):
        content = """WEBVTT

NOTE ignored comment
00:09.000 --> 00:10.000
Not a cue

intro
00:01.000 --> 00:03.250 align:start position:10%
First line
Second line

chorus
01:02:03.000 --> 01:02:04.000
Later
"""
        with TemporaryDirectory() as raw:
            path = Path(raw) / "words.vtt"
            path.write_text(content, encoding="utf-8")
            cues = LyricsService.load(path)
        self.assertEqual(cues, [
            {"start": 1.0, "end": 3.25, "text": "First line\nSecond line"},
            {"start": 3723.0, "end": 3724.0, "text": "Later"},
        ])
        self.assertEqual(PlaylistTrack.from_dict({"file_path": "song.wav", "title": "Song", "lyrics": cues}).lyrics, cues)

    def test_invalid_timestamps_and_backwards_cues_are_rejected(self):
        for start, end in (
            ("00:00:10,000", "00:00:05,000"),
            ("00:60:00,000", "01:01:00,000"),
            ("00:00:60,000", "00:01:01,000"),
            ("-00:00:01,000", "00:00:02,000"),
            ("00:00:01,0000", "00:00:02,000"),
            ("9" * 4000 + ":00:00,000", "00:00:02,000"),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(LyricsError):
                LyricsService._parse_srt(f"1\n{start} --> {end}\nWords\n")

    def test_srt_fractional_seconds_are_decimal_fractions(self):
        cues = LyricsService._parse_srt("1\n00:00:01,5 --> 00:00:02,25\nWords\n")
        self.assertEqual((cues[0]["start"], cues[0]["end"]), (1.5, 2.25))
