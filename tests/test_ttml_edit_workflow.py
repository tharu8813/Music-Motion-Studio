from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QAbstractItemView, QDialog, QMessageBox

from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.dialogs.ttml_edit_dialog import TtmlRowsDialog, choose_ttml_edit_mode, line_cues, needs_ttml_protection
from app.models.playlist import PlaylistTrack
from app.services.lyrics_service import LyricsError, LyricsService
from app.utils.i18n import Language, Translator
from tests.main_window_base import MainWindowTestCase


def cues():
    return [{'start': 1.0, 'end': 4.0, 'text': 'one two', 'format': 'ttml',
             'words': [{'start': 1.0, 'end': 2.0, 'text': 'one', 'offset': 0},
                       {'start': 3.0, 'end': 4.0, 'text': 'two', 'offset': 4}], 'source_note': 'keep'},
            {'start': 2.0, 'end': 6.5, 'text': 'background', 'format': 'ttml',
             'words': [{'start': 2.2, 'end': 6.4, 'text': 'background', 'offset': 0}]}]


class TtmlEditWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def translator(self):
        translator = Translator()
        translator.set_language(Language.KOREAN)
        return translator

    def test_detection_covers_embedded_downloads_word_data_and_uppercase_file_suffix(self):
        self.assertTrue(needs_ttml_protection(cues()))
        self.assertTrue(needs_ttml_protection([], 'file.TTML'))
        self.assertTrue(needs_ttml_protection([{'words': [{'start': 0}]}]))
        self.assertFalse(needs_ttml_protection(line_cues(cues()), 'file.lrc'))

    def test_warning_choices_are_explicit_and_cancel_is_safe(self):
        for selection, mode in ((0, 'convert'), (1, 'delete'), (2, 'cancel')):
            with patch('app.dialogs.ttml_edit_dialog.QMessageBox') as box_type:
                buttons = [object(), object(), object()]
                box_type.return_value.addButton.side_effect = buttons
                box_type.return_value.clickedButton.return_value = buttons[selection]
                self.assertEqual(choose_ttml_edit_mode(cues(), '', self.translator(), None), mode)
                box_type.return_value.setDefaultButton.assert_called_once_with(buttons[1])

    def test_delete_only_preserves_every_field_and_allows_undo_and_empty_result(self):
        original = cues()
        dialog = TtmlRowsDialog(original, self.translator())
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.table.editTriggers(), QAbstractItemView.EditTrigger.NoEditTriggers)
        dialog.table.selectRow(0)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            dialog._delete_selected()
        self.assertEqual(dialog.timed_cues(), [original[1]])
        result = dialog.timed_cues()
        result[0]['words'][0]['start'] = 99
        self.assertEqual(dialog.timed_cues(), [original[1]])
        self.assertEqual(original, cues())
        # Editing a displayed cell cannot mutate the source cue data.
        dialog.table.item(0, 2).setText('tampered')
        self.assertEqual(dialog.timed_cues(), [original[1]])
        dialog._undo_delete()
        self.assertEqual(dialog.timed_cues(), original)
        dialog.table.selectAll()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            dialog._delete_selected()
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.timed_cues(), [])

    def test_delete_and_undo_clear_selection_and_prevent_accidental_second_delete(self):
        original = cues()
        dialog = TtmlRowsDialog(original, self.translator())
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        dialog.table.selectRow(0)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes) as question:
            QTest.mouseClick(dialog.delete_button, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            self.assertEqual(dialog.table.selectionModel().selectedRows(), [])
            self.assertFalse(dialog.table.currentIndex().isValid())
            self.assertFalse(dialog.delete_button.isEnabled())
            self.assertEqual(dialog.table.verticalHeaderItem(0).text(), '2')
            dialog._delete_selected()
            question.assert_called_once()
            self.assertEqual(dialog.timed_cues(), [original[1]])
        dialog._undo_delete()
        self.assertEqual(dialog.timed_cues(), original)
        self.assertFalse(dialog.table.currentIndex().isValid())
        self.assertFalse(dialog.delete_button.isEnabled())

    def test_nested_editor_finish_close_and_escape_keep_track_form_open(self):
        real_exec = TtmlRowsDialog.exec
        for action in ('finish', 'close', 'escape'):
            with self.subTest(action=action):
                track = PlaylistTrack('', 'Song', lyrics=cues())
                parent = TrackDetailsDialog(track, self.translator())
                self.addCleanup(parent.deleteLater)
                parent.show()
                self.app.processEvents()
                finished = QSignalSpy(parent.finished)

                def run(editor):
                    def edit_and_exit():
                        editor.table.selectRow(0)
                        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                            editor._delete_selected()
                        if action == 'finish':
                            editor.accept()
                        elif action == 'close':
                            editor.close()
                        else:
                            QTest.keyClick(editor, Qt.Key.Key_Escape)
                    QTimer.singleShot(0, edit_and_exit)
                    return real_exec(editor)

                class DrivenEditor(TtmlRowsDialog):
                    def exec(self):
                        return run(self)

                with patch('app.dialogs.track_details_dialog.choose_ttml_edit_mode', return_value='delete'), \
                     patch('app.dialogs.track_details_dialog.TtmlRowsDialog', DrivenEditor):
                    parent._edit_in_lrc_generator()
                self.assertTrue(parent.isVisible())
                self.assertEqual(finished.count(), 0)
                self.assertEqual(parent.selected_lyrics, [cues()[1]] if action == 'finish' else cues())
                self.assertEqual(track.lyrics, cues())
                parent.reject()

    def test_track_ttml_choice_happens_before_regular_editor_and_cancel_changes_nothing(self):
        track = PlaylistTrack('', 'Song', lyrics=cues(), lyrics_path='source.ttml')
        parent = TrackDetailsDialog(track, self.translator())
        self.addCleanup(parent.deleteLater)
        before = deepcopy(parent.selected_lyrics)
        with patch('app.dialogs.track_details_dialog.choose_ttml_edit_mode', return_value='cancel'), \
             patch('app.dialogs.track_details_dialog.LrcGeneratorDialog') as regular, \
             patch('app.dialogs.track_details_dialog.TtmlRowsDialog') as rows:
            parent._edit_in_lrc_generator()
        regular.assert_not_called()
        rows.assert_not_called()
        self.assertEqual(parent.selected_lyrics, before)
        self.assertEqual(parent.selected_lyrics_path, 'source.ttml')
        self.assertEqual(track.lyrics, before)

    def test_track_delete_and_conversion_results_detach_old_file_without_mutating_track(self):
        for mode, expected in [('delete', [cues()[1]]), ('convert', line_cues(cues()))]:
            track = PlaylistTrack('', 'Song', lyrics=cues(), lyrics_path='source.ttml', lyrics_timing_offset_seconds=.25)
            parent = TrackDetailsDialog(track, self.translator())
            self.addCleanup(parent.deleteLater)
            with patch('app.dialogs.track_details_dialog.choose_ttml_edit_mode', return_value=mode), \
                 patch('app.dialogs.track_details_dialog.TtmlRowsDialog') as rows, \
                 patch('app.dialogs.track_details_dialog.LrcGeneratorDialog') as regular:
                editor = (rows if mode == 'delete' else regular).return_value
                editor.exec.return_value = QDialog.DialogCode.Accepted
                editor.timed_cues.return_value = expected
                editor.saved_paths = []
                parent._edit_in_lrc_generator()
                if mode == 'delete':
                    regular.assert_not_called()
                    self.assertEqual(rows.call_args.args[0], cues())
                else:
                    self.assertEqual(regular.call_args.kwargs['initial_cues'], line_cues(cues()))
            self.assertEqual(parent.selected_lyrics, expected)
            self.assertEqual(parent.selected_lyrics_path, '')
            self.assertEqual(parent.selected_timing_offset, .25)
            self.assertEqual(track.lyrics, cues())

    def test_path_only_ttml_is_loaded_and_original_file_is_not_modified(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'source.ttml'
            source = '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="1s" end="4s">Hi</p></body></tt>'
            path.write_text(source, encoding='utf-8')
            parent = TrackDetailsDialog(PlaylistTrack('', 'Song', lyrics_path=str(path)), self.translator())
            self.addCleanup(parent.deleteLater)
            with patch('app.dialogs.track_details_dialog.choose_ttml_edit_mode', return_value='delete') as choice, \
                 patch('app.dialogs.track_details_dialog.TtmlRowsDialog') as rows:
                rows.return_value.exec.return_value = QDialog.DialogCode.Accepted
                rows.return_value.timed_cues.return_value = []
                rows.return_value.saved_paths = []
                parent._edit_in_lrc_generator()
            self.assertEqual(choice.call_args.args[0][0]['format'], 'ttml')
            self.assertEqual(parent.selected_lyrics, [])
            self.assertEqual(parent.selected_lyrics_path, '')
            self.assertEqual(path.read_text(encoding='utf-8'), source)

    def test_regular_editor_cannot_silently_flatten_protected_cues(self):
        dialog = LrcGeneratorDialog([], self.translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(lambda: dialog.done(QDialog.DialogCode.Rejected))
        with self.assertRaises(LyricsError):
            dialog._load_initial_cues(cues())
        self.assertEqual(dialog.windowTitle(), '가사/자막 편집기')

    def test_audio_selected_after_opening_editor_uses_same_ttml_protection(self):
        track = PlaylistTrack('fixture.wav', 'Song', lyrics=cues())
        dialog = LrcGeneratorDialog([], self.translator(), playlist_tracks=[track])
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(lambda: dialog.done(QDialog.DialogCode.Rejected))
        dialog.audio_path = track.file_path
        dialog._applied_lyrics_track = track
        with patch('app.dialogs.lrc_generator_dialog.choose_ttml_edit_mode', return_value='delete'), \
             patch('app.dialogs.lrc_generator_dialog.TtmlRowsDialog') as rows:
            rows.return_value.exec.return_value = QDialog.DialogCode.Accepted
            rows.return_value.timed_cues.return_value = [cues()[1]]
            self.assertFalse(dialog._load_applied_track_lyrics())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.timed_cues(), [cues()[1]])
        self.assertEqual(track.lyrics, cues())

    def test_duplicate_audio_cannot_apply_deletions_to_an_ambiguous_track(self):
        tracks = [PlaylistTrack('fixture.wav', 'One', lyrics=cues()), PlaylistTrack('fixture.wav', 'Two', lyrics=cues())]
        dialog = LrcGeneratorDialog([], self.translator(), playlist_tracks=tracks)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(lambda: dialog.done(QDialog.DialogCode.Rejected))
        dialog.audio_path = 'fixture.wav'
        dialog._applied_lyrics_track = tracks[0]
        with patch('app.dialogs.lrc_generator_dialog.choose_ttml_edit_mode', return_value='delete'), \
             patch('app.dialogs.lrc_generator_dialog.TtmlRowsDialog') as rows, \
             patch('app.dialogs.lrc_generator_dialog.QMessageBox.warning') as warning:
            self.assertFalse(dialog._load_applied_track_lyrics())
        rows.assert_not_called()
        warning.assert_called_once()
        self.assertEqual(tracks[0].lyrics, cues())

    def test_new_action_layout_and_timing_disclosure_preserve_state_across_languages(self):
        parent = TrackDetailsDialog(PlaylistTrack('', 'Song', lyrics=cues()), self.translator())
        self.addCleanup(parent.deleteLater)
        self.assertTrue(parent.load_button.isHidden())
        self.assertTrue(parent.search_lyrics_button.isHidden())
        self.assertEqual(len(parent.import_lyrics_menu.actions()), 3)
        self.assertFalse(parent.timing_toggle.isChecked())
        parent.timing_toggle.setChecked(True)
        parent.timing_offset_spin.setValue(.5)
        parent.translator.set_language(Language.ENGLISH)
        self.assertTrue(parent.timing_toggle.isChecked())
        self.assertEqual(parent.timing_offset_spin.value(), .5)
        self.assertEqual(parent.import_lyrics_button.text(), 'Import…')


class MainWindowTtmlEditTests(MainWindowTestCase):
    def test_selected_ttml_track_uses_protected_deletion_and_can_clear_all(self):
        track = PlaylistTrack('missing.wav', 'Song', lyrics=cues(), lyrics_path='source.ttml')
        self.window.playlist_service.replace([track])
        self.window.playlist_editor.list_widget.item(0).setSelected(True)
        with patch('app.dialogs.ttml_edit_dialog.choose_ttml_edit_mode', return_value='delete'), \
             patch('app.dialogs.ttml_edit_dialog.TtmlRowsDialog') as rows, \
             patch('app.ui.main_window.LrcGeneratorDialog') as regular:
            editor = rows.return_value
            editor.exec.return_value = QDialog.DialogCode.Accepted
            editor.result_track_id = track.id
            editor.saved_paths = []
            editor.add_saved_files_to_project = False
            editor.timed_cues.return_value = []
            self.window._show_lrc_generator()
        regular.assert_not_called()
        self.assertEqual(track.lyrics, [])
        self.assertEqual(track.lyrics_path, '')

    def test_cancel_warning_prevents_constructing_an_editor(self):
        track = PlaylistTrack('missing.wav', 'Song', lyrics=cues())
        self.window.playlist_service.replace([track])
        self.window.playlist_editor.list_widget.item(0).setSelected(True)
        with patch('app.dialogs.ttml_edit_dialog.choose_ttml_edit_mode', return_value='cancel'), \
             patch('app.ui.main_window.LrcGeneratorDialog') as regular:
            self.window._show_lrc_generator()
        regular.assert_not_called()
        self.assertEqual(track.lyrics, cues())

    def test_conversion_explicitly_removes_word_data_before_regular_editor(self):
        track = PlaylistTrack('missing.wav', 'Song', lyrics=cues(), lyrics_path='source.ttml')
        self.window.playlist_service.replace([track])
        self.window.playlist_editor.list_widget.item(0).setSelected(True)
        with patch('app.dialogs.ttml_edit_dialog.choose_ttml_edit_mode', return_value='convert'), \
             patch('app.ui.main_window.LrcGeneratorDialog') as regular:
            editor = regular.return_value
            editor.exec.return_value = QDialog.DialogCode.Accepted
            editor.result_track_id = track.id
            editor.saved_paths = []
            editor.add_saved_files_to_project = False
            editor.timed_cues.return_value = line_cues(cues())
            self.window._show_lrc_generator()
        self.assertEqual(regular.call_args.kwargs['initial_cues'], line_cues(cues()))
        self.assertEqual(track.lyrics_path, '')
        self.assertFalse(any(cue.get('words') for cue in track.lyrics))
