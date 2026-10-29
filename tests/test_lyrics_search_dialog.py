from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.dialogs.lyrics_search_dialog import LyricsSearchDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.models.playlist import PlaylistTrack
from app.services.lyrics_search_service import FoundLyrics, LyricsSearchBatch, LyricsSearchService, LyricsSearchError
from app.utils.i18n import Language, Translator
from tests.test_lyrics_search_service import lrc_result


class LyricsSearchDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, tracks=None, **kwargs):
        translator = Translator()
        translator.set_language(Language.KOREAN)
        dialog = LyricsSearchDialog(tracks or [], translator, **kwargs)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog._shutdown_worker)
        return dialog

    def wait_idle(self, dialog):
        limit = monotonic() + 3
        while dialog.worker is not None and monotonic() < limit:
            QTest.qWait(5)
        self.assertIsNone(dialog.worker, "Lookup worker did not finish")

    def test_all_providers_and_legacy_word_timing_labels(self):
        from dataclasses import replace
        from app.services.lyrics_search_service import PROVIDERS
        dialog = self.dialog()
        self.assertNotIn("LiriQo", dialog.credit.text())
        self.assertIn("KuGou", dialog.credit.text())
        self.assertIn("AMLL", dialog.credit.text())
        self.assertIn("LRCLIB", dialog.credit.text())
        self.assertEqual(dialog.provider_combo.count(), 4)
        self.assertGreaterEqual(dialog.provider_combo.findData("lrclib"), 0)
        self.assertEqual(dialog.provider_combo.currentData(), "all")
        self.assertTrue(dialog.provider_combo.isEnabled())
        results = [replace(lrc_result(), provider="qq_portato", timing="word"),
                   replace(lrc_result(), provider="apple_music_liriqo", timing="syllable")]
        dialog._show_results(LyricsSearchBatch(results))
        self.assertEqual(dialog.table.item(0, 4).text(), "QQ Music")
        self.assertIn("단어", dialog.table.item(0, 5).text())
        self.assertIn("음절", dialog.table.item(1, 5).text())

    def test_title_required_and_empty_playlist_still_allows_manual_search(self):
        service = MagicMock(spec=LyricsSearchService)
        dialog = self.dialog(service=service)
        self.assertFalse(dialog.search_button.isEnabled())
        dialog.title_edit.setText("  ")
        dialog._search()
        service.search.assert_not_called()
        dialog.title_edit.setText("Song")
        self.assertTrue(dialog.search_button.isEnabled())
        self.assertFalse(dialog.attach_button.isEnabled())

    def test_playlist_metadata_prefills_all_fields_and_target(self):
        good = PlaylistTrack("", "Song", artist="Artist", album="Album", duration_seconds=125)
        unknown = PlaylistTrack("", "Other")
        dialog = self.dialog([unknown, good], initial_track_id=good.id)
        self.assertEqual(dialog.playlist_combo.count(), 1)
        self.assertEqual(dialog.title_edit.text(), "Song")
        self.assertEqual(dialog.artist_edit.text(), "Artist")
        self.assertEqual(dialog.album_edit.text(), "Album")
        self.assertNotIn("duration", dialog.labels)
        self.assertFalse(hasattr(dialog, "duration_spin"))
        self.assertEqual(dialog.target_combo.currentData(), good.id)

    def test_user_editing_any_metadata_switches_to_manual_input(self):
        track = PlaylistTrack("", "Song", artist="Artist", album="Album")
        dialog = self.dialog([track], initial_track_id=track.id)
        for edit in (dialog.title_edit, dialog.artist_edit, dialog.album_edit):
            with self.subTest(field=edit):
                dialog.playlist_radio.click()
                self.assertTrue(dialog.playlist_radio.isChecked())
                self.assertTrue(dialog.playlist_combo.isEnabled())
                QTest.keyClicks(edit, " edited")
                self.assertTrue(dialog.manual_radio.isChecked())
                self.assertFalse(dialog.playlist_combo.isEnabled())
                self.assertFalse(dialog.playlist_label.isEnabled())
                self.assertIn("edited", edit.text())
        dialog.playlist_radio.click()
        self.assertEqual(dialog.title_edit.text(), "Song")
        self.assertEqual(dialog.artist_edit.text(), "Artist")
        self.assertEqual(dialog.album_edit.text(), "Album")

    def test_manual_mode_preserves_fields_and_disables_playlist_after_search(self):
        service = MagicMock(spec=LyricsSearchService)
        service.search.return_value = LyricsSearchBatch([])
        track = PlaylistTrack("", "Song", artist="Artist", duration_seconds=125)
        dialog = self.dialog([track], initial_track_id=track.id, service=service)
        dialog.manual_radio.click()
        self.assertEqual(dialog.title_edit.text(), "Song")
        dialog.search_button.click()
        self.assertFalse(dialog.playlist_radio.isEnabled())
        self.assertFalse(dialog.manual_radio.isEnabled())
        self.wait_idle(dialog)
        self.assertEqual(service.search.call_args.args[0].duration, 0)
        self.assertTrue(dialog.manual_radio.isChecked())
        self.assertFalse(dialog.playlist_combo.isEnabled())
        self.assertTrue(dialog.playlist_radio.isEnabled())

    def test_no_searchable_tracks_defaults_to_manual(self):
        dialog = self.dialog([PlaylistTrack("", "Song")])
        self.assertTrue(dialog.manual_radio.isChecked())
        self.assertFalse(dialog.playlist_radio.isEnabled())
        self.assertFalse(dialog.playlist_combo.isEnabled())
        self.assertEqual(dialog.playlist_combo.count(), 0)

    def test_results_emphasize_lyrics_type_in_both_languages(self):
        dialog = self.dialog()
        dialog._show_results(LyricsSearchBatch([lrc_result()]))
        self.assertEqual(dialog.table.horizontalHeaderItem(5).text(), "가사 타입")
        self.assertTrue(dialog.table.horizontalHeaderItem(5).font().bold())
        self.assertTrue(dialog.table.item(0, 5).font().bold())
        self.assertFalse(dialog.table.showGrid())
        dialog.translator.set_language(Language.ENGLISH)
        self.assertEqual(dialog.table.horizontalHeaderItem(5).text(), "Lyrics type")
        self.assertTrue(dialog.table.item(0, 5).font().bold())

    def test_search_needs_no_duration_input_and_only_uses_matching_playlist_metadata_for_ranking(self):
        service = MagicMock(spec=LyricsSearchService)
        service.search.return_value = LyricsSearchBatch([])
        track = PlaylistTrack('', 'Song', artist='Artist', duration_seconds=125)
        dialog = self.dialog([track], service=service, initial_track_id=track.id)
        self.assertFalse(hasattr(dialog, 'duration_spin'))
        dialog.search_button.click()
        self.wait_idle(dialog)
        self.assertEqual(service.search.call_args.args[0].duration, 125)
        dialog.back_button.click()
        dialog.title_edit.setText('Another song')
        dialog.search_button.click()
        self.wait_idle(dialog)
        self.assertEqual(service.search.call_args.args[0].duration, 0)

    def test_empty_results_identify_counts_for_each_successful_provider(self):
        dialog = self.dialog()
        dialog._show_results(LyricsSearchBatch([], provider_counts={'kugou': 0, 'amll': 0}))
        self.assertIn('KuGou 0', dialog.status.text())
        self.assertIn('AMLL TTML DB 0', dialog.status.text())
        self.assertNotIn('해제', dialog.status.text())

    def test_search_does_not_fetch_until_user_selects_a_recording(self):
        service = MagicMock(spec=LyricsSearchService)
        result = lrc_result()
        service.search.return_value = LyricsSearchBatch([result])
        service.fetch.return_value = LyricsSearchService().fetch(result)
        dialog = self.dialog(service=service)
        dialog.show()
        QTest.qWait(5)
        dialog.title_edit.setText("Song")
        dialog.search_button.click()
        self.wait_idle(dialog)
        self.assertEqual(dialog.table.rowCount(), 1)
        service.fetch.assert_not_called()
        self.assertFalse(dialog.download_button.isEnabled())
        dialog.table.selectRow(0)
        self.wait_idle(dialog)
        service.fetch.assert_called_once()
        self.assertEqual(service.fetch.call_args.args[0], result)
        self.assertIn("First", dialog.preview.toPlainText())
        self.assertTrue(dialog.download_button.isEnabled())

    def test_selected_provider_is_sent_and_verified_word_timing_is_shown(self):
        from app.services.lyrics_search_service import LyricsSearchResult
        from tests.test_lyrics_search_service import TTML
        service = MagicMock(spec=LyricsSearchService)
        result = LyricsSearchResult('kugou', '42', 'Song', 'Artist', payload={'ttml': TTML})
        service.search.return_value = LyricsSearchBatch([result])
        service.fetch.return_value = LyricsSearchService().fetch(result)
        dialog = self.dialog(service=service)
        dialog.title_edit.setText('Song')
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData('kugou'))
        dialog.search_button.click()
        self.wait_idle(dialog)
        self.assertEqual(service.search.call_args.args[0].provider, 'kugou')
        self.assertTrue(dialog.provider_combo.isEnabled())
        self.assertIn('선택 후 확인', dialog.table.item(0, 5).text())
        dialog.table.selectRow(0)
        self.wait_idle(dialog)
        self.assertIn('단어', dialog.table.item(0, 5).text())
        self.assertIn('KRC → TTML', dialog.preview_info.text())
        dialog.next_button.click()
        self.assertEqual(dialog.pages.currentIndex(), 2)

    def test_fetch_failure_keeps_result_selection_retryable(self):
        service = MagicMock(spec=LyricsSearchService)
        service.fetch.side_effect = LyricsSearchError('download failed')
        dialog = self.dialog(service=service)
        dialog._show_results(LyricsSearchBatch([lrc_result()]))
        dialog.table.selectRow(0)
        self.wait_idle(dialog)
        self.assertEqual(dialog.pages.currentIndex(), 1)
        self.assertFalse(dialog.next_button.isEnabled())
        self.assertTrue(dialog.table.isEnabled())
        self.assertIn('download failed', dialog.status.text())
        service.fetch.side_effect = None
        service.fetch.return_value = LyricsSearchService().fetch(lrc_result())
        dialog._select_result()
        self.wait_idle(dialog)
        self.assertTrue(dialog.next_button.isEnabled())

    def test_attach_requires_explicit_click_and_uses_chosen_target(self):
        first, second = PlaylistTrack("", "One"), PlaylistTrack("", "Two")
        attach = MagicMock(return_value=True)
        dialog = self.dialog([first, second], on_attach=attach)
        document = LyricsSearchService().fetch(lrc_result())
        dialog._show_document(document)
        self.assertFalse(dialog.attach_button.isEnabled())  # A manual search must choose its destination.
        dialog.target_combo.setCurrentIndex(dialog.target_combo.findData(second.id))
        attach.assert_not_called()
        with patch("app.dialogs.lyrics_search_dialog.QMessageBox.information"):
            dialog.attach_button.click()
        attach.assert_called_once_with(document, second.id)
        self.assertEqual(dialog.attached_track_ids, [second.id])

    def test_local_download_does_not_attach_or_change_playlist(self):
        service = LyricsSearchService()
        attach = MagicMock(return_value=True)
        dialog = self.dialog([PlaylistTrack("", "Song")], on_attach=attach, service=service)
        document = service.fetch(lrc_result())
        dialog._show_document(document)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "song.lrc"
            with patch("app.dialogs.lyrics_search_dialog.QFileDialog.getSaveFileName", return_value=(str(path), "")):
                dialog.download_button.click()
            self.assertEqual(path.read_text(encoding="utf-8"), document.content)
            self.assertEqual(dialog.saved_paths, [path])
        attach.assert_not_called()

    def test_untimed_lyrics_download_only_even_with_a_target(self):
        dialog = self.dialog([PlaylistTrack("", "Song")], on_attach=MagicMock())
        dialog._show_document(FoundLyrics(lrc_result(), "txt", "Plain text"))
        self.assertTrue(dialog.download_button.isEnabled())
        self.assertFalse(dialog.attach_button.isEnabled())

    def test_error_and_no_results_keep_search_available(self):
        service = MagicMock(spec=LyricsSearchService)
        service.search.side_effect = LyricsSearchError("offline")
        dialog = self.dialog(service=service)
        dialog.title_edit.setText("Song")
        dialog._search()
        self.wait_idle(dialog)
        self.assertIn("offline", dialog.status.text())
        self.assertTrue(dialog.search_button.isEnabled())
        service.search.side_effect = None
        service.search.return_value = LyricsSearchBatch([])
        dialog._search()
        self.wait_idle(dialog)
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.attach_button.isEnabled())

    def test_cancel_and_close_discard_in_flight_results(self):
        for close in (False, True):
            with self.subTest(close=close):
                service = MagicMock(spec=LyricsSearchService)
                entered = Event()

                def blocked(query, cancel):
                    entered.set()
                    cancel.wait(2)
                    return LyricsSearchBatch([lrc_result()])

                service.search.side_effect = blocked
                dialog = self.dialog(service=service)
                dialog.title_edit.setText("Song")
                dialog._search()
                limit = monotonic() + 1
                while not entered.is_set() and monotonic() < limit:
                    QTest.qWait(5)
                self.assertTrue(entered.is_set())
                if close:
                    dialog.reject()
                else:
                    dialog.cancel_button.click()
                self.wait_idle(dialog)
                self.assertEqual(dialog.table.rowCount(), 0)
                self.assertIsNone(dialog.document)

    def test_track_settings_stage_attachment_without_changing_track_on_cancel(self):
        track = PlaylistTrack("", "Song", artist="Artist")
        translator = Translator()
        parent = TrackDetailsDialog(track, translator)
        self.addCleanup(parent.deleteLater)
        self.addCleanup(parent.close)
        document = LyricsSearchService().fetch(lrc_result())
        parent.title_edit.setText("Edited title")
        with patch("app.dialogs.lyrics_search_dialog.LyricsSearchDialog") as dialog_type:
            def choose():
                callback = dialog_type.call_args.kwargs["on_attach"]
                self.assertEqual(dialog_type.call_args.args[0][0].title, "Edited title")
                self.assertTrue(callback(document, track.id))
            dialog_type.return_value.exec.side_effect = choose
            parent._find_lyrics()
        self.assertEqual(parent.selected_lyrics, document.cues)
        self.assertEqual(parent.selected_lyrics_path, "")
        self.assertEqual(track.lyrics, [])
        parent.reject()
        self.assertEqual(track.lyrics, [])

    def test_both_languages_and_no_unsupported_country_option(self):
        dialog = self.dialog()
        self.assertNotIn("country", dialog.labels)
        self.assertEqual(dialog.windowTitle(), "가사 검색 및 가져오기")
        dialog.translator.set_language(Language.ENGLISH)
        self.assertEqual(dialog.windowTitle(), "Search & Import Lyrics")
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("amll"))
        self.assertEqual(dialog.provider_combo.currentData(), "amll")

    def test_discovery_ui_filter_preview_and_copy(self):
        from dataclasses import replace
        dialog = self.dialog()
        first = lrc_result()
        second = replace(first, provider="qq_portato", timing="word")
        document = LyricsSearchService().fetch(first)
        dialog._documents[first.key] = document
        self.assertEqual(dialog.result_count.text(), "검색 전")
        dialog._show_results(LyricsSearchBatch([first, second]))
        self.assertEqual(dialog.result_count.text(), "2 / 2개")
        dialog.table.selectRow(0)
        self.assertIs(dialog.document, document)
        self.assertEqual(dialog.preview_stack.currentIndex(), 1)

        # A localized label and multi-word provider search must both work.
        dialog.result_filter.setText("단어")
        self.assertEqual(dialog.result_count.text(), "1 / 2개")
        self.assertTrue(dialog.table.isRowHidden(0))
        self.assertFalse(dialog.table.isRowHidden(1))
        self.assertIsNone(dialog.document)
        self.assertEqual(dialog.table.currentRow(), -1)
        self.assertEqual(dialog.preview_stack.currentIndex(), 0)
        dialog.result_filter.setText("QQ Music")
        self.assertEqual(dialog.result_count.text(), "1 / 2개")
        dialog.result_filter.setText("Song QQ")
        self.assertEqual(dialog.result_count.text(), "1 / 2개")

        # The previously hidden row can be selected again after clearing.
        dialog.result_filter.clear()
        self.assertEqual(dialog.result_count.text(), "2 / 2개")
        dialog.table.selectRow(0)
        self.assertIs(dialog.document, document)
        self.assertTrue(dialog.copy_button.isEnabled())
        dialog.copy_button.click()
        self.assertEqual(QApplication.clipboard().text(), dialog.preview.toPlainText())

        # Existing timing labels should reflect runtime language changes.
        dialog.translator.set_language(Language.ENGLISH)
        self.assertEqual(dialog.table.item(1, 5).text(), "Word timing")
        self.assertIn("Line timing", dialog.preview_info.text())

    def test_empty_search_count_is_not_mistaken_for_not_searched(self):
        dialog = self.dialog()
        dialog._show_results(LyricsSearchBatch([]))
        self.assertEqual(dialog.result_count.text(), "0 / 0개")
        dialog.translator.set_language(Language.ENGLISH)
        self.assertEqual(dialog.result_count.text(), "0 / 0")

    def test_wizard_requires_selection_and_review_before_applying(self):
        track = PlaylistTrack("", "Song", artist="Artist")
        attach = MagicMock(return_value=True)
        dialog = self.dialog([track], initial_track_id=track.id, on_attach=attach)
        dialog.show()
        self.assertEqual(dialog.pages.currentIndex(), 0)
        self.assertTrue(dialog.target_panel.isHidden())
        self.assertTrue(dialog.back_button.isHidden())
        dialog._show_results(LyricsSearchBatch([lrc_result()]))
        self.assertEqual(dialog.pages.currentIndex(), 1)
        self.assertFalse(dialog.next_button.isEnabled())
        dialog._set_step(2)
        self.assertEqual(dialog.pages.currentIndex(), 1)
        dialog._documents[lrc_result().key] = LyricsSearchService().fetch(lrc_result())
        dialog.table.selectRow(0)
        self.assertTrue(dialog.next_button.isEnabled())
        self.assertEqual(dialog.pages.currentIndex(), 1)
        attach.assert_not_called()
        dialog.next_button.click()
        self.assertEqual(dialog.pages.currentIndex(), 2)
        self.assertFalse(dialog.target_panel.isHidden())
        self.assertIn("First", dialog.preview.toPlainText())
        dialog.back_button.click()
        self.assertEqual(dialog.pages.currentIndex(), 1)
        self.assertIsNotNone(dialog.document)
        dialog.next_button.click()
        with patch("app.dialogs.lyrics_search_dialog.QMessageBox.information"):
            dialog.attach_button.click()
        attach.assert_called_once()

    def test_wizard_search_advances_and_back_keeps_query_and_results(self):
        service = MagicMock(spec=LyricsSearchService)
        service.search.return_value = LyricsSearchBatch([lrc_result()])
        dialog = self.dialog(service=service)
        dialog.title_edit.setText("Song")
        dialog.search_button.click()
        self.wait_idle(dialog)
        self.assertEqual(dialog.pages.currentIndex(), 1)
        dialog.back_button.click()
        self.assertEqual(dialog.pages.currentIndex(), 0)
        self.assertEqual(dialog.title_edit.text(), "Song")
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertFalse(dialog.search_button.isHidden())

    def test_search_failure_stays_on_details_and_retry_remains_available(self):
        service = MagicMock(spec=LyricsSearchService)
        service.search.side_effect = LyricsSearchError("HTTP 429")
        dialog = self.dialog(service=service)
        dialog.title_edit.setText("Song")
        dialog.search_button.click()
        self.wait_idle(dialog)
        self.assertEqual(dialog.pages.currentIndex(), 0)
        self.assertTrue(dialog.search_button.isEnabled())
        self.assertTrue(dialog.target_panel.isHidden())

    def test_untimed_result_can_be_reviewed_but_not_attached(self):
        dialog = self.dialog([PlaylistTrack("", "Song")], on_attach=MagicMock())
        dialog._show_results(LyricsSearchBatch([lrc_result()]))
        dialog._show_document(FoundLyrics(lrc_result(), "txt", "Text"))
        dialog.next_button.click()
        self.assertEqual(dialog.pages.currentIndex(), 2)
        self.assertTrue(dialog.download_button.isEnabled())
        self.assertFalse(dialog.attach_button.isEnabled())

    def test_ttml_in_track_settings_only_stages_lyrics(self):
        track = PlaylistTrack("", "Song", lyrics_timing_offset_seconds=0.25)
        parent = TrackDetailsDialog(track, Translator())
        self.addCleanup(parent.deleteLater)
        self.addCleanup(parent.close)
        from app.services.lyrics_service import LyricsService
        content = '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="1s" end="4s">Hello</p></body></tt>'
        document = FoundLyrics(lrc_result(), "ttml", content, LyricsService.parse(content, ".ttml"))
        with patch("app.dialogs.lyrics_search_dialog.LyricsSearchDialog") as dialog_type:
            dialog_type.return_value.exec.side_effect = lambda: dialog_type.call_args.kwargs["on_attach"](document, track.id)
            parent._find_lyrics()
        self.assertEqual(parent.selected_lyrics, document.cues)
        self.assertFalse(getattr(parent, "selected_karaoke_settings", None))
        self.assertEqual(parent.timing_offset_spin.value(), 0.25)
        self.assertEqual(track.lyrics, [])
        parent.reject()


if __name__ == "__main__":
    unittest.main()
