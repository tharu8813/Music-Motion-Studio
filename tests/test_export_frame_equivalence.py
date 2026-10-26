"""Pixel-level equivalence checks for optimized Canvas export capture."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from hashlib import sha256
from math import floor
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.renderer.export_timeline import ExportFrameSample, ExportTimelinePlanner
from app.renderer.ffmpeg_renderer import RenderFrame


def _pixel_signature(image: QImage) -> tuple[int, int, int, str]:
    """Return an exact, format-aware digest of every rendered pixel."""
    pixels = image.constBits()
    payload = bytes(pixels[:image.sizeInBytes()])
    return (
        image.width(), image.height(), image.format().value,
        sha256(payload).hexdigest(),
    )


def _expand_cfr(
    frames: list[RenderFrame], fps: int,
) -> list[tuple[int, int, int, str]]:
    """Expand variable-duration frames with the production rounding contract."""
    expanded: list[tuple[int, int, int, str]] = []
    duration = 0.0
    frame_count = 0
    for frame in frames:
        assert isinstance(frame.image, QImage)
        duration += frame.duration_seconds
        target_count = max(1, floor(duration * fps + 0.5))
        signature = _pixel_signature(frame.image)
        expanded.extend([signature] * max(0, target_count - frame_count))
        frame_count = target_count
    return expanded


def _expand_cfr_images(frames: list[RenderFrame], fps: int) -> list[QImage]:
    """Expand staged images with the same cumulative rounding as encoding."""
    expanded: list[QImage] = []
    duration = 0.0
    frame_count = 0
    for frame in frames:
        assert isinstance(frame.image, QImage)
        duration += frame.duration_seconds
        target_count = max(1, floor(duration * fps + 0.5))
        expanded.extend(
            frame.image.copy() for _ in range(target_count - frame_count)
        )
        frame_count = target_count
    return expanded


def _composite_streams(streams: list[list[QImage]]) -> list[QImage]:
    """Composite ordered Canvas streams exactly at their captured origin."""
    assert streams
    assert len({len(stream) for stream in streams}) == 1
    composed: list[QImage] = []
    for frame_index in range(len(streams[0])):
        image = streams[0][frame_index].copy()
        painter = QPainter(image)
        for stream in streams[1:]:
            painter.drawImage(0, 0, stream[frame_index])
        painter.end()
        composed.append(image)
    return composed


def _position_layer_frames(
    images: list[QImage], size: object, origin: tuple[int, int],
) -> list[QImage]:
    """Place cropped transparent frames at their Canvas stream origin."""
    positioned: list[QImage] = []
    for source in images:
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.drawImage(origin[0], origin[1], source)
        painter.end()
        positioned.append(image)
    return positioned


def _position_render_frames(
    frames: list[RenderFrame], size: object, origin: tuple[int, int],
) -> list[RenderFrame]:
    images = _position_layer_frames(
        [frame.image for frame in frames if isinstance(frame.image, QImage)],
        size,
        origin,
    )
    assert len(images) == len(frames)
    return [
        RenderFrame(image, frame.duration_seconds)
        for image, frame in zip(images, frames, strict=True)
    ]


class ExportFrameEquivalenceTests(unittest.TestCase):
    """Compare the optimized path with the former per-sample capture contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_subframe_captures_preserve_every_encoded_pixel_and_duration(self) -> None:
        scene = CanvasScene()
        scene.set_artboard_size(160, 90)
        scene.addItem(SourceItem(Source(
            SourceType.PROGRESS_BAR, "Progress", x=8, y=10,
            width=144, height=15, z_index=0,
        )))
        scene.addItem(SourceItem(Source(
            SourceType.TEXT, "Animated text", text="Test", x=8, y=35,
            width=144, height=40, font_size=22, z_index=2,
            animation_in="fade", animation_in_duration=1.0,
        )))
        track = PlaylistTrack("subframes.wav", "Subframes", duration_seconds=2.0)
        samples = []
        elapsed = 0.0
        # Mixed grids and very short lyric boundaries can produce zero CFR frames.
        for index in range(360):
            duration = (0.001, 1 / 120, 0.003, 1 / 60)[index % 4]
            samples.append(ExportFrameSample(
                track, 1, 0.0, duration, elapsed, elapsed,
                animation_phase="in", animation_progress=min(1.0, elapsed),
                animation_phase_duration=1.0,
            ))
            elapsed += duration
        bands = [(None, 0), (1, None)]
        for fps in (24, 30, 60):
            with self.subTest(fps=fps):
                results = []
                for output_fps in (None, fps):
                    frames = defaultdict(list)

                    def stage(image, seconds, key):
                        frame = RenderFrame(image.copy(), seconds)
                        frames[key].append(frame)
                        return frame

                    capturer = ExportCanvasCapturer(
                        scene, [track], elapsed, set(), bands, stage,
                        lambda: None, lambda *_args: None,
                        retain_static_frames=False, output_fps=output_fps,
                    )
                    with patch.object(CanvasSnapshot, "capture_track", wraps=CanvasSnapshot.capture_track) as capture:
                        for sample in samples:
                            for key in ("base", "layer:0"):
                                capturer.capture_stream(sample, key)
                        calls = capture.call_count
                    results.append(frames)
                    if output_fps is not None:
                        self.assertGreater(capturer.skipped_cfr_captures, 0)
                        self.assertEqual(calls + capturer.skipped_cfr_captures, len(samples) * 2)
                for key in ("base", "layer:0"):
                    self.assertEqual(
                        _expand_cfr(results[0][key], fps), _expand_cfr(results[1][key], fps),
                    )
                    self.assertEqual(
                        [frame.duration_seconds for frame in results[0][key]],
                        [frame.duration_seconds for frame in results[1][key]],
                    )

    def test_subframe_skip_still_checks_cancellation_and_reports_progress(self) -> None:
        scene = CanvasScene()
        track = PlaylistTrack("cancel.wav", "Cancel", duration_seconds=1.0)
        staged = []
        callbacks = []
        capturer = ExportCanvasCapturer(
            scene, [track], 1.0, set(), [(None, None)],
            lambda image, seconds, key: staged.append(seconds),
            lambda: None, lambda *_args: callbacks.append(True), output_fps=60,
        )
        sample = ExportFrameSample(track, 1, 0, 0.001, 0, 0)
        capturer.capture_stream(sample, "base")
        capturer.capture_stream(replace(sample, timeline_seconds=0.001), "base")
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(staged, [0.001, 0.001])
        with patch.object(capturer, "ensure_not_cancelled", side_effect=RuntimeError("cancelled")):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                capturer.capture_stream(sample, "base")

    def test_planned_animation_endpoints_reach_exact_canvas_alpha(self) -> None:
        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE,
            "Animated",
            animation_in="slide_left",
            animation_out="slide_right",
            animation_in_duration=0.5,
            animation_out_duration=0.5,
        )
        item = SourceItem(source)
        scene.addItem(item)
        track = PlaylistTrack(
            "animation.wav", "Animation", duration_seconds=2.0,
        )
        samples = ExportTimelinePlanner.build([track], [source], 60)
        entrance = [
            sample for sample in samples if sample.animation_phase == "in"
        ]
        exit_samples = [
            sample for sample in samples if sample.animation_phase == "out"
        ]
        observed: list[float] = []

        def inspect_alpha(*_args: object, **_kwargs: object) -> QImage:
            observed.append(item.opacity())
            return QImage(1, 1, QImage.Format.Format_ARGB32)

        with patch.object(CanvasSnapshot, "capture", side_effect=inspect_alpha):
            for sample in (
                entrance[0], entrance[-1], exit_samples[0], exit_samples[-1],
            ):
                CanvasSnapshot.capture_track(
                    scene,
                    track,
                    1,
                    1,
                    0.0,
                    animation_phase=sample.animation_phase,
                    animation_progress=sample.animation_progress,
                    elapsed_seconds=sample.elapsed_seconds,
                    timeline_seconds=sample.timeline_seconds,
                    animation_phase_duration=sample.animation_phase_duration,
                )

        self.assertEqual(observed, [0.0, 1.0, 1.0, 0.0])
        self.assertEqual(item.opacity(), 1.0)

    @staticmethod
    def _legacy_capture(
        scene: CanvasScene,
        track: PlaylistTrack,
        samples: list[ExportFrameSample],
        tracks: list[PlaylistTrack],
        dynamic_ids: set[str],
        band: tuple[float | None, float | None],
        *,
        transparent: bool,
    ) -> list[RenderFrame]:
        z_min, z_max = band
        duration = sum(item.duration_seconds for item in samples)
        return [
            RenderFrame(
                CanvasSnapshot.capture_track(
                    scene,
                    sample.track,
                    sample.track_number,
                    len(tracks),
                    sample.track_start_seconds,
                    elapsed_seconds=sample.elapsed_seconds,
                    hide_visualizers=dynamic_ids,
                    playlist_duration_seconds=duration,
                    playlist_tracks=tracks,
                    timeline_seconds=sample.timeline_seconds,
                    z_min=z_min,
                    z_max=z_max,
                    transparent=transparent,
                    animation_phase=sample.animation_phase,
                    animation_progress=sample.animation_progress,
                    animation_phase_duration=sample.animation_phase_duration,
                ),
                sample.duration_seconds,
            )
            for sample in samples
        ]

    def test_safe_cache_and_legacy_capture_have_identical_band_pixels(self) -> None:
        """A cached base and an uncached token layer must both remain exact."""
        scene = CanvasScene()
        scene.set_artboard_size(192, 108)
        base = Source(
            SourceType.SHAPE, "Base", x=0, y=0, width=192, height=108,
            fill_color="#17324D", z_index=0,
        )
        separator = Source(
            SourceType.AUDIO_VISUALIZER, "Reactive separator", z_index=1,
        )
        translucent = Source(
            SourceType.SHAPE, "Glass", x=12, y=12, width=168, height=84,
            fill_color="#F0A040", opacity=0.42, z_index=2,
        )
        clock = Source(
            SourceType.TEXT, "Clock", x=20, y=30, width=152, height=48,
            text="%title%", font_size=24, z_index=3,
        )
        for source in (base, separator, translucent, clock):
            scene.addItem(SourceItem(source))

        track = PlaylistTrack("first.wav", "First", duration_seconds=1.0)
        second_track = PlaylistTrack("second.wav", "Second", duration_seconds=1.0)
        tracks = [track, second_track]
        samples = [
            ExportFrameSample(track, 1, 0.0, 1.0, 0.0, 0.0),
            ExportFrameSample(second_track, 2, 1.0, 1.0, 0.0, 1.0),
        ]
        dynamic_ids = {separator.id}
        bands = CanvasSnapshot.z_bands(scene, dynamic_ids)
        self.assertEqual(len(bands), 2)

        legacy = {
            "base": self._legacy_capture(
                scene, track, samples, tracks, dynamic_ids, bands[0],
                transparent=False,
            ),
            "layer:0": self._legacy_capture(
                scene, track, samples, tracks, dynamic_ids, bands[1],
                transparent=True,
            ),
        }
        optimized: dict[str, list[RenderFrame]] = defaultdict(list)

        def stage(image: QImage, seconds: float, key: str) -> RenderFrame:
            frame = RenderFrame(image.copy(), seconds)
            optimized[key].append(frame)
            return frame

        capturer = ExportCanvasCapturer(
            scene, tracks, 2.0, dynamic_ids, bands, stage,
            lambda: None, lambda _track, _key: None,
        )
        self.assertEqual(capturer.invariant_stream_keys, {"base"})
        original_capture = CanvasSnapshot.capture_track
        with patch.object(
            CanvasSnapshot, "capture_track", side_effect=original_capture,
        ) as capture:
            for sample in samples:
                capturer.capture_stream(sample, "base")
                capturer.capture_stream(sample, "layer:0")

        # Former behavior rasterized 2 bands × 2 samples. The safe base now
        # rasterizes once, while the changing clock layer still rasterizes twice.
        self.assertEqual(capture.call_count, 3)
        for key in ("base", "layer:0"):
            actual_frames = optimized[key]
            if key != "base" and (
                actual_frames[0].image.size() != legacy[key][0].image.size()
            ):
                actual_frames = _position_render_frames(
                    actual_frames,
                    legacy[key][0].image.size(),
                    capturer.stream_origin(key),
                )
            self.assertEqual(
                [_pixel_signature(frame.image) for frame in actual_frames],
                [_pixel_signature(frame.image) for frame in legacy[key]],
                key,
            )
            self.assertEqual(
                [frame.duration_seconds for frame in optimized[key]],
                [frame.duration_seconds for frame in legacy[key]],
                key,
            )
        self.assertNotEqual(
            _pixel_signature(optimized["layer:0"][0].image),
            _pixel_signature(optimized["layer:0"][1].image),
        )

    def test_single_static_capture_expands_to_legacy_cfr_frame_sequence(self) -> None:
        """One full-duration safe frame must encode like all legacy segments."""
        scene = CanvasScene()
        scene.set_artboard_size(160, 90)
        scene.addItem(SourceItem(Source(
            SourceType.TEXT, "Title", x=8, y=18, width=144, height=54,
            text="Static playlist", font_size=22,
        )))
        track = PlaylistTrack("static.wav", "Static", duration_seconds=2.0)
        samples = [
            ExportFrameSample(track, 1, 0.0, 0.35, 0.0, 0.0),
            ExportFrameSample(track, 1, 0.0, 0.65, 0.35, 0.35),
            ExportFrameSample(track, 1, 0.0, 1.0, 1.0, 1.0),
        ]
        legacy = self._legacy_capture(
            scene, track, samples, [track], set(), (None, None),
            transparent=False,
        )
        optimized: list[RenderFrame] = []

        def stage(image: QImage, seconds: float, _key: str) -> RenderFrame:
            frame = RenderFrame(image.copy(), seconds)
            optimized.append(frame)
            return frame

        capturer = ExportCanvasCapturer(
            scene, [track], 2.0, set(), [(None, None)], stage,
            lambda: None, lambda _track, _key: None,
        )
        capturer.capture_invariant_stream(samples[0], "base", 2.0)

        self.assertEqual(len(optimized), 1)
        self.assertEqual(optimized[0].duration_seconds, 2.0)
        self.assertEqual(_expand_cfr(optimized, 30), _expand_cfr(legacy, 30))
        self.assertEqual(len(_expand_cfr(optimized, 30)), 60)

    def test_element_state_key_skips_equal_time_text_captures_exactly(self) -> None:
        """Equal resolved token text should extend a frame, not repaint it."""
        scene = CanvasScene()
        scene.set_artboard_size(160, 90)
        scene.addItem(SourceItem(Source(
            SourceType.TEXT, "Clock", x=8, y=18, width=144, height=54,
            text="Elapsed %current_time%", font_size=22,
        )))
        track = PlaylistTrack("clock.wav", "Clock", duration_seconds=1.5)
        samples = [
            ExportFrameSample(track, 1, 0.0, 0.2, 0.10, 0.10),
            ExportFrameSample(track, 1, 0.0, 0.3, 0.40, 0.40),
            ExportFrameSample(track, 1, 0.0, 0.5, 0.90, 0.90),
            ExportFrameSample(track, 1, 0.0, 0.5, 1.10, 1.10),
        ]
        legacy = self._legacy_capture(
            scene, track, samples, [track], set(), (None, None),
            transparent=False,
        )
        optimized: list[RenderFrame] = []

        def stage(image: QImage, seconds: float, _key: str) -> RenderFrame:
            frame = RenderFrame(image.copy(), seconds)
            optimized.append(frame)
            return frame

        capturer = ExportCanvasCapturer(
            scene, [track], 1.5, set(), [(None, None)], stage,
            lambda: None, lambda _track, _key: None,
        )
        coalesced = capturer.coalesce_samples(samples, "base")
        self.assertEqual(len(coalesced), 2)
        self.assertAlmostEqual(coalesced[0].duration_seconds, 1.0)
        original_capture = CanvasSnapshot.capture_track
        with patch.object(
            CanvasSnapshot, "capture_track", side_effect=original_capture,
        ) as capture:
            for sample in coalesced:
                capturer.capture_stream(sample, "base")

        self.assertEqual(capture.call_count, 2)
        self.assertEqual(_expand_cfr(optimized, 30), _expand_cfr(legacy, 30))
        self.assertEqual(len(_expand_cfr(optimized, 30)), 45)

    def test_element_state_key_preserves_lyric_highlight_boundaries(self) -> None:
        """Inactive lyric gaps may merge, but highlight changes must not."""
        scene = CanvasScene()
        lyric_source = Source(
            SourceType.LYRICS, "Lyrics", subtitle_animation="none",
        )
        scene.addItem(SourceItem(lyric_source))
        track = PlaylistTrack(
            "lyrics.wav", "Lyrics", duration_seconds=3.0,
            lyrics=[
                {"start": 0.0, "end": 0.5, "text": "First"},
                {"start": 2.0, "end": 3.0, "text": "Second"},
            ],
        )
        samples = [
            ExportFrameSample(track, 1, 0.0, 0.5, 0.25, 0.25),
            ExportFrameSample(track, 1, 0.0, 0.5, 0.75, 0.75),
            ExportFrameSample(track, 1, 0.0, 1.0, 1.25, 1.25),
            ExportFrameSample(track, 1, 0.0, 1.0, 2.25, 2.25),
        ]
        capturer = ExportCanvasCapturer(
            scene, [track], 3.0, set(), [(None, None)],
            lambda image, seconds, _key: RenderFrame(image, seconds),
            lambda: None, lambda _track, _key: None,
        )
        coalesced = capturer.coalesce_samples(samples, "base")

        self.assertEqual(len(coalesced), 3)
        self.assertAlmostEqual(coalesced[1].duration_seconds, 1.5)
        self.assertEqual(coalesced[0].elapsed_seconds, 0.25)
        self.assertEqual(coalesced[-1].elapsed_seconds, 2.25)

    def test_state_key_reduces_real_fractional_track_time_schedule(self) -> None:
        """Local/global second boundaries must not repaint equal clock text."""
        scene = CanvasScene()
        clock = Source(SourceType.TEXT, "Clock", text="%current_time%")
        scene.addItem(SourceItem(clock))
        track = PlaylistTrack(
            "clock.wav", "Clock", duration_seconds=10.0,
            start_time_seconds=0.37,
        )
        samples = ExportTimelinePlanner.build([track], [clock], 30)
        capturer = ExportCanvasCapturer(
            scene, [track], 10.37, set(), [(None, None)],
            lambda image, seconds, _key: RenderFrame(image, seconds),
            lambda: None, lambda _track, _key: None,
        )
        coalesced = capturer.coalesce_samples(samples, "base")

        self.assertEqual(len(samples), 21)
        self.assertEqual(len(coalesced), 10)
        self.assertAlmostEqual(
            sum(sample.duration_seconds for sample in coalesced),
            sum(sample.duration_seconds for sample in samples),
        )

    def test_dynamic_partial_region_matches_full_canvas_pixels(self) -> None:
        """A cropped dynamic paint must expand to the exact full layer image."""
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        source = Source(
            SourceType.TEXT, "Moving clock", x=104, y=62,
            width=112, height=42, text="%current_time%", font_size=22,
            rotation=7.0, z_index=1, animation_in="slide_right",
            animation_in_duration=1.0,
        )
        source.shadow.enabled = True
        source.shadow.offset_x = 7.0
        source.shadow.offset_y = 5.0
        source.shadow.blur_radius = 16.0
        item = SourceItem(source)
        scene.addItem(item)
        item.setSelected(True)
        track = PlaylistTrack("partial.wav", "Partial", duration_seconds=2.0)
        sample = ExportFrameSample(
            track, 1, 0.0, 0.5, 0.2, 0.2,
            animation_phase="in", animation_progress=0.2,
            animation_phase_duration=1.0,
        )
        common = {
            "elapsed_seconds": sample.elapsed_seconds,
            "timeline_seconds": sample.timeline_seconds,
            "playlist_duration_seconds": 2.0,
            "playlist_tracks": [track],
            "animation_phase": "in",
            "animation_progress": 0.2,
            "animation_phase_duration": 1.0,
            "z_min": 1.0,
            "z_max": None,
            "transparent": True,
        }
        full = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0, partial_render=False, **common,
        )
        staged: list[RenderFrame] = []

        def stage(image: QImage, seconds: float, _key: str) -> RenderFrame:
            frame = RenderFrame(image.copy(), seconds)
            staged.append(frame)
            return frame

        capturer = ExportCanvasCapturer(
            scene, [track], 2.0, set(),
            [(None, 0.0), (1.0, None)], stage,
            lambda: None, lambda _track, _key: None,
        )
        capturer.capture_stream(sample, "layer:0")

        self.assertEqual(len(staged), 1)
        origin_x, origin_y = capturer.stream_origin("layer:0")
        expanded = QImage(
            full.size(), QImage.Format.Format_ARGB32_Premultiplied,
        )
        expanded.fill(0)
        painter = QPainter(expanded)
        painter.drawImage(origin_x, origin_y, staged[0].image)
        painter.end()
        self.assertLess(staged[0].image.width(), full.width())
        self.assertLess(staged[0].image.height(), full.height())
        self.assertEqual(
            _pixel_signature(expanded), _pixel_signature(full),
        )
        self.assertEqual(capturer.partial_render_capture_count, 1)
        self.assertLess(
            capturer.scene_render_source_pixels,
            capturer.full_frame_source_pixels,
        )

    def test_partial_region_falls_back_for_base_and_near_full_layers(self) -> None:
        """Cropping must not add overhead where a partial pass cannot help."""
        scene = CanvasScene()
        scene.set_artboard_size(320, 180)
        scene.addItem(SourceItem(Source(
            SourceType.SHAPE, "Full overlay", x=0, y=0,
            width=320, height=180, z_index=1,
        )))
        track = PlaylistTrack("full.wav", "Full", duration_seconds=1.0)
        overlay_metrics: dict[str, object] = {}
        overlay = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0,
            elapsed_seconds=0.25, timeline_seconds=0.25,
            z_min=1.0, transparent=True, partial_render=True,
            render_metrics=overlay_metrics,
        )
        base_metrics: dict[str, object] = {}
        base = CanvasSnapshot.capture_track(
            scene, track, 1, 1, 0.0,
            elapsed_seconds=0.25, timeline_seconds=0.25,
            z_max=1.0, transparent=False, partial_render=True,
            render_metrics=base_metrics,
        )

        self.assertFalse(overlay_metrics["partial_render"])
        self.assertFalse(base_metrics["partial_render"])
        self.assertEqual((overlay.width(), overlay.height()), (320, 180))
        self.assertEqual((base.width(), base.height()), (320, 180))

    def test_independent_z_timelines_reduce_captures_without_changing_frames(self) -> None:
        """Changes in one Z band must not add samples to another band."""
        scene = CanvasScene()
        scene.set_artboard_size(192, 108)
        animated_base = Source(
            SourceType.SHAPE, "Animated base", x=0, y=0,
            width=192, height=108, fill_color="#172554", z_index=0,
            animation_in="fade", animation_in_duration=0.5,
        )
        separator = Source(
            SourceType.AUDIO_VISUALIZER, "Reactive separator", z_index=1,
        )
        clock = Source(
            SourceType.TEXT, "Clock", x=20, y=30, width=152, height=48,
            text="%current_time%", font_size=24, z_index=2,
        )
        sources = [animated_base, separator, clock]
        for source in sources:
            scene.addItem(SourceItem(source))
        track = PlaylistTrack("timeline.wav", "Timeline", duration_seconds=4.0)
        dynamic_ids = {separator.id}
        bands = CanvasSnapshot.z_bands(scene, dynamic_ids)
        legacy_samples = ExportTimelinePlanner.build([track], sources, 10)
        independent = ExportTimelinePlanner.build_by_z_band(
            [track], sources, dynamic_ids, bands, 10,
        )

        self.assertEqual(set(independent), {"base", "layer:0"})
        self.assertLess(
            sum(len(samples) for samples in independent.values()),
            len(legacy_samples) * len(bands),
        )
        for samples in independent.values():
            self.assertAlmostEqual(
                sum(sample.duration_seconds for sample in samples), 4.0,
            )

        legacy = {
            "base": self._legacy_capture(
                scene, track, legacy_samples, [track], dynamic_ids, bands[0],
                transparent=False,
            ),
            "layer:0": self._legacy_capture(
                scene, track, legacy_samples, [track], dynamic_ids, bands[1],
                transparent=True,
            ),
        }
        optimized: dict[str, list[RenderFrame]] = defaultdict(list)

        def stage(image: QImage, seconds: float, key: str) -> RenderFrame:
            frame = RenderFrame(image.copy(), seconds)
            optimized[key].append(frame)
            return frame

        capturer = ExportCanvasCapturer(
            scene, [track], 4.0, dynamic_ids, bands, stage,
            lambda: None, lambda _track, _key: None,
        )
        for key, samples in independent.items():
            for sample in samples:
                capturer.capture_stream(sample, key)

        for key in ("base", "layer:0"):
            actual_frames = optimized[key]
            if key != "base" and (
                actual_frames[0].image.size() != legacy[key][0].image.size()
            ):
                actual_frames = _position_render_frames(
                    actual_frames,
                    legacy[key][0].image.size(),
                    capturer.stream_origin(key),
                )
            self.assertEqual(
                _expand_cfr(actual_frames, 30),
                _expand_cfr(legacy[key], 30),
                key,
            )
            self.assertEqual(len(_expand_cfr(optimized[key], 30)), 120)

    def test_mixed_static_dynamic_sublayers_preserve_final_composed_pixels(self) -> None:
        """Splitting a mixed band must preserve its final alpha composition."""
        scene = CanvasScene()
        scene.set_artboard_size(192, 108)
        sources = [
            Source(
                SourceType.SHAPE, "Static background", x=0, y=0,
                width=192, height=108, fill_color="#13243A", z_index=0,
            ),
            Source(
                SourceType.TEXT, "Clock", x=12, y=18, width=168, height=34,
                text="%current_time%", font_size=20, z_index=1,
            ),
            Source(
                SourceType.SHAPE, "Static glass", x=24, y=56,
                width=144, height=34, fill_color="#EBAF55", opacity=0.38,
                z_index=2,
            ),
            Source(
                SourceType.AUDIO_VISUALIZER, "External separator", z_index=3,
            ),
            Source(
                SourceType.SHAPE, "Static foreground", x=0, y=0,
                width=12, height=108, fill_color="#42D3A5", opacity=0.7,
                z_index=4,
            ),
        ]
        for source in sources:
            scene.addItem(SourceItem(source))

        track = PlaylistTrack("mixed.wav", "Mixed", duration_seconds=4.0)
        dynamic_ids = {sources[3].id}
        broad_bands = CanvasSnapshot.z_bands(scene, dynamic_ids)
        split_bands = CanvasSnapshot.split_mixed_capture_bands(
            scene, dynamic_ids, broad_bands, 4.0,
        )
        self.assertEqual(len(broad_bands), 2)
        self.assertGreater(len(split_bands), len(broad_bands))

        legacy_samples = ExportTimelinePlanner.build([track], sources, 10)
        legacy_frames = [
            self._legacy_capture(
                scene, track, legacy_samples, [track], dynamic_ids, band,
                transparent=band_index > 0,
            )
            for band_index, band in enumerate(broad_bands)
        ]
        legacy_composed = _composite_streams([
            _expand_cfr_images(frames, 30) for frames in legacy_frames
        ])

        independent = ExportTimelinePlanner.build_by_z_band(
            [track], sources, dynamic_ids, split_bands, 10,
        )
        optimized: dict[str, list[RenderFrame]] = defaultdict(list)

        def stage(image: QImage, seconds: float, key: str) -> RenderFrame:
            frame = RenderFrame(image.copy(), seconds)
            optimized[key].append(frame)
            return frame

        capturer = ExportCanvasCapturer(
            scene, [track], 4.0, dynamic_ids, split_bands, stage,
            lambda: None, lambda _track, _key: None,
        )
        for key, samples in independent.items():
            if key in capturer.invariant_stream_keys:
                capturer.capture_invariant_stream(samples[0], key, 4.0)
            else:
                for sample in samples:
                    capturer.capture_stream(sample, key)

        ordered_keys = ["base", *(
            f"layer:{index}" for index in range(len(split_bands) - 1)
        )]
        optimized_streams: list[list[QImage]] = []
        for key in ordered_keys:
            images = _expand_cfr_images(optimized[key], 30)
            if key != "base" and images[0].size() != legacy_composed[0].size():
                images = _position_layer_frames(
                    images, legacy_composed[0].size(),
                    capturer.stream_origin(key),
                )
            optimized_streams.append(images)
        optimized_composed = _composite_streams(optimized_streams)

        self.assertEqual(len(legacy_composed), 120)
        self.assertEqual(len(optimized_composed), 120)
        self.assertEqual(
            [_pixel_signature(image) for image in optimized_composed],
            [_pixel_signature(image) for image in legacy_composed],
        )
        self.assertLess(
            sum(
                1 if key in capturer.invariant_stream_keys else len(samples)
                for key, samples in independent.items()
            ),
            len(legacy_samples) * len(broad_bands),
        )

    def test_output_scale_rasterizes_canvas_at_final_resolution(self) -> None:
        """A >1 output scale must render the vector scene larger, not upscale it."""
        scene = CanvasScene()
        scene.set_artboard_size(160, 90)
        scene.addItem(SourceItem(Source(
            SourceType.TEXT, "Title", x=8, y=18, width=144, height=54,
            text="Static playlist", font_size=22,
        )))
        track = PlaylistTrack("scale.wav", "Scale", duration_seconds=1.0)
        sample = ExportFrameSample(track, 1, 0.0, 1.0, 0.0, 0.0)
        staged: list[RenderFrame] = []

        def stage(image: QImage, seconds: float, _key: str) -> RenderFrame:
            frame = RenderFrame(image.copy(), seconds)
            staged.append(frame)
            return frame

        capturer = ExportCanvasCapturer(
            scene, [track], 1.0, set(), [(None, None)], stage,
            lambda: None, lambda _track, _key: None, output_scale=2.0,
        )
        capturer.capture_stream(sample, "base")

        self.assertEqual(capturer.output_scale, 2.0)
        self.assertEqual(
            (staged[0].image.width(), staged[0].image.height()), (320, 180),
        )

    def test_small_canvas_capture_does_not_apply_a_low_resolution_cap(self) -> None:
        scene = CanvasScene()
        scene.set_artboard_size(64, 64)
        scene.addItem(SourceItem(Source(
            SourceType.TEXT, "Tiny canvas", text="4K title", width=64, height=64,
        )))

        captured = CanvasSnapshot.capture(scene, output_scale=9.0)

        self.assertEqual((captured.width(), captured.height()), (576, 576))

    def test_animated_source_remains_on_pixel_identical_legacy_path(self) -> None:
        """Animation samples must never be collapsed by the safe-frame cache."""
        scene = CanvasScene()
        scene.set_artboard_size(160, 90)
        scene.addItem(SourceItem(Source(
            SourceType.SHAPE, "Animated", x=30, y=18, width=100, height=54,
            fill_color="#38BDF8", animation_in="fade",
            animation_in_duration=1.0,
        )))
        track = PlaylistTrack("animated.wav", "Animated", duration_seconds=1.0)
        samples = [
            ExportFrameSample(
                track, 1, 0.0, 0.5, 0.0, 0.0,
                animation_phase="in", animation_progress=0.0,
                animation_phase_duration=1.0,
            ),
            ExportFrameSample(
                track, 1, 0.0, 0.5, 0.5, 0.5,
                animation_phase="in", animation_progress=0.5,
                animation_phase_duration=1.0,
            ),
        ]
        legacy = self._legacy_capture(
            scene, track, samples, [track], set(), (None, None),
            transparent=False,
        )
        optimized: list[RenderFrame] = []

        def stage(image: QImage, seconds: float, _key: str) -> RenderFrame:
            frame = RenderFrame(image.copy(), seconds)
            optimized.append(frame)
            return frame

        capturer = ExportCanvasCapturer(
            scene, [track], 1.0, set(), [(None, None)], stage,
            lambda: None, lambda _track, _key: None,
        )
        self.assertEqual(capturer.invariant_stream_keys, set())
        self.assertEqual(len(capturer.coalesce_samples(samples, "base")), 2)
        original_capture = CanvasSnapshot.capture_track
        with patch.object(
            CanvasSnapshot, "capture_track", side_effect=original_capture,
        ) as capture:
            for sample in samples:
                capturer.capture_stream(sample, "base")

        self.assertEqual(capture.call_count, len(samples))
        self.assertEqual(
            [_pixel_signature(frame.image) for frame in optimized],
            [_pixel_signature(frame.image) for frame in legacy],
        )
        self.assertNotEqual(
            _pixel_signature(optimized[0].image),
            _pixel_signature(optimized[-1].image),
        )

    def test_timeline_window_animation_changes_export_cache_key(self) -> None:
        """A source with its own timeline window must not be coalesced across
        that window's entrance animation just because the track-level
        animation_phase (which the window is independent of) stays None."""
        scene = CanvasScene()
        source = Source(
            SourceType.SHAPE, "Windowed", timeline_start=1.0,
            animation_in="slide_left", animation_in_duration=1.0,
        )
        scene.addItem(SourceItem(source))
        track = PlaylistTrack("windowed.wav", "Windowed", duration_seconds=5.0)
        capturer = ExportCanvasCapturer(
            scene, [track], 5.0, set(), [(None, None)],
            lambda image, seconds, _key: RenderFrame(image, seconds),
            lambda: None, lambda _track, _key: None,
        )
        # Both samples sit inside the window's [1.0, 2.0) entrance animation
        # at different progress; sample.animation_phase is None throughout
        # because this is a per-source window, not a track-level gap.
        just_started = ExportFrameSample(track, 1, 0.0, 0.5, 1.0, 1.0)
        halfway = ExportFrameSample(track, 1, 0.0, 0.5, 1.5, 1.5)
        self.assertIsNone(just_started.animation_phase)
        self.assertIsNone(halfway.animation_phase)

        key_start = capturer._source_state_key(source, just_started)
        key_halfway = capturer._source_state_key(source, halfway)

        self.assertIsNotNone(key_start)
        self.assertNotEqual(key_start, key_halfway)


if __name__ == "__main__":
    unittest.main()
