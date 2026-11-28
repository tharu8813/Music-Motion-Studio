"""Keep independent Windows test shards complete and disjoint."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.run_tests import discover_modules, parse_unittest_slowest_tests, partition_modules


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

    def test_six_ci_shards_cover_every_module_exactly_once(self) -> None:
        directory = Path(__file__).resolve().parent
        all_modules = discover_modules(directory, [], [])
        gui = discover_modules(directory, ["main_window"], [])
        other = discover_modules(directory, [], ["main_window"])
        shards = [partition_modules(group, index, 3)
                  for group in (gui, other) for index in range(3)]
        self.assertTrue(all(shards))
        combined = [module for shard in shards for module in shard]
        self.assertCountEqual(combined, all_modules)
        self.assertEqual(len(combined), len(set(combined)))
        self.assertEqual(partition_modules(all_modules, 0, 1), all_modules)

    def test_shard_partition_is_deterministic_and_rejects_invalid_ranges(self) -> None:
        modules = ["a", "b", "c", "d", "e"]
        self.assertEqual(partition_modules(modules, 0, 3), ["a", "d"])
        self.assertEqual(partition_modules(modules, 1, 3), ["b", "e"])
        self.assertEqual(partition_modules(modules, 2, 3), ["c"])
        for index, count in ((-1, 3), (3, 3), (0, 0), (0, -1)):
            with self.subTest(index=index, count=count), self.assertRaises(ValueError):
                partition_modules(modules, index, count)

    def test_unittest_slowest_case_parser_keeps_per_test_durations(self) -> None:
        stderr = (
            "test_alpha (tests.test_main_window.Sample.test_alpha) ... ok\n"
            "Slowest test durations\n"
            "----------------------------------------------------------------------\n"
            "1.250s test_one (tests.test_main_window.Sample.test_one)\n"
            "0.125s setUpClass (tests.test_main_window.Sample)\n"
            "Ran 2 tests in 1.375s\n"
            "OK\n"
        )
        self.assertEqual(
            parse_unittest_slowest_tests(stderr),
            [
                {"test": "test_one (tests.test_main_window.Sample.test_one)", "seconds": 1.25},
                {"test": "setUpClass (tests.test_main_window.Sample)", "seconds": 0.125},
            ],
        )

    def test_parser_does_not_invent_durations_from_old_python_logs(self) -> None:
        self.assertEqual(parse_unittest_slowest_tests("Ran 5 tests\nOK"), [])
        self.assertEqual(parse_unittest_slowest_tests(""), [])

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
