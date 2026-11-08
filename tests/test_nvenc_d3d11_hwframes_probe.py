"""Safety and command contracts of D3D11 hardware-frame NVENC capability probe."""

import json
import unittest
from pathlib import Path

from tools.benchmark_nvenc_d3d11_hwframes import (
    build_encode_command,
    build_probe_command,
    inspect_video,
)


class D3D11HardwareFramesProbeTests(unittest.TestCase):
    def test_d3d11_hardware_frame_pipeline_has_no_cpu_readback(self):
        command = build_encode_command(
            "ffmpeg", 1920, 1080, 60, 120,
            Path("probe.mp4"), d3d11=True, adapter_index=0,
        )
        text = " ".join(command)
        self.assertIn("-init_hw_device d3d11va=mms:0", text)
        self.assertIn("-filter_hw_device mms", text)
        self.assertIn("-vf format=nv12,hwupload", text)
        self.assertIn("-c:v h264_nvenc", text)
        self.assertIn("-frames:v 120", text)
        self.assertNotIn("hwdownload", text)
        self.assertNotIn("h264_cuvid", text)
        self.assertEqual(command[-1], "probe.mp4")

    def test_same_input_source_and_encoder_between_modes(self):
        software = build_encode_command(
            "ffmpeg", 640, 360, 30, 60,
            Path("ref.mp4"), d3d11=False, adapter_index=0,
        )
        d3d11 = build_encode_command(
            "ffmpeg", 640, 360, 30, 60,
            Path("hw.mp4"), d3d11=True, adapter_index=0,
        )
        for option in ("-i", "-c:v", "-frames:v", "-preset", "-cq"):
            self.assertEqual(
                software[software.index(option) + 1],
                d3d11[d3d11.index(option) + 1],
            )
        self.assertNotIn("-init_hw_device", software)
        self.assertNotIn("hwupload", " ".join(software))

    def test_ffprobe_checks_actual_decoded_frame_count_and_codec(self):
        payload = json.dumps({"streams": [{
            "codec_name": "h264", "width": 1920, "height": 1080,
            "nb_read_frames": "120", "pix_fmt": "yuv420p",
            "avg_frame_rate": "60/1", "color_primaries": "bt709",
            "color_space": "bt709",
        }]})
        result = inspect_video(
            payload, width=1920, height=1080, frames=120, fps=60,
        )
        self.assertEqual(result["decoded_frames"], 120)
        self.assertEqual(result["codec"], "h264")
        self.assertEqual(result["fps"], 60)
        self.assertEqual(result["color_primaries"], "bt709")

    def test_rejects_truncated_output_wrong_codec_or_bad_rate(self):
        valid = {
            "codec_name": "h264", "width": 1920, "height": 1080,
            "nb_read_frames": "120", "avg_frame_rate": "60/1",
        }
        for field, replacement in (
            ("codec_name", "hevc"),
            ("width", 1280),
            ("nb_read_frames", "119"),
            ("avg_frame_rate", "30000/1001"),
            ("avg_frame_rate", "garbled"),
        ):
            broken = dict(valid)
            broken[field] = replacement
            with self.subTest(field=field, replacement=replacement):
                with self.assertRaises((ValueError, TypeError)):
                    inspect_video(
                        json.dumps({"streams": [broken]}),
                        width=1920, height=1080, frames=120, fps=60,
                    )
        with self.assertRaises(ValueError):
            inspect_video(
                '{"streams": []}', width=1920, height=1080,
                frames=120, fps=60,
            )

    def test_ffprobe_counts_decoded_frames_not_only_container_metadata(self):
        command = build_probe_command("ffprobe", Path("sample.mp4"))
        self.assertIn("-count_frames", command)
        self.assertIn("-show_entries", command)
        self.assertIn("nb_read_frames", " ".join(command))
        self.assertEqual(command[-1], "sample.mp4")


if __name__ == "__main__":
    unittest.main()
