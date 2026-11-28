from concurrent.futures import CancelledError
from pathlib import Path
import os
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

import numpy as np
import soundfile

from app.services.audio_decode import AudioDecodeError, _decode_windows, read_pcm


class AudioDecodeTests(TestCase):
    def test_m4a_edit_duration_caps_presentation_window_without_second_offset(self):
        rate = 44_100
        decoder = Mock(return_value=(rate, 2, 133_120, b""))
        native = SimpleNamespace(decode_windows_audio=decoder)
        source = Path("audio.m4a")
        with patch.dict(sys.modules, {"music_motion_native": native}), \
                patch("app.services.audio_decode.m4a_audio_edit", return_value=(1024 / rate, 3.0)):
            for start, requested, bounded in ((0.0, None, 3.0), (1.2, 0.8, 0.8),
                                              (2.75, 0.5, 0.25), (3.5, None, 0.0),
                                              (0.0, 0.0, 0.0)):
                with self.subTest(start=start, requested=requested):
                    decoder.reset_mock()
                    _, _, frames, _ = _decode_windows(source, start, requested)
                    decoder.assert_called_once_with(source.resolve(), start, bounded, None)
                    self.assertEqual(frames, 132_300)

    def test_reads_only_requested_frames_and_converts_channels(self):
        rate = 8_000
        original = np.tile([[0.5, 0.25]], (rate, 1)).astype(np.float32)
        original[:rate // 2] = -0.5
        with TemporaryDirectory() as directory:
            source = Path(directory) / "음원.wav"
            soundfile.write(source, original, rate)
            pcm = read_pcm(
                source, Event(), sample_rate=rate, channels=1, start=0.75, duration=1.0,
            )
            end = read_pcm(source, Event(), sample_rate=rate, channels=2, start=2.0)

        self.assertEqual(pcm.shape, (2_000, 1))
        self.assertEqual(pcm.dtype, np.float32)
        np.testing.assert_array_equal(pcm, 0.375)
        self.assertEqual(end.shape, (0, 2))

    def test_resamples_long_mono_input_and_preserves_stereo_mix_level(self):
        rate = 48_000
        with TemporaryDirectory() as directory:
            source = Path(directory) / "mono.flac"
            soundfile.write(source, np.full(rate * 2, 0.25, dtype=np.float32), rate)
            pcm = read_pcm(source, Event(), sample_rate=8_000, channels=2)

        self.assertEqual(pcm.shape, (16_000, 2))
        np.testing.assert_array_equal(pcm[:, 0], pcm[:, 1])
        np.testing.assert_allclose(pcm[200:-200], 0.25 / np.sqrt(2), atol=1e-5)

    def test_reports_corrupt_files_and_cancellation_separately(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "damaged.wav"
            source.write_bytes(b"not PCM")
            cancel = Event()
            with self.assertRaises(AudioDecodeError):
                read_pcm(source, cancel, sample_rate=8_000, channels=1)
            cancel.set()
            with self.assertRaises(CancelledError):
                read_pcm(source, cancel, sample_rate=8_000, channels=1)


def fixture_ffmpeg():
    return os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")


@skipUnless(sys.platform == "win32" and fixture_ffmpeg(),
            "Requires Windows audio decoding and FFmpeg for test fixture generation.")
class WindowsAudioDecodeTests(TestCase):
    def test_aac_and_m4a_keep_the_timeline_through_local_analysis_and_eq(self):
        from scipy.signal import chirp

        from app.automix.analysis.basic import BasicAnalysisProvider
        from app.models.playlist import PlaylistTrack
        from app.preview.audio_analysis_worker import AudioAnalysisWorker
        from app.renderer.python_visualizer import PythonVisualizerRenderer
        from app.services.eq_preview import EqPreviewAudio
        from app.services.lyrics_alignment_service import _extract_recognition_audio

        rate = 44_100
        cancel = Event()
        time = np.arange(rate * 3) / rate
        signal = np.column_stack((0.15 * chirp(time, 100, 3, 2500), 0.2 * chirp(time, 2200, 3, 250)))
        ffmpeg = fixture_ffmpeg()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / "source.wav"
            soundfile.write(wav, signal, rate)
            for extension in ("aac", "m4a"):
                with self.subTest(extension=extension):
                    source = root / f"음원.{extension}"
                    subprocess.run([ffmpeg, "-v", "error", "-i", str(wav), "-c:a", "aac", str(source)],
                                   check=True, capture_output=True)
                    reference = subprocess.check_output(
                        [ffmpeg, "-v", "error", "-i", str(source), "-f", "f32le", "-"],
                    )
                    expected = np.frombuffer(reference, dtype="<f4").reshape(-1, 2)
                    with patch("subprocess.Popen", side_effect=AssertionError("unexpected external decoder")):
                        full = read_pcm(source, cancel, sample_rate=rate, channels=2)
                        window = read_pcm(source, cancel, sample_rate=rate, channels=2, start=1.2, duration=0.8)
                        track = PlaylistTrack(str(source), "Tone", duration_seconds=3.0)
                        analysis = AudioAnalysisWorker(PythonVisualizerRenderer(None), track, 24)._analyze_audio()
                        mono = BasicAnalysisProvider(None)._decode_mono_pcm(source, cancel)
                        lyrics = root / "recognition.wav"
                        _extract_recognition_audio(source, lyrics, 1.2, 0.8, None, cancel)
                        eq = root / "eq.flac"
                        EqPreviewAudio().render(source, eq, "volume=-3.00dB", 0, None, cancel)

                    start = round(1.2 * rate)
                    frames = len(signal) if extension == "m4a" else len(expected)
                    def common_mae(left, right):
                        count = min(len(left), len(right))
                        return float(np.mean(np.abs(left[:count] - right[:count]))) if count else float("inf")
                    full_error = common_mae(full, expected[:frames])
                    window_error = common_mae(window, expected[start:start + len(window)])
                    native_window_error = common_mae(window, full[start:start + len(window)])
                    if (len(full) != frames or window.shape != (35_280, 2)
                            or max(full_error, window_error, native_window_error) >= 0.002):
                        from scipy.signal import correlate
                        from app.video.duration_probe import m4a_audio_edit
                        # Search a bounded neighborhood for displacement relative
                        # to the reference and the local decoder's full output.
                        def best_alignment(reference, position, observed):
                            lo = max(0, position - rate // 4)
                            hi = min(len(reference), position + len(observed) + rate // 4)
                            if not len(observed) or hi - lo < len(observed):
                                return None
                            scores = correlate(reference[lo:hi, 0], observed[:, 0],
                                               mode="valid", method="fft")
                            index = lo + int(np.argmax(scores))
                            aligned = reference[index:index + len(observed)]
                            return index - position, float(np.mean(np.abs(aligned - observed)))
                        edit = m4a_audio_edit(source) if extension == "m4a" else None
                        print(f"PCM alignment {extension}: edit={edit}, "
                              f"frames={len(full)}/{frames}, reference_frames={len(expected)}, "
                              f"window_frames={len(window)}, "
                              f"full_mae={full_error:.6f}, window_mae={window_error:.6f}, "
                              f"window_vs_full_mae={native_window_error:.6f}, "
                              f"reference_shift={best_alignment(expected, start, window)}, "
                              f"native_shift={best_alignment(full, start, window)}", flush=True)
                    self.assertEqual(window.shape, (35_280, 2))
                    self.assertEqual(len(full), frames)
                    self.assertLess(full_error, 0.002)
                    self.assertLess(window_error, 0.002)
                    self.assertLess(native_window_error, 0.002)
                    if extension == "m4a":
                        for seek_start, seconds in ((0.0, 0.15), (0.25, 0.1),
                                                    (0.75, 0.2), (2.9, 0.2)):
                            with self.subTest(seek=seek_start, duration=seconds), \
                                    patch("subprocess.Popen", side_effect=AssertionError("unexpected external decoder")):
                                part = read_pcm(source, cancel, sample_rate=rate, channels=2,
                                                start=seek_start, duration=seconds)
                                first = round(seek_start * rate)
                                stop = min(frames, round((seek_start + seconds) * rate))
                                self.assertEqual(part.shape, (stop - first, 2))
                                self.assertLess(common_mae(part, full[first:stop]), 0.002)
                                self.assertLess(common_mae(part, expected[first:stop]), 0.002)
                    self.assertGreater(analysis["levels"].max(), 0.0)
                    self.assertGreater(len(mono), 65_000)
                    self.assertEqual(soundfile.info(lyrics).frames, 12_800)
                    self.assertEqual(soundfile.info(eq).frames, frames)
