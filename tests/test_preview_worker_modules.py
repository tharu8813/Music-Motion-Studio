"""Moved preview workers retain background safety and public import names."""

from __future__ import annotations

from unittest import TestCase
from unittest.mock import Mock, patch

from app.dialogs.export_preview_dialog import AudioAnalysisWorker, VideoDurationProbeWorker
from app.preview.audio_analysis_worker import AudioAnalysisWorker as IsolatedAudioWorker
from app.video.duration_probe_worker import VideoDurationProbeWorker as IsolatedVideoWorker


class PreviewWorkerModuleTests(TestCase):
    def test_existing_dialog_imports_reexport_identical_worker_types(self):
        self.assertIs(AudioAnalysisWorker, IsolatedAudioWorker)
        self.assertIs(VideoDurationProbeWorker, IsolatedVideoWorker)

    def test_cancel_audio_analysis_sets_shared_event(self):
        renderer = Mock()
        track = Mock(id="test", file_path="not-read-in-this-test.wav")
        worker = AudioAnalysisWorker(renderer, track, 10)
        self.assertFalse(worker.cancel_event.is_set())
        worker.cancel()
        self.assertTrue(worker.cancel_event.is_set())

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
