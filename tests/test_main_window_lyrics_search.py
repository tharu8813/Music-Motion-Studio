from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QDialog

from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.services.lyrics_search_service import FoundLyrics, LyricsSearchService
from app.services.lyrics_service import LyricsService
from app.services.project_service import ProjectService
from tests.main_window_base import MainWindowTestCase
from tests.test_lyrics_search_service import lrc_result


class MainWindowLyricsSearchTests(MainWindowTestCase):
    def test_tools_action_opens_search_with_current_playlist(self):
        track = PlaylistTrack("", "Song", artist="Artist")
        self.window.playlist_service.replace([track])
        with patch("app.ui.main_window.LyricsSearchDialog") as dialog_type:
            self.window.lyrics_search_action.trigger()
            dialog_type.assert_called_once()
            self.assertEqual(dialog_type.call_args.args[0], [track])
            self.assertEqual(dialog_type.call_args.kwargs["on_attach"], self.window._attach_found_lyrics)
            dialog_type.return_value.exec.assert_called_once()

    def test_attached_lyrics_are_embedded_and_project_saves_without_a_sidecar(self):
        track = PlaylistTrack("", "Song", artist="Artist")
        self.window.playlist_service.replace([track])
        document = LyricsSearchService().fetch(lrc_result())
        self.assertTrue(self.window._attach_found_lyrics(document, track.id))
        self.assertEqual(track.lyrics, document.cues)
        self.assertEqual(track.lyrics_path, "")
        self.assertTrue(any(source.source_type is SourceType.LYRICS for source in self.window.store.sources()))
        with TemporaryDirectory() as directory:
            path = ProjectService.save(Path(directory) / "found.json", self.window._project_document())
            self.assertEqual(ProjectService.load(path).playlist[0].lyrics, document.cues)

    def test_keep_existing_lyrics_does_not_mutate_track(self):
        track = PlaylistTrack("", "Song", lyrics_path="existing.lrc", lyrics=[{"start": 0, "end": 2, "text": "Existing"}])
        self.window.playlist_service.replace([track])
        before = track.to_dict()
        comparison = MagicMock()
        comparison.exec.return_value = QDialog.DialogCode.Rejected
        comparison.replace_requested = False
        with patch("app.ui.main_window.LyricsCompareDialog", return_value=comparison):
            self.assertFalse(self.window._attach_found_lyrics(LyricsSearchService().fetch(lrc_result()), track.id))
        self.assertEqual(track.to_dict(), before)

    def test_ttml_attachment_preserves_common_display_settings(self):
        content = '<tt xmlns="http://www.w3.org/ns/ttml"><body><p begin="1s" end="4s">Hello</p></body></tt>'
        document = FoundLyrics(lrc_result(), "ttml", content, LyricsService.parse(content, ".ttml"))
        for existing_element in (False, True):
            with self.subTest(existing_element=existing_element):
                source = Source(SourceType.LYRICS, "Lyrics", subtitle_accent_color="#22AAFF", subtitle_karaoke_lines=[1])
                self.window.store.replace([source] if existing_element else [])
                before = source.to_dict()
                track = PlaylistTrack("", "Song")
                self.window.playlist_service.replace([track])
                self.assertTrue(self.window._attach_found_lyrics(document, track.id))
                lyric_sources = [entry for entry in self.window.store.sources() if entry.source_type is SourceType.LYRICS]
                self.assertEqual(len(lyric_sources), 1)
                self.assertFalse(lyric_sources[0].subtitle_karaoke_enabled)
                if existing_element:
                    self.assertEqual(source.to_dict(), before)
                self.assertEqual(track.lyrics, document.cues)

    def test_stale_target_cannot_attach_to_another_track(self):
        track = PlaylistTrack("", "Song")
        self.window.playlist_service.replace([track])
        with patch("app.ui.main_window.QMessageBox.warning"):
            self.assertFalse(self.window._attach_found_lyrics(LyricsSearchService().fetch(lrc_result()), "removed-track"))
        self.assertEqual(track.lyrics, [])
