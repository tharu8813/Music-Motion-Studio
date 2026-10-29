"""A virtual lyric strip keeps wrapped neighbours attached while scrolling."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.canvas.renderers import lyrics_renderer
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot, _effective_lyric_context
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import RenderFrame
from app.ui.design_system import apply_studio_style


class LyricsContinuousStripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def scene(self, **options):
        settings = dict(width=460, height=360, font_size=24, fill_color="#00000000",
                        subtitle_auto_wrap=True, subtitle_wrap_to_source=False,
                        subtitle_wrap_width=220, subtitle_current_scale=1,
                        subtitle_animation="glow", subtitle_motion_easing="linear",
                        subtitle_animation_duration=.5, subtitle_line_spacing=28,
                        subtitle_context_lines=-1, subtitle_next_lines=-1,
                        subtitle_glow_radius=0, subtitle_glow_strength=0,
                        subtitle_previous_distance_fade=0, subtitle_next_distance_fade=0,
                        subtitle_previous_blur=0,
                        subtitle_role_styles={role: {"opacity": .8, "blur": 0, "scale": 1}
                                              for role in ("previous", "current", "next")})
        settings.update(options)
        source = Source(SourceType.LYRICS, "Lyrics", **settings)
        scene = CanvasScene()
        scene.set_artboard_size(source.width, source.height)
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("", "Wrapped lyrics", duration_seconds=30, lyrics=[
            {"start": 2 * index, "end": 2 * (index + 1), "text": text}
            for index, text in enumerate((
                "Opening", "First phrase that wraps into several rows", "Short",
                "A much longer phrase with more words for wrapping here",
                "Original\nTranslation", "Next", "Last phrase also wraps naturally", "End"))])
        return scene, item, track

    def observe(self, scene, item, track, time, scale=1):
        rows, windows = {}, []
        draw, paint = item._draw_text, lyrics_renderer.paint_lines

        def record(painter, rect, flags, text):
            rows[text] = (painter.worldTransform().mapRect(rect), painter.opacity())
            draw(painter, rect, flags, text)

        def layout(graphics, painter, rect):
            windows.append((graphics._subtitle_strip_window, graphics._subtitle_cue_layout))
            paint(graphics, painter, rect)

        with patch.object(item, "_draw_text", side_effect=record), \
                patch.object(lyrics_renderer, "paint_lines", side_effect=layout):
            frame = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                elapsed_seconds=time, transparent=True, output_scale=scale)
        self.assertIsNone(item._subtitle_strip_window)
        return rows, frame, windows[-1]

    def test_wrapped_neighbours_keep_positions_across_every_cue_boundary(self):
        for direction in ("up", "down"):
            for anchor in (.05, .5, .95):
                scene, item, track = self.scene(subtitle_flow_direction=direction, subtitle_anchor=anchor)
                for boundary in (2, 4, 6, 8, 10, 12, 14):
                    # Stay outside the resolver's 0.5 ms boundary tolerance.
                    before = self.observe(scene, item, track, boundary - .001)[0]
                    after = self.observe(scene, item, track, boundary)[0]
                    viewport = QRectF(0, 0, item.source.width, item.source.height)
                    checked = 0
                    for text, (rect, alpha) in before.items():
                        if rect.intersects(viewport):
                            checked += 1
                            with self.subTest(direction=direction, anchor=anchor, boundary=boundary, text=text):
                                self.assertIn(text, after)
                                self.assertAlmostEqual(rect.center().y(), after[text][0].center().y(), delta=.03)
                                self.assertAlmostEqual(alpha, after[text][1], delta=.001)
                    self.assertGreater(checked, 0)

    def test_all_rows_share_scroll_and_keep_compact_continuations(self):
        for direction in ("up", "down"):
            scene, item, track = self.scene(subtitle_flow_direction=direction)
            samples = [self.observe(scene, item, track, time)[0] for time in (6.01, 6.15, 6.3, 6.49)]
            common = set.intersection(*(set(rows) for rows in samples))
            self.assertGreater(len(common), 3)
            for first, second in zip(samples, samples[1:]):
                deltas = [second[text][0].center().y() - first[text][0].center().y() for text in common]
                self.assertLess(max(deltas) - min(deltas), .00001)
            parts = lyrics_renderer.lyric_wrapped_rows(item, track.lyrics[3]["text"])
            self.assertGreater(len(parts), 1)
            rows = samples[1]
            for (first, _), (second, _) in zip(parts, parts[1:]):
                self.assertAlmostEqual(rows[second][0].center().y() - rows[first][0].center().y(),
                                       item._lyric_line_height() - item.source.subtitle_line_spacing)

    def test_auto_context_includes_partial_long_cues_on_each_side(self):
        scene, item, track = self.scene(height=190, subtitle_anchor=.2)
        previous, following = _effective_lyric_context(item, track.lyrics, 2)
        self.assertGreaterEqual(previous, 1)
        self.assertGreaterEqual(following, 1)
        rows = self.observe(scene, item, track, 5)[0]
        for index in (1, 3):
            self.assertTrue(any(part in rows for part, _ in
                                lyrics_renderer.lyric_wrapped_rows(item, track.lyrics[index]["text"])))

    def test_manual_context_preserves_count_without_separate_entry_translation(self):
        scene, item, track = self.scene(subtitle_context_lines=1, subtitle_next_lines=1)
        before = self.observe(scene, item, track, 5.999)[0]
        start = self.observe(scene, item, track, 6)[0]
        middle = self.observe(scene, item, track, 6.25)[0]
        for text in set(before) & set(start):
            self.assertAlmostEqual(before[text][0].center().y(), start[text][0].center().y(), delta=.02)
        # A newly admitted context cue stays attached to the strip as it
        # passes through the explicit one-cue viewport edge.
        self.assertIn("Original", middle)
        long_rows = lyrics_renderer.lyric_wrapped_rows(item, track.lyrics[3]["text"])
        last = long_rows[-1][0]
        self.assertAlmostEqual(middle["Original"][0].center().y() - middle[last][0].center().y(),
                               item._lyric_line_height())
        settled = self.observe(scene, item, track, 7)[0]
        expected = {part for cue in track.lyrics[2:5] for line in cue["text"].splitlines()
                    for part, _ in lyrics_renderer.lyric_wrapped_rows(item, line)}
        self.assertEqual(set(settled), expected)

    def test_auto_context_blur_does_not_jump_when_virtual_window_changes(self):
        scene, item, track = self.scene(subtitle_previous_distance_fade=1,
                                       subtitle_next_distance_fade=1)
        item.source.subtitle_role_styles = {
            role: {"opacity": .8, "scale": 1, "blur": 0 if role == "current" else 6}
            for role in ("previous", "current", "next")}
        original = item._lyric_blur_pixmap
        for boundary in (4, 6, 8, 10, 12):
            samples = []
            for time in (boundary - .001, boundary):
                radii = {}
                def record(text, color, radius, *args):
                    radii[text] = max(radius, radii.get(text, 0))
                    return original(text, color, radius, *args)
                with patch.object(item, "_lyric_blur_pixmap", side_effect=record):
                    self.observe(scene, item, track, time)
                samples.append(radii)
            common = samples[0].keys() & samples[1].keys()
            self.assertTrue(common)
            for text in common:
                self.assertAlmostEqual(samples[0][text], samples[1][text], delta=.001)

    def test_virtual_window_is_bounded_and_keeps_two_blocks_on_each_side(self):
        scene, item, track = self.scene()
        track.lyrics = [{"start": index * 2, "end": (index + 1) * 2, "text": f"Cue {index}"}
                        for index in range(2000)]
        original = deepcopy(item.source.to_dict())
        with patch.object(lyrics_renderer, "lyric_wrapped_rows", wraps=lyrics_renderer.lyric_wrapped_rows) as wrap:
            _, _, (window, layout) = self.observe(scene, item, track, 2001)
        first, last = window[2:]
        self.assertEqual(first, 2)
        self.assertEqual(len({slot for slot, _, _ in layout}) - last, 2)
        self.assertLess(len(layout), 25)
        self.assertLess(wrap.call_count, 50)
        self.assertLessEqual(len(item._lyric_wrap_cache), 256)
        self.assertEqual(item.source.to_dict(), original)

    def test_visible_pixels_match_a_fully_laid_out_lyric_strip(self):
        for direction in ("up", "down"):
            scene, item, track = self.scene(subtitle_flow_direction=direction,
                subtitle_previous_distance_fade=1, subtitle_next_distance_fade=1)
            item.source.subtitle_role_styles = {
                role: {"opacity": 1 if role == "current" else .6,
                       "blur": 0 if role == "current" else 3, "scale": 1}
                for role in ("previous", "current", "next")}
            def full_strip(graphics, cues, index):
                return index, len(cues) - index - 1
            for time in (2.1, 4.25, 6, 6.25, 8.5, 12.1, 14.25):
                actual = self.observe(scene, item, track, time)[1]
                with patch("app.preview.canvas_snapshot._effective_lyric_context", side_effect=full_strip):
                    reference = self.observe(scene, item, track, time)[1]
                with self.subTest(direction=direction, time=time):
                    self.assertEqual(actual, reference)

    def test_preview_export_and_reverse_seeking_match_at_two_resolutions(self):
        scene, item, track = self.scene()
        original = deepcopy(item.source.to_dict())
        capturer = ExportCanvasCapturer(scene, [track], 30, set(), [(None, None)],
            lambda *_: None, lambda: None, lambda *_: None)
        capturer.stage_frame = lambda image, duration, key: RenderFrame(image, duration)
        reference = None
        for time in (6.25, 12.1, 4.1, 6.25):
            preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=time,
                playlist_duration_seconds=30, playlist_tracks=[track], timeline_seconds=time)
            self.assertEqual(preview, capturer.capture(ExportFrameSample(track, 1, 0, .1, time, time)).image)
            if reference is None:
                reference = preview
            scaled = self.observe(scene, item, track, time, scale=2)[1]
            self.assertEqual(scaled.width(), preview.width() * 2)
        self.assertEqual(preview, reference)
        self.assertEqual(item.source.to_dict(), original)


if __name__ == "__main__":
    unittest.main()
