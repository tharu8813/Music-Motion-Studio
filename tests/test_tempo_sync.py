"""Tempo controls and playback use the same saved loop settings."""

import os
from threading import Event
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.animation.curves import source_loop_pose
from app.automix.models import TrackAnalysis
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.inspector.source_inspector import SourceInspector
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.preview.audio_analysis_worker import AudioAnalysisWorker
from app.preview.canvas_snapshot import CanvasSnapshot
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.preview.native_canvas_layer_packets import capture_native_canvas_track_frame, straight_bgra_bytes
from app.renderer.export_timeline import ExportFrameSample
from app.renderer.ffmpeg_renderer import RenderFrame
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.services.source_store import SourceStore
from app.services.track_tempo_service import prepare_track_tempos, track_tempo
from app.utils.i18n import Translator


class TempoSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_inspector_enables_sync_for_supported_motion_and_saves_settings(self):
        store = SourceStore()
        inspector = SourceInspector(store, Translator())
        self.addCleanup(inspector.deleteLater)
        source = Source(SourceType.SHAPE, "Shape")
        store.add(source)
        widgets = inspector.motion.widgets
        sync = widgets["loop_tempo_sync"]
        self.assertFalse(sync.isEnabled())
        loop = widgets["loop_motion"]
        loop.setCurrentIndex(loop.findData("pulse"))
        self.assertTrue(sync.isEnabled())
        self.assertFalse(inspector._field_visibility["loop_tempo_beats"])
        sync.click()
        self.assertTrue(inspector._field_visibility["loop_tempo_beats"])
        self.assertTrue(inspector._field_visibility["loop_tempo_offset"])
        self.assertFalse(inspector._field_visibility["loop_motion_period"])
        beats = widgets["loop_tempo_beats"]
        beats.setCurrentIndex(beats.findData(4.0))
        widgets["loop_tempo_offset"].setValue(-0.15)
        saved = Source.from_dict(source.to_dict())
        self.assertTrue(saved.uses_tempo_sync)
        self.assertEqual((saved.loop_tempo_beats, saved.loop_tempo_offset), (4, -0.15))
        loop.setCurrentIndex(loop.findData("float"))
        self.assertFalse(sync.isEnabled())
        self.assertFalse(sync.isChecked())
        self.assertTrue(inspector._field_visibility["loop_motion_period"])
        self.assertFalse(inspector._field_visibility["loop_tempo_offset"])
        video = Source(SourceType.VIDEO, "Video", loop_motion="pulse", loop_tempo_sync=True)
        store.add(video)
        self.assertFalse(sync.isEnabled())
        self.assertFalse(video.uses_tempo_sync)
        legacy = {key: value for key, value in saved.to_dict().items() if not key.startswith("loop_tempo_")}
        self.assertFalse(Source.from_dict(legacy).uses_tempo_sync)

    def test_phase_follows_source_time_and_detected_beats_with_offset(self):
        tempo = track_tempo(TrackAnalysis("track", "song.wav", 4, bpm=120, bpm_confidence=0.9,
                                          beats=(0.2, 0.7, 0.7, 1.3, 1.8, 2.2)))
        source = Source(SourceType.SHAPE, "Beat", loop_motion="spin", loop_tempo_sync=True,
                        loop_tempo_beats=2, loop_tempo_offset=0.1)
        self.assertEqual(source_loop_pose(source, 10, 1.4, tempo).rotation, 0)
        self.assertAlmostEqual(source_loop_pose(source, 50, 1.7, tempo).rotation, 108)
        self.assertEqual(source_loop_pose(source, 0.5, 1.7, None).rotation, 45)
        source.loop_tempo_sync = False
        self.assertEqual(source_loop_pose(source, 0.5, 1.7, tempo).rotation, 45)
        self.assertIsNone(track_tempo(TrackAnalysis("silent", "silent.wav", 2)))

    def test_preview_export_and_native_canvas_share_the_tempo_pose(self):
        track = PlaylistTrack("", "Song", duration_seconds=4)
        source = Source(SourceType.SHAPE, "Beat", x=32, y=24, width=80, height=50, fill_color="#FFFFFF",
                        loop_motion="spin", loop_tempo_sync=True)
        tempo = track_tempo(TrackAnalysis(track.id, "song.wav", 4, bpm=120, bpm_confidence=0.9,
                                          beats=(0.2, 0.7, 1.2, 1.7)))
        scene = CanvasScene()
        scene.set_artboard_size(160, 120)
        scene.addItem(SourceItem(source))
        preview = ExportPreviewDialog(scene, [track], Translator(), preferred_backend="cpu")
        self.addCleanup(preview.deleteLater)
        self.addCleanup(preview.close)
        preview.timeline.setValue(round(0.2 * preview.timeline.maximum() / track.duration_seconds))
        preview.refresh_preview()
        fallback = preview._image.copy()
        preview._store_track_levels(track.id, preview.preview_fps, {"tempo": tempo})
        preview.refresh_preview()
        self.assertNotEqual(preview._image, fallback)

        expected = CanvasSnapshot.capture_track(scene, track, 1, 1, 0, elapsed_seconds=0.2, track_tempo=tempo)
        capturer = ExportCanvasCapturer(
            scene, [track], 4, set(), [(None, None)], lambda image, seconds, _key: RenderFrame(image, seconds),
            lambda: None, lambda *_: None, track_tempos={track.id: tempo},
        )
        sample = ExportFrameSample(track, 1, 0, 0.1, 0.2, 0.2)
        self.assertEqual(capturer.capture_stream(sample, "base").image, expected)
        native = capture_native_canvas_track_frame(
            scene, track, [(None, None)], elapsed_seconds=0.2, track_tempos={track.id: tempo},
        )
        self.assertEqual(native.layers[0].pixels, straight_bgra_bytes(expected))

    def test_tempo_only_preview_skips_fft_and_export_prepares_only_when_needed(self):
        track = PlaylistTrack("song.wav", "Song", duration_seconds=4)
        tempo = track_tempo(TrackAnalysis(track.id, track.file_path, 4, bpm=120, bpm_confidence=0.9))
        source = Source(SourceType.SHAPE, "Beat", loop_motion="pulse")
        with patch("app.services.track_tempo_service.analyze_track_tempos", return_value={track.id: tempo}) as analyze:
            self.assertEqual(prepare_track_tempos([track], [source], "ffmpeg", Event()), {})
            analyze.assert_not_called()
            source.loop_tempo_sync = True
            self.assertEqual(prepare_track_tempos([track], [source], "ffmpeg", Event()), {track.id: tempo})
            analyze.assert_called_once()
        worker = AudioAnalysisWorker(PythonVisualizerRenderer(None), track, 24, needs_tempo=True, needs_levels=False)
        payloads = []
        worker.ready.connect(lambda _track_id, _fps, payload: payloads.append(payload))
        with (patch("app.preview.audio_analysis_worker.analyze_track_tempos", return_value={track.id: tempo}),
              patch.object(worker, "_analyze_audio") as fft):
            worker.run()
            fft.assert_not_called()
        self.assertEqual(payloads[0]["tempo"], tempo)
