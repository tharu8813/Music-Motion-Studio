"""Large playlists open without waiting for artwork or rebuilding rows."""

import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog, PreviewCoverWorker
from app.preview.cover_worker import PreviewCoverWorker as IsolatedPreviewCoverWorker
from app.dialogs.export_preview_dialog import (
    PreviewTrackDelegate, TRACK_ROW_ROLE, _track_details,
)
from app.widgets.preview_track_delegate import (
    PreviewTrackDelegate as IsolatedPreviewTrackDelegate,
    TRACK_ROW_ROLE as ISOLATED_TRACK_ROW_ROLE,
    _track_details as isolated_track_details,
)
from app.models.playlist import PlaylistTrack
from app.utils.i18n import Translator


class LargePlaylistPreviewTests(unittest.TestCase):
    def test_track_delegate_isolated_from_dialog_and_keeps_legacy_import(self):
        self.assertIs(PreviewTrackDelegate, IsolatedPreviewTrackDelegate)
        self.assertEqual(TRACK_ROW_ROLE, ISOLATED_TRACK_ROW_ROLE)
        self.assertIs(_track_details, isolated_track_details)
        self.assertEqual(PreviewTrackDelegate.__module__, "app.widgets.preview_track_delegate")
        track = PlaylistTrack("a.mp3", "Song", duration_seconds=20)
        self.assertEqual(_track_details(track, " / "), "a.mp3")

        preview = self.preview(2)
        self.assertIsInstance(preview.track_list.itemDelegate(), IsolatedPreviewTrackDelegate)
        self.assertEqual(
            preview.track_list.item(0).data(TRACK_ROW_ROLE)[1],
            "03:00",
        )

    def test_cover_worker_isolated_from_dialog_and_keeps_legacy_identity(self):
        self.assertIs(PreviewCoverWorker, IsolatedPreviewCoverWorker)
        self.assertEqual(PreviewCoverWorker.__module__, "app.preview.cover_worker")
        tracks = [PlaylistTrack("1.wav", "One", cover_path="cover.png")]
        worker = PreviewCoverWorker(tracks)
        try:
            self.assertEqual(worker.paths, (("1.wav", "cover.png"),))
            with patch("app.preview.cover_worker.prewarm_cover_thumbnail") as load:
                worker.requestInterruption()
                worker.run()
            load.assert_not_called()
        finally:
            worker.deleteLater()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def preview(self, count=1000):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        tracks = [PlaylistTrack(f"{index}.wav", f"Track {index}", duration_seconds=180)
                  for index in range(count)]
        preview = ExportPreviewDialog(scene, tracks, Translator(), preferred_backend="cpu",
                                      transition_mode="crossfade", crossfade_seconds=3)
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        return preview

    def test_slow_artwork_does_not_block_open_or_gui_heartbeat(self):
        started, release = threading.Event(), threading.Event()
        reads = []

        def slow_cover(*_paths):
            reads.append(threading.get_ident())
            started.set()
            release.wait(3)
            return QImage()

        with patch("app.preview.cover_worker.prewarm_cover_thumbnail", side_effect=slow_cover):
            preview = self.preview()
            worker = preview._cover_worker
            self.assertEqual(preview.track_list.count(), 1000)
            self.assertFalse(reads)
            self.app.processEvents()
            try:
                self.assertTrue(started.wait(2))
                heartbeats = []
                timer = QTimer()
                timer.setInterval(10)
                timer.timeout.connect(lambda: heartbeats.append(True))
                timer.start()
                QTest.qWait(80)
                timer.stop()
                self.assertGreaterEqual(len(heartbeats), 3)
                self.assertNotEqual(reads[0], threading.get_ident())
                with patch.object(worker, "wait", side_effect=AssertionError("GUI must not wait")):
                    preview._stop_preview()
                self.assertTrue(worker.isRunning())
                self.assertIs(worker.parent(), self.app)
            finally:
                worker.requestInterruption()
                release.set()
                self.assertTrue(worker.wait(2000))
                self.app.processEvents()
            self.assertEqual(len(reads), 1)

    def test_plan_and_language_changes_preserve_rows_covers_and_navigation(self):
        preview = self.preview(3)
        row = preview.track_list.item(1)
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0xff304050)
        preview._store_track_cover(1, image)
        icon_key = row.icon().cacheKey()
        preview.track_list.setCurrentRow(1)
        preview.track_list.verticalScrollBar().setValue(0)
        with patch.object(preview.track_list, "clear", side_effect=AssertionError("Keep existing rows")):
            from app.timeline.crossfade import compile_crossfade
            plan = compile_crossfade(preview.tracks, 6)
            preview._apply_blended_audio(Path("mix.wav"), plan)
            preview.retranslate()
        self.assertIs(preview.track_list.item(1), row)
        self.assertEqual(row.icon().cacheKey(), icon_key)
        self.assertEqual(row.data(Qt.ItemDataRole.UserRole + 1), 174)
        preview.track_list.itemDoubleClicked.emit(row)
        self.assertEqual(preview.timeline.value(), 17400)
        preview._stop_preview()
        preview._store_track_cover(1, image)
        self.assertEqual(row.icon().cacheKey(), icon_key)

    def test_close_before_deferred_load_never_starts_worker(self):
        with patch("app.preview.cover_worker.prewarm_cover_thumbnail") as load:
            preview = self.preview(2)
            preview._stop_preview()
            self.app.processEvents()
            load.assert_not_called()

    def test_worker_delivers_icons_to_gui_rows(self):
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0xff405060)
        with patch("app.preview.cover_worker.prewarm_cover_thumbnail", return_value=image):
            preview = self.preview(3)
            self.app.processEvents()
            self.assertTrue(preview._cover_worker.wait(2000))
            self.app.processEvents()
        for index in range(3):
            self.assertFalse(preview.track_list.item(index).icon().isNull())

    def test_compiled_plan_lookups_do_not_scan_large_playlist_per_frame(self):
        preview = self.preview(300)
        tracks = list(preview.tracks)
        self.assertEqual(len(preview._track_index_by_id), len(tracks))

        class IndexedOnly(list):
            def __iter__(self):
                raise AssertionError("Per-frame owner lookup must not scan playlist")

        # Once the preview is built, both audio ownership and Canvas handover
        # must use indexed access even for the final track in a large mix.
        with patch.object(preview, "tracks", IndexedOnly(tracks)):
            owner = preview._compiled_plan.presentation.windows[-1]
            position = owner.timeline_start + 15.0
            for _ in range(10):
                audio = preview._track_at(position)
                visual, _junction = preview._visual_track_at(position)
                self.assertEqual(audio[0], len(tracks) - 1)
                self.assertIs(audio[1], tracks[-1])
                self.assertEqual(visual[0], len(tracks) - 1)
                self.assertIs(visual[1], tracks[-1])

        # A new compiled AutoMix/crossfade plan retains the same stable track
        # identity mapping, without requiring an extra per-frame rebuild.
        from app.timeline.crossfade import compile_crossfade
        preview._compiled_plan = compile_crossfade(tracks, 6)
        with patch.object(preview, "tracks", IndexedOnly(tracks)):
            owner = preview._compiled_plan.presentation.windows[-1]
            visual, _junction = preview._visual_track_at(owner.timeline_start + 15)
            self.assertEqual(visual[0], len(tracks) - 1)

    def test_track_keyframes_resume_at_next_track_after_silent_gap(self):
        from app.timeline.crossfade import compile_crossfade

        preview = self.preview(2)
        preview.tracks[0].start_time_seconds = 5
        preview.tracks[1].start_time_seconds = 210
        preview._compiled_plan = compile_crossfade(preview.tracks, 3)
        for seconds, active in ((4.99, False), (5.0, True), (184.99, True),
                                (185.0, False), (209.99, False), (210.0, True)):
            with self.subTest(seconds=seconds):
                self.assertEqual(preview._keyframes_active_at(seconds), active)

    def test_cover_errors_do_not_abort_remaining_tracks(self):
        tracks = [PlaylistTrack("broken.wav", "Broken"), PlaylistTrack("ok.wav", "OK")]
        worker = PreviewCoverWorker(tracks)
        images = []
        worker.ready.connect(lambda index, image: images.append((index, image)))
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0xff405060)
        with patch("app.preview.cover_worker.prewarm_cover_thumbnail",
                   side_effect=[OSError("unreadable cover"), image]):
            worker.start()
            self.assertTrue(worker.wait(2000))
            self.app.processEvents()
        self.assertEqual([index for index, _image in images], [1])
        worker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
