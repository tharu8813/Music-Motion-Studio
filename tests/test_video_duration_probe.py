"""Video container probes must not require external subprocesses for MP4."""

import struct
import tempfile
import unittest
from pathlib import Path

from app.video.duration_probe import mp4_container_duration
from app.dialogs.export_preview_dialog import VideoDurationProbeWorker
from app.services.playlist_service import PlaylistService
from unittest.mock import patch


def atom(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), name) + payload


class VideoContainerDurationTests(unittest.TestCase):
    def test_fast_mp4_probe_reads_video_only_movie_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "silent-video.mp4"
            version_zero = (
                bytes([0, 0, 0, 0]) + bytes(8)
                + struct.pack(">II", 1000, 2250) + bytes(48)
            )
            path.write_bytes(
                atom(b"ftyp", b"isom" + bytes(8))
                + atom(b"mdat", bytes(40))
                + atom(b"moov", atom(b"mvhd", version_zero))
            )
            self.assertEqual(mp4_container_duration(path), 2.25)
            worker = VideoDurationProbeWorker(str(path))
            events = []
            worker.ready.connect(lambda _name, seconds: events.append(seconds))
            with patch.object(PlaylistService, "_probe_duration", side_effect=AssertionError("Must not run FFprobe")):
                worker.run()
            self.assertEqual(events, [2.25])

    def test_version_one_and_extended_size_movie_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large-time.mov"
            mvhd = bytes([1, 0, 0, 0]) + bytes(16) + struct.pack(">IQ", 1000, 3100)
            inner = atom(b"mvhd", mvhd)
            outer = struct.pack(">I4sQ", 1, b"moov", 16 + len(inner)) + inner
            path.write_bytes(outer)
            self.assertEqual(mp4_container_duration(path), 3.1)

    def test_missing_metadata_and_other_extensions_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            invalid = Path(tmp) / "broken.mp4"
            invalid.write_bytes(b"not a movie")
            self.assertIsNone(mp4_container_duration(invalid))
            with patch.object(PlaylistService, "_probe_duration", return_value=4.5) as probe:
                worker = VideoDurationProbeWorker(str(invalid))
                result = []
                worker.ready.connect(lambda _name, seconds: result.append(seconds))
                worker.run()
            self.assertEqual(result, [4.5])
            probe.assert_called_once_with(invalid)
            self.assertIsNone(mp4_container_duration(Path(tmp) / "track.wav"))


if __name__ == "__main__":
    unittest.main()
