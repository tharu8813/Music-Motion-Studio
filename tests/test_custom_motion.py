"""Normalized relative entrance/exit motion, project compatibility and output."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless

import numpy as np
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from app.animation.custom_motion import CustomMotion, MotionPoint, source_motion
from app.animation.keyframes import Keyframe, evaluate
from app.animation.keyframe_render import KeyframeRenderBinding, ffmpeg_transform
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.timeline.compiler import compile_playlist
from tests import test_keyframe_rendering as render_tests


class MotionModelTests(TestCase):
    def test_continuity_uses_applied_tracks_tolerances_and_keeps_intentional_difference(self):
        from app.animation.custom_motion import exit_pose_differences, pose_differences
        from app.animation.curves import AnimationPose
        intro = CustomMotion.default().with_key("dx", 1, value=100).with_key("scale", 1, value=1.2)
        outro = CustomMotion.default(False).with_key("dx", 0, value=800).set_field_enabled("dx", False)
        source = Source(SourceType.SHAPE, "Link", animation_in="custom", animation_out="custom",
            custom_intro_keyframes=intro, custom_outro_keyframes=outro)
        saved = deepcopy(source.to_dict())
        self.assertEqual(exit_pose_differences(source), ("dx", "scale"))
        self.assertEqual(source.to_dict(), saved)
        self.assertEqual(pose_differences(AnimationPose(), AnimationPose(dx=.1, rotation=.1, scale=1.001, opacity=.999)), ())
        self.assertEqual(pose_differences(AnimationPose(), AnimationPose(rotation=360)), ())
        self.assertEqual(pose_differences(AnimationPose(), AnimationPose(rotation=359.8)), ("rotation",))
        source.custom_intro_keyframes = intro.set_field_enabled("dx", False).set_field_enabled("scale", False)
        self.assertEqual(exit_pose_differences(source), ())

    def test_explicit_link_changes_only_first_values_and_enables_necessary_tracks(self):
        from app.animation.curves import AnimationPose
        outro = CustomMotion.default(False).with_key("dx", .6, value=80, interpolation="bezier", bezier=(.1, -.2, .8, 1.3), custom=True)
        outro = outro.with_key("dx", 0, interpolation="hold").set_field_enabled("dx", False)
        linked = outro.with_start_pose(AnimationPose(dx=100, scale=1.2, rotation=15, opacity=.7))
        self.assertEqual(linked.at(0), AnimationPose(dx=100, scale=1.2, rotation=15, opacity=.7))
        self.assertTrue(linked.field_enabled("dx"))
        self.assertEqual(linked.points, outro.points)
        for name in ("dx", "scale", "rotation", "opacity"):
            self.assertEqual(linked.keys(name)[1:], outro.keys(name)[1:])
            self.assertEqual(linked.keys(name)[0].interpolation, outro.keys(name)[0].interpolation)
            self.assertEqual(linked.keys(name)[0].bezier, outro.keys(name)[0].bezier)
        self.assertEqual(linked.curves[0].custom_segments, (.6,))
        self.assertEqual(linked.keys("dy"), outro.keys("dy"))
        self.assertFalse(outro.field_enabled("dx"))

    def test_resting_policy_for_custom_preset_and_missing_edges(self):
        from app.animation.custom_motion import exit_pose_differences, resting_pose
        from app.animation.curves import AnimationPose
        intro = CustomMotion.default().with_key("dx", 1, value=50)
        for entrance in ("custom", "fade", "none"):
            for exit in ("custom", "fade", "none"):
                source = Source(SourceType.SHAPE, "Policy", animation_in=entrance, animation_out=exit,
                    custom_intro_keyframes=intro, custom_outro_keyframes=CustomMotion.default(False))
                self.assertEqual(resting_pose(source).dx, 50 if entrance == "custom" else 0)
                self.assertEqual(exit_pose_differences(source), ("dx",) if entrance == "custom" and exit == "custom" else ())
                if entrance != "custom":
                    self.assertEqual(resting_pose(source), AnimationPose())

    def test_disabled_track_preserves_saved_keys_and_uses_identity_in_output(self):
        from dataclasses import asdict
        motion = CustomMotion.default().with_key("dx", .4, value=90).with_key("scale", .7, value=2)
        disabled = motion.set_field_enabled("dx", False).set_field_enabled("opacity", False)
        restored = CustomMotion.from_dict(json.loads(json.dumps(asdict(disabled))))
        self.assertEqual(restored, disabled)
        self.assertEqual(restored.keys("dx"), motion.keys("dx"))
        self.assertEqual((restored.at(.4).dx, restored.at(.4).opacity), (0, 1))
        self.assertEqual(restored.at(.7).scale, 2)
        source = Source(SourceType.SHAPE, "Disabled", x=400, opacity=.5,
            animation_in="custom", custom_intro_keyframes=restored)
        binding = KeyframeRenderBinding.build(source, compile_playlist([PlaylistTrack("", "A", duration_seconds=10)]))
        self.assertEqual(binding.values(.1)["x"], 400)
        self.assertEqual(binding.values(.1)["opacity"], .5)
        self.assertEqual(restored.set_field_enabled("dx", True).at(.4).dx, 90)

    def test_independent_property_times_and_curves_survive_json_without_resampling(self):
        from dataclasses import asdict
        motion = CustomMotion.default().with_key("dx", .3, value=80, interpolation="linear")
        motion = motion.with_key("opacity", .7, value=.6, interpolation="bezier", bezier=(.2, -.3, .8, 1.2), custom=True)
        self.assertEqual([k.time for k in motion.keys("dx")], [0, .3, 1])
        self.assertEqual([k.time for k in motion.keys("opacity")], [0, .7, 1])
        self.assertEqual([k.time for k in motion.keys("scale")], [0, 1])
        restored = CustomMotion.from_dict(json.loads(json.dumps(asdict(motion))))
        self.assertEqual(restored, motion)
        for t in (.9, .1, .7, .3, .5, 0, 1):
            self.assertEqual(restored.at(t), motion.at(t))
        deleted = restored.without_key("dx", .3)
        self.assertEqual(deleted.keys("opacity"), restored.keys("opacity"))
        moved = restored.move_key("opacity", .7, .8)
        self.assertEqual(moved.curves[1].custom_segments, (.8,))
        self.assertEqual(moved.keys("dx"), restored.keys("dx"))
        changed = restored.with_key("dx", 0, value=50).with_point(0, opacity=.2)
        self.assertEqual(changed.at(0).dx, 50)

    def test_corrupt_property_curves_do_not_overwrite_other_properties(self):
        motion = CustomMotion.from_dict({"curves": [{"field": "dx", "keys": [
            {"time": .3, "value": 20}, {"time": .7, "value": 10**400},
            {"time": .5, "value": float("nan")}], "custom_segments": [None, {}, .3]},
            {"field": "opacity", "keys": "broken"}, {"field": "unknown", "keys": []}]})
        self.assertEqual([k.time for k in motion.keys("dx")], [0, .3, 1])
        self.assertEqual(motion.at(.3).dx, 20)
        self.assertEqual(motion.keys("opacity"), CustomMotion.default().keys("opacity"))

    def test_normalized_time_duration_and_base_changes_preserve_relative_motion(self):
        motion = CustomMotion((MotionPoint(0, dx=-80, scale=.8, opacity=0),
                               MotionPoint(.5, dx=10, scale=1.1), MotionPoint(1)))
        source = Source(SourceType.SHAPE, "Motion", x=400, opacity=.5, animation_in="custom",
                        custom_intro_keyframes=motion)
        pose = source_motion(source, .5, True)
        self.assertEqual((source.x + pose.dx, source.scale * pose.scale, source.opacity * pose.opacity), (410, 1.1, .5))
        source.x, source.animation_in_duration = 600, 1
        self.assertEqual(source.x + source_motion(source, .5, True).dx, 610)
        self.assertEqual(motion.points[1].progress * source.animation_in_duration, .5)

    def test_intro_outro_preset_switch_and_legacy_banks_survive_json(self):
        source = Source(SourceType.SHAPE, "Motion", animation_in="custom", animation_out="custom",
                        custom_intro_keyframes=CustomMotion.default(True), custom_outro_keyframes=CustomMotion.default(False))
        source.keyframes.global_curves = {"x": [Keyframe(0, 900)]}
        self.assertEqual(evaluate(source, 0), {})
        source.animation_in = source.animation_out = "fade"
        source.animation_in = source.animation_out = "custom"
        restored = Source.from_dict(json.loads(json.dumps(source.to_dict())))
        self.assertEqual(restored, source)
        self.assertEqual(source_motion(restored, 0, True).opacity, 0)
        self.assertEqual(source_motion(restored, 0, False).opacity, 1)
        self.assertEqual(restored.keyframes.global_curves["x"], [Keyframe(0, 900)])

    def test_entrance_end_pose_holds_until_exit_without_rewriting_base(self):
        motion = CustomMotion((MotionPoint(0, opacity=0), MotionPoint(1, dx=30, scale=1.2)))
        source = Source(SourceType.SHAPE, "Hold", x=400, animation_in="custom", custom_intro_keyframes=motion)
        track = PlaylistTrack("", "A", duration_seconds=10)
        binding = KeyframeRenderBinding.build(source, compile_playlist([track]))
        self.assertEqual(binding.values(5)["x"], 430)
        self.assertEqual(binding.values(5)["scale"], 1.2)
        self.assertEqual(source.x, 400)

    def test_add_interpolated_pose_and_protected_endpoints(self):
        motion = CustomMotion.default()
        expanded = motion.with_point(.3)
        self.assertEqual(expanded.at(.3), motion.at(.3))
        self.assertEqual(expanded.without_point(0), expanded)
        self.assertEqual(expanded.without_point(1), expanded)
        self.assertEqual(expanded.without_point(.3), motion)

    def test_malformed_motion_repairs_only_its_own_points(self):
        restored = CustomMotion.from_dict({"points": [{"progress": .5, "dx": 40},
            {"progress": float("nan")}, {"progress": .7, "opacity": float("inf")}]})
        self.assertEqual([p.progress for p in restored.points], [0, .5, 1])
        self.assertEqual(restored.at(.5).dx, 40)
        self.assertEqual(CustomMotion.from_dict(None), CustomMotion.default())

    def test_explicit_custom_duration_survives_legacy_shared_duration(self):
        source = Source(SourceType.SHAPE, "Legacy duration", animation_duration=2,
            animation_in="custom", animation_in_duration=.45, animation_out_duration=.45,
            custom_intro_keyframes=CustomMotion.default())
        restored = Source.from_dict(source.to_dict())
        self.assertEqual(restored.animation_in_duration, .45)
        self.assertEqual(restored.animation_out_duration, .45)


@skipUnless(render_tests.FFMPEG, "FFmpeg is unavailable")
class MotionRenderTests(TestCase):
    ffmpeg = render_tests.KeyframeFfmpegTests.ffmpeg

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_linked_boundary_in_actual_mp4_matches_canvas_without_alpha_reset(self):
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot
        intro = CustomMotion.default().with_key("dx", 1, value=40).with_key("scale", 1, value=1.2)
        intro = intro.with_key("rotation", 1, value=15).with_key("opacity", 1, value=.7)
        outro = CustomMotion.default(False).with_start_pose(intro.at(1))
        source = Source(SourceType.SHAPE, "Linked", x=35, y=35, width=24, height=24, opacity=.4,
            fill_color="#FFFFFF", animation_in="custom", animation_out="custom", animation_in_duration=1,
            animation_out_duration=1, custom_intro_keyframes=intro, custom_outro_keyframes=outro)
        track = PlaylistTrack("", "A", duration_seconds=3)
        plan = compile_playlist([track])
        binding = KeyframeRenderBinding.build(source, plan)
        graph = []
        layer, x, y = ffmpeg_transform(graph, "[1:v]", binding, 24, 24, "link")
        graph.append(f"[0:v]{layer}overlay=x='{x}':y='{y}':format=rgb[out]")
        frames = self.ffmpeg(["color=black:s=160x100:r=10:d=3", "color=white:s=24x24:r=10:d=3,format=rgba"],
            ";".join(graph), width=160, height=100, duration=3, encode=True)
        scene = CanvasScene()
        scene.set_artboard_size(160, 100)
        scene.artboard_color = scene.artboard_border_color = QColor("black")
        scene.addItem(SourceItem(source))
        self.assertEqual(binding.pose(2-1e-8), binding.pose(2))
        for i in (0, 5, 10, 19, 20, 21, 25, 29):
            image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=i/10,
                timeline_seconds=i/10, compiled_plan=plan)
            self.assertAlmostEqual(float(frames[i].max()), binding.values(i/10)["opacity"]*255, delta=3)
            if i not in (0, 29):
                ys, xs = np.nonzero(frames[i, :, :, 0] > 5)
                qt = image.convertToFormat(QImage.Format.Format_RGB888)
                pixels = np.frombuffer(qt.constBits(), dtype=np.uint8).reshape(100, qt.bytesPerLine())[:, :480].reshape(100, 160, 3)
                qy, qx = np.nonzero(pixels[:, :, 0] > 5)
                self.assertAlmostEqual(xs.mean(), qx.mean(), delta=1.5)
                self.assertAlmostEqual(ys.mean(), qy.mean(), delta=1.5)

    def test_isolated_and_compiled_canvas_hold_custom_entrance_under_preset_exit(self):
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot
        motion = CustomMotion.default().with_key("dx", 1, value=30).with_key("opacity", 1, value=.6)
        track = PlaylistTrack("", "A", duration_seconds=4)
        plan = compile_playlist([track])
        for style in ("none", "fade", "slide_left", "rotate", "zoom"):
            source = Source(SourceType.SHAPE, "Held", x=50, y=30, width=24, height=24, fill_color="#FFFFFF",
                opacity=.5, animation_in="custom", animation_in_duration=1, animation_out=style, animation_out_duration=1,
                custom_intro_keyframes=motion)
            scene = CanvasScene()
            scene.set_artboard_size(160, 100)
            scene.artboard_color = scene.artboard_border_color = QColor("black")
            scene.addItem(SourceItem(source))
            for seconds, phase, progress in ((2, None, 1), (3, "out", 0), (3.5, "out", .5)):
                isolated = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=seconds,
                    timeline_seconds=seconds, animation_phase=phase, animation_progress=progress)
                compiled = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=seconds,
                    timeline_seconds=seconds, compiled_plan=plan, animation_phase=phase, animation_progress=progress)
                self.assertEqual(isolated, compiled, (style, seconds))

    def test_relative_motion_ffmpeg_matches_canvas_and_base_alpha_once(self):
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot
        source = Source(SourceType.SHAPE, "Motion", x=30, y=30, width=32, height=32,
            fill_color="#FFFFFF", opacity=.5, animation_in="custom", animation_in_duration=1,
            custom_intro_keyframes=CustomMotion((MotionPoint(0, dx=-20, scale=.8, rotation=-20, opacity=0),
                MotionPoint(.5, dx=20, scale=1.4, rotation=30, opacity=.5), MotionPoint(1))))
        track = PlaylistTrack("", "A", duration_seconds=2)
        binding = KeyframeRenderBinding.build(source, compile_playlist([track]))
        graph = []
        layer, x, y = ffmpeg_transform(graph, "[1:v]", binding, 32, 32, "custom")
        graph.append(f"[0:v]{layer}overlay=x='{x}':y='{y}':format=rgb[out]")
        frames = self.ffmpeg(["color=black:s=160x100:r=10:d=1", "color=white:s=32x32:r=10:d=1,format=rgba"],
                             ";".join(graph), width=160, height=100, encode=True)
        scene = CanvasScene()
        scene.set_artboard_size(160, 100)
        scene.artboard_color = scene.artboard_border_color = QColor("black")
        scene.addItem(SourceItem(source))
        saved = deepcopy(source.to_dict())
        for i in (0, 3, 5, 9):
            image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=i / 10,
                timeline_seconds=i / 10, animation_phase="in", animation_progress=i / 10)
            qt = image.convertToFormat(QImage.Format.Format_RGB888)
            pixels = np.frombuffer(qt.constBits(), dtype=np.uint8).reshape(100, qt.bytesPerLine())[:, :480].reshape(100, 160, 3)
            self.assertAlmostEqual(float(frames[i].max()), float(pixels.max()), delta=3)
            if i:
                for data in (frames[i], pixels):
                    ys, xs = np.nonzero(data[:, :, 0] > 5)
                    self.assertAlmostEqual(xs.mean(), source.x + source.width / 2 + binding.pose(i / 10).dx - .5, delta=1.5)
                    self.assertAlmostEqual(ys.mean(), source.y + source.height / 2 - .5, delta=1.5)
        self.assertEqual(source.to_dict(), saved)

    def test_ffmpeg_evaluates_independent_property_schedules(self):
        motion = CustomMotion.default().with_key("dx", .2, value=25, interpolation="linear")
        motion = motion.with_key("dy", .7, value=12).with_key("opacity", .4, value=.8)
        motion = motion.with_key("scale", .6, value=1.4).with_key("rotation", .3, value=25)
        motion = motion.set_field_enabled("rotation", False)
        self.assertEqual(len(motion.keys("rotation")), 3)
        self.assertEqual(motion.at(.3).rotation, 0)
        source = Source(SourceType.SHAPE, "Lanes", x=35, y=30, width=24, height=24, opacity=.5,
            animation_in="custom", animation_in_duration=1, custom_intro_keyframes=motion)
        binding = KeyframeRenderBinding.build(source, compile_playlist([PlaylistTrack("", "A", duration_seconds=3)]))
        graph = []
        layer, x, y = ffmpeg_transform(graph, "[1:v]", binding, 24, 24, "lanes")
        graph.append(f"[0:v]{layer}overlay=x='{x}':y='{y}':format=rgb[out]")
        frames = self.ffmpeg(["color=black:s=160x100:r=10:d=1", "color=white:s=24x24:r=10:d=1,format=rgba"],
            ";".join(graph), width=160, height=100, encode=True)
        for i in (2, 4, 6, 8, 9):
            pose = binding.pose(i/10)
            self.assertAlmostEqual(float(frames[i].max()), pose.opacity*.5*255, delta=3)
            ys, xs = np.nonzero(frames[i, :, :, 0] > 5)
            self.assertAlmostEqual(xs.mean(), source.x+11.5+pose.dx, delta=1.5)
            self.assertAlmostEqual(ys.mean(), source.y+11.5+pose.dy, delta=1.5)

    def test_custom_motion_follows_crossfade_ownership_and_fit_mix(self):
        from app.timeline.crossfade import compile_crossfade
        tracks = [PlaylistTrack("same.wav", str(i), duration_seconds=5) for i in range(2)]
        source = Source(SourceType.SHAPE, "Mix", animation_in="custom", animation_out="custom", animation_fit_mix=True,
                        custom_intro_keyframes=CustomMotion.default(), custom_outro_keyframes=CustomMotion.default(False))
        binding = KeyframeRenderBinding.build(source, compile_crossfade(tracks, 2))
        self.assertEqual(binding.windows[0][1], binding.windows[1][0])
        self.assertAlmostEqual(binding.pose(binding.windows[1][0]).opacity, 0)
        self.assertAlmostEqual(binding.pose(binding.windows[1][0] + binding.windows[1][2] / 2).opacity, .5)

    def test_automix_replanned_manual_junctions_use_the_same_canvas_clock(self):
        from app.automix.overrides import TransitionOverride
        from app.automix.planner import compile_automix
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot
        from tests.test_automix_manual_transitions import _tracks, _analyses
        from tests.test_automix_planner import ENABLED
        tracks = _tracks()
        analyses = _analyses(tracks, (120, 124, 120))
        plans = [compile_automix(tracks, analyses, ENABLED),
                 compile_automix(tracks, analyses, ENABLED.with_overrides({
                     "a>b": TransitionOverride(150, duration=4, tempo_match=False)}))]
        source = Source(SourceType.SHAPE, "AutoMix", x=10, y=10, width=24, height=24, fill_color="#FFFFFF",
            animation_in="custom", animation_out="custom", animation_fit_mix=True,
            custom_intro_keyframes=CustomMotion.default(), custom_outro_keyframes=CustomMotion.default(False))
        scene = CanvasScene()
        scene.set_artboard_size(64, 64)
        scene.artboard_color = scene.artboard_border_color = QColor("black")
        scene.addItem(SourceItem(source))
        bindings = [KeyframeRenderBinding.build(source, plan) for plan in plans]
        self.assertNotEqual(bindings[0].windows, bindings[1].windows)
        for plan, binding in zip(plans, bindings):
            for start, end, intro, outro in binding.windows:
                for seconds in (start + intro * .25, end - outro * .25):
                    image = CanvasSnapshot.capture_track(scene, tracks[0], 1, len(tracks), 0,
                        elapsed_seconds=seconds, timeline_seconds=seconds, compiled_plan=plan)
                    self.assertAlmostEqual(image.pixelColor(20, 20).red(), binding.values(seconds)["opacity"] * 255, delta=3)

    def test_short_track_clock_matches_canvas_with_other_sources_and_source_window(self):
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot
        target = Source(SourceType.SHAPE, "Short", x=20, y=20, width=32, height=32, fill_color="#FFFFFF",
            animation_in="custom", animation_out="custom", animation_in_duration=.1, animation_out_duration=2,
            custom_intro_keyframes=CustomMotion.default(), custom_outro_keyframes=CustomMotion.default(False))
        other = Source(SourceType.TEXT, "Other", x=1000, y=1000, animation_in="fade", animation_in_duration=2)
        track = PlaylistTrack("", "Short", duration_seconds=.6)
        plan = compile_playlist([track])
        binding = KeyframeRenderBinding.build(target, plan, sources=[target, other])
        self.assertAlmostEqual(binding.windows[0][3], .15)
        scene = CanvasScene()
        scene.set_artboard_size(96, 96)
        scene.artboard_color = scene.artboard_border_color = QColor("black")
        scene.addItem(SourceItem(target))
        scene.addItem(SourceItem(other))
        for seconds in (.025, .05, .1, .4, .45, .5, .55):
            image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=seconds,
                timeline_seconds=seconds, compiled_plan=plan)
            self.assertAlmostEqual(image.pixelColor(35, 35).red(), binding.values(seconds)["opacity"] * 255, delta=3)
        target.timeline_start, target.timeline_duration = .2, .3
        binding = KeyframeRenderBinding.build(target, plan, sources=[target, other])
        self.assertEqual(binding.windows[0][:2], (.2, .5))

    def test_video_custom_motion_runs_through_actual_layered_filter(self):
        from app.renderer.ffmpeg_renderer import VideoClipOverlay
        from app.renderer.ffmpeg.filter_graph import layered_filter_graph
        source = Source(SourceType.VIDEO, "Video", x=30, y=30, width=32, height=32, opacity=.5,
            animation_in="custom", animation_in_duration=1,
            custom_intro_keyframes=CustomMotion((MotionPoint(0, dx=-10, opacity=0), MotionPoint(1, dx=30))))
        track = PlaylistTrack("", "Track", duration_seconds=2)
        binding = KeyframeRenderBinding.build(source, compile_playlist([track]))
        clip = VideoClipOverlay(Path("unused.mp4"), 0, 1, 0, 30, 30, 32, 32, 0, opacity=.5,
            animation_in="custom", keyframe_binding=binding)
        graph = layered_filter_graph([], [], 10, 128, 96, [clip]).replace("[vout]", "[out]")
        frames = self.ffmpeg(["color=black:s=128x96:r=10:d=1", "color=black:s=2x2:r=10:d=1",
                             "color=white:s=32x32:r=10:d=1,format=rgba"], graph, width=128, height=96, encode=True)
        for i in (2, 5, 9):
            pose = binding.values(i / 10)
            self.assertAlmostEqual(float(frames[i].max()), pose["opacity"] * 255, delta=3)
            ys, xs = np.nonzero(frames[i, :, :, 0] > 0)
            self.assertAlmostEqual(xs.mean(), pose["x"] + 15.5, delta=1)

    def test_text_image_album_cover_editor_packaged_project_and_final_mp4_match(self):
        import subprocess
        import wave
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.dialogs.custom_motion_dialog import CustomMotionDialog
        from app.models.project import ProjectDocument
        from app.preview.export_canvas_capture import ExportCanvasCapturer
        from app.renderer.export_timeline import ExportFrameSample
        from app.renderer.ffmpeg_renderer import FFmpegRenderer, RenderFrame, RenderSettings
        from app.services.project_service import ProjectService
        from app.utils.i18n import Translator
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            art = directory / "cover.png"
            image = QImage(32, 32, QImage.Format.Format_RGB32)
            image.fill(QColor("#CCCCCC"))
            for x in range(16):
                for y in range(16):
                    image.setPixelColor(x, y, QColor("#444444"))
            self.assertTrue(image.save(str(art)))
            audio = directory / "silence.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                wav.writeframes(bytes(96000))
            track = PlaylistTrack(str(audio), "Album", artist="Artist", cover_path=str(art), duration_seconds=1)
            motion = CustomMotion((MotionPoint(0, dx=-20, opacity=0), MotionPoint(1, dx=20)))
            motion = motion.with_key("dx", .5, value=35).with_key("opacity", .25, value=.7)
            motion = motion.set_field_enabled("rotation", False)
            sources = [Source(SourceType.TEXT, "Text", x=30, y=15, width=230, height=50,
                              text="Relative motion", font_size=22, fill_color="#00000000", outline_color="#FFFFFF"),
                       Source(SourceType.IMAGE, "Image", x=40, y=90, width=48, height=48,
                              content_path=str(art), fill_color="#00000000"),
                       Source(SourceType.ALBUM_COVER, "Cover", x=180, y=90, width=48, height=48, fill_color="#00000000")]
            for source in sources:
                source.animation_in = "custom"
                source.animation_in_duration = .4
                source.custom_intro_keyframes = motion
            scene = CanvasScene()
            scene.set_artboard_size(320, 180)
            scene.artboard_color = scene.artboard_border_color = QColor("black")
            for source in sources:
                scene.addItem(SourceItem(source))
            plan = compile_playlist([track])
            frames = []
            def stage(image, duration, *_):
                frame = RenderFrame(image.copy(), duration)
                frames.append(frame)
                return frame
            capturer = ExportCanvasCapturer(scene, [track], 1, set(), [(None, None)], stage,
                lambda: None, lambda *_: None, output_fps=10, compiled_plan=plan)
            for i in range(10):
                capturer.capture(ExportFrameSample(track, 1, 0, .1, i / 10, i / 10))
            dialog = CustomMotionDialog(sources[0], scene, [track], Translator())
            dialog.seek(.5)
            editor_frame = dialog.render_now(full_size=True)
            self.assertEqual(editor_frame, frames[2].image)
            dialog.reject()
            dialog.deleteLater()
            project = ProjectService.load(ProjectService.save(directory / "motion.pvsproj",
                ProjectDocument(sources=sources, playlist=[track])))
            self.assertEqual([s.custom_intro_keyframes for s in project.sources], [motion] * 3)
            output = directory / "motion.mp4"
            FFmpegRenderer(render_tests.FFMPEG).render(frames, [track], output,
                RenderSettings(fps=10, output_width=320, output_height=180, crf=0, preset="ultrafast"),
                compiled_plan=plan, prepared_audio_path=audio)
            decoded = subprocess.run([str(render_tests.FFMPEG), "-v", "error", "-i", str(output),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, timeout=30)
            self.assertEqual(decoded.returncode, 0)
            video = np.frombuffer(decoded.stdout, dtype=np.uint8).reshape(-1, 180, 320, 3)
            self.assertEqual(len(video), 10)
            reference = []
            for frame in frames:
                image = frame.image.convertToFormat(QImage.Format.Format_RGB888)
                reference.append(np.frombuffer(image.constBits(), dtype=np.uint8).reshape(180, image.bytesPerLine())[:, :960].copy())
            # Native Windows text can use colored subpixel antialiasing. MP4's
            # required YUV420 chroma subsampling changes those fringes even at
            # CRF 0. Independently convert the exact Qt frames through that color
            # format so the strict comparison still detects wrong motion/time.
            converted = subprocess.run([str(render_tests.FFMPEG), "-v", "error", "-filter_threads", "1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "320x180", "-r", "10", "-i", "pipe:0",
                "-vf", "format=yuv420p,format=rgb24", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                input=np.stack(reference).tobytes(), capture_output=True, timeout=30)
            self.assertEqual(converted.returncode, 0, converted.stderr)
            color_reference = np.frombuffer(converted.stdout, dtype=np.uint8).reshape(video.shape)
            for i, frame in enumerate(frames):
                image = frame.image.convertToFormat(QImage.Format.Format_RGB888)
                expected = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(180, image.bytesPerLine())[:, :960].reshape(180, 320, 3)
                error = np.abs(video[i].astype(float) - expected.astype(float))
                self.assertLess(float(error.mean()), 1)
                codec_error = np.abs(video[i].astype(float) - color_reference[i].astype(float))
                self.assertLess(float(codec_error.mean()), 1)
                self.assertLess(float(np.percentile(codec_error, 99)), 3)

    def test_visualizer_custom_pose_matches_cpu_adapter_and_real_ffmpeg(self):
        import subprocess
        from types import SimpleNamespace
        from app.controllers.export_controller import ExportOrchestrator
        from app.dialogs.export_preview_dialog import ExportPreviewDialog
        from app.renderer.ffmpeg.filter_graph import python_visualizer_filter_graph
        from app.renderer.python_visualizer import PythonVisualizerRenderer
        from app.services.source_store import SourceStore
        source = Source(SourceType.AUDIO_VISUALIZER, "Bars", x=30, y=30, width=32, height=32, opacity=.5,
            animation_in="custom", animation_in_duration=.8, fill_color="#FFFFFF", visualizer_bars=4,
            custom_intro_keyframes=CustomMotion((MotionPoint(0, opacity=0), MotionPoint(1, dx=20, scale=1.4))))
        store = SourceStore()
        store.add(source)
        track = PlaylistTrack("", "A", duration_seconds=2)
        plan = compile_playlist([track])
        overlay = ExportOrchestrator.visualizers(SimpleNamespace(window=SimpleNamespace(store=store)), [track], compiled_plan=plan)[0]
        self.assertEqual(overlay.opacity, 1)
        self.assertEqual(overlay.animation_in, "none")
        raw = PythonVisualizerRenderer._draw_frame(32, 32, overlay, np.ones(4))
        graph = python_visualizer_filter_graph([overlay], 10, 128, 96).replace("[vout]", "[out]")
        with TemporaryDirectory() as raw_dir:
            path = Path(raw_dir) / "bars.png"
            self.assertTrue(raw.save(str(path)))
            command = [str(render_tests.FFMPEG), "-v", "error", "-filter_complex_threads", "1", "-f", "lavfi", "-i",
                "color=black:s=128x96:r=10:d=1", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1",
                "-loop", "1", "-framerate", "10", "-i", str(path), "-filter_complex", graph,
                "-map", "[out]", "-t", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
            result = subprocess.run(command, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 96, 128, 3)
        for i in (2, 4, 7):
            _, cpu = ExportPreviewDialog._apply_overlay_keyframes(SimpleNamespace(_compiled_plan=plan), [overlay], (raw,), i / 10)
            image = cpu[0].convertToFormat(QImage.Format.Format_RGBA8888)
            alpha = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(image.height(), image.width(), 4)[:, :, 3].max()
            self.assertAlmostEqual(float(frames[i].max()), float(alpha), delta=3)


@skipUnless(os.environ.get("QT_QPA_PLATFORM") == "windows", "Requires native Windows OpenGL")
class MotionGpuTests(TestCase):
    def test_custom_visualizer_gpu_layers_match_cpu_composition(self):
        from types import SimpleNamespace
        from PySide6.QtCore import QRectF, QSize, Qt
        from PySide6.QtGui import QPainter
        from PySide6.QtTest import QTest
        from app.controllers.export_controller import ExportOrchestrator
        from app.dialogs.export_preview_dialog import ExportPreviewDialog
        from app.preview.gpu_texture_surface import GpuPreviewLayer, GpuTexturePreviewSurface
        from app.renderer.python_visualizer import PythonVisualizerRenderer
        from app.services.source_store import SourceStore
        app = QApplication.instance() or QApplication([])
        source = Source(SourceType.AUDIO_VISUALIZER, "GPU", x=30, y=20, width=40, height=32, opacity=.5,
            animation_in="custom", animation_in_duration=1, fill_color="#FFFFFF", visualizer_bars=4,
            custom_intro_keyframes=CustomMotion((MotionPoint(0, dx=-10, rotation=-15, opacity=0),
                MotionPoint(1, dx=25, scale=1.5, rotation=35))))
        store = SourceStore()
        store.add(source)
        tracks = [PlaylistTrack("", "A", duration_seconds=3)]
        plan = compile_playlist(tracks)
        overlay = ExportOrchestrator.visualizers(SimpleNamespace(window=SimpleNamespace(store=store)), tracks, compiled_plan=plan)[0]
        raw = PythonVisualizerRenderer._draw_frame(40, 32, overlay, np.ones(4))
        context = SimpleNamespace(_compiled_plan=plan, _active_render_scale=1)
        surface = GpuTexturePreviewSurface()
        errors = []
        surface.backend_failed.connect(errors.append)
        self.addCleanup(surface.deleteLater)
        self.addCleanup(surface.close)
        surface.resize(160, 100)
        surface.show()
        QTest.qWait(200)
        self.assertTrue(surface.ready)
        print("CUSTOM_MOTION_GPU", surface.backend_info, flush=True)
        for seconds in (0, .25, .5, .9, 1.5):
            overlays, images = ExportPreviewDialog._apply_overlay_keyframes(context, [overlay], (raw,), seconds)
            background = QImage(160, 100, QImage.Format.Format_RGB32)
            background.fill(QColor("black"))
            cpu = background.copy()
            painter = QPainter(cpu)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            ExportPreviewDialog._paint_overlay_layer(context, painter, overlays[0], images[0])
            painter.end()
            layers = [GpuPreviewLayer("base", background, QRectF(0, 0, 160, 100)),
                      *ExportPreviewDialog._gpu_audio_layers(context, overlays, images)]
            surface.set_layers(QSize(160, 100), layers)
            QTest.qWait(70)
            gpu = surface.grabFramebuffer()
            self.assertTrue(surface.ready, str(errors))
            if gpu.size() != cpu.size():
                gpu = gpu.scaled(cpu.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            def rgb(image):
                image = image.convertToFormat(QImage.Format.Format_RGB888)
                return np.frombuffer(image.constBits(), dtype=np.uint8).reshape(100, image.bytesPerLine())[:, :480].reshape(100, 160, 3).copy()
            error = np.abs(rgb(gpu).astype(float) - rgb(cpu).astype(float))
            if os.environ.get("MMS_CUSTOM_DEBUG"):
                print("CUSTOM_GPU_ERROR", seconds, float(error.mean()), float(np.percentile(error, 99)),
                      (overlays[0].x, overlays[0].y, overlays[0].width, overlays[0].height, overlays[0].rotation), flush=True)
                cpu.save(str(Path(os.environ["MMS_CUSTOM_DEBUG"]) / f"gpu-cpu-{seconds}.png"))
                gpu.save(str(Path(os.environ["MMS_CUSTOM_DEBUG"]) / f"gpu-frame-{seconds}.png"))
            self.assertLess(float(error.mean()), 1)
            self.assertLess(float(np.percentile(error, 99)), 12)
