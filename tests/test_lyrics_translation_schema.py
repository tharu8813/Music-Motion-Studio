"""Bounded format recovery and structured lyric response contracts."""
from threading import Event
import json
import unittest

from app.services.lyrics_translation_schema import schema_for_prompt
from app.services.lyrics_translation_service import (
    LyricsTranslationService, TranslationCancelled, TranslationConfig, TranslationError,
)


PLAN = {"story": "A farewell", "tone": "quiet", "voice": "first person",
        "style": "plain Korean", "refrains": [], "glossary": []}


class StructuredLyricOutputTests(unittest.TestCase):
    @staticmethod
    def native(text):
        return {"done": True, "done_reason": "stop", "message": {"content": text}}

    @staticmethod
    def config(provider="ollama"):
        return TranslationConfig(provider, source="en", target="ko", model="test-model",
                                 endpoint="http://localhost:11434/v1", key="test")

    def test_schema_stage_contracts_and_requested_row_ids(self):
        plan = schema_for_prompt(json.dumps({"stage": "song_analysis", "full_song": {"cues": []}}))
        self.assertEqual(set(plan["properties"]), {"plan"})
        self.assertEqual(plan["additionalProperties"], False)
        self.assertEqual(set(plan["properties"]["plan"]["properties"]),
                         {"story", "tone", "voice", "style", "refrains", "glossary"})
        batch = schema_for_prompt(json.dumps({"lyrics": [{"id": 3, "text": "Hi"},
                                                         {"id": 8, "text": "Bye"}]}))
        entry = batch["properties"]["translations"]
        self.assertEqual(entry["minItems"], 2)
        self.assertEqual(entry["maxItems"], 2)
        self.assertEqual(entry["items"]["properties"]["id"]["enum"], [3, 8])
        self.assertIsNone(schema_for_prompt("bad json"))
        self.assertIsNone(schema_for_prompt(json.dumps({"lyrics": [{"id": 3}, {"id": 3}]})))

    def test_ollama_bad_json_is_retried_once_with_same_ids(self):
        events, calls = [], []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            self.assertIsInstance(body["format"], dict)
            if payload.get("stage") == "song_analysis":
                return self.native(json.dumps({"plan": PLAN}))
            ids = [row["id"] for row in payload["lyrics"]]
            calls.append((ids, payload.get("quality_feedback", "")))
            if len(calls) == 1:
                return self.native('{"translations":[')
            return self.native(json.dumps({"translations": [
                {"id": index, "text": "안녕"} for index in ids]}))
        result = LyricsTranslationService(transport).translate(
            [{"text": "Hello"}], self.config(), activity=events.append)
        self.assertEqual(result, ["안녕"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertTrue(calls[1][1])
        self.assertEqual([e["stage"] for e in events if e["stage"] == "format_retry"],
                         ["format_retry"])

    def test_format_error_never_retries_indefinitely(self):
        calls = []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.native(json.dumps({"plan": PLAN}))
            calls.append(payload)
            return self.native('{"translations":[')
        with self.assertRaisesRegex(TranslationError, "JSON"):
            LyricsTranslationService(transport).translate(
                [{"text": "Hello"}], self.config())
        self.assertEqual(len(calls), 2)

    def test_authentication_error_never_reissues_translation_request(self):
        seen = []
        def transport(url, body=None, headers=None, **kwargs):
            seen.append(url)
            raise TranslationError("HTTP 401: Unauthorized")
        with self.assertRaisesRegex(TranslationError, "HTTP 401"):
            LyricsTranslationService(transport).translate(
                [{"text": "Hello"}], self.config())
        self.assertEqual(len(seen), 1)

    def test_cancellation_during_malformed_output_prevents_retry(self):
        cancel, calls = Event(), []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.native(json.dumps({"plan": PLAN}))
            calls.append(payload)
            cancel.set()
            return self.native("not valid JSON")
        with self.assertRaises(TranslationCancelled):
            LyricsTranslationService(transport).translate(
                [{"text": "Hello"}], self.config(), cancel=cancel)
        self.assertEqual(len(calls), 1)

    def test_cloud_compatible_schemas_preserve_existing_provider_paths(self):
        for provider in ("openai", "chatgpt", "gemini"):
            with self.subTest(provider=provider):
                requests = []
                def transport(url, body=None, headers=None, **kwargs):
                    requests.append(body)
                    if provider in {"openai", "chatgpt"}:
                        payload = json.loads(body["input"][0]["content"])
                        return json.dumps({"plan": PLAN}) if payload.get("stage") == "song_analysis" else (
                            json.dumps({"translations": [{"id": 0, "text": "안녕"}]}))
                    payload = json.loads(body["contents"][0]["parts"][0]["text"])
                    content = json.dumps({"plan": PLAN}) if payload.get("stage") == "song_analysis" else (
                        json.dumps({"translations": [{"id": 0, "text": "안녕"}]}))
                    return {"candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": content}]}}]}
                result = LyricsTranslationService(transport).translate(
                    [{"text": "Hello"}], self.config(provider))
                self.assertEqual(result, ["안녕"])
                if provider == "openai":
                    self.assertEqual(requests[0]["text"]["format"]["type"], "json_schema")
                elif provider == "chatgpt":
                    self.assertNotIn("text", requests[0])
                else:
                    self.assertIn("responseJsonSchema", requests[0]["generationConfig"])


    def test_unsupported_schema_falls_back_once_per_stage(self):
        for provider in ("ollama", "openai", "gemini"):
            with self.subTest(provider=provider):
                calls = []
                def transport(url, body=None, headers=None, **kwargs):
                    if provider == "ollama":
                        payload = json.loads(body["messages"][1]["content"])
                        structured = isinstance(body["format"], dict)
                    elif provider == "openai":
                        payload = json.loads(body["input"][0]["content"])
                        structured = "text" in body
                    else:
                        payload = json.loads(body["contents"][0]["parts"][0]["text"])
                        structured = "responseJsonSchema" in body["generationConfig"]
                    calls.append((payload.get("stage", "translate"), structured))
                    if structured:
                        raise TranslationError("HTTP 400: schema unsupported")
                    text = json.dumps({"plan": PLAN}) if payload.get("stage") == "song_analysis" else (
                        json.dumps({"translations": [{"id": 0, "text": "안녕"}]}))
                    if provider == "ollama":
                        return self.native(text)
                    if provider == "gemini":
                        return {"candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": text}]}}]}
                    return text
                result = LyricsTranslationService(transport).translate(
                    [{"text": "Hello"}], self.config(provider))
                self.assertEqual(result, ["안녕"])
                self.assertEqual(calls, [("song_analysis", True), ("song_analysis", False),
                                         ("translate", True), ("translate", False)])

    def test_format_retry_preserves_only_valid_partial_rows(self):
        calls, events = [], []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.native(json.dumps({"plan": PLAN}))
            calls.append(payload)
            if len(calls) == 1:
                return self.native(json.dumps({"translations": [{"id": 0, "text": "안녕"}]}))
            return self.native("INVALID")
        with self.assertRaises(TranslationError):
            LyricsTranslationService(transport).translate(
                [{"text": "Hello"}, {"text": "Goodbye"}], self.config(), activity=events.append)
        self.assertEqual(len(calls), 2)
        complete = [e for e in events if e["stage"] == "complete"]
        self.assertEqual([(e["row"], e["text"]) for e in complete], [(0, "안녕")])
        self.assertEqual(next(e for e in events if e["stage"] == "failure")["rows"], [1])

if __name__ == "__main__":
    unittest.main()
