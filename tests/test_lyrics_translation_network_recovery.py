"""Transient provider failure policy: small cancellable retry without duplicate billing."""
import json
import unittest
from threading import Event
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from app.services.lyrics_translation_service import (
    LyricsTranslationService, TransientTranslationError, TranslationCancelled,
    TranslationConfig, TranslationError, request_json,
)


class TransientNetworkPolicyTests(unittest.TestCase):
    @staticmethod
    def config():
        return TranslationConfig("chatgpt", model="test-model", key="fake-test-key",
                                 source="en", target="ko")

    @staticmethod
    def response(payload):
        return json.dumps(payload, ensure_ascii=False)

    def test_http_502_503_504_classified_as_transient_not_auth_or_local_crash(self):
        for code in (502, 503, 504):
            with self.subTest(code=code):
                opener = Mock()
                opener.open.side_effect = HTTPError("http://localhost:11434/api/chat",
                                                    code, "Provider unavailable", {}, None)
                with patch("app.services.lyrics_translation_service.build_opener", return_value=opener):
                    with self.assertRaisesRegex(TransientTranslationError, "HTTP " + str(code)):
                        request_json("http://localhost:11434/api/chat", {})
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.openai.com/v1/responses",
                                             429, "Quota", {}, None)
        with patch("app.services.lyrics_translation_service.build_opener", return_value=opener):
            with self.assertRaises(TranslationError) as error:
                request_json("https://api.openai.com/v1/responses", {})
            self.assertNotIsInstance(error.exception, TransientTranslationError)

    def test_503_during_translation_retries_once_then_succeeds(self):
        calls = []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["input"][0]["content"])
            if payload.get("stage") == "song_analysis":
                return self.response({"plan": {"story": "", "tone": "", "voice": "",
                    "style": "", "refrains": [], "glossary": []}})
            calls.append(payload)
            if len(calls) == 1:
                raise TransientTranslationError("HTTP 503: temporarily unavailable")
            return self.response({"translations": [{"id": 0, "text": "안녕"}]})
        result = LyricsTranslationService(transport).translate(
            [{"text": "Hello"}], self.config())
        self.assertEqual(result, ["안녕"])
        self.assertEqual(len(calls), 2)
        self.assertEqual([c["lyrics"] for c in calls], [calls[0]["lyrics"]] * 2)

    def test_repeated_transient_failure_stops_after_one_retry(self):
        calls = []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["input"][0]["content"])
            if payload.get("stage") == "song_analysis":
                return self.response({"plan": {"story": "", "tone": "", "voice": "",
                    "style": "", "refrains": [], "glossary": []}})
            calls.append(payload)
            raise TransientTranslationError("HTTP 503: retry exhausted")
        with self.assertRaisesRegex(TranslationError, "503"):
            LyricsTranslationService(transport).translate(
                [{"text": "Hello"}], self.config())
        self.assertEqual(len(calls), 2)

    def test_cancellation_during_backoff_does_not_send_second_request(self):
        cancel, calls = Event(), []
        def transport(url, body=None, headers=None, **kwargs):
            calls.append(url)
            cancel.set()
            raise TransientTranslationError("HTTP 503: temporarily unavailable")
        with self.assertRaises(TranslationCancelled):
            LyricsTranslationService(transport)._llm_completion(
                self.config(), cancel, "Follow the JSON contract", '{"lyrics":[{"id":0,"text":"Hello"}]}')
        self.assertEqual(len(calls), 1)

    def test_auth_rate_and_ambiguous_connection_errors_are_not_replayed(self):
        for message in ("HTTP 401: expired", "HTTP 429: rate limited",
                        "Cannot connect to the translation service.", "Local LLM response timed out."):
            with self.subTest(error=message):
                calls = []
                def transport(url, body=None, headers=None, **kwargs):
                    calls.append(url)
                    raise TranslationError(message)
                with self.assertRaises(TranslationError):
                    LyricsTranslationService(transport)._llm_completion(
                        self.config(), None, "Translate", '{"lyrics":[{"id":0,"text":"Hello"}]}')
                self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
