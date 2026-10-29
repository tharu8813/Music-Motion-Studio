import base64
import copy
from dataclasses import replace
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
import zlib

from app.models.playlist import PlaylistTrack
from app.services.krc_lyrics import KRCError, krc_to_ttml
from app.services.lyrics_search_service import (
    AMLL_GET_API, AMLL_SEARCH_API, KUGOU_DOWNLOAD_API, KUGOU_SEARCH_API, KUGOU_CATALOG_API, LRCLIB_SEARCH_API,
    LyricsRequestState, LyricsSearchCancelled, LyricsSearchError, LyricsSearchQuery,
    LyricsSearchResult, LyricsSearchService,
)
from app.services.lyrics_service import LyricsService


TEXT = '[ti:Fixture]\n[1000,5000]<0,1000,0>Hello <2000,1500,0>world & 한글\n'


def encoded_krc(text=TEXT):
    mask = bytes((64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105))
    compressed = zlib.compress(text.encode('utf-8'))
    return base64.b64encode(b'krc1' + bytes(c ^ mask[i % 16] for i, c in enumerate(compressed))).decode('ascii')


def kugou_response(**changes):
    row = {'id': 42, 'accesskey': 'fixture-key', 'song': 'Song', 'singer': 'Artist', 'duration': 123000, **changes}
    return {'status': 200, 'candidates': [row]}


def amll_response(**changes):
    row = {'id': 73, 'filename': 'fixture.ttml', 'musicNames': ['Song'], 'artistNames': ['Artist'],
           'albumNames': ['Album'], **changes}
    return {'status': 200, 'data': {'items': [row], 'pagination': {'hasMore': False}}}


def empty_amll():
    return {'status': 200, 'data': {'items': []}}


class KRCConversionTests(unittest.TestCase):
    def test_unicode_text_offsets_exact_source_timings_and_ttml_roundtrip(self):
        content = krc_to_ttml(encoded_krc(), max_bytes=10000)
        cues = LyricsService.parse(content, '.ttml')
        self.assertEqual(cues[0]['text'], 'Hello world & 한글')
        self.assertEqual((cues[0]['start'], cues[0]['end']), (1, 6))
        self.assertEqual(cues[0]['words'], [
            {'start': 1.0, 'end': 2.0, 'text': 'Hello ', 'offset': 0},
            {'start': 3.0, 'end': 4.5, 'text': 'world & 한글', 'offset': 6},
        ])
        track = PlaylistTrack('', 'Song', lyrics=cues)
        self.assertEqual(PlaylistTrack.from_dict(track.to_dict()).lyrics, cues)

    def test_no_word_tags_remains_line_timed_without_estimation(self):
        cues = LyricsService.parse(krc_to_ttml(encoded_krc('[1000,2000]Plain line'), max_bytes=10000), '.ttml')
        self.assertEqual(cues[0]['text'], 'Plain line')
        self.assertNotIn('words', cues[0])

    def test_invalid_or_oversized_encoded_and_compressed_data(self):
        for content in ('not-base64!', base64.b64encode(b'wrong').decode(), encoded_krc(TEXT * 1000)):
            with self.subTest(content=content[:25]), self.assertRaises(KRCError):
                krc_to_ttml(content, max_bytes=100)
        raw = base64.b64decode(encoded_krc())
        with self.assertRaises(KRCError):
            krc_to_ttml(base64.b64encode(raw[:-2]).decode(), max_bytes=10000)

    def test_invalid_intervals_and_missing_lines_are_rejected(self):
        for text in ('[ti:Only metadata]', '[1,0]Text', '[-1,50]Text', '[1,200]<0,-1,0>Text',
                     '[1,200]<0,300,0>Text', '[1,200]<100,100,0>A<0,50,0>B'):
            with self.subTest(text=text), self.assertRaises(KRCError):
                krc_to_ttml(encoded_krc(text), max_bytes=10000)

    def test_conversion_checks_cancellation(self):
        def cancel():
            raise LyricsSearchCancelled()
        with self.assertRaises(LyricsSearchCancelled):
            krc_to_ttml(encoded_krc(), max_bytes=10000, check_cancel=cancel)


class WordProviderTests(unittest.TestCase):
    def setUp(self):
        self.service = LyricsSearchService(LyricsRequestState(intervals={}, cooldown_delay=0))

    def test_all_providers_search_without_downloading_and_rank_recordings(self):
        kg = kugou_response()
        kg['candidates'].append({**kg['candidates'][0], 'id': 43, 'duration': 300000})
        with patch.object(self.service, '_get_json', side_effect=[kg, amll_response(), []]) as request:
            batch = self.service.search(LyricsSearchQuery('Song', 'Artist', duration=123))
        self.assertEqual([call.args[0] for call in request.call_args_list], [KUGOU_SEARCH_API, AMLL_SEARCH_API, LRCLIB_SEARCH_API])
        self.assertNotIn('duration', request.call_args_list[0].args[1])
        self.assertEqual([r.identifier for r in batch.results], ['42', '73', '43'])
        self.assertTrue(all(r.timing == 'unknown' for r in batch.results))

    def test_explicit_provider_searches_only_selected_host(self):
        for provider, url, data in [('kugou', KUGOU_SEARCH_API, kugou_response()), ('amll', AMLL_SEARCH_API, amll_response())]:
            with self.subTest(provider=provider), patch.object(self.service, '_get_json', return_value=data) as request:
                self.assertEqual(len(self.service.search(LyricsSearchQuery('Song', provider=provider)).results), 1)
            self.assertEqual(request.call_args.args[0], url)
            request.assert_called_once()

    def test_localized_artist_and_title_only_resolve_catalog_before_lyric_lookup(self):
        catalog = {'status': 1, 'data': {'info': [
            {'songname': 'Song', 'singername': 'Artist', 'duration': 123, 'hash': 'a' * 32},
            {'songname': 'Song', 'singername': 'Artist', 'duration': 123, 'hash': 'a' * 32}, None]}}
        for artist in ('가수', ''):
            with self.subTest(artist=artist), patch.object(self.service, '_get_json', side_effect=[
                    {'status': 200, 'candidates': []}, catalog, kugou_response()]) as request:
                batch = self.service.search(LyricsSearchQuery('Song', artist, provider='kugou'))
            self.assertEqual([call.args[0] for call in request.call_args_list],
                             [KUGOU_SEARCH_API, KUGOU_CATALOG_API, KUGOU_SEARCH_API])
            self.assertEqual(request.call_args.args[1]['keyword'], 'Artist - Song')
            self.assertEqual(request.call_args.args[1]['hash'], 'a' * 32)
            self.assertEqual(request.call_args.args[1]['duration'], 123000)
            self.assertEqual(batch.results[0].artist, 'Artist')
            self.assertTrue(batch.warnings)
            self.assertEqual(batch.provider_counts, {'kugou': 1})

    def test_empty_catalog_reports_source_counts_and_keeps_amll_results(self):
        with patch.object(self.service, '_get_json', side_effect=[{'status': 200, 'candidates': []},
                {'status': 1, 'data': {'info': []}}, amll_response(), []]):
            batch = self.service.search(LyricsSearchQuery('Song'))
        self.assertEqual([r.provider for r in batch.results], ['amll'])
        self.assertEqual(batch.provider_counts, {'kugou': 0, 'amll': 1, 'lrclib': 0})
        self.assertTrue(batch.warnings)

    def test_catalog_expansion_is_bounded_and_cancellable(self):
        catalog = {'status': 1, 'data': {'info': [
            {'songname': f'Song {i}', 'singername': f'Artist {i}', 'duration': 120} for i in range(20)]}}
        with patch.object(self.service, '_get_json', side_effect=[{'status': 200, 'candidates': []},
                catalog, *[{'status': 200, 'candidates': []}] * 3]) as request:
            batch = self.service.search(LyricsSearchQuery('Song', provider='kugou'))
        self.assertEqual(len(request.call_args_list), 5)
        self.assertEqual(batch.results, [])
        cancel = Event()
        def response(url, params, token):
            if url == KUGOU_CATALOG_API:
                cancel.set()
                return catalog
            return {'status': 200, 'candidates': []}
        with patch.object(self.service, '_get_json', side_effect=response) as request:
            with self.assertRaises(LyricsSearchCancelled):
                self.service.search(LyricsSearchQuery('Song', provider='kugou'), cancel)
        self.assertEqual(request.call_count, 2)

    def test_catalog_errors_are_not_reported_as_successful_empty_search(self):
        for response in ([], {}, {'status': 1, 'data': {'info': None}}):
            with self.subTest(response=response), patch.object(self.service, '_get_json', side_effect=[
                    {'status': 200, 'candidates': []}, response]):
                with self.assertRaisesRegex(LyricsSearchError, 'catalog response'):
                    self.service.search(LyricsSearchQuery('Song', provider='kugou'))

    def test_partial_failure_keeps_other_provider_results(self):
        for responses, expected in [([LyricsSearchError('HTTP 429'), amll_response()], 'amll'),
                                    ([kugou_response(), LyricsSearchError('offline')], 'kugou')]:
            with patch.object(self.service, '_get_json', side_effect=[*responses, []]):
                batch = self.service.search(LyricsSearchQuery('Song'))
            self.assertEqual([r.provider for r in batch.results], [expected])
            self.assertEqual(len(batch.warnings), 1)

    def test_both_failures_identify_sources_and_empty_success_remains_usable(self):
        with patch.object(self.service, '_get_json', side_effect=LyricsSearchError('offline')):
            with self.assertRaisesRegex(LyricsSearchError, 'KuGou.*offline\nAMLL.*offline'):
                self.service.search(LyricsSearchQuery('Song'))
        with patch.object(self.service, '_get_json', side_effect=[LyricsSearchError('offline'), empty_amll(), []]):
            batch = self.service.search(LyricsSearchQuery('Song'))
        self.assertEqual(batch.results, [])
        self.assertTrue(batch.warnings)

    def test_amll_broadens_artist_without_searching_lyric_body(self):
        with patch.object(self.service, '_get_json', side_effect=[empty_amll(), amll_response()]) as request:
            batch = self.service.search(LyricsSearchQuery('Song', '가수', provider='amll'))
        self.assertIn('artistName', request.call_args_list[0].args[1])
        self.assertEqual(request.call_args.args[1], {'musicName': 'Song', 'pageSize': 50})
        self.assertTrue(batch.warnings)

    def test_bad_rows_are_skipped_and_duplicates_removed(self):
        kg = kugou_response()
        kg['candidates'] += [None, dict(kg['candidates'][0]), {'id': True}, {'id': 'bad'}, {'id': 0, 'song': []}]
        amll = amll_response()
        amll['data']['items'] += [None, dict(amll['data']['items'][0]), {'id': 5, 'musicNames': []}]
        with patch.object(self.service, '_get_json', side_effect=[kg, amll, []]):
            batch = self.service.search(LyricsSearchQuery('Song'))
        self.assertEqual(len(batch.results), 2)

    def test_malformed_search_payloads_do_not_crash_worker(self):
        for provider, values in [('kugou', [[], {}, {'status': 200, 'candidates': None}]),
                                 ('amll', [[], {}, {'status': 200, 'data': {'items': None}},
                                           {'status': 200, 'data': {'items': [], 'pagination': []}}])]:
            for value in values:
                with self.subTest(provider=provider, value=value), patch.object(self.service, '_get_json', return_value=value):
                    with self.assertRaises(LyricsSearchError):
                        self.service.search(LyricsSearchQuery('Song', provider=provider))

    def test_krc_selected_id_download_and_file_save_preserve_words(self):
        with patch.object(self.service, '_get_json', return_value=kugou_response()):
            result = self.service.search(LyricsSearchQuery('Song', provider='kugou')).results[0]
        with patch.object(self.service, '_get_json', return_value={'content': encoded_krc()}) as request:
            document = self.service.fetch(result)
        self.assertEqual(request.call_args.args[0], KUGOU_DOWNLOAD_API)
        self.assertEqual(request.call_args.args[1]['id'], '42')
        self.assertEqual(request.call_args.args[1]['fmt'], 'krc')
        self.assertEqual(document.result.timing, 'word')
        with TemporaryDirectory() as directory:
            path = self.service.save(document, Path(directory) / 'song.ttml')
            self.assertEqual(LyricsService.load(path), document.cues)

    def test_amll_selected_identity_and_original_ttml_are_preserved(self):
        with patch.object(self.service, '_get_json', return_value=amll_response()):
            result = self.service.search(LyricsSearchQuery('Song', provider='amll')).results[0]
        ttml = krc_to_ttml(encoded_krc(), max_bytes=10000)
        response = {'status': 200, 'data': {'id': 73, 'filename': 'fixture.ttml', 'lyrics': ttml}}
        with patch.object(self.service, '_get_json', return_value=response) as request:
            document = self.service.fetch(result)
        request.assert_called_once_with(AMLL_GET_API, {'id': '73'}, None)
        self.assertEqual(document.content, ttml)
        self.assertEqual(document.result.timing, 'word')
        response['data']['filename'] = 'different.ttml'
        with patch.object(self.service, '_get_json', return_value=response):
            with self.assertRaisesRegex(LyricsSearchError, 'identity mismatch'):
                self.service.fetch(result)

    def test_repeated_search_and_fetch_use_bounded_response_cache(self):
        kg = json.dumps(kugou_response()).encode()
        download = json.dumps({'content': encoded_krc()}).encode()
        with patch('app.services.lyrics_search_service.urlopen', side_effect=[io.BytesIO(kg), io.BytesIO(download)]) as network:
            for _ in range(2):
                batch = self.service.search(LyricsSearchQuery('Song', provider='kugou'))
                self.service.fetch(batch.results[0])
        self.assertEqual(network.call_count, 2)

    def test_cancellation_between_providers_discards_partial_results(self):
        cancel = Event()
        def response(*args):
            cancel.set()
            return kugou_response()
        with patch.object(self.service, '_get_json', side_effect=response) as request:
            with self.assertRaises(LyricsSearchCancelled):
                self.service.search(LyricsSearchQuery('Song'), cancel)
        request.assert_called_once()

    def test_ttml_millisecond_rounding_overlap_survives_project_validation(self):
        ttml = '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="1s" end="4s">' \
               '<span begin="0s" end="1s">one </span><span begin="0.999s" end="2s">two</span></p></body></tt>'
        result = LyricsSearchResult('amll', '73', 'Song', 'Artist', payload={'ttml': ttml})
        document = self.service.fetch(result)
        self.assertEqual(document.cues[0]['words'][1]['start'], 1.999)
        PlaylistTrack.from_dict(PlaylistTrack('', 'Song', lyrics=document.cues).to_dict())
        for content in (ttml.replace('0.999s', '0.998s'), ttml.replace('end="2s"', 'end="0.5s"')):
            with self.subTest(content=content), self.assertRaises(LyricsSearchError):
                self.service.fetch(replace(result, payload={'ttml': content}))

    def test_rounding_tolerance_does_not_admit_outside_cue_or_invalid_offsets(self):
        cue = {'start': 1, 'end': 3, 'text': 'ab', 'format': 'ttml', 'words': [
            {'start': 1, 'end': 2, 'offset': 0, 'text': 'a'},
            {'start': 1.999, 'end': 3, 'offset': 1, 'text': 'b'}]}
        for changes in ({'start': .999}, {'end': 3.001}, {'offset': 0}, {'start': 1.998}, {'start': True}):
            value = copy.deepcopy(cue)
            value['words'][1].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PlaylistTrack.from_dict(PlaylistTrack('', 'Song', lyrics=[value]).to_dict())
