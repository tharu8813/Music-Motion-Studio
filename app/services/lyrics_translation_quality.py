"""Conservative translation quality gates and consistent-refrain detection.

A valid JSON object does not prove that a translation is faithful or natural.
These guards catch specific failures without claiming semantic verification.
"""
from __future__ import annotations

from collections import defaultdict
import re
import unicodedata

from app.services.lyrics_translation_prompt import lyric_text

_WORDS = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
_KOREAN = re.compile(r"[\uac00-\ud7a3]")
_ENGLISH_CLUES = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "can",
    "come", "did", "do", "does", "for", "from", "have", "has", "had",
    "he", "her", "here", "his", "how", "i", "if", "in", "into", "is",
    "it", "just", "like", "love", "me", "my", "no", "not", "of",
    "on", "or", "our", "out", "she", "so", "still", "than", "that",
    "the", "their", "there", "they", "this", "through", "time",
    "to", "too", "up", "us", "was", "we", "were", "what", "when",
    "where", "who", "why", "will", "with", "would", "you", "your",
    "hello", "world", "again",
))


def normalized_source(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", lyric_text({"text": text})).split()).casefold()


def _english_words(text: str) -> list[str]:
    return _WORDS.findall(text)


def should_translate_english(source: str, source_language: str, target_language: str) -> bool:
    """Conservatively reject a false 'translation unnecessary' for English->Korean.

    Avoid classifying an all-capitals acronym, proper-name-only line, or
    intentional mixed Korean/English code-switch as definitely mistranslated.
    Explicit en-* directions may reject single-word English lyrics.
    """
    if target_language.casefold().split("-")[0] != "ko":
        return False
    words = _english_words(source)
    if not words or _KOREAN.search(source):
        return False
    if not any(len(word) > 1 and not word.isupper() for word in words):
        return False
    language = source_language.casefold().split("-")[0]
    if language == "en":
        return True
    if language != "auto":
        return False
    lower = [word.casefold() for word in words]
    return (bool(_ENGLISH_CLUES.intersection(lower)) or len(words) >= 3)


def _unexpected_han(source: str, translated: str) -> bool:
    def han(char: str) -> bool:
        return ("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
                or "\U00020000" <= char <= "\U0002fa1f")
    src = {char for char in source if han(char)}
    return any(han(char) and char not in src for char in translated)


def quality_issue(source: str, translated: str | None,
                  source_language: str, target_language: str) -> str | None:
    """A quality signal for targeted retry, not a quality score."""
    if target_language.casefold().split("-")[0] != "ko":
        return None
    if translated and _unexpected_han(source, translated):
        return "한국어 번역에 원문에 없는 한자가 섞였습니다. / Unexpected Han characters in Korean translation."
    if not should_translate_english(source, source_language, target_language):
        return None
    if not translated or normalized_source(translated) == normalized_source(source):
        return "영어 원문을 번역하지 않았습니다. / English lyric was left untranslated."
    # A model may echo the original English sentence instead of translating it.
    if not _KOREAN.search(translated):
        source_words = _english_words(source)
        translated_words = _english_words(translated)
        if source_words and translated_words and (
                len(translated_words) >= 2 or len(translated_words[0]) >= 4):
            return "한국어 대신 영어 문장이 반환됐습니다. / English was returned instead of Korean."
    return None


def stable_refrain_sources(cues: list[dict]) -> set[str]:
    """Only standardize exact repeated lines with substantial refrain evidence.

    An isolated word such as 'run' can change meaning; repeated neighboring
    lines or a recurring substantial phrase are stronger signals of a chorus.
    """
    texts = [normalized_source(lyric_text(cue)) for cue in cues]
    positions: dict[str, list[int]] = defaultdict(list)
    for i, source in enumerate(texts):
        if len(source) >= 7:
            positions[source].append(i)
    result = set()
    for source, indices in positions.items():
        if len(indices) < 2:
            continue
        if len(source) >= 20 and len(indices) >= 3:
            result.add(source)
            continue
        if len(source) < 12:
            continue
        for a in range(len(indices)):
            for b in range(a + 1, len(indices)):
                i, j = indices[a], indices[b]
                previous = (i > 0 and j > 0 and len(texts[i-1]) >= 7
                            and texts[i-1] == texts[j-1])
                following = (i+1 < len(texts) and j+1 < len(texts)
                             and len(texts[i+1]) >= 7 and texts[i+1] == texts[j+1])
                if previous or following:
                    result.add(source)
                    break
            if source in result:
                break
    return result


def rolling_translation_memory(history: list[tuple[int, str, str]], *,
                               max_chars: int = 1800, max_recent: int = 10) -> list[dict]:
    """Include previous translated lines, not only matching repeated originals.

    Retain a brief initial style anchor plus recent contiguous context. Never
    store this history outside the current translation request.
    """
    if not history:
        return []
    selected = history[-max_recent:]
    if len(history) > max_recent + 2:
        selected = history[:2] + selected
    seen, result, remaining = set(), [], max_chars
    for index, source, translated in reversed(selected):
        if index in seen or not translated:
            continue
        seen.add(index)
        size = len(source) + len(translated)
        if size > remaining:
            continue
        result.append({"id": index, "source_text": source, "text": translated})
        remaining -= size
    result.reverse()
    return result
