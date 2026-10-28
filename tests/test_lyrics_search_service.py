from dataclasses import replace
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.models.playlist import PlaylistTrack
from app.models.project import ProjectDocument
from app.services.lyrics_search_service import (
    FoundLyrics, LyricsSearchCancelled, LyricsSearchError, LyricsSearchQuery,
    LyricsSearchResult, LyricsSearchService, LyricsRequestState, LIRIQO_API, PROVIDERS,
)
from app.services.project_service import ProjectService


TTML = ('<tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:i="http://music.apple.com/lyric-ttml-internal" i:timing="Word">'
        '<body dur="0:10.000"><p begin="0:01.000" end="0:05.000">'
        '<span begin="0:01.000" end="0:02.000">Hello</span> '
        '<span begin="0:03.000" end="0:04.000">world</span></p></body></tt>')


def lrc_result(**changes):
    return replace(LyricsSearchResult("lrclib", "42", "Song", "Artist", "Album", 120, "line",
                                     "[00:01.00]First\n[00:03.00]Next\n"), **changes)


class LyricsSearchServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = LyricsSearchService(LyricsRequestState(intervals={}, cooldown_delay=0))

    def test_title_required_before_any_request(self):
        with patch.object(self.service, "_get_json") as network:
            with self.assertRaises(LyricsSearchError):
                self.service.search(LyricsSearchQuery(" \t "))
            network.assert_not_called()

    def response(self, tracks=None, **metadata):
        return {"videoId": "video1", "metadata": {
            "title": "Song", "artist": "Artist", "album": "Album", "duration": 120, **metadata},
            "tracks": tracks if tracks is not None else [
                {"provider": "lrclib", "syncLevel": "line", "lrc": "[00:01]Words"}]}

    def test_unified_request_and_provider_filter(self):
        rows = [{"provider": key, "syncLevel": "line", "lrc": "[00:01]Words"} for key in PROVIDERS]
        with patch.object(self.service, "_get_json", return_value=self.response(rows)) as request:
            batch = self.service.search(LyricsSearchQuery("Song", "Artist", "Alb", 120, "qq_portato"))
        request.assert_called_once_with(LIRIQO_API, {"title": "Song", "artist": "Artist", "duration": 120}, None)
        self.assertEqual([r.provider for r in batch.results], ["qq_portato"])

    def test_quality_sorting_plain_filter_and_unusable_records(self):
        rows = [{"provider": "lrclib", "syncLevel": "line", "lrc": "[00:01]Words"},
                {"provider": "qq_portato", "syncLevel": "word", "timed": [{"text": "Words"}]},
                {"provider": "apple_music_liriqo", "syncLevel": "syllable", "ttml": TTML},
                {"provider": "netease", "syncLevel": "plain", "plain": "Words"},
                {"provider": "unknown", "syncLevel": "line", "lrc": "[00:01]X"}, None,
                {"provider": "lrclib", "syncLevel": "line"}]
        with patch.object(self.service, "_get_json", return_value=self.response(rows)):
            timed = self.service.search(LyricsSearchQuery("Song"))
            all_lyrics = self.service.search(LyricsSearchQuery("Song", synced_only=False))
        self.assertEqual([r.timing for r in timed.results], ["syllable", "word", "line"])
        self.assertEqual(all_lyrics.results[-1].timing, "plain")
        self.assertEqual(len({r.key for r in all_lyrics.results}), 4)

    def test_empty_and_invalid_provider_responses(self):
        with patch.object(self.service, "_get_json", return_value=self.response([])):
            self.assertEqual(self.service.search(LyricsSearchQuery("Song")).results, [])
        for payload in ([], {}, {"tracks": None}, {"tracks": [], "metadata": "bad"}):
            with self.subTest(payload=payload), patch.object(self.service, "_get_json", return_value=payload):
                with self.assertRaises(LyricsSearchError):
                    self.service.search(LyricsSearchQuery("Song"))

    def test_album_filters_actual_metadata(self):
        with patch.object(self.service, "_get_json", return_value=self.response()):
            self.assertEqual(self.service.search(LyricsSearchQuery("Song", album="Other")).results, [])
            self.assertEqual(len(self.service.search(LyricsSearchQuery("Song", album="Alb")).results), 1)

    def test_duration_helps_disambiguate_versions(self):
        query = LyricsSearchQuery("Song", "Artist", "Album", 121)
        self.assertGreater(self.service._score(query, lrc_result(duration=120)),
                           self.service._score(query, lrc_result(duration=300)))

    def test_cancellation_before_search_and_download_has_no_network(self):
        cancel = Event()
        cancel.set()
        with patch.object(self.service, "_get_json") as network:
            with self.assertRaises(LyricsSearchCancelled):
                self.service.search(LyricsSearchQuery("Song"), cancel)
            with self.assertRaises(LyricsSearchCancelled):
                self.service.fetch(lrc_result(), cancel)
            network.assert_not_called()

    def test_selected_ttml_preserves_word_times_without_another_request(self):
        selected = LyricsSearchResult("apple_music_liriqo", "video:1", "Song", "Artist", payload={"ttml": TTML})
        with patch.object(self.service, "_get_json") as request:
            document = self.service.fetch(selected)
        request.assert_not_called()
        self.assertEqual(document.content, TTML)
        self.assertEqual(document.format, "ttml")
        self.assertEqual([word["start"] for word in document.cues[0]["words"]], [1, 3])
        self.assertEqual(PlaylistTrack.from_dict(PlaylistTrack("", "Song", lyrics=document.cues).to_dict()).lyrics, document.cues)

    def test_millisecond_word_timings_and_text_survive_conversion(self):
        words = [{"text": "Hello", "start": 1000, "end": 2000},
                 {"text": "world & <you>", "start": 3000, "end": 4000}]
        for inline in (True, False):
            payload = {"timed": [{"text": "Hello world & <you>", "start": 1000, "end": 5000, "id": 0}]}
            if inline:
                payload["timed"][0]["words"] = words
            else:
                payload["words"] = [words]
            selected = LyricsSearchResult("qq_portato", "video:1", "Song", "Artist", timing="word", payload=payload)
            document = self.service.fetch(selected)
            self.assertEqual(document.cues[0]["text"], "Hello world & <you>")
            self.assertEqual((document.cues[0]["start"], document.cues[0]["end"]), (1, 5))
            self.assertEqual([w["start"] for w in document.cues[0]["words"]], [1, 3])
            self.assertEqual([w["offset"] for w in document.cues[0]["words"]], [0, 6])
            reparsed = self.service.fetch(replace(selected, payload={"ttml": document.content}))
            self.assertEqual(reparsed.cues, document.cues)

    def test_timed_line_without_lrc_preserves_end_and_has_no_word_timings(self):
        selected = LyricsSearchResult("netease", "video:1", "Song", "Artist", timing="line",
            payload={"timed": [{"text": "Words", "start": 1000, "end": 5000}]})
        document = self.service.fetch(selected)
        self.assertEqual((document.cues[0]["start"], document.cues[0]["end"]), (1, 5))
        self.assertFalse(document.cues[0].get("words"))

    def test_invalid_provider_timings_are_rejected(self):
        for start, end in ((float('nan'), 2000), (2000, 1000), (-1, 2000), (1000, float('inf'))):
            with self.subTest(start=start, end=end):
                selected = LyricsSearchResult("qq_portato", "video:1", "Song", "Artist", timing="word",
                    payload={"timed": [{"text": "Words", "start": start, "end": end}]})
                with self.assertRaises(LyricsSearchError):
                    self.service.fetch(selected)
        selected = LyricsSearchResult("qq_portato", "video:1", "Song", "Artist", timing="word", payload={
            "timed": [{"text": "Words", "start": 1000, "end": 2000,
                       "words": [{"text": "Words", "start": 3000, "end": 4000}]}]})
        with self.assertRaises(LyricsSearchError):
            self.service.fetch(selected)

    def test_lrclib_timing_is_not_promoted_to_word_timing(self):
        document = self.service.fetch(lrc_result())
        self.assertEqual(document.format, "lrc")
        self.assertTrue(document.cues)
        self.assertTrue(all("words" not in cue and cue.get("format") != "ttml" for cue in document.cues))

    def test_untimed_results_are_download_only(self):
        document = self.service.fetch(lrc_result(synced_lyrics="", plain_lyrics="Plain lyrics", timing="plain"))
        self.assertEqual((document.format, document.cues), ("txt", []))

    def test_unavailable_or_invalid_ttml_is_reported_before_attachment(self):
        for payload in ({"ttml": "<bad/>"}, {"ttml": "<tt>"}, {"plain": ""}):
            with self.subTest(payload=payload):
                selected = LyricsSearchResult("apple_music_liriqo", "999", "Song", "Artist", payload=payload)
                with self.assertRaises(LyricsSearchError):
                    self.service.fetch(selected)

    def test_original_download_and_embedded_project_roundtrip(self):
        document = self.service.fetch(lrc_result())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved = self.service.save(document, root / document.filename)
            self.assertEqual(saved.read_text(encoding="utf-8"), document.content)
            project = ProjectService.save(root / "lyrics.json", ProjectDocument(playlist=[
                PlaylistTrack("", "Song", lyrics=document.cues)]))
            self.assertEqual(ProjectService.load(project).playlist[0].lyrics, document.cues)
            with self.assertRaises(LyricsSearchError):
                self.service.save(document, root / "wrong.ttml")

    def test_failed_save_keeps_existing_file_and_cleans_temporary(self):
        document = self.service.fetch(lrc_result())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "song.lrc"
            path.write_text("previous", encoding="utf-8")
            with patch.object(Path, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(LyricsSearchError):
                    self.service.save(document, path)
            self.assertEqual(path.read_text(encoding="utf-8"), "previous")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_safe_download_filename(self):
        document = FoundLyrics(lrc_result(title='CON', artist=""), "lrc", "x")
        self.assertEqual(document.filename, "_CON.lrc")
        self.assertNotIn("/", replace(document, result=lrc_result(title="../bad/name")).filename)

    def test_http_handles_invalid_json_network_failures_and_response_size(self):
        with patch("app.services.lyrics_search_service.urlopen", return_value=io.BytesIO(b"not json")):
            with self.assertRaisesRegex(LyricsSearchError, "Invalid provider response"):
                self.service._get_json("https://example.com", {}, None)
        with patch("app.services.lyrics_search_service.urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(LyricsSearchError, "Cannot connect"):
                self.service._get_json("https://example.com", {}, None)
        with patch("app.services.lyrics_search_service.MAX_RESPONSE_BYTES", 4), \
             patch("app.services.lyrics_search_service.urlopen", return_value=io.BytesIO(b"123456")):
            with self.assertRaisesRegex(LyricsSearchError, "too large"):
                self.service._get_json("https://example.com", {}, None)
        with patch("app.services.lyrics_search_service.urlopen", side_effect=HTTPError("", 503, "Unavailable", {}, None)):
            with self.assertRaisesRegex(LyricsSearchError, "temporary failure"):
                self.service._get_json("https://example.com", {}, None)

    def test_shared_response_cache_avoids_repeated_requests_when_filters_change(self):
        raw = json.dumps(self.response()).encode()
        with patch("app.services.lyrics_search_service.urlopen", return_value=io.BytesIO(raw)) as network:
            self.service.search(LyricsSearchQuery("Song"))
            reopened = LyricsSearchService(self.service.request_state)
            batch = reopened.search(LyricsSearchQuery("Song", provider="lrclib"))
        network.assert_called_once()
        self.assertEqual(batch.results[0].title, "Song")

    def test_liriqo_requests_are_paced_across_different_dialog_instances(self):
        state = LyricsRequestState()
        clock = [0.0]
        waits = []
        def wait(seconds, cancel):
            waits.append(seconds)
            clock[0] += seconds
        with patch("app.services.lyrics_search_service.monotonic", side_effect=lambda: clock[0]), \
             patch("app.services.lyrics_search_service.urlopen", side_effect=[io.BytesIO(b"[]"), io.BytesIO(b"[]")]):
            first, second = LyricsSearchService(state), LyricsSearchService(state)
            with patch.object(first, "_wait", side_effect=wait), patch.object(second, "_wait", side_effect=wait):
                first._get_json(LIRIQO_API, {"title": "a"}, None)
                second._get_json(LIRIQO_API, {"title": "b"}, None)
        self.assertEqual([delay for delay in waits if delay > 0], [1.0])

    def test_rate_limit_and_server_errors_never_trigger_automatic_retries(self):
        for code in (429, 500, 502, 503, 504):
            with self.subTest(code=code):
                service = LyricsSearchService(LyricsRequestState(intervals={}, cooldown_delay=0))
                error = HTTPError("", code, "Unavailable", {"Retry-After": "2"}, None)
                with patch("app.services.lyrics_search_service.monotonic", return_value=10), \
                     patch("app.services.lyrics_search_service.urlopen", side_effect=error) as network:
                    with self.assertRaises(LyricsSearchError):
                        service._get_json(LIRIQO_API, {"q": "a"}, None)
                network.assert_called_once()
                self.assertEqual(service.request_state.host("api.liriqo-alfarrizi.my.id").next_request, 12)

    def test_long_server_cooldown_is_retained_and_never_retried_early(self):
        limited = HTTPError("", 429, "Too many", {"Retry-After": "120"}, None)
        with patch("app.services.lyrics_search_service.monotonic", return_value=10), \
             patch("app.services.lyrics_search_service.urlopen", side_effect=limited) as network:
            with self.assertRaises(LyricsSearchError):
                self.service._get_json(LIRIQO_API, {"q": "a"}, None)
            reopened = LyricsSearchService(self.service.request_state)
            with self.assertRaisesRegex(LyricsSearchError, "rate limit"):
                reopened._get_json(LIRIQO_API, {"q": "b"}, None)
        network.assert_called_once()

    def test_retry_after_http_date_and_cancellable_wait(self):
        from email.utils import formatdate
        with patch("app.services.lyrics_search_service.time", return_value=1_700_000_000):
            self.assertEqual(self.service._retry_after({"Retry-After": formatdate(1_700_000_010, usegmt=True)}, 5), 10)
        cancel = Event()
        cancel.set()
        with self.assertRaises(LyricsSearchCancelled):
            self.service._wait(30, cancel)

    def test_http_encodes_query_and_uses_bounded_timeout(self):
        with patch("app.services.lyrics_search_service.urlopen", return_value=io.BytesIO(json.dumps([]).encode())) as network:
            self.service._get_json("https://example.com", {"title": "한글 & Song"}, None)
        self.assertIn("%26", network.call_args.args[0].full_url)
        self.assertEqual(network.call_args.kwargs["timeout"], 10)


if __name__ == "__main__":
    unittest.main()
