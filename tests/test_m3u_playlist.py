"""M3U8 playlist reading and writing."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.models.playlist import PlaylistTrack
from app.services.m3u_playlist import M3uPlaylistError, export_m3u8_folder, read_m3u


class M3uPlaylistTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(prefix="pc-m3u-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.lists = self.root / "lists"
        self.lists.mkdir()
        self.songs = [self.music / "a.mp3", self.root / "b 곡.flac", self.music / "c.wav"]
        for song in self.songs:
            song.write_bytes(b"audio")

    def test_reads_relative_absolute_and_file_url_entries_in_order(self) -> None:
        a, b, c = self.songs
        playlist = self.lists / "mix.m3u8"
        playlist.write_text(
            "﻿#EXTM3U\n#EXTINF:10,Artist - A\n../music/a.mp3\n\n"
            f"{b}\r\n{c.as_uri()}\n"
            "https://radio.example/stream.mp3\n../music/missing.mp3\n../music/a.mp3.txt\n",
            encoding="utf-8",
        )
        entries = read_m3u(playlist)
        self.assertEqual(entries.audio_paths, [a.resolve(), b.resolve(), c.resolve()])
        self.assertEqual(
            entries.skipped,
            ["https://radio.example/stream.mp3", "../music/missing.mp3", "../music/a.mp3.txt"],
        )

    def test_legacy_m3u_in_the_system_code_page_is_read(self) -> None:
        playlist = self.lists / "old.m3u"
        playlist.write_bytes(b"\xff\xfe\n../music/c.wav\n")  # not valid UTF-8
        self.assertEqual(read_m3u(playlist).audio_paths, [self.songs[2].resolve()])

    def test_missing_playlist_file_raises(self) -> None:
        with self.assertRaises(M3uPlaylistError):
            read_m3u(self.lists / "nope.m3u8")

    def test_bundle_still_plays_after_moving_it_and_removing_originals(self) -> None:
        a, b, c = self.songs
        another = self.root / "a.mp3"
        another.write_bytes(b"different audio")
        tracks = [
            PlaylistTrack(str(a), "Song A", "Artist", "Album", 61.4),
            PlaylistTrack(str(b), "Song B", "", duration_seconds=0.0),
            PlaylistTrack(str(c), "Disabled", enabled=False),
            PlaylistTrack(str(another), "Same filename"),
            PlaylistTrack(str(a), "Repeated track"),
        ]
        occupied = self.lists / "out"
        occupied.mkdir()
        (occupied / "keep").touch()
        playlist, count = export_m3u8_folder(tracks, self.lists, "out")
        self.assertEqual(count, 4)
        self.assertEqual(playlist.parent.name, "out (2)")
        self.assertTrue((occupied / "keep").is_file())
        lines = playlist.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[1], "#EXTINF:61,Artist - Song A")
        self.assertEqual(lines[2::2], [
            "music/a.mp3", "music/b 곡.flac", "music/a (2).mp3", "music/a.mp3",
        ])
        for source in (a, b, another):
            source.unlink()
        moved = playlist.parent.rename(self.root / "moved")
        imported = read_m3u(moved / playlist.name)
        self.assertEqual(imported.skipped, [])
        self.assertEqual([entry.read_bytes() for entry in imported.audio_paths],
                         [b"audio", b"audio", b"different audio", b"audio"])

    def test_missing_audio_does_not_leave_an_incomplete_bundle(self) -> None:
        tracks = [PlaylistTrack(str(self.songs[0]), "Valid"),
                  PlaylistTrack(str(self.root / "missing.mp3"), "Missing")]
        with self.assertRaisesRegex(M3uPlaylistError, "Missing"):
            export_m3u8_folder(tracks, self.lists, "out")
        self.assertEqual(list(self.lists.iterdir()), [])

    def test_failed_audio_copy_does_not_leave_partial_output(self) -> None:
        def fail_after_writing(_source: Path, destination: Path) -> None:
            destination.write_bytes(b"partial")
            raise OSError("Disk full")

        with patch("app.services.m3u_playlist.shutil.copyfile", side_effect=fail_after_writing):
            with self.assertRaisesRegex(M3uPlaylistError, "Disk full"):
                export_m3u8_folder([PlaylistTrack(str(self.songs[0]), "Track")], self.lists, "out")
        self.assertEqual(list(self.lists.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
