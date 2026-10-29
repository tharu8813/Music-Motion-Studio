"""Common customization survives persistence and every rendering route."""

from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.canvas.source_item import SourceItem
from app.controllers.export_controller import ExportOrchestrator
from app.inspector.editors.base import MotionSection
from app.inspector.source_inspector import SourceInspector
from app.models.project import ProjectDocument
from app.models.source import SHAPE_KINDS, Source, SourceType
from app.renderer.ffmpeg.filter_graph import layered_filter_graph
from app.renderer.ffmpeg_renderer import VideoClipOverlay, VisualizerOverlay
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.services.source_store import SourceStore
from app.utils.i18n import Translator
from app.utils.source_decoration import frame_geometry
from app.utils.subprocess_utils import hidden_process_kwargs


class SharedDecorationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_every_source_round_trips_new_properties_and_legacy_defaults(self):
        sources = [Source(kind, kind.value, flip_horizontal=True, flip_vertical=True,
                          frame_width=4, frame_color="#80FF0080", frame_inset=7)
                   for kind in SourceType]
        document = ProjectDocument(sources=sources)
        self.assertEqual(ProjectDocument.from_dict(document.to_dict()).to_dict(), document.to_dict())
        for source in sources:
            legacy = source.to_dict()
            for key in MotionSection.APPEARANCE_KEYS:
                legacy.pop(key)
            restored = Source.from_dict(legacy)
            self.assertFalse(restored.flip_horizontal)
            self.assertFalse(restored.flip_vertical)
            self.assertEqual(restored.frame_width, 0)

    def test_invalid_saved_properties_are_rejected(self):
        source = Source(SourceType.SHAPE, "Shape")
        for key, value in (("frame_width", -1), ("frame_inset", 201),
                           ("frame_width", float("nan")), ("frame_color", "red"),
                           ("flip_horizontal", "false"), ("flip_vertical", 1)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Source.from_dict(source.to_dict() | {key: value})

    def test_common_inspector_fields_edit_all_types_and_mixed_selection(self):
        store = SourceStore()
        sources = [Source(kind, kind.value) for kind in SourceType]
        for source in sources:
            store.add(source)
        inspector = SourceInspector(store, Translator())
        try:
            for source in sources:
                inspector.set_source(source)
                for key in ("flip_horizontal", "flip_vertical", "frame_width"):
                    self.assertTrue(inspector._field_visibility[key], (source.source_type, key))
                inspector.motion.widgets["frame_width"].setValue(5)
                self.assertEqual(source.frame_width, 5)
                for key in ("frame_color", "frame_inset"):
                    self.assertTrue(inspector._field_visibility[key], (source.source_type, key))
            inspector.set_sources([source.id for source in sources], sources[-1])
            inspector.motion.widgets["flip_horizontal"].click()
            self.assertTrue(all(source.flip_horizontal for source in sources))
            inspector._apply_decoration_preset("pastel")
            self.assertTrue(all(source.frame_width == 6 and source.frame_color == "#FDA4AF"
                                and source.frame_inset == 8 for source in sources))
            inspector._apply_decoration_preset("none")
            self.assertTrue(all(source.frame_width == 0 for source in sources))
            self.assertTrue(all(source.flip_horizontal for source in sources))
        finally:
            inspector.close()
            inspector.deleteLater()

    @staticmethod
    def render(source):
        item = SourceItem(source)
        image = QImage(100, 80, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        try:
            item.paint(painter, None)
        finally:
            painter.end()
            item.release_video_decoder()
            item.deleteLater()
        return image

    def test_mirroring_reverses_content_pixels_and_preserves_source_placement(self):
        source = Source(SourceType.SHAPE, "Triangle", shape_kind="triangle",
                        width=100, height=80, x=25, y=40, fill_color="#FF0000")
        plain = self.render(source)
        flipped = self.render(replace(source, flip_vertical=True))
        self.assert_images_close(plain.flipped(Qt.Orientation.Vertical), flipped)
        self.assertEqual((source.x, source.y), (25, 40))

    def test_frame_renders_on_all_sources_inside_the_source_box(self):
        for kind in SourceType:
            with self.subTest(kind=kind):
                source = Source(kind, kind.value, width=100, height=80,
                                fill_color="#000000", frame_width=4, frame_inset=8,
                                frame_color="#FF00FF")
                image = self.render(source)
                self.assertEqual(image.pixelColor(50, 9).name(), "#ff00ff")
                self.assertEqual(image.pixelColor(9, 40).name(), "#ff00ff")

    def test_small_sources_clamp_frame_without_inverted_geometry(self):
        rect = QRectF(0, 0, 8, 6)
        box, width = frame_geometry(rect, 80, 200)
        self.assertGreaterEqual(box.width(), 0)
        self.assertGreaterEqual(box.height(), 0)
        self.assertEqual(width, 0)

    def test_reactive_export_frame_and_mirroring_match_canvas(self):
        overlay = VisualizerOverlay(0, 0, 100, 80, "bars", "#FFFFFF",
                                    frame_width=4, frame_color="#FF00FF", frame_inset=8)
        levels = np.array([0.1, 0.8, 0.3])
        image = PythonVisualizerRenderer._draw_frame(100, 80, overlay, levels)
        flipped = PythonVisualizerRenderer._draw_frame(
            100, 80, replace(overlay, flip_horizontal=True), levels)
        self.assert_images_close(image.flipped(Qt.Orientation.Horizontal), flipped)
        self.assertEqual(image.pixelColor(50, 9).name(), "#ff00ff")

    def assert_images_close(self, first, second):
        # Qt rasterizes mirrored curved paths with slightly different edge coverage.
        first = first.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        second = second.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        a = np.frombuffer(first.constBits(), dtype=np.uint8).astype(int)
        b = np.frombuffer(second.constBits(), dtype=np.uint8).astype(int)
        self.assertLessEqual(np.max(np.abs(a - b)), 8)
        self.assertLess(np.mean(np.abs(a - b)), 0.2)

    def test_video_export_flips_and_frames_before_opacity_and_rotation(self):
        clip = VideoClipOverlay(Path("video.mp4"), 0, 1, 0, 0, 0, 100, 80, 0,
                                flip_horizontal=True, flip_vertical=True,
                                frame_width=4, frame_inset=8, frame_color="#80FF00FF",
                                opacity=0.5, rotation=20)
        graph = layered_filter_graph([], [], 30, 100, 80, [clip])
        self.assertIn("hflip,vflip,drawbox=x=8.0000:y=8.0000", graph)
        self.assertIn("color=0xFF00FF80:t=4.0000", graph)
        self.assertLess(graph.index("drawbox="), graph.index("colorchannelmixer="))
        self.assertLess(graph.index("drawbox="), graph.index("rotate="))

    def test_reactive_export_translation_scales_decoration_with_the_source(self):
        store = SourceStore()
        source = Source(SourceType.AUDIO_VISUALIZER, "Bars", scale=1.5,
                        flip_horizontal=True, frame_width=4, frame_inset=8,
                        frame_color="#80FF00FF", loop_motion="orbit")
        store.add(source)
        export = ExportOrchestrator(SimpleNamespace(store=store))
        overlay, = export.visualizers(tracks=[], render_scale=2)
        self.assertTrue(overlay.flip_horizontal)
        self.assertEqual(overlay.frame_width, 12)
        self.assertEqual(overlay.frame_inset, 24)
        self.assertEqual(overlay.frame_color, source.frame_color)
        self.assertEqual(overlay.loop_motion, "orbit")

    def test_actual_ffmpeg_video_pixels_include_mirroring_and_frame(self):
        executable = os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not executable:
            self.skipTest("FFmpeg is unavailable")
        clip = VideoClipOverlay(Path("video.mp4"), 0, 1, 0, 0, 0, 100, 80, 0,
                                flip_horizontal=True, flip_vertical=True,
                                frame_width=4, frame_inset=8, frame_color="#FF00FF",
                                border_radius=40)
        graph = layered_filter_graph([], [], 10, 100, 80, [clip])
        result = subprocess.run([
            executable, "-hide_banner", "-loglevel", "error", "-filter_complex_threads", "1",
            "-f", "lavfi", "-i", "color=black:size=100x80:rate=10",
            "-f", "lavfi", "-i", "anullsrc",
            "-f", "lavfi", "-i", "color=red:size=100x80:rate=10,drawbox=x=50:y=0:w=50:h=80:color=blue:t=fill",
            "-filter_complex", graph, "-map", "[vout]", "-frames:v", "1",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
        ], capture_output=True, timeout=15, **hidden_process_kwargs())
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        image = np.frombuffer(result.stdout, dtype=np.uint8).reshape(80, 100, 3)
        self.assertGreater(int(image[40, 20, 2]), 200)
        self.assertGreater(int(image[40, 80, 0]), 200)
        self.assertGreater(int(image[9, 50, 0]), 200)
        self.assertGreater(int(image[9, 50, 2]), 200)
        self.assertLess(int(image[9, 50, 1]), 30)
        # The frame remains rectangular even when the video itself is rounded.
        self.assertGreater(int(image[9, 9, 0]), 200)
        self.assertGreater(int(image[9, 9, 2]), 200)

    def test_new_shapes_have_distinct_silhouettes_and_round_trip(self):
        images = []
        for shape in SHAPE_KINDS:
            source = Source(SourceType.SHAPE, shape, shape_kind=shape, width=100, height=80)
            self.assertEqual(Source.from_dict(source.to_dict()).shape_kind, shape)
            images.append(self.render(source))
        for index, image in enumerate(images):
            for other in images[index + 1:]:
                self.assertNotEqual(image, other)


if __name__ == "__main__":
    unittest.main()
