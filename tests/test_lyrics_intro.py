"""Temporary intro rows, real vocal evidence, timing, settings and export cadence."""

from concurrent.futures import Future
from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QPointF

from app.animation.lyrics import INTRO_STYLE_LABELS, lyric_progress
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.preview.frame_state import lyric_instrumental_windows, resolve_lyrics_intro_state
from app.renderer.export_timeline import ExportTimelinePlanner
from app.services import lyrics_intro_service as service
from app.utils.i18n import Translator


class LyricsIntroTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def source(self, **options):
        return Source(SourceType.LYRICS, "Lyrics", width=600, height=360, font_size=30,
                      fill_color="#00000000", subtitle_previous_blur=0, subtitle_glow_strength=0,
                      subtitle_previous_distance_fade=0, subtitle_next_distance_fade=0, **options)

    def track(self, **options):
        return PlaylistTrack("", "Track", duration_seconds=25, lyrics=[
            {"start": 4, "end": 6, "text": "First lyric"},
            {"start": 17, "end": 20, "text": "Next lyric"},
        ], **options)

    def capture(self, source, seconds, spans=(), track=None, axis=1):
        track = track or self.track()
        scene = CanvasScene()
        scene.set_artboard_size(source.width, source.height)
        item = SourceItem(source)
        scene.addItem(item)
        before = deepcopy(source.to_dict())
        rows, dots = {}, []
        draw = item._draw_text

        def observe(painter, rect, flags, text):
            center = painter.worldTransform().map(rect.center())
            rows[text] = center.y() if axis else center.x()
            draw(painter, rect, flags, text)

        from app.canvas.renderers import lyrics_renderer
        paint_intro = lyrics_renderer.paint_intro

        def observe_intro(graphics, painter, rect, y):
            center = painter.worldTransform().map(QPointF(rect.center().x(), y))
            dots.append(center.y() if axis else center.x())
            paint_intro(graphics, painter, rect, y)

        with patch.object(item, "_draw_text", side_effect=observe), patch.object(
            lyrics_renderer, "paint_intro", side_effect=observe_intro,
        ):
            image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=seconds,
                                                  transparent=True, lyric_instrumental_spans=spans)
        self.assertEqual(before, source.to_dict())
        self.assertIsNone(item._subtitle_intro_state)
        return rows, dots, image

    def test_completed_analysis_uses_retained_job_without_requesting_again(self):
        source = self.source(subtitle_intro_midtrack=True)
        pending = Future()
        self.assertEqual(service.completed_instrumental_spans(source, pending), ())
        completed = Future()
        completed.set_result((((6, 17),), ()))
        with patch.object(service, 'request_instrumental_analysis', side_effect=AssertionError):
            self.assertEqual(service.completed_instrumental_spans(source, completed), ((6, 17),))
        failed = Future()
        failed.set_exception(RuntimeError('Unavailable'))
        self.assertEqual(service.completed_instrumental_spans(source, failed), ())

    def test_worker_snapshot_uses_source_specific_completed_spans(self):
        from app.preview import canvas_snapshot as snapshot_module

        source = self.source(subtitle_intro_midtrack=True)
        track = self.track()
        spans = ((6, 17),)
        _, _, expected = self.capture(source, 10, spans=spans, track=track)
        scene = CanvasScene()
        scene.set_artboard_size(source.width, source.height)
        scene.addItem(SourceItem(source))
        with patch.object(snapshot_module, 'instrumental_spans', side_effect=AssertionError):
            actual = CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0, elapsed_seconds=10, transparent=True,
                lyric_instrumental_spans_by_source={source.id: spans},
            )
        self.assertEqual(actual, expected)
        scene.clear()

    def test_timing_and_vocal_evidence(self):
        source, track = self.source(subtitle_intro_midtrack=True), self.track()
        self.assertIsNotNone(resolve_lyrics_intro_state(track, source, 1))
        self.assertTrue(resolve_lyrics_intro_state(track, source, 4).exiting)
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 4 + source.subtitle_animation_duration))
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 10))  # Unknown vocals.
        self.assertIsNotNone(resolve_lyrics_intro_state(track, source, 10, ((7, 17),)))
        self.assertTrue(resolve_lyrics_intro_state(track, source, 17, ((7, 17),)).exiting)
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 17 + source.subtitle_animation_duration, ((7, 17),)))
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 24, ((7, 17),)))
        self.assertEqual(lyric_instrumental_windows(track, source), ((6, 17),))
        track.lyrics[0]["end"] = 17  # LRC holds the cue: measure after its initial phrase.
        self.assertEqual(lyric_instrumental_windows(track, source), ((6, 17),))
        track.lyrics_timing_offset_seconds, source.subtitle_timing_offset = 0.5, 1
        self.assertEqual(lyric_instrumental_windows(track, source), ((4.5, 15.5),))
        self.assertTrue(resolve_lyrics_intro_state(track, source, 2.5).exiting)
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 2.5 + source.subtitle_animation_duration))
        track.lyrics = []
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 0))
        self.assertEqual(service.confirmed_instrumental_spans((), (), 5), ())
        self.assertEqual(service.confirmed_instrumental_spans(((6, 17),), ((7, 9),), 5), ((9.3, 17),))
        self.assertEqual(service.confirmed_instrumental_spans(((6, 17),), ((7, 16),), 5), ())

    def test_temporary_row_keeps_lyrics_and_collapses_at_both_ends(self):
        for direction in ("up", "down", "left", "right"):
            source = self.source(subtitle_flow_direction=direction, subtitle_intro_midtrack=True)
            axis = int(direction in {"up", "down"})
            anchor = (source.height if axis else source.width) / 2
            sign = 1 if direction in {"up", "left"} else -1
            rows, intro, _ = self.capture(source, 1.5, axis=axis)
            self.assertEqual(set(rows), {"First lyric", "Next lyric"})
            self.assertAlmostEqual(intro[0], anchor)
            self.assertGreater(sign * (rows["First lyric"] - intro[0]), 0)
            _, intro, _ = self.capture(source, 4 + source.subtitle_animation_duration)
            self.assertFalse(intro)
            middle, intro, _ = self.capture(source, 10, ((7, 17),), axis=axis)
            self.assertAlmostEqual(intro[0], anchor)
            self.assertLess(sign * (middle["First lyric"] - intro[0]), 0)
            self.assertGreater(sign * (middle["Next lyric"] - intro[0]), 0)
            before, intro, _ = self.capture(source, 6.99, ((7, 17),), axis=axis)
            self.assertFalse(intro)
            beginning, intro, _ = self.capture(source, 7.001, ((7, 17),), axis=axis)
            for text in before:
                self.assertAlmostEqual(before[text], beginning[text], delta=0.1)
            _, intro, _ = self.capture(source, 17 + source.subtitle_animation_duration, ((7, 17),))
            self.assertFalse(intro)

    def test_temporary_cue_uses_the_lyric_clock_and_moves_with_incoming_cue(self):
        track, spans = self.track(), ((7, 17),)
        for direction in ("up", "down", "left", "right"):
            source = self.source(subtitle_flow_direction=direction, subtitle_intro_midtrack=True,
                                 subtitle_animation_duration=1, subtitle_motion_easing="linear")
            axis = int(direction in {"up", "down"})
            for end, evidence in ((4, ()), (17, spans)):
                before, marker, _ = self.capture(source, end - 0.00001, evidence, axis=axis)
                boundary, start, _ = self.capture(source, end + 0.00001, evidence, axis=axis)
                for text in before:
                    self.assertAlmostEqual(before[text], boundary[text], delta=0.02)
                self.assertAlmostEqual(marker[0], start[0], delta=0.02)
                middle, marker, _ = self.capture(source, end + 0.5, evidence, axis=axis)
                final, _, _ = self.capture(source, end + 1, evidence, axis=axis)
                incoming = "First lyric" if end == 4 else "Next lyric"
                self.assertAlmostEqual(middle[incoming], (boundary[incoming] + final[incoming]) / 2, delta=0.02)
                self.assertAlmostEqual(marker[0] - start[0], middle[incoming] - boundary[incoming], delta=0.02)
        for easing in ("linear", "smooth", "ease_in", "ease_out"):
            source = self.source(subtitle_motion_easing=easing, subtitle_animation_duration=1.5)
            entering = resolve_lyrics_intro_state(track, source, 0.375)
            leaving = resolve_lyrics_intro_state(track, source, 4.375)
            self.assertAlmostEqual(entering.progress, lyric_progress(source, 0.25))
            self.assertAlmostEqual(leaving.progress, entering.progress)
            self.assertAlmostEqual(leaving.opacity, 1 - entering.opacity)
            self.assertEqual(resolve_lyrics_intro_state(track, source, 3.99).opacity, 1)
            self.assertIsNone(resolve_lyrics_intro_state(track, source, 5.5))
        source = self.source(subtitle_animation="none")
        self.assertIsNone(resolve_lyrics_intro_state(track, source, 4))

        track.lyrics[0]["text"] = "First A\nFirst B"
        track.lyrics[1]["text"] = "Next A\nNext B\nNext C"
        source = self.source(subtitle_intro_midtrack=True, subtitle_animation_duration=1,
                             subtitle_motion_easing="linear")
        before, start, _ = self.capture(source, 16.99999, spans, track)
        boundary, _, _ = self.capture(source, 17.00001, spans, track)
        middle, marker, _ = self.capture(source, 17.5, spans, track)
        final, _, _ = self.capture(source, 18, spans, track)
        for text in before:
            self.assertAlmostEqual(before[text], boundary[text], delta=0.02)
            self.assertAlmostEqual(middle[text], (boundary[text] + final[text]) / 2, delta=0.02)
        self.assertAlmostEqual(marker[0] - start[0], middle["Next B"] - boundary["Next B"], delta=0.02)

    def test_cascade_staggers_only_upcoming_cues_around_temporary_row(self):
        track = PlaylistTrack("", "Cascade", duration_seconds=30, lyrics=[
            {"start": a, "end": b, "text": text}
            for a, b, text in ((4, 6, "Held"), (17, 20, "Incoming"), (20, 24, "Future"), (24, 30, "Last"))
        ])
        common = dict(subtitle_intro_midtrack=True, subtitle_animation_duration=1,
                      subtitle_motion_easing="linear", subtitle_next_lines=3)
        plain, cascade = self.source(**common), self.source(subtitle_animation="cascade", **common)
        for seconds in (7.5, 17.5):
            baseline, marker, _ = self.capture(plain, seconds, ((7, 17),), track)
            rows, staggered_marker, _ = self.capture(cascade, seconds, ((7, 17),), track)
            self.assertAlmostEqual(rows["Held"], baseline["Held"])
            self.assertAlmostEqual(staggered_marker[0], marker[0])
            if seconds > 17:
                self.assertAlmostEqual(rows["Incoming"], baseline["Incoming"])
                self.assertNotAlmostEqual(rows["Future"], baseline["Future"])
            else:
                self.assertAlmostEqual(rows["Future"], baseline["Future"])

    def test_anchor_and_waiting_blur_are_consistent_for_multiline_cues(self):
        source = self.source(subtitle_context_lines=2, subtitle_next_lines=2,
                             subtitle_intro_midtrack=True, subtitle_anchor=0.4,
                             subtitle_role_styles={"previous": {"blur": 6}, "next": {"blur": 6}})
        source.subtitle_previous_distance_fade = source.subtitle_next_distance_fade = 1
        track = PlaylistTrack("", "Multiline", duration_seconds=25, lyrics=[
            {"start": start, "end": end, "text": f"Cue {index} A\nCue {index} B"}
            for index, (start, end) in enumerate(((4, 6), (6, 8), (8, 10), (20, 25)))
        ])
        scene = CanvasScene()
        scene.set_artboard_size(source.width, source.height)
        item = SourceItem(source)
        scene.addItem(item)
        radii, sharp, anchors = {}, [], []
        blur, draw = item._lyric_blur_pixmap, item._draw_text
        from app.canvas.renderers import lyrics_renderer
        intro_paint = lyrics_renderer.paint_intro

        def record_blur(text, color, radius, *args):
            radii[text] = radius
            return blur(text, color, radius, *args)

        def record_draw(painter, rect, flags, text):
            sharp.append((text, painter.worldTransform().map(rect.center()).y()))
            draw(painter, rect, flags, text)

        def record_intro(graphics, painter, rect, y):
            anchors.append(y)
            intro_paint(graphics, painter, rect, y)

        with patch.object(item, "_lyric_blur_pixmap", side_effect=record_blur), patch.object(
            item, "_draw_text", side_effect=record_draw,
        ), patch.object(lyrics_renderer, "paint_intro", side_effect=record_intro):
            CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=2,
                                         lyric_instrumental_spans=())
            self.assertAlmostEqual(anchors[-1], source.height * source.subtitle_anchor)
            for index in range(3):
                self.assertAlmostEqual(radii[f"Cue {index} A"], 2 * (index + 1))
                self.assertEqual(radii[f"Cue {index} A"], radii[f"Cue {index} B"])
            self.assertFalse(sharp)
            radii.clear()
            CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=12,
                                         lyric_instrumental_spans=((10, 20),))
            self.assertAlmostEqual(anchors[-1], source.height * source.subtitle_anchor)
            for index in range(3):
                self.assertAlmostEqual(radii[f"Cue {index} A"], 2 * (3 - index))
                self.assertEqual(radii[f"Cue {index} A"], radii[f"Cue {index} B"])
            self.assertFalse(sharp)  # Held LRC/current cues no longer use a separate blur.
            source.subtitle_next_lines = 4
            radii.clear()
            CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=2,
                                         lyric_instrumental_spans=())
            self.assertAlmostEqual(radii["Cue 0 A"], 6 / 5)
            source.subtitle_next_lines = 2
            CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=20.8,
                                         lyric_instrumental_spans=((10, 20),))
            current_centers = [y for text, y in sharp if text in {"Cue 3 A", "Cue 3 B"}]
            self.assertEqual(len(current_centers), 2)
            self.assertAlmostEqual(sum(current_centers) / 2, source.height * source.subtitle_anchor)

    def test_styles_animate_and_dots_light_in_order(self):
        frames = []
        for kind in INTRO_STYLE_LABELS:
            source = self.source(subtitle_intro_style=kind)
            _, _, a = self.capture(source, 0.9)
            _, _, b = self.capture(source, 1.5)
            self.assertNotEqual(bytes(a.constBits()), bytes(b.constBits()))
            frames.append(bytes(b.constBits()))
        self.assertEqual(len(set(frames)), len(INTRO_STYLE_LABELS))
        _, intro, image = self.capture(self.source(), 1.1)
        y = round(intro[0])
        # Left dot is bright, center is rising, right stays dim.
        levels = [max(image.pixelColor(x, y).alpha() for x in range(a, b))
                  for a, b in ((277, 289), (294, 306), (311, 323))]
        self.assertGreater(levels[0], levels[1])
        self.assertGreater(levels[1], levels[2])

    def test_settings_and_export_do_not_freeze_the_indicator(self):
        source, track = self.source(subtitle_intro_midtrack=True), self.track()
        restored = Source.from_dict(source.to_dict())
        self.assertEqual(restored.to_dict(), source.to_dict())
        for key, value in (("subtitle_intro_enabled", 1), ("subtitle_intro_midtrack", "yes"),
                           ("subtitle_intro_style", "bad"), ("subtitle_intro_gap", float("nan")),
                           ("subtitle_intro_period", 0), ("subtitle_intro_scale", 4)):
            with self.assertRaises(ValueError):
                Source.from_dict(source.to_dict() | {key: value})
        samples = ExportTimelinePlanner.build([track], [source], 30)
        waiting = [s for s in samples if s.elapsed_seconds < 4]
        gap = [s for s in samples if 6 <= s.elapsed_seconds < 17]
        self.assertGreaterEqual(len(waiting), 120)
        self.assertGreaterEqual(len(gap), 330)
        scene = CanvasScene()
        scene.set_artboard_size(600, 360)
        scene.addItem(SourceItem(source))
        capturer = ExportCanvasCapturer(scene, [track], 25, set(), [(None, None)],
                                       lambda *_: None, lambda: None, lambda *_: None)
        self.assertEqual(len(capturer.coalesce_samples(waiting, "base")), len(waiting))
        with patch("app.preview.export_canvas_capture.instrumental_spans", return_value=((7, 17),)):
            self.assertNotEqual(capturer._source_state_key(source, gap[100]),
                                capturer._source_state_key(source, gap[130]))
        dialog = LyricsAnimationDialog(source, Translator())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.category_toggles["intro"].setChecked(True)
        dialog.tabs.setCurrentIndex(3)
        dialog.controls["subtitle_intro_style"].setCurrentIndex(3)
        dialog.controls["subtitle_intro_period"].setValue(4.5)
        self.assertEqual(dialog.settings()["subtitle_advanced_settings"]["subtitle_intro_style"], "bars")
        self.assertEqual(dialog.settings()["subtitle_advanced_settings"]["subtitle_intro_period"], 4.5)
        dialog.intro_preview_mode.setCurrentIndex(1)
        dialog._render_preview(1)
        self.assertFalse(dialog.preview.pixmap().isNull())
        self.assertEqual(source.subtitle_intro_style, "dots")

    def test_analysis_is_nonblocking_and_failure_never_means_instrumental(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "song.wav"
            path.write_bytes(b"placeholder")
            track = self.track(); track.file_path = str(path)
            source = self.source(subtitle_intro_midtrack=True)
            pending = Future()
            with patch.object(service, "_executor") as executor:
                executor.submit.return_value = pending
                self.assertIs(service.request_instrumental_analysis(track, source), pending)
                self.assertEqual(service.instrumental_spans(track, source), ())
                executor.submit.assert_called_once()
                pending.set_result(((), ()))
                self.assertEqual(service.instrumental_spans(track, source), ())
                executor.submit.assert_called_once()
            service._jobs.clear()


if __name__ == "__main__":
    unittest.main()
