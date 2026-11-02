"""Translation adapters and detached, explicitly reviewed lyric translations.

No provider credentials belong in a project. Network calls are made only by the
translation dialog's worker, never by playback or export.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from http.client import HTTPException
import json
import re
from threading import Event
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.services.lyrics_translation_prompt import (
    build_translation_prompt, lyric_text, neighboring_context, reference_translations,
)


PROVIDERS = {
    "chatgpt": "OpenAI · ChatGPT",
    "openai": "OpenAI · API",
    "claude": "Anthropic · Claude",
    "gemini": "Google · Gemini",
    "grok": "xAI · Grok",
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "local_nmt": "Argos · Local translation",
}
LOCAL_LLM_PROVIDERS = frozenset({"ollama", "lmstudio"})
CLOUD_LLM_PROVIDERS = frozenset({"chatgpt", "openai", "claude", "gemini", "grok"})
LLM_PROVIDERS = CLOUD_LLM_PROVIDERS | LOCAL_LLM_PROVIDERS
LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "local_nmt"})
MAX_RESPONSE = 8 * 1024 * 1024


class TranslationError(ValueError):
    pass


class TranslationCancelled(Exception):
    pass


@dataclass(frozen=True)
class IncompleteTranslation:
    """A local model could not translate these physical lines; never a no-op."""
    text: str
    missing_lines: tuple[int, ...]


def check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise TranslationCancelled()


def same_language(source: str, target: str) -> bool:
    """Keep regional/script variants distinct; never treat auto as a language."""
    return isinstance(source, str) and source.lower() != "auto" and source.casefold() == target.casefold()


def translation_text(original: str, translated: str) -> str:
    """Suppress duplicates, including newline, whitespace and Unicode variants."""
    translated = lyric_text({"text": translated})
    comparable = lambda value: " ".join(unicodedata.normalize("NFC", lyric_text({"text": value})).split())
    return "" if comparable(original) == comparable(translated) else translated


@dataclass(frozen=True)
class TranslationConfig:
    provider: str
    target: str = "ko"
    source: str = "auto"
    model: str = ""
    key: str = field(default="", repr=False)
    endpoint: str = ""
    instructions: str = ""

    @property
    def same_language(self) -> bool:
        return same_language(self.source, self.target)

    def validate(self) -> None:
        if self.provider not in PROVIDERS:
            raise TranslationError("지원하지 않는 번역 서비스입니다. / Unsupported translation provider.")
        language = r"[a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{1,8})*"
        if (len(self.target) > 64 or not re.fullmatch(language, self.target) or self.target.lower() == "auto"
                or len(self.source) > 64 or not (self.source == "auto" or re.fullmatch(language, self.source))):
            raise TranslationError("언어 코드를 확인하세요(예: ko, en, ja, zh-Hant). 자동 감지는 원문에만 사용할 수 있습니다. / Check language codes; auto is only valid for the source.")
        if self.same_language:
            return  # No provider request or credentials are needed for this pair.
        if not self.model.strip():
            raise TranslationError("모델을 선택하거나 모델 ID를 입력하세요. / Select or enter a model ID.")
        if self.provider not in LOCAL_PROVIDERS and not self.key.strip():
            raise TranslationError("로그인하거나 API 키를 입력하세요. / Sign in or enter an API key.")
        if self.provider == "local_nmt" and self.source == "auto":
            raise TranslationError("선택한 로컬 모델의 원문 언어를 지정하세요. / Select the local model's source language.")
        if self.provider in {"ollama", "lmstudio"}:
            from app.services.local_llm_service import local_base, validate_local_model
            local_base(self.endpoint)
            validate_local_model(self.model)


def validate_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        parsed.port  # Validate malformed/out-of-range ports before starting a request.
    except ValueError:
        raise TranslationError("서버 주소와 포트 번호를 확인하세요. / Check server URL and port.") from None
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in endpoint)
            or parsed.scheme not in {"http", "https"}
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise TranslationError("로컬 주소(http://localhost…) 또는 HTTPS 서버 주소를 입력하세요. / Use a loopback HTTP or HTTPS server URL.")
    return endpoint.rstrip("/")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a provider key to another host.


def request_json(url: str, body=None, headers=None, *, form=False, stream=False,
                 cancel: Event | None = None):
    """Bounded requests; errors never expose keys, prompts or response bodies."""
    check_cancel(cancel)
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = (urlencode(body).encode() if form else json.dumps(body, ensure_ascii=False).encode())
        request_headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        with build_opener(_NoRedirect()).open(Request(url, data=data, headers=request_headers), timeout=45) as response:
            if stream:
                chunks, completed, total = [], False, 0
                for raw in response:
                    check_cancel(cancel)
                    total += len(raw)
                    if total > MAX_RESPONSE:
                        raise TranslationError("번역 응답이 너무 큽니다. / Translation response is too large.")
                    if not raw.startswith(b"data:"):
                        continue
                    event_data = raw[5:].strip()
                    if event_data == b"[DONE]":
                        continue
                    event = json.loads(event_data)
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        chunks.append(event.get("delta", ""))
                    elif kind == "response.completed":
                        completed = True
                        break
                    elif kind in {"error", "response.failed", "response.incomplete", "response.refusal.delta"}:
                        error = event.get("error") or (event.get("response") or {}).get("error") or {}
                        if error.get("code") in {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"}:
                            raise TranslationError("ChatGPT 구독 사용 한도 또는 공유 설정을 확인하세요. / Check ChatGPT plan usage limits and sharing settings.")
                        raise TranslationError("AI가 번역을 완료하지 못했습니다. / AI did not complete the translation.")
                check_cancel(cancel)
                if not completed:
                    raise TranslationError("번역 응답이 중단되었습니다. / Translation stream ended early.")
                return "".join(chunks)
            raw = response.read(MAX_RESPONSE + 1)
            check_cancel(cancel)
            if len(raw) > MAX_RESPONSE:
                raise TranslationError("번역 응답이 너무 큽니다. / Translation response is too large.")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as error:
        messages = {
            400: "요청 설정·언어·모델을 확인하세요. / Check settings, language and model.",
            401: "로그인 또는 API 키를 확인하세요. / Check sign-in or API key.",
            403: "계정 권한·구독·서비스 이용 가능 여부를 확인하세요. / Check account permissions and service availability.",
            429: "사용량 한도에 도달했습니다. / Usage limit reached.",
            456: "번역 사용량 한도에 도달했습니다. / Translation quota exhausted.",
        }
        raise TranslationError(f"HTTP {error.code}: " + messages.get(error.code, "번역 서비스 요청에 실패했습니다. / Translation request failed.")) from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise TranslationError("번역 서비스에 연결할 수 없습니다. / Cannot connect to the translation service.") from None
    except (UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
        raise TranslationError("번역 서비스 응답을 읽을 수 없습니다. / Invalid translation response.") from None


def approved_translation(cue: dict) -> str:
    value = cue.get("translation")
    if (isinstance(value, dict) and value.get("approved") is True
            and isinstance(value.get("source_text"), str) and isinstance(cue.get("text", ""), str)
            and value["source_text"].replace("\\n", "\n").replace("\r\n", "\n")
                == cue.get("text", "").replace("\\n", "\n").replace("\r\n", "\n")
            and isinstance(value.get("text"), str)):
        return translation_text(cue.get("text", ""), value["text"])
    return ""


def display_cue(cue: dict) -> dict:
    """Append only in the display projection; negative IDs reserve translation styles."""
    translated = approved_translation(cue)
    if not translated:
        return cue
    text = str(cue.get("text", "")).replace("\\n", "\n").strip()
    originals = [line for line in text.splitlines() if line.strip()]
    translations = [line for line in translated.splitlines() if line.strip()]
    translation_index = -2 if cue["translation"].get("provider") == "papago" else -1
    return {**cue, "text": text + "\n" + "\n".join(translations),
            "display_line_positions": tuple(range(len(originals))) + (translation_index,) * len(translations)}


def validate_translation(value) -> None:
    if (not isinstance(value, dict)
            or not set(value) <= {"text", "source_text", "language", "provider", "model", "approved"}
            or not all(isinstance(value.get(key), str) for key in ("text", "source_text", "language", "provider", "model"))
            or not isinstance(value.get("approved"), bool)
            or any(len(value[key]) > 64_000 for key in ("text", "source_text"))):
        raise ValueError("Invalid lyric translation data.")


def apply_translations(cues: list[dict], texts: list[str | None], config: TranslationConfig) -> list[dict]:
    """Only checked review rows are applied. None preserves a previous translation."""
    if len(cues) != len(texts):
        raise TranslationError("번역 줄 수가 원문과 다릅니다. / Translation row count mismatch.")
    result = deepcopy(cues)
    for cue, text in zip(result, texts):
        if text is None:
            continue
        text = translation_text(cue.get("text", ""), text)
        if not text:
            cue.pop("translation", None)
            continue
        previous = cue.get("translation", {})
        if (approved_translation(cue) == text.strip() and previous.get("provider") == config.provider
                and previous.get("language") == config.target and previous.get("model") == config.model):
            continue  # Preserve provenance when reviewing an unchanged translation.
        value = {"text": text.strip(), "source_text": cue.get("text", ""), "language": config.target,
                 "provider": config.provider, "model": config.model, "approved": True}
        try:
            validate_translation(value)
        except ValueError:
            raise TranslationError("번역문이 너무 길거나 형식이 올바르지 않습니다. / Translation is too long or invalid.") from None
        cue["translation"] = value
    return result


class LyricsTranslationService:
    """One result per cue; bounded batches preserve IDs, timings and word offsets."""

    def __init__(self, transport=request_json, *, local_models=None, local_engine=None):
        self.request = transport
        self.local_models, self.local_engine = local_models, local_engine

    @staticmethod
    def prepare(cues: list[dict]) -> list[tuple[int, str]]:
        """Validate every cue before sending any potentially billable request."""
        rows = []
        for index, cue in enumerate(cues):
            text = lyric_text(cue)
            if len(text) > 4000:
                raise TranslationError(f"{index + 1}번째 가사가 너무 깁니다(최대 4,000자). / Lyric row {index + 1} exceeds 4,000 characters.")
            if text:
                rows.append((index, text))
        if not rows:
            raise TranslationError("번역할 가사가 없습니다. 먼저 원문 가사를 추가하세요. / Add original lyrics before translating.")
        return rows

    def translate(self, cues: list[dict], config: TranslationConfig, cancel=None, progress=None, *, activity=None) -> list[str]:
        check_cancel(cancel)
        config.validate()
        rows = self.prepare(cues)
        result = [""] * len(cues)
        if config.same_language:
            if activity:
                for index, _text in rows:
                    activity({"stage": "complete", "row": index, "text": ""})
            if progress:
                progress(len(rows), len(rows))
            return result
        completed: dict[str, tuple[int, str]] = {}
        batch, length = [], 0
        processed = 0
        if progress:
            progress(0, len(rows))
        def flush():
            nonlocal batch, length, processed
            if not batch:
                return
            check_cancel(cancel)
            if activity:
                activity({"stage": "working", "rows": [index for index, _text in batch]})
            try:
                if config.provider in LLM_PROVIDERS:
                    translated = self._translate_batch(batch, config, cancel,
                        neighbors=neighboring_context(cues, batch[0][0], batch[-1][0]),
                        references=reference_translations(batch, completed))
                else:
                    translated = self._translate_batch(batch, config, cancel)
            except (KeyError, TypeError, AttributeError, IndexError):
                raise TranslationError("번역 서비스 응답 형식이 올바르지 않습니다. / Invalid translation service response.") from None
            # Only the in-process engine can explicitly mark a missing result.
            # Keep malformed/empty responses from other providers as errors.
            if len(translated) != len(batch) or not all(t is None or isinstance(t, str) and t.strip()
                    or config.provider == "local_nmt" and isinstance(t, IncompleteTranslation) for t in translated):
                raise TranslationError("번역 누락 또는 줄 수 오류입니다. 다시 번역하세요. / Missing or mismatched translation rows.")
            for (index, original), text in zip(batch, translated):
                missing = text.missing_lines if isinstance(text, IncompleteTranslation) else ()
                if isinstance(text, IncompleteTranslation):
                    text = text.text
                result[index] = translation_text(original, text) if text is not None else ""
                if config.provider in LLM_PROVIDERS and result[index]:
                    completed[original] = (index, result[index])
                if activity:
                    event = {"stage": "complete", "row": index, "text": result[index]}
                    if missing:
                        event["missing_lines"] = missing
                    activity(event)
            processed += len(batch)
            if progress:
                progress(processed, len(rows))
            batch, length = [], 0
        for index, text in rows:
            check_cancel(cancel)
            if batch and (len(batch) >= (1 if config.provider == "local_nmt" else 20) or length + len(text) > 4000):
                flush()
            batch.append((index, text))
            length += len(text)
        flush()
        check_cancel(cancel)
        return result

    def _translate_batch(self, rows, c, cancel, *, neighbors=None, references=None):
        texts = [text for _index, text in rows]
        if c.provider == "local_nmt":
            if self.local_engine is None:
                from app.services.local_translation_service import LocalTranslationEngine, LocalTranslationModels
                self.local_engine = LocalTranslationEngine(self.local_models or LocalTranslationModels())
            return self.local_engine.translate(texts, c, cancel)
        def req(url, body=None, headers=None, **kw):
            check_cancel(cancel)
            value = self.request(url, body, headers, cancel=cancel, **kw)
            check_cancel(cancel)
            return value
        if c.provider in LLM_PROVIDERS:
            instruction, prompt = build_translation_prompt(rows, source_language=c.source,
                target_language=c.target, preferences=c.instructions, neighbors=neighbors, references=references)
            headers = {"Authorization": "Bearer " + c.key} if c.key else {}
            if c.provider in {"chatgpt", "openai"}:
                raw = req("https://api.openai.com/v1/responses", {"model": c.model, "instructions": instruction,
                    "input": [{"role": "user", "content": prompt}], "store": False, "stream": True}, headers, stream=True)
            elif c.provider == "claude":
                data = req("https://api.anthropic.com/v1/messages", {"model": c.model, "max_tokens": 8192,
                    "system": instruction, "messages": [{"role": "user", "content": prompt}]},
                    {"x-api-key": c.key, "anthropic-version": "2023-06-01"})
                if data.get("stop_reason") not in {"end_turn", "stop_sequence"}:
                    raise TranslationError("Claude 번역이 완료되지 않았습니다. / Claude translation is incomplete.")
                raw = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
            elif c.provider == "gemini":
                data = req("https://generativelanguage.googleapis.com/v1beta/models/" + quote(c.model, safe="") + ":generateContent",
                    {"systemInstruction": {"parts": [{"text": instruction}]},
                     "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                     "generationConfig": {"responseMimeType": "application/json"}}, {"x-goog-api-key": c.key})
                candidates = data.get("candidates", [])
                if not candidates or candidates[0].get("finishReason") != "STOP":
                    raise TranslationError("Gemini 번역이 완료되지 않았습니다. / Gemini translation is incomplete.")
                raw = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []) if not p.get("thought"))
            else:
                base = "https://api.x.ai/v1" if c.provider == "grok" else validate_endpoint(c.endpoint)
                data = req(base + "/chat/completions", {"model": c.model, "stream": False,
                    "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": prompt}]}, headers)
                choices = data.get("choices", [])
                if not choices or choices[0].get("finish_reason") != "stop":
                    raise TranslationError("AI 번역이 완료되지 않았습니다. / AI translation is incomplete.")
                raw = choices[0].get("message", {}).get("content", "")
            try:
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
                document = json.loads(raw)
                if not isinstance(document, dict) or set(document) != {"translations"}:
                    raise ValueError()
                parsed = document["translations"]
                if not isinstance(parsed, list):
                    raise ValueError()
                mapping = {}
                for value in parsed:
                    if (not isinstance(value, dict) or set(value) != {"id", "text"}
                            or not isinstance(value["id"], int) or isinstance(value["id"], bool)
                            or value["id"] in mapping or not isinstance(value["text"], str)):
                        raise ValueError()
                    mapping[value["id"]] = value["text"]
                if set(mapping) != {i for i, _text in rows}:
                    raise ValueError()
                for index, original in rows:
                    translated = mapping[index].strip().replace("\r\n", "\n").replace("\r", "\n")
                    if not translated or not translation_text(original, translated):
                        mapping[index] = None  # Prompt contract: the entire cue needs no translation.
                        continue
                    if ([bool(line.strip()) for line in translated.splitlines()]
                            != [bool(line.strip()) for line in original.splitlines()]):
                        raise TranslationError("AI 번역이 원문의 줄바꿈을 변경했습니다. 다시 번역하세요. / AI changed lyric line breaks; translate again.")
                    mapping[index] = translated
                return [mapping[i] for i, _text in rows]
            except TranslationError:
                raise
            except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                raise TranslationError("AI 번역의 줄 연결 정보를 확인할 수 없습니다. / AI returned invalid lyric row IDs.") from None
        raise TranslationError("지원하지 않는 번역 서비스입니다. / Unsupported translation provider.")
