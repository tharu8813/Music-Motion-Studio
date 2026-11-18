"""Provider-neutral structured output schemas for the two lyric LLM stages.

Schemas constrain JSON shape only. The existing ID, newline and translation
quality checks remain authoritative after a model has responded.
"""
from __future__ import annotations

import json


def _strict_object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def schema_for_prompt(prompt: str) -> dict | None:
    """Return an explicit JSON schema for genuine lyric translation payloads."""
    try:
        payload = json.loads(prompt)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("stage") == "song_analysis" and "full_song" in payload:
        glossary = _strict_object({"source": {"type": "string"}, "target": {"type": "string"}})
        plan = _strict_object({
            "story": {"type": "string"}, "tone": {"type": "string"},
            "voice": {"type": "string"}, "style": {"type": "string"},
            "refrains": {"type": "array", "items": {"type": "string"}},
            "glossary": {"type": "array", "items": glossary},
        })
        return _strict_object({"plan": plan})
    rows = payload.get("lyrics")
    if not isinstance(rows, list) or not rows or any(
            not isinstance(row, dict) or type(row.get("id")) is not int
            for row in rows):
        return None
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        return None
    entry = _strict_object({
        "id": {"type": "integer", "enum": ids},
        "text": {"type": "string"},
    })
    return _strict_object({"translations": {
        "type": "array", "items": entry, "minItems": len(ids), "maxItems": len(ids),
    }})
