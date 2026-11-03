"""Post-implementation audit: transactional edits and real render boundaries."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, skipUnless

import numpy as np
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from app.animation.keyframes import Keyframe, evaluate_curve
from app.animation.keyframe_render import KeyframeRenderBinding, curve_expression
from app.controllers.keyframe_controller import KeyframeController
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.services.source_store import SourceStore
from app.timeline.compiler import compile_playlist
from app.utils.i18n import Translator
from tests import test_keyframe_rendering as render_tests
FFMPEG = render_tests.FFMPEG


class AuditEditingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.source = Source(SourceType.SHAPE, "Audit", x=10)
        self.store = SourceStore()
        self.store.add(self.source)
        self.track = PlaylistTrack("same.wav", "A", duration_seconds=10)
        self.entries = [(self.track, 0, 10)]
        self.controller = KeyframeController(self.store, lambda: self.entries)

    def test_new_points_cannot_exceed_video_or_track_end(self):
        for mode in ("global", "single_track", "per_track"):
            with self.subTest(mode=mode):
                self.controller.configure(mode=mode, target=self.track.id)
                self.controller.choose_track(self.track.id)
                saved = deepcopy(self.source.to_dict())
                rejected = QSignalSpy(self.controller.edit_rejected)
                self.controller.replace_points((), [("x", Keyframe(11, 20))])
                self.assertEqual(self.source.to_dict(), saved)
                self.assertEqual(rejected.count(), 1)

    def test_invalid_multi_edit_is_atomic_and_never_deletes_originals(self):
        self.controller.replace_points((), [("x", Keyframe(1, 10)), ("y", Keyframe(2, 20))])
        for bad in (Keyframe(-1, 30), Keyframe(11, 30), Keyframe(float("nan"), 30),
                    Keyframe(3, float("nan")), Keyframe(3, 100001)):
            with self.subTest(bad=bad):
                saved = deepcopy(self.source.to_dict())
                self.controller.replace_points({("x", 1), ("y", 2)},
                    [("x", Keyframe(3, 50)), ("y", bad)])
                self.assertEqual(self.source.to_dict(), saved)

    def test_unsupported_clipboard_is_rejected_without_hidden_data(self):
        self.controller._clipboard = [("font_size", Keyframe(0, 60)), ("x", Keyframe(1, 20))]
        saved = deepcopy(self.source.to_dict())
        self.controller.paste(whole_track=True)
        self.assertEqual(self.source.to_dict(), saved)

    def test_paste_over_end_and_collisions_preserve_entire_bank(self):
        self.controller.replace_points((), [("x", Keyframe(0, 10)), ("y", Keyframe(5, 20))])
        self.controller.copy()
        saved = deepcopy(self.source.to_dict())
        for t in (0, 8):
            self.controller.seek_global(t)
            self.controller.paste()
            self.assertEqual(self.source.to_dict(), saved)

    def test_trim_preserves_old_points_and_allows_value_edit_at_same_time(self):
        self.controller.replace_points((), [("x", Keyframe(9, 40))])
        self.track.duration_seconds = 5
        self.entries[:] = [(self.track, 0, 5)]
        self.controller.replace_points({("x", 9)}, [("x", Keyframe(9, 50))])
        self.assertEqual(self.controller.curves()["x"], [Keyframe(9, 50)])

    def test_zero_length_or_disabled_track_is_not_editable(self):
        self.controller.configure(mode="per_track")
        self.controller.choose_track(self.track.id)
        self.track.duration_seconds = 0
        self.entries[:] = [(self.track, 0, 0)]
        self.assertFalse(self.controller.editable)
        self.track.duration_seconds = 10
        self.track.enabled = False
        self.entries[:] = [(self.track, 0, 10)]
        self.assertFalse(self.controller.editable)

    def test_editor_canvas_paints_keyed_opacity_without_capture_adapter(self):
        from app.animation.canvas_keyframes import CanvasKeyframeBinding
        from app.canvas.live_canvas import LiveCanvas
        from app.preview.canvas_snapshot import CanvasSnapshot
        self.source.x, self.source.y = 20, 20
        self.source.width, self.source.height = 32, 32
        self.source.fill_color = "#FFFFFF"
        self.source.opacity = .5
        self.source.keyframes.global_curves = {"opacity": [Keyframe(0, .8)]}
        canvas = LiveCanvas(self.store, Translator())
        canvas.replace_sources()
        self.addCleanup(canvas.deleteLater)
        canvas.scene_model.set_artboard_size(96, 96)
        canvas.scene_model.artboard_color = canvas.scene_model.artboard_border_color = QColor("black")
        binding = CanvasKeyframeBinding(canvas, self.controller)
        image = CanvasSnapshot.capture(canvas.scene_model)
        self.assertAlmostEqual(image.pixelColor(35, 35).red(), 204, delta=2)
        self.assertEqual(self.source.opacity, .5)

    def test_multi_selection_shadow_edit_respects_locked_sources(self):
        from app.inspector.source_inspector import SourceInspector
        other = Source(SourceType.SHAPE, "Locked", locked=True)
        self.store.add(other)
        inspector = SourceInspector(self.store, Translator())
        self.addCleanup(inspector.deleteLater)
        inspector.set_sources([self.source.id, other.id], self.source)
        before = other.shadow.offset_x
        inspector._update_nested("shadow", "offset_x", 22)
        self.assertEqual(self.source.shadow.offset_x, 22)
        self.assertEqual(other.shadow.offset_x, before)

    def test_compiled_trim_and_speed_bound_new_edits_in_source_time(self):
        from app.timeline.render_plan import PresentationPlan, PresentationWindow
        self.track.duration_seconds = 20
        plan = compile_playlist([self.track])
        plan = replace(plan, presentation=PresentationPlan((PresentationWindow(self.track.id, 10, 12, 5, 2),)),
                       duration_seconds=12)
        self.controller.set_plan(plan)
        self.controller.configure(mode="per_track")
        self.controller.choose_track(self.track.id)
        self.assertEqual(self.controller.time, 5)
        saved = deepcopy(self.source.to_dict())
        for time in (0, 4, 10, 19):
            self.controller.replace_points((), [("x", Keyframe(time, 22))])
            self.assertEqual(self.source.to_dict(), saved)
        self.controller.replace_points((), [("x", Keyframe(7, 22))])
        self.assertEqual(self.controller.curves()["x"][0].time, 7)

    def test_visualizer_base_opacity_scales_every_style_once(self):
        from app.renderer.ffmpeg_renderer import VisualizerOverlay
        from app.renderer.python_visualizer import PythonVisualizerRenderer
        for kind, style in (("visualizer", "spectrum"), ("visualizer", "led"), ("visualizer", "arc"),
                            ("visualizer", "bars"), ("waveform", "filled"), ("level_meter", "led"),
                            ("particles", "dust")):
            with self.subTest(kind=kind, style=style):
                overlay = VisualizerOverlay(0, 0, 64, 64, style, "#FFFFFF", kind=kind, effect_style=style)
                images = [PythonVisualizerRenderer._draw_frame(64, 64, replace(overlay, opacity=alpha),
                    np.ones(4), channel_values=(1, 1)) for alpha in (1, .5, 0)]
                alphas = []
                for image in images:
                    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
                    alphas.append(np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(64, 64, 4)[:, :, 3].copy())
                self.assertLessEqual(int(alphas[2].max()), 0)
                self.assertLessEqual(float(np.abs(alphas[1].astype(float) - alphas[0] / 2).max()), 2)

    def test_trimmed_track_keys_are_inactive_during_compiled_silent_gap(self):
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.export_canvas_capture import ExportCanvasCapturer
        from app.renderer.export_timeline import ExportTimelinePlanner
        from app.timeline.render_plan import (AudioRenderClip, AudioRenderPlan, CompiledRenderPlan,
                                               build_presentation_and_metadata)
        a, b = PlaylistTrack("", "A", duration_seconds=20), PlaylistTrack("", "B", duration_seconds=2)
        clips = (AudioRenderClip("a", a.id, 0, 5, 9), AudioRenderClip("b", b.id, 6, 0, 2))
        presentation, metadata, duration = build_presentation_and_metadata(clips)
        plan = CompiledRenderPlan(AudioRenderPlan(clips), presentation, metadata, duration)
        source = Source(SourceType.SHAPE, "Gap", x=10, y=20, width=32, height=32, fill_color="#FFFFFF")
        source.keyframes.mode = "per_track"
        source.keyframes.per_track_curves[a.id] = {"x": [Keyframe(0, 60)]}
        binding = KeyframeRenderBinding.build(source, plan)
        self.assertEqual(binding.values(5), {})
        scene = CanvasScene()
        scene.set_artboard_size(128, 80)
        scene.artboard_color = scene.artboard_border_color = QColor("black")
        scene.addItem(SourceItem(source))
        sample = next(s for s in ExportTimelinePlanner.iter_build([a, b], [source], 10, compiled_plan=plan)
                      if 4 <= s.timeline_seconds < 6)
        captured = []
        capturer = ExportCanvasCapturer(scene, [a, b], duration, set(), [(None, None)],
            lambda image, *_: captured.append(image.copy()), lambda: None, lambda *_: None)
        capturer.capture(sample)
        self.assertGreater(captured[0].pixelColor(25, 35).red(), 250)


@skipUnless(FFMPEG, "FFmpeg is unavailable")
class AuditFfmpegTests(TestCase):
    ffmpeg = render_tests.KeyframeFfmpegTests.ffmpeg

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_video_alpha_matrix_matches_actual_canvas_pixels(self):
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.preview.canvas_snapshot import CanvasSnapshot
        from app.renderer.ffmpeg.filter_graph import layered_filter_graph
        from app.renderer.ffmpeg_renderer import VideoClipOverlay
        track = PlaylistTrack("", "A", duration_seconds=1)
        for base, position, opacity, fade, geometry in (
                (1, True, False, False, False), (.5, True, False, False, False),
                (.5, False, True, False, False), (.5, False, False, True, False),
                (.5, True, False, True, False), (.5, True, True, True, False),
                (.5, True, True, False, True)):
            with self.subTest(base=base, position=position, opacity=opacity, fade=fade, geometry=geometry):
                source = Source(SourceType.VIDEO, "Video", x=32, y=32, width=32, height=32,
                    opacity=base, fill_color="#00000000", image_fit_mode="stretch",
                    animation_in="fade" if fade else "none", animation_in_duration=1)
                if position:
                    source.keyframes.global_curves["x"] = [Keyframe(0, 32), Keyframe(1, 40)]
                if opacity:
                    source.keyframes.global_curves["opacity"] = [Keyframe(0, .8)]
                if geometry:
                    source.keyframes.global_curves.update(scale=[Keyframe(0, 1), Keyframe(1, 1.5)],
                                                        rotation=[Keyframe(0, 0), Keyframe(1, 90)])
                clip = VideoClipOverlay(Path("unused.mov"), 0, 1, 0, 32, 32, 32, 32, 0,
                    opacity=base, animation_in=source.animation_in,
                    animation_windows=((0, 1, 1, 0),),
                    keyframe_binding=KeyframeRenderBinding.build(source, compile_playlist([track])))
                graph = layered_filter_graph([], [], 10, 96, 96, [clip]).replace("[vout]", "[out]")
                frames = self.ffmpeg(["color=black:s=96x96:r=10:d=1", "color=black:s=2x2:r=10:d=1",
                    "color=white:s=32x32:r=10:d=1,format=rgba"], graph, width=96, height=96)
                scene = CanvasScene()
                scene.set_artboard_size(96, 96)
                scene.artboard_color = scene.artboard_border_color = QColor("black")
                item = SourceItem(source)
                white = QImage(32, 32, QImage.Format.Format_RGB32)
                white.fill(QColor("white"))
                from PySide6.QtGui import QPixmap
                item._pixmap = item._raw_pixmap = QPixmap.fromImage(white)
                scene.addItem(item)
                image = CanvasSnapshot.capture_track(scene, track, 1, 1, 0,
                    elapsed_seconds=.5, timeline_seconds=.5,
                    animation_phase="in" if fade else None, animation_progress=.5)
                expected = (.8 if opacity else base) * (.5 if fade else 1) * 255
                qt = image.pixelColor(50, 48).red()
                ff = float(frames[5, 48, 50, 0])
                print("AUDIT_ALPHA", base, position, opacity, fade, geometry, "Qt", qt, "FFmpeg", ff, "expected", expected)
                self.assertAlmostEqual(qt, expected, delta=3)
                self.assertAlmostEqual(ff, qt, delta=4)

    def test_thousand_points_do_not_exhaust_ffmpeg_expression_parser(self):
        points = [Keyframe(i / 1000, (i % 7) / 7, "linear") for i in range(1001)]
        expression = curve_expression(points, "T", "0")
        # Like production, pass large graphs through a file on Windows.
        import subprocess
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as raw:
            from app.renderer.ffmpeg_renderer import FFmpegRenderer
            args = FFmpegRenderer(FFMPEG)._filter_graph_arguments(
                f"[0:v]format=gbrp,geq=r='255*({expression})':g=0:b=0[out]", Path(raw))
            result = subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i",
                "color=white:s=2x2:r=10:d=1", *args,
                "-map", "[out]", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace")[-1000:])
        frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 2, 2, 3)
        for i, frame in enumerate(frames):
            self.assertAlmostEqual(float(frame[0, 0, 0]), 255 * evaluate_curve(points, i / 10, 0), delta=2)

    def test_visualizer_alpha_matrix_matches_canvas_cpu_preview_and_ffmpeg(self):
        import subprocess
        from tempfile import TemporaryDirectory
        from app.canvas.live_canvas import CanvasScene
        from app.canvas.source_item import SourceItem
        from app.controllers.export_controller import ExportOrchestrator
        from app.dialogs.export_preview_dialog import ExportPreviewDialog
        from app.preview.canvas_snapshot import CanvasSnapshot
        from app.renderer.python_visualizer import PythonVisualizerRenderer
        from app.animation.keyframe_render import ffmpeg_transform
        track = PlaylistTrack("", "A", duration_seconds=1)
        plan = compile_playlist([track])
        for base, keyed_position, keyed_opacity, fade in ((1, True, False, False), (.5, True, False, False),
                (.5, False, True, False), (.5, False, False, True), (.5, True, False, True), (.5, True, True, True)):
            with self.subTest(base=base, position=keyed_position, opacity=keyed_opacity, fade=fade):
                source = Source(SourceType.AUDIO_VISUALIZER, "Bars", x=32, y=32, width=32, height=32,
                    fill_color="#FFFFFF", opacity=base, visualizer_bars=4,
                    animation_in="fade" if fade else "none", animation_in_duration=1)
                if keyed_position:
                    source.keyframes.global_curves["x"] = [Keyframe(0, 32)]
                if keyed_opacity:
                    source.keyframes.global_curves["opacity"] = [Keyframe(0, .8)]
                store = SourceStore()
                store.add(source)
                overlay = ExportOrchestrator.visualizers(SimpleNamespace(window=SimpleNamespace(store=store)),
                    [track], compiled_plan=plan)[0]
                scene = CanvasScene()
                scene.set_artboard_size(96, 96)
                scene.artboard_color = scene.artboard_border_color = QColor("black")
                scene.addItem(SourceItem(source))
                canvas = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=.5,
                    timeline_seconds=.5, animation_phase="in" if fade else None, animation_progress=.5)
                raw = PythonVisualizerRenderer._draw_frame(32, 32, overlay, np.ones(4))
                raw = PythonVisualizerRenderer.transform_layer(raw, overlay, .5, ((0, 1),))
                _, cpu_images = ExportPreviewDialog._apply_overlay_keyframes(SimpleNamespace(_compiled_plan=plan),
                    [overlay], (raw,), .5)
                graph = []
                layer, x, y = "[1:v]", "32", "32"
                if overlay.keyframe_binding:
                    layer, x, y = ffmpeg_transform(graph, layer, overlay.keyframe_binding, 32, 32, "bars")
                graph.append(f"[0:v]{layer}overlay=x='{x}':y='{y}':format=rgb[out]")
                with TemporaryDirectory() as directory:
                    path = Path(directory) / "bars.png"
                    self.assertTrue(raw.save(str(path)))
                    result = subprocess.run([str(FFMPEG), "-v", "error", "-filter_complex_threads", "1",
                        "-f", "lavfi", "-i", "color=black:s=96x96:r=1", "-loop", "1", "-i", str(path),
                        "-filter_complex", ";".join(graph), "-map", "[out]", "-frames:v", "1",
                        "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                ff = int(np.frombuffer(result.stdout, dtype=np.uint8).max())
                cpu = cpu_images[0].convertToFormat(QImage.Format.Format_RGBA8888)
                cpu_alpha = int(np.frombuffer(cpu.constBits(), dtype=np.uint8).reshape(32, 32, 4)[:, :, 3].max())
                qt = canvas.pixelColor(35, 48).red()
                expected = 255 * (.8 if keyed_opacity else base) * (.5 if fade else 1)
                print("AUDIT_VISUALIZER_ALPHA", base, keyed_position, keyed_opacity, fade,
                      "Canvas", qt, "CPU alpha", cpu_alpha, "FFmpeg", ff, "expected", expected)
                self.assertAlmostEqual(qt, expected, delta=3)
                self.assertAlmostEqual(cpu_alpha, qt, delta=3)
                self.assertAlmostEqual(ff, qt, delta=4)

    def test_hundred_track_expression_can_be_initialized(self):
        import subprocess
        from tempfile import TemporaryDirectory
        from app.renderer.ffmpeg_renderer import FFmpegRenderer
        tracks = [PlaylistTrack("same.wav", str(i), duration_seconds=1) for i in range(100)]
        source = Source(SourceType.SHAPE, "Many tracks")
        source.keyframes.mode = "per_track"
        source.keyframes.per_track_curves = {t.id: {"opacity": [Keyframe(0, (i % 7) / 7)]} for i, t in enumerate(tracks)}
        binding = KeyframeRenderBinding.build(source, compile_playlist(tracks))
        expression = binding.expression("opacity", "T*100")
        with TemporaryDirectory() as raw:
            args = FFmpegRenderer(FFMPEG)._filter_graph_arguments(
                f"[0:v]format=gbrp,geq=r='255*({expression})':g=0:b=0[out]", Path(raw))
            result = subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i",
                "color=white:s=2x2:r=10:d=1", *args, "-map", "[out]", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace")[-1000:])
        frames = np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, 2, 2, 3)
        for i, frame in enumerate(frames):
            self.assertAlmostEqual(float(frame[0, 0, 0]), 255 * binding.values(i * 10)["opacity"], delta=2)

    def test_microsecond_segment_at_large_timestamp_matches_python(self):
        points = [Keyframe(1_000_000, 0, "linear"), Keyframe(1_000_000.000001, 1)]
        seconds = 1_000_000.0000005
        expression = curve_expression(points, f"{seconds:.17g}", "0")
        frames = self.ffmpeg(["color=white:s=2x2:r=1:d=1"],
            f"[0:v]format=gbrp,geq=r='255*({expression})':g=0:b=0[out]", width=2, height=2, fps=1)
        self.assertAlmostEqual(float(frames[0, 0, 0, 0]), 255 * evaluate_curve(points, seconds, 0), delta=2)
