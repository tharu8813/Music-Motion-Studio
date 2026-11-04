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
    whole_song_context, LOCAL_SONG_CONTEXT_MAX_CHARS, SONG_CONTEXT_MAX_CHARS,
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
# Local 14B+ models may need minutes to load and generate an entire lyric batch.
# Cloud requests keep the shorter limit.
LOCAL_LLM_TRANSLATION_TIMEOUT = 240


class TranslationError(ValueError):
    """Translation error with validated rows that survived a partial batch."""

    def __init__(self, message, *, partial=None, diagnosed=False):
        super().__init__(message)
        self.partial = partial or {}
        self.diagnosed = diagnosed


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
                 cancel: Event | None = None, timeout=45):
    """Bounded requests; errors never expose keys, prompts or response bodies."""
    check_cancel(cancel)
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = (urlencode(body).encode() if form else json.dumps(body, ensure_ascii=False).encode())
        request_headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        with build_opener(_NoRedirect()).open(Request(url, data=data, headers=request_headers), timeout=timeout) as response:
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
        if urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"} and error.code >= 500:
            raise TranslationError(
                f"로컬 AI 서버가 모델 처리에 실패했습니다(HTTP {error.code}). "
                "Ollama/LM Studio 실행 상태와 사용 가능한 메모리를 확인하세요. / "
                f"Local AI server failed to process the model (HTTP {error.code}); "
                "check runtime logs and available memory.") from None
        messages = {
            400: "요청 설정·언어·모델을 확인하세요. / Check settings, language and model.",
            401: "로그인 또는 API 키를 확인하세요. / Check sign-in or API key.",
            403: "계정 권한·구독·서비스 이용 가능 여부를 확인하세요. / Check account permissions and service availability.",
            429: "사용량 한도에 도달했습니다. / Usage limit reached.",
            456: "번역 사용량 한도에 도달했습니다. / Translation quota exhausted.",
        }
        raise TranslationError(f"HTTP {error.code}: " + messages.get(error.code, "번역 서비스 요청에 실패했습니다. / Translation request failed.")) from None
    except (URLError, TimeoutError, OSError, HTTPException) as error:
        if urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}:
            reason = getattr(error, "reason", error)
            if isinstance(reason, TimeoutError):
                raise TranslationError(
                    f"로컬 LLM이 {timeout}초 안에 응답하지 않았습니다. 큰 모델의 첫 실행·CPU 처리·메모리 "
                    "사용량을 확인하세요. / Local LLM response timed out after "
                    f"{timeout}s; check model loading, CPU speed and available memory.") from None
            if isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) in {10061, 111}:
                raise TranslationError(
                    "로컬 LLM 서버 연결이 거부되었습니다. Ollama/LM Studio를 실행하고 서버 주소를 "
                    "확인하세요. / Local LLM connection refused; start the runtime and check its address.") from None
            raise TranslationError(
                "로컬 LLM 서버와 통신하다가 연결이 끊겼습니다. 서버 종료·메모리 부족·연결 상태를 "
                "확인하세요. / Local LLM connection failed; check runtime crashes, memory and connectivity.") from None
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

    def translate(self, cues: list[dict], config: TranslationConfig, cancel=None, progress=None, *, activity=None,
                  selected_rows=None) -> list[str]:
        check_cancel(cancel)
        config.validate()
        rows = self.prepare(cues)
        if selected_rows is not None:
            selected = set(selected_rows)
            if any(type(index) is not int or not 0 <= index < len(cues) for index in selected):
                raise TranslationError("재번역할 행을 확인하세요. / Check rows selected for retranslation.")
            rows = [(index, text) for index, text in rows if index in selected]
            if not rows:
                raise TranslationError("재번역할 가사 행이 없습니다. / No lyric rows selected for retranslation.")
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
                    song_context = whole_song_context(cues, {index for index, _text in batch},
                        max_chars=LOCAL_SONG_CONTEXT_MAX_CHARS if config.provider in LOCAL_LLM_PROVIDERS
                        else SONG_CONTEXT_MAX_CHARS)
                    translated = self._translate_batch(batch, config, cancel, activity=activity,
                        neighbors=neighboring_context(cues, batch[0][0], batch[-1][0]),
                        references=reference_translations(batch, completed),
                        song_context=song_context)
                else:
                    translated = self._translate_batch(batch, config, cancel)
            except TranslationError as error:
                # Preserve valid rows already returned in an otherwise invalid batch.
                valid_ids = {index for index, _text in batch}
                for index, original in batch:
                    if index not in error.partial:
                        continue
                    value = error.partial[index]
                    if value is not None and not isinstance(value, str):
                        continue
                    final = translation_text(original, value) if value is not None else ""
                    result[index] = final
                    if activity:
                        activity({"stage": "complete", "row": index, "text": final})
                if activity:
                    activity({"stage": "failure", "rows": sorted(valid_ids - set(error.partial)),
                        "reason": str(error)})
                    if config.provider in LOCAL_LLM_PROVIDERS and not error.diagnosed:
                        activity({"stage": "diagnostic", "provider": config.provider, "model": config.model,
                            "rows": sorted(valid_ids), "raw": "", "reason": str(error), "status": "failed"})
                raise
            except (KeyError, TypeError, AttributeError, IndexError):
                message = "번역 서비스 응답 형식이 올바르지 않습니다. / Invalid translation service response."
                if activity:
                    activity({"stage": "failure", "rows": [index for index, _text in batch], "reason": message})
                raise TranslationError(message) from None
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
        max_batch_rows = 1 if config.provider == "local_nmt" else 12 if config.provider in LOCAL_LLM_PROVIDERS else 20
        max_batch_chars = 1800 if config.provider in LOCAL_LLM_PROVIDERS else 4000
        for index, text in rows:
            check_cancel(cancel)
            if batch and (len(batch) >= max_batch_rows or length + len(text) > max_batch_chars):
                flush()
            batch.append((index, text))
            length += len(text)
        flush()
        check_cancel(cancel)
        return result

    def _translate_batch(self, rows, c, cancel, *, neighbors=None, references=None,
                         song_context=None, activity=None):
        texts = [text for _index, text in rows]
        if c.provider == "local_nmt":
            if self.local_engine is None:
                from app.services.local_translation_service import LocalTranslationEngine, LocalTranslationModels
                self.local_engine = LocalTranslationEngine(self.local_models or LocalTranslationModels())
            return self.local_engine.translate(texts, c, cancel)

        def req(url, body=None, headers=None, **kw):
            check_cancel(cancel)
            if c.provider in LOCAL_LLM_PROVIDERS:
                kw["timeout"] = LOCAL_LLM_TRANSLATION_TIMEOUT
            value = self.request(url, body, headers, cancel=cancel, **kw)
            check_cancel(cancel)
            return value

        if c.provider in LLM_PROVIDERS:
            instruction, prompt = build_translation_prompt(rows, source_language=c.source,
                target_language=c.target, preferences=c.instructions, neighbors=neighbors, references=references,
                song_context=song_context)
            headers = {"Authorization": "Bearer " + c.key} if c.key else {}
            raw = None

            def diagnostic(reason, status):
                if c.provider not in LOCAL_LLM_PROVIDERS or activity is None:
                    return
                # Volatile diagnostics only: no prompts, API keys or persistent log files.
                response = raw if isinstance(raw, str) else ""
                limit = 128 * 1024
                activity({"stage": "diagnostic", "provider": c.provider, "model": c.model,
                    "rows": [index for index, _text in rows], "raw": response[:limit],
                    "truncated": len(response) > limit, "reason": reason, "status": status})

            try:
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
                    body = {"model": c.model, "stream": False,
                        "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": prompt}]}
                    # Ollama's OpenAI-compatible endpoint supports JSON mode.
                    if c.provider == "ollama":
                        body["response_format"] = {"type": "json_object"}
                    data = req(base + "/chat/completions", body, headers)
                    choices = data.get("choices", [])
                    if not choices:
                        raise TranslationError("LLM이 응답 후보를 반환하지 않았습니다. / No completion choices returned.")
                    raw = choices[0].get("message", {}).get("content", "")
                    finish = choices[0].get("finish_reason")
                    if finish != "stop":
                        raise TranslationError(
                            f"LLM 생성이 정상 종료되지 않았습니다(finish_reason={finish!s}). / "
                            f"Generation stopped unexpectedly (finish_reason={finish!s}).")
                if not isinstance(raw, str):
                    raise TranslationError("LLM 응답이 문자열이 아닙니다. / LLM response content is not text.")
                cleaned = raw.strip()
                if cleaned.startswith("```"):
                    if "\n" not in cleaned:
                        raise TranslationError("마크다운 코드 블록이 닫히지 않았습니다. / Unclosed markdown code block.")
                    cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                try:
                    document = json.loads(cleaned)
                except json.JSONDecodeError as error:
                    raise TranslationError(
                        f"JSON 파싱 오류({error.lineno}행 {error.colno}열): {error.msg}. / "
                        f"Invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}.") from None
                if not isinstance(document, dict) or set(document) != {"translations"}:
                    raise TranslationError("JSON 최상위 객체에 translations 배열이 필요합니다. / Expected a translations array.")
                parsed = document["translations"]
                if not isinstance(parsed, list):
                    raise TranslationError("translations 값이 배열이 아닙니다. / translations must be an array.")
                mapping = {}
                for value in parsed:
                    if (not isinstance(value, dict) or set(value) != {"id", "text"}
                            or type(value.get("id")) is not int or not isinstance(value.get("text"), str)
                            or value["id"] in mapping):
                        raise TranslationError(
                            "JSON에 잘못된 ID·중복 ID·문자열이 아닌 번역문이 있습니다. / "
                            "Invalid or duplicate ID, or non-string translation text.")
                    mapping[value["id"]] = value["text"]
                expected = {index for index, _text in rows}
                if set(mapping) - expected:
                    raise TranslationError("요청하지 않은 가사 ID가 반환되었습니다. / Unexpected lyric IDs returned.")
                partial, invalid = {}, []
                for index, original in rows:
                    if index not in mapping:
                        invalid.append(index)
                        continue
                    translated = mapping[index].strip().replace("\r\n", "\n").replace("\r", "\n")
                    if not translated or not translation_text(original, translated):
                        partial[index] = None
                    elif ([bool(line.strip()) for line in translated.splitlines()]
                          != [bool(line.strip()) for line in original.splitlines()]):
                        invalid.append(index)
                    else:
                        partial[index] = translated
                if invalid:
                    numbers = ", ".join(str(index + 1) for index in invalid[:20])
                    raise TranslationError(
                        f"번역 결과 누락 또는 줄바꿈 불일치: 가사 {numbers}번. / "
                        f"Missing translations or incorrect line breaks: lyric rows {numbers}.", partial=partial)
                diagnostic("JSON 형식 검증 통과 / Valid JSON response", "success")
                return [partial[index] for index, _text in rows]
            except TranslationError as error:
                diagnostic(str(error), "failed")
                error.diagnosed = c.provider in LOCAL_LLM_PROVIDERS and activity is not None
                raise
            except (KeyError, TypeError, AttributeError, IndexError, ValueError) as error:
                message = "LLM 응답 구조를 읽지 못했습니다. / Invalid LLM response structure."
                diagnostic(f"{message} ({type(error).__name__})", "failed")
                raise TranslationError(message, diagnosed=True) from None
        raise TranslationError("지원하지 않는 번역 서비스입니다. / Unsupported translation provider.")
