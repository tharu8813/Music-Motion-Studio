"""Real pipe and process lifecycle checks without requiring an FFmpeg install."""

import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from app.ffmpeg.process import (
    FFmpegProcessCancelled, FFmpegProcessError, parse_progress_seconds, run_ffmpeg,
)


class FFmpegProcessTests(unittest.TestCase):
    def run_child(self, script, **kwargs):
        processes = []
        popen = subprocess.Popen

        def spawn(*args, **options):
            process = popen(*args, **options)
            processes.append(process)
            return process

        try:
            with patch("app.ffmpeg.process.subprocess.Popen", side_effect=spawn):
                run_ffmpeg([sys.executable, "-u", "-c", script], **kwargs)
        finally:
            for process in processes:
                self.assertIsNotNone(process.poll(), "child must be reaped on every exit path")
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)

    def test_drains_both_pipes_and_bounds_diagnostics(self):
        progress = []
        diagnostics = []
        analysis = []
        self.run_child(
            "import sys\nfor n in range(3000):\n print('out_time_us=' + str(n))\n"
            " print('diagnostic-' + str(n), file=sys.stderr)",
            progress_parser=progress.append, capture_stderr=diagnostics, stderr_parser=analysis.append,
        )
        self.assertEqual(len(progress), 3000)
        self.assertEqual(len(diagnostics), 400)
        self.assertEqual(diagnostics[0], "diagnostic-2600\n")
        self.assertEqual(diagnostics[-1], "diagnostic-2999\n")
        self.assertEqual(len(analysis), 3000)
        self.assertEqual(analysis[0], "diagnostic-0\n")
        self.assertEqual(analysis[-1], "diagnostic-2999\n")

    def test_cancels_a_process_that_stops_writing_progress(self):
        cancel = threading.Event()
        with self.assertRaises(FFmpegProcessCancelled):
            self.run_child(
                "import time; print('ready'); time.sleep(30)",
                progress_parser=lambda _line: cancel.set(), cancel_event=cancel,
            )

    def test_pre_cancelled_work_does_not_spawn(self):
        cancel = threading.Event()
        cancel.set()
        with patch("app.ffmpeg.process.subprocess.Popen") as spawn:
            with self.assertRaises(FFmpegProcessCancelled):
                run_ffmpeg([sys.executable], cancel_event=cancel)
        spawn.assert_not_called()

    def test_progress_failure_reaches_caller_and_stops_child(self):
        def broken_progress(_line):
            raise ValueError("broken callback")

        with self.assertRaisesRegex(FFmpegProcessError, "broken callback") as caught:
            self.run_child(
                "import time; print('ready'); time.sleep(30)",
                progress_parser=broken_progress,
            )
        self.assertIsInstance(caught.exception.__cause__, ValueError)

    def test_nonzero_exit_keeps_stderr_for_caller(self):
        diagnostics = []
        with self.assertRaisesRegex(FFmpegProcessError, "bad input"):
            self.run_child(
                "import sys; print('bad input', file=sys.stderr); sys.exit(7)",
                capture_stderr=diagnostics,
            )
        self.assertEqual(diagnostics, ["bad input\n"])

    def test_start_failure_has_context(self):
        with patch("app.ffmpeg.process.subprocess.Popen", side_effect=OSError("missing")):
            with self.assertRaisesRegex(FFmpegProcessError, "Could not start FFmpeg: missing"):
                run_ffmpeg(["missing-ffmpeg"])

    def test_progress_formats(self):
        for line in ("out_time_us=1500000", "out_time_ms=1500000", "out_time=00:00:01.5"):
            self.assertEqual(parse_progress_seconds(line), 1.5)
        for line in ("out_time_us=N/A", "out_time=N/A", "progress=end", "out_time=0:0:bad"):
            self.assertIsNone(parse_progress_seconds(line))


if __name__ == "__main__":
    unittest.main()
