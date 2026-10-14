from __future__ import annotations

import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from dataclasses import asdict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.animation.curves import slide_distance
from app.models.playlist import PlaylistTrack
from app.models.source import Gradient, Shadow, Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.album_art import adjust_personal_color, extract_track_personal_color
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer, PreparedVideoInput, RenderError, RenderFrame, RenderSettings,
    StaticOverlayLayer, VideoClipOverlay, VisualizerOverlay,
    WORK_MODE_AUTO, WORK_MODE_MAX_SPEED, WORK_MODE_STABLE,
)
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.services.playlist_export_service import PlaylistExportError, PlaylistExportService
from app.services.playlist_service import PlaylistService
from app.services.lyrics_service import LyricsService
from app.services.project_service import ProjectService
from app.ffmpeg.install_worker import FFmpegInstallWorker
from app.ffmpeg.managed_installer import (
    FFmpegInstallError,
    FFmpegReleaseOption,
    ManagedFFmpegInstallation,
    ManagedFFmpegInstaller,
)
from app.dialogs.about_dialog import AboutDialog
from app.dialogs.export_preview_dialog import ExportPreviewDialog, TIMELINE_SCALE
from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog
from app.utils.i18n import Translator
from app import __version__


class FunctionalRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_source_timing_controls_snapshot_visibility(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE, "Timed", x=20, y=20, width=120, height=80,
            fill_color="#FF0000", timeline_start=5.0, timeline_duration=2.0,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("track.wav", "Track", duration_seconds=10.0)

        before = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0, timeline_seconds=4.0,
        )
        during = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0, timeline_seconds=6.0,
        )
        after = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0, timeline_seconds=7.0,
        )

        self.assertNotEqual(before.pixelColor(60, 50), during.pixelColor(60, 50))
        self.assertEqual(before.pixelColor(60, 50), after.pixelColor(60, 50))
        self.assertTrue(item.isVisible())

    def test_capture_invariant_classifier_is_deliberately_conservative(self) -> None:
        duration = 20.0
        self.assertTrue(CanvasSnapshot.source_is_capture_invariant(
            Source(SourceType.SHAPE, "Static shape"), duration,
        ))
        self.assertTrue(CanvasSnapshot.source_is_capture_invariant(
            Source(SourceType.TEXT, "Static title", text="Playlist"), duration,
        ))
        # ``track_total`` and unknown tokens resolve to the same string for the
        # whole playlist, so a count caption stays capture-invariant.
        self.assertTrue(CanvasSnapshot.source_is_capture_invariant(
            Source(SourceType.TEXT, "Count", text="%track_total% songs"), duration,
        ))
        self.assertTrue(CanvasSnapshot.source_is_capture_invariant(
            Source(SourceType.TEXT, "Unknown token", text="%mixtape%"), duration,
        ))
        for source in (
            Source(SourceType.TEXT, "Token", text="%title%"),
            Source(SourceType.TEXT, "Count and title", text="%track_total% · %artist%"),
            Source(SourceType.TEXT, "Clock", text="%video_current_time%"),
            Source(SourceType.LYRICS, "Lyrics"),
            Source(SourceType.PROGRESS_BAR, "Progress"),
            Source(SourceType.BACKGROUND, "Cover", background_mode="album_art"),
            Source(SourceType.SHAPE, "Animated", animation_in="fade"),
            Source(SourceType.IMAGE, "Timed", timeline_start=1.0),
        ):
            self.assertFalse(
                CanvasSnapshot.source_is_capture_invariant(source, duration),
                source.name,
            )

        personal = Source(
            SourceType.SHAPE, "Track color", personal_color_enabled=True,
        )
        self.assertFalse(
            CanvasSnapshot.source_is_capture_invariant(personal, duration)
        )

    def test_personal_color_extracts_artwork_and_restores_configured_color(self) -> None:
        with TemporaryDirectory(prefix="playlist-personal-color-") as directory:
            artwork_path = Path(directory) / "cover.png"
            artwork = QImage(32, 32, QImage.Format.Format_ARGB32)
            artwork.fill(QColor("#E03030"))
            self.assertTrue(artwork.save(str(artwork_path)))

            extracted = extract_track_personal_color("missing.wav", artwork_path)
            self.assertTrue(extracted.isValid())
            self.assertGreater(extracted.red(), extracted.green() * 2)
            self.assertGreater(extracted.red(), extracted.blue() * 2)

            scene = CanvasScene()
            source = Source(
                SourceType.SHAPE, "Personal shape", x=10, y=10,
                width=100, height=70, fill_color="#2040D0",
                personal_color_enabled=True,
            )
            scene.addItem(SourceItem(source))
            track = PlaylistTrack(
                "missing.wav", "Track", duration_seconds=5.0,
                cover_path=str(artwork_path),
            )
            captured = CanvasSnapshot.capture_track(scene, track, 1, 1, 0.0)
            rendered = captured.pixelColor(50, 40)

            self.assertGreater(rendered.red(), rendered.blue())
            self.assertEqual(source.fill_color, "#2040D0")

    def test_image_and_background_filters_reach_preview_and_export_capture(self) -> None:
        with TemporaryDirectory(prefix="playlist-image-filter-") as directory:
            art = Path(directory) / "photo.png"
            image = QImage(64, 64, QImage.Format.Format_ARGB32)
            image.fill(QColor(120, 140, 160))
            self.assertTrue(image.save(str(art)))

            track = PlaylistTrack(
                "missing.wav", "Track", duration_seconds=5.0,
                cover_path=str(art),
            )

            def captured_center(
                source_type: SourceType, **source_kwargs: object,
            ) -> tuple[int, int, int, int]:
                scene = CanvasScene()
                source = Source(
                    source_type, "Filtered", x=10, y=10, width=100, height=70,
                    **source_kwargs,  # type: ignore[arg-type]
                )
                scene.addItem(SourceItem(source))
                frame = CanvasSnapshot.capture_track(scene, track, 1, 1, 0.0)
                return frame.pixelColor(50, 40).getRgb()

            # Background element in image mode.
            plain_bg = captured_center(
                SourceType.BACKGROUND, background_mode="image", content_path=str(art),
            )
            dark_bg = captured_center(
                SourceType.BACKGROUND, background_mode="image", content_path=str(art),
                brightness=-45.0,
            )
            self.assertLess(sum(dark_bg[:3]), sum(plain_bg[:3]))

            # Track-driven album cover (no explicit content_path).
            plain_cover = captured_center(
                SourceType.ALBUM_COVER, image_fit_mode="stretch",
            )
            dark_cover = captured_center(
                SourceType.ALBUM_COVER, image_fit_mode="stretch", brightness=-45.0,
            )
            self.assertLess(sum(dark_cover[:3]), sum(plain_cover[:3]))

    def test_text_glyph_outline_renders_for_text_bearing_sources(self) -> None:
        for source_type in (
            SourceType.TEXT, SourceType.TIME, SourceType.LYRICS,
            SourceType.NOW_PLAYING, SourceType.TRACK_LIST,
        ):
            source = Source(
                source_type, "WWWW", text="WWWW", x=0, y=0, width=360, height=160,
                font_size=44.0, outline_color="#FFFFFF",
                text_stroke_color="#FF0000", text_stroke_width=4.0,
            )
            item = SourceItem(source)

            def red_glyph_pixels() -> int:
                frame = QImage(360, 160, QImage.Format.Format_ARGB32)
                frame.fill(QColor("#101010"))
                painter = QPainter(frame)
                item.paint(painter, None, None)
                painter.end()
                return sum(
                    1
                    for y in range(0, 160, 2)
                    for x in range(0, 360, 2)
                    if frame.pixelColor(x, y).red() > 150
                    and frame.pixelColor(x, y).green() < 80
                )

            with_stroke = red_glyph_pixels()
            source.text_stroke_width = 0.0
            without_stroke = red_glyph_pixels()
            self.assertGreater(
                with_stroke, without_stroke + 10,
                f"{source_type.value}: text outline did not render",
            )

    def test_text_outline_uses_a_reusable_vector_glyph_path(self) -> None:
        source = Source(SourceType.TEXT, "Outlined", width=420, height=120)
        item = SourceItem(source)
        image = QImage(480, 160, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        painter.setFont(QFont("Segoe UI", 42))
        rect = item.content_rect()
        flags = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine
        first = item._text_layout_path(painter, rect, flags, "Smooth g곡")
        second = item._text_layout_path(painter, rect, flags, "Smooth g곡")
        painter.end()

        self.assertFalse(first.isEmpty())
        self.assertEqual(first.boundingRect(), second.boundingRect())
        self.assertIsNotNone(item._text_outline_path_key)

    def test_text_stroke_width_is_validated(self) -> None:
        payload = Source(
            SourceType.TEXT, "Bad stroke", text_stroke_width=40.0,
        ).to_dict()
        with self.assertRaisesRegex(ValueError, "text stroke width"):
            Source.from_dict(payload)

    def test_legacy_lyrics_style_migrates_to_the_shared_text_color(self) -> None:
        payload = Source(
            SourceType.LYRICS, "Legacy neon lyrics", outline_color="#123456",
        ).to_dict()
        payload["subtitle_style"] = "neon"

        restored = Source.from_dict(payload)

        self.assertEqual(restored.outline_color, "#72E8FF")
        self.assertNotIn("subtitle_style", restored.to_dict())

    def test_personal_color_adjustments_blend_and_preserve_alpha(self) -> None:
        personal = QColor("#804020")
        self.assertEqual(
            adjust_personal_color(personal, "#7F102030", strength=0.0),
            "#7F102030",
        )
        bright = QColor(adjust_personal_color(
            personal, "#000000", brightness=40.0, strength=1.0,
        ))
        dark = QColor(adjust_personal_color(
            personal, "#000000", brightness=-40.0, strength=1.0,
        ))
        self.assertGreater(bright.value(), dark.value())
        shifted = QColor(adjust_personal_color(
            personal, "#000000", hue_shift=120.0, strength=1.0,
        ))
        self.assertNotEqual(shifted.hsvHue(), personal.hsvHue())

    def test_personal_color_project_values_are_validated(self) -> None:
        source = Source(
            SourceType.TEXT, "Personal text", personal_color_enabled=True,
            personal_color_brightness=25.0,
            personal_color_saturation=-10.0,
            personal_color_hue_shift=45.0,
            personal_color_strength=0.65,
        )
        restored = Source.from_dict(source.to_dict())
        self.assertTrue(restored.personal_color_enabled)
        self.assertEqual(restored.personal_color_strength, 0.65)

        invalid = source.to_dict()
        invalid["personal_color_strength"] = 1.1
        with self.assertRaisesRegex(ValueError, "personal_color_strength"):
            Source.from_dict(invalid)

    def test_visualizer_personal_color_changes_follow_track_index(self) -> None:
        overlay = VisualizerOverlay(
            0, 0, 16, 16, "bars", "#FFFFFF",
            personal_colors=("#FF0000", "#00FF00"),
        )
        changes = ExportPreviewDialog._personal_overlay_changes(overlay, 1)
        self.assertEqual(changes["color"], "#00FF00")
        self.assertEqual(changes["particle_secondary_color"], "#00FF00")
        self.assertEqual(
            PythonVisualizerRenderer._track_index_at(
                2.5, ((0.0, 2.0), (2.0, 3.0)),
            ),
            1,
        )
        self.assertEqual(
            PythonVisualizerRenderer._track_index_at(
                8.0, ((0.0, 2.0), (3.0, 2.0)),
            ),
            -1,
        )

    def test_capture_invariant_stream_rasterizes_scene_only_once(self) -> None:
        scene = CanvasScene()
        scene.addItem(SourceItem(Source(SourceType.SHAPE, "Static shape")))
        track = PlaylistTrack("track.wav", "Track", duration_seconds=2.0)
        staged: list[tuple[QImage, float, str]] = []
        sample = ExportFrameSample(track, 1, 0.0, 1.0, 0.0, 0.0)

        def stage(image: QImage, duration: float, key: str) -> RenderFrame:
            staged.append((image, duration, key))
            return RenderFrame(image, duration)

        capturer = ExportCanvasCapturer(
            scene, [track], 2.0, set(), [(None, None)], stage,
            lambda: None, lambda _track, _key: None,
        )
        original_capture = CanvasSnapshot.capture_track
        with patch.object(
            CanvasSnapshot, "capture_track", side_effect=original_capture,
        ) as capture:
            capturer.capture_invariant_stream(sample, "base", 2.0)

        self.assertEqual(capture.call_count, 1)
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0][1:], (2.0, "base"))

    def test_z_band_capture_updates_only_sources_inside_that_band(self) -> None:
        scene = CanvasScene()
        scene.addItem(SourceItem(Source(
            SourceType.TEXT, "Base token", text="%title%", z_index=0,
        )))
        scene.addItem(SourceItem(Source(
            SourceType.TEXT, "Other band token", text="%artist%", z_index=2,
        )))
        track = PlaylistTrack("band.wav", "Band", artist="Artist", duration_seconds=1.0)
        from app.preview import canvas_snapshot as snapshot_module

        original_expand = snapshot_module.expand_track_template
        with patch.object(
            snapshot_module,
            "expand_track_template",
            side_effect=original_expand,
        ) as expand:
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0,
                elapsed_seconds=0.25, timeline_seconds=0.25,
                z_max=0.0,
            )

        expand.assert_called_once()

    def test_mixed_capture_bands_split_static_and_dynamic_z_runs(self) -> None:
        scene = CanvasScene()
        sources = [
            Source(SourceType.SHAPE, "Static bottom", z_index=0),
            Source(SourceType.TEXT, "Dynamic token", text="%title%", z_index=1),
            Source(SourceType.SHAPE, "Static middle", z_index=2),
            Source(SourceType.AUDIO_VISUALIZER, "Reactive", z_index=3),
            Source(SourceType.IMAGE, "Static top", z_index=4),
        ]
        for source in sources:
            scene.addItem(SourceItem(source))
        dynamic_ids = {sources[3].id}
        broad = CanvasSnapshot.z_bands(scene, dynamic_ids)
        split = CanvasSnapshot.split_mixed_capture_bands(
            scene, dynamic_ids, broad, 10.0,
        )

        self.assertEqual(len(broad), 2)
        self.assertEqual(split, [
            (None, 0.0),
            (1.0, 1.0),
            (2.0, broad[0][1]),
            (3, None),
        ])
        invariant = CanvasSnapshot.invariant_stream_keys(
            scene, dynamic_ids, split, 10.0,
        )
        self.assertEqual(invariant, {"base", "layer:1", "layer:2"})

    def test_mixed_capture_band_split_respects_stream_cap(self) -> None:
        scene = CanvasScene()
        for z_index in range(20):
            source = (
                Source(SourceType.SHAPE, f"Static {z_index}", z_index=z_index)
                if z_index % 2 == 0 else
                Source(
                    SourceType.TEXT, f"Dynamic {z_index}",
                    text="%current_time%", z_index=z_index,
                )
            )
            scene.addItem(SourceItem(source))
        split = CanvasSnapshot.split_mixed_capture_bands(
            scene, set(), [(None, None)], 30.0, max_streams=5,
        )
        self.assertLessEqual(len(split), 5)
        self.assertIsNone(split[0][0])
        self.assertIsNone(split[-1][1])

    def test_resize_handle_cursors_follow_item_screen_rotation(self) -> None:
        cursor = SourceItem.cursor_for_edit_handle
        self.assertEqual(cursor("e"), Qt.CursorShape.SizeHorCursor)
        self.assertEqual(cursor("n"), Qt.CursorShape.SizeVerCursor)
        self.assertEqual(cursor("nw"), Qt.CursorShape.SizeFDiagCursor)
        self.assertEqual(cursor("ne"), Qt.CursorShape.SizeBDiagCursor)
        self.assertEqual(cursor("e", 45.0), Qt.CursorShape.SizeFDiagCursor)
        self.assertEqual(cursor("e", 90.0), Qt.CursorShape.SizeVerCursor)
        self.assertEqual(cursor("n", 45.0), Qt.CursorShape.SizeBDiagCursor)
        self.assertEqual(cursor("rotate"), Qt.CursorShape.CrossCursor)

    def test_small_sources_hide_only_crowded_middle_resize_handles(self) -> None:
        source = Source(
            SourceType.SHAPE, "Small", width=32.0, height=24.0,
        )
        item = SourceItem(source)
        item.setSelected(True)

        self.assertEqual(
            set(item.resize_handle_rects()), {"nw", "ne", "se", "sw"},
        )
        source.width = 100.0
        self.assertEqual(
            set(item.resize_handle_rects()),
            {"nw", "n", "ne", "se", "s", "sw"},
        )
        source.height = 80.0
        self.assertEqual(
            set(item.resize_handle_rects()),
            {"nw", "n", "ne", "e", "se", "s", "sw", "w"},
        )

    def test_resize_modifiers_and_album_cover_enforce_requested_aspect(self) -> None:
        def resize(
            source: Source, modifiers: Qt.KeyboardModifiers,
            delta: QPointF,
        ) -> Source:
            item = SourceItem(source)
            item._resizing = True
            item._resize_origin_scene = QPointF(0, 0)
            item._resize_size = (source.width, source.height)
            item._resize_handle = "se"
            item._resize_anchor_scene = item.mapToScene(QPointF(0, 0))
            event = SimpleNamespace(
                scenePos=lambda: delta,
                modifiers=lambda: modifiers,
                accept=lambda: None,
            )
            item.mouseMoveEvent(event)
            return source

        preserved = resize(
            Source(SourceType.SHAPE, "Preserved", width=200, height=100),
            Qt.KeyboardModifier.AltModifier,
            QPointF(50, 10),
        )
        self.assertAlmostEqual(preserved.width / preserved.height, 2.0)

        square = resize(
            Source(SourceType.SHAPE, "Square", width=200, height=100),
            Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier,
            QPointF(50, 10),
        )
        self.assertAlmostEqual(square.width, square.height)

        cover = resize(
            Source(SourceType.ALBUM_COVER, "Cover", width=180, height=120),
            Qt.KeyboardModifier.NoModifier,
            QPointF(60, 10),
        )
        self.assertAlmostEqual(cover.width, cover.height)

    def test_album_cover_model_normalizes_rectangular_input(self) -> None:
        cover = Source(
            SourceType.ALBUM_COVER, "Cover", width=260, height=140,
        )
        self.assertEqual((cover.width, cover.height), (260, 260))

    def test_playlist_preview_keeps_audio_stopped_during_leading_gap(self) -> None:
        track = PlaylistTrack(
            "delayed.wav", "Delayed", duration_seconds=20.0,
            start_time_seconds=10.0,
        )
        selection = ExportPreviewDialog._track_at(
            SimpleNamespace(tracks=[track]), 5.0
        )
        self.assertIsNotNone(selection)
        self.assertFalse(ExportPreviewDialog._selection_has_audio(selection, 5.0))
        self.assertTrue(ExportPreviewDialog._selection_has_audio(selection, 10.0))

    def test_playlist_gap_hides_track_video_but_keeps_timeline_video_running(self) -> None:
        scene = CanvasScene()
        track_source = Source(
            SourceType.VIDEO, "Per-track", video_timing_mode="track",
            video_repeat_mode="loop_one",
        )
        timeline_source = Source(
            SourceType.VIDEO, "Timeline", video_timing_mode="timeline",
            video_repeat_mode="loop_one", video_paths=["timeline.mp4"],
        )
        track_item = SourceItem(track_source)
        timeline_item = SourceItem(timeline_source)
        scene.addItem(track_item)
        scene.addItem(timeline_item)
        observed: dict[str, tuple[str | None, float]] = {}
        track_item.set_video_preview_position = lambda path, seconds=0.0: observed.__setitem__(
            "track", (path, seconds),
        )
        timeline_item.set_video_preview_position = lambda path, seconds=0.0: observed.__setitem__(
            "timeline", (path, seconds),
        )
        preview = SimpleNamespace(
            scene=scene,
            timeline=SimpleNamespace(value=lambda: round(1.5 * TIMELINE_SCALE)),
            _video_duration_cache={"track.mp4": 1.0, "timeline.mp4": 4.0},
        )
        track = PlaylistTrack(
            "song.wav", "Track", duration_seconds=1.0,
            video_paths=["track.mp4"],
        )

        ExportPreviewDialog._sync_video_sources(
            preview, track, track.duration_seconds, track_active=False,
        )

        self.assertEqual(observed["track"], (None, 0.0))
        self.assertEqual(observed["timeline"], ("timeline.mp4", 1.5))
        track_item.release_video_decoder()
        timeline_item.release_video_decoder()

    def test_track_video_time_restarts_at_a_clipped_source_boundary(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.VIDEO, "Clipped track video",
            video_timing_mode="track", video_repeat_mode="once",
            timeline_start=2.0, timeline_duration=1.0,
        )
        item = SourceItem(source)
        scene.addItem(item)
        observed: list[tuple[str | None, float]] = []
        item.set_video_preview_position = lambda path, seconds=0.0: observed.append(
            (path, seconds),
        )
        timeline = SimpleNamespace(value=lambda: round(2.5 * TIMELINE_SCALE))
        preview = SimpleNamespace(
            scene=scene, timeline=timeline,
            _video_duration_cache={"track.mp4": 4.0},
        )
        track = PlaylistTrack(
            "song.wav", "Track", duration_seconds=5.0,
            video_paths=["track.mp4"],
        )

        ExportPreviewDialog._sync_video_sources(
            preview, track, 2.5, track_start=0.0, track_active=True,
        )
        self.assertEqual(observed[-1], ("track.mp4", 0.5))

        timeline.value = lambda: round(3.0 * TIMELINE_SCALE)
        ExportPreviewDialog._sync_video_sources(
            preview, track, 3.0, track_start=0.0, track_active=True,
        )
        self.assertEqual(observed[-1], (None, 0.0))
        item.release_video_decoder()

        class FakePlayer:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        player = FakePlayer()
        preview = SimpleNamespace(
            tracks=[track],
            timeline=SimpleNamespace(value=lambda: round(5.0 * TIMELINE_SCALE)),
            media_player=player,
            _active_track_index=0,
            _last_media_position_ms=5000,
            _closing=False,
            _scrubbing=False,
            _live_mix=None,
        )
        preview._track_at = lambda seconds: ExportPreviewDialog._track_at(
            preview, seconds
        )
        preview._selection_has_audio = ExportPreviewDialog._selection_has_audio
        ExportPreviewDialog._start_audio_at_playhead(preview)
        self.assertTrue(player.stopped)
        self.assertEqual(preview._active_track_index, -1)
        self.assertEqual(preview._last_media_position_ms, 0)

    def test_overlapping_requested_starts_are_sequenced(self) -> None:
        tracks = [
            PlaylistTrack("one.wav", "One", duration_seconds=10.0),
            PlaylistTrack(
                "two.wav", "Two", duration_seconds=10.0, start_time_seconds=5.0,
            ),
        ]
        self.assertEqual(FFmpegRenderer._timeline_duration(tracks), 20.0)

    def test_export_rejects_misaligned_visual_streams(self) -> None:
        base = [(Path("one.png"), 1.0), (Path("two.png"), 1.0)]
        aligned_layer = StaticOverlayLayer(
            1.0, [RenderFrame(Path("layer.png"), 2.0)],
        )
        FFmpegRenderer._validate_visual_timeline(base, [aligned_layer], 2.0, 30)

        with self.assertRaises(RenderError):
            FFmpegRenderer._validate_visual_timeline(base, [], 3.0, 30)
        with self.assertRaises(RenderError):
            FFmpegRenderer._validate_visual_timeline(
                base, [StaticOverlayLayer(1.0, [RenderFrame(Path("layer.png"), 1.5)])],
                2.0, 30,
            )

    def test_audio_normalization_uses_exact_lossless_segments(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        commands: list[list[str]] = []
        renderer._run = lambda arguments, **_kwargs: commands.append(arguments)  # type: ignore[method-assign]
        track = PlaylistTrack(
            "song.mp3", "Song", duration_seconds=2.75, start_time_seconds=0.4,
        )
        with TemporaryDirectory() as directory:
            segments = renderer._normalize_audio(
                [track], Path(directory), RenderSettings(), None, threading.Event(),
            )
            durations = renderer._insert_silence_for_gaps(
                [track], segments, Path(directory), RenderSettings(), None,
                threading.Event(),
            )
            manifest = Path(directory) / "audio.ffconcat"
            renderer._write_concat_file(manifest, segments, durations)
            manifest_text = manifest.read_text(encoding="utf-8")
        self.assertEqual(segments[0].suffix, ".nut")
        self.assertEqual(segments[1].suffix, ".nut")
        self.assertEqual(durations, [0.4, 2.75])
        self.assertIn("apad=whole_dur=2.750000", commands[0])
        self.assertIn("2.750000", commands[0])
        self.assertIn("pcm_f32le", commands[0])
        self.assertIn("duration 0.400000", manifest_text)
        self.assertIn("duration 2.750000", manifest_text)

    def test_audio_tracks_normalize_concurrently_but_keep_playlist_order(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        running = 0
        maximum_running = 0
        lock = threading.Lock()

        def fake_run(_arguments: list[str], **_kwargs: object) -> None:
            nonlocal running, maximum_running
            with lock:
                running += 1
                maximum_running = max(maximum_running, running)
            time.sleep(0.04)
            with lock:
                running -= 1

        renderer._run = fake_run  # type: ignore[method-assign]
        tracks = [
            PlaylistTrack(f"track-{index}.mp3", f"Track {index}", duration_seconds=2.0)
            for index in range(6)
        ]
        with TemporaryDirectory() as directory, patch(
            "app.renderer.ffmpeg_renderer.os.cpu_count", return_value=8,
        ):
            segments = renderer._normalize_audio(
                tracks, Path(directory), RenderSettings(), None, threading.Event(),
            )

        self.assertEqual(maximum_running, 4)
        self.assertEqual(
            [path.name for path in segments],
            [f"track_{index:04d}.nut" for index in range(6)],
        )

    def test_audio_normalization_reports_live_ffmpeg_time(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        messages: list[str] = []

        def fake_run(_arguments: list[str], **kwargs: object) -> None:
            parser = kwargs.get("progress_parser")
            self.assertIsNotNone(parser)
            parser("out_time_us=1000000")  # type: ignore[operator]

        renderer._run = fake_run  # type: ignore[method-assign]
        track = PlaylistTrack("song.mp3", "Song", duration_seconds=2.0)
        with TemporaryDirectory() as directory:
            renderer._normalize_audio(
                [track], Path(directory), RenderSettings(),
                lambda _stage, _fraction, message: messages.append(message),
                threading.Event(),
            )

        self.assertTrue(any(
            "song.mp3" in message and "1.0s / 2.0s" in message
            for message in messages
        ))

    _LOUDNORM_STATS_JSON = (
        '{"input_i" : "-23.10", "input_tp" : "-4.20", "input_lra" : "6.30", '
        '"input_thresh" : "-33.50", "output_i" : "-16.00", "output_tp" : "-1.50", '
        '"output_lra" : "5.90", "output_thresh" : "-26.10", '
        '"normalization_type" : "linear", "target_offset" : "0.40"}'
    )

    def test_prepare_playlist_audio_applies_two_pass_loudness_normalization(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        commands: list[list[str]] = []

        def fake_run(arguments: list[str], **kwargs: object) -> None:
            commands.append(arguments)
            capture_stderr = kwargs.get("capture_stderr")
            if capture_stderr is not None:
                capture_stderr.append(self._LOUDNORM_STATS_JSON)

        renderer._run = fake_run  # type: ignore[method-assign]
        track = PlaylistTrack("song.mp3", "Song", duration_seconds=2.0)
        with TemporaryDirectory() as directory:
            renderer.prepare_playlist_audio(
                [track], Path(directory), RenderSettings(),
            )
        measure_pass, combine_pass = commands[-2], commands[-1]
        self.assertIn("print_format=json", " ".join(measure_pass))
        self.assertIn("-f", measure_pass)
        self.assertIn("null", measure_pass)
        self.assertIn("-af", combine_pass)
        combine_filter = combine_pass[combine_pass.index("-af") + 1]
        self.assertIn("measured_I=-23.1", combine_filter)
        self.assertIn("offset=0.4", combine_filter)
        self.assertIn("linear=true", combine_filter)

    @staticmethod
    def _ebur128_summary(integrated: str, lra: str, true_peak: str) -> str:
        return (
            "[Parsed_ebur128_0 @ 0000] Summary:\n\n  Integrated loudness:\n"
            f"    I:         {integrated} LUFS\n    Threshold: -18.9 LUFS\n\n  Loudness range:\n"
            f"    LRA:         {lra} LU\n    Threshold:  -28.9 LUFS\n\n  True peak:\n"
            f"    Peak:        {true_peak} dBFS\n"
        )

    def _normalization_commands(self, summary: str) -> list[list[str]]:
        renderer = object.__new__(FFmpegRenderer)
        commands: list[list[str]] = []

        def fake_run(arguments: list[str], **kwargs: object) -> None:
            commands.append(arguments)
            capture_stderr = kwargs.get("capture_stderr")
            if capture_stderr is not None:
                joined = " ".join(arguments)
                capture_stderr.append(summary if "ebur128" in joined else self._LOUDNORM_STATS_JSON)

        renderer._run = fake_run  # type: ignore[method-assign]
        track = PlaylistTrack("song.mp3", "Song", duration_seconds=2.0)
        with TemporaryDirectory() as directory:
            renderer.prepare_playlist_audio([track], Path(directory), RenderSettings())
        return commands

    def test_a_linear_normalization_is_the_measured_gain_and_nothing_else(self) -> None:
        # -8.6 LUFS -> -14: -5.4 dB; the true peak lands at -2.0 dBTP, the LRA fits.
        commands = self._normalization_commands(self._ebur128_summary("-8.6", "9.9", "3.4"))
        self.assertFalse(any("print_format=json" in " ".join(command) for command in commands))
        final = commands[-1]
        self.assertEqual(final[final.index("-af") + 1], "volume=-5.40dB")

    def test_peaks_over_the_ceiling_are_limited_without_riding_the_gain(self) -> None:
        limiter = ",aresample=192000,alimiter=limit=0.8414:attack=1:release=60:level=0:latency=1,aresample=48000"
        for (integrated, lra, true_peak), expected in (
            # Loud blends peaking at +5.1 dBTP: the full gain, the few overs limited.
            (("-8.6", "9.9", "5.1"), "volume=-5.40dB" + limiter),
            # A wide loudness range is not compressed: no per-song leveling.
            (("-8.6", "14.0", "-3.0"), "volume=-5.40dB"),
            # Quiet and peaky: at most 3 dB of limiting, so it stays under the target.
            (("-22.0", "9.9", "-1.0"), "volume=2.50dB" + limiter),
        ):
            with self.subTest(integrated=integrated, lra=lra, true_peak=true_peak):
                commands = self._normalization_commands(self._ebur128_summary(integrated, lra, true_peak))
                self.assertFalse(any("loudnorm" in " ".join(command) for command in commands))
                final = commands[-1]
                self.assertEqual(final[final.index("-af") + 1], expected)

    def test_prepare_playlist_audio_skips_normalization_for_near_silent_input(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        commands: list[list[str]] = []
        silent_stats = self._LOUDNORM_STATS_JSON.replace('"-23.10"', '"-inf"')

        def fake_run(arguments: list[str], **kwargs: object) -> None:
            commands.append(arguments)
            capture_stderr = kwargs.get("capture_stderr")
            if capture_stderr is not None:
                capture_stderr.append(silent_stats)

        renderer._run = fake_run  # type: ignore[method-assign]
        track = PlaylistTrack("silence.mp3", "Silence", duration_seconds=2.0)
        with TemporaryDirectory() as directory:
            result = renderer.prepare_playlist_audio(
                [track], Path(directory), RenderSettings(),
            )
        self.assertEqual(result, Path(directory) / "playlist_audio.m4a")
        final_combine = commands[-1]
        self.assertNotIn("-af", final_combine)

    def test_prepare_playlist_audio_falls_back_when_measurement_pass_fails(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        commands: list[list[str]] = []

        def fake_run(arguments: list[str], **kwargs: object) -> None:
            commands.append(arguments)
            if "print_format=json" in " ".join(arguments):
                raise RenderError("FFmpeg returned an unknown error.")

        renderer._run = fake_run  # type: ignore[method-assign]
        track = PlaylistTrack("song.mp3", "Song", duration_seconds=2.0)
        with TemporaryDirectory() as directory:
            result = renderer.prepare_playlist_audio(
                [track], Path(directory), RenderSettings(),
            )
        self.assertEqual(result, Path(directory) / "playlist_audio.m4a")
        final_combine = commands[-1]
        self.assertNotIn("-af", final_combine)

    def test_prepare_playlist_audio_falls_back_when_normalized_combine_fails(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        commands: list[list[str]] = []

        def fake_run(arguments: list[str], **kwargs: object) -> None:
            commands.append(arguments)
            capture_stderr = kwargs.get("capture_stderr")
            if capture_stderr is not None:
                capture_stderr.append(self._LOUDNORM_STATS_JSON)
                return
            if "measured_I=" in " ".join(arguments):
                raise RenderError("[aac] Input contains (near) NaN/+-Inf")

        renderer._run = fake_run  # type: ignore[method-assign]
        track = PlaylistTrack("song.mp3", "Song", duration_seconds=2.0)
        with TemporaryDirectory() as directory:
            result = renderer.prepare_playlist_audio(
                [track], Path(directory), RenderSettings(),
            )
        self.assertEqual(result, Path(directory) / "playlist_audio.m4a")
        # Measurement, the failed normalized combine, then a plain retry.
        audio_path = str(Path(directory) / "playlist_audio.m4a")
        combining_attempts = [
            command for command in commands if audio_path in command
        ]
        self.assertEqual(len(combining_attempts), 2)
        self.assertIn("-af", combining_attempts[0])
        self.assertNotIn("-af", combining_attempts[1])

    def test_timed_progress_message_includes_time_and_percent(self) -> None:
        self.assertEqual(
            FFmpegRenderer._timed_progress_message(
                "Combining audio", 15.0, 60.0, 0.25,
            ),
            "Combining audio 15.0s / 60.0s · 25%",
        )

    def test_export_filter_parallelism_is_bounded_for_high_resolution(self) -> None:
        with patch("app.renderer.ffmpeg_renderer.os.cpu_count", return_value=12):
            hd = FFmpegRenderer._filter_worker_count(RenderSettings(
                fps=60, output_width=1920, output_height=1080,
            ))
            uhd = FFmpegRenderer._filter_worker_count(RenderSettings(
                fps=60, output_width=3840, output_height=2160,
            ))
        # A larger frame keeps fewer workers in flight, never more.
        self.assertLessEqual(uhd, hd)
        self.assertEqual(hd, 6)
        self.assertEqual(uhd, 3)

    def test_export_work_modes_change_bounded_worker_counts(self) -> None:
        with patch("app.renderer.ffmpeg_renderer.os.cpu_count", return_value=12):
            self.assertEqual(FFmpegRenderer._audio_worker_count(
                RenderSettings(work_mode=WORK_MODE_STABLE), 10,
            ), 1)
            self.assertEqual(FFmpegRenderer._audio_worker_count(
                RenderSettings(work_mode=WORK_MODE_AUTO), 10,
            ), 4)
            self.assertEqual(FFmpegRenderer._audio_worker_count(
                RenderSettings(work_mode=WORK_MODE_MAX_SPEED), 10,
            ), 6)
            self.assertEqual(FFmpegRenderer._filter_worker_count(RenderSettings(
                work_mode=WORK_MODE_STABLE,
            )), 1)
            self.assertEqual(FFmpegRenderer._filter_worker_count(RenderSettings(
                work_mode=WORK_MODE_MAX_SPEED,
            )), 8)
            self.assertEqual(FFmpegRenderer._filter_worker_count(RenderSettings(
                work_mode=WORK_MODE_AUTO,
            )), 6)
            self.assertEqual(FFmpegRenderer._filter_worker_count(RenderSettings(
                work_mode=WORK_MODE_MAX_SPEED,
                output_width=3840, output_height=2160, fps=60,
            )), 3)

    def test_cropped_static_stream_is_composited_at_original_coordinates(self) -> None:
        graph = FFmpegRenderer._layered_filter_graph(
            [],
            [(2.0, Path("cropped.mkv"), "alpha_pair", 104, 62)],
            30,
            320,
            180,
        )

        self.assertIn("[2:v:0][2:v:1]alphamerge", graph)
        self.assertIn("overlay=104:62:eof_action=pass", graph)

    def test_export_preflight_checks_inputs_destination_and_real_encoder(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        with TemporaryDirectory(prefix="pvs-preflight-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "song.wav"
            audio.touch()
            output = directory / "new-folder" / "result.mp4"
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with (
                patch.object(renderer, "ensure_encoder_available") as available,
                patch(
                    "app.renderer.ffmpeg_renderer.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                renderer.preflight_export(
                    [PlaylistTrack(str(audio), "Song", duration_seconds=1.0)],
                    output,
                    RenderSettings(video_codec="h264_nvenc"),
                )

            available.assert_called_once_with("h264_nvenc")
            command = run.call_args.args[0]
            self.assertIn("color=c=black:s=1920x1080:r=30", command)
            self.assertIn("h264_nvenc", command)
            self.assertIn("nv12", command)
            self.assertIn("-cq", command)
            self.assertTrue(output.parent.is_dir())
            self.assertEqual(
                list(output.parent.glob(".playlist-canvas-write-test-*.tmp")),
                [],
            )

    def test_export_preflight_rejects_missing_audio_before_encoder_probe(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        with TemporaryDirectory(prefix="pvs-preflight-missing-") as raw_directory:
            output = Path(raw_directory) / "result.mp4"
            with (
                patch.object(renderer, "ensure_encoder_available") as available,
                patch("app.renderer.ffmpeg_renderer.subprocess.run") as run,
                self.assertRaisesRegex(RenderError, "Audio file is missing"),
            ):
                renderer.preflight_export(
                    [PlaylistTrack(
                        str(Path(raw_directory) / "missing.wav"),
                        "Missing", duration_seconds=1.0,
                    )],
                    output,
                    RenderSettings(),
                )

            available.assert_not_called()
            run.assert_not_called()

    def test_export_preflight_reports_unusable_hardware_encoder(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        failed = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="No capable devices found",
        )
        with patch(
            "app.renderer.ffmpeg_renderer.subprocess.run",
            return_value=failed,
        ):
            with self.assertRaises(RenderError) as raised:
                renderer.ensure_encoder_usable(RenderSettings(
                    video_codec="h264_nvenc",
                ))
        self.assertIn("installed but could not start", str(raised.exception))
        self.assertIn("No capable devices", str(raised.exception))

    def test_amf_preflight_uses_selected_format_and_amf_quality_arguments(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        settings = RenderSettings(
            video_codec="h264_amf", output_width=2560, output_height=1440,
            fps=60, crf=17, preset="medium",
        )
        with patch(
            "app.renderer.ffmpeg_renderer.subprocess.run",
            return_value=completed,
        ) as run:
            renderer.ensure_encoder_usable(settings)

        command = run.call_args.args[0]
        self.assertIn("color=c=black:s=2560x1440:r=60", command)
        self.assertIn("h264_amf", command)
        self.assertIn("nv12", command)
        self.assertIn("-quality", command)
        self.assertIn("balanced", command)
        self.assertIn("-qp_i", command)
        self.assertIn("17", command)

    def test_final_video_copies_prepared_aac_without_reencoding(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        renderer.ensure_encoder_available = lambda _encoder: None  # type: ignore[method-assign]
        commands: list[list[str]] = []

        def fake_run(arguments: list[str], **_kwargs: object) -> None:
            commands.append(arguments)
            output = Path(arguments[-1])
            if output.suffix.lower() in {".nut", ".m4a", ".mp4"}:
                output.touch()

        renderer._run = fake_run  # type: ignore[method-assign]
        frame = QImage(16, 16, QImage.Format.Format_RGB32)
        frame.fill(0xFF336699)
        with TemporaryDirectory(prefix="pvs-audio-copy-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "song.wav"
            audio.touch()
            output = directory / "result.mp4"
            renderer.render(
                frame,
                [PlaylistTrack(str(audio), "Song", duration_seconds=1.0)],
                output,
                RenderSettings(
                    fps=30, output_width=16, output_height=16,
                ),
            )
            self.assertTrue(output.is_file())

            final_command = next(command for command in commands if "-c:v" in command)
            output_staging = Path(final_command[-1])
            self.assertEqual(output_staging.parent.resolve(), output.parent.resolve())
            self.assertTrue(output_staging.name.endswith(".rendering.mp4"))
            self.assertFalse(output_staging.exists())

        audio_codec_index = final_command.index("-c:a")
        self.assertEqual(final_command[audio_codec_index + 1], "copy")
        duration_index = final_command.index("-t")
        self.assertEqual(final_command[duration_index + 1], "1.000000")
        self.assertNotIn("-shortest", final_command)

    def test_one_explicit_frame_can_cover_a_multi_track_playlist(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        renderer.ensure_encoder_available = lambda _encoder: None  # type: ignore[method-assign]

        def fake_run(arguments: list[str], **_kwargs: object) -> None:
            output = Path(arguments[-1])
            if output.suffix.lower() in {".nut", ".m4a", ".mp4"}:
                output.touch()

        renderer._run = fake_run  # type: ignore[method-assign]
        frame = QImage(16, 16, QImage.Format.Format_RGB32)
        frame.fill(0xFF224466)
        with TemporaryDirectory(prefix="pvs-sparse-base-") as raw_directory:
            directory = Path(raw_directory)
            first_audio = directory / "first.wav"
            second_audio = directory / "second.wav"
            first_audio.touch()
            second_audio.touch()
            output = directory / "result.mp4"

            result = renderer.render(
                [RenderFrame(frame, 2.0)],
                [
                    PlaylistTrack(str(first_audio), "First", duration_seconds=1.0),
                    PlaylistTrack(str(second_audio), "Second", duration_seconds=1.0),
                ],
                output,
                RenderSettings(fps=30, output_width=16, output_height=16),
            )
            self.assertTrue(output.is_file())

        self.assertEqual(result.track_count, 2)

    def test_failed_final_encode_keeps_existing_output_and_removes_staging(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        renderer.ensure_encoder_available = lambda _encoder: None  # type: ignore[method-assign]

        def fake_run(arguments: list[str], **_kwargs: object) -> None:
            output = Path(arguments[-1])
            if "-c:v" in arguments:
                output.write_bytes(b"incomplete replacement")
                raise RenderError("simulated final encode failure")
            if output.suffix.lower() in {".nut", ".m4a"}:
                output.touch()

        renderer._run = fake_run  # type: ignore[method-assign]
        frame = QImage(16, 16, QImage.Format.Format_RGB32)
        frame.fill(0xFF336699)
        with TemporaryDirectory(prefix="pvs-output-staging-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "song.wav"
            audio.touch()
            output = directory / "existing.mp4"
            output.write_bytes(b"existing completed video")

            with self.assertRaisesRegex(RenderError, "simulated final encode failure"):
                renderer.render(
                    frame,
                    [PlaylistTrack(str(audio), "Song", duration_seconds=1.0)],
                    output,
                    RenderSettings(fps=30, output_width=16, output_height=16),
                )

            self.assertEqual(output.read_bytes(), b"existing completed video")
            self.assertEqual(list(directory.glob(".*.rendering.mp4")), [])

    def test_prepared_canvas_video_bypasses_png_concat_input(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        renderer.ensure_encoder_available = lambda _encoder: None  # type: ignore[method-assign]
        commands: list[list[str]] = []

        def fake_run(arguments: list[str], **_kwargs: object) -> None:
            commands.append(arguments)
            output = Path(arguments[-1])
            if output.suffix.lower() in {".nut", ".m4a", ".mp4"}:
                output.touch()

        renderer._run = fake_run  # type: ignore[method-assign]
        with TemporaryDirectory(prefix="pvs-prepared-video-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "song.wav"
            audio.touch()
            prepared_path = directory / "canvas-stream.mkv"
            prepared_path.touch()
            output = directory / "result.mp4"
            renderer.render(
                PreparedVideoInput(prepared_path, 1.0, 16, 16, 30),
                [PlaylistTrack(str(audio), "Song", duration_seconds=1.0)],
                output,
                RenderSettings(fps=30, output_width=16, output_height=16),
            )

        final_command = next(command for command in commands if "-c:v" in command)
        self.assertIn(str(prepared_path.resolve()), final_command)
        self.assertFalse(any("video.ffconcat" in value for value in final_command))

    def test_directly_encoded_canvas_is_muxed_without_second_video_encode(self) -> None:
        renderer = object.__new__(FFmpegRenderer)
        renderer.executable = Path("ffmpeg.exe")
        encoder_checks: list[str] = []
        renderer.ensure_encoder_available = encoder_checks.append  # type: ignore[method-assign]
        commands: list[list[str]] = []

        def fake_run(arguments: list[str], **_kwargs: object) -> None:
            commands.append(arguments)
            output = Path(arguments[-1])
            if output.suffix.lower() in {".nut", ".m4a", ".mp4"}:
                output.touch()

        renderer._run = fake_run  # type: ignore[method-assign]
        with TemporaryDirectory(prefix="pvs-direct-mux-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "song.wav"
            audio.touch()
            prepared_path = directory / "canvas-final.mkv"
            prepared_path.touch()
            output = directory / "result.mp4"
            renderer.render(
                PreparedVideoInput(
                    prepared_path, 1.0, 1920, 1080, 30,
                    ready_for_mux=True, encoded_codec="h264_amf",
                ),
                [PlaylistTrack(str(audio), "Song", duration_seconds=1.0)],
                output,
                RenderSettings(
                    fps=30, output_width=1920, output_height=1080,
                    video_codec="h264_amf",
                ),
            )

        final_command = next(command for command in commands if command[-1].endswith(".mp4"))
        video_codec_index = final_command.index("-c:v")
        self.assertEqual(final_command[video_codec_index + 1], "copy")
        self.assertNotIn("-vf", final_command)
        self.assertNotIn("-filter_complex", final_command)
        duration_index = final_command.index("-t")
        self.assertEqual(final_command[duration_index + 1], "1.000000")
        self.assertNotIn("-shortest", final_command)
        self.assertEqual(encoder_checks, [])

    def test_visualizer_layers_render_concurrently_and_return_in_z_input_order(self) -> None:
        renderer = PythonVisualizerRenderer(Path("ffmpeg.exe"))
        overlays = [
            SimpleNamespace(kind="visualizer", bar_count=4),
            SimpleNamespace(kind="visualizer", bar_count=4),
        ]
        running = 0
        maximum_running = 0
        lock = threading.Lock()
        progress: list[float] = []

        def fake_encode(*_args: object, **_kwargs: object) -> None:
            nonlocal running, maximum_running
            callback = _args[5]
            with lock:
                running += 1
                maximum_running = max(maximum_running, running)
            callback(0.5, 1, 2)
            time.sleep(0.04)
            callback(1.0, 2, 2)
            with lock:
                running -= 1

        with (
            TemporaryDirectory() as directory,
            patch.object(renderer, "_decode_mono_audio", return_value=np.zeros(8)),
            patch.object(renderer, "_analyze_levels", return_value=np.zeros((2, 4))),
            patch.object(renderer, "_encode_layer", side_effect=fake_encode),
            patch("app.renderer.python_visualizer.os.cpu_count", return_value=8),
        ):
            paths = renderer.render_layers(
                Path("audio.m4a"), overlays, 30, Path(directory), threading.Event(),
                lambda fraction, _message: progress.append(fraction),
            )

        self.assertGreaterEqual(maximum_running, 2)
        self.assertEqual(
            [path.name for path in paths],
            ["python_visualizer_00.mov", "python_visualizer_01.mov"],
        )
        self.assertTrue(all(
            current <= following
            for current, following in zip(progress, progress[1:])
        ))

    def test_preview_audio_analysis_falls_back_when_ffmpeg_cannot_start(self) -> None:
        renderer = PythonVisualizerRenderer(Path("missing-ffmpeg.exe"))
        with patch(
            "app.renderer.python_visualizer.subprocess.run",
            side_effect=OSError("cannot start"),
        ):
            levels = renderer.preview_levels(Path("damaged.mp3"), 2.0, 12)
        self.assertEqual(levels.shape, (12,))
        self.assertTrue(np.allclose(levels, 0.08))

    def test_visualizer_log_bands_have_no_structural_zero_holes(self) -> None:
        renderer = PythonVisualizerRenderer(Path("ffmpeg.exe"))
        rng = np.random.default_rng(20260821)
        samples = rng.normal(0.0, 0.2, renderer.sample_rate).astype(np.float32)

        for band_count in (36, 96):
            levels = renderer._analyze_levels(
                samples, 30, band_count, threading.Event(),
            )
            self.assertEqual(levels.shape, (30, band_count))
            active = np.max(levels, axis=0)
            self.assertTrue(
                np.all(active > 0.0),
                f"{band_count} bands contained fixed zero indices: "
                f"{(np.flatnonzero(active == 0.0) + 1).tolist()}",
            )
            if band_count == 36:
                self.assertTrue(np.all(active[[0, 1, 3, 5, 8]] > 0.0))
                displayed = renderer.process_level_sequence(
                    levels, SimpleNamespace(kind="visualizer"), 36,
                )
                displayed_active = np.max(displayed, axis=0)
                self.assertTrue(np.all(displayed_active[[0, 1, 3, 5, 8]] > 0.0))

    def test_bass_envelope_normalizes_low_frequency_hits(self) -> None:
        levels = np.full((12, 24), 0.01, dtype=np.float32)
        levels[4:7, :4] = 0.8
        envelope = PythonVisualizerRenderer.bass_envelope(levels)
        self.assertEqual(envelope.shape, (12,))
        self.assertTrue(np.all((0.0 <= envelope) & (envelope <= 1.0)))
        self.assertGreater(float(envelope[5]), float(envelope[1]))
        self.assertGreater(float(envelope[7]), 0.0)  # release is smooth, not a snap

    def test_batched_visualizer_fft_is_exact_and_uses_bounded_calls(self) -> None:
        renderer = PythonVisualizerRenderer(Path("ffmpeg.exe"))
        rng = np.random.default_rng(20260829)
        samples = rng.normal(
            0.0, 0.2, renderer.sample_rate * 3,
        ).astype(np.float32)
        fps = 29
        bands = 36

        def scalar_reference() -> np.ndarray:
            frame_count = max(
                1, math.ceil(len(samples) * fps / renderer.sample_rate),
            )
            window = np.hanning(renderer.fft_size).astype(np.float32)
            frequencies = np.fft.rfftfreq(
                renderer.fft_size, 1 / renderer.sample_rate,
            )
            edges = np.geomspace(35.0, renderer.sample_rate / 2, bands + 1)
            bins, probes = renderer._frequency_band_layout(frequencies, edges)
            result = np.zeros((frame_count, bands), dtype=np.float32)
            for frame_index in range(frame_count):
                center = int(frame_index * renderer.sample_rate / fps)
                start = center - renderer.fft_size // 2
                end = start + renderer.fft_size
                segment = np.zeros(renderer.fft_size, dtype=np.float32)
                source_start = max(0, start)
                source_end = min(len(samples), end)
                if source_end > source_start:
                    target_start = source_start - start
                    segment[
                        target_start:target_start + source_end - source_start
                    ] = samples[source_start:source_end]
                spectrum = (
                    np.abs(np.fft.rfft(segment * window))
                    / (renderer.fft_size / 2)
                )
                result[frame_index] = np.clip(
                    renderer._frequency_band_values(
                        spectrum, frequencies, bins, probes,
                    ),
                    0.0,
                    2.0,
                )
            return result

        expected = scalar_reference()
        original_rfft = np.fft.rfft
        with patch(
            "app.renderer.python_visualizer.np.fft.rfft",
            side_effect=original_rfft,
        ) as batched_rfft:
            actual = renderer._analyze_levels(
                samples, fps, bands, threading.Event(),
            )

        self.assertTrue(np.array_equal(actual, expected))
        self.assertEqual(batched_rfft.call_count, math.ceil(len(actual) / 256))

    def test_ffmpeg_encoder_catalog_is_reused_within_one_export(self) -> None:
        with TemporaryDirectory(prefix="encoder-cache-test-") as raw_directory:
            executable = Path(raw_directory) / "ffmpeg.exe"
            executable.touch()
            renderer = FFmpegRenderer(executable)
            completed = SimpleNamespace(
                returncode=0,
                stdout=" V..... libx264rgb\n V..... ffv1\n V..... libx264\n",
            )
            with patch(
                "app.renderer.ffmpeg_renderer.subprocess.run",
                return_value=completed,
            ) as run:
                renderer.ensure_encoder_available("libx264rgb")
                renderer.ensure_encoder_available("ffv1")
                renderer.ensure_encoder_available("libx264")

        run.assert_called_once()

    def test_static_source_with_same_z_as_visualizer_is_not_dropped(self) -> None:
        scene = CanvasScene()
        dynamic = Source(SourceType.AUDIO_VISUALIZER, "Dynamic", z_index=4.0)
        static = Source(SourceType.TEXT, "Static", z_index=4.0)
        scene.addItem(SourceItem(dynamic))
        scene.addItem(SourceItem(static))
        bands = CanvasSnapshot.z_bands(scene, {dynamic.id})
        self.assertEqual(len(bands), 2)
        lower, upper = bands[1]
        self.assertLessEqual(lower or 0.0, static.z_index)
        self.assertTrue(upper is None or static.z_index <= upper)

    def test_static_foreground_stays_above_later_dynamic_layer(self) -> None:
        """A skipped empty band must not make particles cover the foreground."""
        overlays = [
            VisualizerOverlay(0, 0, 16, 16, "bars", "#FFFFFF", z_index=1.0),
            VisualizerOverlay(0, 0, 16, 16, "noise", "#FFFFFF",
                              kind="particles", z_index=3.0),
        ]
        graph = FFmpegRenderer._layered_filter_graph(
            overlays, [(3.0, Path("foreground.ffconcat"))], 30, 16, 16,
        )

        first_dynamic = graph.index("[base][2:v]overlay=")
        second_dynamic = graph.index("[zlayer0][3:v]overlay=")
        foreground = graph.index("[zlayer1][4:v]overlay=0:0")
        self.assertLess(first_dynamic, second_dynamic)
        self.assertLess(second_dynamic, foreground)

    def test_streamed_static_foreground_merges_alpha_before_compositing(self) -> None:
        overlays = [
            VisualizerOverlay(0, 0, 16, 16, "bars", "#FFFFFF", z_index=1.0),
        ]
        graph = FFmpegRenderer._layered_filter_graph(
            overlays, [(2.0, Path("foreground.mkv"), "alpha_pair")],
            30, 16, 16,
        )

        dynamic = graph.index("[base][2:v]overlay=")
        alpha_merge = graph.index("[3:v:0][3:v:1]alphamerge[staticrgba1]")
        foreground = graph.index("[zlayer0][staticrgba1]overlay=0:0")
        self.assertLess(dynamic, alpha_merge)
        self.assertLess(alpha_merge, foreground)

    def test_rotated_dynamic_layers_keep_expanded_corners_transparent(self) -> None:
        overlay = VisualizerOverlay(
            0, 0, 16, 16, "bars", "#FFFFFF", rotation=45.0,
        )

        layered = FFmpegRenderer._layered_filter_graph(
            [overlay], [], 30, 16, 16,
        )
        legacy = FFmpegRenderer._python_visualizer_filter_graph(
            [overlay], 30, 16, 16,
        )

        self.assertIn("rotate=0.785398163397", layered)
        self.assertIn(
            "ow=rotw(0.785398163397):oh=roth(0.785398163397)", layered,
        )
        self.assertNotIn("rotw(iw)", layered)
        self.assertIn("fillcolor=none", layered)
        self.assertIn(
            "ow=rotw(0.785398163397):oh=roth(0.785398163397)", legacy,
        )
        self.assertNotIn("rotw(iw)", legacy)
        self.assertIn("fillcolor=none", legacy)

    def test_half_turn_visualizer_keeps_its_full_canvas_extent(self) -> None:
        overlay = VisualizerOverlay(
            0, 0, 1280, 110, "center", "#D14A4A", rotation=180.0,
        )

        graph = FFmpegRenderer._layered_filter_graph(
            [overlay], [], 30, 1920, 1080,
        )

        self.assertIn(
            "rotate=3.141592653590:ow=rotw(3.141592653590):"
            "oh=roth(3.141592653590)",
            graph,
        )
        self.assertIn("overlay=0:0:eof_action=pass", graph)

    def test_video_export_converts_canvas_effect_percentages(self) -> None:
        clip = VideoClipOverlay(
            Path("clip.mp4"), 0.0, 1.0, 0.0,
            0, 0, 100, 50, 1.0,
            brightness=50.0, contrast=-25.0, saturation=1.4,
        )

        graph = FFmpegRenderer._layered_filter_graph(
            [], [], 30, 320, 180, video_clips=[clip],
        )

        self.assertIn(
            "eq=brightness=0.5000:contrast=0.7500:saturation=1.4000",
            graph,
        )

    def test_rotated_video_export_keeps_the_canvas_center(self) -> None:
        clip = VideoClipOverlay(
            Path("clip.mp4"), 0.0, 1.0, 0.0,
            100, 50, 100, 50, 1.0, rotation=90.0,
        )

        graph = FFmpegRenderer._layered_filter_graph(
            [], [], 30, 320, 180, video_clips=[clip],
        )

        self.assertIn("rotate=1.570796326795", graph)
        self.assertIn(
            "ow=rotw(1.570796326795):oh=roth(1.570796326795)", graph,
        )
        self.assertNotIn("rotw(iw)", graph)
        self.assertIn("overlay=125:25:eof_action=pass", graph)

    def test_video_export_preserves_contain_fill_and_rounded_clip(self) -> None:
        clip = VideoClipOverlay(
            Path("clip.mp4"), 0.0, 1.0, 0.0,
            0, 0, 160, 90, 1.0, fit_mode="contain",
            fill_color="#123ABC", border_radius=12.0,
        )

        graph = FFmpegRenderer._layered_filter_graph(
            [], [], 30, 320, 180, video_clips=[clip],
        )

        self.assertIn("pad=160:90:(ow-iw)/2:(oh-ih)/2:color=0x123ABC", graph)
        self.assertIn("geq=r='r(X,Y)'", graph)
        self.assertIn("pow(12.0000,2)", graph)

    def test_repeated_video_occurrences_share_one_physical_input(self) -> None:
        clips = [
            VideoClipOverlay(
                Path("repeat.mp4"), 0.0, 1.0, 0.0,
                0, 0, 32, 32, 1.0,
            ),
            VideoClipOverlay(
                Path("repeat.mp4"), 2.0, 2.0, 0.0,
                0, 0, 32, 32, 1.0, speed=1.5, loop_input=True,
            ),
            VideoClipOverlay(
                Path("repeat.mp4"), 5.0, 1.0, 0.5,
                0, 0, 32, 32, 1.0,
            ),
        ]

        inputs, slots = FFmpegRenderer._video_input_plan(clips)

        self.assertEqual(slots, [0, 0, 1])
        self.assertEqual(len(inputs), 2)
        self.assertEqual(inputs[0].duration_seconds, 3.0)
        self.assertTrue(inputs[0].loop_input)
        self.assertEqual(inputs[1].media_start_seconds, 0.5)

    def test_shared_video_input_splits_and_trims_each_occurrence(self) -> None:
        clips = [
            VideoClipOverlay(
                Path("repeat.mp4"), 0.0, 0.5, 0.0,
                0, 0, 32, 32, 1.0,
            ),
            VideoClipOverlay(
                Path("repeat.mp4"), 1.0, 0.25, 0.0,
                0, 0, 32, 32, 1.0,
            ),
        ]

        graph = FFmpegRenderer._layered_filter_graph(
            [], [(2.0, Path("foreground.mkv"))], 10, 32, 32,
            video_clips=clips,
        )

        self.assertIn("[2:v]split=2[vsrc0_0][vsrc0_1]", graph)
        self.assertIn("[vsrc0_0]trim=duration=0.50000000", graph)
        self.assertIn("[vsrc0_1]trim=duration=0.25000000", graph)
        # Two logical occurrences use input 2; the static layer follows at 3.
        self.assertIn("[3:v]", graph)
        self.assertNotIn("[4:v]", graph)

    def test_reactive_overlay_uses_track_animation_windows(self) -> None:
        overlay = SimpleNamespace(
            animation_in="fade", animation_out="zoom",
            animation_in_duration=0.5, animation_out_duration=1.0,
        )
        windows = [(1.0, 3.0), (5.0, 2.0)]
        self.assertEqual(
            PythonVisualizerRenderer._animation_state(0.5, windows, overlay),
            ("fade", 0.0, True),
        )
        self.assertEqual(
            PythonVisualizerRenderer._animation_state(1.25, windows, overlay),
            ("fade", 0.5, True),
        )
        self.assertEqual(
            PythonVisualizerRenderer._animation_state(3.5, windows, overlay),
            ("zoom", 0.5, False),
        )
        self.assertEqual(
            PythonVisualizerRenderer._animation_state(4.5, windows, overlay),
            ("zoom", 1.0, False),
        )

    def test_visualizer_layer_uses_same_soft_animation_endpoints(self) -> None:
        image = QImage(320, 180, QImage.Format.Format_RGBA8888)
        image.fill(0xFFFFFFFF)

        entrance_start = PythonVisualizerRenderer._apply_animation(
            image, "slide_left", 0.0, True, 500, 160,
        )
        entrance_end = PythonVisualizerRenderer._apply_animation(
            image, "slide_left", 1.0, True, 500, 160,
        )
        exit_end = PythonVisualizerRenderer._apply_animation(
            image, "slide_left", 1.0, False, 500, 160,
        )

        self.assertEqual(entrance_start.pixelColor(160, 90).alpha(), 0)
        self.assertEqual(exit_end.pixelColor(160, 90).alpha(), 0)
        self.assertEqual(entrance_end.pixelColor(160, 90).alpha(), 255)

    def test_preview_and_export_share_bounded_per_source_animation_timing(self) -> None:
        scene = CanvasScene()
        short = Source(
            SourceType.SHAPE, "Short", animation_in="fade",
            animation_out="fade", animation_duration=0.2, opacity=0.8,
        )
        long = Source(
            SourceType.SHAPE, "Long", animation_in="fade",
            animation_out="fade", animation_duration=1.0, opacity=0.8,
            z_index=1,
        )
        scene.addItem(SourceItem(short))
        scene.addItem(SourceItem(long))
        track = PlaylistTrack("short.wav", "Short track", duration_seconds=1.0)

        preview = SimpleNamespace(scene=scene)
        self.assertEqual(
            ExportPreviewDialog._animation_state(preview, track, 0.6),
            (None, 1.0, 0.0),
        )
        phase, progress, duration = ExportPreviewDialog._animation_state(
            preview, track, 0.8
        )
        self.assertEqual(phase, "out")
        self.assertAlmostEqual(progress, 0.2)
        self.assertAlmostEqual(duration, 0.25)

        observed: dict[str, float] = {}

        def inspect_opacity(capture_scene: CanvasScene, *_args: object, **_kwargs: object) -> QImage:
            observed.update({
                item.source.name: item.opacity()
                for item in capture_scene.items() if isinstance(item, SourceItem)
            })
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=inspect_opacity):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0,
                animation_phase="in", animation_progress=0.5,
                elapsed_seconds=0.5, animation_phase_duration=1.0,
            )
        self.assertAlmostEqual(observed["Short"], 1.0)
        self.assertLess(observed["Long"], 1.0)

    def test_slide_animation_has_restrained_travel_and_no_end_frame_pop(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE, "Slide", x=100, y=80, width=500, height=160,
            opacity=0.82, animation_in="slide_left",
            animation_out="slide_left", animation_in_duration=1.0,
            animation_out_duration=1.0,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("track.wav", "Track", duration_seconds=4.0)
        observed: list[tuple[float, float]] = []

        def inspect_state(*_args: object, **_kwargs: object) -> QImage:
            observed.append((item.pos().x(), item.opacity()))
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=inspect_state):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, animation_phase="in",
                elapsed_seconds=0.0, animation_phase_duration=1.0,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, animation_phase="out",
                elapsed_seconds=4.0, animation_phase_duration=1.0,
            )

        expected_hidden_x = source.x - slide_distance(source.width, source.height)
        self.assertAlmostEqual(observed[0][0], expected_hidden_x)
        self.assertAlmostEqual(observed[1][0], expected_hidden_x)
        self.assertAlmostEqual(observed[0][1], 0.0)
        self.assertAlmostEqual(observed[1][1], 0.0)
        self.assertLess(slide_distance(source.width, source.height), 100.0)
        self.assertAlmostEqual(item.pos().x(), source.x)
        self.assertAlmostEqual(item.opacity(), 1.0)

    def test_moving_animation_stays_legible_then_fades_without_end_pop(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE, "Smooth exit", x=100, y=80,
            animation_out="slide_right", animation_out_duration=1.0,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("track.wav", "Track", duration_seconds=4.0)
        observed: list[float] = []

        def inspect_opacity(*_args: object, **_kwargs: object) -> QImage:
            observed.append(item.opacity())
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=inspect_opacity):
            for elapsed in (3.25, 3.5, 3.75, 3.99):
                CanvasSnapshot.capture_track(
                    scene, track, 1, 1, 0.0, animation_phase="out",
                    elapsed_seconds=elapsed, animation_phase_duration=1.0,
                )

        self.assertEqual(len(observed), 4)
        # A slide exit lingers a little longer than a plain fade so it stays
        # readable while it travels, but keeps dropping every frame and is
        # nearly gone before the window ends (no final-frame pop).
        self.assertGreater(observed[0], observed[1])
        self.assertGreater(observed[1], observed[2])
        self.assertGreater(observed[2], observed[3])
        self.assertGreater(observed[0], 0.6)
        self.assertLess(observed[2], 0.2)
        self.assertLess(observed[3], 0.02)

    def test_exit_and_entrance_opacity_curves_are_bounded_and_distinct(self) -> None:
        from app.animation.curves import entrance_opacity, exit_opacity

        styles = ("fade", "slide_left", "zoom", "pop", "rotate")
        for style in styles:
            for progress in (i / 20 for i in range(21)):
                self.assertGreaterEqual(entrance_opacity(style, progress), 0.0)
                self.assertLessEqual(entrance_opacity(style, progress), 1.0)
                self.assertGreaterEqual(exit_opacity(style, progress), 0.0)
                self.assertLessEqual(exit_opacity(style, progress), 1.0)
            self.assertAlmostEqual(entrance_opacity(style, 0.0), 0.0)
            self.assertAlmostEqual(entrance_opacity(style, 1.0), 1.0)
            self.assertAlmostEqual(exit_opacity(style, 0.0), 1.0)
            self.assertAlmostEqual(exit_opacity(style, 1.0), 0.0)
        # Every exit fade keeps dropping (monotonic, no mid-window rebound).
        for style in styles:
            values = [exit_opacity(style, i / 20) for i in range(21)]
            self.assertTrue(all(a >= b - 1e-9 for a, b in zip(values, values[1:])))
        # The styles genuinely differ at the mid-point of the exit.
        midpoints = {exit_opacity(style, 0.5) for style in styles}
        self.assertGreaterEqual(len(midpoints), 4)

    def test_timeline_windowed_source_fades_at_its_own_edges(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE, "Windowed", x=40, y=40, width=200, height=140,
            fill_color="#FFFFFF", shape_kind="rectangle",
            timeline_start=3.0, timeline_duration=6.0,
            animation_in="fade", animation_in_duration=1.0,
            animation_out="fade", animation_out_duration=1.0,
        )
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("missing.wav", "Track", duration_seconds=20.0)

        def brightness(seconds: float) -> int:
            frame = CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0,
                timeline_seconds=seconds, elapsed_seconds=seconds,
            )
            return frame.pixelColor(120, 100).red()

        self.assertLess(brightness(2.9), 40)          # before its window
        self.assertLess(brightness(3.3), brightness(3.7))   # fading in
        self.assertGreater(brightness(3.7), 150)
        self.assertGreater(brightness(6.0), 240)      # steady, fully shown
        self.assertGreater(brightness(8.3), brightness(8.8))  # fading out
        self.assertLess(brightness(9.1), 40)          # after its window

    def test_animation_opacity_survives_source_item_paint(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE, "Fading", x=0, y=0, width=120, height=120,
            fill_color="#FFFFFF", shape_kind="rectangle",
        )
        item = SourceItem(source)
        scene.addItem(item)
        frame = QImage(120, 120, QImage.Format.Format_ARGB32)

        def render() -> int:
            frame.fill(QColor("#000000"))
            painter = QPainter(frame)
            item.paint(painter, None, None)
            painter.end()
            return frame.pixelColor(60, 60).red()

        painter_full = render()
        painter = QPainter(frame)
        painter.setOpacity(0.4)  # what QGraphicsScene applies for a fading item
        frame.fill(QColor("#000000"))
        item.paint(painter, None, None)
        painter.end()
        half = frame.pixelColor(60, 60).red()
        self.assertGreater(painter_full, 240)
        self.assertLess(half, painter_full - 80)

    def test_pop_and_rotate_animations_restore_source_transform(self) -> None:
        scene = CanvasScene()
        pop = Source(
            SourceType.SHAPE, "Pop", scale=1.2, animation_in="pop",
            animation_in_duration=1.0,
        )
        rotate = Source(
            SourceType.SHAPE, "Rotate", rotation=20.0,
            animation_out="rotate", animation_out_duration=1.0,
        )
        pop_item = SourceItem(pop)
        rotate_item = SourceItem(rotate)
        scene.addItem(pop_item)
        scene.addItem(rotate_item)
        track = PlaylistTrack("track.wav", "Track", duration_seconds=4.0)
        observed: list[tuple[float, float, float]] = []

        def inspect_transform(*_args: object, **_kwargs: object) -> QImage:
            observed.append((pop_item.scale(), rotate_item.rotation(), rotate_item.opacity()))
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=inspect_transform):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, animation_phase="in",
                elapsed_seconds=0.5, animation_phase_duration=1.0,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, animation_phase="out",
                elapsed_seconds=3.5, animation_phase_duration=1.0,
            )

        self.assertGreater(observed[0][0], pop.scale * 0.76)
        self.assertLess(observed[0][0], pop.scale)
        self.assertGreater(observed[1][1], rotate.rotation)
        self.assertLess(observed[1][1], rotate.rotation + 12.0)
        self.assertAlmostEqual(observed[1][2], 0.5)
        self.assertAlmostEqual(pop_item.scale(), pop.scale)
        self.assertAlmostEqual(rotate_item.rotation(), rotate.rotation)
        self.assertAlmostEqual(rotate_item.opacity(), 1.0)

    def test_visualizer_moving_animation_uses_balanced_fade(self) -> None:
        image = QImage(320, 180, QImage.Format.Format_RGBA8888)
        image.fill(0xFFFFFFFF)

        midway = PythonVisualizerRenderer._apply_animation(
            image, "slide_right", 0.5, False, 500, 160,
        )

        self.assertAlmostEqual(midway.pixelColor(160, 90).alpha(), 128, delta=1)

    def test_now_playing_motion_fades_smoothly_during_the_same_exit(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.NOW_PLAYING, "Now Playing",
            x=120.0, y=180.0, width=440.0, height=170.0,
            now_playing_duration=3.0,
            now_playing_exit_animation="slide_up",
            now_playing_exit_duration=1.0,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("track.wav", "Track", duration_seconds=8.0)
        observed: list[tuple[float, float]] = []

        def inspect_state(*_args: object, **_kwargs: object) -> QImage:
            observed.append((item.pos().y(), item.opacity()))
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=inspect_state):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=2.25,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=2.5,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=2.75,
            )

        # Cubic ease-in-out produces a readable fade across the whole exit,
        # instead of retaining full opacity until the final few frames.
        self.assertAlmostEqual(observed[0][1], 0.9375)
        self.assertAlmostEqual(observed[1][1], 0.5)
        self.assertAlmostEqual(observed[2][1], 0.0625)
        self.assertAlmostEqual(observed[1][0], source.y - 12.0)
        self.assertGreater(observed[0][0], observed[1][0])
        self.assertGreater(observed[1][0], observed[2][0])
        self.assertAlmostEqual(item.pos().y(), source.y)
        self.assertAlmostEqual(item.opacity(), 1.0)

    def test_entrance_and_exit_animation_durations_are_independent(self) -> None:
        legacy = Source.from_dict({
            "source_type": "shape", "name": "Legacy",
            "animation_duration": 0.7,
        })
        self.assertEqual(legacy.animation_in_duration, 0.7)
        self.assertEqual(legacy.animation_out_duration, 0.7)

        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE, "Split timing", animation_in="fade",
            animation_out="fade", animation_in_duration=0.2,
            animation_out_duration=0.8,
        )
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("split.wav", "Split", duration_seconds=4.0)
        preview = SimpleNamespace(scene=scene)

        self.assertEqual(
            ExportPreviewDialog._animation_state(preview, track, 0.1),
            ("in", 0.5, 0.2),
        )
        phase, progress, duration = ExportPreviewDialog._animation_state(
            preview, track, 3.6,
        )
        self.assertEqual(phase, "out")
        self.assertAlmostEqual(progress, 0.5)
        self.assertAlmostEqual(duration, 0.8)

        restored = Source.from_dict(source.to_dict())
        self.assertEqual(restored.animation_in_duration, 0.2)
        self.assertEqual(restored.animation_out_duration, 0.8)

    def test_legacy_time_source_is_expanded_dynamically(self) -> None:
        scene = CanvasScene()
        source = Source(SourceType.TIME, "Time", text="12:34")
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("track.wav", "Track", duration_seconds=90.0)
        with patch(
            "app.preview.canvas_snapshot.expand_track_template",
            return_value="01:05",
        ) as expand:
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0,
                elapsed_seconds=65.0, timeline_seconds=65.0,
            )
        expand.assert_called_once()
        self.assertEqual(source.text, "12:34")

    def test_lyrics_lookup_uses_sorted_cues_and_track_plus_source_offset(self) -> None:
        cues = [
            {"start": float(index), "end": float(index + 1), "text": str(index)}
            for index in range(10_000)
        ]
        self.assertEqual(LyricsService.current_cue_index(cues, 8_765.25), 8_765)
        self.assertIsNone(LyricsService.current_cue_index(cues, 10_001.0))

        scene = CanvasScene()
        source = Source(
            SourceType.LYRICS, "Lyrics", width=500, height=160,
            subtitle_timing_offset=0.25,
        )
        scene.addItem(SourceItem(source))
        track = PlaylistTrack(
            "track.wav", "Track", duration_seconds=10.0,
            lyrics=[{"start": 1.0, "end": 3.0, "text": "Line"}],
            lyrics_timing_offset_seconds=0.5,
        )
        with patch.object(
            LyricsService, "current_cue_index", return_value=0,
        ) as lookup:
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=1.0,
            )
        self.assertAlmostEqual(lookup.call_args.args[1], 1.75)

    def test_lyrics_preview_keeps_the_user_selected_text_color(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.LYRICS, "Custom lyrics", width=500, height=160,
            outline_color="#34C98A",
        )
        scene.addItem(SourceItem(source))
        track = PlaylistTrack(
            "track.wav", "Track", duration_seconds=10.0,
            lyrics=[{"start": 0.0, "end": 5.0, "text": "Line"}],
        )
        observed_colors: list[str] = []
        original_capture = CanvasSnapshot.capture

        def observe_capture(*arguments: object, **keywords: object):
            observed_colors.append(source.outline_color)
            return original_capture(*arguments, **keywords)

        with patch.object(CanvasSnapshot, "capture", side_effect=observe_capture):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=1.0,
            )

        self.assertEqual(observed_colors, ["#34C98A"])
        self.assertEqual(source.outline_color, "#34C98A")

    def test_track_with_lyrics_displays_first_line_from_playback_start(self) -> None:
        cues = [
            {"start": 5.0, "end": 6.0, "text": "First line\ncontinued line"},
            {"start": 10.0, "end": 11.0, "text": "Second line"},
        ]
        self.assertEqual(LyricsService.display_cue_index(cues, 0.0), 0)
        self.assertEqual(LyricsService.display_cue_index(cues, 8.0), 0)
        self.assertEqual(LyricsService.display_cue_index(cues, 12.0), 1)
        self.assertIsNone(LyricsService.display_cue_index([], 0.0))

        scene = CanvasScene()
        source = Source(
            SourceType.LYRICS, "Lyrics", text="Configured placeholder",
            subtitle_fallback="No lyrics", subtitle_animation="glow",
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack(
            "track.wav", "Track", duration_seconds=20.0, lyrics=cues,
        )
        displayed: list[tuple[str, float, int, int]] = []
        original_capture = CanvasSnapshot.capture

        def observe_capture(*arguments: object, **keywords: object):
            displayed.append((
                source.text, item.opacity(), source.subtitle_current_line,
                source.subtitle_current_line_count,
            ))
            return original_capture(*arguments, **keywords)

        with patch.object(CanvasSnapshot, "capture", side_effect=observe_capture):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=0.0,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=5.5,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=7.0,
            )

        self.assertTrue(displayed[0][0].startswith("First line"))
        self.assertIn("First line\ncontinued line", displayed[0][0])
        self.assertNotIn("Configured placeholder", displayed[0][0])
        self.assertAlmostEqual(displayed[0][1], source.opacity)
        self.assertEqual(displayed[0][2], -1)
        self.assertEqual(displayed[1][2], 0)
        self.assertEqual(displayed[1][3], 2)
        self.assertEqual(displayed[2][2], -1)
        self.assertEqual(source.text, "Configured placeholder")

    def test_modern_lyric_transitions_animate_and_restore_render_state(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.LYRICS, "Lyrics", width=560, height=220,
            subtitle_animation="glow", subtitle_animation_duration=0.4,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack(
            "track.wav", "Track", duration_seconds=12.0,
            lyrics=[
                {"start": 1.0, "end": 4.0, "text": "Previous lyric line"},
                {"start": 5.0, "end": 8.0, "text": "A softer lyric line"},
            ],
        )
        observed: list[tuple[float, float, float, float, int]] = []
        original_capture = CanvasSnapshot.capture

        def observe_capture(*arguments: object, **keywords: object):
            observed.append((
                item.opacity(), item.scale(), source.subtitle_scroll_offset,
                item._subtitle_transition_progress, item._subtitle_anchor_line,
            ))
            return original_capture(*arguments, **keywords)

        with patch.object(CanvasSnapshot, "capture", side_effect=observe_capture):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=5.1,
            )
            source.subtitle_animation = "rise"
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=5.1,
            )

        apple, spotify = observed
        # Cue changes animate only lyric layout/paint. The containing card no
        # longer pulses in opacity or scale on every line.
        self.assertAlmostEqual(apple[0], 1.0)
        self.assertAlmostEqual(apple[1], source.scale)
        self.assertGreater(apple[2], 0.0)
        self.assertGreater(apple[3], 0.0)
        self.assertLess(apple[3], 1.0)
        self.assertEqual(apple[4], 1)
        # Glow settles symmetrically; Rise moves quickly then slows down.
        self.assertGreater(apple[2], spotify[2])
        self.assertAlmostEqual(item.opacity(), 1.0)
        self.assertAlmostEqual(item.scale(), source.scale)
        self.assertAlmostEqual(source.subtitle_scroll_offset, 0.0)
        self.assertAlmostEqual(item._subtitle_transition_progress, 1.0)
        self.assertEqual(item._subtitle_anchor_line, -1)

    def test_legacy_subtitle_animation_names_migrate_to_glow_or_rise(self) -> None:
        base = Source(SourceType.LYRICS, "Lyrics").to_dict()
        for legacy, expected in (
            ("apple_music", "glow"), ("blur_reveal", "glow"), ("fade", "glow"),
            ("spotify", "rise"), ("scroll_up", "rise"), ("pop", "rise"),
            ("glow", "glow"), ("none", "none"), ("bogus", "glow"),
        ):
            restored = Source.from_dict({**base, "subtitle_animation": legacy})
            self.assertEqual(restored.subtitle_animation, expected, legacy)

    def test_outgoing_lyric_fades_out_instead_of_cutting_with_no_context(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.LYRICS, "Lyrics", width=560, height=220,
            subtitle_context_lines=0, subtitle_next_lines=0,
            subtitle_animation="glow", subtitle_animation_duration=0.5,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack(
            "t.wav", "T", duration_seconds=12.0,
            lyrics=[
                {"start": 1.0, "end": 4.0, "text": "Outgoing line"},
                {"start": 5.0, "end": 9.0, "text": "Incoming line"},
            ],
        )
        seen: list[tuple[float, int, str]] = []
        original_capture = CanvasSnapshot.capture

        def observe(*args: object, **kwargs: object):
            seen.append((
                item._subtitle_transition_progress,
                item._subtitle_previous_line_count,
                source.text,
            ))
            return original_capture(*args, **kwargs)

        with patch.object(CanvasSnapshot, "capture", side_effect=observe):
            # Mid-transition into the second cue.
            CanvasSnapshot.capture_track(scene, track, 1, 1, 0.0, elapsed_seconds=5.15)
            # Then well after it completes.
            CanvasSnapshot.capture_track(scene, track, 1, 1, 0.0, elapsed_seconds=6.5)

        mid, done = seen
        self.assertLess(mid[0], 1.0)
        self.assertGreater(mid[1], 0)
        self.assertIn("Outgoing line", mid[2])
        self.assertIn("Incoming line", mid[2])
        # The outgoing cue is only borrowed for the fade, not kept afterwards.
        self.assertNotIn("Outgoing line", done[2])

    def test_oldest_context_lyric_stays_for_fade_before_leaving(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.LYRICS, "Lyrics", width=560, height=260,
            subtitle_context_lines=1, subtitle_next_lines=0,
            subtitle_animation="rise", subtitle_animation_duration=0.5,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack(
            "t.wav", "T", duration_seconds=16.0,
            lyrics=[
                {"start": 1.0, "end": 4.0, "text": "Old top\\nsecond row"},
                {"start": 5.0, "end": 8.0, "text": "Former current"},
                {"start": 9.0, "end": 13.0, "text": "New current"},
            ],
        )
        seen: list[tuple[float, int, str]] = []

        def observe(*_args: object, **_kwargs: object) -> QImage:
            seen.append((
                item._subtitle_transition_progress,
                item._subtitle_leaving_line_count,
                source.text,
            ))
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=observe):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=9.15,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=10.0,
            )

        mid, done = seen
        self.assertLess(mid[0], 1.0)
        self.assertEqual(mid[1], 2)
        self.assertIn("Old top\nsecond row", mid[2])
        self.assertIn("Former current", mid[2])
        self.assertIn("New current", mid[2])
        self.assertEqual(done[1], 0)
        self.assertNotIn("Old top", done[2])
        self.assertEqual(item._subtitle_leaving_line_count, 0)

    def test_lyric_context_starts_new_cue_from_previous_stable_position(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.LYRICS, "Lyrics", width=560, height=220,
            font_size=30, subtitle_line_spacing=10,
            subtitle_context_lines=1, subtitle_next_lines=1,
            subtitle_animation="glow", subtitle_animation_duration=0.4,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack(
            "track.wav", "Track", duration_seconds=12.0,
            lyrics=[
                {"start": 1.0, "end": 4.0, "text": "Previous"},
                {"start": 5.0, "end": 8.0, "text": "Current"},
                {"start": 9.0, "end": 11.0, "text": "Next"},
            ],
        )
        observed: list[tuple[int, float, int, float, float, int, float]] = []

        def inspect_layout(*_args: object, **_kwargs: object) -> QImage:
            line_height = item._lyric_line_height()
            visual_origin = (
                -item._subtitle_anchor_line * line_height
                + source.subtitle_scroll_offset
            )
            observed.append((
                item._subtitle_anchor_line, source.subtitle_scroll_offset,
                source.subtitle_current_line, item.opacity(), visual_origin,
                item._subtitle_previous_line_count,
                item._subtitle_transition_progress,
            ))
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=inspect_layout):
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=4.999,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=5.0,
            )
            CanvasSnapshot.capture_track(
                scene, track, 1, 1, 0.0, elapsed_seconds=5.2,
            )

        before, transition_start, transition_middle = observed
        self.assertEqual(before[0], 0)
        self.assertEqual(transition_start[0], 1)
        self.assertAlmostEqual(before[4], transition_start[4])
        self.assertLess(transition_middle[1], transition_start[1])
        self.assertEqual(transition_start[2], 1)
        self.assertAlmostEqual(transition_start[3], 1.0)
        self.assertEqual(transition_start[5], 1)
        self.assertEqual(transition_middle[5], 1)
        self.assertAlmostEqual(transition_start[6], 0.0)
        self.assertAlmostEqual(transition_middle[6], 0.5)

    def test_lyric_cue_change_starts_without_a_visible_jump(self) -> None:
        # The first transition frame must look like the last frame before it:
        # the upcoming row neither dips nor jumps, the outgoing one keeps its
        # highlight, and the newly revealed bottom row starts invisible.
        for style in ("glow", "rise"):
            with self.subTest(style=style):
                scene = CanvasScene()
                source = Source(
                    SourceType.LYRICS, "Lyrics", x=40, y=40, width=560, height=260,
                    fill_color="#00000000", outline_color="#FFFFFF", font_size=30,
                    subtitle_context_lines=1, subtitle_next_lines=1,
                    subtitle_animation=style, subtitle_animation_duration=0.4,
                )
                scene.addItem(SourceItem(source))
                track = PlaylistTrack(
                    "track.wav", "Track", duration_seconds=12.0,
                    lyrics=[
                        {"start": 1.0, "end": 5.0, "text": "First lyric"},
                        {"start": 5.0, "end": 8.0, "text": "Second lyric"},
                        {"start": 8.0, "end": 11.0, "text": "Third lyric"},
                    ],
                )
                before, start, middle = (
                    CanvasSnapshot.capture_track(
                        scene, track, 1, 1, 0.0, elapsed_seconds=elapsed,
                        transparent=True,
                    )
                    for elapsed in (4.999, 5.0, 5.2)
                )

                def alpha_difference(first: QImage, second: QImage) -> int:
                    return max(
                        abs(first.pixelColor(x, y).alpha() - second.pixelColor(x, y).alpha())
                        for y in range(40, 300, 2) for x in range(40, 600, 2)
                    )

                self.assertLessEqual(alpha_difference(before, start), 8)
                self.assertGreater(alpha_difference(start, middle), 8)

    def test_text_design_options_render(self) -> None:
        from app.canvas.renderers.base import linear_gradient

        def render(source: Source) -> QImage:
            item = SourceItem(source)
            image = QImage(
                round(source.width), round(source.height),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            image.fill(0)
            painter = QPainter(image)
            item.paint(painter, None)
            painter.end()
            item.deleteLater()
            return image

        def text(**options: object) -> Source:
            return Source(
                SourceType.TEXT, "T", text="Hello", width=300, height=120,
                fill_color="#00000000", outline_color="#FFFFFF", font_size=40,
                **options,
            )

        # Rotation 0 keeps the diagonal gradient older projects were saved with.
        rect = QRectF(0, 0, 200, 100)
        legacy = linear_gradient(rect, 0.0)
        self.assertEqual((legacy.start(), legacy.finalStop()), (rect.topLeft(), rect.bottomRight()))
        turned = linear_gradient(rect, 90.0)
        self.assertNotEqual(turned.start(), rect.topLeft())

        # A glyph shadow leaves the empty box corners clear; a box shadow does not.
        shadow = Shadow(enabled=True, color="#FF0000", blur_radius=8, offset_x=0, offset_y=0, opacity=1.0)
        box = render(text(shadow=shadow))
        glyph = render(text(shadow=Shadow(**asdict(shadow)), text_shadow_glyph=True))
        self.assertGreater(box.pixelColor(4, 4).alpha(), 0)
        self.assertEqual(glyph.pixelColor(4, 4).alpha(), 0)
        self.assertNotEqual(glyph, render(text()))

        # The gradient moves from the box into the glyphs.
        gradient = Gradient(enabled=True, start_color="#FF0000", end_color="#0000FF")
        in_text = render(text(gradient=gradient, text_gradient=True))
        self.assertEqual(in_text.pixelColor(4, 4).alpha(), 0)
        self.assertTrue(any(
            in_text.pixelColor(x, 60).red() > 150 and in_text.pixelColor(x, 60).blue() < 100
            for x in range(0, 300)
        ))

        spaced = SourceItem(text(text_letter_spacing=12.0, text_italic=True, text_case="upper"))
        font = spaced.text_font(40, 600)
        self.assertTrue(font.italic())
        self.assertEqual(font.capitalization(), QFont.Capitalization.AllUppercase)
        self.assertGreater(
            QFontMetricsF(font).horizontalAdvance("Hello"),
            QFontMetricsF(SourceItem(text()).text_font(40, 600)).horizontalAdvance("Hello"),
        )

        def first_ink_column(image: QImage) -> int:
            return min(
                x for y in range(image.height()) for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 40
            )

        def lyric(alignment: str) -> Source:
            return Source(
                SourceType.LYRICS, "L", text="Short", width=400, height=80,
                fill_color="#00000000", outline_color="#FFFFFF", font_size=30,
                text_alignment=alignment, subtitle_animation="none",
            )

        self.assertLess(first_ink_column(render(lyric("left"))), 30)
        self.assertGreater(first_ink_column(render(lyric("center"))), 60)

    def test_long_lyric_line_shrinks_to_one_line_instead_of_being_cut(self) -> None:
        line = "A lyric line a little longer than its box"
        probe = SourceItem(Source(SourceType.LYRICS, "L", font_size=28))
        advance = QFontMetricsF(probe._lyric_fonts["current"]).horizontalAdvance(line)
        width = round(advance * 0.75) + 24  # needs ~0.75 scale to fit

        def ink(text: str) -> tuple[int, int, int]:
            item = SourceItem(Source(
                SourceType.LYRICS, "L", text=text, width=width, height=120,
                fill_color="#00000000", outline_color="#FFFFFF", font_size=28,
                subtitle_animation="none", subtitle_current_line=0,
            ))
            image = QImage(width, 120, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            item.paint(painter, None)
            painter.end()
            points = [(x, y) for y in range(120) for x in range(width)
                      if image.pixelColor(x, y).alpha() > 60]
            xs, ys = [x for x, _ in points], [y for _, y in points]
            return min(xs), max(xs), max(ys) - min(ys)

        left, right, height = ink(line)
        _short_left, _short_right, short_height = ink("Short line")
        # All of it is visible inside the box, one line tall (a wrapped row
        # showed the clipped halves of two lines: ~1.6x a single line).
        self.assertGreaterEqual(left, 6)
        self.assertLessEqual(right, width - 6)
        self.assertLess(height, short_height * 1.2)

    def test_every_design_preset_is_valid_and_renders(self) -> None:
        from app.presets.preset_preview import render_preset_thumbnail
        from app.presets.preset_service import PresetService

        presets = PresetService.all()
        self.assertEqual(len({preset.identifier for preset in presets}), len(presets))
        lyric_presets = 0
        for preset in presets:
            with self.subTest(preset=preset.identifier):
                sources = preset.builder()
                # Project validation must accept every value a preset sets.
                restored = [Source.from_dict(source.to_dict()) for source in sources]
                self.assertEqual([s.to_dict() for s in restored], [s.to_dict() for s in sources])
                lyric_presets += any(s.source_type is SourceType.LYRICS for s in sources)
                self.assertFalse(render_preset_thumbnail(sources, 160, 90).isNull())
        self.assertGreaterEqual(lyric_presets, 5)

    def test_mask_cuts_the_source_and_its_shadow_to_the_shape(self) -> None:
        def render(**options: object) -> QImage:
            item = SourceItem(Source(
                SourceType.SHAPE, "S", width=200, height=200, fill_color="#FFFFFF",
                **options,
            ))
            image = QImage(200, 200, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            item.paint(painter, None)
            painter.end()
            return image

        plain, circle, star = render(), render(mask_shape="circle"), render(mask_shape="star")
        self.assertGreater(plain.pixelColor(3, 3).alpha(), 0)
        self.assertEqual(circle.pixelColor(3, 3).alpha(), 0)
        self.assertGreater(circle.pixelColor(100, 100).alpha(), 0)
        self.assertEqual(star.pixelColor(100, 196).alpha(), 0)  # between the lower points

    def test_loop_motion_moves_the_capture_and_restores_the_item(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE, "Floating", x=300, y=200, width=160, height=160,
            fill_color="#FFFFFF", loop_motion="float", loop_motion_period=4.0,
            loop_motion_amount=2.0,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack("t.wav", "T", duration_seconds=20.0)
        positions: list[float] = []

        def observe(*_args: object, **_kwargs: object) -> QImage:
            positions.append(item.pos().y())
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=observe):
            for elapsed in (0.0, 1.0, 3.0):
                CanvasSnapshot.capture_track(scene, track, 1, 1, 0.0, elapsed_seconds=elapsed)

        rest, up, down = positions
        self.assertAlmostEqual(rest, 200.0)
        self.assertLess(up, 200.0)
        self.assertGreater(down, 200.0)
        self.assertAlmostEqual(item.pos().y(), 200.0)
        self.assertFalse(CanvasSnapshot.source_is_capture_invariant(source, 60.0))

    def test_text_line_gap_spreads_multi_line_text(self) -> None:
        def ink_height(gap: float) -> int:
            item = SourceItem(Source(
                SourceType.TEXT, "T", text="One\nTwo\nThree", width=300, height=300,
                fill_color="#00000000", outline_color="#FFFFFF", font_size=24,
                text_line_gap=gap,
            ))
            image = QImage(300, 300, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            item.paint(painter, None)
            painter.end()
            rows = [y for y in range(300) if any(
                image.pixelColor(x, y).alpha() > 60 for x in range(0, 300, 2)
            )]
            return rows[-1] - rows[0]

        self.assertGreater(ink_height(30.0), ink_height(0.0) + 40)
        self.assertLess(ink_height(-6.0), ink_height(0.0))

    def test_now_playing_label_text_and_alignment(self) -> None:
        def render(**options: object) -> QImage:
            item = SourceItem(Source(
                SourceType.NOW_PLAYING, "NP", width=440, height=170,
                fill_color="#00000000", outline_color="#FFFFFF",
                text="NOW PLAYING\nTitle\nArtist", now_playing_style="minimal", **options,
            ))
            image = QImage(440, 170, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            item.paint(painter, None)
            painter.end()
            return image

        def ink_columns(image: QImage, top: int, bottom: int) -> list[int]:
            return [x for x in range(image.width()) if any(
                image.pixelColor(x, y).alpha() > 60 for y in range(top, bottom)
            )]

        default = render()
        self.assertTrue(ink_columns(default, 12, 30))  # heading drawn
        self.assertFalse(ink_columns(render(now_playing_label=""), 12, 22))
        self.assertNotEqual(render(now_playing_label="지금 재생 중"), default)
        right = ink_columns(render(now_playing_align="right"), 12, 30)
        self.assertGreater(min(right), max(ink_columns(default, 12, 30)) - 60)

    def test_center_cover_joins_a_radial_visualizer(self) -> None:
        from app.inspector.source_inspector import SourceInspector
        from app.services.source_store import SourceStore

        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        try:
            visualizer = Source(
                SourceType.AUDIO_VISUALIZER, "Ring", x=100, y=50, width=400, height=400,
                visualizer_style="radial", visualizer_inner_radius=0.5, z_index=3,
            )
            store.add(visualizer)
            inspector._add_center_cover()
            covers = [s for s in store.sources() if s.source_type is SourceType.ALBUM_COVER]
            self.assertEqual(len(covers), 1)
            cover = covers[0]
            self.assertEqual(cover.album_frame_style, "circle")
            self.assertAlmostEqual(cover.x + cover.width / 2, 300.0)
            self.assertAlmostEqual(cover.y + cover.height / 2, 250.0)
            self.assertLess(cover.width, 400 * 0.5)
            self.assertGreater(cover.z_index, visualizer.z_index)
            self.assertIsNotNone(cover.group_id)
            self.assertEqual(cover.group_id, visualizer.group_id)
        finally:
            inspector.deleteLater()

        # The ring itself paints on the Canvas and in reactive export layers.
        image = PythonVisualizerRenderer.preview_image(
            200, 200, SimpleNamespace(
                style="radial", kind="visualizer", color="#FFFFFF", opacity=1.0,
                line_width=4.0, inner_radius=0.5,
            ),
            np.full(24, 0.8, dtype=np.float32), 0, processed=True,
        )
        self.assertEqual(image.pixelColor(100, 100).alpha(), 0)  # free centre
        self.assertTrue(any(  # bars radiate above the ring
            image.pixelColor(x, y).alpha() > 0 for x in range(92, 109) for y in range(15, 45)
        ))

    def test_progress_knob_styles_draw_a_handle(self) -> None:
        def render(knob: str) -> QImage:
            item = SourceItem(Source(
                SourceType.PROGRESS_BAR, "P", width=300, height=20,
                progress_value=0.5, progress_knob=knob, progress_style="rounded",
                fill_color="#FFFFFF", progress_track_color="#000000",
            ))
            image = QImage(300, 20, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            item.paint(painter, None)
            painter.end()
            return image

        plain, circle, bar = render("none"), render("circle"), render("bar")
        # The slimmer track leaves the top edge clear except where the handle is.
        self.assertEqual(circle.pixelColor(60, 1).alpha(), 0)
        self.assertGreater(circle.pixelColor(150, 2).alpha(), 0)
        self.assertGreater(bar.pixelColor(150, 1).alpha(), 0)
        self.assertGreater(plain.pixelColor(60, 10).alpha(), 0)
        self.assertEqual(render("auto"), plain)

    def test_lyric_line_height_preserves_font_descenders(self) -> None:
        source = Source(
            SourceType.LYRICS,
            "Descenders",
            text="gypqj",
            font_size=42,
            subtitle_line_spacing=0,
        )
        item = SourceItem(source)
        regular = QFontMetricsF(item._lyric_fonts["regular"])
        current = QFontMetricsF(item._lyric_fonts["current"])
        required = (
            max(regular.height(), current.height())
            + max(2.0, max(regular.descent(), current.descent()) * 0.35)
        )

        self.assertGreaterEqual(item._lyric_line_height(), required)

    def test_preview_lyrics_do_not_paint_outside_the_canvas_element_bounds(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.LYRICS, "Bounded lyrics",
            x=180, y=210, width=360, height=76,
            fill_color="#00000000", outline_color="#FFFFFF",
            font_size=36, subtitle_line_spacing=10,
            subtitle_context_lines=1, subtitle_next_lines=1,
            subtitle_animation="none",
        )
        scene.addItem(SourceItem(source))
        track = PlaylistTrack(
            "track.wav", "Track", duration_seconds=12.0,
            lyrics=[
                {"start": 0.0, "end": 2.0, "text": "Previous line"},
                {"start": 2.0, "end": 6.0, "text": "Current line"},
                {"start": 6.0, "end": 10.0, "text": "Next line"},
            ],
        )

        image = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0, elapsed_seconds=3.0,
            transparent=True,
        )
        left = round(source.x)
        right = round(source.x + source.width)
        top = round(source.y)
        bottom = round(source.y + source.height)
        outside_alpha = [
            image.pixelColor(x, y).alpha()
            for y in (*range(max(0, top - 80), top),
                      *range(bottom, min(image.height(), bottom + 80)))
            for x in range(left, min(image.width(), right + 1))
        ]

        self.assertTrue(any(
            image.pixelColor(x, y).alpha() > 0
            for y in range(top, min(image.height(), bottom))
            for x in range(left, min(image.width(), right + 1))
        ))
        self.assertFalse(any(outside_alpha))

    def test_lyric_font_and_blur_cache_survives_unrelated_source_edits(self) -> None:
        source = Source(SourceType.LYRICS, "Lyrics")
        item = SourceItem(source)
        item._lyric_ghost_cache[("sentinel",)] = object()  # type: ignore[assignment]
        source.x += 20
        item.apply_source()
        self.assertIn(("sentinel",), item._lyric_ghost_cache)
        source.font_size += 1
        item.apply_source()
        self.assertNotIn(("sentinel",), item._lyric_ghost_cache)

    def test_webvtt_lyrics_are_supported_and_sorted(self) -> None:
        with TemporaryDirectory(prefix="pvs-vtt-test-") as raw_directory:
            path = Path(raw_directory) / "lyrics.vtt"
            path.write_text(
                "WEBVTT\n\n00:00:02.000 --> 00:00:03.000\nSecond\n\n"
                "00:00:00.500 --> 00:00:01.500\nFirst\n",
                encoding="utf-8",
            )
            cues = LyricsService.load(path)
        self.assertEqual([cue["text"] for cue in cues], ["First", "Second"])

    def test_audio_import_detection_preserves_missing_tag_fields(self) -> None:
        service = PlaylistService()
        with TemporaryDirectory(prefix="audio-metadata-detection-") as raw_directory:
            path = Path(raw_directory) / "untagged.mp3"
            path.touch()
            audio = SimpleNamespace(
                tags={"title": ["Tagged title"]},
                info=SimpleNamespace(length=42.5),
            )
            with patch(
                "app.services.playlist_service.MutagenFile", return_value=audio,
            ):
                candidates = service.inspect_files([path])

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.track.title, "Tagged title")
        self.assertEqual(candidate.track.artist, "Unknown Artist")
        self.assertEqual(candidate.track.album, "Unknown Album")
        self.assertEqual(candidate.missing_fields, ("artist", "album"))
        self.assertAlmostEqual(candidate.track.duration_seconds, 42.5)

    def test_lrc_generator_service_writes_metadata_and_round_trips_cues(self) -> None:
        cues = [
            {"start": 12.34, "end": 20.0, "text": "Second"},
            {"start": 1.25, "end": 12.34, "text": "First\ncontinued"},
        ]
        rendered = LyricsService.format_lrc(
            cues, title="Example", artist="Artist"
        )
        self.assertIn("[ti:Example]", rendered)
        self.assertIn("[ar:Artist]", rendered)
        self.assertIn(r"[00:01.25]First\ncontinued", rendered)
        self.assertNotIn("[00:01.25]First\ncontinued", rendered)
        self.assertLess(rendered.index("[00:01.25]First"), rendered.index("[00:12.34]Second"))
        with TemporaryDirectory(prefix="pvs-lrc-writer-") as raw_directory:
            saved = LyricsService.save_lrc(
                Path(raw_directory) / "generated", cues,
                title="Example", artist="Artist",
            )
            restored = LyricsService.load(saved)
        self.assertEqual(saved.suffix, ".lrc")
        self.assertEqual([cue["text"] for cue in restored], ["First\ncontinued", "Second"])
        self.assertAlmostEqual(float(restored[0]["start"]), 1.25)

    def test_lrc_generator_supports_blank_line_separated_multiline_units(self) -> None:
        dialog = LrcGeneratorDialog([], Translator())
        try:
            multiline_index = dialog.input_mode_combo.findData("multiline")
            dialog.input_mode_combo.setCurrentIndex(multiline_index)
            dialog.lyrics_editor.setPlainText(
                "First visual line\nSecond visual line\n\nNext timed lyric"
            )
            dialog._prepare_lines()
            self.assertEqual(
                dialog.lines,
                ["First visual line\nSecond visual line", "Next timed lyric"],
            )
            self.assertEqual(dialog.timeline_table.rowCount(), 2)
            self.assertIn("\n", dialog.timeline_table.item(0, 2).text())
            dialog.timestamps = [1.0, 4.0]
            rendered = LyricsService.format_lrc(dialog.timed_cues())
            self.assertIn(r"[00:01.00]First visual line\nSecond visual line", rendered)
            self.assertEqual(
                LyricsService._parse_lrc(rendered)[0]["text"],
                "First visual line\nSecond visual line",
            )
        finally:
            dialog.close()

    def test_lrc_generator_records_undoes_and_previews_partial_timing(self) -> None:
        dialog = LrcGeneratorDialog([], Translator())
        dialog.lyrics_editor.setPlainText("First line\nSecond line\nThird line")
        dialog._prepare_lines()
        dialog.audio_path = str(Path("test-audio.wav").resolve())
        dialog.calibration_spin.setValue(-40)
        with patch.object(dialog.media_player, "position", return_value=12_340):
            dialog._record_timestamp()
        self.assertAlmostEqual(float(dialog.timestamps[0] or 0.0), 12.30)
        self.assertEqual(dialog.current_index, 1)
        self.assertIn("First line", dialog.timeline_table.item(0, 2).text())
        dialog._undo_record()
        self.assertIsNone(dialog.timestamps[0])
        self.assertEqual(dialog.current_index, 0)
        dialog._redo_record()
        self.assertAlmostEqual(float(dialog.timestamps[0] or 0.0), 12.30)
        cues = dialog.timed_cues()
        self.assertEqual(len(cues), 1)
        dialog.pages.setCurrentIndex(2)
        dialog.preview_mode_check.setChecked(True)
        dialog._position_changed(12_500)
        self.assertEqual(dialog._playback_highlight_row, 0)
        self.assertTrue(dialog.timeline_table.item(0, 0).text().startswith("♪"))
        self.assertFalse(dialog.record_button.isEnabled())
        self.assertFalse(dialog.timeline_table.item(0, 0).text().startswith("▶"))
        dialog._open_shortcuts()
        self.assertIsNotNone(dialog._shortcuts_dialog)
        assert dialog._shortcuts_dialog is not None
        self.assertGreaterEqual(dialog._shortcuts_dialog.table.rowCount(), 6)
        self.assertIn("F1", dialog._shortcuts_dialog.table.item(4, 0).text())
        self.assertEqual(dialog.shortcuts_shortcut.key().toString(), "Shift+F1")
        with patch("app.dialogs.help_dialog.HelpDialog") as help_dialog:
            dialog.help_shortcut.activated.emit()
        self.assertEqual(help_dialog.call_args.kwargs, {"tab": "lyrics_editor", "topic": "lrc_timing"})
        dialog.close()

    def test_ffprobe_is_used_as_duration_fallback(self) -> None:
        with TemporaryDirectory(prefix="pvs-probe-test-") as raw_directory:
            directory = Path(raw_directory)
            ffmpeg = directory / "ffmpeg.exe"
            ffprobe = directory / "ffprobe.exe"
            ffmpeg.touch()
            ffprobe.touch()
            with (
                patch(
                    "app.services.playlist_service.FFmpegRenderer.find_executable",
                    return_value=ffmpeg,
                ),
                patch(
                    "app.services.playlist_service.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="123.45\n"),
                ),
            ):
                self.assertEqual(
                    PlaylistService._probe_duration(directory / "track.wav"), 123.45
                )

    def test_playlist_files_require_explicit_overwrite(self) -> None:
        service = PlaylistExportService()
        tracks = [PlaylistTrack("one.wav", "One", duration_seconds=10.0)]
        with TemporaryDirectory(prefix="pvs-playlist-export-") as raw_directory:
            directory = Path(raw_directory)
            service.export(tracks, directory)
            with self.assertRaises(PlaylistExportError):
                service.export(tracks, directory)
            service.export(tracks, directory, overwrite=True)

    def test_stale_project_cache_is_removed_but_current_cache_is_kept(self) -> None:
        with TemporaryDirectory(prefix="pvs-cache-test-") as raw_directory:
            roots = [
                Path(raw_directory) / name / "project-cache"
                for name in ("PlaylistCanvas", "PlaylistVideoStudio")
            ]
            old = time.time() - 60 * 86_400
            for root in roots:
                stale = root / "stale"
                current = root / "current"
                stale.mkdir(parents=True)
                current.mkdir()
                os.utime(stale, (old, old))
                os.utime(current, (old, old))
            with patch("app.services.project_service.gettempdir", return_value=raw_directory):
                ProjectService.cleanup_cache(max_age_days=30, keep={"current"})
            for root in roots:
                self.assertFalse((root / "stale").exists())
                self.assertTrue((root / "current").exists())

    def test_ffmpeg_worker_reports_unexpected_errors(self) -> None:
        class BrokenInstaller:
            def install_latest(self, *_args: object) -> object:
                raise PermissionError("install folder denied")

        worker = FFmpegInstallWorker(BrokenInstaller())  # type: ignore[arg-type]
        messages: list[str] = []
        worker.failed.connect(messages.append)
        worker.run()
        self.assertEqual(len(messages), 1)
        self.assertIn("install folder denied", messages[0])

    def test_ffmpeg_release_api_failure_uses_official_latest_links(self) -> None:
        installer = ManagedFFmpegInstaller(Path("unused-test-install-root"))
        updates: list[tuple[str, float, str]] = []
        with patch.object(
            installer, "_read_json",
            side_effect=FFmpegInstallError("GitHub API rate limited"),
        ):
            tag, archive_url, checksum_url = installer._release_assets(
                threading.Event(), lambda *update: updates.append(update),
            )
        self.assertEqual(tag, "latest")
        self.assertTrue(archive_url.endswith(installer.archive_name))
        self.assertTrue(checksum_url.endswith(installer.checksum_name))
        self.assertIn("/releases/download/latest/", archive_url)
        self.assertTrue(any("official latest" in message for _, _, message in updates))

    def test_ffmpeg_catalog_lists_supported_static_versions_and_recommends_nine(self) -> None:
        installer = ManagedFFmpegInstaller(Path("unused-test-install-root"))
        release = {
            "tag_name": "latest",
            "name": "Latest Auto-Build",
            "published_at": "2026-08-25T13:00:00Z",
            "body": "Windows master `N-126300-gabc`\n9.0 `n9.0.1-gdef`\n8.1 `n8.1.2-ghij`",
            "assets": [
                {"name": "checksums.sha256", "browser_download_url": "https://test/checksums"},
                {"name": "ffmpeg-master-latest-win64-gpl.zip", "browser_download_url": "https://test/master"},
                {"name": "ffmpeg-n9.0-latest-win64-gpl-9.0.zip", "browser_download_url": "https://test/9"},
                {"name": "ffmpeg-n8.1-latest-win64-gpl-8.1.zip", "browser_download_url": "https://test/8"},
                {"name": "ffmpeg-n9.0-latest-win64-gpl-shared-9.0.zip", "browser_download_url": "https://test/shared"},
                {"name": "ffmpeg-n9.0-latest-win64-lgpl-9.0.zip", "browser_download_url": "https://test/lgpl"},
            ],
        }
        with patch.object(installer, "_read_json", return_value=release):
            options = installer.available_releases()

        self.assertEqual([option.series for option in options], ["9.0", "8.1", "master"])
        self.assertTrue(options[0].recommended)
        self.assertEqual(options[0].build, "n9.0.1-gdef")
        self.assertEqual(options[1].build, "n8.1.2-ghij")
        self.assertEqual(options[2].build, "N-126300-gabc")
        self.assertTrue(all(option.checksum_url == "https://test/checksums" for option in options))
        self.assertIn("FFmpeg 9.0", options[0].notes)

    def test_managed_ffmpeg_delete_is_scoped_to_the_active_version(self) -> None:
        with TemporaryDirectory(prefix="pvs-managed-ffmpeg-delete-") as raw_directory:
            root = Path(raw_directory)
            installer = ManagedFFmpegInstaller(root)
            active = root / "versions" / "9.0-test" / "bin" / "ffmpeg.exe"
            sibling = root / "versions" / "8.1-keep" / "bin" / "ffmpeg.exe"
            active.parent.mkdir(parents=True)
            sibling.parent.mkdir(parents=True)
            active.touch()
            sibling.touch()
            installation = installer.current_installation()
            self.assertIsNone(installation)
            installer._activate(ManagedFFmpegInstallation(
                active, "n9-test", "9.0", "latest"
            ))
            # Deletion must remain available even when the executable cannot
            # pass validation (for example after an interrupted disk cleanup).
            self.assertIsNone(installer.current_installation())
            self.assertIsNotNone(installer.recorded_installation())
            removed = installer.uninstall_current()
            self.assertIsNotNone(removed)
            self.assertFalse(active.parent.parent.exists())
            self.assertTrue(sibling.exists())
            self.assertFalse((root / "current.json").exists())

    def test_ffmpeg_safe_extract_rejects_oversized_expansion(self) -> None:
        with TemporaryDirectory(prefix="pvs-ffmpeg-limit-test-") as raw_directory:
            directory = Path(raw_directory)
            archive = directory / "ffmpeg.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
                package.writestr("ffmpeg/bin/ffmpeg.exe", b"x" * 64)
            installer = ManagedFFmpegInstaller(directory / "install")
            installer.max_extracted_bytes = 32
            with self.assertRaisesRegex(FFmpegInstallError, "safe size limit"):
                installer._safe_extract(archive, directory / "extracted")

    def test_visualizer_progress_reports_frames_percent_and_eta(self) -> None:
        message = PythonVisualizerRenderer._frame_progress_message(
            layer_number=1,
            layer_count=2,
            frame_number=120,
            layer_frames=300,
            completed_frames=120,
            total_frames=600,
            elapsed_seconds=10.0,
        )
        self.assertIn("Visualizer 1/2", message)
        self.assertIn("frame 120/300", message)
        self.assertIn("20.0%", message)
        self.assertIn("00:40 remaining", message)

    def test_parallel_visualizer_progress_combines_every_layer_status(self) -> None:
        message = PythonVisualizerRenderer._combined_layer_progress_message(
            [120, 96], [300, 300],
        )

        lines = message.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("Visualizer 1/2 · frame 120/300 · 40.0%", lines[0])
        self.assertIn("Visualizer 2/2 · frame 96/300 · 32.0%", lines[1])
        self.assertEqual(
            lines[2], "All visualizers · frame 216/600 · 36.0%",
        )

    def test_parallel_visualizer_workers_publish_one_combined_update(self) -> None:
        renderer = PythonVisualizerRenderer(Path("ffmpeg.exe"))
        overlays = [
            SimpleNamespace(kind="visualizer", bar_count=8, width=16, height=16),
            SimpleNamespace(kind="waveform", bar_count=8, width=16, height=16),
        ]
        updates: list[tuple[float, str]] = []

        def fake_encode(
            path: Path, _overlay: object, _levels: np.ndarray, _fps: int,
            _cancel: threading.Event, report: object, *_arguments: object,
        ) -> None:
            report(0.5, 12, 24)
            path.touch()

        with TemporaryDirectory(prefix="visualizer-progress-test-") as raw_directory:
            with (
                patch.object(
                    renderer, "_decode_mono_audio",
                    return_value=np.zeros(24, dtype=np.float32),
                ),
                patch.object(
                    renderer, "_analyze_levels",
                    return_value=np.zeros((24, 8), dtype=np.float32),
                ),
                patch.object(
                    renderer, "_analyze_waveform",
                    return_value=np.zeros((24, 8), dtype=np.float32),
                ),
                patch.object(renderer, "_encode_layer", side_effect=fake_encode),
            ):
                paths = renderer.render_layers(
                    Path("audio.m4a"), overlays, 30, Path(raw_directory),
                    threading.Event(), lambda fraction, message: updates.append(
                        (fraction, message)
                    ),
                )

        combined = [message for _fraction, message in updates if "All visualizers" in message]
        self.assertTrue(combined)
        self.assertTrue(all(
            "Visualizer 1/2" in message and "Visualizer 2/2" in message
            for message in combined
        ))
        self.assertEqual(len(paths), 2)
        fractions = [fraction for fraction, _message in updates]
        self.assertEqual(fractions, sorted(fractions))

    def test_about_dialog_provides_copyable_diagnostics(self) -> None:
        with TemporaryDirectory(prefix="pvs-about-test-") as raw_directory:
            ffmpeg = Path(raw_directory) / "ffmpeg.exe"
            dialog = AboutDialog(
                Translator(), ffmpeg, Path(raw_directory) / "logs"
            )
            diagnostics = dialog.diagnostic_text()
            self.assertIn(f"App version: {__version__}", diagnostics)
            self.assertIn(str(ffmpeg), diagnostics)
            self.assertIn("https://github.com/tharu8813/Playlist-Canvas", diagnostics)
            self.assertTrue(dialog.repository_link.openExternalLinks())
            self.assertIn("github.com/tharu8813/Playlist-Canvas", dialog.repository_link.text())
            dialog._copy_diagnostics()
            self.assertEqual(QApplication.clipboard().text(), diagnostics)
            dialog.close()


if __name__ == "__main__":
    unittest.main()
