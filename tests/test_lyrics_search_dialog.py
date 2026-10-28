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

    def test_liriqo_provider_variants_and_word_timing_labels(self):
        from dataclasses import replace
        from app.services.lyrics_search_service import PROVIDERS
        dialog = self.dialog()
        self.assertIn("LiriQo", dialog.credit.text())
        self.assertEqual(dialog.provider_combo.count(), len(PROVIDERS) + 1)
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
        self.assertEqual(dialog.playlist_combo.count(), 2)
        self.assertEqual(dialog.title_edit.text(), "Song")
        self.assertEqual(dialog.artist_edit.text(), "Artist")
        self.assertEqual(dialog.album_edit.text(), "Album")
        self.assertEqual(dialog.duration_spin.value(), 125)
        self.assertEqual(dialog.target_combo.currentData(), good.id)

    def test_search_does_not_fetch_until_user_selects_a_recording(self):
        service = MagicMock(spec=LyricsSearchService)
        result = lrc_result()
        service.search.return_value = LyricsSearchBatch([result])
        service.fetch.return_value = LyricsSearchService().fetch(result)
        dialog = self.dialog(service=service)
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

    def test_attach_requires_explicit_click_and_uses_chosen_target(self):
        first, second = PlaylistTrack("", "One"), PlaylistTrack("", "Two")
        attach = MagicMock(return_value=True)
        dialog = self.dialog([first, second], on_attach=attach)
        document = LyricsSearchService().fetch(lrc_result())
        dialog._show_document(document)
        self.assertFalse(dialog.attach_button.isEnabled())  # A manual search must choose its destination.
        dialog.target_combo.setCurrentIndex(dialog.target_combo.findData(second.id))
        attach.assert_not_called()
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
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("lrclib"))
        self.assertEqual(dialog.provider_combo.currentData(), "lrclib")

    def test_discovery_ui_filter_preview_and_copy(self):
        dialog = self.dialog()
        first = lrc_result()
        from dataclasses import replace
        second = replace(first, provider="apple_music_liriqo")
        dialog._show_results(LyricsSearchBatch([first, second]))
        self.assertEqual(dialog.result_count.text(), "2 / 2개")
        dialog.result_filter.setText("QQ Music")
        self.assertEqual(dialog.result_count.text(), "0 / 2개")
        dialog.result_filter.clear()
        self.assertEqual(dialog.result_count.text(), "2 / 2개")
        dialog._show_document(LyricsSearchService().fetch(first))
        self.assertEqual(dialog.preview_stack.currentIndex(), 1)
        self.assertTrue(dialog.copy_button.isEnabled())
        dialog.copy_button.click()
        self.assertEqual(QApplication.clipboard().text(), dialog.preview.toPlainText())

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
