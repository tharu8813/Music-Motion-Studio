"""Offline tests for lyric translation QA reports; never require a live model."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.services.lyrics_translation_service import TranslationConfig, TranslationError
from scripts.benchmark_lyrics_translation import CASES, main, run_benchmark


class RecordingModel:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def translate(self, cues, config):
        self.calls.append((config.source, len(cues)))
        if self.fail:
            raise TranslationError("Simulated local model failure")
        return [f"번역 {i}" for i in range(len(cues))]


class LyricQualityBenchmarkTests(unittest.TestCase):
    @staticmethod
    def config():
        return TranslationConfig("ollama", source="auto", target="ko",
                                 model="qwen3.5:9b", endpoint="http://localhost:11434/v1")

    def test_suite_covers_negation_slang_mixed_language_and_timing(self):
        self.assertEqual(len(CASES), 8)
        self.assertEqual({x["id"] for x in CASES}, {
            "negation", "slang", "code_switch", "refrain",
            "ambiguous", "split_sentence", "multiline", "entities"})
        self.assertTrue(all(x["review"] and x["cues"] for x in CASES))

    def test_report_keeps_per_case_language_and_human_review_note(self):
        engine = RecordingModel()
        report = run_benchmark(engine, self.config(), cases=CASES[:3])
        self.assertEqual(engine.calls, [("en", 3), ("en", 3), ("auto", 3)])
        self.assertEqual(report["summary"]["completed_cases"], 3)
        self.assertEqual(report["summary"]["failed_cases"], 0)
        self.assertEqual(report["semantic_accuracy"], "not measured; human review required")
        self.assertEqual(report["cases"][0]["original"][0], "I don't miss you anymore.")
        self.assertTrue(all(case["elapsed_seconds"] >= 0 for case in report["cases"]))

    def test_failed_model_is_reported_without_fabricated_translation(self):
        report = run_benchmark(RecordingModel(fail=True), self.config(), cases=CASES[:1])
        self.assertEqual(report["summary"]["failed_cases"], 1)
        self.assertEqual(report["summary"]["completed_cases"], 0)
        self.assertEqual(report["cases"][0]["translations"], [])
        self.assertIn("Simulated local model failure", report["cases"][0]["error"])

    def test_repeated_phrases_with_different_outputs_are_flagged_not_overwritten(self):
        sample = ({"id": "repeats", "purpose": "test", "source_language": "en",
                   "cues": ["Same street", "Same street"], "review": "Compare"},)
        class DifferentTranslations:
            def translate(self, cues, config):
                return ["같은 거리", "똑같은 길"]
        report = run_benchmark(DifferentTranslations(), self.config(), cases=sample)
        self.assertEqual(report["summary"]["refrain_differences"], 1)
        self.assertEqual(report["cases"][0]["translations"], ["같은 거리", "똑같은 길"])

    def test_cli_writes_no_credentials_and_obeys_limit(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "qa.json"
            with patch("scripts.benchmark_lyrics_translation.LyricsTranslationService",
                       return_value=RecordingModel()):
                exit_code = main(["--provider", "ollama", "--model", "qwen3.5:9b",
                                  "--limit", "2", "--output", str(target)])
            self.assertEqual(exit_code, 0)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["total_cases"], 2)
            self.assertNotIn("key", payload)
            self.assertNotIn("endpoint", payload)


if __name__ == "__main__":
    unittest.main()
