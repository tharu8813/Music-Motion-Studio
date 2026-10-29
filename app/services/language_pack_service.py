"""Validation and storage for data-only external JSON language packs."""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys

from PySide6.QtCore import QStandardPaths

from app import __version__
from app.services.update_service import normalized_version


LOGGER = logging.getLogger(__name__)
LANGUAGE_PACK_SCHEMA = 1
# The blank template alone is ~0.9 MB (overrides plus source_references), so a
# fully translated copy -- exactly what its instructions ask for -- passed the
# old 1 MB cap. 4 MB leaves room for 3-byte scripts and a growing template.
MAX_PACK_BYTES = 4 * 1024 * 1024
# The generated full-program template now contains slightly more than 5,000
# literals.  Keep a generous but bounded ceiling so complete community packs
# remain valid without weakening the file-size and text-length safeguards.
MAX_TRANSLATIONS = 10_000
MAX_TEXT_LENGTH = 20_000
TEMPLATE_FILE_NAME = "language-pack-template.json"
BUILT_IN_LANGUAGE_FILES = ("ko.json", "en.json")
DEFAULT_LANGUAGE_FILES = (*BUILT_IN_LANGUAGE_FILES, TEMPLATE_FILE_NAME)
_LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


class LanguagePackError(RuntimeError):
    """A language pack is malformed, incompatible, or unsafe to install."""


@dataclass(frozen=True, slots=True)
class LanguagePack:
    locale: str
    name: str
    native_name: str
    author: str
    version: str
    minimum_app_version: str
    strings: dict[str, str]
    overrides: dict[str, str]
    source_path: Path


class LanguagePackService:
    """Load language packs without executing any content from them."""

    def __init__(self, directory: Path | None = None) -> None:
        uses_default_directory = directory is None
        if directory is None:
            directory = self.default_directory()
        self.directory = directory
        self._packs: dict[str, LanguagePack] = {}
        self._errors: dict[Path, str] = {}
        if uses_default_directory:
            self.ensure_default_files()
        self.refresh()

    @staticmethod
    def default_directory() -> Path:
        """Return the documented per-user pack folder on every platform."""
        if sys.platform == "win32":
            local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
            if local_app_data:
                return Path(local_app_data) / "PlaylistCanvas" / "languages"
        data_root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        return Path(data_root or Path.home() / ".playlist-canvas") / "languages"

    @property
    def template_path(self) -> Path:
        return self.directory / TEMPLATE_FILE_NAME

    def ensure_template(self) -> Path:
        """Compatibility wrapper returning the installed blank template."""
        self.ensure_default_files()
        return self.template_path

    def ensure_default_files(self) -> tuple[Path, ...]:
        """Install Korean, English, and blank references without overwriting them."""
        resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
        installed: list[Path] = []
        for file_name in DEFAULT_LANGUAGE_FILES:
            destination = self.directory / file_name
            installed.append(destination)
            if destination.is_file():
                continue
            source = resource_root / "app" / "resources" / file_name
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            except OSError as error:
                LOGGER.warning("Could not install default language file %s: %s", file_name, error)
        return tuple(installed)

    @property
    def packs(self) -> dict[str, LanguagePack]:
        return dict(self._packs)

    @property
    def errors(self) -> dict[Path, str]:
        return dict(self._errors)

    def pack(self, locale: str) -> LanguagePack | None:
        """Return one installed pack without copying the complete registry."""
        return self._packs.get(locale)

    def refresh(self) -> None:
        packs: dict[str, LanguagePack] = {}
        errors: dict[Path, str] = {}
        if self.directory.is_dir():
            for path in sorted(self.directory.glob("*.json")):
                if path.name.casefold() in {
                    name.casefold() for name in DEFAULT_LANGUAGE_FILES
                }:
                    continue
                try:
                    pack = self.load_file(path)
                    if pack.locale in packs:
                        raise LanguagePackError(
                            f"Duplicate locale '{pack.locale}' is already installed."
                        )
                    packs[pack.locale] = pack
                except LanguagePackError as error:
                    errors[path] = str(error)
                    LOGGER.warning("Ignoring language pack %s: %s", path, error)
        self._packs = packs
        self._errors = errors

    def import_pack(self, source: Path) -> LanguagePack:
        """Validate a selected pack before copying it into per-user storage."""
        pack = self.load_file(source)
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / f"{pack.locale}.json"
        if source.resolve() != destination.resolve():
            temporary = destination.with_suffix(".json.importing")
            try:
                shutil.copyfile(source, temporary)
                # Validate the copied bytes too, then replace atomically.
                self.load_file(temporary)
                temporary.replace(destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        self.refresh()
        return self._packs[pack.locale]

    def remove_pack(self, locale: str) -> None:
        pack = self._packs.get(locale)
        if pack is None:
            raise LanguagePackError(f"Language pack '{locale}' is not installed.")
        resolved_directory = self.directory.resolve()
        resolved_path = pack.source_path.resolve()
        if resolved_path.parent != resolved_directory:
            raise LanguagePackError("Refusing to remove a language pack outside its folder.")
        resolved_path.unlink()
        self.refresh()

    def load_file(self, path: Path) -> LanguagePack:
        try:
            size = path.stat().st_size
        except OSError as error:
            raise LanguagePackError("The language pack file could not be read.") from error
        if size < 2 or size > MAX_PACK_BYTES:
            raise LanguagePackError("Language packs must be between 2 bytes and 4 MB.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LanguagePackError("The language pack is not valid UTF-8 JSON.") from error
        if not isinstance(payload, dict):
            raise LanguagePackError("The language pack root must be a JSON object.")
        if payload.get("schema_version") != LANGUAGE_PACK_SCHEMA:
            raise LanguagePackError(
                f"Unsupported language pack schema; expected {LANGUAGE_PACK_SCHEMA}."
            )
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise LanguagePackError("The language pack metadata object is missing.")
        locale = self._required_text(metadata, "locale", 40)
        if not _LOCALE_PATTERN.fullmatch(locale) or locale.lower() in {"ko", "en"}:
            raise LanguagePackError("The locale is invalid or reserved by a built-in language.")
        name = self._required_text(metadata, "name", 100)
        native_name = self._required_text(metadata, "native_name", 100)
        author = self._required_text(metadata, "author", 160)
        version = self._required_text(metadata, "version", 40)
        minimum = str(metadata.get("minimum_app_version", "1.0.1") or "1.0.1").strip()
        try:
            if normalized_version(minimum) > normalized_version(__version__):
                raise LanguagePackError(
                    f"This pack requires Music Motion Studio {minimum} or later."
                )
        except LanguagePackError:
            raise
        except Exception as error:
            raise LanguagePackError("minimum_app_version is invalid.") from error
        strings = self._translation_map(payload.get("strings", {}), keyed=True)
        overrides = self._translation_map(payload.get("overrides", {}), keyed=False)
        if not strings and not overrides:
            raise LanguagePackError("The language pack contains no translations.")
        return LanguagePack(
            locale=locale,
            name=name,
            native_name=native_name,
            author=author,
            version=version,
            minimum_app_version=minimum,
            strings=strings,
            overrides=overrides,
            source_path=path,
        )

    @staticmethod
    def _required_text(metadata: dict[object, object], key: str, limit: int) -> str:
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise LanguagePackError(f"metadata.{key} is missing or invalid.")
        return value.strip()

    @staticmethod
    def _translation_map(value: object, *, keyed: bool) -> dict[str, str]:
        if not isinstance(value, dict) or len(value) > MAX_TRANSLATIONS:
            raise LanguagePackError("A translation map is invalid or too large.")
        result: dict[str, str] = {}
        for raw_source, raw_translation in value.items():
            if not isinstance(raw_source, str) or not isinstance(raw_translation, str):
                raise LanguagePackError("Every translation key and value must be text.")
            source = raw_source.strip() if keyed else raw_source
            if (not source or len(source) > MAX_TEXT_LENGTH
                    or len(raw_translation) > MAX_TEXT_LENGTH):
                raise LanguagePackError("A translation entry is empty or too long.")
            if keyed and not _KEY_PATTERN.fullmatch(source):
                raise LanguagePackError(f"Invalid translation key: {source}")
            if raw_translation:
                LanguagePackService._validate_placeholders(source, raw_translation, keyed)
            result[source] = raw_translation
        return result

    @staticmethod
    def _validate_placeholders(source: str, translation: str, keyed: bool) -> None:
        if keyed:
            # Keyed strings are checked against their built-in fallback by Translator.
            return
        try:
            source_fields = {
                field for _, field, _, _ in Formatter().parse(source) if field
            }
            translated_fields = {
                field for _, field, _, _ in Formatter().parse(translation) if field
            }
        except ValueError as error:
            raise LanguagePackError("A literal override has malformed placeholders.") from error
        if source_fields != translated_fields:
            raise LanguagePackError(
                "A literal override must preserve all {placeholder} names."
            )
