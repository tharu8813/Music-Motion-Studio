"""Validate hardware composition feasibility commands without requiring a GPU."""

import unittest
from unittest.mock import patch

from tools.benchmark_nvenc_cuda_overlay import ffmpeg_chain, run_once


class NvencCudaOverlayProbeTests(unittest.TestCase):
    def test_gpu_compositor_uses_cuda_hwframes_and_nvenc(self):
        args = ffmpeg_chain(
            "ffmpeg", 1920, 1080, 60, 60, gpu=True,
        )
        command = " ".join(args)
        self.assertIn("-init_hw_device cuda=mms:0", command)
        self.assertIn("-filter_hw_device mms", command)
        self.assertIn("format=yuv420p,hwupload", command)
        self.assertIn("format=yuva420p,hwupload", command)
        self.assertIn("overlay_cuda=64:48", command)
        self.assertIn("-c:v h264_nvenc", command)
        self.assertNotIn("hwdownload", command)

    def test_reference_cpu_compositor_keeps_same_video_and_nvenc(self):
        cpu = ffmpeg_chain("ffmpeg", 1920, 1080, 60, 60, gpu=False)
        gpu = ffmpeg_chain("ffmpeg", 1920, 1080, 60, 60, gpu=True)
        self.assertNotIn("-init_hw_device", cpu)
        self.assertEqual(
            cpu[cpu.index("-c:v") + 1],
            gpu[gpu.index("-c:v") + 1],
        )
        self.assertEqual(
            cpu[cpu.index("-frames:v") + 1],
            gpu[gpu.index("-frames:v") + 1],
        )
        self.assertIn("overlay=64:48", " ".join(cpu))

    def test_missing_executable_is_reported_as_unsupported(self):
        result = run_once(["definitely_not_a_real_ffmpeg_executable_482"])
        self.assertFalse(result["ok"])
        self.assertIsNone(result["seconds"])
        self.assertTrue(result["error"])


if __name__ == "__main__":
    unittest.main()
