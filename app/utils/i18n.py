"""Runtime translation service with safe external JSON language-pack support."""

from __future__ import annotations

from enum import Enum
import re
from string import Formatter
from typing import NamedTuple

import shiboken6
from PySide6.QtCore import (
    QCoreApplication, QEvent, QLibraryInfo, QObject, QSettings, QTimer, QTranslator, Signal,
)
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QTabWidget,
    QWidget,
)

from app.services.language_pack_service import LanguagePack, LanguagePackService


class Language(str, Enum):
    KOREAN = "ko"
    ENGLISH = "en"


class ExternalLanguage(str):
    """String locale that retains the `.value` interface used by built-ins."""

    @property
    def value(self) -> str:
        return str(self)


LanguageSelection = Language | ExternalLanguage


class LocaleOption(NamedTuple):
    locale: str
    display_name: str
    built_in: bool


_TEXT: dict[str, dict[Language, str]] = {
    "app_title": {Language.KOREAN: "Music Motion Studio — 새 프로젝트", Language.ENGLISH: "Music Motion Studio — Untitled Project"},
    "new": {Language.KOREAN: "새로 만들기", Language.ENGLISH: "New"},
    "open": {Language.KOREAN: "열기", Language.ENGLISH: "Open"},
    "save": {Language.KOREAN: "저장", Language.ENGLISH: "Save"},
    "fit_canvas": {Language.KOREAN: "캔버스 맞춤", Language.ENGLISH: "Fit canvas"},
    "grid": {Language.KOREAN: "그리드", Language.ENGLISH: "Grid"},
    "snap": {Language.KOREAN: "스냅", Language.ENGLISH: "Snap"},
    "delete": {Language.KOREAN: "삭제", Language.ENGLISH: "Delete"},
    "export": {Language.KOREAN: "내보내기", Language.ENGLISH: "Export"},
    "language": {Language.KOREAN: "언어", Language.ENGLISH: "Language"},
    "add_to_canvas": {Language.KOREAN: "캔버스에 추가", Language.ENGLISH: "Add to canvas"},
    "image": {Language.KOREAN: "이미지", Language.ENGLISH: "Image"},
    "text": {Language.KOREAN: "텍스트", Language.ENGLISH: "Text"},
    "shape": {Language.KOREAN: "도형", Language.ENGLISH: "Shape"},
    "progress_bar": {Language.KOREAN: "진행 바", Language.ENGLISH: "Progress bar"},
    "album_cover": {Language.KOREAN: "앨범 커버", Language.ENGLISH: "Album cover"},
    "time": {Language.KOREAN: "시간", Language.ENGLISH: "Time"},
    "logo": {Language.KOREAN: "로고", Language.ENGLISH: "Logo"},
    "watermark": {Language.KOREAN: "워터마크", Language.ENGLISH: "Watermark"},
    "background": {Language.KOREAN: "배경", Language.ENGLISH: "Background"},
    "audio_visualizer": {Language.KOREAN: "오디오 비주얼라이저", Language.ENGLISH: "Audio visualizer"},
    "playlist": {Language.KOREAN: "플레이리스트", Language.ENGLISH: "Playlist"},
    "timeline": {Language.KOREAN: "타임라인", Language.ENGLISH: "Timeline"},
    "inspector": {Language.KOREAN: "속성", Language.ENGLISH: "Inspector"},
    "select_object": {Language.KOREAN: "요소를 선택해 속성을 편집하세요.", Language.ENGLISH: "Select an element to edit its properties."},
    "content": {Language.KOREAN: "콘텐츠", Language.ENGLISH: "Content"},
    "transform": {Language.KOREAN: "변형", Language.ENGLISH: "Transform"},
    "appearance": {Language.KOREAN: "모양", Language.ENGLISH: "Appearance"},
    "track_coming": {Language.KOREAN: "트랙 편집기는 Phase 1C에서 추가됩니다", Language.ENGLISH: "Track editor is added in Phase 1C"},
    "timeline_coming": {Language.KOREAN: "타임라인 편집은 Phase 3에서 연결됩니다", Language.ENGLISH: "Timeline editing will be connected in Phase 3"},
}


_QT_TRANSLATORS: list[QTranslator] = []
_QT_TRANSLATION_STATE: tuple[int, str] | None = None  # (application id, language)


def install_qt_translations(locale: str) -> None:
    """Load Qt's own strings (standard buttons, file dialogs, edit menus) for ``locale``.

    Without this, every QMessageBox showed "Yes / No / Save / Discard / Cancel" and every
    file dialog stayed English in the Korean UI. A missing catalog keeps Qt's English.
    """
    global _QT_TRANSLATION_STATE
    application = QCoreApplication.instance()
    if application is None:
        return
    language = locale.replace("_", "-").split("-")[0].lower()
    state = (id(application), language)
    if state == _QT_TRANSLATION_STATE:
        return
    for translator in _QT_TRANSLATORS:
        if shiboken6.isValid(translator):
            application.removeTranslator(translator)
    _QT_TRANSLATORS.clear()
    _QT_TRANSLATION_STATE = state
    if not language or language == "en":
        return
    directory = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(application)
    if translator.load(f"qtbase_{language}", directory):
        application.installTranslator(translator)
        _QT_TRANSLATORS.append(translator)


class Translator(QObject):
    """Persist locale selection and layer external translations over English UI."""

    language_changed = Signal()
    packs_changed = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        pack_service: LanguagePackService | None = None,
    ) -> None:
        super().__init__(parent)
        self.pack_service = pack_service or LanguagePackService()
        saved = str(QSettings().value("language", Language.KOREAN.value) or "ko")
        self._language = self._selection(saved)
        self._applying = False
        self._event_filter_installed = False
        self._formatted_override_identity: tuple[str, str] | None = None
        self._formatted_overrides: list[
            tuple[re.Pattern[str], list[str], str]
        ] = []
        self._sync_event_filter()
        install_qt_translations(self.locale)

    @property
    def language(self) -> LanguageSelection:
        return self._language

    @property
    def locale(self) -> str:
        return self._language.value

    @property
    def is_korean(self) -> bool:
        return self._language is Language.KOREAN

    @property
    def active_pack(self) -> LanguagePack | None:
        return self.pack_service.pack(self.locale)

    def available_languages(self) -> list[LocaleOption]:
        options = [
            LocaleOption("ko", "한국어", True),
            LocaleOption("en", "English", True),
        ]
        options.extend(
            LocaleOption(pack.locale, pack.native_name, False)
            for pack in sorted(
                self.pack_service.packs.values(), key=lambda item: item.native_name.casefold()
            )
        )
        return options

    def set_language(self, language: Language | str) -> None:
        code = language.value if isinstance(language, Language) else str(language)
        normalized = self._selection(code)
        if normalized == self._language:
            self._schedule_apply()
            return
        self._language = normalized
        self._clear_override_cache()
        QSettings().setValue("language", normalized.value)
        self._sync_event_filter()
        install_qt_translations(self.locale)
        self.language_changed.emit()
        self._schedule_apply()

    def refresh_packs(self) -> None:
        self.pack_service.refresh()
        self._clear_override_cache()
        if isinstance(self._language, ExternalLanguage) and self.locale not in self.pack_service.packs:
            self._language = Language.ENGLISH
            QSettings().setValue("language", Language.ENGLISH.value)
            install_qt_translations(self.locale)
            self.language_changed.emit()
        self._sync_event_filter()
        self.packs_changed.emit()
        self._schedule_apply()

    def text(self, key: str) -> str:
        fallback = _TEXT[key][Language.ENGLISH]
        if self._language is Language.KOREAN:
            return _TEXT[key][Language.KOREAN]
        pack = self.active_pack
        if pack is None:
            return fallback
        translated = pack.strings.get(key) or fallback
        return translated if self._same_placeholders(fallback, translated) else fallback

    def literal(self, english: str, korean: str | None = None) -> str:
        if self._language is Language.KOREAN:
            return korean if korean is not None else english
        pack = self.active_pack
        if pack is None:
            return english
        translated = self._external_literal(pack, english)
        return translated if self._same_placeholders(english, translated) else english

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (self.active_pack is not None and isinstance(watched, QWidget)
                and event.type() == QEvent.Type.Show):
            QTimer.singleShot(0, lambda widget=watched: self._translate_if_valid(widget))
        return False

    def translate_widget_tree(self, root: QWidget) -> None:
        """Apply literal overrides to stable widget properties after normal retranslate."""
        pack = self.active_pack
        if pack is None or self._applying:
            return
        self._applying = True
        try:
            widgets = [root, *root.findChildren(QWidget)]
            for widget in widgets:
                title = widget.windowTitle()
                widget.setWindowTitle(self._external_literal(pack, title))
                widget.setToolTip(self._external_literal(pack, widget.toolTip()))
                widget.setStatusTip(self._external_literal(pack, widget.statusTip()))
                if isinstance(widget, QLabel):
                    widget.setText(self._external_literal(pack, widget.text()))
                if isinstance(widget, QAbstractButton):
                    widget.setText(self._external_literal(pack, widget.text()))
                if isinstance(widget, QGroupBox):
                    widget.setTitle(self._external_literal(pack, widget.title()))
                if isinstance(widget, QLineEdit):
                    widget.setPlaceholderText(
                        self._external_literal(pack, widget.placeholderText())
                    )
                if isinstance(widget, QComboBox):
                    for index in range(widget.count()):
                        source = widget.itemText(index)
                        widget.setItemText(index, self._external_literal(pack, source))
                if isinstance(widget, QTabWidget):
                    for index in range(widget.count()):
                        source = widget.tabText(index)
                        widget.setTabText(index, self._external_literal(pack, source))
            for action in root.findChildren(QAction):
                action.setText(self._external_literal(pack, action.text()))
                action.setToolTip(self._external_literal(pack, action.toolTip()))
                action.setStatusTip(self._external_literal(pack, action.statusTip()))
        finally:
            self._applying = False

    def _selection(self, code: str) -> LanguageSelection:
        if code == Language.KOREAN.value:
            return Language.KOREAN
        if code == Language.ENGLISH.value:
            return Language.ENGLISH
        if self.pack_service.pack(code) is not None:
            return ExternalLanguage(code)
        return Language.ENGLISH

    def _schedule_apply(self) -> None:
        if self.parent() is not None:
            QTimer.singleShot(0, self._apply_to_open_windows)

    def _sync_event_filter(self) -> None:
        """Avoid a process-wide filter cost while a built-in language is active."""
        application = QApplication.instance()
        should_install = (
            application is not None and self.parent() is not None
            and self.active_pack is not None
        )
        if should_install and not self._event_filter_installed:
            application.installEventFilter(self)
            self._event_filter_installed = True
        elif not should_install and self._event_filter_installed and application is not None:
            application.removeEventFilter(self)
            self._event_filter_installed = False

    def _apply_to_open_windows(self) -> None:
        application = QApplication.instance()
        if application is None:
            return
        for widget in application.topLevelWidgets():
            self._translate_if_valid(widget)

    def _translate_if_valid(self, widget: QWidget) -> None:
        if shiboken6.isValid(widget):
            self.translate_widget_tree(widget)

    def _clear_override_cache(self) -> None:
        self._formatted_override_identity = None
        self._formatted_overrides.clear()

    def _external_literal(self, pack: LanguagePack, source: str) -> str:
        """Translate an exact literal or a rendered `{placeholder}` template."""
        if not source:
            return source
        exact = pack.overrides.get(source)
        if exact:
            return exact
        self._prepare_formatted_overrides(pack)
        for pattern, fields, translation in self._formatted_overrides:
            match = pattern.fullmatch(source)
            if match is None:
                continue
            values = {
                field: match.group(index + 1)
                for index, field in enumerate(fields)
            }
            return self._substitute_captured_fields(translation, values)
        return source

    def _prepare_formatted_overrides(self, pack: LanguagePack) -> None:
        identity = (pack.locale, pack.version)
        if identity == self._formatted_override_identity:
            return
        compiled: list[tuple[re.Pattern[str], list[str], str]] = []
        for source, translation in pack.overrides.items():
            if not translation:
                continue
            try:
                parsed = tuple(Formatter().parse(source))
            except ValueError:
                continue
            fields = [field for _, field, _, _ in parsed if field]
            if not fields:
                continue
            literal_text = "".join(literal for literal, _, _, _ in parsed)
            # A template made only from a placeholder would match arbitrary user
            # content, so require a meaningful fixed phrase around dynamic values.
            if len(re.sub(r"\W", "", literal_text)) < 3:
                continue
            parts: list[str] = []
            for literal, field, _format_spec, _conversion in parsed:
                parts.append(re.escape(literal))
                if field:
                    parts.append("(.+?)")
            try:
                compiled.append((re.compile("".join(parts), re.DOTALL), fields, translation))
            except re.error:
                continue
        compiled.sort(key=lambda entry: len(entry[0].pattern), reverse=True)
        self._formatted_overrides = compiled
        self._formatted_override_identity = identity

    @staticmethod
    def _substitute_captured_fields(template: str, values: dict[str, str]) -> str:
        parts: list[str] = []
        try:
            for literal, field, _format_spec, _conversion in Formatter().parse(template):
                parts.append(literal)
                if field:
                    parts.append(values.get(field, ""))
        except ValueError:
            return template
        return "".join(parts)

    @staticmethod
    def _same_placeholders(source: str, translation: str) -> bool:
        try:
            source_fields = {field for _, field, _, _ in Formatter().parse(source) if field}
            translated_fields = {
                field for _, field, _, _ in Formatter().parse(translation) if field
            }
        except ValueError:
            return False
        return source_fields == translated_fields
