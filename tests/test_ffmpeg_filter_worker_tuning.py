"""Safety and rollback contracts for targeted FFmpeg filter thread tuning."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.renderer.ffmpeg_renderer import (
    FFmpegRenderer,
    RenderSettings,
    WORK_MODE_STABLE,
    WORK_MODE_AUTO,
    WORK_MODE_MAX_SPEED,
)


class FfmpegFilterWorkerTuningTests(unittest.TestCase):
    @staticmethod
    def count(settings: RenderSettings, **overrides) -> int:
        context = dict(
            piped=True,
            base_is_cfr=True,
            static_input_kinds=("pipe", "pipe"),
            has_dynamic_overlays=False,
        )
        context.update(overrides)
        return FFmpegRenderer._final_filter_worker_count(settings, **context)

    def setUp(self):
        self.settings = RenderSettings(
            fps=30, output_width=1920, output_height=1080,
            work_mode=WORK_MODE_AUTO, video_codec="libx264", preset="medium",
        )

    def test_matching_live_multilayer_x264_uses_at_most_two_workers(self):
        with patch("app.renderer.ffmpeg_renderer.os.cpu_count", return_value=20), patch.dict(
            os.environ, {"PLAYLIST_CANVAS_DISABLE_FFMPEG_FILTER_WORKER_TUNING": "0"},
        ):
            self.assertEqual(FFmpegRenderer._filter_worker_count(self.settings), 8)
            self.assertEqual(self.count(self.settings), 2)
            self.assertEqual(self.count(RenderSettings(
                fps=30, output_width=1920, output_height=1080,
                work_mode=WORK_MODE_MAX_SPEED, video_codec="libx264",
            )), 2)

    def test_stable_mode_and_low_cpu_never_allocate_extra_workers(self):
        with patch("app.renderer.ffmpeg_renderer.os.cpu_count", return_value=20):
            stable = RenderSettings(
                fps=30, output_width=1920, output_height=1080,
                work_mode=WORK_MODE_STABLE, video_codec="libx264",
            )
            self.assertEqual(self.count(stable), 1)
        with patch("app.renderer.ffmpeg_renderer.os.cpu_count", return_value=2):
            self.assertEqual(self.count(self.settings), 2)

    def test_opt_out_restores_exact_previous_worker_selection(self):
        with patch("app.renderer.ffmpeg_renderer.os.cpu_count", return_value=20):
            for disabled in ("1", "TRUE", "yes"):
                with self.subTest(disabled=disabled), patch.dict(os.environ, {
                    "PLAYLIST_CANVAS_DISABLE_FFMPEG_FILTER_WORKER_TUNING": disabled,
                }):
                    self.assertEqual(
                        self.count(self.settings),
                        FFmpegRenderer._filter_worker_count(self.settings),
                    )

    def test_unmeasured_cases_retain_original_worker_policy(self):
        settings = self.settings
        variations = (
            (settings, {"piped": False}),
            (settings, {"base_is_cfr": False}),
            (settings, {"static_input_kinds": ("pipe",)}),
            (settings, {"static_input_kinds": ("concat", "pipe")}),
            (settings, {"has_dynamic_overlays": True}),
            (RenderSettings(fps=60, output_width=1920, output_height=1080), {}),
            (RenderSettings(fps=30, output_width=3840, output_height=2160), {}),
            (RenderSettings(fps=30, output_width=1920, output_height=1080,
                            video_codec="h264_nvenc"), {}),
            (RenderSettings(fps=30, output_width=1920, output_height=1080,
                            video_codec="libx265"), {}),
        )
        with patch("app.renderer.ffmpeg_renderer.os.cpu_count", return_value=20), patch.dict(
            os.environ, {"PLAYLIST_CANVAS_DISABLE_FFMPEG_FILTER_WORKER_TUNING": "0"},
        ):
            for case_settings, inputs in variations:
                with self.subTest(codec=case_settings.video_codec, fps=case_settings.fps,
                                  size=case_settings.output_width, extra=inputs):
                    self.assertEqual(
                        self.count(case_settings, **inputs),
                        FFmpegRenderer._filter_worker_count(case_settings),
                    )


if __name__ == "__main__":
    unittest.main()
