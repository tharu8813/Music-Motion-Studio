"""Detached, resumable translation drafts. Credentials never enter this store."""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json

from PySide6.QtCore import QObject, Signal

from app.services.lyrics_translation_service import TranslationConfig, approved_translation


@dataclass
class DraftRow:
    text: str = ""
    checked: bool = True
    status: str = "ready"
    detail: str = ""
    config: TranslationConfig | None = None
    edited: bool = False


class TranslationDraft(QObject):
    changed = Signal()

    def __init__(self, cues, settings, *, identity="", parent=None):
        super().__init__(parent)
        self.cues, self.settings = deepcopy(cues), settings
        digest = hashlib.sha256(json.dumps([identity, cues], ensure_ascii=False,
            sort_keys=True).encode("utf-8")).hexdigest()
        self.key = "translation/drafts/" + digest
        self.rows = []
        self.has_result = False
        for cue in cues:
            text = approved_translation(cue)
            metadata = cue.get("translation", {}) if text else {}
            config = TranslationConfig(metadata.get("provider", "manual"),
                target=metadata.get("language", "ko"), model=metadata.get("model", "")) if text else None
            self.rows.append(DraftRow(text, bool(str(cue.get("text", "")).strip()),
                "saved" if text else "ready", config=config))
            self.has_result |= bool(text)
        self.restored = self.restore()

    @staticmethod
    def safe_config(config):
        # Keys, endpoint credentials and prompts are not part of a lyric draft.
        return replace(config, key="", endpoint="", instructions="") if config else None

    def save(self):
        if not self.has_result and not any(row.status == "failed" for row in self.rows):
            return
        payload = {"version": 1, "has_result": self.has_result, "rows": []}
        for row in self.rows:
            value = asdict(row)
            value["config"] = asdict(self.safe_config(row.config)) if row.config else None
            payload["rows"].append(value)
        self.settings.setValue(self.key, json.dumps(payload, ensure_ascii=False))
        self.settings.sync()

    def restore(self):
        try:
            value = json.loads(str(self.settings.value(self.key, "{}")))
            if value.get("version") != 1 or len(value["rows"]) != len(self.rows):
                return False
            restored = []
            for raw in value["rows"]:
                row = DraftRow(**raw)
                if not isinstance(row.text, str) or row.status not in {
                        "ready", "saved", "complete", "unneeded", "incomplete", "failed", "edited"}:
                    return False
                row.config = self.safe_config(TranslationConfig(**row.config)) if row.config else None
                restored.append(row)
            self.rows, self.has_result = restored, bool(value.get("has_result", True))
            return True
        except (ValueError, TypeError, KeyError):
            return False

    def clear(self):
        self.settings.remove(self.key)
        self.settings.sync()
