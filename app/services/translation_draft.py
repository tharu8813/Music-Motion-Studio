"""Detached, resumable translation drafts. Credentials never enter this store."""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from time import time

from PySide6.QtCore import QObject, Signal

from app.services.lyrics_translation_service import TranslationConfig, TranslationError, approved_translation


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
        self.owner = hashlib.sha256(identity.encode("utf-8")).hexdigest() if identity else ""
        self.identity = identity
        digest = self._fingerprint(cues)
        self.base_hash = digest
        self.source_hash = self._fingerprint([{k: v for k, v in cue.items() if k != "translation"} for cue in cues])
        self.applied_hash = ""
        self.key = "translation/drafts/" + digest
        self.rows = []
        self.has_result = False
        self.dirty = False
        for cue in cues:
            text = approved_translation(cue)
            metadata = cue.get("translation", {}) if text else {}
            config = TranslationConfig(metadata.get("provider", "manual"),
                target=metadata.get("language", "ko"), model=metadata.get("model", "")) if text else None
            self.rows.append(DraftRow(text, bool(str(cue.get("text", "")).strip()),
                "saved" if text else "ready", config=config))
            self.has_result |= bool(text)
        self._original_rows = deepcopy(self.rows)
        self._original_has_result = self.has_result
        self.restored = self.restore()

    @staticmethod
    def safe_config(config):
        # Keys, endpoint credentials and prompts are not part of a lyric draft.
        return replace(config, key="", endpoint="", instructions="") if config else None

    def _fingerprint(self, cues):
        return hashlib.sha256(json.dumps([self.identity, cues], ensure_ascii=False,
            sort_keys=True).encode("utf-8")).hexdigest()

    def record_applied(self, cues):
        self.applied_hash = self._fingerprint(cues)
        self.dirty = True

    def save(self):
        if not self.dirty:
            return False
        payload = {"version": 2, "owner": self.owner, "saved_at": time(),
                   "source_hash": self.source_hash, "base_hash": self.base_hash, "applied_hash": self.applied_hash,
                   "has_result": self.has_result, "rows": []}
        for row in self.rows:
            value = asdict(row)
            value["config"] = asdict(self.safe_config(row.config)) if row.config else None
            payload["rows"].append(value)
        self.settings.setValue(self.key, json.dumps(payload, ensure_ascii=False))
        self._sync()
        return True

    def restore(self):
        try:
            value = self._recovery_payload()
            if not isinstance(value, dict) or value.get("version") not in {1, 2} or len(value["rows"]) != len(self.rows):
                return False
            restored = []
            for raw in value["rows"]:
                row = DraftRow(**raw)
                if not isinstance(row.text, str) or row.status not in {
                        "ready", "saved", "complete", "unneeded", "incomplete", "failed", "edited"}:
                    return False
                if (type(row.checked) is not bool or type(row.edited) is not bool
                        or not isinstance(row.detail, str)):
                    return False
                row.config = self.safe_config(TranslationConfig(**row.config)) if row.config else None
                restored.append(row)
            self.rows, self.has_result = restored, bool(value.get("has_result", True))
            self.applied_hash = str(value.get("applied_hash", ""))
            self.dirty = True
            return True
        except (ValueError, TypeError, KeyError):
            return False

    def _recovery_payload(self):
        """Follow only known staging transitions, never unrelated saved translations."""
        try:
            exact = json.loads(str(self.settings.value(self.key, "{}")))
        except (ValueError, TypeError):
            exact = {}
        if not self.owner:
            return exact
        records = []
        for key in self._stored_keys():
            try:
                value = json.loads(str(self.settings.value(key, "{}")))
                if (isinstance(value, dict) and value.get("owner") == self.owner
                        and value.get("source_hash") == self.source_hash
                        and isinstance(value.get("base_hash"), str)
                        and isinstance(value.get("applied_hash"), str)):
                    records.append(value)
            except (ValueError, TypeError):
                continue
        reachable = {self.base_hash}
        for _ in records:
            before = len(reachable)
            for value in records:
                base, applied = value.get("base_hash"), value.get("applied_hash")
                if isinstance(base, str) and isinstance(applied, str) and applied and ({base, applied} & reachable):
                    reachable.update((base, applied))
            if len(reachable) == before:
                break
        eligible = [value for value in records if value.get("base_hash") in reachable]
        if isinstance(exact, dict) and exact.get("version") in {1, 2}:
            eligible.append(exact)
        def saved_at(value):
            stamp = value.get("saved_at", 0)
            return stamp if isinstance(stamp, (int, float)) else 0
        return max(eligible, key=saved_at) if eligible else exact

    def _stored_keys(self):
        self.settings.beginGroup("translation/drafts")
        names = self.settings.childKeys()
        self.settings.endGroup()
        return ["translation/drafts/" + name for name in names]

    def clear(self):
        """Delete only this snapshot. Applying lyrics must never call this."""
        self.settings.remove(self.key)
        self._sync()

    def discard(self):
        """Explicitly remove this track's recovery snapshots and reset the editor."""
        keys = [self.key]
        if self.owner:
            for key in self._stored_keys():
                try:
                    value = json.loads(str(self.settings.value(key, "{}")))
                    if isinstance(value, dict) and value.get("owner") == self.owner:
                        keys.append(key)
                except (ValueError, TypeError):
                    continue
        for key in set(keys):
            self.settings.remove(key)
        self._sync()
        self.rows = deepcopy(self._original_rows)
        self.has_result = self._original_has_result
        self.dirty, self.restored = False, False
        self.applied_hash = ""

    def _sync(self):
        self.settings.sync()
        if self.settings.status() != self.settings.Status.NoError:
            raise TranslationError("번역 초안을 저장하거나 삭제할 수 없습니다. 설정 폴더 권한을 확인하세요. / Cannot save or delete draft; check settings permissions.")
