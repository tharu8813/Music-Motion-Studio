"""Exact rendering and bounded resource reuse for lyric typography."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFontMetricsF, QImage, QPainter
from app.canvas.renderers import lyrics_renderer
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.models.playlist import PlaylistTrack
from app.preview.canvas_snapshot import CanvasSnapshot
from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
from app.utils.i18n import Translator
from app.services.lyrics_translation_service import TranslationConfig, apply_translations


def uncached_font(item, size, weight, italic, family=""):
    font = item.text_font(size, weight)
    if family:
        font.setFamily(family)
    font.setItalic(italic)
    return font


def uncached_height(item):
    regular = QFontMetricsF(item._lyric_fonts['regular'])
    current = QFontMetricsF(item._lyric_fonts['current'])
    height = max(regular.height(), current.height())
    if item.source.subtitle_translation_enabled:
        style = item.source.subtitle_translation_style
        size = float(style.get('font_size', item.source.font_size + float(style.get('font_size_offset', 0))))
        font = uncached_font(item, round(max(8, min(120, size)) * item.source.subtitle_current_scale),
            int(style.get('font_weight', item.source.font_weight)), bool(style.get('italic', False)),
            str(style.get('font_family', '')))
        height = max(height, QFontMetricsF(font).height())
    return max(16.0, height + max(0.0, float(item.source.subtitle_line_spacing))
               + max(2.0, max(regular.descent(), current.descent()) * 0.35))


class LyricsRenderCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_canvas_word_reveal_accepts_scalar_and_tuple_qtextline_coordinates(self):
        # Track details already supports both PySide6 cursorToX variants.
        # Canvas lyric preview/export used to index [0] unconditionally.
        text = "안녕 🎵 world"
        for cursor in (0, 1, 3, 4, 6):
            class FakeLine:
                def __init__(self, as_tuple):
                    self.as_tuple = as_tuple

                def cursorToX(self, pos):
                    x = float(pos) * 7.0
                    return (x, pos) if self.as_tuple else x

            self.assertEqual(
                lyrics_renderer._qtextline_cursor_x(FakeLine(False), cursor),
                lyrics_renderer._qtextline_cursor_x(FakeLine(True), cursor),
            )

        snapshots = []
        for as_tuple in (False, True):
            class FakeLine:
                def setLineWidth(self, width):
                    pass

                def cursorToX(self, pos):
                    x = float(pos) * 7.0
                    return (x, pos) if as_tuple else x

            class FakeLayout:
                def __init__(self, text, font):
                    pass

                def setTextOption(self, option):
                    pass

                def beginLayout(self):
                    pass

                def createLine(self):
                    return FakeLine()

                def endLayout(self):
                    pass

            item = SourceItem(Source(
                SourceType.LYRICS, "Karaoke", width=200, height=80,
                font_size=22,
            ))
            image = QImage(200, 80, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                painter.setFont(item.text_font(22, 500))
                with patch.object(lyrics_renderer, "QTextLayout", FakeLayout):
                    lyrics_renderer.paint_word_reveal(
                        item, painter, QRectF(0, 0, 200, 80),
                        Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextSingleLine,
                        text, 0.0, highlight_ranges=((0.0, 4.5),),
                    )
            finally:
                painter.end()
            snapshots.append(bytes(image.constBits()))
            self.assertTrue(item._lyric_reveal_cache)
        self.assertEqual(snapshots[0], snapshots[1])

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
                        subtitle_translation_style={'font_family': 'Malgun Gothic', 'font_size': 26,
                                                    'font_weight': 400, 'color': '#B8C4D4'},
                        subtitle_line_styles=[{'font_size': 28, 'italic': True}])
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack('', 'Song', duration_seconds=8, lyrics=[
            {'start': i*2, 'end': i*2+1.8, 'text': '안녕 world 🌙\nSecond line', 'format': 'ttml',
             'words': [{'start': i*2, 'end': i*2+1.7, 'text': '안녕 world 🌙', 'offset': 0}]}
            for i in range(3)])
        track.lyrics = apply_translations(track.lyrics, ['번역 가사\n둘째 줄'] * 3, TranslationConfig('google'))
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
