import unittest
from unittest.mock import patch

from app.services.lyrics_search_service import (
    LRCLIB_SEARCH_API, LyricsRequestState, LyricsSearchError, LyricsSearchQuery, LyricsSearchService,
)


def record(**changes):
    return {'id': 42, 'trackName': 'Song', 'artistName': 'Artist', 'albumName': 'Album', 'duration': 123,
            'instrumental': False, 'syncedLyrics': '[00:01.00]First\n[00:03.00]Next\n',
            'plainLyrics': 'First\nNext', **changes}


class LRCLIBProviderTests(unittest.TestCase):
    def setUp(self):
        self.service = LyricsSearchService(LyricsRequestState(intervals={}))

    def test_explicit_search_keeps_selected_record_without_refetch_or_duration_constraint(self):
        with patch.object(self.service, '_get_json', return_value=[record(), record(id=43, duration=300)]) as request:
            batch = self.service.search(LyricsSearchQuery(' Song ', ' Artist ', 'Other album', 124, 'lrclib'))
            document = self.service.fetch(batch.results[0])
        request.assert_called_once_with(LRCLIB_SEARCH_API, {'track_name': 'Song', 'artist_name': 'Artist'}, None)
        self.assertEqual(batch.provider_counts, {'lrclib': 2})
        self.assertEqual(document.result.identifier, '42')
        self.assertEqual(document.content, record()['syncedLyrics'])
        self.assertEqual(document.format, 'lrc')
        self.assertEqual(document.result.timing, 'line')
        self.assertEqual(document.cues[0]['start'], 1)
        self.assertNotIn('words', document.cues[0])

    def test_title_only_search_omits_optional_fields(self):
        with patch.object(self.service, '_get_json', return_value=[]) as request:
            batch = self.service.search(LyricsSearchQuery('제목', provider='lrclib'))
        request.assert_called_once_with(LRCLIB_SEARCH_API, {'track_name': '제목'}, None)
        self.assertEqual(batch.provider_counts, {'lrclib': 0})

    def test_timed_filter_and_plain_text_download(self):
        data = [record(), record(id=43, syncedLyrics=None), record(id=44, syncedLyrics=' ', plainLyrics=' '),
                record(id=45, instrumental=True)]
        with patch.object(self.service, '_get_json', return_value=data):
            self.assertEqual(len(self.service.search(LyricsSearchQuery('Song', provider='lrclib')).results), 1)
            results = self.service.search(LyricsSearchQuery('Song', provider='lrclib', synced_only=False)).results
            self.assertEqual(len(results), 2)
            document = self.service.fetch(next(result for result in results if result.identifier == '43'))
        self.assertEqual(document.format, 'txt')
        self.assertEqual(document.content, 'First\nNext')
        self.assertEqual(document.cues, [])

    def test_invalid_response_and_bad_or_duplicate_rows(self):
        for invalid in ({}, None, {'statusCode': 500}):
            with self.subTest(invalid=invalid), patch.object(self.service, '_get_json', return_value=invalid):
                with self.assertRaises(LyricsSearchError):
                    self.service.search(LyricsSearchQuery('Song', provider='lrclib'))
        data = [None, record(id=True), record(id='bad'), record(trackName=None), record(), record(),
                record(id=43, duration='bad', artistName=[], albumName=None)]
        with patch.object(self.service, '_get_json', return_value=data):
            results = self.service.search(LyricsSearchQuery('Song', provider='lrclib')).results
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1].duration, 0)

    def test_other_provider_failures_preserve_lrclib_results(self):
        with patch.object(self.service, '_get_json', side_effect=[LyricsSearchError('offline'),
                LyricsSearchError('offline'), [record()]]):
            batch = self.service.search(LyricsSearchQuery('Song'))
        self.assertEqual([result.provider for result in batch.results], ['lrclib'])
        self.assertEqual(len(batch.warnings), 2)

    def test_lrclib_failure_preserves_other_provider_results(self):
        from tests.test_word_lyrics_providers import kugou_response, amll_response
        with patch.object(self.service, '_get_json', side_effect=[kugou_response(), amll_response(),
                LyricsSearchError('HTTP 429')]):
            batch = self.service.search(LyricsSearchQuery('Song'))
        self.assertEqual({result.provider for result in batch.results}, {'kugou', 'amll'})
        self.assertIn('LRCLIB', batch.warnings[0])
