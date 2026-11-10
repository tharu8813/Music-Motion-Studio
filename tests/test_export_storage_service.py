from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.services.export_storage_service import (
    ExportStorageMonitor,
    estimate_export_storage,
    format_bytes,
)


class ExportStorageServiceTests(unittest.TestCase):
    def test_estimate_scales_with_resolution_fps_duration_and_layers(self) -> None:
        small = estimate_export_storage(1280, 720, 30, 60, 18, "192k", 1)
        large = estimate_export_storage(3840, 2160, 60, 120, 18, "320k", 3)

        self.assertGreater(large.visual_files, small.visual_files)
        self.assertGreater(large.result_low, small.result_low)
        self.assertGreaterEqual(large.result_high, large.result_low)
        self.assertGreater(large.peak_temporary, large.result_high)
        self.assertIn("GB", format_bytes(5 * 1024**3))

    def test_native_gpu_audio_h264_and_mux_directory_grow_without_double_counting(self) -> None:
        with TemporaryDirectory(prefix="mms-native-storage-test-") as raw:
            root = Path(raw)
            audio = root / "audio"
            render = root / "render"
            mux = root / "mux"
            audio.mkdir()
            render.mkdir()
            mux.mkdir()
            (audio / "playlist.wav").write_bytes(b"a" * 51)
            (render / "complete.h264.part").write_bytes(b"h" * 175)
            (mux / ".export.mp4.part").write_bytes(b"v" * 40)

            monitor = ExportStorageMonitor(root / "output.mp4")
            monitor.set_path("audio", audio)
            monitor.set_path("render", render)
            monitor.set_path("output", mux)
            first = monitor._snapshot()
            self.assertEqual(first.categories["audio"], 51)
            self.assertEqual(first.categories["visuals"], 0)
            self.assertEqual(first.categories["processing"], 175)
            self.assertEqual(first.output_in_progress, 40)
            self.assertEqual(first.temporary_total, 51 + 175 + 40)
            (render / "complete.h264.part").write_bytes(b"h" * 260)
            second = monitor._snapshot()
            self.assertEqual(second.categories["processing"], 260)
            self.assertEqual(second.temporary_total, 51 + 260 + 40)

    def test_snapshot_classifies_all_export_owned_files_without_double_counting(self) -> None:
        with TemporaryDirectory(prefix="pc-storage-test-") as raw:
            root = Path(raw)
            frames = root / "frames"
            render = root / "render"
            frames.mkdir()
            render.mkdir()
            (frames / "canvas-base.mkv").write_bytes(b"v" * 11)
            (render / "track_0001.nut").write_bytes(b"a" * 13)
            (render / "python_visualizer_00.mov").write_bytes(b"e" * 17)
            (render / "metadata.ffmeta").write_bytes(b"m" * 19)
            output = root / ".movie-rendering.mp4"
            output.write_bytes(b"o" * 23)

            monitor = ExportStorageMonitor(root / "movie.mp4")
            monitor.set_path("frames", frames)
            monitor.set_path("render", render)
            monitor.set_path("output", output)
            snapshot = monitor._snapshot()

            self.assertEqual(snapshot.categories["visuals"], 11)
            self.assertEqual(snapshot.categories["audio"], 13)
            self.assertEqual(snapshot.categories["effects"], 17)
            self.assertEqual(snapshot.categories["processing"], 19)
            self.assertEqual(snapshot.output_in_progress, 23)
            self.assertEqual(snapshot.temporary_total, 83)


if __name__ == "__main__":
    unittest.main()
