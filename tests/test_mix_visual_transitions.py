"""Crossfade/AutoMix Canvas handover: mid-overlap owner change and mix animations."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.preview.overlay_frame_cache import OverlayFrameCache
from app.dialogs.export_preview_dialog import ExportPreviewDialog  # noqa: E402
from app.inspector.source_inspector import SourceInspector  # noqa: E402
from app.models.source import Source, SourceType  # noqa: E402
from app.preview.frame_state import MixJunction, resolve_edge_animation  # noqa: E402
from app.renderer.export_timeline import ExportTimelinePlanner  # noqa: E402
from app.renderer.python_visualizer import PythonVisualizerRenderer  # noqa: E402
from app.services.source_store import SourceStore  # noqa: E402
from app.timeline.models import TransitionType  # noqa: E402
from app.timeline.render_plan import (  # noqa: E402
    AudioRenderClip, AudioRenderPlan, AudioRenderTransition, CompiledRenderPlan,
    build_presentation_and_metadata, visual_segments,
)
from app.utils.i18n import Translator  # noqa: E402
from tests.test_automix_planner import _track  # noqa: E402


def _crossfaded_plan() -> CompiledRenderPlan:
    """a: 0-60, b: 52-112, an 8 s crossfade from 52: the Canvas hands over at 56."""
    clips = (AudioRenderClip("a", "a", 0, 0, 60), AudioRenderClip("b", "b", 52, 0, 60))
    presentation, metadata, duration = build_presentation_and_metadata(clips)
    transitions = (AudioRenderTransition("a", "b", 52.0, 8.0, TransitionType.CROSSFADE),)
    return CompiledRenderPlan(AudioRenderPlan(clips, transitions), presentation, metadata, duration)


def _fading_text() -> Source:
    source = Source(SourceType.TEXT, "Title")
    source.animation_in = "fade"
    source.animation_out = "fade"
    source.animation_in_duration = 1.0
    source.animation_out_duration = 1.0
    return source


class MixVisualTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_canvas_hands_over_in_the_middle_of_the_overlap(self) -> None:
        plan = _crossfaded_plan()
        first, second = visual_segments(plan)
        self.assertAlmostEqual(first.end, 56.0)
        self.assertAlmostEqual(second.start, 56.0)
        self.assertTrue(first.mixed_out and second.mixed_in)
        self.assertFalse(first.mixed_in or second.mixed_out)
        self.assertAlmostEqual(first.mix_out_seconds, 4.0)
        self.assertAlmostEqual(second.mix_in_seconds, 4.0)
        # Chapters keep the audible start.
        self.assertAlmostEqual(plan.metadata.chapters[1].start, 52.0)

    def test_track_animation_runs_from_the_handover_clock(self) -> None:
        source = _fading_text()
        source.animation_in = "slide_up"
        kwargs = dict(elapsed_seconds=4.0, track_duration=60.0, phase_duration=0.45)
        self.assertEqual(
            resolve_edge_animation(source, "in", junction=MixJunction(0.25, None), **kwargs),
            ("in", "slide_up", 0.25),
        )
        self.assertEqual(
            resolve_edge_animation(source, None, junction=MixJunction(None, 0.25), **kwargs),
            ("out", "fade", 0.75),
        )
        # After the entrance, the plain track-start phase never replays there.
        self.assertIsNone(resolve_edge_animation(source, "in", junction=MixJunction(2.0, None), **kwargs))

    def test_fit_to_mix_stretches_over_each_half_of_the_overlap(self) -> None:
        source = _fading_text()
        junction = MixJunction(2.0, 1.0, mix_in_seconds=4.0, mix_out_seconds=8.0)
        kwargs = dict(elapsed_seconds=0.0, track_duration=60.0)
        self.assertIsNone(resolve_edge_animation(source, None, junction=junction, **kwargs))
        source.animation_fit_mix = True
        self.assertEqual(resolve_edge_animation(source, None, junction=junction, **kwargs),
                         ("in", "fade", 0.5))
        exit_only = MixJunction(None, 2.0, mix_out_seconds=8.0)
        self.assertEqual(resolve_edge_animation(source, None, junction=exit_only, **kwargs),
                         ("out", "fade", 0.75))

    def test_visualizer_layers_follow_the_handover_and_fit(self) -> None:
        overlay = SimpleNamespace(animation_in="fade", animation_out="fade",
                                  animation_in_duration=1.0, animation_out_duration=1.0,
                                  animation_fit_mix=True)
        windows = [(0.0, 56.0, 0.0, 4.0), (56.0, 56.0, 4.0, 0.0)]
        self.assertEqual(PythonVisualizerRenderer._animation_state(54.0, windows, overlay),
                         ("fade", 0.5, False))
        self.assertEqual(PythonVisualizerRenderer._animation_state(57.0, windows, overlay),
                         ("fade", 0.25, True))
        self.assertEqual(PythonVisualizerRenderer._track_index_at(55.9, windows), 0)
        self.assertEqual(PythonVisualizerRenderer._track_index_at(56.1, windows), 1)

    def test_preview_animates_reactive_layers_at_the_handover(self) -> None:
        overlay = SimpleNamespace(animation_in="fade", animation_out="fade",
                                  animation_in_duration=1.0, animation_out_duration=1.0)
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor(255, 255, 255, 255))
        preview = SimpleNamespace(_compiled_plan=_crossfaded_plan())

        def alpha(seconds: float) -> int:
            (result,) = ExportPreviewDialog._animated_overlay_images(preview, [overlay], (image,), seconds)
            return result.pixelColor(4, 4).alpha()

        self.assertEqual(alpha(30.0), 255)          # mid-track: untouched
        self.assertLess(alpha(55.8), 128)           # outgoing exit, just before the handover
        self.assertLess(alpha(56.2), 128)           # incoming entrance, just after it
        self.assertEqual(alpha(58.0), 255)

    def test_legacy_mix_style_keys_still_load(self) -> None:
        data = _fading_text().to_dict()
        data.update(mix_animation_in="zoom", mix_animation_out="same", mix_animation_duration=0.8)
        self.assertFalse(Source.from_dict(data).animation_fit_mix)

    def test_preview_keeps_visualizer_frames_of_the_drawn_track(self) -> None:
        plan = _crossfaded_plan()
        tracks = [_track("a", 60), _track("b", 60)]
        fps = 30
        preview = SimpleNamespace(
            tracks=tracks, _compiled_plan=plan, preview_fps=fps, _overlay_prefetch_count=6,
            timeline=SimpleNamespace(value=lambda: round(54.0 * 1000)),
            _overlay_cache=OverlayFrameCache(),
        )
        preview._overlay_cache.store("a", (0,), [(54 * fps, ())])
        preview._overlay_cache.store("b", (0,), [(2 * fps, ())])
        preview._visual_track_at = lambda seconds: ExportPreviewDialog._visual_track_at(preview, seconds)
        with patch("app.dialogs.export_preview_dialog.TIMELINE_SCALE", 1000):
            ExportPreviewDialog._trim_overlay_frame_cache(preview, "a")
        self.assertIsNone(preview._overlay_cache.first_missing("a", (0,), 54 * fps, 1))
        self.assertEqual(preview._overlay_cache.first_missing("b", (0,), 2 * fps, 1), 2 * fps)

    def test_export_and_preview_share_the_handover(self) -> None:
        plan = _crossfaded_plan()
        tracks = [_track("a", 60), _track("b", 60)]
        samples = ExportTimelinePlanner.build(tracks, [_fading_text()], 30, plan)
        self.assertAlmostEqual(sum(sample.duration_seconds for sample in samples), plan.duration_seconds)
        position = 0.0
        for sample in samples:
            self.assertEqual(sample.track.id, "a" if position < 56.0 - 1e-6 else "b")
            position += sample.duration_seconds
        entrance = [s for s in samples if s.track.id == "b" and s.junction
                    and s.junction.visual_elapsed is not None and s.junction.visual_elapsed < 1.0]
        self.assertGreaterEqual(len(entrance), 25)  # dense, about 30 fps over 1 s
        self.assertAlmostEqual(entrance[0].elapsed_seconds, 4.0, places=3)
        exit_frames = [s for s in samples if s.track.id == "a" and s.junction
                       and s.junction.visual_remaining is not None and s.junction.visual_remaining < 1.0]
        self.assertAlmostEqual(min(s.junction.visual_remaining for s in exit_frames), 0.0)

        preview = SimpleNamespace(tracks=tracks, _compiled_plan=plan)
        selected, junction = ExportPreviewDialog._visual_track_at(preview, 55.5)
        self.assertEqual(selected[1].id, "a")
        self.assertAlmostEqual(junction.visual_remaining, 0.5)
        selected, junction = ExportPreviewDialog._visual_track_at(preview, 56.5)
        self.assertEqual(selected[1].id, "b")
        self.assertAlmostEqual(selected[2], 4.5)
        self.assertAlmostEqual(junction.visual_elapsed, 0.5)
        # Audio transport still switches when b starts playing.
        self.assertEqual(ExportPreviewDialog._track_at(preview, 53.0)[1].id, "b")

    def test_inspector_shows_mix_rows_only_while_the_project_mixes(self) -> None:
        store = SourceStore()
        source = _fading_text()
        store.add(source)
        inspector = SourceInspector(store, Translator())
        try:
            inspector.set_source(source)
            self.assertFalse(inspector._field_visibility.get("animation_fit_mix", False))
            inspector.set_mix_transitions_active(True)
            self.assertTrue(inspector._field_visibility["animation_fit_mix"])
            inspector.animation_fit_mix_check.setChecked(True)
            self.assertTrue(source.animation_fit_mix)
            inspector.set_mix_transitions_active(False)
            self.assertFalse(inspector._field_visibility["animation_fit_mix"])
        finally:
            inspector.close()
            inspector.deleteLater()


if __name__ == "__main__":
    unittest.main()
