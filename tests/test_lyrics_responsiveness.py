"""Checks for lyric frame reuse and editing without repeated whole-view work."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, QSizeF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from app.canvas.source_item import SourceItem
import app.canvas.source_item as source_module
from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog
from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.frame_state import resolve_lyrics_intro_state
from app.services.lyrics_service import LyricsError, LyricsService
from app.utils.i18n import Translator


class LyricsResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, dialog):
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        return dialog

    def test_intro_does_not_scan_unused_or_later_cues(self):
        class FirstOnly(list):
            def __iter__(self):
                yield self[0]
                raise AssertionError("Traversed later cues")

        track = PlaylistTrack("", "Track", lyrics=FirstOnly([
            {"start": 5, "end": 10, "text": "First"},
            {"start": 10, "end": 15, "text": "Second"},
        ]))
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_intro_enabled=True)
        self.assertTrue(resolve_lyrics_intro_state(track, source, 1).before_first)
        source.subtitle_intro_enabled = False
        track.lyrics = [None]  # Disabled intros do not inspect cue contents.
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 1))

    def test_cached_blur_reuses_coverage_and_preserves_exact_pixels(self):
        item = SourceItem(Source(SourceType.LYRICS, "Lyrics", font_size=24))
        font = item._lyric_fonts["current"]
        flags = int(Qt.AlignmentFlag.AlignCenter)
        for alpha in (255, 128):
            with patch.object(source_module, "gaussian_filter", wraps=source_module.gaussian_filter) as blur:
                for color in (QColor(220, 50, 90, alpha), QColor(20, 190, 255, alpha)):
                    pixmap, margin = item._lyric_blur_pixmap("가사 🌙 gyp", color, 4.3, 380, 70, 1.5, flags)

                    def paint(painter):
                        painter.setPen(color)
                        painter.setFont(font)
                        painter.drawText(QRectF(0, 0, 380, 70), flags, "가사 🌙 gyp")

                    reference, reference_margin = SourceItem._blurred_raster(
                        paint, QSizeF(380, 70), 4.3, 1.5, solid_color=color)
                    self.assertEqual(margin, reference_margin)
                    self.assertEqual(pixmap.toImage(), reference.toImage())
                self.assertEqual(blur.call_count, 3)  # One cached mask plus two references.
        item.source.font_size = 30
        item.apply_source()
        self.assertFalse(item._lyric_coverage_cache)

    def test_blur_caches_are_bounded_without_clearing_all_rows(self):
        item = SourceItem(Source(SourceType.LYRICS, "Lyrics", font_size=12))
        for index in range(110):
            item._lyric_blur_pixmap(str(index), QColor("white"), 1, 80, 30, 1,
                                    int(Qt.AlignmentFlag.AlignCenter))
        self.assertEqual(len(item._lyric_ghost_cache), 96)
        self.assertEqual(len(item._lyric_coverage_cache), 48)
        self.assertLessEqual(sum(mask[0].nbytes for mask in item._lyric_coverage_cache.values()), 16 * 1024 * 1024)

    def test_active_blur_survives_cache_pressure_from_changing_rows(self):
        item = SourceItem(Source(SourceType.LYRICS, "Lyrics", font_size=12))
        flags = int(Qt.AlignmentFlag.AlignCenter)
        hot, _ = item._lyric_blur_pixmap("Current", QColor("white"), 1, 80, 30, 1, flags)
        with patch.object(item, "_blurred_raster", wraps=item._blurred_raster) as raster:
            for index in range(110):
                item._lyric_blur_pixmap(str(index), QColor("white"), 1, 80, 30, 1, flags)
                cached, _ = item._lyric_blur_pixmap("Current", QColor("white"), 1, 80, 30, 1, flags)
                self.assertEqual(hot.cacheKey(), cached.cacheKey())
        self.assertEqual(raster.call_count, 110)
        self.assertEqual(len(item._lyric_ghost_cache), 96)

    def test_inplace_blur_color_conversion_preserves_rounding_and_clipping(self):
        import numpy as np
        from app.video.frame_filter import _rgb_pixels

        expected = []
        def averaged(pixels, **_kwargs):
            values = np.linspace(-0.5, 255.5, pixels.size, dtype=np.float32).reshape(pixels.shape)
            values[:, :, 3] = 255
            expected.append(np.clip(values + 0.5, 0, 255).astype(np.uint8))
            return values

        with patch.object(source_module, "gaussian_filter", side_effect=averaged):
            pixmap, _ = SourceItem._blurred_raster(lambda _painter: None, QSizeF(40, 20), 1, 1)
        image = pixmap.toImage()
        np.testing.assert_array_equal(_rgb_pixels(image), expected[0])

    def test_rapid_settings_render_once_and_pause_keeps_comparison_time(self):
        source = Source(SourceType.LYRICS, "Lyrics")
        original = source.to_dict()
        dialog = self.dialog(LyricsAnimationDialog(source, Translator()))
        dialog.show()
        self.app.processEvents()
        dialog.category_toggles["motion"].setChecked(True)
        dialog.pause.setChecked(True)
        seconds = dialog._preview_seconds()
        with patch.object(dialog, "_render_preview", wraps=dialog._render_preview) as render:
            for value in range(21, 41):
                dialog.controls["subtitle_motion_distance"].setValue(value)
            self.app.processEvents()
            render.assert_called_once_with(seconds)
        self.assertFalse(dialog._timer.isActive())
        self.assertEqual(dialog._preview_seconds(), seconds)
        self.assertEqual(dialog._preview_source.subtitle_motion_distance, 40)
        dialog.tabs.setCurrentIndex(2)
        self.assertTrue(dialog.pause.isChecked())
        self.assertEqual(dialog._preview_seconds(), seconds)
        dialog.pause.setChecked(False)
        self.assertTrue(dialog._timer.isActive())
        self.assertGreaterEqual(dialog._preview_seconds(), seconds)
        dialog.controls["subtitle_motion_distance"].setValue(41)
        dialog.reject()
        self.assertFalse(dialog._refresh_timer.isActive())
        self.assertFalse(dialog._timer.isActive())
        self.assertEqual(source.to_dict(), original)

    def test_large_preview_captures_only_display_resolution(self):
        dialog = self.dialog(LyricsAnimationDialog(Source(SourceType.LYRICS, "Lyrics"), Translator()))
        dialog.category_toggles["layout"].setChecked(True)
        dialog.preview_count.setValue(13)
        direction = dialog.controls["subtitle_flow_direction"]
        direction.setCurrentIndex(direction.findData("left"))
        self.assertLess(dialog._preview_scale(), 1)
        dialog._render_preview(0.3)
        self.assertLessEqual(dialog.preview.pixmap().width(), dialog.preview.width())

    def test_timing_playback_only_updates_changed_marker_rows(self):
        dialog = self.dialog(LrcGeneratorDialog([], Translator()))
        dialog.lines = [f"Lyric {i}" for i in range(2000)]
        dialog.timestamps = [i * 2.0 for i in range(2000)]
        dialog.current_index = 51
        dialog._refresh_table()
        with patch.object(dialog.timeline_table, "item", wraps=dialog.timeline_table.item) as access:
            dialog._position_changed(100100)
            self.assertLess(access.call_count, 30)
            access.reset_mock()
            dialog._position_changed(100200)
            self.assertEqual(access.call_count, 0)
        self.assertIn("♪", dialog.timeline_table.item(50, 0).text())
        dialog._position_changed(104100)
        self.assertNotIn("♪", dialog.timeline_table.item(50, 0).text())
        self.assertEqual(dialog.timeline_table.item(50, 0).toolTip(), "")
        item = dialog.timeline_table.item(50, 2)
        dialog.timestamps[50] = 107.0  # Out-of-order times remain supported.
        dialog._refresh_table(50)
        self.assertIs(dialog.timeline_table.item(50, 2), item)
        self.assertEqual(dialog._playback_row_at(107000), 50)
        dialog.timestamps[49] = 107.0
        dialog._refresh_table()
        self.assertEqual(dialog._playback_row_at(107000), 50)
        dialog._position_changed(0)
        self.assertEqual(dialog._playback_highlight_row, 0)

    def test_preview_mode_gives_lyrics_room_and_restores_editing_tools(self):
        dialog = self.dialog(LrcGeneratorDialog([], Translator()))
        dialog.resize(1200, 760)
        dialog.show()
        dialog.pages.setCurrentIndex(2)
        self.app.processEvents()
        height = dialog.timeline_table.height()
        dialog.preview_mode_check.setChecked(True)
        self.app.processEvents()
        self.assertGreater(dialog.timeline_table.height(), height + 150)
        self.assertTrue(dialog.timing_tools_group.isHidden())
        self.assertTrue(dialog.record_button.isHidden())
        dialog.preview_mode_check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(dialog.timing_tools_group.isHidden())
        self.assertFalse(dialog.record_button.isHidden())
        dialog.done(dialog.DialogCode.Rejected)

    def test_utf16_lyrics_load_and_invalid_encoding_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lyrics.lrc"
            path.write_text("[00:01.00]가사\n[00:02.00]다음", encoding="utf-16")
            self.assertEqual(LyricsService.load(path)[0]["text"], "가사")
            path.write_bytes(b"[00:01.00]\x80\x81")
            with self.assertRaisesRegex(LyricsError, "UTF-8"):
                LyricsService.load(path)

    def test_invalid_import_retains_existing_lyrics(self):
        cues = [{"start": 1, "end": 4, "text": "Original"}]
        track = PlaylistTrack("", "Track", lyrics=cues, lyrics_path="original.lrc")
        dialog = self.dialog(TrackDetailsDialog(track, Translator()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.lrc"
            path.write_text("Untimed lyrics", encoding="utf-8")
            self.assertFalse(dialog._apply_lyrics_from_path(str(path)))
            self.assertEqual(dialog.selected_lyrics, cues)
            self.assertEqual(dialog.selected_lyrics_path, "original.lrc")
            path.write_bytes(b"\x80\x81")
            self.assertFalse(dialog._apply_lyrics_from_path(str(path)))
            self.assertEqual(dialog.selected_lyrics, cues)


if __name__ == "__main__":
    unittest.main()
