"""Moved preview workers retain background safety and public import names."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import numpy as np
import soundfile

from app.dialogs.export_preview_dialog import AudioAnalysisWorker, VideoDurationProbeWorker
from app.dialogs.export_preview_dialog import OverlayFrameWorker
from app.dialogs.export_preview_dialog import PreviewCoverWorker
from app.models.playlist import PlaylistTrack
from app.preview.cover_worker import PreviewCoverWorker as IsolatedPreviewCoverWorker
from app.preview.overlay_frame_worker import OverlayFrameWorker as IsolatedOverlayFrameWorker
from app.preview.audio_analysis_worker import AudioAnalysisWorker as IsolatedAudioWorker
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.video.duration_probe_worker import VideoDurationProbeWorker as IsolatedVideoWorker


class PreviewWorkerModuleTests(TestCase):
    def test_existing_dialog_imports_reexport_identical_worker_types(self):
        self.assertIs(AudioAnalysisWorker, IsolatedAudioWorker)
        self.assertIs(VideoDurationProbeWorker, IsolatedVideoWorker)
        self.assertIs(PreviewCoverWorker, IsolatedPreviewCoverWorker)

    def test_overlay_worker_reexports_same_class_and_cancel_is_nonblocking(self):
        self.assertIs(OverlayFrameWorker, IsolatedOverlayFrameWorker)
        self.assertEqual(OverlayFrameWorker.__module__, "app.preview.overlay_frame_worker")
        overlay = Mock(kind="waveform", width=4, height=4)
        analysis = {"levels": [[0.0]], "waveform": [[0.0]]}
        worker = OverlayFrameWorker(
            "track", 30, 5, 0, 1, (overlay,), (0,), analysis,
        )
        ready = []
        worker.ready.connect(lambda *_args: ready.append(True))
        worker.cancel()
        self.assertTrue(worker.cancel_event.is_set())
        worker.run()  # A cancelled batch must not emit late results.
        self.assertEqual(ready, [])

    def test_cancel_audio_analysis_sets_shared_event(self):
        renderer = Mock()
        track = Mock(id="test", file_path="not-read-in-this-test.wav")
        worker = AudioAnalysisWorker(renderer, track, 10)
        self.assertFalse(worker.cancel_event.is_set())
        worker.cancel()
        self.assertTrue(worker.cancel_event.is_set())

    def test_preview_builds_spectrum_waveform_and_stereo_meter_without_ffmpeg(self):
        renderer = PythonVisualizerRenderer(None)
        rate = renderer.sample_rate
        tone = 0.25 * np.sin(2 * np.pi * 400 * np.arange(rate) / rate)
        overlay = VisualizerOverlay(
            kind="level_meter", width=100, height=100, x=0, y=0, style="bars", color="#ffffff",
        )
        with TemporaryDirectory() as directory:
            source = Path(directory) / "stereo.wav"
            soundfile.write(source, np.column_stack((tone, tone / 4)), rate)
            worker = AudioAnalysisWorker(
                renderer, PlaylistTrack(str(source), "Tone"), 32,
                overlays=(overlay,), needs_waveform=True,
            )
            with patch("app.renderer.python_visualizer.subprocess.Popen") as process:
                result = worker._analyze_audio()
            process.assert_not_called()

        self.assertEqual(result["levels"].shape, (30, 32))
        self.assertGreater(float(np.max(result["levels"])), 0)
        self.assertEqual(result["waveform"].shape, (30, 32))
        self.assertGreater(float(np.max(np.abs(result["waveform"]))), 0)
        levels, _peaks = result["meters"][0]
        self.assertGreater(float(np.max(levels[:, 0])), float(np.max(levels[:, 1])))

    def test_preview_falls_back_to_ffmpeg_for_audio_the_local_decoder_rejects(self):
        renderer = PythonVisualizerRenderer(Path("ffmpeg"))
        source = Path("track.m4a")
        worker = AudioAnalysisWorker(renderer, PlaylistTrack(str(source), "Track"), 32)
        with patch.object(renderer, "_decode_mono_audio", return_value=np.zeros(800, dtype=np.float32)) as decode:
            result = worker._analyze_audio()
        decode.assert_called_once_with(source, worker.cancel_event)
        self.assertEqual(result["levels"].shape, (3, 32))

    def test_video_probe_runs_container_probe_before_ffprobe(self):
        worker = VideoDurationProbeWorker("video.mp4")
        received = []
        worker.ready.connect(lambda path, duration: received.append((path, duration)))
        with patch("app.video.duration_probe_worker.mp4_container_duration", return_value=2.75) as container, \
                patch("app.video.duration_probe_worker.PlaylistService._probe_duration") as slow:
            worker.run()
        self.assertEqual(received, [("video.mp4", 2.75)])
        container.assert_called_once()
        slow.assert_not_called()

    def test_video_probe_falls_back_to_ffprobe(self):
        worker = VideoDurationProbeWorker("incomplete.mp4")
        received = []
        worker.ready.connect(lambda path, duration: received.append((path, duration)))
        with patch("app.video.duration_probe_worker.mp4_container_duration", return_value=None), \
                patch("app.video.duration_probe_worker.PlaylistService._probe_duration", return_value=3.5):
            worker.run()
        self.assertEqual(received, [("incomplete.mp4", 3.5)])


if __name__ == "__main__":
    import unittest
    unittest.main()
