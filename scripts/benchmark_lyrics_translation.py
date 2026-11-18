"""Reproducible human-review-oriented benchmark for local LLM lyric translation.

Run from the repository root:
    python scripts/benchmark_lyrics_translation.py --provider ollama --model qwen3.5:9b

This suite uses ORIGINAL short lyric-like samples. It does not assign a
semantic accuracy score: fluent but incorrect translations require human review.
No provider credentials, full model responses, or user project lyrics are saved.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.lyrics_translation_quality import normalized_source, quality_issue
from app.services.lyrics_translation_service import LyricsTranslationService, TranslationConfig, TranslationError


# Original benchmark sentences, not excerpts from released songs.
CASES = (
    {"id": "negation", "purpose": "Negation, emotional reversal, and speaker intent",
     "source_language": "en", "cues": [
         "I don't miss you anymore.",
         "I never wanted to break your heart.",
         "I still know the street where we met.",
     ], "review": "Do not drop don't/never or invent that the speaker wants to reunite."},
    {"id": "slang", "purpose": "Hip-hop/R&B slang and natural Korean diction",
     "source_language": "en", "cues": [
         "That beat hits different at midnight.",
         "I keep it low-key when the lights go down.",
         "No flex, I just want a quiet ride.",
     ], "review": "Avoid literal physical-hit translations and forced dictionary slang."},
    {"id": "code_switch", "purpose": "Korean/English within the same cue",
     "source_language": "auto", "cues": [
         "우리 둘만, one last night",
         "다시 돌아와, back to you",
         "이 도시는 cold without you",
     ], "review": "Translate English phrases without deleting the existing Korean."},
    {"id": "refrain", "purpose": "Consistency across substantial repeated chorus phrases",
     "source_language": "en", "cues": [
         "The same street remembers our names",
         "But the rain forgot our promises",
         "The same street remembers our names",
         "Nothing feels the way it used to",
         "The same street remembers our names",
     ], "review": "Keep repeated chorus wording coherent without forcing unrelated metaphors."},
    {"id": "ambiguous", "purpose": "Ambiguity, emotion, and unsupported inferences",
     "source_language": "en", "cues": [
         "You said you'd stay, but the door stayed open.",
         "I can't tell if this is goodbye.",
         "Maybe we were just passing through.",
     ], "review": "Retain uncertainty, do not invent identities or relationship details."},
    {"id": "split_sentence", "purpose": "One English sentence across multiple timed cues",
     "source_language": "en", "cues": [
         "If you had called me",
         "Before the lights went out",
         "I might have found my way home",
     ], "review": "Read as one conditional thought, but preserve each cue ID and order."},
    {"id": "multiline", "purpose": "Intra-cue line breaks and music phrasing",
     "source_language": "en", "cues": [
         "Stay a little longer\\nDon't let the moment fade",
         "I hear the city\\nBreathing under neon",
     ], "review": "Keep every internal line break and the negative command."},
    {"id": "entities", "purpose": "Names, places, time, and tense",
     "source_language": "en", "cues": [
         "Maya left the radio on.",
         "Seoul kept our secret until Sunday.",
         "We used to dance when the trains were late.",
     ], "review": "Preserve Maya/Seoul/Sunday and the distinction between past and present."},
)


def run_benchmark(service, config: TranslationConfig, *, cases=CASES) -> dict:
    """Return objective structure flags and unscored human review samples."""
    report = {"schema_version": 1,
              "provider": config.provider, "model": config.model,
              "target_language": config.target,
              "semantic_accuracy": "not measured; human review required",
              "cases": []}
    for sample in cases:
        original = [{"text": source} for source in sample["cues"]]
        case_config = replace(config, source=sample["source_language"])
        started = perf_counter()
        case = {"id": sample["id"], "purpose": sample["purpose"],
                "source_language": sample["source_language"], "review": sample["review"],
                "original": list(sample["cues"]), "translations": [],
                "quality_flags": [], "refrain_differences": [], "error": ""}
        try:
            translated = service.translate(original, case_config)
            case["translations"] = translated
            for index, (source, target) in enumerate(zip(sample["cues"], translated)):
                issue = quality_issue(source, target, sample["source_language"], config.target)
                if issue:
                    case["quality_flags"].append({"row": index + 1, "reason": issue})
            seen = {}
            for i, (source, target) in enumerate(zip(sample["cues"], translated)):
                normalized = normalized_source(source)
                if normalized in seen and seen[normalized][1] != target:
                    case["refrain_differences"].append({
                        "rows": [seen[normalized][0] + 1, i + 1], "original": source})
                else:
                    seen.setdefault(normalized, (i, target))
        except TranslationError as error:
            case["error"] = str(error)
        case["elapsed_seconds"] = round(perf_counter() - started, 3)
        report["cases"].append(case)
    report["summary"] = {
        "total_cases": len(report["cases"]),
        "completed_cases": sum(not row["error"] for row in report["cases"]),
        "failed_cases": sum(bool(row["error"]) for row in report["cases"]),
        "quality_flags": sum(len(row["quality_flags"]) for row in report["cases"]),
        "refrain_differences": sum(len(row["refrain_differences"]) for row in report["cases"]),
        "total_elapsed_seconds": round(sum(row["elapsed_seconds"] for row in report["cases"]), 3),
    }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local LLM lyric QA with original test lyrics")
    parser.add_argument("--provider", choices=("ollama", "lmstudio"), default="ollama")
    parser.add_argument("--model", required=True, help="Already installed local generation model")
    parser.add_argument("--endpoint", default="", help="Loopback OpenAI-compatible server URL")
    parser.add_argument("--limit", type=int, default=len(CASES), help="Number of cases to run (1-8)")
    parser.add_argument("--output", default="work/translation-quality-benchmark.json")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > len(CASES):
        parser.error(f"--limit must be 1..{len(CASES)}")
    endpoint = args.endpoint or (
        "http://localhost:11434/v1" if args.provider == "ollama" else "http://localhost:1234/v1")
    config = TranslationConfig(args.provider, source="auto", target="ko",
                               model=args.model, endpoint=endpoint)
    config.validate()
    report = run_benchmark(LyricsTranslationService(), config, cases=CASES[:args.limit])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, output)
    print(f"Saved {report['summary']['total_cases']} lyric QA cases to {output}")
    print("Structure/omission flags are heuristic; naturalness and faithfulness need human review.")
    return 0 if report["summary"]["failed_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
