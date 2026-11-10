"""Real child-process regression checks; no Qt or GPU dependency."""
from concurrent.futures import CancelledError
from pathlib import Path
import subprocess
import sys
from threading import Event, Timer
from time import monotonic
import unittest
from unittest.mock import patch

from app.utils.cancellable_subprocess import run_probe


class CancellableProbeTests(unittest.TestCase):
    def test_drains_both_noisy_pipes_with_bounded_retention(self):
        result = run_probe([sys.executable, "-c",
                            "import sys; sys.stdout.write('x'*200000); "
                            "sys.stderr.write('y'*200000)"], cancel_event=Event(),
                           timeout=5, output_limit=2048)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"x" * 2048)
        self.assertEqual(result.stderr, b"y" * 2048)

    def test_cancel_reaps_sleeping_child_promptly(self):
        flag = Event()
        timer = Timer(0.2, flag.set)
        timer.start()
        original = subprocess.Popen
        children = []

        def launch(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child

        start = monotonic()
        try:
            with patch("app.utils.cancellable_subprocess.subprocess.Popen", side_effect=launch):
                with self.assertRaises(CancelledError):
                    run_probe([sys.executable, "-c", "import time; time.sleep(30)"],
                              cancel_event=flag)
        finally:
            timer.cancel()
        self.assertLess(monotonic() - start, 3)
        self.assertIsNotNone(children[0].poll())

    def test_timeout_and_nonzero_exit(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_probe([sys.executable, "-c", "import time; time.sleep(30)"],
                      cancel_event=Event(), timeout=0.15)
        result = run_probe([sys.executable, "-c", "raise SystemExit(7)"],
                           cancel_event=Event())
        self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
