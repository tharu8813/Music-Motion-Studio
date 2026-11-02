"""Read-only model discovery for API credentials; never sends lyric content."""
from app.services.lyrics_translation_service import TranslationError


def available_models(request, config, cancel):
    if not config.key.strip():
        raise TranslationError("API 키를 입력한 뒤 연결을 테스트하세요. / Enter an API key first.")
    if config.provider == "gemini":
        data = request("https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": config.key}, cancel=cancel)
        return [(row["name"].removeprefix("models/"), row.get("displayName", row["name"]))
                for row in data.get("models", [])
                if "generateContent" in row.get("supportedGenerationMethods", [])]
    if config.provider == "claude":
        data = request("https://api.anthropic.com/v1/models",
            headers={"x-api-key": config.key, "anthropic-version": "2023-06-01"}, cancel=cancel)
    else:
        host = {"openai": "https://api.openai.com/v1", "grok": "https://api.x.ai/v1"}[config.provider]
        data = request(host + "/models", headers={"Authorization": "Bearer " + config.key}, cancel=cancel)
    return [(row["id"], row.get("display_name", row["id"])) for row in data.get("data", [])]
