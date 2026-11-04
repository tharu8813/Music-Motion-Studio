import os
import threading
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.video.frame_mailbox import VideoFrameMailbox


class VideoFrameMailboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    @staticmethod
    def frame(timestamp):
        frame = QVideoFrame(QVideoFrameFormat(QSize(32, 32), QVideoFrameFormat.PixelFormat.Format_RGBA8888))
        frame.setStartTime(timestamp)
        return frame

    def test_decoder_burst_queues_one_gui_wakeup_and_converts_latest_only(self):
        item = SourceItem(Source(SourceType.SHAPE, "No live decoder"))
        mailbox = VideoFrameMailbox()
        item._video_frame_mailbox = mailbox
        item._video_frame_changed = Mock()
        mailbox.available.connect(item._consume_video_frame, Qt.ConnectionType.QueuedConnection)
        producer_thread = []
        def produce():
            producer_thread.append(threading.get_ident())
            for index in range(1000):
                mailbox.offer(self.frame(index * 1000))
        producer = threading.Thread(target=produce)
        producer.start()
        producer.join()
        self.assertNotEqual(producer_thread[0], threading.get_ident())
        self.assertEqual(item._video_frame_changed.call_count, 0)
        self.application.processEvents()
        self.assertEqual(item._video_frame_changed.call_count, 1)
        self.assertEqual(item._video_frame_changed.call_args.args[0].startTime(), 999000)
        self.assertEqual(item.video_decoder_stats().pressure_drops, 999)
        self.assertEqual(item.video_decoder_stats().dropped_frames, 999)
        mailbox.offer(self.frame(1000000))
        self.application.processEvents()
        self.assertEqual(item._video_frame_changed.call_count, 2)
        mailbox.close()

    def test_end_of_stream_keeps_final_valid_frame(self):
        mailbox = VideoFrameMailbox()
        mailbox.offer(self.frame(44000))
        mailbox.offer(QVideoFrame())
        frame, drops = mailbox.take()
        self.assertEqual(frame.startTime(), 44000)
        self.assertEqual(drops, 0)
        self.assertEqual(mailbox.take(), (None, 0))

    def test_seek_reuses_pending_wakeup_without_delivering_preseek_frame(self):
        item = SourceItem(Source(SourceType.SHAPE, "No live decoder"))
        mailbox = VideoFrameMailbox()
        item._video_frame_mailbox = mailbox
        wakeups = QSignalSpy(mailbox.available)
        mailbox.offer(self.frame(44000))
        item._invalidate_video_seek_frame()
        mailbox.offer(self.frame(250000))
        self.assertEqual(wakeups.count(), 1)
        frame, _ = mailbox.take()
        self.assertEqual(frame.startTime(), 250000)

    def test_release_discards_pending_and_late_decoder_frames(self):
        item = SourceItem(Source(SourceType.SHAPE, "No live decoder"))
        mailbox = VideoFrameMailbox()
        item._video_frame_mailbox = mailbox
        item._video_frame_changed = Mock()
        mailbox.available.connect(item._consume_video_frame, Qt.ConnectionType.QueuedConnection)
        mailbox.offer(self.frame(12000))
        item.release_video_decoder()
        mailbox.offer(self.frame(13000))
        self.application.processEvents()
        self.assertEqual(mailbox.take(), (None, 0))
        item._video_frame_changed.assert_not_called()

    def test_release_clears_media_and_detaches_sink_before_returning(self):
        item = SourceItem(Source(SourceType.SHAPE, "Decoder teardown"))
        player = item._ensure_video_decoder()
        player.setSource(QUrl.fromLocalFile("missing-preview-fixture.mp4"))
        self.assertFalse(player.source().isEmpty())
        item.release_video_decoder()
        self.assertTrue(player.source().isEmpty())
        self.assertIsNone(player.videoOutput())
        self.assertIsNone(item._video_player)


if __name__ == "__main__":
    unittest.main()
