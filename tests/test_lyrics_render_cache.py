"""Exact rendering and bounded resource reuse for lyric typography."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontMetricsF
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.models.playlist import PlaylistTrack
from app.preview.canvas_snapshot import CanvasSnapshot
from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
from app.utils.i18n import Translator


def uncached_font(item, size, weight, italic):
    font = item.text_font(size, weight)
    font.setItalic(italic)
    return font


def uncached_height(item):
    regular = QFontMetricsF(item._lyric_fonts['regular'])
    current = QFontMetricsF(item._lyric_fonts['current'])
    return max(16.0, max(regular.height(), current.height()) + max(0.0, float(item.source.subtitle_line_spacing))
               + max(2.0, max(regular.descent(), current.descent()) * 0.35))


class LyricsRenderCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_metrics_are_reused_and_property_changes_invalidate_resources(self):
        item = SourceItem(Source(SourceType.LYRICS, 'Lyrics'))
        font = item._lyric_font(30, 600, False)
        with patch('app.canvas.source_item.QFontMetricsF', wraps=QFontMetricsF) as metrics:
            values = [item._lyric_text_advance(font, '안녕 world 🌙') for _ in range(30)]
            self.assertEqual(metrics.call_count, 1)
        self.assertEqual(len(set(values)), 1)
        changed = item._lyric_font(30, 600, False)
        changed.setItalic(True)
        self.assertFalse(item._lyric_font(30, 600, False).italic())
        for size in range(8, 121):
            item._lyric_font(size, 400, False)
        self.assertLessEqual(len(item._lyric_font_cache), 64)
        for index in range(1000):
            item._lyric_text_advance(font, f'Line {index}')
        self.assertLessEqual(len(item._lyric_advance_cache), 256)
        item.source.font_size += 5
        item.apply_source()
        self.assertEqual(item._lyric_advance_cache, {})
        self.assertEqual(item._lyric_font_cache, {})
        self.assertEqual(item._lyric_line_height(), uncached_height(item))
        for property_name, value in (('text_letter_spacing', 2.5), ('text_case', 'upper'),
                                     ('font_weight', 700), ('subtitle_line_spacing', 18)):
            item._lyric_font(30, 600, False)
            setattr(item.source, property_name, value)
            item.apply_source()
            self.assertEqual(item._lyric_font_cache, {}, property_name)
            self.assertEqual(item._lyric_line_height(), uncached_height(item))

    def test_cached_and_uncached_frames_are_exact_at_preview_and_export_scales(self):
        scene = CanvasScene()
        scene.set_artboard_size(640, 360)
        source = Source(SourceType.LYRICS, 'Lyrics', width=620, height=340, font_size=22,
                        subtitle_karaoke_enabled=True, subtitle_animation='glow',
                        subtitle_previous_blur=3, subtitle_line_spacing=8,
                        subtitle_line_styles=[{'font_size': 28, 'italic': True}])
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack('', 'Song', duration_seconds=8, lyrics=[
            {'start': i*2, 'end': i*2+1.8, 'text': '안녕 world 🌙\nSecond line', 'format': 'ttml',
             'words': [{'start': i*2, 'end': i*2+1.7, 'text': '안녕 world 🌙', 'offset': 0}]}
            for i in range(3)])
        try:
            for scale in (0.5, 1.5):
                for seconds in (0.4, 2.05, 2.4, 5.0):
                    kwargs = dict(elapsed_seconds=seconds, output_scale=scale, transparent=True)
                    cached = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, **kwargs)
                    with patch.object(SourceItem, '_lyric_font', uncached_font), \
                         patch.object(SourceItem, '_lyric_text_advance', lambda _, f, t: QFontMetricsF(f).horizontalAdvance(t)), \
                         patch.object(SourceItem, '_lyric_line_height', uncached_height):
                        uncached = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, **kwargs)
                    self.assertEqual(bytes(cached.constBits()), bytes(uncached.constBits()), (scale, seconds))
        finally:
            scene.clear()

    def test_effective_summary_and_preview_count_do_not_modify_video_settings(self):
        source = Source(SourceType.LYRICS, 'Lyrics', subtitle_animation='rise',
                        subtitle_advanced_categories=['motion'],
                        subtitle_advanced_settings={'subtitle_animation': 'glow'})
        original = deepcopy(source.to_dict())
        dialog = LyricsAnimationDialog(source, Translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        settings = deepcopy(dialog.settings())
        dialog.preview_count.setValue(13)
        self.assertEqual(dialog.settings(), settings)
        self.assertEqual(source.to_dict(), original)
        self.assertIn('글로우' if dialog._korean else 'Glow', dialog.effective_summary.text())
        dialog.category_toggles['motion'].setChecked(False)
        self.assertIn('라이즈' if dialog._korean else 'Rise', dialog.effective_summary.text())
        self.assertEqual(source.to_dict(), original)
