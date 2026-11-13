"""Keep CI's two independent Windows test shards complete and disjoint."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.run_tests import discover_modules


class TestModulePartitionTests(unittest.TestCase):
    def test_main_window_and_other_shards_are_disjoint_and_complete(self) -> None:
        tests_directory = Path(__file__).resolve().parent
        all_modules = discover_modules(tests_directory, [], [])
        gui_modules = discover_modules(tests_directory, ["main_window"], [])
        other_modules = discover_modules(tests_directory, [], ["main_window"])
        self.assertEqual(set(gui_modules) & set(other_modules), set())
        self.assertEqual(set(gui_modules) | set(other_modules), set(all_modules))
        self.assertTrue(gui_modules)
        self.assertTrue(other_modules)
        self.assertEqual(len(all_modules), len(gui_modules) + len(other_modules))

    def test_filename_filters_and_exclusions_are_deterministic(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            for name in ("test_main_window.py", "test_main_window_preview.py",
                         "test_preview.py", "test_audio.py", "helper.py"):
                (directory / name).touch()
            self.assertEqual(discover_modules(directory, ["main_window"], []),
                             ["tests.test_main_window", "tests.test_main_window_preview"])
            self.assertEqual(discover_modules(directory, [], ["main_window"]),
                             ["tests.test_audio", "tests.test_preview"])
            self.assertEqual(discover_modules(directory, ["preview"], ["main_window"]),
                             ["tests.test_preview"])


if __name__ == "__main__":
    unittest.main()
