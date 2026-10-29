from copy import deepcopy
from unittest.mock import Mock, patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog
from app.dialogs.lyrics_search_dialog import LyricsSearchDialog
from app.dialogs.lyrics_compare_dialog import LyricsCompareDialog
from app.models.playlist import PlaylistTrack
from app.models.source import SourceType
from app.services.lyrics_search_service import FoundLyrics
from tests.main_window_base import MainWindowTestCase
from tests.test_lyrics_search_service import lrc_result


class LyricsSelectionWarningTests(MainWindowTestCase):
    def test_checked_rows_only_apply_and_keep_word_metadata_then_close(self):
        track = PlaylistTrack("", "Target")
        callback = Mock(return_value=True)
        dialog = LyricsSearchDialog([track], self.window.translator, on_attach=callback)
        self.addCleanup(dialog.deleteLater)
        cues = [{"start": i, "end": i+1, "text": str(i), "format": "ttml",
                 "words": [{"start": i, "end": i+0.5, "text": str(i), "offset": 0}]} for i in range(3)]
        document = FoundLyrics(lrc_result(), "ttml", "original", deepcopy(cues))
        dialog._show_document(document)
        dialog.target_combo.setCurrentIndex(1)
        self.assertEqual(dialog.preview.selected_cues(), cues)
        dialog.preview.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
        with patch("app.dialogs.lyrics_search_dialog.QMessageBox.information") as notice:
            dialog._attach()
        selected = callback.call_args.args[0]
        self.assertEqual(selected.cues, [cues[0], cues[2]])
        self.assertEqual(document.cues, cues)
        notice.assert_called_once()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_empty_selection_cannot_apply_and_rejected_replacement_stays_open(self):
        from app.services.lyrics_search_service import LyricsSearchService
        callback = Mock(return_value=False)
        dialog = LyricsSearchDialog([PlaylistTrack("", "Target")], self.window.translator, on_attach=callback)
        self.addCleanup(dialog.deleteLater)
        dialog._show_document(LyricsSearchService().fetch(lrc_result()))
        dialog.target_combo.setCurrentIndex(1)
        dialog.preview.select_all_rows(False)
        self.assertFalse(dialog.attach_button.isEnabled())
        dialog._attach()
        callback.assert_not_called()
        dialog.preview.select_all_rows(True)
        with patch("app.dialogs.lyrics_search_dialog.QMessageBox.information") as notice:
            dialog._attach()
        callback.assert_called_once()
        notice.assert_not_called()
        self.assertEqual(dialog.attached_track_ids, [])

    def test_ttml_warning_pin_and_resolution_follow_actual_source_settings(self):
        self.window.playlist_service.add_tracks([PlaylistTrack("", "TTML", lyrics=[
            {"start": 0, "end": 1, "text": "word", "format": "ttml"}])])
        self.window._refresh_status_warnings()
        widget = self.window.warning_indicator
        self.assertIn("ttml", widget.active_keys)
        widget.set_pinned(True)
        self.assertTrue(widget.pinned)
        self.assertTrue(widget.popup.isVisible())
        self.assertTrue(widget.popup.rows["ttml"].detail.wordWrap())
        self.window._refresh_status_warnings()
        self.assertIn("TTML", widget.popup.rows["ttml"].detail.text())
        self.window._add_source(SourceType.LYRICS)
        source = self.window.store.selected
        self.window.store.update(source.id, subtitle_karaoke_enabled=True)
        self.window._refresh_status_warnings()
        self.assertNotIn("ttml", widget.active_keys)
        self.assertFalse(widget.pinned)
        self.assertTrue(widget.isHidden())

    def test_missing_audio_and_excluded_tracks_are_actionable_and_clear(self):
        self.window.playlist_service.add_tracks([PlaylistTrack("absent-audit-file.wav", "Missing", enabled=False)])
        self.window._refresh_status_warnings()
        self.assertEqual(set(self.window.warning_indicator.active_keys), {"missing_audio", "excluded"})
        self.window.playlist_service.replace([])
        self.window._refresh_status_warnings()
        self.assertTrue(self.window.warning_indicator.isHidden())

    def test_comparison_is_tabular_and_highlights_changed_rows(self):
        before = [{"start": 1, "end": 2, "text": "Before"}]
        after = [{"start": 1, "end": 2, "text": "After"}]
        dialog = LyricsCompareDialog(before, after, "", "", self.window.translator)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual([t.rowCount() for t in dialog.previews], [1, 1])
        self.assertEqual(dialog.previews[1].item(0, 2).text(), "After")
        self.assertEqual(dialog.previews[1].item(0, 2).background().color().name(), "#40392b")
