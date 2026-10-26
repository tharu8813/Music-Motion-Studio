"""Preview prefetch keeps one native worker through repeated frame requests."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from app.dialogs.export_preview_dialog import ExportPreviewDialog, OverlayFrameWorker
from app.renderer.ffmpeg_renderer import VisualizerOverlay
from app.preview.overlay_frame_cache import OverlayFrameCache


class PreviewHarness(QWidget):
    _request_overlay_frames = ExportPreviewDialog._request_overlay_frames
    _overlay_worker_finished = ExportPreviewDialog._overlay_worker_finished

    def __init__(self):
        super().__init__()
        self._closing = False
        self._overlay_worker = None
        self._overlay_generation = 0
        self._overlay_prefetch_count = 2
        self._overlay_cache = OverlayFrameCache()
        self.preview_fps = 30
        self.frames = []
        self.errors = []

    def _schedule_refresh(self):
        pass

    def _store_overlay_frames(self, track, fps, generation, signature, frames):
        self.frames.append((track, generation, signature, frames))

    def _preview_worker_failed(self, message):
        self.errors.append(message)


class PreviewWorkerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.preview = PreviewHarness()
        self.analysis = {"levels": np.ones((1, 32), dtype=np.float32),
                         "waveform": np.ones((1, 32), dtype=np.float32)}
        self.overlays = (VisualizerOverlay(0, 0, 64, 40, "bars", "#FFFFFF"),)

    def tearDown(self):
        worker = self.preview._overlay_worker
        if worker is not None:
            worker.cancel()
            self.assertTrue(worker.wait(2000))
        self.app.processEvents()
        self.preview.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_prefetch_reuses_worker_and_waits_for_finished_delivery(self):
        original = None
        for generation in range(80):
            self.preview._overlay_generation = generation
            self.preview._request_overlay_frames("track", generation * 2, self.overlays, (0,), self.analysis)
            worker = self.preview._overlay_worker
            original = original or worker
            self.assertIs(worker, original)
            self.assertTrue(worker.wait(2000))
            # isRunning() is already false, but finished still awaits the GUI:
            # neither replace the QObject nor mutate the completed request yet.
            self.preview._request_overlay_frames("other-track", 0, self.overlays, (0,), self.analysis)
            self.assertIs(self.preview._overlay_worker, original)
            self.assertEqual(worker.track_id, "track")
            self.app.processEvents()
            self.assertFalse(worker.busy)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(len(self.preview.findChildren(OverlayFrameWorker)), 1)
        self.assertEqual(len(self.preview.frames), 80)
        self.assertFalse(self.preview.errors)
        for generation, (_, actual, signature, frames) in enumerate(self.preview.frames):
            self.assertEqual(actual, generation)
            self.assertEqual(signature, (0,))
            self.assertEqual(len(frames), 2)
            self.assertFalse(frames[0][1][0].isNull())

    def test_closing_during_prefetch_discards_worker_without_restart(self):
        self.preview._request_overlay_frames("track", 0, self.overlays, (0,), self.analysis)
        worker = self.preview._overlay_worker
        self.preview._closing = True
        worker.cancel()
        ExportPreviewDialog._finish_or_detach_worker(worker)
        self.assertTrue(worker.wait(2000))
        self.app.processEvents()
        self.assertIsNone(self.preview._overlay_worker)
        self.preview._request_overlay_frames("track", 2, self.overlays, (0,), self.analysis)
        self.assertIsNone(self.preview._overlay_worker)


if __name__ == "__main__":
    unittest.main()
