"""Translation continuity, false-unneeded rows and conservative quality checks."""
import json
import unittest

from app.services.lyrics_translation_quality import (
    normalized_source, quality_issue, rolling_translation_memory,
    should_translate_english, stable_refrain_sources,
)
from app.services.lyrics_translation_service import (
    LyricsTranslationService, TranslationConfig, TranslationError,
)


PLAN = {"story": "Love song", "tone": "tender", "voice": "informal",
        "style": "natural Korean lyric", "refrains": [], "glossary": []}


class QualityGateTests(unittest.TestCase):
    def test_english_to_korean_does_not_mark_missing_translation_as_unneeded(self):
        self.assertTrue(should_translate_english("Hello", "en", "ko"))
        self.assertTrue(should_translate_english("We are here", "auto", "ko"))
        self.assertTrue(should_translate_english("A name", "auto", "ko"))
        self.assertFalse(should_translate_english("한국어 가사", "auto", "ko"))
        self.assertTrue(should_translate_english("I love you, 사랑해", "auto", "ko"))
        self.assertFalse(should_translate_english("BTS와 함께", "auto", "ko"))
        self.assertFalse(should_translate_english("L.O.V.E", "auto", "ko"))
        self.assertFalse(should_translate_english("NASA", "en", "ko"))
        self.assertFalse(should_translate_english("Hello", "en", "ja"))
        self.assertIn("untranslated", quality_issue("Hello", "", "en", "ko"))
        self.assertIn("untranslated", quality_issue("Hello", "Hello", "en", "ko"))
        self.assertIn("instead of Korean", quality_issue("We are here", "We are still here", "en", "ko"))
        self.assertIsNone(quality_issue("Hello", "안녕", "en", "ko"))
        self.assertIsNone(quality_issue("이미 한글", "", "auto", "ko"))

    def test_empty_or_copied_lyrics_are_not_silent_success_in_any_language(self):
        for source, direction, target in (
                ("LOVE", "auto", "ko"), ("Forever", "auto", "ko"),
                ("Dreaming", "auto", "ko"), ("Hello", "en", "ja"),
                ("사랑해", "ko", "en"), ("Hola", "auto", "en"),
                ("こんにちは", "ja", "ko")):
            with self.subTest(source=source, direction=direction, target=target):
                self.assertIsNotNone(quality_issue(source, "", direction, target))
                self.assertIsNotNone(quality_issue(source, source, direction, target))
        for source, direction, target in (
                ("이미 한국어", "auto", "ko"), ("L.O.V.E", "en", "ko"),
                ("こんにちは", "auto", "ja"), ("...", "auto", "en")):
            with self.subTest(source=source, target=target):
                self.assertIsNone(quality_issue(source, "", direction, target))

    def test_unexpected_chinese_script_is_flagged_but_existing_source_is_allowed(self):
        self.assertIn("Unexpected Han", quality_issue("We're holding the reins", "우리가 쥔 缰绳", "en", "ko"))
        self.assertIsNone(quality_issue("龍의 노래", "龍의 노래", "auto", "ko"))
        self.assertIsNone(quality_issue("Chinese characters", "漢字", "en", "ja"))

    def test_refrain_detection_avoids_single_ambiguous_words(self):
        refrain = "Because the beat and melody belong together"
        chorus = "L.O.V.E is coming back to us"
        cues = [{"text": text} for text in
            [refrain, chorus, "run", "other", refrain, chorus, "run",
             "again", refrain, chorus]]
        stable = stable_refrain_sources(cues)
        self.assertIn(normalized_source(refrain), stable)
        self.assertIn(normalized_source(chorus), stable)
        self.assertNotIn(normalized_source("run"), stable)

    def test_rolling_memory_contains_early_style_anchor_and_recent_lines_with_bounds(self):
        history = [(i, f"Source line {i}", f"번역된 문장 {i}") for i in range(15)]
        rows = rolling_translation_memory(history)
        self.assertEqual([row["id"] for row in rows], [0, 1] + list(range(5, 15)))
        self.assertEqual(rows[-1]["text"], "번역된 문장 14")
        limited = rolling_translation_memory(history, max_chars=60)
        self.assertLessEqual(sum(len(r["source_text"]) + len(r["text"]) for r in limited), 60)


class LyricsQualityIntegrationTests(unittest.TestCase):
    @staticmethod
    def config(provider="ollama"):
        return TranslationConfig(provider, source="en", target="ko", model="qwen3.5:9b",
                                 endpoint="http://localhost:11434/v1", key="test")

    @staticmethod
    def response(payload):
        return {"done": True, "done_reason": "stop", "message": {
            "content": json.dumps(payload, ensure_ascii=False)}}

    def test_previous_completed_translations_are_sent_with_every_later_batch(self):
        prompts = []
        cues = [{"text": f"We are here at night {i}"} for i in range(14)]
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            prompts.append(payload)
            return self.response({"translations": [
                {"id": row["id"], "text": f"밤에 함께 있어 {row['id']}"} for row in payload["lyrics"]]})
        output = LyricsTranslationService(transport).translate(cues, self.config())
        self.assertEqual(output[-1], "밤에 함께 있어 13")
        self.assertEqual([len(p["lyrics"]) for p in prompts], [6, 6, 2])
        self.assertEqual(prompts[0]["context_only"]["previous_translations"], [])
        second = prompts[1]["context_only"]["previous_translations"]
        self.assertEqual([row["id"] for row in second], list(range(6)))
        self.assertEqual(second[2]["text"], "밤에 함께 있어 2")
        third = prompts[2]["context_only"]["previous_translations"]
        self.assertIn(0, [row["id"] for row in third])
        self.assertIn(11, [row["id"] for row in third])

    def test_existing_edited_or_approved_rows_are_context_for_selected_retranslation(self):
        payloads = []
        cues = [{"text": f"We love the sky {i}"} for i in range(8)]
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            payloads.append(payload)
            return self.response({"translations": [
                {"id": row["id"], "text": f"다시 {row['id']}"} for row in payload["lyrics"]]})
        result = LyricsTranslationService(transport).translate(
            cues, self.config(), selected_rows=[5, 7],
            existing_translations={0: "처음 정한 문체", 2: "직접 수정한 가사", 5: "바꾸려는 가사", 6: "미래 가사"})
        self.assertEqual(result[5], "다시 5")
        history = payloads[0]["context_only"]["previous_translations"]
        self.assertEqual([row["id"] for row in history], [0, 2])
        self.assertNotIn(5, [row["id"] for row in history])
        self.assertNotIn(6, [row["id"] for row in history])

    def test_repeated_chorus_is_canonicalized_across_batches(self):
        refrain = "Because our beat and melody sound as one"
        cues = [{"text": refrain if i in (0, 7, 13) else f"We are together {i}"} for i in range(15)]
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            rows = []
            for row in payload["lyrics"]:
                translation = ("비트와 선율이 하나가 돼" if row["id"] == 0
                               else "비트와 멜로디가 합쳐졌어" if row["text"] == refrain
                               else f"함께해 {row['id']}")
                rows.append({"id": row["id"], "text": translation})
            return self.response({"translations": rows})
        output = LyricsTranslationService(transport).translate(cues, self.config())
        self.assertEqual(output[0], "비트와 선율이 하나가 돼")
        self.assertEqual(output[7], output[0])
        self.assertEqual(output[13], output[0])

    def test_untranslated_english_is_retried_instead_of_marked_unneeded(self):
        prompts, events = [], []
        def transport(url, body=None, headers=None, **kwargs):
            data = json.loads(body["messages"][1]["content"])
            if data.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            prompts.append(data)
            if data["quality_feedback"]:
                return self.response({"translations": [{"id": 0, "text": "안녕, 오늘은 어때"}]})
            return self.response({"translations": [{"id": 0, "text": ""}]})
        result = LyricsTranslationService(transport).translate(
            [{"text": "Hello, how are you?"}], self.config(), activity=events.append)
        self.assertEqual(result, ["안녕, 오늘은 어때"])
        self.assertEqual(len(prompts), 2)
        self.assertEqual([e["stage"] for e in events if e["stage"] == "quality_retry"], ["quality_retry"])
        self.assertIn("untranslated", prompts[1]["quality_feedback"])

    def test_english_copy_retry_failure_is_error_not_not_needed(self):
        events = []
        def transport(url, body=None, headers=None, **kwargs):
            data = json.loads(body["messages"][1]["content"])
            if data.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            return self.response({"translations": [
                {"id": row["id"], "text": "그대와 함께" if row["id"] == 0 else row["text"]}
                for row in data["lyrics"]]})
        with self.assertRaisesRegex(TranslationError, "quality check failed") as error:
            LyricsTranslationService(transport).translate(
                [{"text": "We are together"}, {"text": "I love you"}],
                self.config(), activity=events.append)
        self.assertEqual(error.exception.partial, {0: "그대와 함께"})
        self.assertTrue(any(e["stage"] == "complete" and e["row"] == 0 for e in events))
        self.assertTrue(any(e["stage"] == "failure" and e["rows"] == [1] for e in events))
        self.assertFalse(any(e["stage"] == "complete" and e.get("row") == 1 for e in events))

    def test_mixed_or_already_target_language_lines_can_be_unchanged(self):
        def transport(url, body=None, headers=None, **kwargs):
            data = json.loads(body["messages"][1]["content"])
            if data.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            return self.response({"translations": [{"id": row["id"], "text": ""} for row in data["lyrics"]]})
        cues = [{"text": "이미 한국어 가사"}, {"text": "L.O.V.E"}]
        result = LyricsTranslationService(transport).translate(cues, self.config())
        self.assertEqual(result, ["", ""])

    def test_partial_empty_row_is_rejected_after_retry_json_failure(self):
        events, calls = [], []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            calls.append(payload)
            if payload["quality_feedback"]:
                # First invalid row still blank, second row absent: a partial
                # None must not be promoted to completed/unneeded.
                return self.response({"translations": [{"id": 0, "text": ""}]})
            return self.response({"translations": [
                {"id": 0, "text": ""}, {"id": 1, "text": ""}]})
        with self.assertRaises(TranslationError):
            LyricsTranslationService(transport).translate(
                [{"text": "Forever"}, {"text": "Dreaming"}],
                self.config(), activity=events.append)
        # Quality retry receives an incomplete response. P1 then retries
        # the malformed JSON/ID contract once more; neither result may be
        # promoted to a successful or unnecessary translation.
        self.assertEqual(len(calls), 3)
        self.assertIn("Previous response violated", calls[2]["quality_feedback"])
        self.assertFalse(any(event["stage"] == "complete" for event in events))
        failure = next(event for event in events if event["stage"] == "failure")
        self.assertEqual(failure["rows"], [0, 1])

    def test_non_korean_unchanged_result_is_retried_not_skipped(self):
        calls = []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            calls.append(payload)
            return self.response({"translations": [{"id": 0, "text": "Hello"}]})
        with self.assertRaisesRegex(TranslationError, "quality check failed"):
            LyricsTranslationService(transport).translate(
                [{"text": "Hello"}], self.config().__class__(
                    "ollama", source="en", target="ja", model="qwen3.5:9b",
                    endpoint="http://localhost:11434/v1"))
        self.assertEqual(len(calls), 2)

    def test_negation_reversal_is_corrected_in_targeted_retry(self):
        requests = []
        def transport(url, body=None, headers=None, **kwargs):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            requests.append(payload)
            return self.response({"translations": [{
                "id": 0,
                "text": "이제 네가 그립지 않아" if payload["quality_feedback"]
                        else "아직도 네가 그리워",
            }]})
        output = LyricsTranslationService(transport).translate(
            [{"text": "I don't miss you anymore."}], self.config())
        self.assertEqual(output, ["이제 네가 그립지 않아"])
        self.assertEqual(len(requests), 2)
        self.assertIn("negated phrase", requests[1]["quality_feedback"])

    def test_unexpected_han_gets_targeted_korean_retranslation(self):
        requests = []
        def transport(url, body=None, headers=None, **kwargs):
            data = json.loads(body["messages"][1]["content"])
            if data.get("stage") == "song_analysis":
                return self.response({"plan": PLAN})
            requests.append(data)
            translation = "우리의 缰绳" if not data["quality_feedback"] else "우리가 쥐고 있는 고삐"
            return self.response({"translations": [{"id": 0, "text": translation}]})
        result = LyricsTranslationService(transport).translate(
            [{"text": "The reins that we hold"}], self.config())
        self.assertEqual(result, ["우리가 쥐고 있는 고삐"])
        self.assertEqual(len(requests), 2)
        self.assertIn("Unexpected Han", requests[1]["quality_feedback"])


if __name__ == "__main__":
    unittest.main()
