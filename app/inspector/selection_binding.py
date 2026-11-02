"""Shared property access and batched edits, independent of inspector layout."""

from dataclasses import replace


def read_property(source, path):
    value = source
    for key in path.split("."):
        value = getattr(value, key)
    return value


class SelectionPropertyBinding:
    def __init__(self, store):
        self.store = store

    def apply(self, identifiers, changes):
        from app.models.source import SourceType
        for identifier in identifiers:
            source = self.store.get(identifier)
            if source is None or source.locked:
                continue
            updates = dict(changes)
            nested = {}
            for path in list(updates):
                if "." in path:
                    container, field = path.split(".", 1)
                    nested.setdefault(container, {})[field] = updates.pop(path)
            for container, values in nested.items():
                updates[container] = replace(getattr(source, container), **values)
            if source.source_type is SourceType.LYRICS and "text" in updates and updates["text"] != source.text:
                updates["subtitle_fallback"] = updates["text"]
            self.store.update(identifier, **updates)
