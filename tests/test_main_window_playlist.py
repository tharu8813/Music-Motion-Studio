"""Main window playlist: tracks, lyrics/LRC tools, track details and audio import."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QDropEvent, QImage, QPalette
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QAbstractItemView, QDialog, QFileDialog, QMessageBox, QWidget
from app.models.project import ProjectContent
from app.models.playlist import PlaylistTrack
from app.models.source import SUBTITLE_ANIMATIONS, Source, SourceType
from app.dialogs.settings_dialog import SettingsDialog
from app.dialogs.audio_metadata_dialog import AudioMetadataDialog
from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog
from app.dialogs.m3u_import_dialog import M3uImportDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.services.playlist_service import AudioImportCandidate, PlaylistService
from app.services.lrc_draft_service import LrcDraftService
from app.utils.i18n import Language
from tests.main_window_base import MainWindowTestCase


class MainWindowPlaylistTests(MainWindowTestCase):
    def test_lyrics_inspector_offers_modern_transition_styles(self) -> None:
        values = {
            self.window.inspector.lyrics.widgets["subtitle_animation"].itemData(index)
            for index in range(self.window.inspector.lyrics.widgets["subtitle_animation"].count())
        }
        self.assertEqual(values, set(SUBTITLE_ANIMATIONS))
        self.assertEqual(Source(SourceType.LYRICS, "Lyrics").subtitle_animation, "glow")
        labels = [
            self.window.inspector.lyrics.widgets["subtitle_animation"].itemText(index)
            for index in range(self.window.inspector.lyrics.widgets["subtitle_animation"].count())
        ]
        self.assertIn("글로우", labels)
        self.assertIn("라이즈", labels)
        self.assertFalse(any("Apple" in label or "Spotify" in label for label in labels))

    def test_audio_import_prompts_only_for_missing_metadata_and_honors_cancel(self) -> None:
        original_count = len(self.window.playlist_service.tracks)
        track = PlaylistTrack(
            str(Path("missing-tags.mp3").resolve()), "missing-tags",
        )
        candidate = AudioImportCandidate(track, ("artist", "album"))
        edited = PlaylistTrack(
            track.file_path, "missing-tags", artist="Edited artist",
            album="Edited album",
        )
        with (
            patch.object(
                self.window.playlist_service, "inspect_files",
                return_value=[candidate],
            ),
            patch("app.ui.main_window.AudioMetadataDialog") as dialog_type,
        ):
            metadata_dialog = dialog_type.return_value
            metadata_dialog.exec.return_value = QDialog.DialogCode.Rejected
            added, accepted, _notes = self.window._import_audio_files(
                [track.file_path]
            )
            self.assertEqual((added, accepted), (0, []))
            self.assertEqual(len(self.window.playlist_service.tracks), original_count)

            metadata_dialog.exec.return_value = QDialog.DialogCode.Accepted
            metadata_dialog.selected_tracks = [edited]
            added, accepted, _notes = self.window._import_audio_files(
                [track.file_path]
            )

        self.assertEqual(added, 1)
        self.assertEqual(accepted, [Path(track.file_path)])
        imported = self.window.playlist_service.tracks[-1]
        self.assertEqual(imported.artist, "Edited artist")
        self.assertEqual(imported.album, "Edited album")

    def test_same_name_lyric_file_is_attached_automatically_when_always(self) -> None:
        imported, notes = self._import_with_sidecar(
            {"Nightfall.lrc": self._LRC}, "always", "Nightfall.mp3",
        )
        self.assertTrue(imported.lyrics)
        self.assertEqual(Path(imported.lyrics_path).name, "Nightfall.lrc")
        self.assertEqual(len(notes), 1)

    def test_never_mode_skips_even_an_exact_lyric_match(self) -> None:
        imported, notes = self._import_with_sidecar(
            {"Nightfall.lrc": self._LRC}, "never", "Nightfall.mp3",
        )
        self.assertFalse(imported.lyrics)
        self.assertEqual(imported.lyrics_path, "")
        self.assertEqual(notes, [])

    def test_ttml_sidecar_is_attached_automatically(self) -> None:
        imported, notes = self._import_with_sidecar(
            {"Nightfall.ttml": '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="1s" end="5s">Hello</p></body></tt>'},
            "always", "Nightfall.mp3",
        )
        self.assertEqual(Path(imported.lyrics_path).name, "Nightfall.ttml")
        self.assertEqual(imported.lyrics[0]["format"], "ttml")
        self.assertEqual(len(notes), 1)

    def test_competing_lyrics_always_offer_file_choice_and_honor_cancel(self) -> None:
        files = {"Nightfall.lrc": self._LRC, "Nightfall.srt": "1\n00:00:01,000 --> 00:00:05,000\nHello\n",
                 "Nightfall.ttml": '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="1s" end="5s">Hello</p></body></tt>'}
        for mode in ("always", "ask"):
            with self.subTest(mode=mode), patch("app.ui.main_window.QInputDialog.getItem") as choose, patch.object(QMessageBox, "question") as confirm:
                choose.side_effect = lambda *args: (next(label for label in args[3] if "Nightfall.ttml" in label), True)
                imported, notes = self._import_with_sidecar(files, mode, "Nightfall.mp3")
                choose.assert_called_once()
                confirm.assert_not_called()
                self.assertEqual(Path(imported.lyrics_path).name, "Nightfall.ttml")
                self.assertEqual(len(notes), 1)
            with patch("app.ui.main_window.QInputDialog.getItem", return_value=("", False)):
                imported, notes = self._import_with_sidecar(files, mode, "Nightfall.mp3")
                self.assertEqual(imported.lyrics, [])
                self.assertEqual(imported.lyrics_path, "")
                self.assertEqual(notes, [])

    def test_similar_name_lyric_always_prompts_even_in_always_mode(self) -> None:
        with patch.object(
            QMessageBox, "question",
            return_value=QMessageBox.StandardButton.Yes,
        ) as question:
            imported, notes = self._import_with_sidecar(
                {"Golden Hour.lrc": self._LRC}, "always", "01 - Golden Hour.mp3",
            )
        question.assert_called_once()
        self.assertTrue(imported.lyrics)
        self.assertEqual(Path(imported.lyrics_path).name, "Golden Hour.lrc")
        self.assertEqual(len(notes), 1)

    def test_settings_dialog_round_trips_the_lyrics_auto_attach_mode(self) -> None:
        dialog = SettingsDialog(
            replace(
                self.window.settings_service.current,
                lyrics_auto_attach_mode="ask",
            ),
            self.window.translator.language,
            self.window.theme_service.preference,
            self.window.translator,
            self.window,
        )
        try:
            self.assertEqual(
                dialog.lyrics_auto_attach_combo.currentData(), "ask",
            )
            dialog.lyrics_auto_attach_combo.setCurrentIndex(
                dialog.lyrics_auto_attach_combo.findData("never")
            )
            self.assertEqual(
                dialog.app_settings.lyrics_auto_attach_mode, "never",
            )
        finally:
            dialog.close()

    def test_new_lyrics_and_track_lists_default_to_automatic_line_counts(self) -> None:
        self.window.translator.set_language(Language.KOREAN)
        self.window._add_source(SourceType.LYRICS)
        lyrics = self.window.store.sources()[-1]
        self.assertEqual(lyrics.subtitle_context_lines, -1)
        self.assertEqual(lyrics.subtitle_next_lines, -1)
        self.assertEqual(self.window.inspector.lyrics.widgets["subtitle_context_lines"].text(), "자동")
        self.assertEqual(self.window.inspector.lyrics.widgets["subtitle_next_lines"].text(), "자동")

        self.window._add_source(SourceType.TRACK_LIST)
        track_list = self.window.store.sources()[-1]
        self.assertEqual(track_list.track_list_count, 0)
        self.assertEqual(self.window.inspector.track_list.widgets["track_list_count"].text(), "자동")
        self.assertGreater(
            self.window.canvas._items[track_list.id].effective_track_list_count(),
            2,
        )

    def test_lyrics_drop_targets_the_playlist_row_under_pointer(self) -> None:
        track = PlaylistTrack("song.wav", "Drop target", duration_seconds=10.0)
        self.window.playlist_service.add_tracks([track])
        self.application.processEvents()
        item = self.window.playlist_editor.list_widget.item(0)
        self.assertIsNotNone(item)
        with TemporaryDirectory(prefix="playlist-lyrics-drop-") as directory:
            lyrics_path = Path(directory) / "incoming.lrc"
            lyrics_path.write_text("[00:01.00]Incoming lyric\n", encoding="utf-8")
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(lyrics_path))])
            event = QDropEvent(
                QPointF(self.window.playlist_editor.list_widget.visualItemRect(item).center()),
                Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            self.window.playlist_editor.list_widget.dropEvent(event)

        self.assertTrue(event.isAccepted())
        updated = self.window.playlist_service.tracks[0]
        self.assertEqual(updated.lyrics[0]["text"], "Incoming lyric")
        self.assertTrue(updated.lyrics_path.endswith("incoming.lrc"))

    def test_playlist_accepts_external_drops_for_project_content_dnd(self) -> None:
        from PySide6.QtGui import QDragEnterEvent

        track = PlaylistTrack("song.wav", "Row", duration_seconds=10.0)
        self.window.playlist_service.add_tracks([track])
        self.application.processEvents()
        playlist_list = self.window.playlist_editor.list_widget

        # InternalMove silently rejects external drops before dropEvent runs,
        # which is why lyrics dragged from Project Content never attached.
        self.assertEqual(
            playlist_list.dragDropMode(),
            QAbstractItemView.DragDropMode.DragDrop,
        )

        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QDragMoveEvent

        with TemporaryDirectory(prefix="pl-ext-drop-") as directory:
            lyrics_path = Path(directory) / "song.lrc"
            lyrics_path.write_text("[00:01.00]hi\n", encoding="utf-8")
            item = playlist_list.item(0)
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(lyrics_path))])
            row_center = playlist_list.visualItemRect(item).center()

            def make(kind, pos):
                return kind(
                    pos, Qt.DropAction.CopyAction, mime,
                    Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                )

            # The drag border is crossed over the spacing gap, not a row.  The
            # ENTER must still be accepted or Qt stops delivering move events
            # and the cursor stays forbidden even after reaching the row.
            enter = make(QDragEnterEvent, QPoint(2, 1))
            playlist_list.dragEnterEvent(enter)
            self.assertTrue(enter.isAccepted())

            off_row = make(QDragMoveEvent, QPoint(2, 1))
            playlist_list.dragMoveEvent(off_row)
            self.assertFalse(off_row.isAccepted())

            on_row = make(QDragMoveEvent, row_center)
            playlist_list.dragMoveEvent(on_row)
            self.assertTrue(on_row.isAccepted())

    def test_playlist_drag_reorder_moves_rows_without_duplicating(self) -> None:
        from PySide6.QtGui import QDropEvent

        self.window.playlist_service.add_tracks([
            PlaylistTrack("a.wav", "Alpha", duration_seconds=10.0),
            PlaylistTrack("b.wav", "Bravo", duration_seconds=10.0),
            PlaylistTrack("c.wav", "Charlie", duration_seconds=10.0),
        ])
        self.application.processEvents()
        playlist_list = self.window.playlist_editor.list_widget
        service = self.window.playlist_service

        # Drag "Alpha" (row 0) onto the lower half of "Charlie" (row 2).
        playlist_list.item(0).setSelected(True)
        target = playlist_list.item(2)
        rect = playlist_list.visualItemRect(target)
        drop_point = QPointF(rect.center().x(), rect.bottom() - 1)
        mime = QMimeData()
        drop = QDropEvent(
            drop_point, Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        playlist_list._reorder_from_drop(drop)
        self.application.processEvents()

        titles = [track.title for track in service.tracks]
        self.assertEqual(titles, ["Bravo", "Charlie", "Alpha"])
        self.assertEqual(len(titles), 3)  # moved, not copied

    def test_existing_lyrics_are_replaced_only_after_comparison_choice(self) -> None:
        track = PlaylistTrack(
            "song.wav", "Existing lyrics", duration_seconds=10.0,
            lyrics_path="old.lrc",
            lyrics=[{"start": 0.0, "end": 4.0, "text": "Old lyric"}],
        )
        self.window.playlist_service.add_tracks([track])
        with TemporaryDirectory(prefix="playlist-lyrics-compare-") as directory:
            lyrics_path = Path(directory) / "replacement.lrc"
            lyrics_path.write_text("[00:02.00]New lyric\n", encoding="utf-8")
            with patch("app.ui.main_window.LyricsCompareDialog") as dialog_type:
                dialog_type.return_value.exec.return_value = QDialog.DialogCode.Rejected
                dialog_type.return_value.replace_requested = False
                self.assertFalse(
                    self.window._attach_lyrics_to_track(lyrics_path, track.id)
                )
                self.assertEqual(
                    self.window.playlist_service.tracks[0].lyrics[0]["text"],
                    "Old lyric",
                )

                dialog_type.return_value.exec.return_value = QDialog.DialogCode.Accepted
                dialog_type.return_value.replace_requested = True
                self.assertTrue(
                    self.window._attach_lyrics_to_track(lyrics_path, track.id)
                )

        self.assertEqual(
            self.window.playlist_service.tracks[0].lyrics[0]["text"], "New lyric",
        )

    def test_track_lyrics_dialog_adjusts_only_selected_track_timing(self) -> None:
        original_language = self.window.translator.language
        self.window.translator.set_language(Language.KOREAN)
        track = PlaylistTrack(
            "track.wav", "Track", duration_seconds=20.0,
            lyrics_path="track.lrc",
            lyrics=[{"start": 5.0, "end": 7.0, "text": "첫 줄"}],
            lyrics_timing_offset_seconds=1.0,
        )
        dialog = TrackDetailsDialog(track, self.window.translator, self.window)
        try:
            self.assertIn("00:04.000", dialog.preview.toPlainText())
            dialog.timing_offset_spin.setValue(2.0)
            self.assertIn("00:03.000", dialog.preview.toPlainText())
            self.assertEqual(track.lyrics_timing_offset_seconds, 1.0)
            dialog._accept()
            self.assertEqual(dialog.selected_timing_offset, 2.0)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        finally:
            dialog.close()
            self.window.translator.set_language(original_language)
            self.application.processEvents()

    def test_lrc_generator_loads_existing_cues_and_preserves_timing_for_text_edits(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "generator-existing-song.wav"
            audio_path.write_bytes(b"test")
            candidate = AudioImportCandidate(PlaylistTrack(
                str(audio_path), "Loaded title", "Loaded artist",
                duration_seconds=8.0,
            ))
            with (
                patch.object(PlaylistService, "inspect_files", return_value=[candidate]),
                patch("app.dialogs.lrc_generator_dialog.QMediaPlayer.setSource"),
            ):
                dialog = LrcGeneratorDialog(
                    [],
                    self.window.translator,
                    self.window,
                    initial_audio_path=str(audio_path),
                    initial_cues=[
                        {"start": 1.25, "end": 3.0, "text": "First\nSecond"},
                        {"start": 4.5, "end": 7.0, "text": "Next"},
                    ],
                    initial_title="Loaded title",
                    initial_artist="Loaded artist",
                )
                try:
                    self.assertEqual(dialog.audio_path, str(audio_path.resolve()))
                    self.assertEqual(dialog.lines, ["First\nSecond", "Next"])
                    self.assertEqual(dialog.timestamps, [1.25, 4.5])
                    self.assertEqual(dialog.input_mode_combo.currentData(), "multiline")
                    self.assertEqual(dialog.title_edit.text(), "Loaded title")
                    self.assertEqual(dialog.artist_edit.text(), "Loaded artist")

                    dialog.lyrics_editor.setPlainText("Edited first\nSecond\n\nEdited next")
                    dialog._prepare_lines()
                    self.assertEqual(dialog.lines, ["Edited first\nSecond", "Edited next"])
                    self.assertEqual(dialog.timestamps, [1.25, 4.5])
                finally:
                    dialog.done(QDialog.DialogCode.Rejected)
                    self.application.processEvents()

    def test_lrc_generator_uses_four_step_wizard_navigation(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "wizard-song.wav"
            audio_path.write_bytes(b"test")
            candidate = AudioImportCandidate(PlaylistTrack(
                str(audio_path), "Wizard song", duration_seconds=8.0,
            ))
            with (
                patch.object(PlaylistService, "inspect_files", return_value=[candidate]),
                patch("app.dialogs.lrc_generator_dialog.QMediaPlayer.setSource"),
            ):
                dialog = LrcGeneratorDialog(
                    [],
                    self.window.translator,
                    self.window,
                    initial_audio_path=str(audio_path),
                    initial_cues=[
                        {"start": 1.0, "end": 3.0, "text": "First"},
                        {"start": 4.0, "end": 7.0, "text": "Second"},
                    ],
                )
                try:
                    self.assertEqual(dialog.pages.count(), 4)
                    self.assertEqual(dialog.pages.currentIndex(), 0)
                    self.assertTrue(dialog.back_button.isHidden())

                    dialog._next_step()
                    self.assertEqual(dialog.pages.currentIndex(), 1)
                    dialog._next_step()
                    self.assertEqual(dialog.pages.currentIndex(), 2)
                    self.assertEqual(dialog.timestamps, [1.0, 4.0])
                    dialog._next_step()
                    self.assertEqual(dialog.pages.currentIndex(), 3)
                    self.assertIn("[00:01.00]First", dialog.review_text.toPlainText())
                    self.assertTrue(dialog.next_button.isHidden())
                    self.assertFalse(dialog.finish_button.isHidden())

                    dialog._previous_step()
                    self.assertEqual(dialog.pages.currentIndex(), 2)
                finally:
                    dialog.done(QDialog.DialogCode.Rejected)

    def test_lrc_generator_wizard_blocks_progress_without_required_input(self) -> None:
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            dialog._next_step()
            self.assertEqual(dialog.pages.currentIndex(), 0)
            self.assertTrue(dialog.status_label.property("error"))
        finally:
            dialog.close()

    def test_lrc_generator_offers_to_load_already_applied_track_lyrics(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "song-with-lyrics.wav"
            audio_path.write_bytes(b"test")
            track = PlaylistTrack(
                str(audio_path),
                "Applied title",
                artist="Applied artist",
                lyrics=[
                    {"start": 1.0, "end": 3.0, "text": "Applied first"},
                    {"start": 4.0, "end": 7.0, "text": "Applied second"},
                ],
            )
            candidate = MagicMock()
            candidate.track.title = track.title
            candidate.track.artist = track.artist
            with patch.object(PlaylistService, "inspect_files", return_value=[candidate]):
                dialog = LrcGeneratorDialog(
                    [], self.window.translator, self.window,
                    playlist_tracks=[track], initial_audio_path=str(audio_path),
                )
            try:
                with patch.object(
                    dialog, "_existing_lyrics_choice", return_value="load",
                ) as choice:
                    dialog._next_step()
                choice.assert_called_once()
                self.assertEqual(dialog.pages.currentIndex(), 2)
                self.assertEqual(dialog.lines, ["Applied first", "Applied second"])
                self.assertEqual(dialog.timestamps, [1.0, 4.0])
                self.assertEqual(
                    dialog.lyrics_editor.toPlainText(), "Applied first\nApplied second",
                )
            finally:
                dialog.done(QDialog.DialogCode.Rejected)
                self.application.processEvents()

    def test_lrc_generator_can_start_over_or_cancel_when_lyrics_exist(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "song-with-existing-lyrics.wav"
            audio_path.write_bytes(b"test")
            track = PlaylistTrack(
                str(audio_path), "Track",
                lyrics=[{"start": 1.0, "end": 3.0, "text": "Existing"}],
            )
            candidate = MagicMock()
            candidate.track.title = track.title
            candidate.track.artist = track.artist
            with patch.object(PlaylistService, "inspect_files", return_value=[candidate]):
                dialog = LrcGeneratorDialog(
                    [], self.window.translator, self.window,
                    playlist_tracks=[track], initial_audio_path=str(audio_path),
                )
            try:
                with patch.object(dialog, "_existing_lyrics_choice", return_value="cancel"):
                    dialog._next_step()
                self.assertEqual(dialog.pages.currentIndex(), 0)

                dialog.lyrics_editor.setPlainText("Old draft")
                dialog.lines = ["Old draft"]
                dialog.timestamps = [2.0]
                with patch.object(dialog, "_existing_lyrics_choice", return_value="new"):
                    dialog._next_step()
                self.assertEqual(dialog.pages.currentIndex(), 1)
                self.assertEqual(dialog.lyrics_editor.toPlainText(), "")
                self.assertEqual(dialog.lines, [])
                self.assertEqual(dialog.timestamps, [])
            finally:
                dialog.done(QDialog.DialogCode.Rejected)
                self.application.processEvents()

    def test_lrc_generator_audio_step_uses_combo_and_read_only_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "project-song.wav"
            audio_path.write_bytes(b"test")
            candidate = MagicMock()
            candidate.track.title = "Metadata title"
            candidate.track.artist = "Metadata artist"
            with patch.object(PlaylistService, "inspect_files", return_value=[candidate]):
                dialog = LrcGeneratorDialog(
                    [ProjectContent(str(audio_path), "audio", "Project song")],
                    self.window.translator,
                    self.window,
                )
            try:
                self.assertEqual(dialog.audio_path, str(audio_path.resolve()))
                self.assertEqual(dialog.title_edit.text(), "Metadata title")
                self.assertEqual(dialog.artist_edit.text(), "Metadata artist")
                self.assertTrue(dialog.title_edit.isReadOnly())
                self.assertTrue(dialog.artist_edit.isReadOnly())
                self.assertFalse(hasattr(dialog, "use_project_audio_button"))
                local_index = dialog.project_audio_combo.findData("__local__")
                self.assertGreaterEqual(local_index, 0)
                dialog.project_audio_combo.setCurrentIndex(local_index)
                self.assertFalse(dialog.local_audio_row.isHidden())
            finally:
                dialog.done(QDialog.DialogCode.Rejected)
                self.application.processEvents()

    def test_lrc_generator_preprocesses_bracket_lines_and_regex_on_next(self) -> None:
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            dialog.pages.setCurrentIndex(1)
            dialog.lyrics_editor.setPlainText(
                "[Verse 1]\n01. First lyric\n\n02. Second lyric"
            )
            dialog.ignore_line_breaks_check.setChecked(True)
            dialog.ignore_bracketed_lines_check.setChecked(True)
            dialog.advanced_filter_check.setChecked(True)
            dialog.regex_filter_edit.setText(r"^\d+\.\s*")
            dialog._next_step()
            self.assertEqual(dialog.pages.currentIndex(), 2)
            self.assertEqual(dialog.lines, ["First lyric", "Second lyric"])
            self.assertEqual(dialog.timestamps, [None, None])
        finally:
            dialog.close()

    def test_lrc_generator_keeps_selection_separate_from_timing_cursor(self) -> None:
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            dialog.lines = ["First", "Second", "Third"]
            dialog.timestamps = [1.0, 2.0, None]
            dialog.current_index = 0
            dialog._refresh_table(0)
            dialog._seek_to_row(0)
            self.assertEqual(dialog._selected_row(), 0)
            self.assertEqual(dialog.current_index, 1)
            self.assertTrue(dialog.timeline_table.item(0, 0).text().startswith("●"))
            self.assertTrue(dialog.timeline_table.item(1, 0).text().startswith("▶"))
            cursor_color = dialog.timeline_table.item(1, 0).background().color()
            self.assertEqual(cursor_color.red(), 255)
            self.assertLessEqual(cursor_color.alpha(), 32)
            selection_style = dialog.timeline_table.styleSheet()
            self.assertIn("background-color: transparent", selection_style)
            self.assertNotIn("#2563EB", selection_style)

            dialog._seek_to_row(0)
            self.assertEqual(dialog._selected_row(), 0)
            self.assertEqual(dialog.current_index, 0)
            self.assertTrue(dialog.timeline_table.item(0, 0).text().startswith("▶ ●"))
        finally:
            dialog.close()

    def test_lrc_generator_highlights_and_centers_playback_lyric(self) -> None:
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            dialog.lines = ["First", "Second", "Third"]
            dialog.timestamps = [1.0, 4.0, 8.0]
            dialog.current_index = 2
            dialog.pages.setCurrentIndex(2)
            dialog._refresh_table()
            with patch.object(dialog.timeline_table, "scrollToItem") as scroll_to_item:
                dialog._position_changed(4_500)
            self.assertEqual(dialog._playback_highlight_row, 1)
            self.assertTrue(dialog.timeline_table.item(1, 0).text().startswith("♪"))
            self.assertEqual(
                dialog.timeline_table.item(1, 1).background().color(), QColor("#FFD54F"),
            )
            scroll_to_item.assert_not_called()

            dialog.audio_path = str(Path("preview-mode.wav").resolve())
            dialog._update_enabled()
            with patch.object(dialog.timeline_table, "scrollToItem") as preview_scroll:
                dialog.preview_mode_check.setChecked(True)
                dialog._position_changed(4_600)
            preview_scroll.assert_called_with(
                dialog.timeline_table.item(1, 0),
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )
            self.assertFalse(dialog.record_button.isEnabled())
            self.assertFalse(dialog.edit_line_button.isEnabled())
            self.assertFalse(dialog.back_button.isEnabled())
            self.assertNotIn("▶", dialog.timeline_table.item(2, 0).text())
            self.assertIn("♪", dialog.timeline_table.item(1, 0).text())

            palette = dialog.timeline_table.palette()
            palette.setColor(QPalette.ColorRole.Text, QColor("#F1F5F9"))
            dialog.timeline_table.setPalette(palette)
            dialog.preview_mode_check.setChecked(False)
            dialog._apply_timeline_visuals()
            self.assertEqual(
                dialog.timeline_table.item(0, 1).foreground().color(),
                QColor("#F1F5F9"),
            )
            self.assertEqual(
                dialog.timeline_table.item(1, 1).foreground().color(),
                QColor("#202020"),
            )
        finally:
            dialog.close()

    def test_lrc_generator_uses_labeled_role_grouped_timing_tools(self) -> None:
        original_language = self.window.translator.language
        self.window.translator.set_language(Language.KOREAN)
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            tools = (
                dialog.undo_button, dialog.redo_button, dialog.use_selected_button,
                dialog.nudge_back_button, dialog.nudge_forward_button,
                dialog.clear_time_button, dialog.reset_all_button,
                dialog.add_line_button, dialog.edit_line_button,
                dialog.delete_line_button,
            )
            for button in tools:
                self.assertFalse(button.icon().isNull())
                self.assertTrue(button.toolTip())
                self.assertEqual(
                    button.toolButtonStyle(),
                    Qt.ToolButtonStyle.ToolButtonTextBesideIcon,
                )
                self.assertTrue(button.text())
                self.assertGreaterEqual(button.minimumHeight(), 36)
            self.assertEqual(dialog.history_tools_group.title(), "실행 이력")
            self.assertEqual(dialog.timing_tools_group.title(), "선택 시간 조정")
            self.assertEqual(dialog.lyric_tools_group.title(), "가사 편집")
            self.assertFalse(
                dialog.timing_tools_group.isAncestorOf(dialog.use_selected_button)
            )
            self.assertEqual(dialog.record_button.objectName(), "primaryButton")
            self.assertEqual(dialog.play_button.objectName(), "transportPlayButton")
            self.assertFalse(dialog.forward_button.icon().isNull())
            self.assertGreaterEqual(dialog.position_slider.minimumHeight(), 28)
            self.assertEqual(dialog.preview_group.title(), "음악 재생")
        finally:
            dialog.close()
            self.window.translator.set_language(original_language)

    def test_lrc_generator_recording_always_advances_to_immediate_next_line(self) -> None:
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            dialog.lines = ["First", "Already timed", "Third"]
            dialog.timestamps = [None, 5.0, None]
            dialog.current_index = 0
            dialog.audio_path = str(Path("record-next-line.wav").resolve())
            dialog._refresh_table(0)
            with (
                patch.object(dialog.media_player, "position", return_value=1_250),
                patch.object(dialog, "_autosave_after_change"),
                patch.object(dialog.timeline_table, "scrollToItem") as scroll_to_item,
            ):
                dialog._record_timestamp()
            self.assertEqual(dialog.timestamps, [1.25, 5.0, None])
            self.assertEqual(dialog.current_index, 1)
            self.assertTrue(dialog.timeline_table.item(1, 0).text().startswith("▶"))
            scroll_to_item.assert_called_with(
                dialog.timeline_table.item(1, 0),
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )
        finally:
            dialog.close()

    def test_lrc_generator_accepts_value_equivalent_yes_for_untimed_lyrics(self) -> None:
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            dialog.lines = ["Timed", "Untimed"]
            dialog.timestamps = [1.0, None]
            dialog.current_index = 1
            dialog.pages.setCurrentIndex(2)
            with patch.object(
                QMessageBox, "warning",
                # Packaged PySide may return a value-equivalent integer wrapper.
                return_value=QMessageBox.StandardButton.Yes.value,
            ):
                dialog._next_step()
            self.assertEqual(dialog.pages.currentIndex(), 3)
            self.assertIn("Timed", dialog.review_text.toPlainText())
            self.assertNotIn("Untimed", dialog.review_text.toPlainText())
        finally:
            dialog.close()

    def test_lrc_generator_add_edit_delete_and_back_change_policy(self) -> None:
        dialog = LrcGeneratorDialog([], self.window.translator, self.window)
        try:
            dialog.lines = ["First", "Second"]
            dialog.timestamps = [1.0, 2.0]
            dialog._timing_baseline_lines = list(dialog.lines)
            dialog._timing_baseline_timestamps = list(dialog.timestamps)
            dialog.pages.setCurrentIndex(2)
            dialog._refresh_table(0)

            with patch(
                "app.dialogs.lrc_generator_dialog.QInputDialog.getMultiLineText",
                return_value=("Edited", True),
            ):
                dialog._edit_selected_line()
            self.assertEqual(dialog.lines[0], "Edited")
            self.assertEqual(dialog.timestamps[0], 1.0)

            with patch(
                "app.dialogs.lrc_generator_dialog.QInputDialog.getMultiLineText",
                return_value=("Added", True),
            ):
                dialog._add_lyric_line()
            self.assertEqual(dialog.lines, ["Edited", "Added", "Second"])
            self.assertEqual(dialog.timestamps, [1.0, None, 2.0])

            with patch.object(dialog, "_confirm_timing_changes_before_back", return_value="discard"):
                dialog._previous_step()
            self.assertEqual(dialog.pages.currentIndex(), 1)
            self.assertEqual(dialog.lines, ["First", "Second"])
            self.assertEqual(dialog.timestamps, [1.0, 2.0])
            self.assertEqual(dialog.lyrics_editor.toPlainText(), "First\nSecond")

            dialog.pages.setCurrentIndex(2)
            dialog._refresh_table(0)
            with patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog._delete_selected_line()
            self.assertEqual(dialog.lines, ["Second"])
            self.assertEqual(dialog.timestamps, [2.0])
        finally:
            dialog.close()

    def test_lrc_generator_recovers_legacy_draft_with_empty_raw_input(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "legacy-draft.wav"
            audio_path.write_bytes(b"test")
            service = LrcDraftService(root)
            service.save(str(audio_path), {
                "lyrics_text": "",
                "lines": ["Legacy first", "Legacy second"],
                "timestamps": [2.0, None],
                "current_index": 1,
                "title": "Legacy title",
                "artist": "Legacy artist",
                "input_mode": "single",
            })
            candidate = MagicMock()
            candidate.track.title = "Audio title"
            candidate.track.artist = "Audio artist"
            dialog = LrcGeneratorDialog([], self.window.translator, self.window)
            dialog._draft_service = service
            try:
                with (
                    patch.object(PlaylistService, "inspect_files", return_value=[candidate]),
                    patch.object(
                        QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes,
                    ),
                ):
                    dialog._set_audio(str(audio_path))
                self.assertEqual(dialog.pages.currentIndex(), 2)
                self.assertEqual(dialog.lines, ["Legacy first", "Legacy second"])
                self.assertEqual(
                    dialog.lyrics_editor.toPlainText(), "Legacy first\nLegacy second",
                )
            finally:
                dialog.done(QDialog.DialogCode.Rejected)
                self.application.processEvents()

    def test_track_lyrics_dialog_round_trips_edits_from_lrc_generator(self) -> None:
        track = PlaylistTrack(
            "track.wav", "Track title", artist="Track artist",
            lyrics_path="original.lrc",
            lyrics=[{"start": 2.0, "end": 5.0, "text": "Original"}],
        )
        dialog = TrackDetailsDialog(track, self.window.translator, self.window)
        edited = [{"start": 3.0, "end": 8.0, "text": "Edited"}]
        saved = Path("edited-track.lrc").resolve()
        try:
            with patch(
                "app.dialogs.track_details_dialog.LrcGeneratorDialog",
            ) as generator_type:
                generator = generator_type.return_value
                generator.exec.return_value = QDialog.DialogCode.Accepted
                generator.timed_cues.return_value = edited
                generator.saved_paths = [saved]
                dialog._edit_in_lrc_generator()

            kwargs = generator_type.call_args.kwargs
            self.assertTrue(kwargs["track_edit_mode"])
            self.assertEqual(kwargs["initial_audio_path"], track.file_path)
            self.assertEqual(kwargs["initial_cues"], track.lyrics)
            self.assertEqual(kwargs["initial_title"], track.title)
            self.assertEqual(kwargs["initial_artist"], track.artist)
            self.assertEqual(dialog.selected_lyrics, edited)
            self.assertEqual(dialog.selected_lyrics_path, str(saved))
            self.assertEqual(track.lyrics[0]["text"], "Original")
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        finally:
            dialog.close()

    def test_track_lyrics_dialog_shows_all_track_information_labels(self) -> None:
        original_language = self.window.translator.language
        self.window.translator.set_language(Language.KOREAN)
        track = PlaylistTrack(
            "C:/Music/long folder/song.m4a", "Visible title",
            artist="Visible artist", album="", duration_seconds=125,
        )
        dialog = TrackDetailsDialog(track, self.window.translator, self.window)
        try:
            self.assertEqual(
                [label.text() for label in dialog.info_name_labels], ["제목", "아티스트", "앨범"],
            )
            self.assertEqual(
                [label.text() for label in dialog.info_labels], ["Visible title", "Visible artist", ""],
            )
            self.assertEqual(
                [label.text() for label in dialog.file_name_labels],
                ["위치", "재생 시간", "형식", "음질", "크기"],
            )
            self.assertEqual(dialog.duration_label.text(), "02:05")
            self.assertEqual(dialog.format_label.text(), "파일을 찾을 수 없음")
            self.assertFalse(dialog.reveal_file_button.isEnabled())
            self.assertEqual(dialog.album_edit.placeholderText(), "—")
            self.assertTrue(
                dialog.file_label.textInteractionFlags()
                & Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.assertEqual(dialog.file_label.toolTip(), "C:/Music/long folder/song.m4a")
            # The header sums the track up above every tab, following unsaved edits.
            self.assertEqual(dialog.header_title.text(), "Visible title")
            self.assertEqual(dialog.header_subtitle.text(), "Visible artist")
            self.assertIn("분석 전", dialog.header_facts.text())
            dialog.album_edit.setText("New album")
            self.assertEqual(dialog.header_subtitle.text(), "Visible artist · New album")
            # Analysis is offered whatever the transition mode.
            self.assertEqual(dialog.analysis_panel.analyze_button.text(), "이 곡 분석")
            self.assertIn("AutoMix를 쓰지 않아도", dialog.analysis_panel.status_label.text())
        finally:
            dialog.close()
            self.window.translator.set_language(original_language)

    def test_track_details_uses_information_and_lyrics_tabs_and_edits_cover(self) -> None:
        with TemporaryDirectory(prefix="playlist-track-cover-") as raw_directory:
            directory = Path(raw_directory)
            cover_path = directory / "custom-cover.png"
            cover = QImage(180, 180, QImage.Format.Format_ARGB32)
            cover.fill(QColor("#7C3AED"))
            self.assertTrue(cover.save(str(cover_path)))
            track = PlaylistTrack("missing-audio.mp3", "Cover track")
            dialog = TrackDetailsDialog(track, self.window.translator, self.window)
            try:
                self.assertEqual(dialog.windowTitle(), "곡 정보/설정")
                self.assertEqual(dialog.tabs.count(), 5)
                self.assertEqual(
                    [dialog.tabs.tabText(index) for index in range(5)],
                    ["곡 정보", "분석", "가사 설정", "이 곡의 영상", "오디오"],
                )
                self.assertIs(dialog.tabs.widget(1), dialog.analysis_tab)
                self.assertTrue(dialog.info_tab.isAncestorOf(dialog.info_group))
                self.assertTrue(dialog.lyrics_tab.isAncestorOf(dialog.lyrics_group))
                self.assertTrue(dialog.lyrics_tab.isAncestorOf(dialog.playback_group))

                with patch.object(
                    QFileDialog, "getOpenFileName",
                    return_value=(str(cover_path), "Images (*.png)"),
                ):
                    dialog._choose_cover()
                self.assertEqual(dialog.selected_cover_path, str(cover_path.resolve()))
                self.assertFalse(dialog.cover_preview.pixmap().isNull())
                self.assertEqual(dialog.cover_source_label.text(), cover_path.name)
                self.assertTrue(dialog.reset_cover_button.isEnabled())

                dialog._reset_cover()
                self.assertEqual(dialog.selected_cover_path, "")
                self.assertFalse(dialog.reset_cover_button.isEnabled())
            finally:
                dialog.close()

    def test_track_details_can_attach_lyrics_from_project_content(self) -> None:
        with TemporaryDirectory(prefix="pvs-track-content-lyrics-") as raw:
            lrc = Path(raw) / "my song.lrc"
            lrc.write_text("[00:02.00]From content\n", encoding="utf-8")
            self.window.project_content_service.add_paths([lrc])
            track = PlaylistTrack("my song.wav", "My Song", duration_seconds=30.0)
            self.window.playlist_service.replace([track])

            # The window feeds the dialog the project's lyrics content.
            captured: list[TrackDetailsDialog] = []
            original = TrackDetailsDialog

            with patch(
                "app.ui.main_window.TrackDetailsDialog",
                side_effect=lambda *a, **k: captured.append(original(*a, **k)) or captured[-1],
            ), patch.object(original, "exec", return_value=QDialog.DialogCode.Rejected):
                self.window._show_track_details(track.id)

            dialog = captured[0]
            try:
                self.assertEqual(dialog._content_lyrics, [("my song.lrc", str(lrc.resolve()))])
                self.assertFalse(dialog.content_lyrics_button.isHidden())
                self.assertTrue(dialog._apply_lyrics_from_path(str(lrc)))
                self.assertEqual(dialog.selected_lyrics[0]["text"], "From content")
                self.assertTrue(dialog.selected_lyrics_path.endswith("my song.lrc"))
            finally:
                dialog.close()

            # An empty library hides the button.
            self.window.project_content_service.replace([])
            empty_dialog = TrackDetailsDialog(track, self.window.translator, self.window)
            try:
                self.assertTrue(empty_dialog.content_lyrics_button.isHidden())
            finally:
                empty_dialog.close()

    def test_track_video_tab_explains_scope_and_supports_sequence_reordering(self) -> None:
        track = PlaylistTrack(
            "track.wav", "Track", duration_seconds=10.0,
            video_paths=["first.mp4", "second.mp4"],
        )
        dialog = TrackDetailsDialog(track, self.window.translator, self.window)
        try:
            self.assertEqual(dialog.tabs.tabText(3), "이 곡의 영상")
            self.assertIn("이 곡이 재생되는 동안만", dialog.video_scope_badge.text())
            self.assertEqual(dialog.video_count_label.text(), "2개 영상")
            dialog.video_list.setCurrentRow(1)
            dialog._move_track_video(-1)
            self.assertEqual(dialog.selected_video_paths, ["second.mp4", "first.mp4"])
            self.assertTrue(dialog.video_down_button.isEnabled())
        finally:
            dialog.close()

    def test_track_details_analyzes_one_track_without_automix_and_shows_each_step(self) -> None:
        from app.automix.models import TrackAnalysis

        self.window.project_settings = replace(self.window.project_settings, transition_mode="none")
        track = PlaylistTrack(str(Path("analyze-me.wav").resolve()), "Analyze me", duration_seconds=200.0)
        self.window.playlist_service.replace([track])
        analysis = TrackAnalysis(track.id, track.file_path, 200.0, bpm=120.0, bpm_confidence=0.8, energy=0.4)
        controller = self.window.track_analysis_controller
        shown: list[TrackDetailsDialog] = []

        def press_analyze(dialog: TrackDetailsDialog) -> int:
            shown.append(dialog)
            dialog.analysis_panel.analyze_button.click()
            controller.track_step_changed.emit(track.id, "decode", 0.0)
            controller.track_step_changed.emit("another-track", "vocals", 0.8)  # not this dialog's
            self.assertEqual(dialog.analysis_panel.step_states["decode"], "running")
            self.assertEqual(dialog.analysis_panel.step_states["vocals"], "pending")
            controller.analyses_updated.emit({track.id: analysis})
            controller.running_changed.emit(False)
            return QDialog.DialogCode.Rejected

        with (
            patch("app.ui.main_window.FFmpegRenderer") as renderer,
            patch.object(controller, "start") as start,
            patch.object(TrackDetailsDialog, "exec", press_analyze),
        ):
            renderer.return_value.executable = Path("ffmpeg.exe")
            self.window._show_track_details(track.id)
        (tracks, executable), options = start.call_args
        self.assertEqual([entry.id for entry in tracks], [track.id])
        self.assertEqual(options, {"provider_id": "auto", "enable_structure_analysis": True})
        panel = shown[0].analysis_panel
        self.assertEqual(panel.step_states["decode"], "done")
        self.assertFalse(panel.running)
        self.assertEqual(panel.cards[0][1].text(), "120.0 BPM")
        self.assertIn("120 BPM", shown[0].header_facts.text())
        self.assertIs(self.window.automix_analyses[track.id], analysis)  # reaches the Playlist badges
        self.assertNotIn(self.window.TRACK_ANALYSIS_ACTIVITY, self.window.activity_progress.active_keys)

    def test_empty_playlist_offers_the_add_action_and_rows_hide_placeholder_metadata(self) -> None:
        editor = self.window.playlist_editor
        self.window.playlist_service.replace([])
        self.assertFalse(editor.empty_state.isHidden())
        self.assertFalse(editor.empty_add_button.isHidden())
        requested: list[bool] = []
        editor.request_files.disconnect(self.window._choose_audio_files)  # no real file dialog
        self.addCleanup(editor.request_files.connect, self.window._choose_audio_files)
        editor.request_files.connect(lambda: requested.append(True))
        editor.empty_add_button.click()
        self.assertEqual(requested, [True])
        track = PlaylistTrack("C:/music/song.mp3", "Song", duration_seconds=10.0)  # Unknown Artist/Album
        self.window.playlist_service.replace([track])
        self.assertTrue(editor.empty_state.isHidden())
        row = editor.list_widget.itemWidget(editor.list_widget.item(0))
        from PySide6.QtWidgets import QLabel

        text = row.findChild(QLabel, "trackMetadata").text()
        self.assertNotIn("Unknown", text)
        self.assertIn("song.mp3", text)
        editor.search_edit.setText("zzz")
        editor.refresh()
        self.assertFalse(editor.empty_state.isHidden())
        self.assertTrue(editor.empty_add_button.isHidden())  # "no results" is not "add music"

    def test_playlist_exposes_track_lyrics_settings_button(self) -> None:
        track = PlaylistTrack("track.wav", "Track", duration_seconds=20.0)
        self.window.playlist_service.replace([track])
        self.application.processEvents()
        item = self.window.playlist_editor.list_widget.item(0)
        item.setSelected(True)
        self.window.playlist_editor.list_widget.setCurrentItem(item)
        self.application.processEvents()
        requested: list[str] = []
        signal = self.window.playlist_editor.track_double_clicked
        signal.disconnect(self.window._show_track_details)
        try:
            signal.connect(requested.append)
            self.window.playlist_editor.details_button.click()
        finally:
            signal.disconnect(requested.append)
            signal.connect(self.window._show_track_details)
        self.assertEqual(requested, [track.id])

    def test_playlist_rows_have_no_checkbox_and_show_excluded_state(self) -> None:
        from PySide6.QtWidgets import QCheckBox

        from app.widgets.playlist_editor import TrackRow

        on = PlaylistTrack("a.wav", "Kept", duration_seconds=10.0)
        off = PlaylistTrack("b.wav", "Dropped", duration_seconds=10.0, enabled=False)
        self.window.playlist_service.replace([on, off])
        self.application.processEvents()
        editor = self.window.playlist_editor
        rows = [
            editor.list_widget.itemWidget(editor.list_widget.item(index))
            for index in range(editor.list_widget.count())
        ]
        from PySide6.QtWidgets import QWidget

        for row in rows:
            self.assertIsInstance(row, TrackRow)
            self.assertEqual(row.findChildren(QCheckBox), [])
        self.assertFalse(rows[0].property("trackDisabled"))
        self.assertTrue(rows[1].property("trackDisabled"))
        # An excluded row must not disable any child widget: a disabled label
        # swallows the right-click and the include/exclude menu never opens.
        self.assertTrue(all(
            child.isEnabled() for child in rows[1].findChildren(QWidget)
        ))

    def _m3u8_import(self, answer: QDialog.DialogCode) -> tuple[list[M3uImportDialog], Path]:
        """Drop a two-song M3U8 (plus one missing entry) and answer its review window."""
        directory = TemporaryDirectory(prefix="pc-m3u8-import-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        songs = [root / "one.mp3", root / "two.flac"]
        for song in songs:
            song.write_bytes(b"ID3 stub")
        playlist = root / "mix.m3u8"
        playlist.write_text("#EXTM3U\none.mp3\ntwo.flac\nmissing.mp3\n", encoding="utf-8")
        candidates = [
            AudioImportCandidate(PlaylistTrack(str(song.resolve()), song.stem, "Artist", "Album", 30.0))
            for song in songs
        ]
        shown: list[M3uImportDialog] = []

        def answer_review(dialog: M3uImportDialog) -> int:
            shown.append(dialog)
            return answer

        with (
            patch.object(self.window.playlist_service, "inspect_files", return_value=candidates) as inspect,
            patch.object(M3uImportDialog, "exec", answer_review),
        ):
            self.window._handle_dropped_files([str(playlist)])
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(inspect.call_args.args, ([song.resolve() for song in songs],))
        self.assertEqual(set(inspect.call_args.kwargs), {"cancel_event", "progress"})
        return shown, playlist

    def test_m3u8_drop_shows_its_songs_then_adds_them_but_not_the_playlist_file(self) -> None:
        before = len(self.window.playlist_service.tracks)
        shown, playlist = self._m3u8_import(QDialog.DialogCode.Accepted)
        self.assertEqual(len(shown), 1)
        table = shown[0].table
        self.assertEqual(table.rowCount(), 2)
        self.assertFalse(table.item(0, 0).icon().isNull())  # album art, or the placeholder
        self.assertEqual(
            [table.item(0, column).text() for column in (1, 2, 3)], ["one", "Artist", "Album"],
        )
        self.assertEqual(shown[0].skipped, ["missing.mp3"])
        tracks = self.window.playlist_service.tracks
        self.assertEqual([track.title for track in tracks[before:]], ["one", "two"])
        content = {Path(item.path).name for item in self.window.project_content_service.items}
        self.assertTrue({"one.mp3", "two.flac"} <= content)
        self.assertNotIn(playlist.name, content)

    def test_cancelled_m3u8_review_adds_nothing(self) -> None:
        before = self.window.playlist_service.tracks
        content_before = self.window.project_content_service.items
        self._m3u8_import(QDialog.DialogCode.Rejected)
        self.assertEqual(self.window.playlist_service.tracks, before)
        self.assertEqual(self.window.project_content_service.items, content_before)

    def test_m3u8_export_writes_enabled_tracks_in_playlist_order(self) -> None:
        with TemporaryDirectory(prefix="pc-m3u8-export-") as raw:
            root = Path(raw)
            self.window.playlist_service.replace([
                PlaylistTrack(str(root / "a.mp3"), "A", "Artist", duration_seconds=10.0),
                PlaylistTrack(str(root / "b.mp3"), "B", enabled=False),
                PlaylistTrack(str(root / "c.mp3"), "C"),
            ])
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(root / "out"), "")):
                self.window._export_m3u_playlist()
            lines = (root / "out.m3u8").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            ["#EXTM3U", "#EXTINF:10,Artist - A", "a.mp3", "#EXTINF:-1,Unknown Artist - C", "c.mp3"],
        )


if __name__ == "__main__":
    unittest.main()
