"""Timeline scrubbing keeps the newest frame and preserves playback intent."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.canvas.live_canvas import CanvasScene
from app.dialogs.export_preview_dialog import ExportPreviewDialog, TIMELINE_SCALE
from app.models.playlist import PlaylistTrack
from app.utils.i18n import Translator


class PreviewResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def preview(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        preview = ExportPreviewDialog(scene, [PlaylistTrack("preview.wav", "Preview", duration_seconds=60)],
                                      Translator(), preferred_backend="cpu")
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview._stop_preview)
        return preview

    def test_display_refresh_controls_playback_and_decoder_but_not_audio_analysis(self):
        preview = self.preview()
        original_sampling_fps = preview.preview_fps
        self.assertEqual(original_sampling_fps, 60)
        self.assertEqual(preview._active_render_scale, 1.0)
        with patch("app.dialogs.export_preview_dialog.screen_preview_fps", return_value=144):
            preview._sync_presentation_cadence()
        self.assertEqual(preview.play_timer.interval(), 7)
        self.assertEqual(preview.presentation_fps, 144)
        self.assertEqual(preview._decoder_backpressure.fps_limit, 144)
        self.assertEqual(preview.preview_fps, original_sampling_fps)
        with patch("app.dialogs.export_preview_dialog.screen_preview_fps", return_value=60):
            preview._sync_presentation_cadence()
        self.assertEqual(preview.play_timer.interval(), 17)
        self.assertEqual(preview._decoder_backpressure.fps_limit, 60)

    def test_drag_coalesces_and_commits_the_latest_position_once(self):
        preview = self.preview()
        preview._playing = True
        with patch.object(preview, "refresh_preview", wraps=preview.refresh_preview) as render, \
             patch.object(preview, "_start_audio_at_playhead") as seek:
            preview.timeline.setSliderDown(True)
            for value in range(1, 61):
                preview.timeline.setValue(value * 10)
            self.assertEqual(render.call_count, 0)
            self.assertEqual(seek.call_count, 0)
            self.assertEqual(preview._playhead_seconds, 6)
            self.assertTrue(preview._refresh_timer.isActive())
            preview._advance_playback()
            self.assertEqual(preview.timeline.value(), 600)
            preview.timeline.setSliderDown(False)
            render.assert_called_once_with()
            seek.assert_called_once_with()
            self.assertTrue(preview.play_timer.isActive())
            self.assertFalse(preview._refresh_timer.isActive())
            preview.play_timer.stop()
            self.app.processEvents()
            self.assertEqual(render.call_count, 1)

    def test_drag_keeps_refreshing_while_held_and_paused_stays_paused(self):
        preview = self.preview()
        with patch.object(preview, "refresh_preview", wraps=preview.refresh_preview) as render:
            preview.timeline.setSliderDown(True)
            preview.timeline.setValue(300)
            QTest.qWait(80)
            self.assertEqual(render.call_count, 1)
            preview.timeline.setValue(400)
            preview.timeline.setSliderDown(False)
            self.assertEqual(render.call_count, 2)
        self.assertFalse(preview._playing)
        self.assertFalse(preview.play_timer.isActive())
        self.assertEqual(preview.timeline.value(), 400)

    def test_pending_media_readiness_does_not_resume_during_a_drag(self):
        preview = self.preview()
        preview._playing = True
        preview._active_track_index = 0
        preview._pending_media_seek_ms = 1000
        preview._pending_media_generation = preview._media_source_generation
        preview.timeline.setSliderDown(True)
        preview.timeline.setValue(500)
        with patch.object(preview.media_player, "mediaStatus", return_value=QMediaPlayer.MediaStatus.LoadedMedia), \
             patch.object(preview.media_player, "isSeekable", return_value=True), \
             patch.object(preview.media_player, "setPosition") as position, \
             patch.object(preview.media_player, "play") as play:
            preview._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
            position.assert_not_called()
            play.assert_not_called()
            preview.timeline.setSliderDown(False)
            position.assert_called_once_with(5000)
            play.assert_called_once_with()
        self.assertIsNone(preview._pending_media_seek_ms)

    def test_pause_requested_while_dragging_is_preserved_on_release(self):
        preview = self.preview()
        with patch.object(preview, "_start_audio_at_playhead") as seek:
            preview.play_button.setChecked(True)
            preview.timeline.setSliderDown(True)
            preview.timeline.setValue(500)
            preview.play_button.setChecked(False)
            seek.reset_mock()
            preview.timeline.setSliderDown(False)
            seek.assert_not_called()
        self.assertFalse(preview._playing)
        self.assertFalse(preview.play_timer.isActive())

    def test_close_cancels_queued_drag_refresh(self):
        preview = self.preview()
        preview.timeline.setSliderDown(True)
        preview.timeline.setValue(500)
        preview._stop_preview()
        with patch.object(preview, "refresh_preview") as render, \
             patch.object(preview, "_start_audio_at_playhead") as seek:
            preview.timeline.setSliderDown(False)
            QTest.qWait(60)
            render.assert_not_called()
            seek.assert_not_called()
        self.assertFalse(preview._refresh_timer.isActive())
        self.assertFalse(preview.play_timer.isActive())

    def test_programmatic_seek_remains_immediate_and_buttons_reflect_boundaries(self):
        preview = self.preview()
        with patch.object(preview, "refresh_preview", wraps=preview.refresh_preview) as render:
            preview._seek_to_seconds(7.25)
            render.assert_called_once_with()
        self.assertEqual(preview.timeline.value(), round(7.25 * TIMELINE_SCALE))
        self.assertFalse(preview.previous_button.isEnabled())
        self.assertFalse(preview.next_button.isEnabled())
        self.assertTrue(preview.timeline.accessibleDescription())

    def test_custom_timeline_emits_native_press_and_release_signals(self):
        preview = self.preview()
        preview.timeline.resize(800, 46)
        with patch.object(preview, "_start_audio_at_playhead"):
            QTest.mousePress(preview.timeline, Qt.MouseButton.LeftButton, pos=QPoint(200, 20))
            self.assertTrue(preview._scrubbing)
            self.assertTrue(preview.timeline.isSliderDown())
            QTest.mouseRelease(preview.timeline, Qt.MouseButton.LeftButton, pos=QPoint(600, 20))
            self.assertFalse(preview._scrubbing)
            self.assertFalse(preview.timeline.isSliderDown())
        self.assertGreater(preview.timeline.value(), preview.timeline.maximum() / 2)

    def test_cpu_quality_adapts_under_load_and_recovers_without_changing_export(self):
        preview = self.preview()

        class Clock:
            def elapsed(self):
                return 600

            def restart(self):
                pass

        # Adaptive downscaling belongs to frame/power modes; the default
        # resolution preset deliberately keeps the canvas at 100%.
        with patch("app.dialogs.export_preview_dialog.save_preview_quality", side_effect=lambda v: v):
            preview.set_preview_quality("frames")
        rect = preview.scene.artboard_rect
        base = preview.preview_render_scale
        preview._playing = True
        preview._frame_stats_clock = Clock()
        preview._scene_cadence_target = 30
        for _ in range(2):
            preview._presented_frames = 5
            preview._record_presented_frame()
        self.assertLess(preview._active_render_scale, base)
        self.assertTrue(preview._base_image.isNull())
        self.assertFalse(preview._gpu_refresh_deferred)
        for _ in range(6):
            preview._presented_frames = 18
            preview._record_presented_frame()
        self.assertEqual(preview._active_render_scale, base)
        self.assertEqual(preview.preview_render_scale, base)
        self.assertEqual(preview.scene.artboard_rect, rect)

    def test_progressive_mix_waits_until_drag_release(self):
        preview = self.preview()
        preview._progressive = True
        preview.timeline.setSliderDown(True)
        preview.timeline.setValue(700)
        with patch.object(preview, "_apply_blended_audio") as swap:
            preview._on_blended_audio_ready("mix.wav", preview._compiled_plan)
            swap.assert_not_called()
            self.assertIsNotNone(preview._pending_swap)
            preview.timeline.setSliderDown(False)
            self.assertEqual(swap.call_count, 1)
            self.assertEqual(swap.call_args.kwargs["playhead"], 7)
        self.assertIsNone(preview._pending_swap)

    def test_decoded_frame_refreshes_the_position_while_scrubbing(self):
        preview = self.preview()
        preview._playing = True
        preview.timeline.setSliderDown(True)
        with patch.object(preview, "_schedule_refresh") as refresh:
            preview._video_frame_ready()
            refresh.assert_called_once_with(preview.play_timer.interval())


if __name__ == "__main__":
    unittest.main()
