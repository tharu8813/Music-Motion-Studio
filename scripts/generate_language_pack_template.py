"""Generate bundled Korean, English, and blank language-pack JSON files."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from string import Formatter
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"
RESOURCE_ROOT = APP_ROOT / "resources"
ASCII_TEXT = re.compile(r"[A-Za-z]")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            nodes.add(id(body[0].value))
    return nodes


def _expression_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        return _expression_name(node.value) or "value"
    return "value"


def _joined_string(node: ast.JoinedStr) -> str:
    parts: list[str] = []
    used: dict[str, int] = {}
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
            continue
        if not isinstance(value, ast.FormattedValue):
            continue
        base = re.sub(r"[^A-Za-z0-9_]", "_", _expression_name(value.value)) or "value"
        count = used.get(base, 0)
        used[base] = count + 1
        name = base if count == 0 else f"{base}_{count + 1}"
        conversion = f"!{chr(value.conversion)}" if value.conversion >= 0 else ""
        format_spec = ""
        if value.format_spec is not None:
            format_spec = f":{_joined_string(value.format_spec)}"
        parts.append(f"{{{name}{conversion}{format_spec}}}")
    return "".join(parts)


def _node_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return _joined_string(node)
    return None


def _is_translatable(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > 20_000 or not ASCII_TEXT.search(text):
        return False
    try:
        tuple(Formatter().parse(text))
    except ValueError:
        return False
    return True


def _same_placeholders(source: str, translation: str) -> bool:
    try:
        source_fields = {field for _, field, _, _ in Formatter().parse(source) if field}
        translated_fields = {
            field for _, field, _, _ in Formatter().parse(translation) if field
        }
    except ValueError:
        return False
    return source_fields == translated_fields


def _korean_test_direction(node: ast.AST) -> bool | None:
    """Return whether the true branch represents Korean, when recognizable."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        direction = _korean_test_direction(node.operand)
        return None if direction is None else not direction
    text = ast.unparse(node)
    if re.search(r"\b(?:korean|is_korean)\b", text, re.IGNORECASE):
        return True
    return None


def collect_literals() -> tuple[dict[str, list[str]], dict[str, str]]:
    occurrences: dict[str, set[str]] = {}
    korean_overrides: dict[str, str] = {}
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            text: str | None = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = _joined_string(node)
            if text is not None and _is_translatable(text):
                occurrences.setdefault(text, set()).add(relative)

            english: str | None = None
            korean: str | None = None
            if isinstance(node, ast.IfExp):
                direction = _korean_test_direction(node.test)
                if direction is True:
                    korean = _node_text(node.body)
                    english = _node_text(node.orelse)
                elif direction is False:
                    english = _node_text(node.body)
                    korean = _node_text(node.orelse)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "literal" and len(node.args) >= 2:
                    english = _node_text(node.args[0])
                    korean = _node_text(node.args[1])
            if (english and korean and _is_translatable(english)
                    and _same_placeholders(english, korean)):
                korean_overrides.setdefault(english, korean)

    normalized = {text: sorted(paths) for text, paths in sorted(occurrences.items())}
    return normalized, korean_overrides


def built_in_strings(locale: str) -> dict[str, str]:
    from app.utils.i18n import Language, _TEXT

    language = Language.KOREAN if locale == "ko" else Language.ENGLISH
    return {key: values[language] for key, values in sorted(_TEXT.items())}


def _metadata(locale: str, name: str, native_name: str) -> dict[str, str]:
    return {
        "locale": locale,
        "name": name,
        "native_name": native_name,
        "author": "Music Motion Studio",
        "version": "1.0.2",
        "minimum_app_version": "1.0.2",
    }


def _write(name: str, payload: dict[str, object]) -> None:
    path = RESOURCE_ROOT / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}")


def main() -> None:
    occurrences, korean_matches = collect_literals()
    english_overrides = {text: text for text in occurrences}
    korean_overrides = {
        text: korean_matches.get(text, text)
        for text in occurrences
    }
    blank_overrides = {text: "" for text in occurrences}

    common = {"schema_version": 1, "built_in": True}
    _write("en.json", {
        **common,
        "metadata": _metadata("en", "English", "English"),
        "strings": built_in_strings("en"),
        "overrides": english_overrides,
    })
    _write("ko.json", {
        **common,
        "metadata": _metadata("ko", "Korean", "한국어"),
        "strings": built_in_strings("ko"),
        "overrides": korean_overrides,
    })
    _write("language-pack-template.json", {
        "schema_version": 1,
        "metadata": {
            "locale": "xx-XX",
            "name": "",
            "native_name": "",
            "author": "",
            "version": "1.0.0",
            "minimum_app_version": "1.0.2",
        },
        "instructions": {
            "en": "Copy this file, rename it to a locale such as fr-FR.json, and fill only the empty values.",
            "ko": "이 파일을 복사해 fr-FR.json 같은 로캘 이름으로 변경한 뒤 빈 값만 번역하세요.",
        },
        "strings": {key: "" for key in built_in_strings("en")},
        "overrides": blank_overrides,
        "source_references": occurrences,
    })
    print(
        f"Generated 30 stable keys and {len(occurrences)} literals; "
        f"found {len(korean_matches)} explicit Korean/English pairs."
    )


if __name__ == "__main__":
    main()
