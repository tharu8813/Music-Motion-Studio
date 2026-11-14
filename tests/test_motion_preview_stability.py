"""Delayed frames must never acquire a newer seek's identity."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtWidgets import QApplication

from app.canvas.source_item import SourceItem
from app.dialogs.motion_preview_media import MotionPreviewMedia
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.audio_reaction_cache import AudioReactionCache
from app.animation.keyframes import Keyframe
from app.dialogs.motion_timeline import MotionTimeline


class MediaStabilityTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def item(self):
        source = Source(SourceType.IMAGE, "Video clone")
        item = SourceItem(source)
        source.source_type = SourceType.VIDEO
        item._video_validate_timestamps = True
        item._video_timeline_preview_active = True
        item._video_last_requested_target_ms = 1000
        self.addCleanup(item.release_video_decoder)
        return item

    def image(self, color):
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        return image

    def test_delayed_filter_from_before_seek_or_close_cannot_publish(self):
        item = self.item()
        old = item._video_filter_key()
        item._video_filter_busy = True
        item._pixmap = QPixmap.fromImage(self.image("red"))
        item._invalidate_video_seek_frame()
        revision = item.video_preview_revision
        self.assertTrue(item._pixmap.isNull())
        self.assertEqual(item.video_frame_state, "loading")
        item._video_filter_finished(old, self.image("red"))
        self.assertEqual(item.video_preview_revision, revision)
        self.assertTrue(item.video_preview_frame().isNull())
        current = item._video_filter_key()
        item._video_filter_finished(current, self.image("green"))
        self.assertEqual(item.video_frame_state, "synced")
        item.release_video_decoder()
        item._video_filter_finished(current, self.image("red"))
        self.assertTrue(item.video_preview_frame().isNull())

    def test_old_timestamp_and_unknown_time_rejected_before_filtering(self):
        item = self.item()
        item._invalidate_video_seek_frame()
        stale = QVideoFrame(self.image("red"))
        stale.setStartTime(100_000)
        latest = QVideoFrame(self.image("green"))
        latest.setStartTime(1_000_000)
        with patch.object(item, "_start_next_video_filter") as filtering:
            item._video_frame_changed(stale)
            filtering.assert_not_called()
            self.assertEqual(item.video_frame_state, "loading")
            item._video_frame_changed(QVideoFrame(self.image("red")))
            self.assertEqual(item.video_frame_state, "unsupported")
            item._video_frame_changed(latest)
            filtering.assert_called_once()
            image, generation = item._video_pending_frame
            item._video_pending_frame = None
        item._video_filter_finished(generation, image.toImage())
        self.assertEqual(item.video_preview_frame().pixelColor(0, 0), QColor("green"))
        self.assertEqual(item.video_frame_state, "synced")
        item._video_last_requested_target_ms = 1500
        self.assertEqual(item.video_frame_state, "loading")

    def test_loop_and_latest_pending_seek_invalidate_prior_generation(self):
        item = self.item()
        player = Mock()
        item._video_player = player
        old = item._video_filter_key()
        item._apply_video_seek(0)
        self.assertNotEqual(old, item._video_filter_key())
        self.assertTrue(item._video_frame_waiting)
        item._video_player = None

    def test_slow_filter_is_revalidated_after_playback_advances_without_seek(self):
        item = self.item()
        frame = QVideoFrame(self.image("red"))
        frame.setStartTime(1_000_000)
        with patch.object(item, "_start_next_video_filter"):
            item._video_frame_changed(frame)
        image, generation = item._video_pending_frame
        item._video_pending_frame = None
        revision = item.video_preview_revision
        item._video_last_requested_target_ms = 1500
        item._video_filter_finished(generation, image.toImage())
        self.assertEqual(item.video_preview_revision, revision)
        self.assertTrue(item.video_preview_frame().isNull())

    def test_proxy_and_multiple_source_status_do_not_report_loading_as_synced(self):
        first, second = self.item(), self.item()
        media = MotionPreviewMedia([first, second], {}, None)
        self.addCleanup(media.close)
        media.states = {first.source.id: "active", second.source.id: "unsupported"}
        first._video_frame_waiting = True
        self.assertIn("loading", media.status_text(False))
        self.assertIn("unsupported", media.status_text(False))
        self.assertNotIn("synchronized", media.status_text(False))
        media.states[first.source.id] = "still"
        self.assertIn("정지 화면", media.status_text(True))

    def test_proxy_requests_and_fast_scrubs_keep_latest_position_for_each_source(self):
        with TemporaryDirectory() as raw:
            path, proxy = Path(raw)/"video.mp4", Path(raw)/"proxy.mp4"
            path.touch()
            proxy.touch()
            sources = [Source(SourceType.VIDEO, f"Video {i}", video_paths=[str(path)]) for i in range(2)]
            items = [Mock(source=source) for source in sources]
            for item in items:
                item.isVisible.return_value = True
            media = MotionPreviewMedia(items, {str(path): 2}, None)
            self.addCleanup(media.close)
            media.proxy_cache.cached_path = Mock(return_value=proxy)
            track = PlaylistTrack("", "A", duration_seconds=10)
            for local in (.1, 1.8, .2, 1.0):
                media.sync(track, local, local, 0, playing=True)
            for item in items:
                self.assertEqual(media.last_requests[item.source.id], (str(path), 1.0))
                self.assertEqual(item.set_video_preview_position.call_args.args, (str(proxy), 1.0))
            media.sync(track, .1, .1, 0, playing=True)
            self.assertTrue(items[0].set_video_preview_position.call_args.kwargs["force_seek"])
            last = dict(media.last_requests)
            media.sync(track, 1, 1, 0, enabled=False)
            self.assertEqual(media.last_requests, last)
            self.assertIn("still", media.status_text(False))

    def test_marker_cache_tracks_geometry_selection_and_enabled_fields(self):
        timeline = MotionTimeline()
        self.addCleanup(timeline.deleteLater)
        timeline.resize(500, 38)
        timeline.points = (Keyframe(0, 0), Keyframe(.5, 0), Keyframe(1, 0))
        timeline.selected = .1
        original = timeline.marker_pixmap().cacheKey()
        timeline.progress = timeline.selected = .2
        self.assertEqual(timeline.marker_pixmap().cacheKey(), original)
        for change in (lambda: setattr(timeline, "selected", .5),
                       lambda: timeline.resize(600, 38),
                       lambda: setattr(timeline, "maximum", 1.5),
                       lambda: setattr(timeline, "enabled_track", False),
                       lambda: setattr(timeline, "points", (Keyframe(0, 0), Keyframe(.8, 0), Keyframe(1, 0)))):
            previous = timeline.marker_pixmap().cacheKey()
            change()
            self.assertNotEqual(timeline.marker_pixmap().cacheKey(), previous)


class ReactionCacheStabilityTests(TestCase):
    def test_file_change_track_identity_and_reanalysis_never_mix_envelopes(self):
        cache = AudioReactionCache()
        with TemporaryDirectory() as raw:
            path = Path(raw) / "song.wav"
            path.write_bytes(b"first")
            a, b = (PlaylistTrack(str(path), name, duration_seconds=10) for name in ("A", "B"))
            cache.store(a, 30, {"bass": [1]})
            self.assertIsNone(cache.get(b))
            cache.store(a, 60, {"bass": [2]})
            self.assertEqual(cache.get(a), (60, [2], {}))
            path.write_bytes(b"changed audio")
            self.assertIsNone(cache.get(a))
            cache.store(a, 30, {"bass": [3]})
            cache.store(b, 30, {"bass": [4]})
            self.assertLessEqual(len(cache.entries), 2)
            self.assertEqual(cache.get(a)[1], [3])
            path.unlink()
            self.assertIsNone(cache.get(a))
