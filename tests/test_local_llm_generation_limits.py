"""Regression tests for token-limited Ollama/LM Studio lyric generations."""
import json
from threading import Event
import unittest

from app.services.lyrics_translation_service import (
    LyricsTranslationService, TranslationCancelled, TranslationConfig, TranslationError,
)


PLAN = {"story": "A farewell", "tone": "quiet", "voice": "neutral",
        "style": "faithful and natural", "refrains": [], "glossary": []}


class LocalLLMGenerationLimitTests(unittest.TestCase):
    def config(self, provider="ollama"):
        return TranslationConfig(provider, model="qwen3.5:9b",
                                 endpoint="http://localhost:11434/v1")

    @staticmethod
    def request_data(body):
        return json.loads(body["messages"][1]["content"])

    @staticmethod
    def native(content="", reason="stop"):
        return {"done": True, "done_reason": reason, "message": {"content": content}}

    def test_ollama_native_api_disables_thinking_and_splits_length_limited_batches(self):
        calls, events = [], []
        cues = [{"text": f"Verse {i}"} for i in range(6)]

        def transport(url, body=None, headers=None, **kw):
            self.assertEqual(url, "http://localhost:11434/api/chat")
            self.assertIs(body["stream"], False)
            self.assertIs(body["think"], False)
            self.assertEqual(body["format"], "json")
            self.assertEqual(body["options"]["num_ctx"], 8192)
            self.assertEqual(body["options"]["num_predict"], 3072)
            payload = self.request_data(body)
            if payload.get("stage") == "song_analysis":
                return self.native(json.dumps({"plan": PLAN}))
            ids = [row["id"] for row in payload["lyrics"]]
            calls.append(ids)
            if len(ids) == 6:
                return self.native(reason="length")
            return self.native(json.dumps({"translations": [
                {"id": index, "text": f"번역 {index}"} for index in ids
            ]}))

        output = LyricsTranslationService(transport).translate(
            cues, self.config(), activity=events.append)
        self.assertEqual(output, [f"번역 {i}" for i in range(6)])
        self.assertEqual(calls, [list(range(6)), list(range(3)), list(range(3, 6))])
        self.assertTrue(any(event["stage"] == "retry" for event in events))
        self.assertEqual(len([e for e in events if e["stage"] == "complete"]), 6)

    def test_single_line_gets_one_larger_output_retry(self):
        budgets = []

        def transport(url, body=None, headers=None, **kw):
            payload = self.request_data(body)
            if payload.get("stage") == "song_analysis":
                return self.native(json.dumps({"plan": PLAN}))
            budgets.append(body["options"]["num_predict"])
            if len(budgets) == 1:
                return self.native(reason="length")
            return self.native(json.dumps({"translations": [{"id": 0, "text": "안녕"}]}))

        self.assertEqual(LyricsTranslationService(transport).translate(
            [{"text": "Hello"}], self.config()), ["안녕"])
        self.assertEqual(budgets, [3072, 6144])

    def test_unrecoverable_half_preserves_other_completed_rows_and_stops(self):
        calls, events = [], []

        def transport(url, body=None, headers=None, **kw):
            data = self.request_data(body)
            if data.get("stage") == "song_analysis":
                return self.native(json.dumps({"plan": PLAN}))
            ids = [row["id"] for row in data["lyrics"]]
            calls.append((ids, body["options"]["num_predict"]))
            if ids == [0]:
                return self.native(json.dumps({"translations": [{"id": 0, "text": "하나"}]}))
            return self.native(reason="length")

        with self.assertRaisesRegex(TranslationError, "token budget") as raised:
            LyricsTranslationService(transport).translate(
                [{"text": "One"}, {"text": "Two"}], self.config(), activity=events.append)
        self.assertEqual(raised.exception.partial, {0: "하나"})
        self.assertEqual(calls, [([0, 1], 3072), ([0], 3072),
                                ([1], 3072), ([1], 6144)])
        self.assertTrue(any(e.get("stage") == "complete" and e.get("row") == 0
                            for e in events))
        self.assertTrue(any(e.get("stage") == "failure" and e.get("rows") == [1]
                            for e in events))

    def test_lmstudio_length_recovery_uses_compatible_completion_endpoint(self):
        calls = []
        def transport(url, body=None, headers=None, **kw):
            self.assertTrue(url.endswith("/chat/completions"))
            data = self.request_data(body)
            if data.get("stage") == "song_analysis":
                return {"choices": [{"finish_reason": "stop", "message":
                    {"content": json.dumps({"plan": PLAN})}}]}
            ids = [row["id"] for row in data["lyrics"]]
            calls.append((ids, body["max_tokens"]))
            if len(ids) > 1:
                return {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
                {"translations": [{"id": ids[0], "text": f"번역 {ids[0]}"}]})}}]}
        self.assertEqual(LyricsTranslationService(transport).translate(
            [{"text": "A"}, {"text": "B"}], self.config("lmstudio")), ["번역 0", "번역 1"])
        self.assertEqual(calls, [([0, 1], 3072), ([0], 3072), ([1], 3072)])

    def test_cancel_before_retry_aborts_immediately(self):
        cancel = Event()
        calls = []

        def transport(url, body=None, headers=None, **kw):
            data = self.request_data(body)
            if data.get("stage") == "song_analysis":
                return self.native(json.dumps({"plan": PLAN}))
            calls.append(data)
            cancel.set()
            return self.native(reason="length")

        with self.assertRaises(TranslationCancelled):
            LyricsTranslationService(transport).translate(
                [{"text": "First"}, {"text": "Second"}], self.config(), cancel=cancel)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
