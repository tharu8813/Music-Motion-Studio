"""Animation data, clocks, editing policy, and preview/export regression checks."""
import json
import os
from copy import deepcopy
from dataclasses import replace
from unittest import TestCase

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRectF, Qt
from PySide6.QtTest import QSignalSpy, QTest

from app.animation.keyframes import (
    Keyframe, KeyframeAnimation, PROPERTIES, evaluate, evaluate_curve, normalize_curves,
)
from app.animation.keyframe_render import KeyframeRenderBinding, keyframe_windows
from app.controllers.keyframe_controller import KeyframeController
from app.models.source import Source, SourceType
from app.models.playlist import PlaylistTrack
from app.services.source_store import SourceStore
from app.timeline.track_schedule import resolve_track_windows
from app.timeline.compiler import compile_playlist
from app.timeline.crossfade import compile_crossfade


class KeyframeEngineTests(TestCase):
    def setUp(self):
        self.source = Source(SourceType.SHAPE, "Cover", x=10)
        self.a = PlaylistTrack("duplicate.wav", "Same", duration_seconds=10)
        self.b = PlaylistTrack("duplicate.wav", "Same", duration_seconds=20)

    def test_all_interpolations_and_endpoint_holds(self):
        expected = {"hold": 0, "linear": .25, "ease_in": .015625,
                    "ease_out": .578125, "ease_in_out": .0625}
        for easing, value in expected.items():
            points = [Keyframe(2, 0, easing), Keyframe(6, 1)]
            self.assertAlmostEqual(evaluate_curve(points, 3, -1), value)
            self.assertEqual(evaluate_curve(points, 0, -1), 0)
            self.assertEqual(evaluate_curve(points, 6, -1), 1)
            self.assertEqual(evaluate_curve(points, 100, -1), 1)

    def test_global_never_resets_at_track_change(self):
        self.source.keyframes.global_curves = {"x": [Keyframe(0, 0, "linear"), Keyframe(20, 200)]}
        self.assertEqual(evaluate(self.source, 12, self.b.id, 2), {"x": 120})
        self.assertEqual(evaluate(self.source, 12, self.a.id, 12), {"x": 120})

    def test_single_track_uses_id_and_inactive_falls_back(self):
        animation = self.source.keyframes
        animation.mode, animation.target_track_id = "single_track", self.b.id
        animation.single_track_curves[self.b.id] = {"scale": [Keyframe(0, 1), Keyframe(10, 2)]}
        self.assertEqual(evaluate(self.source, 15, self.a.id, 5), {})
        self.assertEqual(evaluate(self.source, 15, self.b.id, None), {})
        self.assertEqual(evaluate(self.source, 15, self.b.id, 5), {"scale": 1.5})

    def test_each_track_has_independent_properties_and_empty_new_tracks(self):
        animation = self.source.keyframes
        animation.mode = "per_track"
        animation.per_track_curves[self.a.id] = {"x": [Keyframe(0, 100)]}
        animation.per_track_curves[self.b.id] = {"opacity": [Keyframe(0, .5)]}
        self.assertEqual(evaluate(self.source, 1, self.a.id, 1), {"x": 100})
        self.assertEqual(evaluate(self.source, 11, self.b.id, 1), {"opacity": .5})
        self.assertEqual(evaluate(self.source, 20, "new-track", 0), {})
        for window in resolve_track_windows([self.b, self.a]):
            values = evaluate(self.source, window.start + 1, window.track.id, 1)
            self.assertIn("x" if window.track.id == self.a.id else "opacity", values)

    def test_modes_survive_json_roundtrip_without_aliasing(self):
        animation = self.source.keyframes
        animation.global_curves = {"x": [Keyframe(1, 20)]}
        animation.single_track_curves[self.a.id] = {"rotation": [Keyframe(2, 40)]}
        animation.per_track_curves[self.b.id] = {"scale": [Keyframe(3, 2)]}
        restored = Source.from_dict(json.loads(json.dumps(self.source.to_dict())))
        self.assertEqual(restored.keyframes, animation)
        restored.keyframes.global_curves.clear()
        self.assertTrue(animation.global_curves)
        for mode in ("per_track", "single_track", "global"):
            animation.mode = mode
        self.assertEqual(animation.global_curves["x"][0].value, 20)

    def test_legacy_project_has_no_animated_changes(self):
        data = self.source.to_dict()
        data.pop("keyframes", None)
        restored = Source.from_dict(data)
        self.assertFalse(restored.keyframes.animated)
        self.assertEqual(evaluate(restored, 100), {})

    def test_malformed_data_is_bounded_sorted_and_deduplicated(self):
        curves = normalize_curves({"x": [{"time": 2, "value": 12}, {"time": 0, "value": 2},
            {"time": 2, "value": 42}, {"time": -1, "value": 4}, {"time": float("nan"), "value": 2},
            {"time": 3, "value": float("inf")}, {"time": True, "value": 1}],
            "opacity": [{"time": 0, "value": 900}], "unknown": [{"time": 0, "value": 0}]})
        self.assertEqual([(p.time, p.value) for p in curves["x"]], [(0, 2), (2, 42)])
        self.assertEqual(curves["opacity"][0].value, 1)
        self.assertNotIn("unknown", curves)
        self.assertEqual(KeyframeAnimation.from_dict({"global_curves": None}).global_curves, {})

    def test_arbitrary_frame_order_and_fps_do_not_accumulate_state(self):
        self.source.keyframes.global_curves = {"rotation": [Keyframe(0, 0), Keyframe(5, 180)]}
        saved = self.source.to_dict()
        for fps in (24, 30, 60):
            for frame in (120, 0, 30, 120, 10):
                values = evaluate(self.source, frame / fps)
                self.assertEqual(values, evaluate(self.source, frame / fps))
        self.assertEqual(saved, self.source.to_dict())

    def test_canvas_resize_adapts_all_saved_banks_without_changing_key_times(self):
        from app.animation.keyframes import transform_positions
        animation = self.source.keyframes
        animation.global_curves = {"x": [Keyframe(3, 10)]}
        animation.single_track_curves[self.a.id] = {"y": [Keyframe(5, 20)]}
        animation.per_track_curves[self.b.id] = {"x": [Keyframe(7, 30)]}
        changed = transform_positions(animation, 2, 3, 4, 5)
        self.assertEqual(changed.global_curves["x"][0], Keyframe(3, 24))
        self.assertEqual(changed.single_track_curves[self.a.id]["y"][0], Keyframe(5, 65))
        self.assertEqual(changed.per_track_curves[self.b.id]["x"][0], Keyframe(7, 64))
        self.assertEqual(animation.global_curves["x"][0].value, 10)

    def test_render_binding_uses_crossfade_visual_owner_and_local_audio_clock(self):
        plan = compile_crossfade([self.a, self.b], 2)
        animation = self.source.keyframes
        animation.mode = "per_track"
        animation.per_track_curves = {self.a.id: {"x": [Keyframe(0, 100)]},
                                      self.b.id: {"x": [Keyframe(0, 200, "linear"), Keyframe(20, 400)]}}
        binding = KeyframeRenderBinding.build(self.source, plan)
        for time in (0, 8.25, 9, 10, 12, 27.9):
            owner = next(w for w in keyframe_windows(plan) if w.timeline_start <= time < w.timeline_end)
            local = owner.source_time_at_start + (time - owner.timeline_start) * owner.playback_rate
            self.assertEqual(binding.values(time), evaluate(self.source, time, owner.track_id, local))


class KeyframeEditingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = SourceStore()
        self.source = Source(SourceType.SHAPE, "Shape", x=10)
        self.store.add(self.source)
        self.tracks = [PlaylistTrack("a.wav", "A", duration_seconds=10), PlaylistTrack("b.wav", "B", duration_seconds=10)]
        self.controller = KeyframeController(self.store,
            lambda: [(w.track, w.start, w.end) for w in resolve_track_windows(self.tracks)])

    def test_scrubbing_and_reading_never_mutate_store(self):
        saved = deepcopy(self.source.to_dict())
        spy = QSignalSpy(self.store.source_changed)
        for time in (0, 2, 10, 0):
            self.controller.seek_global(time)
            self.controller.evaluated()
        self.assertEqual(spy.count(), 0)
        self.assertEqual(saved, self.source.to_dict())

    def test_auto_off_updates_only_existing_point(self):
        self.assertFalse(self.controller.auto_key)
        self.controller.toggle("x")
        self.assertTrue(self.controller.edit_value("x", 40))
        self.assertEqual(self.controller.curves()["x"][0].value, 40)
        self.controller.seek(3)
        self.assertFalse(self.controller.edit_value("x", 20))
        self.assertEqual(self.source.x, 10)
        self.controller.auto_key = True
        self.assertTrue(self.controller.edit_value("x", 20))
        self.assertEqual(len(self.controller.curves()["x"]), 2)

    def test_modes_preserve_data_copy_is_explicit_and_reset_is_local(self):
        self.controller.toggle("x")
        self.controller.configure(mode="per_track")
        self.controller.choose_track(self.tracks[0].id)
        self.controller.toggle("rotation")
        self.controller.copy()
        self.controller.choose_track(self.tracks[1].id)
        self.assertEqual(self.controller.curves(), {})
        self.controller.paste(whole_track=True)
        self.controller.clear_track()
        self.controller.choose_track(self.tracks[0].id)
        self.assertTrue(self.controller.curves()["rotation"])
        self.controller.configure(mode="global")
        self.assertTrue(self.controller.curves()["x"])

    def test_multi_selection_and_locked_sources_do_not_get_accidental_keys(self):
        self.store.add(Source(SourceType.TEXT, "Other"))
        self.store.select_many([s.id for s in self.store.sources()], self.source.id)
        self.controller.toggle("x")
        self.assertFalse(self.source.keyframes.animated)
        self.store.select(self.source.id)
        self.store.update(self.source.id, locked=True)
        self.controller.toggle("x")
        self.assertFalse(self.source.keyframes.animated)

    def test_missing_track_does_not_retarget_and_recovers_when_restored(self):
        self.controller.configure(mode="single_track", target=self.tracks[1].id)
        self.controller.choose_track(self.tracks[1].id)
        self.controller.toggle("x")
        removed = self.tracks.pop()
        self.assertFalse(self.controller.editable)
        self.assertEqual(self.source.keyframes.target_track_id, removed.id)
        self.tracks.append(removed)
        self.assertTrue(self.controller.editable)
        self.assertTrue(self.controller.curves()["x"])

    def test_canvas_capture_evaluates_and_restores_without_changing_project(self):
        from app.canvas.live_canvas import LiveCanvas
        from app.animation.canvas_keyframes import CanvasKeyframeBinding
        from app.preview.canvas_snapshot import CanvasSnapshot
        from app.utils.i18n import Translator
        canvas = LiveCanvas(self.store, Translator())
        self.addCleanup(canvas.deleteLater)
        # LiveCanvas normally receives add events after construction.
        canvas.replace_sources()
        canvas.scene_model.set_artboard_size(320, 180)
        self.source.width, self.source.height = 32, 32
        self.source.keyframes.global_curves = {"x": [Keyframe(0, 10, "linear"), Keyframe(10, 110)],
                                                "opacity": [Keyframe(0, 0), Keyframe(10, 1)]}
        canvas.update_source(self.source)
        binding = CanvasKeyframeBinding(canvas, self.controller)
        self.controller.seek_global(8)
        item = canvas._items[self.source.id]
        saved, position = self.source.to_dict(), item.pos()
        observed = []
        from unittest.mock import patch
        from PySide6.QtGui import QImage
        def capture(*args, **kwargs):
            observed.append((item.x(), self.source.opacity))
            return QImage(320, 180, QImage.Format.Format_RGBA8888)
        with patch.object(CanvasSnapshot, "capture", side_effect=capture):
            for time in (5, 0, 9):
                CanvasSnapshot.capture_track(canvas.scene_model, self.tracks[0], 1, 2, 0,
                    elapsed_seconds=time, timeline_seconds=time)
        self.assertEqual([entry[0] for entry in observed], [60, 10, 100])
        self.assertAlmostEqual(observed[0][1], .5)
        self.assertEqual(self.source.to_dict(), saved)
        self.assertEqual(item.pos(), position)
        self.assertFalse(item._suppress_position_sync)

    def test_animated_video_cannot_bypass_keyframes_through_direct_gpu_layers(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from app.dialogs.export_preview_dialog import ExportPreviewDialog
        source = Source(SourceType.VIDEO, "Video", image_fit_mode="stretch")
        source.keyframes.global_curves = {"x": [Keyframe(0, 200)]}
        item = SimpleNamespace(source=source, video_preview_active=True,
            video_preview_frame=Mock(side_effect=AssertionError("Must use Canvas capture")))
        preview = SimpleNamespace(gpu_preview_enabled=True, gpu_surface=object(), _active_render_scale=1,
            _cached_video_items=[item], _active_overlay_entries=lambda _: ())
        ids, layers = ExportPreviewDialog._direct_gpu_video_layers(preview, {source.id}, False, 1, {})
        self.assertEqual(ids, frozenset())
        self.assertEqual(layers, [])

    def test_shared_audio_overlay_adapter_applies_keyed_geometry_and_alpha(self):
        from types import SimpleNamespace
        from PySide6.QtGui import QImage
        from app.dialogs.export_preview_dialog import ExportPreviewDialog
        from app.renderer.ffmpeg_renderer import VisualizerOverlay
        source = Source(SourceType.AUDIO_VISUALIZER, "Bars", x=10, width=16, height=16)
        source.keyframes.global_curves = {"x": [Keyframe(0, 0), Keyframe(1, 100)],
            "scale": [Keyframe(0, 1), Keyframe(1, 2)], "opacity": [Keyframe(0, .25), Keyframe(1, .75)]}
        plan = compile_playlist(self.tracks)
        overlay = VisualizerOverlay(10, 0, 16, 16, "bars", "#FFFFFF",
            keyframe_binding=KeyframeRenderBinding.build(source, plan))
        image = QImage(16, 16, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.white)
        overlays, images = ExportPreviewDialog._apply_overlay_keyframes(SimpleNamespace(_compiled_plan=plan), [overlay], (image,), .5)
        self.assertEqual(overlays[0].x, 46)
        self.assertEqual((images[0].width(), images[0].height()), (24, 24))
        self.assertAlmostEqual(images[0].pixelColor(12, 12).alphaF(), .5, delta=.01)
        self.assertEqual(image.pixelColor(8, 8).alpha(), 255)
