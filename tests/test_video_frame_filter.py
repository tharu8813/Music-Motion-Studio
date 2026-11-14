from __future__ import annotations

import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoFrameFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEvent
import shiboken6

from app.canvas.live_canvas import LiveCanvas
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.services.history_service import HistoryService
from app.services.source_store import SourceStore
from app.video.frame_filter import VideoFrameFilterSettings, filter_video_frame


class VideoFrameFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_scales_large_decoder_frame_to_display_budget(self) -> None:
        image = QImage(3840, 2160, QImage.Format.Format_RGBA8888)
        image.fill(QColor(20, 40, 60, 255))
        filtered = filter_video_frame(
            image, VideoFrameFilterSettings(480, 270),
        )
        self.assertEqual((filtered.width(), filtered.height()), (480, 270))

    def test_vector_color_math_matches_existing_percentage_semantics(self) -> None:
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor(100, 120, 140, 77))
        filtered = filter_video_frame(
            image,
            VideoFrameFilterSettings(
                8, 8, brightness=10.0, contrast=20.0,
            ),
        )
        color = filtered.pixelColor(4, 4)
        self.assertEqual(color.alpha(), 77)
        self.assertAlmostEqual(color.red(), 119, delta=1)
        self.assertAlmostEqual(color.green(), 143, delta=1)
        self.assertAlmostEqual(color.blue(), 167, delta=1)

    def test_small_clean_decoder_frame_keeps_its_native_pixels(self) -> None:
        image = QImage(640, 360, QImage.Format.Format_RGBA8888)
        image.fill(QColor(20, 40, 60, 255))

        filtered = filter_video_frame(image, VideoFrameFilterSettings(1920, 1080))

        self.assertEqual(filtered.size(), image.size())
        self.assertEqual(filtered.pixelColor(0, 0), image.pixelColor(0, 0))

    def test_grayscale_and_saturation_are_applied_without_losing_alpha(self) -> None:
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor(240, 40, 20, 123))
        filtered = filter_video_frame(
            image, VideoFrameFilterSettings(8, 8, grayscale=True),
        )
        color = filtered.pixelColor(2, 2)
        self.assertAlmostEqual(color.red(), color.green(), delta=1)
        self.assertAlmostEqual(color.green(), color.blue(), delta=1)
        self.assertEqual(color.alpha(), 123)

    def test_box_blur_spreads_an_impulse_and_preserves_dimensions(self) -> None:
        image = QImage(9, 9, QImage.Format.Format_RGBA8888)
        image.fill(QColor(0, 0, 0, 255))
        image.setPixelColor(4, 4, QColor(255, 255, 255, 255))
        filtered = filter_video_frame(
            image, VideoFrameFilterSettings(9, 9, blur=2.0),
        )
        self.assertEqual((filtered.width(), filtered.height()), (9, 9))
        self.assertLess(filtered.pixelColor(4, 4).red(), 255)
        self.assertGreater(filtered.pixelColor(4, 3).red(), 0)

    def test_full_resolution_blur_stays_fast(self) -> None:
        # The box blur runs on a bounded working image; a full-frame blur must
        # not regress to the multi-hundred-millisecond cumulative-sum path.
        from app.video.frame_filter import apply_color_filters

        image = QImage(1920, 1080, QImage.Format.Format_RGBA8888)
        image.fill(QColor(120, 90, 160))
        apply_color_filters(image, blur=24.0)  # warm up numpy / Qt
        start = time.perf_counter()
        blurred = apply_color_filters(image, blur=24.0)
        elapsed = time.perf_counter() - start
        self.assertEqual((blurred.width(), blurred.height()), (1920, 1080))
        self.assertLess(elapsed, 0.12)

    def test_task_failure_emits_empty_frame_and_logs_each_error_once(self) -> None:
        from app.video import frame_filter
        from app.video.frame_filter import VideoFrameFilterSignals, VideoFrameFilterTask

        signals = VideoFrameFilterSignals()
        results: list[QImage] = []
        signals.finished.connect(lambda _generation, image, _raw: results.append(image))
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        settings = VideoFrameFilterSettings(8, 8, blur=2.0)
        errors = [ValueError("bad shape")] * 100 + [MemoryError("out of memory")]
        with (
            patch.object(frame_filter, "_LOGGED_FAILURES", set()),
            patch.object(frame_filter, "filter_video_frame", side_effect=errors),
            self.assertLogs(frame_filter.LOGGER, "WARNING") as logs,
        ):
            for _ in errors:
                VideoFrameFilterTask(image, settings, 1, signals).run()

        self.assertEqual(len(results), 101)
        self.assertTrue(all(result.isNull() for result in results))
        self.assertEqual(len(logs.records), 2)
        self.assertIn("8x8", logs.output[0])
        self.assertIn("blur=2.0", logs.output[0])
        self.assertIs(logs.records[1].exc_info[0], MemoryError)

    def test_task_survives_a_deleted_result_receiver(self) -> None:
        from app.video.frame_filter import VideoFrameFilterSignals, VideoFrameFilterTask

        signals = VideoFrameFilterSignals()
        task = VideoFrameFilterTask(
            QImage(8, 8, QImage.Format.Format_RGBA8888),
            VideoFrameFilterSettings(8, 8), 1, signals,
        )
        shiboken6.delete(signals)
        task.run()  # must not raise

    def test_image_element_filters_change_the_rendered_pixmap_quickly(self) -> None:
        with TemporaryDirectory(prefix="playlist-image-filter-") as directory:
            path = Path(directory) / "sample.png"
            base = QImage(1280, 720, QImage.Format.Format_ARGB32)
            base.fill(QColor(90, 110, 130))
            self.assertTrue(base.save(str(path)))

            source = Source(SourceType.IMAGE, "Filtered image", width=1280, height=720)
            source.content_path = str(path)
            item = SourceItem(source)
            item.apply_source()
            before = item._pixmap.toImage().pixelColor(640, 360)

            source.brightness = 40.0
            source.contrast = 25.0
            source.blur = 6.0
            start = time.perf_counter()
            item.apply_source()
            elapsed = time.perf_counter() - start
            after = item._pixmap.toImage().pixelColor(640, 360)

            self.assertNotEqual(before.getRgb(), after.getRgb())
            self.assertLess(elapsed, 1.0)

    def test_busy_item_keeps_only_the_newest_decoder_frame(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Frame queue"))
        item._video_filter_busy = True
        first = QImage(8, 8, QImage.Format.Format_RGBA8888)
        first.fill(QColor(255, 0, 0))
        second = QImage(8, 8, QImage.Format.Format_RGBA8888)
        second.fill(QColor(0, 0, 255))

        item._video_frame_changed(QVideoFrame(first))
        item._video_frame_changed(QVideoFrame(second))

        self.assertIsNotNone(item._video_pending_frame)
        pending, _generation = item._video_pending_frame
        self.assertIsInstance(pending, QVideoFrame)
        self.assertEqual(pending.toImage().pixelColor(4, 4), QColor(0, 0, 255))
        self.assertGreaterEqual(item.video_decoder_stats().dropped_frames, 1)

    def test_cpu_decoder_frame_is_converted_on_worker_while_gui_remains_responsive(self) -> None:
        from app.video import frame_filter

        item = SourceItem(Source(SourceType.VIDEO, "Worker conversion", width=16, height=16))
        self.addCleanup(item.release_video_decoder)
        original = QImage(16, 16, QImage.Format.Format_RGB32)
        original.fill(QColor("green"))
        entered, finish = Event(), Event()
        gui_thread = get_ident()
        converted_on = []
        ready_on = []
        item.video_frame_ready.connect(lambda: ready_on.append(get_ident()))

        def convert(frame):
            converted_on.append(get_ident())
            entered.set()
            finish.wait(2)
            return frame.toImage()

        with patch.object(frame_filter, "video_frame_to_image", side_effect=convert):
            try:
                item._video_frame_changed(QVideoFrame(original))
                self.assertTrue(entered.wait(2))
                self.assertNotEqual(converted_on, [gui_thread])
                self.assertTrue(item.video_preview_frame().isNull())
            finally:
                finish.set()
            for _ in range(100):
                if ready_on:
                    break
                QTest.qWait(10)
        self.assertEqual(ready_on, [gui_thread])
        self.assertEqual(item.video_preview_frame().pixelColor(3, 3), QColor("green"))

    def test_completed_filtered_video_frame_announces_preview_refresh(self) -> None:
        item = SourceItem(Source(SourceType.VIDEO, "Paused frame"))
        image = QImage(16, 9, QImage.Format.Format_RGBA8888)
        image.fill(QColor(12, 34, 56))
        ready: list[bool] = []
        item.video_frame_ready.connect(lambda: ready.append(True))

        item._video_filter_busy = True
        item._video_filter_finished(item._video_filter_key(), image)

        self.assertEqual(ready, [True])
        self.assertEqual(item._pixmap.toImage().pixelColor(4, 4), QColor(12, 34, 56))
        self.assertEqual(
            item.video_preview_frame().pixelColor(4, 4), QColor(12, 34, 56),
        )
        self.assertFalse(item.video_preview_frame().isNull())
        item.release_video_decoder()
        self.assertTrue(item.video_preview_frame().isNull())

    def test_timeline_video_paints_without_converting_to_pixmap(self) -> None:
        item = SourceItem(Source(SourceType.VIDEO, "Timeline frame", width=100, height=60))
        frame = QImage(16, 9, QImage.Format.Format_RGBA8888)
        frame.fill(QColor(12, 34, 56, 160))

        def render() -> QImage:
            target = QImage(100, 60, QImage.Format.Format_ARGB32_Premultiplied)
            target.fill(QColor(60, 20, 80))
            painter = QPainter(target)
            try:
                item.paint(painter, None)
            finally:
                painter.end()
            return target

        item._video_filter_finished(item._video_filter_key(), frame)
        expected = render()
        item.release_video_decoder()
        item._video_timeline_preview_active = True
        item._video_filter_finished(item._video_filter_key(), frame)

        self.assertTrue(item._pixmap.isNull())
        self.assertEqual(render(), expected)
        self.assertTrue(item._pixmap.isNull())

    def test_supported_rhi_frame_is_retained_without_calling_to_image(self) -> None:
        class RhiFrame(QVideoFrame):
            def handleType(self) -> QVideoFrame.HandleType:
                return QVideoFrame.HandleType.RhiTextureHandle

            def pixelFormat(self) -> QVideoFrameFormat.PixelFormat:
                return QVideoFrameFormat.PixelFormat.Format_RGBA8888

            def toImage(self) -> QImage:
                raise AssertionError("RHI preview frame must not call toImage()")

        item = SourceItem(Source(SourceType.VIDEO, "Native GPU frame"))
        item.set_video_direct_gpu_frame(True)
        item._video_timeline_preview_active = True
        source_image = QImage(16, 9, QImage.Format.Format_RGBA8888)
        source_image.fill(QColor("#123456"))

        item._video_frame_changed(RhiFrame(source_image))

        gpu_frame, serial = item.video_preview_gpu_frame()
        self.assertTrue(gpu_frame.isValid())
        self.assertEqual(serial, 1)
        self.assertTrue(item.video_preview_frame().isNull())
        self.assertTrue(item.video_preview_active)
        item.release_video_decoder()

    def test_transport_pause_and_speed_are_applied_to_active_decoder(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Decoder clock"))
        player = MagicMock()
        item._video_player = player
        item._video_timeline_preview_active = True

        item.set_video_preview_playback(False, 1.5)
        player.setPlaybackRate.assert_called_with(1.5)
        player.pause.assert_called_once()

        item.set_video_preview_playback(True, 2.0)
        player.setPlaybackRate.assert_called_with(2.0)
        player.play.assert_called_once()
        item._video_player = None

    def test_loading_decoder_coalesces_seek_requests_until_media_is_ready(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Deferred seek"))
        item.source.source_type = SourceType.VIDEO
        player = MagicMock()
        player.mediaStatus.return_value = QMediaPlayer.MediaStatus.LoadingMedia
        item._video_player = player
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.touch()
            item.set_video_preview_playback(False, 1.25)
            item.set_video_preview_position(str(path), 1.0)
            item.set_video_preview_position(str(path), 2.0)

        player.setPosition.assert_not_called()
        self.assertEqual(item._video_pending_seek_ms, 2000)
        item._video_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
        player.setPosition.assert_called_once_with(2000)
        self.assertIsNone(item._video_pending_seek_ms)
        self.assertEqual(item.video_decoder_stats().seek_count, 1)
        item._video_player = None

    def test_playing_decoder_limits_corrective_seeks_when_position_stalls(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Stalled decoder"))
        item.source.source_type = SourceType.VIDEO
        player = MagicMock()
        player.position.return_value = 0
        player.playbackState.return_value = QMediaPlayer.PlaybackState.PlayingState
        item._video_player = player
        item._video_preview_path = "same.mp4"
        item._video_timeline_preview_active = True
        item._video_should_play = True
        item._video_applied_playback_rate = 1.0

        request_times = [10.0 + frame / 30.0 for frame in range(31)]
        with patch("app.canvas.source_item.monotonic", side_effect=request_times):
            for frame in range(31):
                item.set_video_preview_position("same.mp4", frame / 30.0)

        self.assertEqual(player.setPosition.call_count, 1)
        self.assertEqual(item.video_decoder_stats().seek_count, 1)
        item._video_player = None

    def test_playing_decoder_seeks_immediately_for_real_timeline_jump(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Timeline jump"))
        item.source.source_type = SourceType.VIDEO
        player = MagicMock()
        player.position.return_value = 0
        player.playbackState.return_value = QMediaPlayer.PlaybackState.PlayingState
        item._video_player = player
        item._video_preview_path = "same.mp4"
        item._video_timeline_preview_active = True
        item._video_should_play = True
        item._video_applied_playback_rate = 1.0

        with patch("app.canvas.source_item.monotonic", side_effect=[10.0, 10.033]):
            item.set_video_preview_position("same.mp4", 0.0)
            item.set_video_preview_position("same.mp4", 5.0)

        player.setPosition.assert_called_once_with(5000)
        item._video_player = None

    def test_explicit_playhead_seek_bypasses_normal_playback_cooldown(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Explicit seek"))
        item.source.source_type = SourceType.VIDEO
        player = MagicMock()
        player.position.return_value = 0
        player.playbackState.return_value = QMediaPlayer.PlaybackState.PlayingState
        item._video_player = player
        item._video_preview_path = "same.mp4"
        item._video_timeline_preview_active = True
        item._video_should_play = True
        item._video_applied_playback_rate = 1.0
        item._video_last_seek_time = 10.0

        with patch("app.canvas.source_item.monotonic", return_value=10.1):
            item.set_video_preview_position(
                "same.mp4", 0.5, force_seek=True,
            )

        player.setPosition.assert_called_once_with(500)
        item._video_player = None

    def test_paused_decoder_keeps_precise_seek_behavior(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Paused seek"))
        item.source.source_type = SourceType.VIDEO
        player = MagicMock()
        player.position.return_value = 0
        item._video_player = player
        item._video_preview_path = "same.mp4"
        item._video_timeline_preview_active = True
        item._video_should_play = False

        with patch("app.canvas.source_item.monotonic", return_value=10.0):
            item.set_video_preview_position("same.mp4", 0.25)

        player.setPosition.assert_called_once_with(250)
        item._video_player = None

    def test_editor_video_accepts_one_poster_then_leaves_live_preview_off(self) -> None:
        item = SourceItem(Source(SourceType.VIDEO, "Poster"))
        item._video_filter_busy = True
        item._video_editor_poster_pending = True
        image = QImage(16, 9, QImage.Format.Format_RGBA8888)
        image.fill(QColor(30, 60, 90))

        item._video_frame_changed(QVideoFrame(image))

        self.assertFalse(item._video_editor_poster_pending)
        self.assertFalse(item._video_timeline_preview_active)
        self.assertIsNotNone(item._video_pending_frame)
        item.release_video_decoder()

    def test_live_video_filter_uses_preview_quality_budget(self) -> None:
        source = Source(
            SourceType.VIDEO, "Budgeted", width=1000, height=500, scale=1.0,
        )
        item = SourceItem(source)

        item.set_video_preview_budget(0.55, 15)
        key = item._video_filter_key()

        self.assertEqual(key[2:4], (550, 275))
        self.assertEqual(item._video_preview_fps, 15)
        item.release_video_decoder()

    def test_intentional_fps_throttle_is_not_decoder_pressure(self) -> None:
        item = SourceItem(Source(SourceType.VIDEO, "Throttled"))
        item._video_timeline_preview_active = True
        item._video_preview_fps = 10
        item._video_next_frame_deadline = float("inf")
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)

        item._video_frame_changed(QVideoFrame(image))

        stats = item.video_decoder_stats()
        self.assertEqual(stats.throttled_frames, 1)
        self.assertEqual(stats.pressure_drops, 0)
        self.assertFalse(stats.filter_pending)
        item.release_video_decoder()

    def test_nominal_video_cadence_accepts_clock_jitter_but_still_throttles_double_rate(self) -> None:
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#336699"))
        for fps, early_interval in ((30, 0.032), (60, 0.016)):
            with self.subTest(fps=fps):
                item = SourceItem(Source(SourceType.VIDEO, "Clock jitter"))
                item._video_timeline_preview_active = True
                item._video_preview_fps = fps
                item._video_filter_busy = True  # isolate scheduling from worker completion
                item._video_next_frame_deadline = 10.0 + 1.0 / fps
                try:
                    with patch("app.canvas.source_item.monotonic", return_value=10.0 + early_interval):
                        item._video_frame_changed(QVideoFrame(image))
                    self.assertEqual(item.video_decoder_stats().accepted_frames, 1)
                    with patch("app.canvas.source_item.monotonic", return_value=10.0 + early_interval + 0.5 / fps):
                        item._video_frame_changed(QVideoFrame(image))
                    self.assertEqual(item.video_decoder_stats().accepted_frames, 1)
                    self.assertEqual(item.video_decoder_stats().throttled_frames, 1)
                    self.assertEqual(item.video_decoder_stats().pressure_drops, 0)
                finally:
                    item.release_video_decoder()

    def test_decoder_throttle_keeps_48_of_60_source_frames(self) -> None:
        item = SourceItem(Source(SourceType.VIDEO, "48 FPS budget"))
        self.addCleanup(item.release_video_decoder)
        item._video_timeline_preview_active = True
        item.set_video_preview_budget(1.0, 48)
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#336699"))

        with patch.object(item, "_start_next_video_filter"), \
                patch("app.canvas.source_item.monotonic") as clock:
            for index in range(60):
                clock.return_value = 10.0 + index / 60
                item._video_frame_changed(QVideoFrame(image))
                item._video_pending_frame = None

        stats = item.video_decoder_stats()
        self.assertEqual(stats.accepted_frames, 48)
        self.assertEqual(stats.throttled_frames, 12)
        self.assertEqual(stats.pressure_drops, 0)

    def test_fps_backpressure_does_not_invalidate_resolution_filter(self) -> None:
        item = SourceItem(Source(SourceType.VIDEO, "Independent budgets"))
        generation = item._video_filter_generation

        item.set_video_preview_budget(item._video_display_scale, 15)

        self.assertEqual(item._video_preview_fps, 15)
        self.assertEqual(item._video_filter_generation, generation)
        item.set_video_preview_budget(0.5, 15)
        self.assertEqual(item._video_filter_generation, generation + 1)
        item.release_video_decoder()

    def test_gpu_color_filter_keeps_scaling_and_blur_on_cpu_only(self) -> None:
        source = Source(
            SourceType.VIDEO, "GPU color",
            brightness=12.0, contrast=18.0, video_saturation=1.7,
            video_grayscale=True, blur=3.0,
        )
        item = SourceItem(source)

        cpu_settings = item._video_filter_settings(item._video_filter_key())
        self.assertEqual(cpu_settings.brightness, 12.0)
        self.assertEqual(cpu_settings.contrast, 18.0)
        self.assertTrue(cpu_settings.grayscale)
        item.set_video_gpu_color_filter(True)
        gpu_settings = item._video_filter_settings(item._video_filter_key())

        self.assertEqual(gpu_settings.brightness, 0.0)
        self.assertEqual(gpu_settings.contrast, 0.0)
        self.assertEqual(gpu_settings.saturation, 1.0)
        self.assertFalse(gpu_settings.grayscale)
        self.assertEqual(gpu_settings.blur, 3.0)
        item.release_video_decoder()

    def test_suspending_preview_invalidates_pending_filter_work(self) -> None:
        item = SourceItem(Source(SourceType.TEXT, "Suspend"))
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        item._video_pending_frame = (image, item._video_filter_key())
        generation = item._video_filter_generation

        item.suspend_video_preview()

        self.assertIsNone(item._video_pending_frame)
        self.assertEqual(item._video_filter_generation, generation + 1)
        self.assertEqual(item._video_preview_path, "")

    def test_deleted_video_releases_decoder_and_undo_recreates_it(self) -> None:
        store = SourceStore()
        canvas = LiveCanvas(store, object())  # Translator is not used by this path.
        source = Source(SourceType.VIDEO, "Deleted decoder")
        store.add(source)
        item = canvas._items[source.id]
        player = item._video_player
        sink = item._video_sink
        self.assertIsNotNone(player)
        self.assertIsNotNone(sink)

        store.remove(source.id)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        self.assertIsNone(item._video_player)
        self.assertIsNone(item._video_sink)
        self.assertTrue(item._pixmap.isNull())
        self.assertFalse(shiboken6.isValid(player))
        self.assertFalse(shiboken6.isValid(sink))

        store.add(source)
        self.assertIs(canvas._items[source.id], item)
        self.assertIsNotNone(item._video_player)
        self.assertIsNot(item._video_player, player)
        canvas.release_video_decoders()

    def test_hidden_video_waits_to_create_decoder_until_shown(self) -> None:
        source = Source(SourceType.VIDEO, "Hidden", visible=False)
        item = SourceItem(source)
        self.assertIsNone(item._video_player)
        source.visible = True
        item.apply_source()
        self.assertIsNotNone(item._video_player)
        item.release_video_decoder()

    def test_unreachable_retired_item_is_removed_from_the_scene(self) -> None:
        store = SourceStore()
        canvas = LiveCanvas(store, object())
        source = Source(SourceType.VIDEO, "Pruned decoder")
        store.add(source)
        item = canvas._items[source.id]
        store.remove(source.id)

        canvas.prune_retired_items({source.id})
        self.assertIn(source.id, canvas._retired_items)
        canvas.prune_retired_items(set())

        self.assertNotIn(source.id, canvas._retired_items)
        self.assertNotIn(item, canvas.scene_model.items())

    def test_history_reports_only_undo_reachable_source_ids(self) -> None:
        history = HistoryService(max_entries=10)
        history.reset({"sources": [{"id": "first"}]})
        history.commit({"sources": [{"id": "second"}]})
        self.assertEqual(history.retained_source_ids(), {"first", "second"})

        history.undo()
        history.commit({"sources": [{"id": "replacement"}]})

        self.assertEqual(
            history.retained_source_ids(), {"first", "replacement"},
        )


if __name__ == "__main__":
    unittest.main()
