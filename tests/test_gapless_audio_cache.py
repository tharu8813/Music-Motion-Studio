"""Cache only completed, identical audio; preview ownership stays independent."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.playlist import PlaylistTrack
from app.preview.gapless_audio_cache import GaplessPreviewCache, gapless_preview_key
from app.timeline.compiler import compile_playlist


class GaplessPreviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.executable = self.root / "ffmpeg.exe"
        self.executable.write_bytes(b"binary")
        (self.root / "ffprobe.exe").write_bytes(b"probe")
        self.tracks = []
        for name in ("a.mp3", "b.mp3"):
            path = self.root / name
            path.write_bytes(name.encode())
            self.tracks.append(PlaylistTrack(str(path), name, duration_seconds=3))
        self.source = self.root / "original.flac"
        self.source.write_bytes(b"test continuous audio")
        self.cache = GaplessPreviewCache(max_bytes=100)
        self.addCleanup(self.cache.clear)
        self.plan = compile_playlist(self.tracks)

    def key(self, effects=None):
        return gapless_preview_key(self.executable, self.tracks, effects)

    def test_reuse_after_original_preview_closes_and_eviction(self):
        key = self.key()
        self.assertIsNotNone(key)
        self.cache.remember(key, self.source, self.plan)
        self.source.unlink()
        target = self.root / "next-preview.flac"
        self.assertIs(self.cache.restore(key, target), self.plan)
        self.cache.clear()
        self.assertEqual(target.read_bytes(), b"test continuous audio")

    def test_audio_edits_invalidate_but_visual_edits_do_not(self):
        key = self.key()
        self.cache.remember(key, self.source, self.plan)
        self.tracks[0].title = "new title"
        self.tracks[0].lyrics_path = "new lyrics"
        self.tracks[0].cover_path = "new cover"
        self.assertEqual(self.key(), key)
        self.tracks[0].volume_db = 2
        self.assertNotEqual(self.key(), key)
        self.tracks[0].volume_db = 0
        self.tracks.reverse()
        self.assertNotEqual(self.key(), key)
        self.tracks.reverse()
        self.assertNotEqual(self.key({"auto_pan": {"enabled": True}}), key)
        Path(self.tracks[0].file_path).write_bytes(b"changed file")
        self.assertNotEqual(self.key(), key)
        self.assertIsNone(self.cache.restore(self.key(), self.root / "miss.flac"))

    def test_size_limit_and_missing_inputs_are_cache_misses(self):
        small = GaplessPreviewCache(max_bytes=1)
        self.addCleanup(small.clear)
        key = self.key()
        small.remember(key, self.source, self.plan)
        self.assertIsNone(small.restore(key, self.root / "too-large.flac"))
        Path(self.tracks[0].file_path).unlink()
        self.assertIsNone(self.key())

    def test_cache_never_overwrites_existing_destination(self):
        key = self.key()
        self.cache.remember(key, self.source, self.plan)
        target = self.root / "already.flac"
        target.write_bytes(b"existing")
        self.assertIsNone(self.cache.restore(key, target))
        self.assertEqual(target.read_bytes(), b"existing")
