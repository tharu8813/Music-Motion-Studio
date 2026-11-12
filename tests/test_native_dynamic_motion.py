"""Pixel and clock contracts; no native encoder or audio subprocess required."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication
from app.animation.keyframes import Keyframe
from app.models.source import Source, SourceType
from app.renderer.native_dynamic_motion import dynamic_layer_frame
from app.renderer.native_project_overlays import project_visualizers
from app.renderer.native_project_support import resolve_project_frame
from app.renderer.native_project_support import validate_project_for_gpu_capture
from app.renderer.native_project_transitions import transition_preflight_reason
from app.timeline.render_plan import AudioRenderClip, AudioRenderPlan, AudioRenderTransition, CompiledRenderPlan, build_presentation_and_metadata
from app.timeline.models import TransitionType
from tests.test_native_canvas_project_probe import project_fixture


class DynamicMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def image(self):
        image = QImage(8, 16, QImage.Format.Format_RGBA8888)
        image.fill(QColor(255, 0, 0, 128))
        return image

    def alpha(self, packet, x, y):
        return packet.pixels[(y * packet.width + x) * 4 + 3]

    def test_particle_geometry_is_rounded_once_at_output_resolution(self):
        project = project_fixture()
        source = Source(SourceType.PARTICLE_OVERLAY, "Fractional placement",
                        x=15.4, y=22.6, width=40.4, height=50.4, z_index=9)
        project.sources.append(source)
        for scale, expected in ((0.5, (8, 11, 20, 25)), (1.5, (23, 34, 61, 76))):
            with self.subTest(scale=scale):
                overlay, = project_visualizers(project, project.playlist, scale)
                self.assertEqual((overlay.x, overlay.y, overlay.width, overlay.height), expected)

    def test_rotation_expands_outside_original_box_and_keeps_alpha(self):
        packet = dynamic_layer_frame(self.image(), SimpleNamespace(x=20, y=20, rotation=90),
                                     64, 64, seconds=0)
        self.assertEqual(self.alpha(packet, 17, 28), 128)
        self.assertEqual(self.alpha(packet, 24, 21), 0)

    def test_production_binding_seeks_global_motion_and_applies_opacity_once(self):
        project = project_fixture()
        source = Source(SourceType.AUDIO_VISUALIZER, "moving", x=20, y=20,
                        width=8, height=16, opacity=.5, z_index=9)
        source.keyframes.global_curves["x"] = [Keyframe(0, 20), Keyframe(1, 40)]
        project.sources.append(source)
        overlay = project_visualizers(project, project.playlist)[0]
        self.assertEqual(overlay.opacity, 1)
        before = project.to_dict()
        for seconds, x in ((1, 41), (0, 21), (.5, 31)):
            packet = dynamic_layer_frame(self.image(), overlay, 64, 64, seconds=seconds)
            self.assertAlmostEqual(self.alpha(packet, x, 28), 64, delta=1)
        self.assertEqual(before, project.to_dict())

    def test_custom_motion_uses_existing_export_binding_at_requested_time(self):
        project = project_fixture()
        source = Source(SourceType.AUDIO_VISUALIZER, "custom", x=20, y=20,
                        width=8, height=16, animation_in="custom", z_index=9)
        project.sources.append(source)
        overlay = project_visualizers(project, project.playlist)[0]
        binding = overlay.keyframe_binding
        for seconds in (0, .1, .49, .5, .6):
            with patch.object(type(binding), "values", autospec=True,
                              side_effect=type(binding).values) as values:
                packet = dynamic_layer_frame(self.image(), overlay, 64, 64, seconds=seconds)
                values.assert_called_once_with(binding, seconds)
            self.assertEqual(len(packet.pixels), 64 * 64 * 4)

    def test_overlap_owner_switches_at_existing_visual_midpoint(self):
        project = project_fixture()
        for track in project.playlist:
            track.duration_seconds = 10
        a = AudioRenderClip("a", project.playlist[0].id, 0, 0, 10)
        b = AudioRenderClip("b", project.playlist[1].id, 8, 0, 10)
        presentation, metadata, duration = build_presentation_and_metadata((a, b))
        transition = AudioRenderTransition("a", "b", 8, 2, TransitionType.CROSSFADE)
        plan = CompiledRenderPlan(AudioRenderPlan((a, b), (transition,)), presentation, metadata, duration)
        for seconds, index, local in ((8, 0, 8), (8.999, 0, 8.999), (9, 1, 1), (9.5, 1, 1.5)):
            state = resolve_project_frame(project, seconds, plan=plan)
            self.assertIs(state["track"], project.playlist[index])
            self.assertAlmostEqual(state["track_elapsed_seconds"], local)
        self.assertIn("existing FFmpeg", transition_preflight_reason("automix"))

    def test_missing_frame_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            dynamic_layer_frame(QImage(), SimpleNamespace(), 64, 64, seconds=0)

    def test_production_preflight_accepts_supplied_motion_and_rejects_video(self):
        project = project_fixture()
        project.sources.append(Source(SourceType.AUDIO_VISUALIZER, "rotating", rotation=90, z_index=9))
        self.assertEqual(validate_project_for_gpu_capture(
            project, scale=.25, source_policy="dynamic-audio"), [])
        video = Source(SourceType.VIDEO, "movie", z_index=10)
        project.sources.append(video)
        with self.assertRaisesRegex(ValueError, "native_video_frame_provider_unavailable") as failure:
            validate_project_for_gpu_capture(project, scale=.25, source_policy="dynamic-audio")
        self.assertIn(video.id, str(failure.exception))

    def test_output_scale_maps_binding_coordinates_to_packet(self):
        project = project_fixture()
        source = Source(SourceType.AUDIO_VISUALIZER, "scaled", x=80, y=80,
                        width=32, height=64, z_index=9)
        source.keyframes.global_curves["x"] = [Keyframe(0, 80), Keyframe(1, 160)]
        project.sources.append(source)
        overlay = project_visualizers(project, project.playlist)[0]
        packet = dynamic_layer_frame(self.image(), overlay, 64, 64,
                                     seconds=1, output_scale=.25)
        self.assertEqual(self.alpha(packet, 41, 28), 128)
        self.assertEqual(self.alpha(packet, 21, 28), 0)
