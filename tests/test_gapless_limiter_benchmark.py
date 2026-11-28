"""Unit coverage for same-input benchmark ordering and EBU summary parsing."""

import unittest

from tools.gapless_limiter_benchmark import order, parse_measurement


class GaplessLimiterBenchmarkTests(unittest.TestCase):
    def test_abba_balances_variant_order(self):
        self.assertEqual(order(0), ("auto", "single", "single", "auto"))
        self.assertEqual(order(1), ("single", "auto", "auto", "single"))

    def test_parse_final_summary_ignores_intermediate_measurements(self):
        log = """t: 0.1 I: -70.0 LUFS Peak: -40.0 dBFS
[Parsed_ebur128] Summary:

  Integrated loudness:
    I:         -23.5 LUFS
    Threshold: -34.4 LUFS
  True peak:
    Peak:       -1.7 dBFS
"""
        self.assertEqual(parse_measurement(log), (-23.5, -1.7))

    def test_missing_or_nonfinite_summary_is_rejected(self):
        for text in ("I: -20.0 LUFS Peak: -1.0 dBFS", "Summary: I: -inf LUFS Peak: -inf dBFS"):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                parse_measurement(text)
