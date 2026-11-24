"""Regression coverage for sequential playlists with no requested gaps."""
from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
import wave

import numpy as np

from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer, RenderCancelledError, RenderSettings, uses_gapless_playlist,
)


class GaplessPCMTests(unittest.TestCase):
    def test_join_preserves_sample_order_and_exact_lengths(self):
        with TemporaryDirectory() as directory:
            paths = [Path(directory) / f"part{i}.f32" for i in range(2)]
            payloads = [b"\x01" * (8 * 1001), b"\x02" * (8 * 2003)]
            for path, payload in zip(paths, payloads, strict=True):
                path.write_bytes(payload)
            result, durations = FFmpegRenderer._join_gapless_pcm(paths, Event())
            self.assertEqual(result.read_bytes(), b"".join(payloads))
            self.assertEqual(durations, [1001 / 48000, 2003 / 48000])
            self.assertFalse(paths[1].exists())

    def test_explicit_starts_keep_legacy_timeline(self):
        tracks = [PlaylistTrack("a.mp3", "A", duration_seconds=1),
                  PlaylistTrack("b.mp3", "B", duration_seconds=1)]
        self.assertTrue(uses_gapless_playlist(tracks))
        tracks[1].start_time_seconds = 2
        self.assertFalse(uses_gapless_playlist(tracks))

    def test_cancelled_join_stops(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "a.f32"
            path.write_bytes(b"\x00" * 8)
            cancel = Event()
            cancel.set()
            with self.assertRaises(RenderCancelledError):
                FFmpegRenderer._join_gapless_pcm([path], cancel)


FFMPEG = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG", "").strip() or shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "FFmpeg is required for sample-accurate integration checks.")
class GaplessFFmpegTests(unittest.TestCase):
    def test_no_padding_or_truncation_and_plan_matches_audio(self):
        renderer = FFmpegRenderer(FFMPEG)

        def ff(*args):
            return subprocess.run(
                [str(FFMPEG), "-v", "error", "-y", *map(str, args)],
                capture_output=True, check=True, timeout=30,
            ).stdout

        with TemporaryDirectory() as directory:
            root = Path(directory)
            counts = [48048, 47952]
            cursor = 0
            for index, count in enumerate(counts):
                samples = (np.sin((np.arange(count) + cursor) * 2 * math.pi * 440 / 48000)
                           * 9000).astype("<i2")
                with wave.open(str(root / f"{index}.wav"), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(48000)
                    wav.writeframes(samples.tobytes())
                cursor += count

            for ext in ("wav", "flac", "mp3", "m4a"):
                with self.subTest(ext=ext):
                    if ext == "m4a" and not (Path(str(FFMPEG)).with_name(
                        "ffprobe.exe" if str(FFMPEG).lower().endswith(".exe") else "ffprobe"
                    ).is_file() or shutil.which("ffprobe")):
                        continue
                    tracks, parts = [], []
                    work = root / ext
                    work.mkdir()
                    for index, count in enumerate(counts):
                        source = root / f"{index}.wav"
                        encoded = work / f"{index}.{ext}"
                        if ext == "wav":
                            shutil.copyfile(source, encoded)
                        else:
                            ff("-i", source, encoded)
                        # Deliberately inaccurate stored durations in both directions.
                        tracks.append(PlaylistTrack(str(encoded), str(index),
                                                   duration_seconds=count / 48000 + (0.03 if index == 0 else -0.02)))
                        trim = ["-af", f"atrim=end_sample={count}"] if ext == "m4a" else []
                        pcm = ff("-i", encoded, *trim, "-ar", "48000", "-ac", "2",
                                 "-f", "f32le", "-")
                        parts.append(np.frombuffer(pcm, dtype="<f4").reshape(-1, 2))
                    plans = []
                    with patch.object(renderer, "_measure_loudness_filter", return_value=None):
                        audio = renderer.prepare_playlist_audio(
                            tracks, work, RenderSettings(), audio_codec="flac",
                            plan_callback=plans.append,
                        )
                    actual = np.frombuffer(ff("-i", audio, "-f", "f32le", "-"),
                                           dtype="<f4").reshape(-1, 2)
                    expected = np.concatenate(parts)
                    self.assertEqual(actual.shape, expected.shape)
                    # FLAC converts float input to integer; allow its quantization only.
                    np.testing.assert_allclose(actual, expected, atol=1 / 32768, rtol=0)
                    self.assertAlmostEqual(plans[0].audio.clips[1].timeline_start,
                                           len(parts[0]) / 48000, places=10)
                    self.assertAlmostEqual(plans[0].duration_seconds, len(expected) / 48000, places=10)
                    self.assertAlmostEqual(tracks[0].duration_seconds, counts[0] / 48000 + 0.03)


if __name__ == "__main__":
    unittest.main()
