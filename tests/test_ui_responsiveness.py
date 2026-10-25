"""User-flow regressions: responsive imports, safe search and reusable rows."""

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
from unittest.mock import patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QProgressDialog, QMessageBox

from app.models.playlist import PlaylistTrack
from app.services.history_service import HistoryService
from app.services.playlist_service import AudioImportCandidate, PlaylistService
from tests.main_window_base import MainWindowTestCase


class UiResponsivenessTests(MainWindowTestCase):
    def test_slow_import_keeps_heartbeat_and_commits_on_gui_thread(self):
        gui_thread = threading.get_ident()
        inspected = []
        committed = []
        heartbeats = []
        importing = threading.Event()
        responsive = threading.Event()
        timer = QTimer()
        timer.setInterval(10)

        def heartbeat():
            if importing.is_set():
                heartbeats.append(monotonic())
                if len(heartbeats) >= 5:
                    responsive.set()

        timer.timeout.connect(heartbeat)
        self.window.playlist_service.playlist_changed.connect(
            lambda: committed.append(threading.get_ident()))
        track = PlaylistTrack("missing.wav", "Ready", duration_seconds=3)

        def inspect(_paths, **_options):
            inspected.append(threading.get_ident())
            importing.set()
            responsive.wait(3)
            importing.clear()
            return [AudioImportCandidate(track)]

        timer.start()
        try:
            with patch.object(self.window.playlist_service, "inspect_files", side_effect=inspect):
                added, _, _ = self.window._import_audio_files([track.file_path])
        finally:
            timer.stop()
        self.assertEqual(added, 1)
        self.assertNotEqual(inspected, [gui_thread])
        self.assertEqual(committed, [gui_thread])
        self.assertGreaterEqual(len(heartbeats), 5)
        self.assertIsNone(self.window._audio_import_worker)
        self.assertTrue(self.window.isEnabled())

    def test_cancel_discards_whole_import_and_restores_editor(self):
        started = threading.Event()
        observed_cancel = threading.Event()

        def inspect(_paths, *, cancel_event, progress):
            started.set()
            progress(0, 1, "slow.wav")
            cancel_event.wait(2)
            if cancel_event.is_set():
                observed_cancel.set()
            return [AudioImportCandidate(PlaylistTrack("slow.wav", "Cancelled"))]

        def cancel():
            if started.is_set():
                dialogs = self.window.findChildren(QProgressDialog)
                if dialogs:
                    # QProgressDialog.cancel() doesn't emit canceled(); Escape does.
                    dialogs[-1].show()
                    QTest.keyClick(dialogs[-1], Qt.Key.Key_Escape)

        timer = QTimer()
        timer.setInterval(50)
        timer.timeout.connect(cancel)
        timer.start()
        before = self.window.playlist_service.tracks
        try:
            with patch.object(self.window.playlist_service, "inspect_files", side_effect=inspect):
                result = self.window._import_audio_files(["slow.wav"])
        finally:
            timer.stop()
        self.assertTrue(observed_cancel.is_set())
        self.assertEqual(result, (0, [], []))
        self.assertEqual(self.window.playlist_service.tracks, before)
        self.assertTrue(self.window.isEnabled())
        self.assertNotIn("content_add", self.window.activity_progress.active_keys)

    def test_import_error_leaves_playlist_intact_and_allows_retry(self):
        before = self.window.playlist_service.tracks
        with patch.object(self.window.playlist_service, "inspect_files", side_effect=OSError("unreadable")), \
                patch.object(QMessageBox, "warning") as warning:
            self.assertEqual(self.window._import_audio_files(["bad.wav"]), (0, [], []))
        warning.assert_called_once()
        self.assertEqual(self.window.playlist_service.tracks, before)
        self.assertTrue(self.window.isEnabled())
        track = PlaylistTrack("good.wav", "Good")
        with patch.object(self.window.playlist_service, "inspect_files", return_value=[AudioImportCandidate(track)]):
            self.assertEqual(self.window._import_audio_files(["good.wav"])[0], 1)

    def test_probe_cancel_kills_and_reaps_child_process(self):
        cancel = threading.Event()
        popen = subprocess.Popen
        children = []

        def spawn(_command, **kwargs):
            child = popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
            children.append(child)
            cancel.set()
            return child

        with TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "ffprobe.exe").touch()
            with patch("app.services.playlist_service.FFmpegRenderer.find_executable",
                       return_value=root / "ffmpeg.exe"), \
                    patch("app.services.playlist_service.subprocess.Popen", side_effect=spawn):
                start = monotonic()
                self.assertEqual(PlaylistService._probe_duration(root / "song.wav", cancel_event=cancel), 0)
        self.assertLess(monotonic() - start, 2)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_search_and_append_reuse_rows_and_keep_visible_selection(self):
        tracks = [PlaylistTrack(f"{i}.wav", f"Song {i}") for i in range(20)]
        self.window.playlist_service.replace(tracks)
        editor = self.window.playlist_editor
        items = [editor.list_widget.item(i) for i in range(20)]
        rows = [editor.list_widget.itemWidget(item) for item in items]
        editor.list_widget.setCurrentItem(items[10])
        editor.search_edit.setText("Song 1")
        editor.refresh()
        self.assertEqual(editor._selected_ids(), [tracks[10].id])
        self.assertTrue(items[0].isHidden())
        self.assertFalse(items[10].isHidden())
        editor.search_edit.clear()
        editor.refresh()
        self.window.playlist_service.add_tracks([PlaylistTrack("new.wav", "New")])
        self.assertEqual(editor._selected_ids(), [tracks[10].id])
        for index, row in enumerate(rows):
            self.assertIs(editor.list_widget.itemWidget(editor.list_widget.item(index)), row)
        self.assertEqual(editor.list_widget.count(), 21)
        editor.list_widget.setCurrentItem(items[11])
        items[10].setSelected(True)
        self.window.playlist_service.reorder([track.id for track in reversed(self.window.playlist_service.tracks)])
        self.assertEqual(set(editor._selected_ids()), {tracks[10].id, tracks[11].id})

    def test_hidden_selection_is_not_a_keyboard_or_delete_target(self):
        track = PlaylistTrack("keep.wav", "Keep")
        self.window.playlist_service.replace([track])
        editor = self.window.playlist_editor
        editor.list_widget.setCurrentRow(0)
        editor.search_edit.setText("zzz")
        editor.refresh()
        self.assertEqual(editor._selected_ids(), [])
        requested = []
        editor.track_double_clicked.disconnect(self.window._show_track_details)
        editor.track_double_clicked.connect(requested.append)
        try:
            QTest.keyClick(editor.list_widget, Qt.Key.Key_Return)
        finally:
            editor.track_double_clicked.disconnect(requested.append)
            editor.track_double_clicked.connect(self.window._show_track_details)
        self.assertEqual(requested, [])
        editor.remove_selected()
        self.assertEqual(self.window.playlist_service.tracks, [track])

    def test_move_buttons_and_accessible_names_match_available_actions(self):
        self.window.playlist_service.replace([PlaylistTrack("a.wav", "A"), PlaylistTrack("b.wav", "B")])
        editor = self.window.playlist_editor
        editor.list_widget.setCurrentRow(0)
        self.assertFalse(editor.up_button.isEnabled())
        self.assertTrue(editor.down_button.isEnabled())
        editor.list_widget.setCurrentRow(1)
        self.assertTrue(editor.up_button.isEnabled())
        self.assertFalse(editor.down_button.isEnabled())
        self.assertEqual(editor.up_button.accessibleName(), "위로 이동")
        self.assertEqual(editor.search_edit.accessibleName(), "곡 검색")

    def test_content_refresh_reuses_items_icons_and_multiple_selection(self):
        with TemporaryDirectory() as raw:
            paths = [Path(raw) / f"{i}.wav" for i in range(3)]
            for path in paths:
                path.touch()
            self.window.project_content_service.add_paths(paths)
            panel = self.window.content_library_panel
            items = [panel.list.item(i) for i in range(3)]
            items[0].setSelected(True)
            items[1].setSelected(True)
            with patch.object(panel, "_content_item_icon", wraps=panel._content_item_icon) as icon:
                panel.refresh()
            icon.assert_not_called()
            self.assertEqual(len(panel._selected_ids()), 2)
            for index, item in enumerate(items):
                self.assertIs(panel.list.item(index), item)
            panel.list.setCurrentItem(items[1])
            items[0].setSelected(True)
            self.window.project_content_service.replace(reversed(self.window.project_content_service.items))
            self.assertEqual(len(panel._selected_ids()), 2)

    def test_sidecar_batch_scans_each_folder_once(self):
        settings = self.window.settings_service.current
        self.addCleanup(self.window.settings_service.save, settings)
        self.window.settings_service.save(replace(settings, lyrics_auto_attach_mode="always"))
        with TemporaryDirectory() as raw:
            folder = Path(raw)
            tracks = [PlaylistTrack(str(folder / f"song{i}.wav"), f"song{i}") for i in range(4)]
            for i in range(4):
                (folder / f"song{i}.lrc").write_text("[00:01.00]Words\n", encoding="utf-8")
            iterdir = Path.iterdir
            scans = []

            def scan(path):
                scans.append(path)
                return iterdir(path)

            with patch.object(Path, "iterdir", scan):
                notes = self.window._attach_sidecar_lyrics(tracks)
        self.assertEqual(scans, [folder])
        self.assertEqual(len(notes), 4)
        self.assertTrue(all(track.lyrics for track in tracks))

    def test_unchanged_history_skips_copy_and_changed_state_remains_isolated(self):
        history = HistoryService()
        snapshot = {"sources": [{"id": "one", "text": ["original"]}]}
        history.reset(snapshot)
        with patch.object(history, "_copy", wraps=history._copy) as copy:
            history.commit(snapshot)
        copy.assert_not_called()
        snapshot["sources"][0]["text"].append("change")
        history.commit(snapshot)
        snapshot["sources"][0]["text"].append("later")
        self.assertEqual(history.undo()["sources"][0]["text"], ["original"])
        self.assertEqual(history.redo()["sources"][0]["text"], ["original", "change"])


if __name__ == "__main__":
    import unittest
    unittest.main()
