"""Text music reaction follows the audio clock in preview and export."""

from copy import deepcopy
import os
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, QPointF
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import QApplication

from app.animation.canvas_preview import CanvasAnimationPreviewController
from app.animation.curves import AnimationPose, music_reactive_pose
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.export_preview_dialog import AudioAnalysisWorker, ExportPreviewDialog
from app.inspector.editors.base import MotionSection
from app.inspector.source_inspector import SourceInspector
from app.models.playlist import PlaylistTrack
from app.models.source import MUSIC_REACTIVE_EFFECTS, Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportTimelinePlanner
from app.renderer.ffmpeg_renderer import RenderFrame
from app.renderer.python_visualizer import PythonVisualizerError, PythonVisualizerRenderer
from app.services.source_store import SourceStore
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Translator


class TextMusicReactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_studio_style(cls.app)

    def test_toggle_reveals_text_and_current_lyrics_controls_and_roundtrips(self):
        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.close)
        source = Source(SourceType.TEXT, "Title", text="Bass")
        store.add(source)
        self.assertTrue(inspector._field_visibility["music_reactive_enabled"])
        self.assertFalse(inspector._field_visibility["music_reactive_effect"])
        toggle = inspector.motion.widgets["music_reactive_enabled"]
        with patch("sys.excepthook") as slot_error:
            toggle.click()
            slot_error.assert_not_called()
        self.assertTrue(source.uses_bass_reaction)
        self.assertTrue(inspector._field_visibility["music_reactive_effect"])
        combo = inspector.motion.widgets["music_reactive_effect"]
        self.assertEqual(combo.count(), 4)
        combo.setCurrentIndex(combo.findData("bass_tilt"))
        inspector.motion.widgets["music_reactive_strength"].setValue(0.4)
        self.assertEqual(Source.from_dict(source.to_dict()).music_reactive_effect, "bass_tilt")
        self.assertEqual(source.music_reactive_strength, 0.4)
        for key, value in (("music_reactive_attack", 0.2), ("music_reactive_release", 0.8),
                           ("music_reactive_sensitivity", 1.8), ("music_reactive_threshold", 0.3),
                           ("music_reactive_offset", -0.1)):
            inspector.motion.widgets[key].setValue(value)
            self.assertEqual(getattr(Source.from_dict(source.to_dict()), key), value)
            self.assertTrue(inspector.motion.widgets[key].toolTip())
        for key, value in (("music_reactive_band", "mid"), ("music_reactive_curve", "punchy")):
            widget = inspector.motion.widgets[key]
            widget.setCurrentIndex(widget.findData(value))
            self.assertEqual(getattr(Source.from_dict(source.to_dict()), key), value)
        with patch("sys.excepthook") as slot_error:
            toggle.click()
            slot_error.assert_not_called()
        self.assertFalse(inspector._field_visibility["music_reactive_effect"])
        self.assertFalse(source.uses_bass_reaction)
        self.assertEqual(source.music_reactive_effect, "bass_tilt")
        lyrics = Source(SourceType.LYRICS, "Lyrics")
        store.add(lyrics)
        self.assertTrue(inspector._field_visibility["music_reactive_enabled"])
        with patch("sys.excepthook") as slot_error:
            toggle.click()
            slot_error.assert_not_called()
        self.assertTrue(lyrics.uses_bass_reaction)
        self.assertTrue(all(inspector._field_visibility[key] for key in MotionSection.REACTIVE_KEYS))
        store.add(Source(SourceType.TIME, "Time"))
        self.assertFalse(inspector._field_visibility["music_reactive_enabled"])
        old = Source(SourceType.TEXT, "Old").to_dict()
        for key in MotionSection.REACTIVE_KEYS:
            old.pop(key)
        self.assertFalse(Source.from_dict(old).music_reactive_enabled)
        for key, bad in (("music_reactive_enabled", "yes"), ("music_reactive_effect", "unknown"),
                         ("music_reactive_strength", float("nan")), ("music_reactive_strength", 2),
                         ("music_reactive_attack", -1), ("music_reactive_release", float("nan")),
                         ("music_reactive_band", "unknown"), ("music_reactive_curve", "unknown"),
                         ("music_reactive_threshold", 1), ("music_reactive_sensitivity", 5),
                         ("music_reactive_offset", 3)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                Source.from_dict(source.to_dict() | {key: bad})

    def test_bass_attack_release_and_four_distinct_effects(self):
        levels = np.zeros((60, 24), dtype=np.float32)
        levels[10:15, :4] = 1
        envelope = PythonVisualizerRenderer.bass_envelope(levels, 30)
        self.assertGreater(envelope[10], 0.7)
        self.assertGreater(envelope[15], 0.7)
        self.assertLess(envelope[25], envelope[15])
        self.assertLess(envelope[-1], 0.001)
        faster = PythonVisualizerRenderer.bass_envelope(np.repeat(levels, 2, axis=0), 60)
        np.testing.assert_allclose(faster[1::2], envelope, atol=1e-6)
        np.testing.assert_array_equal(PythonVisualizerRenderer.bass_envelope(np.zeros_like(levels)), 0)
        poses = [music_reactive_pose(effect, 1, 0.25, 40) for effect in MUSIC_REACTIVE_EFFECTS]
        self.assertEqual(len(set(poses)), 4)
        self.assertEqual(poses[0].scale, 1.25)
        self.assertLess(poses[1].dy, 0)
        self.assertGreater(poses[2].scale_x, 1)
        self.assertNotEqual(poses[3].rotation, 0)
        for effect in MUSIC_REACTIVE_EFFECTS:
            self.assertEqual(music_reactive_pose(effect, 0, 0.25, 40), AnimationPose())

    def test_capture_export_and_editor_preview_restore_source(self):
        track = PlaylistTrack("", "Song", duration_seconds=2)
        source = Source(SourceType.TEXT, "Title", text="Bass", x=180, y=120,
                        width=240, height=90, font_size=36, fill_color="#00000000",
                        music_reactive_enabled=True)
        scene = CanvasScene()
        scene.set_artboard_size(640, 400)
        item = SourceItem(source)
        scene.addItem(item)
        original = deepcopy(source.to_dict())
        preview = ExportPreviewDialog(scene, [track], Translator(), preferred_backend="cpu")
        preview._refresh_source_partitions()
        self.assertTrue(preview._bass_reactive)
        self.assertIn(source.id, preview._cached_always_dynamic_ids)
        preview.close()
        preview.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        quiet = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, bass_level=0)
        images = []
        for effect in MUSIC_REACTIVE_EFFECTS:
            source.music_reactive_effect = effect
            image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, bass_level=1)
            self.assertNotEqual(image, quiet)
            images.append(image)
            self.assertEqual(item.scale(), source.scale)
            self.assertEqual(item.pos(), QPointF(source.x, source.y))
        self.assertTrue(all(images[i] != images[j] for i in range(4) for j in range(i)))
        self.assertFalse(CanvasSnapshot.source_is_capture_invariant(source, 2))
        samples = ExportTimelinePlanner.build([track], [source], 30)
        self.assertEqual(len(samples), 60)
        capturer = ExportCanvasCapturer(scene, [track], 2, set(), [(None, None)],
            lambda image, duration, _key: RenderFrame(image, duration), lambda: None,
            lambda *_: None, bass_envelopes={track.id: np.ones(60)}, bass_fps=30)
        self.assertEqual(len(capturer.coalesce_samples(samples, "base")), 60)
        self.assertEqual(capturer.capture_stream(samples[0], "base").image, images[-1])
        controller = CanvasAnimationPreviewController()
        self.assertTrue(controller.preview(item, source))
        controller.cancel()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(item.scale(), source.scale)
        self.assertEqual(item.rotation(), source.rotation)
        original["music_reactive_effect"] = source.music_reactive_effect
        self.assertEqual(source.to_dict(), original)
        source.music_reactive_enabled = False
        self.assertEqual(CanvasSnapshot.capture_track(scene, track, 1, 1, 0, bass_level=1), quiet)
        self.assertTrue(CanvasSnapshot.source_is_capture_invariant(source, 2))

    def test_response_timing_bands_gate_curves_offset_and_cancel(self):
        renderer = PythonVisualizerRenderer
        levels = np.zeros((120, 24), dtype=np.float32)
        levels[20:35, :4] = 1
        source = Source(SourceType.TEXT, "Title", music_reactive_attack=0,
                        music_reactive_release=0)
        instant = renderer.music_envelope(levels, 30, source.music_reaction_profile)
        self.assertEqual(instant[20], 1)
        self.assertEqual(instant[35], 0)
        source.music_reactive_attack = 0.5
        source.music_reactive_release = 1
        slow = renderer.music_envelope(levels, 30, source.music_reaction_profile)
        self.assertLess(slow[20], 0.2)
        self.assertGreater(slow[35], 0.8)
        doubled = renderer.music_envelope(np.repeat(levels, 2, axis=0), 60, source.music_reaction_profile)
        np.testing.assert_allclose(doubled[1::2], slow, atol=1e-6)
        for band in ("mid", "treble"):
            source.music_reactive_band = band
            np.testing.assert_array_equal(renderer.music_envelope(levels, 30, source.music_reaction_profile), 0)
        source.music_reactive_band = "bass"
        source.music_reactive_attack = source.music_reactive_release = 0
        source.music_reactive_sensitivity = 0.5
        source.music_reactive_threshold = 0.6
        np.testing.assert_array_equal(renderer.music_envelope(levels, 30, source.music_reaction_profile), 0)
        source.music_reactive_threshold = 0
        values = []
        for curve in ("punchy", "linear", "soft"):
            source.music_reactive_curve = curve
            values.append(renderer.music_envelope(levels, 30, source.music_reaction_profile)[20])
        self.assertLess(values[0], values[1])
        self.assertLess(values[1], values[2])
        self.assertEqual(renderer.music_reaction_level(instant, 30, 20 / 30 + 0.2, 0.2), 1)
        self.assertEqual(renderer.music_reaction_level(instant, 30, 0.1, 0.2), 0)
        self.assertEqual(renderer.music_reaction_level(instant, 30, 9), 0)
        cancelled = threading.Event(); cancelled.set()
        with self.assertRaises(PythonVisualizerError):
            renderer.music_envelope(levels, 30, source.music_reaction_profile, cancelled)

    def test_lyrics_react_only_current_multiline_cue_and_export_matches(self):
        track = PlaylistTrack("", "Song", duration_seconds=8, lyrics=[
            {"start": 0, "end": 2, "text": "Previous"},
            {"start": 2, "end": 4, "text": "Current A\nCurrent B"},
            {"start": 4, "end": 8, "text": "Next"},
        ])
        source = Source(SourceType.LYRICS, "Lyrics", width=600, height=400,
                        fill_color="#00000000", font_size=28, music_reactive_enabled=True,
                        subtitle_previous_blur=0, subtitle_animation="none", subtitle_current_scale=1)
        scene = CanvasScene(); scene.set_artboard_size(600, 400)
        item = SourceItem(source); scene.addItem(item)
        original = deepcopy(source.to_dict())

        def capture(level, elapsed=3):
            rows = {}
            draw = item._draw_text
            def observe(painter, rect, flags, text):
                rows[text] = (QTransform(painter.worldTransform()), painter.worldTransform().map(rect.center()))
                draw(painter, rect, flags, text)
            with patch.object(item, "_draw_text", side_effect=observe):
                image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=elapsed,
                                                     music_levels={source.id: level})
            self.assertEqual(source.to_dict(), original)
            self.assertEqual(item._music_reaction_level, 0)
            return rows, image

        quiet, _ = capture(0)
        for effect in MUSIC_REACTIVE_EFFECTS:
            source.music_reactive_effect = effect; original["music_reactive_effect"] = effect
            moving, image = capture(1)
            for text in ("Previous", "Next"):
                self.assertEqual(moving[text], quiet[text])
            self.assertNotEqual(moving["Current A"][0], quiet["Current A"][0])
            self.assertNotEqual(moving["Current B"][0], quiet["Current B"][0])
            self.assertEqual(moving["Current A"][0], moving["Current B"][0])
        samples = ExportTimelinePlanner.build([track], [source], 30)
        self.assertGreaterEqual(len(samples), 240)
        sample = min(samples, key=lambda sample: abs(sample.elapsed_seconds - 3))
        image = capture(1, sample.elapsed_seconds)[1]
        capturer = ExportCanvasCapturer(scene, [track], 8, set(), [(None, None)],
            lambda frame, duration, _key: RenderFrame(frame, duration), lambda: None,
            lambda *_: None, bass_envelopes={track.id: np.zeros(240),
                (track.id, source.music_reaction_profile): np.ones(240)})
        self.assertEqual(capturer.capture_stream(sample, "base").image, image)
        source.music_reactive_enabled = False; original["music_reactive_enabled"] = False
        self.assertEqual(capture(1)[0], quiet)
        source.music_reactive_enabled = True; original["music_reactive_enabled"] = True
        intro_track = track.lyrics[0]["start"]; track.lyrics[0]["start"] = 1
        self.assertEqual(capture(0, 0.5)[1], capture(1, 0.5)[1])
        track.lyrics[0]["start"] = intro_track
        full_preview = capture(1)[1]
        controller = CanvasAnimationPreviewController()
        self.assertTrue(controller.preview(item, source))
        controller._group.setCurrentTime(600)
        self.assertGreater(item._music_reaction_level, 0)
        self.assertEqual(item._music_preview_current_line, 0)
        self.assertEqual(source.subtitle_current_line, -1)
        self.assertEqual(source.to_dict(), original)
        self.assertEqual(CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=3,
                         music_levels={source.id: 1}), full_preview)
        self.assertEqual(item._music_preview_current_line, 0)
        self.assertEqual(item.scale(), source.scale)
        controller.cancel()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(item._music_reaction_level, 0)
        self.assertIsNone(item._music_preview_current_line)

    def test_preview_profiles_reuse_fft_and_remain_independent(self):
        source = Source(SourceType.LYRICS, "Lyrics", music_reactive_enabled=True)
        other = Source(SourceType.TEXT, "Title", music_reactive_enabled=True, music_reactive_band="treble")
        scene = CanvasScene(); scene.addItem(SourceItem(source)); scene.addItem(SourceItem(other))
        track = PlaylistTrack("", "Song", duration_seconds=4)
        renderer = PythonVisualizerRenderer(None)
        levels = np.zeros((120, 24), dtype=np.float32); levels[20:35, :4] = 1
        preview = ExportPreviewDialog(scene, [track], Translator(), preferred_backend="cpu")
        preview._refresh_source_partitions()
        preview.visualizer_renderer = renderer
        cached = {"levels": levels, "bass": renderer.bass_envelope(levels), "processed": (), "meters": ()}
        preview._track_levels[track.id] = cached
        profiles = (source.music_reaction_profile, other.music_reaction_profile)
        worker = AudioAnalysisWorker(renderer, track, 24, music_profiles=profiles, cached_analysis=cached)
        worker.ready.connect(preview._store_track_levels)
        with patch.object(renderer, "_decode_mono_audio", side_effect=AssertionError("Re-decoded cached audio")):
            worker.run()
        self.assertNotIn("music_envelopes", cached)
        values = preview._music_reaction_levels(track, 1)
        self.assertGreater(values[source.id], 0.9)
        self.assertEqual(values[other.id], 0)
        self.assertIsNone(preview._analysis_worker)
        source.music_reactive_offset = 1
        self.assertEqual(preview._music_reaction_levels(track, 1)[source.id], 0)
        source.music_reactive_release = 1.5
        preview._ensure_track_analysis(track, 24)
        self.assertIsNotNone(preview._analysis_worker)
        self.assertIs(preview._analysis_worker.cached_analysis, preview._track_levels[track.id])
        preview.close(); preview.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_full_preview_refreshes_lyrics_when_audio_analysis_arrives(self):
        source = Source(SourceType.LYRICS, "Lyrics", width=600, height=400,
                        music_reactive_enabled=True, subtitle_karaoke_enabled=True,
                        subtitle_animation="none", font_size=28)
        scene = CanvasScene(); scene.set_artboard_size(600, 400)
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("", "Song", duration_seconds=8,
                              lyrics=[{"start": 0, "end": 8, "text": "Singing words"}])
        preview = ExportPreviewDialog(scene, [track], Translator(), preferred_backend="cpu")
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview.close)
        preview.timeline.setValue(300)
        preview.refresh_preview()
        quiet = preview._image.copy()
        preview._store_track_levels(track.id, preview.preview_fps, {
            "bass": np.ones(8 * preview.preview_fps),
            "music_envelopes": {source.music_reaction_profile: np.ones(8 * preview.preview_fps)},
        })
        preview.refresh_preview()
        self.assertNotEqual(preview._image, quiet)

    def test_inspector_groups_karaoke_and_hides_conflicting_accent_toggle(self):
        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.close)
        source = Source(SourceType.LYRICS, "Lyrics")
        store.add(source)
        group = inspector._sections[("special", "sub_karaoke")]
        self.assertIs(inspector.lyrics_karaoke_button.parentWidget(), group._body)
        self.assertTrue(inspector._field_visibility["subtitle_accent_enabled"])
        self.assertFalse(inspector._field_visibility["subtitle_karaoke_offset"])
        inspector.lyrics.widgets["subtitle_karaoke_enabled"].click()
        self.assertTrue(source.subtitle_karaoke_enabled)
        self.assertFalse(inspector._field_visibility["subtitle_accent_enabled"])
        self.assertTrue(inspector._field_visibility["subtitle_karaoke_offset"])
        for key in ("subtitle_karaoke_enabled", "subtitle_karaoke_offset", "subtitle_accent_color"):
            self.assertEqual(inspector._field_sections[key], ("special", "sub_karaoke"))


if __name__ == "__main__":
    unittest.main()
