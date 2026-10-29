"""A delayed first lyric is upcoming until its actual, offset-adjusted start."""
from copy import deepcopy
import unittest

from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.preview.frame_state import resolve_lyrics_cue_state, resolve_lyrics_intro_state
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import RenderFrame
from app.services.lyrics_service import LyricsService
from app.utils.i18n import Translator
from app.ui.design_system import apply_studio_style


FIRST = '어디긴 어디야 네 학원 앞에 있지 난'


class LyricsFirstCueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def track(self):
        return PlaylistTrack('', '랩스타', duration_seconds=35, lyrics=LyricsService._parse_lrc(
            f'[00:25.180] {FIRST}\n[00:28.010] Next lyric'))

    def test_first_lrc_is_not_active_until_its_timestamp_in_either_mode(self):
        track = self.track()
        for whole_lines in (False, True):
            for elapsed in (0, 12, 25.179):
                with self.subTest(whole_lines=whole_lines, elapsed=elapsed):
                    state = resolve_lyrics_cue_state(track, elapsed, 0, 0, 'rise', .4,
                                                     whole_lines=whole_lines)
                    self.assertTrue(state.before_first)
                    self.assertEqual(state.cue_index, 0)  # Upcoming layout anchor only.
                    self.assertIsNone(state.active_cue_index)
                    self.assertFalse(state.transitioning)
                    self.assertIsNone(state.release_progress)
            state = resolve_lyrics_cue_state(track, 25.18, 0, 0, 'rise', .4, whole_lines=whole_lines)
            self.assertFalse(state.before_first)
            self.assertEqual(state.active_cue_index, 0)
            self.assertAlmostEqual(state.transition_progress, 0)
            state = resolve_lyrics_cue_state(track, 25.38, 0, 0, 'rise', .4, whole_lines=whole_lines)
            self.assertAlmostEqual(state.transition_progress, .5)

    def test_offsets_include_negative_clock_and_preserve_already_started_first_line(self):
        track = self.track()
        for elapsed, before in ((22.179, True), (22.18, False)):
            state = resolve_lyrics_cue_state(track, elapsed, 2, 1, 'rise', .4, whole_lines=True)
            self.assertEqual(state.before_first, before)
            self.assertEqual(state.active_cue_index, None if before else 0)
        track.lyrics[0]['start'] = 0
        state = resolve_lyrics_cue_state(track, 0, -2, 0, 'rise', .4, whole_lines=True)
        self.assertTrue(state.before_first)
        self.assertIsNone(state.active_cue_index)
        for offset in (0, 2):
            state = resolve_lyrics_cue_state(track, 0, offset, 0, 'rise', .4, whole_lines=True)
            self.assertEqual(state.active_cue_index, 0)
            self.assertEqual(state.transition_progress, 1)

    def test_track_dialog_waits_and_resets_when_seeking_back(self):
        track = self.track()
        original = deepcopy(track.to_dict())
        dialog = TrackDetailsDialog(track, Translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        for position in (0, 25179, 25180, 0):
            dialog._update_live_lyrics(position)
            if position == 25180:
                self.assertEqual(dialog.current_lyric.text(), FIRST)
                self.assertEqual(dialog.next_lyric.text(), 'Next lyric')
            else:
                self.assertNotIn(FIRST, dialog.current_lyric.text())
                self.assertIn('00:25.180', dialog.current_lyric.text())
                self.assertEqual(dialog.previous_lyric.text(), '')
                self.assertEqual(dialog.next_lyric.text(), FIRST)
        dialog.timing_offset_spin.setValue(2)
        dialog._update_live_lyrics(0)
        self.assertIn('00:23.180', dialog.current_lyric.text())
        dialog._update_live_lyrics(23180)
        self.assertEqual(dialog.current_lyric.text(), FIRST)
        self.assertEqual(track.to_dict(), original)

    def test_first_wait_indicator_works_for_ordinary_realtime_lyrics(self):
        source = Source(SourceType.LYRICS, 'Lyrics', subtitle_karaoke_enabled=True,
                        subtitle_intro_enabled=True)
        track = self.track()
        intro = resolve_lyrics_intro_state(track, source, 1)
        self.assertIsNotNone(intro)
        self.assertTrue(intro.before_first)
        source.subtitle_intro_enabled = False
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 1))

    def test_preview_and_export_highlight_only_after_start_and_do_not_reuse_wait_frame(self):
        track = self.track()
        source = Source(SourceType.LYRICS, 'Lyrics', width=600, height=240, font_size=24,
                        fill_color='#00000000', subtitle_animation='none', subtitle_intro_enabled=False,
                        subtitle_karaoke_enabled=True, subtitle_context_lines=0, subtitle_next_lines=0,
                        subtitle_accent_color='#ff0000', subtitle_karaoke_pending_mode='custom',
                        subtitle_karaoke_pending_color='#ffffff',
                        subtitle_role_styles={'previous': {'color': '#00ff00', 'blur': 0},
                                              'next': {'opacity': .4, 'blur': 0}})
        scene = CanvasScene()
        self.addCleanup(scene.clear)
        scene.set_artboard_size(600, 240)
        item = SourceItem(source)
        scene.addItem(item)
        original = deepcopy(source.to_dict())
        capturer = ExportCanvasCapturer(scene, [track], 35, set(), [(None, None)],
                                       lambda *args: None, lambda: None, lambda *args: None)
        capturer.stage_frame = lambda image, duration, _key: RenderFrame(image, duration)
        images = []
        samples = [ExportFrameSample(track, 1, 0, .1, elapsed, elapsed)
                   for elapsed in (0, 25.179, 25.18, 26)]
        for sample in samples:
            exported = capturer.capture(sample).image
            preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                elapsed_seconds=sample.elapsed_seconds, playlist_duration_seconds=35,
                playlist_tracks=[track], timeline_seconds=sample.elapsed_seconds)
            self.assertEqual(exported, preview)
            images.append(preview)
            self.assertEqual(source.to_dict(), original)
            self.assertFalse(item._subtitle_before_first)  # Capture restores transient state.
        def red_pixels(image):
            return sum(image.pixelColor(x, y).red() > 150 and image.pixelColor(x, y).green() < 80
                       for y in range(image.height()) for x in range(image.width()))
        self.assertEqual(red_pixels(images[0]), 0)
        self.assertEqual(images[0], images[1])
        self.assertGreater(red_pixels(images[2]), 0)
        self.assertEqual(images[2], images[3])
        self.assertEqual(len(capturer.coalesce_samples(samples[1:3], 'base')), 2)
