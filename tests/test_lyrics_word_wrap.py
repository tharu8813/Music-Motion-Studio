"""Independent lyric fitting, whole-word wrapping and timed-row mapping."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QPushButton

from app.canvas.source_item import SourceItem
from app.canvas.live_canvas import CanvasScene
from app.canvas.renderers.lyrics_renderer import lyric_wrapped_rows, paint_lines
from app.inspector.editors.lyrics_editor import LyricsSection
from app.inspector.source_inspector import SourceInspector
from app.models.source import Source, SourceType
from app.models.source_components import LyricsComponent
from app.models.playlist import PlaylistTrack
from app.preview.canvas_snapshot import CanvasSnapshot, _lyric_cue_line_count
from app.services.source_store import SourceStore
from app.utils.i18n import Translator


class LyricsWordWrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def source(self, **options):
        values = dict(width=220, height=420, font_size=24, subtitle_current_scale=1,
                      subtitle_animation="none", subtitle_auto_wrap=True,
                      subtitle_previous_blur=0, subtitle_context_lines=0,
                      subtitle_next_lines=0, subtitle_current_line=0,
                      text="One two three four five six seven eight nine")
        values.update(options)
        return Source(SourceType.LYRICS, "Lyrics", **values)

    def draw(self, item):
        image = QImage(int(item.source.width), int(item.source.height), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        recorded = []
        draw = item._draw_text
        def observe(painter, rect, flags, text):
            recorded.append((text, painter.worldTransform().m11(), rect.center().y(), painter.pen().color().name()))
            draw(painter, rect, flags, text)
        try:
            with patch.object(item, "_draw_text", side_effect=observe):
                paint_lines(item, painter, QRectF(0, 0, item.source.width, item.source.height))
        finally:
            painter.end()
        return recorded

    def test_words_korean_emoji_and_offsets_are_preserved(self):
        item = SourceItem(self.source())
        for text in ("One two three four five six seven", "긴 문장을 단어 단위로 나누어 표시합니다", "hello 🌙 world again"):
            rows = lyric_wrapped_rows(item, text)
            self.assertGreater(len(rows), 1)
            self.assertEqual(" ".join(row for row, _ in rows), text)
            for row, offset in rows:
                self.assertEqual(text[offset:offset + len(row)], row)
        word = "AReallyLongUnbrokenWordThatExceedsTheAvailableWidth"
        self.assertEqual(lyric_wrapped_rows(item, word), ((word, 0),))

    def test_custom_width_is_independent_and_element_width_tracks_resize(self):
        item = SourceItem(self.source(subtitle_wrap_to_source=False, subtitle_wrap_width=180))
        before = lyric_wrapped_rows(item, item.source.text)
        item.source.width = 4000
        self.assertEqual(lyric_wrapped_rows(item, item.source.text), before)
        item.source.subtitle_wrap_to_source = True
        self.assertEqual(len(lyric_wrapped_rows(item, item.source.text)), 1)
        item.source.width = 180
        self.assertGreater(len(lyric_wrapped_rows(item, item.source.text)), 1)

    def test_fitting_and_wrapping_switch_independently(self):
        for shrink in (False, True):
            for wrap in (False, True):
                with self.subTest(shrink=shrink, wrap=wrap):
                    item = SourceItem(self.source(subtitle_auto_shrink=shrink, subtitle_auto_wrap=wrap))
                    rows = self.draw(item)
                    self.assertEqual(len(rows) > 1, wrap)
                    if not shrink:
                        self.assertTrue(all(abs(scale - 1) < .001 for _, scale, _, _ in rows))
                    elif not wrap:
                        self.assertLess(rows[0][1], 1)
        item = SourceItem(self.source(subtitle_auto_shrink=True, text="X" * 40))
        self.assertEqual(len(self.draw(item)), 1)
        self.assertLess(self.draw(item)[0][1], 1)

    def test_wrapped_karaoke_uses_original_character_positions(self):
        item = SourceItem(self.source(subtitle_karaoke_enabled=True))
        item._subtitle_cue_layout = ((0, 0, 1),)
        item._subtitle_word_reveal = (10.5,)
        expected = lyric_wrapped_rows(item, item.source.text)
        with patch("app.canvas.renderers.lyrics_renderer.paint_word_reveal") as reveal:
            self.draw(item)
        self.assertEqual([(call.args[4], call.args[5]) for call in reveal.call_args_list],
                         [(text, max(0, 10.5 - offset)) for text, offset in expected])

    def test_wrapped_rows_retain_original_line_styles_and_auto_context_cost(self):
        source = self.source(text="One two three four five six\nTranslated line also wraps",
                             subtitle_line_styles=[{"color": "#FF0000"}, {"color": "#0000FF"}],
                             subtitle_current_line_count=2)
        item = SourceItem(source)
        item._subtitle_cue_layout = ((0, 0, 2), (0, 1, 2))
        item._subtitle_line_style_indices = (0, 1)
        rows = self.draw(item)
        first = len(lyric_wrapped_rows(item, source.text.splitlines()[0], 0))
        self.assertTrue(all(row[3] == "#ff0000" for row in rows[:first]))
        self.assertTrue(all(row[3] == "#0000ff" for row in rows[first:]))
        self.assertEqual(_lyric_cue_line_count({"text": source.text}, item), len(rows))
        self.assertEqual(source.text.count("\n"), 1)

    def test_saved_settings_validation_and_legacy_defaults(self):
        source = self.source(subtitle_auto_shrink=False, subtitle_wrap_to_source=False, subtitle_wrap_width=333)
        restored = Source.from_dict(source.to_dict())
        self.assertEqual(LyricsComponent.from_source(source), LyricsComponent.from_source(restored))
        legacy = source.to_dict()
        for key in ("subtitle_auto_shrink", "subtitle_auto_wrap", "subtitle_wrap_to_source", "subtitle_wrap_width"):
            legacy.pop(key)
        restored = Source.from_dict(legacy)
        self.assertTrue(restored.subtitle_auto_shrink)
        self.assertFalse(restored.subtitle_auto_wrap)
        for changes in ({"subtitle_wrap_width": 0}, {"subtitle_auto_wrap": "yes"}):
            with self.assertRaises(ValueError):
                Source.from_dict(replace(source, **changes).to_dict())

    def test_auto_wrapped_rows_omit_gap_but_original_newlines_keep_it(self):
        text = "One two three four five six\nSeven eight nine ten eleven twelve"
        for direction in ("up", "down", "left", "right"):
            with self.subTest(direction=direction):
                source = self.source(text=text, subtitle_line_spacing=28,
                                     subtitle_auto_shrink=False, subtitle_current_line_count=2,
                                     subtitle_flow_direction=direction)
                item = SourceItem(source)
                item._subtitle_cue_layout = ((0, 0, 2), (0, 1, 2))
                item._subtitle_line_style_indices = (0, 1)
                first_count = len(lyric_wrapped_rows(item, text.splitlines()[0]))
                rows = self.draw(item)
                self.assertGreater(first_count, 1)
                height = item._lyric_line_height()
                for index in range(1, len(rows)):
                    gap = rows[index][2] - rows[index - 1][2]
                    expected = height if index == first_count else height - source.subtitle_line_spacing
                    self.assertAlmostEqual(gap, expected)

    def test_original_two_line_lyrics_keep_positions_with_wrap_enabled(self):
        original_rows = []
        for wrap in (False, True):
            source = self.source(text="First\nSecond", width=800, subtitle_auto_wrap=wrap,
                                 subtitle_line_spacing=30, subtitle_current_line_count=2)
            item = SourceItem(source)
            item._subtitle_cue_layout = ((0, 0, 2), (0, 1, 2))
            item._subtitle_anchor_line = 0
            item._subtitle_anchor_line_count = 2
            original_rows.append(self.draw(item))
        self.assertEqual(original_rows[0], original_rows[1])

    def test_timed_transitions_capture_at_preview_and_export_sizes_without_mutation(self):
        track = PlaylistTrack("", "Song", duration_seconds=8, lyrics=[
            {"start": 0, "end": 2, "text": "First line needs several wrapped rows"},
            {"start": 2, "end": 4, "text": "Second line also wraps\n두 번째 번역 줄도 나누어 표시합니다"},
            {"start": 4, "end": 8, "text": "The final line of this song"},
        ])
        for direction in ("up", "down", "left", "right"):
            source = self.source(subtitle_flow_direction=direction, subtitle_animation="glow",
                                 subtitle_context_lines=-1, subtitle_next_lines=-1)
            scene = CanvasScene()
            scene.set_artboard_size(source.width, source.height)
            item = SourceItem(source)
            scene.addItem(item)
            original = source.to_dict()
            for seconds in (0, 1.9, 2.01, 2.3, 4.01, 7.9):
                for scale in (1, 2):
                    with self.subTest(direction=direction, seconds=seconds, scale=scale):
                        frame = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                            elapsed_seconds=seconds, transparent=True,
                            output_scale=scale)
                        self.assertFalse(frame.isNull())
                        self.assertEqual(source.to_dict(), original)
                        self.assertEqual(item._subtitle_cue_layout, ())

    def test_inspector_exposes_independent_options_and_custom_width(self):
        def spin(low, high, step):
            widget = QDoubleSpinBox()
            widget.setRange(low, high)
            widget.setSingleStep(step)
            return widget
        section = LyricsSection(spin, QPushButton)
        section.retranslate(True)
        section.fill(self.source(subtitle_wrap_to_source=False), set_color=lambda widget, value: None)
        self.assertFalse(section.widgets["subtitle_wrap_to_source"].currentData())
        self.assertTrue(section.hidden_when_off(self.source(subtitle_wrap_to_source=False))["subtitle_wrap_width"])
        self.assertFalse(section.hidden_when_off(self.source())["subtitle_wrap_width"])
        self.assertFalse(section.hidden_when_off(self.source(subtitle_auto_wrap=False))["subtitle_wrap_to_source"])

    def test_inspector_updates_source_and_hides_lyric_settings_for_other_elements(self):
        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.deleteLater)
        self.addCleanup(inspector.close)
        source = self.source(subtitle_auto_wrap=False)
        store.add(source)
        inspector.lyrics.widgets["subtitle_auto_wrap"].click()
        self.assertTrue(source.subtitle_auto_wrap)
        self.assertTrue(inspector._field_visibility["subtitle_wrap_to_source"])
        inspector.lyrics.widgets["subtitle_wrap_to_source"].setCurrentIndex(1)
        self.assertFalse(source.subtitle_wrap_to_source)
        self.assertTrue(inspector._field_visibility["subtitle_wrap_width"])
        inspector.lyrics.widgets["subtitle_wrap_width"].setValue(320)
        self.assertEqual(source.subtitle_wrap_width, 320)
        store.add(Source(SourceType.TEXT, "Text"))
        self.assertFalse(inspector._field_visibility["subtitle_auto_wrap"])
        self.assertFalse(inspector._field_visibility["subtitle_auto_shrink"])


if __name__ == "__main__":
    unittest.main()
