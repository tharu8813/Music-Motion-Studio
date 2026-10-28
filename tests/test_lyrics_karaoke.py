"""Word timing, Canvas restoration and export motion regressions."""

from copy import deepcopy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase
from app.canvas.renderers.lyrics_renderer import pending_color
from app.canvas.renderers import lyrics_renderer
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.inspector.editors.lyrics_editor import LyricsSection
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample, ExportTimelinePlanner
from app.renderer.ffmpeg_renderer import RenderFrame
from app.services.lyrics_service import LyricsError, LyricsService


class KaraokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt" and not QFontDatabase.families():
            for font in ("segoeui.ttf", "malgun.ttf"):
                QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"]) / "Fonts" / font))

    def test_untimed_words_show_whole_lines_without_estimation(self):
        cue = {"start": 2, "end": 10, "text": "하나 둘\n셋 넷"}
        self.assertEqual(LyricsService.word_reveal(cue, 0), (0, 0))
        self.assertEqual(LyricsService.word_reveal(cue, 6), (4, 3))
        self.assertEqual(LyricsService.word_reveal(cue, 10), (4, 3))
        self.assertEqual(LyricsService.word_reveal(cue, 20), (4, 3))
        self.assertEqual(LyricsService.word_reveal({"start": 0, "end": 4, "text": "가나다라"}, 1), (4,))
        self.assertEqual(LyricsService.word_reveal({"start": 0, "end": 0, "text": ""}, 0), ())

    def test_selected_lines_use_only_explicit_word_times(self):
        cue = {"start": 0, "end": 8, "text": "one two three four\r\n번역 가사", "words": [
            {"offset": 4, "text": "two", "start": 2, "end": 3},
            {"offset": 8, "text": "three", "start": 5, "end": 6},
        ]}
        before = deepcopy(cue)
        progress = [LyricsService.word_reveal(cue, time, line_positions=(0,)) for time in (0, 1, 2, 4, 7, 8)]
        self.assertEqual(progress[1], (0, 0))  # No estimated first word.
        self.assertEqual(progress[-2], (13, 0))  # Untimed tail is never animated.
        self.assertEqual(progress[-1], (13, 0))
        self.assertTrue(all(a[0] <= b[0] for a, b in zip(progress, progress[1:])))
        self.assertEqual(LyricsService.word_reveal(cue, 1, line_positions=(0,)), progress[1])
        self.assertEqual(cue, before)
        cue.pop("words")
        self.assertEqual(LyricsService.word_reveal(cue, 8, line_positions=(0,)), (18, 0))
        self.assertGreater(LyricsService.word_reveal(cue, 4, line_positions=(0, 1))[1], 0)
        # CRLF and blank rows must not shift an imported second-line text offset.
        cue = {"start": 0, "end": 4, "text": "one\r\n\r\n😀 끝", "words": [
            {"offset": 7, "text": "😀", "start": 0, "end": 2},
            {"offset": 9, "text": "끝", "start": 2, "end": 4},
        ]}
        self.assertEqual(LyricsService.word_reveal(cue, 1, line_positions=(1,)), (0, 0.5))
        self.assertEqual(LyricsService.word_reveal(cue, 4, line_positions=(1,)), (0, 3))

    def test_missing_middle_words_are_not_estimated_and_timings_are_cached(self):
        cue = {"start": 0, "end": 8, "text": "one two three", "words": [
            {"offset": 0, "text": "one", "start": 0, "end": 2},
            {"offset": 8, "text": "three", "start": 6, "end": 8},
        ]}
        LyricsService._reveal_spans.cache_clear()
        self.assertEqual(LyricsService.word_reveal(cue, 4), (3,))
        for time in (4.1, 4.2, 4.3):
            LyricsService.word_reveal(cue, time)
        self.assertEqual(LyricsService._reveal_spans.cache_info().misses, 1)
        self.assertEqual(LyricsService._reveal_spans.cache_info().hits, 3)
        cue["words"][1]["start"] = 4
        self.assertEqual(LyricsService.word_reveal(cue, 3), (3,))
        self.assertEqual(LyricsService._reveal_spans.cache_info().misses, 2)

    def test_timed_lrc_roundtrip_and_repeated_timestamps(self):
        cues = LyricsService._parse_lrc("[00:02.00][00:12.00]<00:02.50>하나 <00:04.00>둘<00:05.00>\n[00:20.00]끝")
        self.assertEqual(cues[0]["text"], "하나 둘")
        self.assertEqual(LyricsService.word_reveal(cues[0], 2.4), (0,))
        self.assertEqual(LyricsService.word_reveal(cues[0], 3.25), (1.5,))
        self.assertEqual(LyricsService.word_reveal(cues[0], 4.5), (3.5,))
        self.assertEqual(cues[1]["words"][0]["start"], 12.5)
        self.assertEqual(LyricsService._parse_lrc(LyricsService.format_lrc(cues)), cues)
        track = PlaylistTrack("", "Track", lyrics=cues)
        self.assertEqual(PlaylistTrack.from_dict(track.to_dict()).lyrics, cues)
        gap_cues = LyricsService._parse_lrc("[00:00.00]<00:00.50>A <00:01.00><00:02.00>B<00:03.00>")
        self.assertEqual(LyricsService._parse_lrc(LyricsService.format_lrc(gap_cues)), gap_cues)
        with self.assertRaises(LyricsError):
            LyricsService._parse_lrc("[00:02.00]<00:03.00>A<00:02.00>B")
        with self.assertRaises(LyricsError):
            LyricsService._parse_lrc("[00:02.00]<00:03.00>A<00:05.00>\n[00:04.00]B")

    def test_project_word_validation(self):
        valid = PlaylistTrack("", "Track", lyrics=LyricsService._parse_lrc(
            "[00:00.00]<00:00.00>A <00:01.00>B<00:02.00>" )).to_dict()
        for key, value in (("start", float("nan")), ("end", 99), ("offset", -1),
                           ("offset", True), ("text", "wrong")):
            data = deepcopy(valid)
            data["lyrics"][0]["words"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                PlaylistTrack.from_dict(data)

    def scene(self, text="Hello world", **options):
        source = Source(SourceType.LYRICS, "Lyrics", width=600, height=240,
                        subtitle_animation="none", subtitle_intro_enabled=False,
                        subtitle_karaoke_enabled=True, subtitle_context_lines=0,
                        subtitle_next_lines=0, subtitle_current_scale=1,
                        font_size=48, outline_color="#FFFFFF", subtitle_accent_color="#FF0000",
                        fill_color="#00000000", **options)
        scene = CanvasScene()
        scene.set_artboard_size(600, 240)
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("", "Track", duration_seconds=8,
                              lyrics=[{"start": 1, "end": 5, "text": text}])
        # Explicit TTML fixture: each physical line has a recorded span.
        cue = track.lyrics[0]
        cue["format"] = "ttml"
        cue["words"] = []
        offset = 0
        for line in text.splitlines(keepends=True):
            cue["words"].append({"start": 1, "end": 5, "text": line.rstrip("\n"), "offset": offset})
            offset += len(line)
        return scene, item, track

    @staticmethod
    def red_pixels(image):
        return sum(image.pixelColor(x, y).red() > 100 and image.pixelColor(x, y).green() < 80
                   and image.pixelColor(x, y).alpha() > 100
                   for y in range(image.height()) for x in range(image.width()))

    def test_canvas_progress_offsets_seek_and_state_restoration(self):
        for alignment in ("left", "center", "right"):
            with self.subTest(alignment=alignment):
                scene, item, track = self.scene("하나 둘\nHello world", text_alignment=alignment)
                original = deepcopy(item.source.to_dict())
                images = [CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                    elapsed_seconds=time, transparent=True) for time in (1, 2, 3, 4.9)]
                reds = [self.red_pixels(image) for image in images]
                self.assertEqual(reds[0], 0)
                self.assertTrue(all(a < b for a, b in zip(reds, reds[1:])), reds)
                again = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=2, transparent=True)
                self.assertEqual(again, images[1])
                track.lyrics_timing_offset_seconds = 0.75
                item.source.subtitle_timing_offset = 0.25
                shifted = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=1, transparent=True)
                self.assertEqual(shifted, images[1])
                item.source.subtitle_timing_offset = 0
                self.assertEqual(item.source.to_dict(), original)
                self.assertEqual(item._subtitle_word_reveal, ())

    def test_ordinary_lyrics_render_whole_from_start_and_hold_until_next_cue(self):
        scene, item, track = self.scene("First line")
        # Ordinary LRC/SRT data, including a legacy word result, cannot animate.
        track.lyrics = [
            {"start": 5, "end": 7, "text": "First line", "words": [
                {"offset": 0, "text": "First", "start": 5, "end": 6}]},
            {"start": 12, "end": 14, "text": "Next line"},
        ]
        track.duration_seconds = 20
        item.source.subtitle_intro_enabled = True
        item.source.subtitle_intro_midtrack = True
        images = [CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
            elapsed_seconds=time, transparent=True) for time in (0, 5.5, 9, 11.9, 12, 19)]
        self.assertGreater(self.red_pixels(images[0]), 0)
        self.assertTrue(all(image == images[0] for image in images[:4]))
        self.assertEqual(images[4], images[5])
        self.assertNotEqual(images[0], images[4])
        capturer = ExportCanvasCapturer(scene, [track], 20, set(), [(None, None)],
                                      lambda *args: None, lambda: None, lambda *args: None)
        capturer.stage_frame = lambda image, duration, _key: RenderFrame(image, duration)
        for elapsed in (0, 9, 12, 19):
            sample = ExportFrameSample(track, 1, 0, .1, elapsed, elapsed)
            exported = capturer.capture(sample).image
            preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                elapsed_seconds=elapsed, playlist_duration_seconds=20,
                playlist_tracks=[track], timeline_seconds=elapsed)
            self.assertEqual(exported, preview)

    def test_export_samples_do_not_freeze_word_fill(self):
        scene, item, track = self.scene()
        samples = ExportTimelinePlanner.build([track], [item.source], 30)
        times = [sample.elapsed_seconds for sample in samples if 1 < sample.elapsed_seconds < 5]
        self.assertGreater(len(times), 100)
        self.assertLessEqual(max(b - a for a, b in zip(times, times[1:])), 1 / 30 + 0.001)
        capturer = ExportCanvasCapturer(scene, [track], 8, set(), [(None, None)],
                                      lambda *args: None, lambda: None, lambda *args: None)
        pair = [ExportFrameSample(track, 1, 0, 0.1, time, time) for time in (2, 2.1)]
        self.assertEqual(len(capturer.coalesce_samples(pair, "base")), 2)
        item.source.subtitle_karaoke_enabled = False
        self.assertEqual(len(capturer.coalesce_samples(pair, "base")), 1)

    def test_word_offset_leaves_cues_unchanged_and_matches_export(self):
        scene, item, track = self.scene()
        before = track.to_dict()
        reference = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=2.4)
        capturer = ExportCanvasCapturer(scene, [track], 8, set(), [(None, None)],
                                      lambda *args: None, lambda: None, lambda *args: None)
        capturer.stage_frame = lambda image, duration, _key: RenderFrame(image, duration)
        for offset, elapsed in ((0.4, 2), (-0.4, 2.8)):
            item.source.subtitle_karaoke_offset = offset
            shifted = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=elapsed)
            self.assertEqual(shifted, reference)
            self.assertEqual(capturer.capture(ExportFrameSample(track, 1, 0, 0.1, elapsed, elapsed)).image, shifted)
        self.assertEqual(track.to_dict(), before)

    def test_interlude_suppresses_held_word_fill_until_vocals_resume(self):
        for direction in ("up", "left"):
            scene, item, track = self.scene("First lyric\nTranslation", subtitle_karaoke_lines=[0],
                subtitle_intro_midtrack=True, subtitle_flow_direction=direction, subtitle_karaoke_offset=0.2)
            source = item.source
            source.subtitle_animation, source.subtitle_animation_duration = "glow", 0.5
            source.subtitle_next_lines = 1
            track.duration_seconds = 14
            track.lyrics = [{"start": 1, "end": 10, "text": "First lyric\nTranslation", "format": "ttml"},
                            {"start": 10, "end": 13, "text": "Next lyric\nTranslation", "format": "ttml"}]
            capturer = ExportCanvasCapturer(scene, [track], 14, set(), [(None, None)],
                                          lambda *args: None, lambda: None, lambda *args: None)
            capturer.stage_frame = lambda image, duration, _key: RenderFrame(image, duration)
            with patch("app.preview.canvas_snapshot.instrumental_spans", return_value=((3, 10),)), \
                 patch("app.preview.export_canvas_capture.instrumental_spans", return_value=((3, 10),)):
                images = []
                for elapsed in (5, 10.1, 5):
                    with patch.object(lyrics_renderer, "paint_word_reveal", wraps=lyrics_renderer.paint_word_reveal) as reveal:
                        preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=elapsed)
                        self.assertEqual(bool(reveal.call_count), elapsed > 10)
                    exported = capturer.capture(ExportFrameSample(track, 1, 0, 0.1, elapsed, elapsed)).image
                    self.assertEqual(preview, exported)
                    images.append(preview)
                self.assertEqual(images[0], images[2])

    def test_untimed_prefix_keeps_canvas_and_export_frame_state_in_sync(self):
        scene, item, track = self.scene("하나 둘\nTranslation", subtitle_karaoke_lines=[0])
        track.lyrics[0]["words"] = [{"offset": 3, "text": "둘", "start": 3, "end": 4}]
        capturer = ExportCanvasCapturer(scene, [track], 8, set(), [(None, None)],
                                      lambda *args: None, lambda: None, lambda *args: None)
        pair = [ExportFrameSample(track, 1, 0, 0.1, time, time) for time in (3.5, 3.6)]
        self.assertEqual(len(capturer.coalesce_samples(pair, "base")), 2)
        images = [CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                    elapsed_seconds=time, transparent=True) for time in (3.5, 3.6, 3.5)]
        self.assertLess(self.red_pixels(images[0]), self.red_pixels(images[1]))
        self.assertEqual(images[0], images[2])
        self.assertEqual(len(item._lyric_reveal_cache), 1)
        with patch.object(lyrics_renderer, "QTextLayout", side_effect=AssertionError("Repeated shaping")):
            self.assertEqual(CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                elapsed_seconds=3.5, transparent=True), images[0])
        capturer.stage_frame = lambda image, duration, _key: RenderFrame(image, duration)
        for sample in pair:
            exported = capturer.capture(sample).image
            preview = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                elapsed_seconds=sample.elapsed_seconds, playlist_duration_seconds=8,
                playlist_tracks=[track], timeline_seconds=sample.timeline_seconds)
            self.assertEqual(exported, preview)

    def test_next_cue_word_fill_does_not_wait_for_fade_or_blur(self):
        for effect, next_lines, blur in (("glow", 0, 0), ("glow", 1, 4),
                                        ("rise", 0, 0), ("crossfade", 1, 0)):
            with self.subTest(effect=effect, next_lines=next_lines, blur=blur):
                scene, item, track = self.scene()
                source = item.source
                source.subtitle_animation = effect
                source.subtitle_animation_duration = 1
                source.subtitle_next_lines = next_lines
                source.subtitle_role_styles = {"next": {"blur": blur}}
                track.lyrics = [
                    {"start": 1, "end": 2, "text": "Previous"},
                    {"start": 2, "end": 5, "text": "Hello world", "format": "ttml", "words": [
                        {"offset": 0, "text": "Hello", "start": 2, "end": 2.25},
                        {"offset": 6, "text": "world", "start": 2.75, "end": 5},
                    ]},
                ]
                original = deepcopy(source.to_dict())
                fills = []
                paint = lyrics_renderer.paint_word_reveal

                def observe(graphics, painter, row, flags, text, revealed):
                    fills.append((revealed, painter.opacity(), painter.worldTransform().mapRect(row)))
                    paint(graphics, painter, row, flags, text, revealed)

                for seconds, expected in ((2.05, 1), (2.15, 3), (2.4, 5), (2.6, 5), (2.05, 1)):
                    fills.clear()
                    with patch.object(lyrics_renderer, "paint_word_reveal", side_effect=observe):
                        image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                            elapsed_seconds=seconds, transparent=True)
                    self.assertEqual(len(fills), 1, (seconds, fills))
                    revealed, opacity, row = fills[0]
                    self.assertAlmostEqual(revealed, expected)
                    self.assertAlmostEqual(opacity, 1)
                    self.assertGreater(self.red_pixels(image.copy(row.toAlignedRect().intersected(image.rect()))), 0)
                self.assertEqual(source.to_dict(), original)
                self.assertEqual(item._subtitle_word_reveal, ())

    def test_saved_toggle_and_accent_picker(self):
        source = Source(SourceType.LYRICS, "Lyrics", subtitle_karaoke_enabled=True)
        self.assertTrue(Source.from_dict(source.to_dict()).subtitle_karaoke_enabled)
        self.assertTrue(LyricsSection.hidden_when_off(None, source)["subtitle_accent_color"])
        with self.assertRaises(ValueError):
            Source.from_dict(source.to_dict() | {"subtitle_karaoke_enabled": "true"})
        for model in ("tiny", "base"):
            restored = Source.from_dict(source.to_dict() | {
                "subtitle_karaoke_analysis_model": model, "subtitle_karaoke_offset": 0.2})
            self.assertNotIn("subtitle_karaoke_analysis_model", restored.to_dict())
            self.assertEqual(restored.subtitle_karaoke_offset, 0.2)
        for key, value in (("subtitle_karaoke_offset", float("nan")), ("subtitle_karaoke_offset", True),
                           ("subtitle_karaoke_offset", 5.01)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Source.from_dict(source.to_dict() | {key: value})

    def test_pending_color_modes_persist_and_render_in_preview(self):
        scene, item, track = self.scene("Hello", subtitle_karaoke_pending_mode="custom",
                                        subtitle_karaoke_pending_color="#00FF00")
        first = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=1, transparent=True)
        reached = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=4.9, transparent=True)
        green = lambda image: sum(image.pixelColor(x, y).green() > 150 and image.pixelColor(x, y).red() < 80
                                 for y in range(image.height()) for x in range(image.width()))
        self.assertGreater(green(first), green(reached))
        self.assertGreater(self.red_pixels(reached), self.red_pixels(first))
        item.source.subtitle_karaoke_pending_mode = "dim"
        item.source.subtitle_accent_color = "#80FF8000"
        item.source.subtitle_karaoke_pending_brightness = 0.25
        color = pending_color(item.source)
        self.assertEqual((color.red(), color.green(), color.blue(), color.alpha()), (64, 32, 0, 128))
        saved = Source.from_dict(item.source.to_dict())
        self.assertEqual(saved.to_dict(), item.source.to_dict())
        self.assertTrue(LyricsSection.hidden_when_off(None, saved)["subtitle_karaoke_pending_brightness"])
        self.assertFalse(LyricsSection.hidden_when_off(None, saved)["subtitle_karaoke_pending_color"])
        for key, value in (("subtitle_karaoke_pending_mode", "wrong"), ("subtitle_karaoke_pending_color", "wrong"),
                           ("subtitle_karaoke_pending_brightness", float("nan"))):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Source.from_dict(saved.to_dict() | {key: value})

    def test_unselected_translation_never_gets_estimated_word_highlights(self):
        scene, item, track = self.scene("Hello\nWorld", subtitle_karaoke_lines=[0])
        selected = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=4.9, transparent=True)
        item.source.subtitle_karaoke_lines = []
        all_lines = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=4.9, transparent=True)
        self.assertGreater(self.red_pixels(all_lines), self.red_pixels(selected))


if __name__ == "__main__":
    unittest.main()
