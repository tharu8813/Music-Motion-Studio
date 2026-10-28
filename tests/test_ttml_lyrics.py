"""TTML input, explicit word timing and continuous ordinary lyric display."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.models.playlist import PlaylistTrack
from app.models.project import ProjectDocument
from app.models.source import Source, SourceType
from app.preview.frame_state import (
    resolve_lyrics_cue_state, resolve_lyrics_intro_state, resolve_lyrics_word_reveal,
)
from app.services.lyrics_intro_service import request_instrumental_analysis
from app.services.lyrics_service import LyricsError, LyricsService, find_sidecar_lyrics
from app.services.project_content_service import ProjectContentService
from app.services.project_service import ProjectService
from app.services.ttml_parser import parse_ttml


def document(body, attributes=""):
    return f'<tt xmlns="http://www.w3.org/ns/ttml" {attributes}><body>{body}</body></tt>'


class TTMLLyricsTests(unittest.TestCase):
    def test_standard_nested_offsets_and_words(self):
        cues = parse_ttml(document('<div begin="10s"><p begin="2s" dur="5s">'
            '<span begin="0.5s" dur="1s">Hello</span> '
            '<span begin="2s" end="3s">world</span></p></div>'))
        self.assertEqual(cues[0]["text"], "Hello world")
        self.assertEqual((cues[0]["start"], cues[0]["end"]), (12, 17))
        self.assertEqual(cues[0]["words"], [
            {"start": 12.5, "end": 13.5, "text": "Hello", "offset": 0},
            {"start": 14, "end": 15, "text": "world", "offset": 6},
        ])

    def test_apple_profile_uses_song_relative_times_at_every_level(self):
        cues = parse_ttml(document('<div begin="00:10.000" end="00:20.000">'
            '<p begin="00:12.000" end="00:17.000"><span begin="00:12.500" end="00:13.500">Hi</span>'
            ' <span begin="00:14.000" end="00:15.000">there</span></p></div>',
            'xmlns:itunes="http://itunes.apple.com/lyric-ttml-extensions" itunes:timing="Word"'))
        self.assertEqual((cues[0]["start"], cues[0]["end"]), (12, 17))
        self.assertEqual(cues[0]["words"][0]["start"], 12.5)
        self.assertEqual(cues[0]["words"][1]["offset"], 3)

    def test_internal_apple_namespace_and_numeric_seconds(self):
        cues = parse_ttml(document('<p begin="10.5" end="12"><span begin="10.5" end="11">한</span>'
            '<span begin="11" end="12">글</span></p>',
            'xmlns:i="http://music.apple.com/lyric-ttml-internal"'))
        self.assertEqual(cues[0]["text"], "한글")
        self.assertEqual(LyricsService.word_reveal(cues[0], 10.75), (0.5,))

    def test_apple_unpadded_minutes_import_and_project_save_roundtrip(self):
        # MelodyDock's Apple Line profile uses M:SS.mmm even in body dur.
        content = ('<tt xmlns="http://www.w3.org/ns/ttml" '
                   'xmlns:itunes="http://music.apple.com/lyric-ttml-internal" itunes:timing="Line">'
                   '<body dur="2:02.987"><div begin="0:05.000" end="2:00.500">'
                   '<p begin="0:05.250" end="0:08.500">First line</p>'
                   '<p begin="1:59.000" end="2:00.500">Last line</p>'
                   '</div></body></tt>')
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "song.ttml"
            path.write_text(content, encoding="utf-8")
            cues = LyricsService.load(path)
            self.assertEqual([(cue["start"], cue["end"]) for cue in cues], [(5.25, 8.5), (119, 120.5)])
            self.assertTrue(all("words" not in cue for cue in cues))
            document_data = ProjectDocument(playlist=[PlaylistTrack(
                "", "Song", lyrics_path=str(path), lyrics=cues)])
            for suffix in (".json", ".pvsproj"):
                with self.subTest(suffix=suffix):
                    saved = ProjectService.save(root / ("project" + suffix), document_data)
                    restored = ProjectService.load(saved)
                    self.assertEqual(restored.playlist[0].lyrics, cues)
                    self.assertEqual(LyricsService.load(restored.playlist[0].lyrics_path), cues)
            ProjectService.release_cache_locks()

    def test_apple_line_timing_never_invents_word_highlighting(self):
        cue = parse_ttml(document('<p begin="00:10.000" end="00:15.000">'
            '<span begin="00:10.000" end="00:11.000">Hello</span> world</p>',
            'xmlns:i="http://itunes.apple.com/lyric-ttml-extensions" i:timing="Line"'))[0]
        self.assertNotIn("words", cue)
        self.assertEqual(LyricsService.word_reveal(cue, 12), (11,))

    def test_apple_paragraph_is_clipped_to_parent_interval(self):
        cue = parse_ttml(document('<div begin="00:10.000" end="00:15.000">'
            '<p begin="00:08.000" end="00:20.000">Hello</p></div>',
            'xmlns:i="http://itunes.apple.com/lyric-ttml-extensions"'))[0]
        self.assertEqual((cue["start"], cue["end"]), (10, 15))

    def test_frame_subframe_tick_and_duration(self):
        cues = parse_ttml(document('<p begin="00:00:01:15.1" dur="60f">'
            '<span begin="25t" dur="500ms">A</span></p>',
            'xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:frameRate="30" '
            'ttp:subFrameRate="2" ttp:tickRate="100"'))
        self.assertAlmostEqual(cues[0]["start"], 1 + 15.5 / 30)
        self.assertAlmostEqual(cues[0]["end"] - cues[0]["start"], 2)
        self.assertAlmostEqual(cues[0]["words"][0]["start"] - cues[0]["start"], .25)

    def test_sequential_containers_and_parent_clipping(self):
        cues = parse_ttml(document('<div begin="1s" dur="4s" timeContainer="seq">'
            '<p dur="2s">First</p><p dur="4s">Second</p></div>'))
        self.assertEqual([(cue["start"], cue["end"]) for cue in cues], [(1, 3), (3, 5)])

    def test_inline_times_can_define_paragraph_end(self):
        cues = parse_ttml(document('<p><span begin="1s" end="2s">A</span><span begin="2s" end="3s">B</span></p>'))
        self.assertEqual(cues[0]["end"], 3)

    def test_multiline_nested_spans_entities_and_whitespace(self):
        cues = parse_ttml(document('<p begin="1s" end="6s">\n  '
            '<span begin="0s" end="2s"><span>A&amp;B</span></span> '
            '<span begin="2s" end="4s">😀</span><br/>번역 가사\n</p>'))
        cue = cues[0]
        self.assertEqual(cue["text"], "A&B 😀\n번역 가사")
        self.assertEqual([word["offset"] for word in cue["words"]], [0, 4])
        restored = PlaylistTrack.from_dict(PlaylistTrack("", "Track", lyrics=cues).to_dict())
        self.assertEqual(restored.lyrics, cues)

    def test_simultaneous_original_and_translation_keep_independent_words(self):
        cues = parse_ttml(document('<p begin="1s" end="4s"><span begin="0s" end="1s">Hello</span></p>'
            '<p begin="1s" end="4s"><span begin="0s" end="1s">안녕</span></p>'))
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["text"], "Hello\n안녕")
        self.assertEqual(cues[0]["words"][1]["offset"], 6)
        self.assertEqual(PlaylistTrack.from_dict(PlaylistTrack("", "Track", lyrics=cues).to_dict()).lyrics, cues)

    def test_invalid_input_is_a_lyrics_error_at_load(self):
        invalid = ["<tt>", "<html/>", '<!DOCTYPE tt [<!ENTITY x "a">]><tt/>',
            document('<p begin="bad" end="4s">A</p>'),
            document('<p begin="4s" end="2s">A</p>'),
            document('<p begin="1s">Unbounded</p>'),
            document('<p begin="1s" end="2s">A</p>', 'xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:frameRate="0"')]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "song.ttml"
            for content in invalid:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(LyricsError):
                        LyricsService.load(path)

    def test_utf16_case_insensitive_load_classification_and_discovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "Song.TTML"
            path.write_text(document('<p begin="1s" end="3s">한글</p>'), encoding="utf-16")
            (root / "Song.lrc").write_text("[00:01]한글", encoding="utf-8")
            cues = LyricsService.load(path)
            self.assertEqual(cues[0]["format"], "ttml")
            self.assertEqual(ProjectContentService.classify(path), "lyrics")
            exact, similar = find_sidecar_lyrics(root / "Song.mp3")
            self.assertEqual({item.path.name for item in exact}, {"Song.TTML", "Song.lrc"})
            self.assertEqual(similar, [])

    def test_ordinary_lyrics_are_complete_from_start_through_gaps(self):
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_karaoke_enabled=True)
        # Even enhanced LRC/previous audio-analysis word data is never animated.
        track = PlaylistTrack("missing.wav", "Track", lyrics=LyricsService._parse_lrc(
            "[00:05]<00:05>First<00:06>\n[00:12]Next"))
        track.lyrics[0]["end"] = 7
        for elapsed, expected, length in [(0, 0, 5), (5, 0, 5), (10, 0, 5), (12, 1, 4), (30, 1, 4)]:
            state = resolve_lyrics_cue_state(track, elapsed, 0, 0, "glow", .5, whole_lines=True)
            self.assertEqual(state.active_cue_index, expected)
            self.assertIsNone(state.release_progress)
            self.assertEqual(resolve_lyrics_word_reveal(track, source, elapsed, state, None), (length,))
            self.assertIsNone(resolve_lyrics_intro_state(track, source, elapsed))

    def test_ttml_uses_recorded_words_and_does_not_estimate_gaps(self):
        cue = parse_ttml(document('<p begin="1s" end="8s">Missing '
            '<span begin="2s" end="3s">word</span> tail</p>'))[0]
        self.assertEqual(LyricsService.word_reveal(cue, 2), (0,))
        self.assertEqual(LyricsService.word_reveal(cue, 3.5), (10,))
        self.assertEqual(LyricsService.word_reveal(cue, 7), (12,))

    def test_real_time_lyrics_never_request_vocal_analysis(self):
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_karaoke_enabled=True, subtitle_intro_midtrack=True)
        track = PlaylistTrack("missing.wav", "Track", duration_seconds=30, lyrics=parse_ttml(document(
            '<p begin="1s" end="3s">A</p><p begin="15s" end="20s">B</p>')))
        with patch("app.services.lyrics_intro_service._measure", side_effect=AssertionError("Audio read")):
            future = request_instrumental_analysis(track, source)
            self.assertTrue(future.done())
            self.assertEqual(future.result(), (((3, 15),), ()))



if __name__ == "__main__":
    unittest.main()
