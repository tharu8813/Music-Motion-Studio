from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication

from app.controllers.preview_audio_controller import (
    PreviewAudioController,
    _PreviewAudioWorker,
)
from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderCancelledError, RenderError


def _renderer() -> FFmpegRenderer:
    renderer = FFmpegRenderer.__new__(FFmpegRenderer)
    renderer.executable = Path("ffmpeg")
    return renderer


def _track(name: str) -> PlaylistTrack:
    return PlaylistTrack(file_path=name, title=name, duration_seconds=30.0)


class PreviewAudioWorkerTests(unittest.TestCase):
    def test_run_emits_ready_with_the_rendered_path(self) -> None:
        with TemporaryDirectory(prefix="preview-audio-") as directory:
            renderer = _renderer()
            worker = _PreviewAudioWorker(
                renderer, [_track("a.mp3")], Path(directory), "crossfade", 3.0,
            )
            with patch.object(renderer, "prepare_playlist_audio", return_value=Path(directory) / "out.m4a"):
                received: list[str] = []
                worker.ready.connect(lambda path, plan: received.append((path, plan)))
                worker.run()
            self.assertEqual(received[0][0], str(Path(directory) / "out.m4a"))
            self.assertEqual(received[0][1].duration_seconds, 30.0)

    def test_render_failure_emits_failed_not_an_exception(self) -> None:
        with TemporaryDirectory(prefix="preview-audio-") as directory:
            renderer = _renderer()
            worker = _PreviewAudioWorker(
                renderer, [_track("a.mp3")], Path(directory), "automix", 3.0,
            )
            with patch.object(renderer, "prepare_playlist_audio", side_effect=RenderError("boom")):
                failures: list[str] = []
                worker.failed.connect(failures.append)
                worker.run()  # must not raise
            self.assertEqual(failures, ["boom"])

    def test_cancellation_emits_neither_signal(self) -> None:
        with TemporaryDirectory(prefix="preview-audio-") as directory:
            renderer = _renderer()
            worker = _PreviewAudioWorker(
                renderer, [_track("a.mp3")], Path(directory), "automix", 3.0,
            )
            with patch.object(renderer, "prepare_playlist_audio", side_effect=RenderCancelledError("cancelled")):
                ready: list[str] = []
                failures: list[str] = []
                worker.ready.connect(lambda path, plan: ready.append((path, plan)))
                worker.failed.connect(failures.append)
                worker.run()
            self.assertEqual(ready, [])
            self.assertEqual(failures, [])


class PreviewAudioControllerTests(unittest.TestCase):
    def test_start_with_transition_mode_none_creates_no_worker(self) -> None:
        controller = PreviewAudioController(_renderer())
        controller.start([_track("a.mp3")], Path("."), "none", 3.0)
        self.assertIsNone(controller._worker)

    def test_sequential_playlist_without_effects_starts_lossless_mix(self) -> None:
        renderer = _renderer()
        controller = PreviewAudioController(renderer)
        tracks = [_track("a.mp3"), _track("b.mp3")]
        with TemporaryDirectory(prefix="gapless-preview-") as directory:
            target = Path(directory) / "playlist.flac"
            ready = []
            controller.audio_ready.connect(lambda path, plan: ready.append((path, plan)))
            with patch.object(_PreviewAudioWorker, "start", lambda worker: worker.run()), \
                 patch.object(renderer, "prepare_playlist_audio", return_value=target) as prepare:
                controller.start(tracks, Path(directory), "none", 3.0)
            self.assertEqual(len(ready), 1)
            self.assertEqual(ready[0][0], str(target))
            self.assertEqual(ready[0][1].duration_seconds, 60.0)
            self.assertEqual(prepare.call_args.kwargs["audio_codec"], "flac")
            self.assertEqual(prepare.call_args.kwargs["transition_mode"], "none")
            controller.shutdown()

    def test_explicit_placement_without_effects_keeps_raw_playback(self) -> None:
        controller = PreviewAudioController(_renderer())
        tracks = [_track("a.mp3"), _track("b.mp3")]
        tracks[1].start_time_seconds = 35.0
        controller.start(tracks, Path("."), "none", 3.0)
        self.assertIsNone(controller._worker)

    def test_start_with_no_tracks_creates_no_worker(self) -> None:
        controller = PreviewAudioController(_renderer())
        controller.start([], Path("."), "automix", 3.0)
        self.assertIsNone(controller._worker)

    def test_cancel_with_no_worker_is_a_no_op(self) -> None:
        controller = PreviewAudioController(_renderer())
        controller.cancel()  # must not raise

    def test_a_worker_that_finishes_on_its_own_is_forgotten(self) -> None:
        controller = PreviewAudioController(_renderer())
        worker = _PreviewAudioWorker(_renderer(), [_track("a.mp3")], Path("."), "automix", 3.0, controller)
        controller._worker = worker
        controller._forget(worker)
        self.assertIsNone(controller._worker)

    def test_start_forwards_worker_progress_to_the_controllers_own_signal(self) -> None:
        """The preparation dialog subscribes to PreviewAudioController.progress,
        not to the internal worker directly, so start() must forward it."""
        controller = PreviewAudioController(_renderer())
        received: list[tuple[str, float, str]] = []
        controller.progress.connect(lambda stage, fraction, message: received.append(
            (stage, fraction, message),
        ))
        with patch.object(_PreviewAudioWorker, "start", lambda self: self.progress.emit(
            "Preparing audio", 0.2, "Rendering AutoMix transitions",
        )):
            controller.start([_track("a.mp3")], Path("."), "automix", 3.0)
        self.assertEqual(received, [("Preparing audio", 0.2, "Rendering AutoMix transitions")])

    def test_cancel_survives_an_already_deleted_qt_object(self) -> None:
        class _DeletedWorker:
            def isRunning(self) -> bool:
                raise RuntimeError("libshiboken: Internal C++ object already deleted.")

        controller = PreviewAudioController(_renderer())
        controller._worker = _DeletedWorker()
        controller.cancel()  # must not raise
        self.assertIsNone(controller._worker)


class PreviewAudioControllerShutdownTests(unittest.TestCase):
    """Real-threading regressions for the shutdown()/_forget() ownership race.

    A prior version let `_forget()` (connected to `finished` in start())
    call `worker.deleteLater()` even while `shutdown()` was still waiting
    on that same worker in a nested QEventLoop -- on Windows this produced
    a 0xC0000409 fail-fast crash the first time it was exercised under
    real threading rather than mocks. These tests use a real QThread and
    a real event loop specifically to catch that class of bug again; a
    passing assertion here is not enough on its own; the whole process
    must also exit cleanly (verified by the harness that invokes this
    module, not by any assertion below).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_shutdown_waits_for_a_running_worker_without_deleting_it_early(self) -> None:
        class SlowWorker(QThread):
            def __init__(self, parent: object) -> None:
                super().__init__(parent)
                self.release = threading.Event()

            def run(self) -> None:
                self.release.wait(5)

            def cancel(self) -> None:
                pass

        controller = PreviewAudioController(_renderer())
        worker = SlowWorker(controller)
        controller._worker = worker
        worker.finished.connect(lambda w=worker, c=controller: c._forget(w))
        worker.start()
        start = time.monotonic()
        controller.cancel()
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertIs(controller._worker, worker)
        self.assertTrue(worker.isRunning())

        heartbeat: list[bool] = []
        QTimer.singleShot(30, lambda: heartbeat.append(True))
        QTimer.singleShot(60, worker.release.set)
        controller.shutdown()

        self.assertTrue(heartbeat, "shutdown() must keep processing Qt events while it waits")
        self.assertIsNone(controller._worker)

    def test_pending_replacement_is_dropped_after_shutdown(self) -> None:
        release = threading.Event()
        controller = PreviewAudioController(_renderer())
        with patch.object(_PreviewAudioWorker, "run", lambda self: release.wait(5)):
            controller.start([_track("old.mp3")], Path("."), "automix", 3.0)
            old = controller._worker
            controller.start([_track("new.mp3")], Path("."), "automix", 3.0)
            self.assertIs(controller._worker, old)
            self.assertIsNotNone(controller._pending)
            QTimer.singleShot(10, release.set)
            controller.shutdown()
            self.assertIsNone(controller._pending)
            self.assertIsNone(controller._worker)

    def test_sync_prepare_tears_the_worker_down_through_the_shared_helper(self) -> None:
        import shiboken6

        from app.controllers import preview_audio_controller as module

        stopped: list[QThread] = []

        def recording_stop(worker: QThread) -> None:
            stopped.append(worker)
            module_stop(worker)

        module_stop = module.stop_qthread_now
        with TemporaryDirectory(prefix="preview-audio-sync-") as directory:
            renderer = _renderer()
            output = Path(directory) / "out.flac"
            with (
                patch.object(renderer, "prepare_playlist_audio", return_value=output),
                patch.object(module, "stop_qthread_now", recording_stop),
            ):
                path, plan = module.prepare_audio_for_ui(
                    renderer, [_track("a.mp3")], Path(directory), "automix", 3.0,
                    None, threading.Event(),
                )
        self.assertEqual(path, output)
        self.assertEqual(plan.duration_seconds, 30.0)
        self.assertEqual(len(stopped), 1)
        self.assertFalse(shiboken6.isValid(stopped[0]))


if __name__ == "__main__":
    unittest.main()
