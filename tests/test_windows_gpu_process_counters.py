"""Process-scoped GPU Engine parsing requires no Windows GPU."""
from __future__ import annotations

import unittest

from tools.windows_gpu_process_counters import aggregate_engine_rows, parse_engine_instance


class WindowsGpuEngineTests(unittest.TestCase):
    def test_instance_name_extracts_pid_and_engine(self):
        self.assertEqual(parse_engine_instance(
            "pid_123_luid_0x00000000_0x00000002_phys_0_eng_1_engtype_VideoEncode"),
            (123, "videoencode"))
        self.assertEqual(parse_engine_instance(
            "pid_21_luid_0x1_eng_0_engtype_3D"), (21, "3d"))
        self.assertIsNone(parse_engine_instance("engtype_copy"))

    def test_unrelated_processes_never_enter_report(self):
        rows = [
            ("pid_100_luid_0x1_eng_0_engtype_3D", 7.25),
            ("pid_100_luid_0x1_eng_1_engtype_Copy", 1.5),
            ("pid_101_luid_0x1_eng_2_engtype_VideoEncode", 29.5),
            ("pid_200_luid_0x1_eng_2_engtype_VideoEncode", 100.0),
            ("pid_100_luid_0x1_eng_3_engtype_3D", 2.75),
            ("pid_100_luid_0x1_eng_4_engtype_3D", float("nan")),
            ("pid_100_luid_0x1_eng_5_engtype_3D", -3),
        ]
        self.assertEqual(aggregate_engine_rows(rows, {100, 101}), {
            "100": {"3d": 10.0, "copy": 1.5},
            "101": {"videoencode": 29.5},
        })
        self.assertEqual(aggregate_engine_rows(rows, set()), {})


if __name__ == "__main__":
    unittest.main()
