import unittest
from copy import deepcopy
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import RenderFrame


class LyricsTailScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def scene(self, next_lines=-1, direction='up', multiline=False):
        source = Source(SourceType.LYRICS, 'Lyrics', width=600, height=400, font_size=24,
            fill_color='#00000000', subtitle_animation='cascade', subtitle_motion_easing='linear',
            subtitle_animation_duration=.5, subtitle_stagger=0, subtitle_context_lines=1,
            subtitle_next_lines=next_lines, subtitle_flow_direction=direction, subtitle_current_scale=1,
            subtitle_previous_blur=0, subtitle_previous_distance_fade=0, subtitle_next_distance_fade=0,
            subtitle_glow_radius=0, subtitle_glow_strength=0,
            subtitle_role_styles={role: {'opacity': .8, 'blur': 0, 'scale': 1} for role in ('previous','current','next')})
        scene = CanvasScene()
        scene.set_artboard_size(600, 400)
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack('', 'Track', duration_seconds=18, lyrics=[
            {'start': index * 2, 'end': (index + 1) * 2, 'text': f'Cue {index}'} for index in range(8)])
        if multiline:
            track.lyrics[-1]['text'] = 'Last A\nLast B'
        return scene, item, track

    def observe(self, scene, item, track, time):
        rows, entering = {}, []
        draw = item._draw_text
        from app.canvas.renderers import lyrics_renderer
        paint = lyrics_renderer.paint_lines
        def record(painter, rect, flags, text):
            rows[text] = (painter.worldTransform().mapRect(rect), painter.opacity())
            return draw(painter, rect, flags, text)
        def motion(graphics, painter, rect):
            entering.append(graphics._subtitle_entering_line_count)
            return paint(graphics, painter, rect)
        with patch.object(item, '_draw_text', side_effect=record), \
             patch.object(item, 'lyric_line_capacity', return_value=5), \
             patch.object(lyrics_renderer, 'paint_lines', side_effect=motion):
            image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=time, transparent=True)
        return rows, entering[-1], image

    def test_tail_already_visible_in_automatic_context_does_not_fade_in_again(self):
        scene, item, track = self.scene()
        before, _, _ = self.observe(scene, item, track, 9.999)
        self.assertIn('Cue 7', before)
        for time in (10, 10.1, 12, 12.1, 14, 14.1):
            rows, entering, _ = self.observe(scene, item, track, time)
            self.assertIn('Cue 7', rows)
            self.assertEqual(entering, 0)
            self.assertAlmostEqual(rows['Cue 7'][1], before['Cue 7'][1])
        at_start, _, _ = self.observe(scene, item, track, 10)
        self.assertAlmostEqual(before['Cue 7'][0].center().y(), at_start['Cue 7'][0].center().y(), delta=.2)

    def test_new_final_cue_enters_from_bottom_instead_of_fading_in_inside_blank_space(self):
        scene, item, track = self.scene(next_lines=1)
        values = [self.observe(scene, item, track, time)[0]['Cue 7'] for time in (11.5, 11.6, 11.75, 11.999)]
        self.assertGreaterEqual(values[0][0].top(), 400)
        self.assertTrue(all(first[0].top() > second[0].top() for first, second in zip(values, values[1:])))
        self.assertTrue(all(abs(value[1] - .8) < 1e-6 for value in values))
        self.assertLess(values[-1][0].bottom(), 400)

    def test_multiline_tail_moves_as_one_block(self):
        scene, item, track = self.scene(next_lines=1, multiline=True)
        distances = []
        for time in (11.5, 11.6, 11.75, 11.999):
            rows, _, _ = self.observe(scene, item, track, time)
            first, second = rows['Last A'][0], rows['Last B'][0]
            distances.append(second.center().y() - first.center().y())
            if time == 11.5:
                self.assertGreaterEqual(first.top(), 400)
        self.assertTrue(all(abs(value - distances[0]) < 1e-6 for value in distances))

    def test_multiple_new_tail_cues_keep_their_spacing_when_automatic_context_expands(self):
        scene, item, track = self.scene()
        track.lyrics[3]['text'] = 'Many\nrows\nhere'
        distances = []
        for time in (9.5, 9.6, 9.75, 9.999):
            rows, _, _ = self.observe(scene, item, track, time)
            a, b = (rows[f'Cue {index}'][0] for index in (6, 7))
            if time == 9.5:
                self.assertGreaterEqual(a.top(), 400)
            distances.append(b.center().y() - a.center().y())
        self.assertTrue(all(abs(value - distances[0]) < 1e-6 for value in distances))

    def test_entry_respects_all_flow_directions(self):
        for direction in ('up', 'down', 'left', 'right'):
            with self.subTest(direction=direction):
                scene, item, track = self.scene(next_lines=1, direction=direction)
                rows, _, _ = self.observe(scene, item, track, 11.5)
                rect = rows['Cue 7'][0]
                if direction == 'up': self.assertGreaterEqual(rect.top(), 400)
                elif direction == 'down': self.assertLessEqual(rect.bottom(), 0)
                elif direction == 'left': self.assertGreaterEqual(rect.left(), 600)
                else: self.assertLessEqual(rect.right(), 0)

    def test_tail_motion_matches_export_and_seeking_is_deterministic(self):
        scene, item, track = self.scene(next_lines=1)
        original = deepcopy(item.source.to_dict())
        capturer = ExportCanvasCapturer(scene, [track], 18, set(), [(None,None)],
            lambda *args: None, lambda: None, lambda *args: None)
        capturer.stage_frame = lambda image, duration, key: RenderFrame(image, duration)
        first = None
        for time in (12.1, 12.25, 14.1, 12.1):
            preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=time,
                playlist_duration_seconds=18, playlist_tracks=[track], timeline_seconds=time)
            sample = ExportFrameSample(track, 1, 0, .1, time, time)
            self.assertEqual(preview, capturer.capture(sample).image)
            if first is None: first = preview
        self.assertEqual(first, preview)
        self.assertEqual(item.source.to_dict(), original)

    def test_two_future_cues_are_prepared_outside_before_they_are_displayed(self):
        for direction in ('up', 'down', 'left', 'right'):
            with self.subTest(direction=direction):
                scene, item, track = self.scene(next_lines=1, direction=direction)
                rows, entering, _ = self.observe(scene, item, track, 5)
                self.assertIn('Cue 3', rows)  # displayed next cue
                for title in ('Cue 4', 'Cue 5'):
                    self.assertIn(title, rows)  # prepared one/two cues ahead
                    rect = rows[title][0]
                    if direction == 'up': self.assertGreaterEqual(rect.top(), 400)
                    elif direction == 'down': self.assertLessEqual(rect.bottom(), 0)
                    elif direction == 'left': self.assertGreaterEqual(rect.left(), 600)
                    else: self.assertLessEqual(rect.right(), 0)
                self.assertNotIn('Cue 6', rows)
                self.assertEqual(item._subtitle_prefetch_line_count, 0)

    def test_prepared_cue_and_displayed_cue_share_the_same_entry_origin(self):
        for direction in ('up', 'down', 'left', 'right'):
            with self.subTest(direction=direction):
                scene, item, track = self.scene(next_lines=1, direction=direction)
                before = self.observe(scene, item, track, 7.99999)[0]['Cue 5']
                after = self.observe(scene, item, track, 8)[0]['Cue 5']
                self.assertAlmostEqual(before[0].center().x(), after[0].center().x(), delta=.2)
                self.assertAlmostEqual(before[0].center().y(), after[0].center().y(), delta=.2)
                self.assertAlmostEqual(before[1], after[1])

    def test_preroll_changes_visible_frames_before_every_cue_boundary(self):
        from app.canvas.renderers import lyrics_renderer
        paint = lyrics_renderer.paint_lines

        def without_preroll(item, painter, rect):
            original = item._subtitle_prefetch_progress
            item._subtitle_prefetch_progress = 0
            try:
                return paint(item, painter, rect)
            finally:
                item._subtitle_prefetch_progress = original

        for direction in ('up', 'down'):
            for wrap in (False, True):
                scene, item, track = self.scene(next_lines=1, direction=direction)
                item.source.subtitle_auto_wrap = wrap
                item.source.subtitle_wrap_to_source = False
                item.source.subtitle_wrap_width = 120
                item.source.height = 800
                scene.set_artboard_size(600, 800)
                track.lyrics[4]['text'] = 'Future A\nFuture B'
                for seconds in (3.9, 5.9, 7.9, 9.9):
                    with self.subTest(direction=direction, wrap=wrap, seconds=seconds):
                        options = dict(elapsed_seconds=seconds, transparent=True, output_scale=2)
                        before = deepcopy(item.source.to_dict())
                        actual = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, **options)
                        with patch.object(lyrics_renderer, 'paint_lines', side_effect=without_preroll):
                            baseline = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, **options)
                        self.assertNotEqual(actual, baseline)
                        self.assertEqual(item.source.to_dict(), before)

    def test_every_promoted_cue_keeps_position_and_only_scrolls_one_slot(self):
        for boundary in (4, 6, 8, 10, 12):
            scene, item, track = self.scene(next_lines=1)
            title = f'Cue {int(boundary / 2) + 1}'
            before = self.observe(scene, item, track, boundary - .00001)[0][title][0]
            start = self.observe(scene, item, track, boundary)[0][title][0]
            end = self.observe(scene, item, track, boundary + .5)[0][title][0]
            self.assertAlmostEqual(before.center().y(), start.center().y(), delta=.1)
            self.assertAlmostEqual(start.center().y() - end.center().y(), item._lyric_line_height())
            self.assertEqual(item._subtitle_prefetch_progress, 0)
