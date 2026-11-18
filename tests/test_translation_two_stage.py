"""Two-stage LLM lyrics translation: whole-song context, fallbacks, and cancellation."""
import json
import unittest

from app.services.lyrics_translation_service import (
    LyricsTranslationService, TranslationConfig, TranslationCancelled,
)


PLAN = {
    "story": "A changing relationship", "tone": "bittersweet to hopeful",
    "voice": "first-person, addressee unclear", "style": "natural and faithful",
    "refrains": ["Still here"], "glossary": [{"source": "Still here", "target": "여전히 여기"}],
}


class TwoStageTranslationTests(unittest.TestCase):
    def test_full_song_analyzed_once_then_shared_between_bounded_batches(self):
        cues = [{"text": "Still here" if i in (0, 20) else f"Verse line {i}"}
                for i in range(25)]
        requests, events = [], []

        def transport(url, body=None, headers=None, **kwargs):
            data = json.loads(body["input"][0]["content"])
            requests.append(data)
            if data.get("stage") == "song_analysis":
                return json.dumps({"plan": PLAN})
            return json.dumps({"translations": [
                {"id": row["id"], "text": f"번역 {row['id']}"} for row in data["lyrics"]
            ]})

        config = TranslationConfig("chatgpt", model="model", key="test")
        results = LyricsTranslationService(transport).translate(cues, config, activity=events.append)
        self.assertEqual(len(requests), 3)  # one plan, two small translation batches
        self.assertEqual(requests[0]["stage"], "song_analysis")
        self.assertEqual(len(requests[0]["full_song"]["cues"]), len(cues))
        self.assertEqual([len(r["lyrics"]) for r in requests[1:]], [20, 5])
        self.assertTrue(all(r["translation_plan"] == PLAN for r in requests[1:]))
        self.assertEqual(results[20], "번역 20")
        self.assertEqual([e["stage"] for e in events if e["stage"].startswith("planning")],
                         ["planning", "planning_complete"])
        self.assertEqual(requests[2]["context_only"]["reference_translations"],
                         [{"id": 0, "source_text": "Still here", "text": "번역 0"}])

    def test_selective_translation_still_analyzes_every_original_cue(self):
        cues = [{"text": f"Source {i}"} for i in range(6)]
        requests = []

        def transport(url, body=None, headers=None, **kwargs):
            data = json.loads(body["input"][0]["content"])
            requests.append(data)
            if data.get("stage") == "song_analysis":
                return json.dumps({"plan": PLAN})
            return json.dumps({"translations": [
                {"id": row["id"], "text": "완료"} for row in data["lyrics"]
            ]})

        output = LyricsTranslationService(transport).translate(
            cues, TranslationConfig("chatgpt", model="model", key="test"), selected_rows=[2, 5])
        self.assertEqual([row["id"] for row in requests[0]["full_song"]["cues"]], list(range(6)))
        self.assertEqual([row["id"] for row in requests[1]["lyrics"]], [2, 5])
        self.assertEqual(output, ["", "", "완료", "", "", "완료"])

    def test_invalid_analysis_falls_back_to_song_context_without_losing_rows(self):
        events, requests = [], []

        def transport(url, body=None, headers=None, **kwargs):
            data = json.loads(body["input"][0]["content"])
            requests.append(data)
            if data.get("stage") == "song_analysis":
                return '{"plan":{}}'
            return json.dumps({"translations": [{"id": 0, "text": "번역"}]})

        output = LyricsTranslationService(transport).translate(
            [{"text": "Original"}], TranslationConfig("chatgpt", model="model", key="test"),
            activity=events.append)
        self.assertEqual(output, ["번역"])
        self.assertIsNone(requests[1]["translation_plan"])
        self.assertEqual(requests[1]["song_context"]["coverage"], "full")
        self.assertIn("planning_fallback", [event["stage"] for event in events])

    def test_cancel_during_analysis_prevents_all_translation_batches(self):
        calls = []
        def transport(url, body=None, headers=None, **kwargs):
            calls.append(body)
            raise TranslationCancelled()
        with self.assertRaises(TranslationCancelled):
            LyricsTranslationService(transport).translate(
                [{"text": "Original"}], TranslationConfig("chatgpt", model="model", key="test"))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
