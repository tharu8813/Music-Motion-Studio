"""Independent current intervals, centered active groups, and readable gaps."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.canvas.renderers import lyrics_renderer
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.preview.frame_state import (
    lyric_instrumental_windows, lyrics_display_groups, resolve_lyrics_cue_state,
    resolve_lyrics_word_reveal, resolve_lyrics_intro_state,
)
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.renderer.export_timeline import ExportFrameSample, ExportTimelinePlanner
from app.renderer.ffmpeg_renderer import RenderFrame
from app.services.lyrics_service import LyricsService
from app.services.ttml_parser import parse_ttml
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Translator


def timed(text, start, end):
    return {"start": start, "end": end, "text": text, "format": "ttml", "words": [
        {"start": start, "end": end, "text": text, "offset": 0}]}


class LyricsOverlapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def state(self, track, elapsed, offset=0):
        return resolve_lyrics_cue_state(track, elapsed, track.lyrics_timing_offset_seconds,
                                        offset, "glow", .5, whole_lines=True)

    def scene(self, direction="up"):
        source = Source(SourceType.LYRICS, "Lyrics", width=600, height=300,
                        subtitle_animation="none", subtitle_intro_enabled=False,
                        subtitle_karaoke_enabled=True, subtitle_context_lines=0,
                        subtitle_next_lines=0, subtitle_current_scale=1,
                        subtitle_flow_direction=direction, font_size=36,
                        fill_color="#00000000", subtitle_anchor=.5)
        scene = CanvasScene()
        scene.set_artboard_size(600, 300)
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("", "Overlap", duration_seconds=18, lyrics=[
            timed("Sustained", 1, 10), timed("Short", 3, 6), timed("Next", 12, 15)])
        return scene, item, track

    def observe(self, scene, item, track, elapsed):
        observations = []
        paint = lyrics_renderer.paint_word_reveal
        def record(graphics, painter, rect, flags, text, revealed):
            observations.append((text, revealed, painter.opacity(), painter.worldTransform().mapRect(rect)))
            paint(graphics, painter, rect, flags, text, revealed)
        with patch.object(lyrics_renderer, "paint_word_reveal", side_effect=record):
            image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                elapsed_seconds=elapsed, transparent=True)
        return observations, image

    def test_both_intervals_stay_current_and_older_one_survives_shorter_one(self):
        _, _, track = self.scene()
        self.assertEqual(self.state(track, 4).active_cue_indices, (0, 1))
        after_short = self.state(track, 7)
        self.assertEqual(after_short.active_cue_indices, (0,))
        self.assertEqual(after_short.active_cue_index, 0)
        self.assertFalse(after_short.gap_unblurred)
        self.assertEqual(self.state(track, 12).active_cue_indices, (2,))

    def test_gap_keeps_the_actual_last_finished_line_without_blur(self):
        _, _, track = self.scene()
        state = self.state(track, 11)
        self.assertEqual(state.active_cue_indices, ())
        self.assertIsNone(state.active_cue_index)
        self.assertEqual(state.cue_index, 0)  # Sustained ended after Short.
        self.assertTrue(state.gap_unblurred)
        self.assertFalse(self.state(track, .5).gap_unblurred)

    def test_two_phrases_finishing_together_are_both_retained_in_the_gap(self):
        _, _, track = self.scene()
        track.lyrics[1] = timed("Second", 3, 10)
        state = self.state(track, 11)
        self.assertEqual(state.held_cue_indices, (0, 1))
        cues, anchor = lyrics_display_groups(track, state)
        self.assertEqual(cues[anchor]["text"], "Sustained\nSecond")
        self.assertTrue(state.gap_unblurred)

    def test_expired_middle_cue_never_enters_the_current_group(self):
        _, item, track = self.scene()
        track.lyrics = [timed("A", 1, 10), timed("B", 2, 4), timed("C", 5, 8)]
        state = self.state(track, 6)
        self.assertEqual(state.active_cue_indices, (0, 2))
        cues, anchor = lyrics_display_groups(track, state)
        self.assertEqual(cues[anchor]["text"], "A\nC")
        self.assertEqual(cues[anchor]["display_line_positions"], (0, 0))
        self.assertEqual(cues[0]["text"], "B")
        self.assertEqual(len(resolve_lyrics_word_reveal(track, item.source, 6, state, None)), 2)

    def test_offsets_apply_to_the_interval_clock_and_word_clock(self):
        _, item, track = self.scene()
        track.lyrics_timing_offset_seconds = 1
        state = self.state(track, 2, offset=1)
        self.assertEqual(state.active_cue_indices, (0, 1))
        item.source.subtitle_timing_offset = 1
        values = resolve_lyrics_word_reveal(track, item.source, 2, state, None)
        self.assertAlmostEqual(values[0], 3)
        self.assertAlmostEqual(values[1], 5 / 3)

    def test_gap_detection_uses_union_of_overlapping_cues(self):
        _, source_item, track = self.scene()
        source_item.source.subtitle_intro_gap = 1
        self.assertEqual(lyric_instrumental_windows(track, source_item.source), ((10, 12),))

    def test_long_interlude_uses_last_finished_cue_as_its_held_anchor(self):
        _, item, track = self.scene()
        track.lyrics[-1] = timed("Next", 20, 23)
        track.duration_seconds = 25
        item.source.subtitle_intro_midtrack = True
        windows = lyric_instrumental_windows(track, item.source)
        intro = resolve_lyrics_intro_state(track, item.source, 15, windows)
        self.assertEqual(intro.held_cue_index, 0)

    def test_track_settings_preview_uses_same_overlapping_and_held_lines(self):
        _, _, track = self.scene()
        dialog = TrackDetailsDialog(track, Translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog._update_live_lyrics(4000)
        self.assertEqual(dialog.current_lyric.text(), "Sustained\nShort")
        for position in (7000, 11000):
            dialog._update_live_lyrics(position)
            self.assertEqual(dialog.current_lyric.text(), "Sustained")
            self.assertEqual(dialog.next_lyric.text(), "Next")

    def test_same_start_with_different_end_times_are_not_merged(self):
        cues = parse_ttml('<tt xmlns="http://www.w3.org/ns/ttml"><body>'
            '<p begin="1s" end="3s">Short</p><p begin="1s" end="8s">Sustained</p>'
            '</body></tt>')
        self.assertEqual(len(cues), 2)
        self.assertEqual(LyricsService.active_cue_indices(cues, 4), (1,))
        self.assertEqual(PlaylistTrack.from_dict(PlaylistTrack("", "Song", lyrics=cues).to_dict()).lyrics, cues)

    def test_active_group_is_centered_and_word_clocks_remain_independent(self):
        for direction in ("up", "down", "left", "right"):
            with self.subTest(direction=direction):
                scene, item, track = self.scene(direction)
                before = deepcopy(item.source.to_dict())
                observed, _ = self.observe(scene, item, track, 4)
                self.assertEqual([row[0] for row in observed], ["Sustained", "Short"])
                self.assertAlmostEqual(observed[0][1], 3)
                self.assertAlmostEqual(observed[1][1], 5 / 3)
                self.assertTrue(all(row[2] == 1 for row in observed))
                self.assertAlmostEqual(sum(row[3].center().y() for row in observed) / 2, 150)
                after_short, _ = self.observe(scene, item, track, 7)
                self.assertEqual([row[0] for row in after_short], ["Sustained"])
                self.assertAlmostEqual(after_short[0][3].center().y(), 150)
                self.assertEqual(item.source.to_dict(), before)
                self.assertEqual(item._subtitle_word_reveal, ())
                self.assertFalse(item._subtitle_gap_unblurred)

    def test_current_phrase_does_not_reset_its_animation_when_second_phrase_starts(self):
        scene, item, track = self.scene()
        item.source.subtitle_animation = "glow"
        observed, _ = self.observe(scene, item, track, 3.1)
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(row[2] == 1 for row in observed))
        self.assertIsNone(self.state(track, 3.1).release_progress)

    def test_held_line_is_visible_in_a_gap_and_uses_no_blur_pixmap(self):
        scene, item, track = self.scene()
        item.source.subtitle_role_styles = {"next": {"blur": 5}, "previous": {"blur": 5}}
        captures = []
        render = lyrics_renderer.paint_lines
        def record(graphics, painter, rect):
            captures.append((graphics._render_text(), graphics._subtitle_gap_unblurred))
            return render(graphics, painter, rect)
        with patch.object(lyrics_renderer, "paint_lines", side_effect=record), \
             patch.object(item, "_lyric_blur_pixmap", side_effect=AssertionError("Held gap lyric was blurred")):
            image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=11, transparent=True)
        self.assertIn(("Sustained", True), captures)
        self.assertTrue(any(image.pixelColor(x, y).alpha() > 0
                            for y in range(image.height()) for x in range(image.width())))

    def test_export_matches_preview_and_seeking_restores_group_state(self):
        scene, item, track = self.scene()
        capturer = ExportCanvasCapturer(scene, [track], 18, set(), [(None, None)],
                                      lambda *args: None, lambda: None, lambda *args: None)
        capturer.stage_frame = lambda image, duration, key: RenderFrame(image, duration)
        images = []
        for elapsed in (4, 7, 11, 12.5, 4):
            preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=elapsed,
                playlist_duration_seconds=18, playlist_tracks=[track], timeline_seconds=elapsed)
            exported = capturer.capture(ExportFrameSample(track, 1, 0, .1, elapsed, elapsed)).image
            self.assertEqual(preview, exported)
            images.append(preview)
        self.assertEqual(images[0], images[-1])
        pair = [ExportFrameSample(track, 1, 0, .1, time, time) for time in (5.9, 6.1)]
        self.assertEqual(len(capturer.coalesce_samples(pair, "base")), 2)
        times = [sample.elapsed_seconds for sample in ExportTimelinePlanner.build([track], [item.source], 30)]
        self.assertTrue(any(abs(time - 6) < 1 / 30 for time in times))


if __name__ == "__main__":
    unittest.main()
