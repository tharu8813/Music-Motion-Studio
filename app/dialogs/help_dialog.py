"""Offline user guide: one tab per window, tagged topics, screenshots and F1 context help.

Every window opens this guide with F1 on its own tab (and, where it can tell, on the
topic for the part in use): see ``install_help_shortcut`` and ``open_help``. The
text lives in ``help_content``; screenshots are PNGs under
``app/resources/help/<language>/`` made by ``tools/capture_help_images.py``, and a
missing one is simply left out.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QSettings, QSize, QTimer, QUrl, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QKeySequence, QPainter, QShortcut,
    QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSplitter, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QTabBar, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from app.dialogs.help_content import (
    HELP_TAB_IDS, HelpTab, HelpTopic, help_tabs, help_topics, topic_images,
)
from app.ui.design_system import COLORS
from app.utils.i18n import Translator

__all__ = [
    "HELP_TAB_IDS", "HelpDialog", "HelpTab", "HelpTopic", "help_image_path",
    "install_help_shortcut", "open_help",
]

HelpContext = tuple[str | None, str | None]
"""(tab id, topic id): where F1 opens the guide; either may be None."""

_IMAGE_MARKER = re.compile(r"\[\[img:([a-z0-9_]+)\]\]")
_TAGS_ROLE = Qt.ItemDataRole.UserRole + 1
_IMAGE_SCHEME = "helpimg"


def help_image_path(name: str, korean: bool) -> Path | None:
    """The screenshot for ``name`` in the UI language, else the other language, else None."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "app/resources/help"
    for language in ("ko", "en") if korean else ("en", "ko"):
        path = root / language / f"{name}.png"
        if path.is_file():
            return path
    return None


def _plain_text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", _IMAGE_MARKER.sub(" ", markup)))


def _topic_score(topic: HelpTopic, plain_body: str, terms: list[str], tag: str | None) -> int | None:
    """None when ``topic`` does not match; otherwise a rank (title beats tags beats body)."""
    if tag and tag not in topic.tags:
        return None
    score = 0
    title = topic.title.casefold()
    tags = [name.casefold() for name in topic.tags]
    keywords = topic.keywords.casefold()
    body = plain_body.casefold()
    for term in terms:
        if term.startswith("#"):
            name = term[1:]
            if name and not any(tag_name.startswith(name) for tag_name in tags):
                return None
            score += 30
        elif term in title:
            score += 10
        elif any(term in tag_name for tag_name in tags):
            score += 6
        elif term in keywords:
            score += 4
        elif term in body:
            score += 1
        else:
            return None
    return score


def find_translator(widget: QObject | None) -> Translator | None:
    """The first ``translator`` up the parent chain (the main window keeps one)."""
    current = widget
    while current is not None:
        translator = getattr(current, "translator", None)
        if isinstance(translator, Translator):
            return translator
        current = current.parent()
    return None


def open_help(
    parent: QWidget | None, translator: Translator | None = None,
    tab: str | None = None, topic: str | None = None,
) -> None:
    """Open the guide over ``parent`` on ``tab``/``topic`` (modal to that window only)."""
    HelpDialog(translator or find_translator(parent), parent, tab=tab, topic=topic).exec()


def install_help_shortcut(
    window: QWidget, context: HelpContext | Callable[[QWidget], HelpContext],
    translator: Translator | None = None,
) -> QShortcut:
    """Make F1 in ``window`` open the guide on the tab/topic ``context`` names.

    A callable ``context`` is given the window and returns ``(tab, topic)``; it
    must not capture the window itself (see ``_HelpShortcut``).
    """
    return _HelpShortcut(window, context, translator)


class _HelpShortcut(QShortcut):
    """F1 for one window.

    It reaches its window through ``parent()`` and its slot is its own method, so
    nothing here keeps a closed dialog alive: a lambda capturing the dialog would
    (and a leaked dialog is only deleted inside ~QApplication, which crashed the
    interpreter at exit).
    """

    def __init__(
        self, window: QWidget, context: HelpContext | Callable[[QWidget], HelpContext],
        translator: Translator | None,
    ) -> None:
        super().__init__(QKeySequence("F1"), window)
        self.setContext(Qt.ShortcutContext.WindowShortcut)
        self._help_context = context
        self._translator = translator
        self.activated.connect(self.open_help)

    def open_help(self) -> None:
        window = self.parent()
        context = self._help_context
        tab, topic = context(window) if callable(context) else context
        open_help(window, self._translator, tab, topic)


class _TopicDelegate(QStyledItemDelegate):
    """Two lines per topic: the title, then its tags in the muted (or accent) color."""

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802 - Qt API
        metrics = QFontMetrics(option.font)
        return QSize(option.rect.width(), metrics.height() * 2 + 18)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        item_option = QStyleOptionViewItem(option)
        self.initStyleOption(item_option, index)
        title = item_option.text
        item_option.text = ""
        widget = option.widget
        style = widget.style() if widget is not None else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, item_option, painter, widget)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        rect = option.rect.adjusted(12, 6, -10, -6)
        painter.save()
        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS["text"]))
        metrics = QFontMetrics(title_font)
        top = rect.adjusted(0, 0, 0, -rect.height() // 2)
        painter.drawText(top, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         metrics.elidedText(title, Qt.TextElideMode.ElideRight, top.width()))
        tag_font = QFont(option.font)
        if tag_font.pointSizeF() > 0:
            tag_font.setPointSizeF(max(7.0, tag_font.pointSizeF() * 0.88))
        painter.setFont(tag_font)
        painter.setPen(QColor(COLORS["accent"] if selected else COLORS["muted"]))
        bottom = rect.adjusted(0, rect.height() // 2, 0, 0)
        tags = "  ".join(f"#{name}" for name in (index.data(_TAGS_ROLE) or ()))
        painter.drawText(bottom, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         QFontMetrics(tag_font).elidedText(tags, Qt.TextElideMode.ElideRight, bottom.width()))
        painter.restore()


class _HelpBrowser(QTextBrowser):
    """Serves ``helpimg:`` screenshots already scaled to the reading width."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.images: dict[str, QImage] = {}

    def loadResource(self, kind: int, name: QUrl):  # noqa: N802 - Qt API
        if name.scheme() == _IMAGE_SCHEME:
            return self.images.get(name.toString(), QImage())
        return super().loadResource(kind, name)


class HelpDialog(QDialog):
    """Tabs per window, a tag filter and a search across every tab; works without a network."""

    def __init__(
        self, translator: Translator | None, parent: QWidget | None = None, *,
        tab: str | None = None, topic: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.translator = translator
        self._tabs: list[HelpTab] = []
        self._topics: list[HelpTopic] = []
        self._plain: dict[str, str] = {}
        self._tab_id = tab if tab in HELP_TAB_IDS else HELP_TAB_IDS[0]
        self._tag: str | None = None
        self._shown_topic: str | None = None
        self._image_cache: dict[tuple[str, bool], QImage | None] = {}
        self._rendered_width = 0
        self.setObjectName("helpDialog")
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumSize(880, 600)
        self.resize(1180, 800)

        self.title_label = QLabel()
        self.title_label.setObjectName("dialogTitle")
        self.intro_label = QLabel()
        self.intro_label.setObjectName("mutedLabel")
        self.intro_label.setWordWrap(True)
        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setObjectName("helpSearch")
        self.tag_combo = QComboBox()
        self.tag_combo.setObjectName("helpTagFilter")
        self.tag_combo.setMinimumWidth(190)
        self.tab_bar = QTabBar()
        self.tab_bar.setObjectName("helpTabBar")
        self.tab_bar.setExpanding(False)
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setUsesScrollButtons(True)
        for _ in HELP_TAB_IDS:
            self.tab_bar.addTab("")
        self.tab_summary_label = QLabel()
        self.tab_summary_label.setObjectName("mutedLabel")

        self.topic_list = QListWidget()
        self.topic_list.setObjectName("helpTopicList")
        self.topic_list.setItemDelegate(_TopicDelegate(self.topic_list))
        self.topic_list.setMinimumWidth(230)
        self.topic_list.setMaximumWidth(340)
        self.topic_list.setUniformItemSizes(True)
        self.topic_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # rows elide
        self.browser = _HelpBrowser()
        self.browser.setObjectName("helpBrowser")
        self.browser.setOpenLinks(False)
        self.browser.document().setDocumentMargin(18)
        self.browser.document().setDefaultStyleSheet(
            f"a{{color:{COLORS['accent']};text-decoration:none;}}"
            "h1{font-size:23px;margin:2px 0 6px 0;}"
            "h2{font-size:17px;margin:22px 0 8px 0;}"
            "h3{font-size:14px;margin:16px 0 6px 0;}"
            "p,li,td{font-size:13px;line-height:1.55;}"
            "li{margin-bottom:5px;}"
            f"th{{font-size:13px;text-align:left;color:{COLORS['muted']};}}"
            f"code{{font-family:Consolas,monospace;font-weight:600;color:{COLORS['accent_hover']};}}"
            f".crumb{{font-size:12px;color:{COLORS['muted']};margin:0;}}"
            f".tags{{font-size:12px;margin:0 0 10px 0;}}"
            f".note{{background-color:{COLORS['alternate']};padding:8px 12px;margin:12px 0;}}"
            f".tip{{background-color:{COLORS['info_surface']};padding:8px 12px;margin:12px 0;}}"
        )
        self.browser.viewport().installEventFilter(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self.topic_list)
        splitter.addWidget(self.browser)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([270, 880])

        self.hint_label = QLabel()
        self.hint_label.setObjectName("mutedLabel")
        self.close_button = QPushButton()
        # Enter belongs to the search box (it moves to the results), never to Close.
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.accept)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.tag_combo)
        footer = QHBoxLayout()
        footer.addWidget(self.hint_label, 1)
        footer.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(self.title_label)
        layout.addWidget(self.intro_label)
        layout.addLayout(search_row)
        layout.addWidget(self.tab_bar)
        layout.addWidget(self.tab_summary_label)
        layout.addWidget(splitter, 1)
        layout.addLayout(footer)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._rerender_for_width)

        self.search_edit.textChanged.connect(self._search_changed)
        self.search_edit.returnPressed.connect(self.topic_list.setFocus)
        self.tag_combo.currentIndexChanged.connect(self._tag_changed)
        self.tab_bar.currentChanged.connect(self._tab_changed)
        self.topic_list.currentItemChanged.connect(self._item_changed)
        self.browser.anchorClicked.connect(self._open_link)
        if translator is not None:
            translator.language_changed.connect(self.retranslate)
        self.retranslate()
        if topic:
            self.select_topic(topic)
        self.search_edit.setFocus()

    # -- language ------------------------------------------------------------------------

    @property
    def korean(self) -> bool:
        if self.translator is not None:
            return bool(self.translator.is_korean)
        return str(QSettings().value("language", "ko") or "ko").startswith("ko")

    def _text(self, korean: str, english: str) -> str:
        return korean if self.korean else english

    def retranslate(self) -> None:
        korean = self.korean
        self.setWindowTitle(self._text("Music Motion Studio 도움말", "Music Motion Studio Help"))
        self.title_label.setText(self._text("사용 설명서", "User Guide"))
        self.intro_label.setText(self._text(
            "창마다 탭이 나뉘어 있습니다. 어느 창에서든 F1을 누르면 그 창의 도움말이 열립니다. "
            "검색은 모든 탭을 함께 찾고, #태그로 주제를 좁힐 수 있습니다.",
            "One tab per window, and F1 in any window opens its tab. Search looks through every "
            "tab at once, and a #tag narrows the topics.",
        ))
        self.search_edit.setPlaceholderText(self._text(
            "도움말 검색 (예: 내보내기, 가사 타이밍, #단축키)",
            "Search help (for example: export, lyric timing, #shortcuts)",
        ))
        self.hint_label.setText(self._text(
            "파란 글씨를 누르면 관련 주제로, #태그를 누르면 같은 태그의 주제로 이동합니다.",
            "Select a link to jump to a related topic, or a #tag to list topics with that tag.",
        ))
        self.close_button.setText(self._text("닫기", "Close"))
        self._tabs = help_tabs(korean)
        self._topics = help_topics(korean)
        self._plain = {topic.identifier: _plain_text(topic.body) for topic in self._topics}
        for index, tab in enumerate(self._tabs):
            self.tab_bar.setTabToolTip(index, tab.summary)
        self._fill_tags()
        self._refresh(self._shown_topic)

    def _fill_tags(self) -> None:
        counts: dict[str, int] = {}
        for topic in self._topics:
            for name in topic.tags:
                counts[name] = counts.get(name, 0) + 1
        names = sorted(counts, key=lambda name: (-counts[name], name))
        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem(self._text("모든 태그", "All tags"), None)
        for name in names:
            self.tag_combo.addItem(f"#{name}  ({counts[name]})", name)
        if self._tag not in counts:
            self._tag = None
        self.tag_combo.setCurrentIndex(max(0, self.tag_combo.findData(self._tag)) if self._tag else 0)
        self.tag_combo.blockSignals(False)

    # -- selection API ---------------------------------------------------------------------

    @property
    def current_topic_id(self) -> str | None:
        item = self.topic_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    @property
    def current_tab_id(self) -> str:
        return self._tab_id

    def visible_topic_ids(self) -> list[str]:
        """Topic ids listed on the current tab, in list order."""
        return [
            str(self.topic_list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.topic_list.count())
        ]

    def select_tab(self, identifier: str) -> None:
        if identifier in HELP_TAB_IDS and identifier != self._tab_id:
            self._tab_id = identifier
            self._refresh()

    def select_topic(self, identifier: str) -> None:
        """Show one topic by its stable id, switching tabs and clearing filters that hide it."""
        topic = self._topic(identifier)
        if topic is None:
            return
        if self._tag is not None and self._tag not in topic.tags:
            self._set_tag(None)
        if self.search_edit.text() and self._matches(topic) is None:
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
        self._tab_id = topic.tab
        self._refresh(identifier)

    def set_tag_filter(self, tag: str | None) -> None:
        self._set_tag(tag)
        self._refresh()

    def _set_tag(self, tag: str | None) -> None:
        self._tag = tag
        self.tag_combo.blockSignals(True)
        self.tag_combo.setCurrentIndex(max(0, self.tag_combo.findData(tag)) if tag else 0)
        self.tag_combo.blockSignals(False)

    def _topic(self, identifier: str | None) -> HelpTopic | None:
        return next((topic for topic in self._topics if topic.identifier == identifier), None)

    def _tab(self, identifier: str) -> HelpTab | None:
        return next((tab for tab in self._tabs if tab.identifier == identifier), None)

    # -- filtering -------------------------------------------------------------------------

    def _terms(self) -> list[str]:
        return [term for term in self.search_edit.text().casefold().split() if term]

    def _matches(self, topic: HelpTopic) -> int | None:
        return _topic_score(topic, self._plain.get(topic.identifier, ""), self._terms(), self._tag)

    def _search_changed(self, _text: str) -> None:
        self._refresh()

    def _tag_changed(self, index: int) -> None:
        self._tag = self.tag_combo.itemData(index)
        self._refresh()

    def _tab_changed(self, index: int) -> None:
        if 0 <= index < len(HELP_TAB_IDS) and HELP_TAB_IDS[index] != self._tab_id:
            self._tab_id = HELP_TAB_IDS[index]
            self._refresh()

    def _refresh(self, preferred_id: str | None = None) -> None:
        terms = self._terms()
        filtering = bool(terms or self._tag)
        found: dict[str, list[tuple[int, int, HelpTopic]]] = {tab: [] for tab in HELP_TAB_IDS}
        for order, topic in enumerate(self._topics):
            score = self._matches(topic)
            if score is not None:
                found.setdefault(topic.tab, []).append((score, order, topic))
        preferred = self._topic(preferred_id)
        if preferred is not None and any(entry[2] is preferred for entry in found.get(preferred.tab, [])):
            self._tab_id = preferred.tab
        elif filtering and not found.get(self._tab_id):
            best = max(HELP_TAB_IDS, key=lambda tab: (
                max((entry[0] for entry in found[tab]), default=-1), len(found[tab])))
            if found[best]:
                self._tab_id = best
        self.tab_bar.blockSignals(True)
        for index, tab_id in enumerate(HELP_TAB_IDS):
            tab = self._tab(tab_id)
            title = tab.title if tab is not None else tab_id
            self.tab_bar.setTabText(index, f"{title}  {len(found[tab_id])}" if filtering else title)
        self.tab_bar.setCurrentIndex(HELP_TAB_IDS.index(self._tab_id))
        self.tab_bar.blockSignals(False)
        current_tab = self._tab(self._tab_id)
        self.tab_summary_label.setText(current_tab.summary if current_tab is not None else "")

        entries = found[self._tab_id]
        if terms:
            entries = sorted(entries, key=lambda entry: (-entry[0], entry[1]))
        selected_id = preferred_id or self.current_topic_id
        self.topic_list.blockSignals(True)
        self.topic_list.clear()
        for _score, _order, topic in entries:
            item = QListWidgetItem(topic.title)
            item.setData(Qt.ItemDataRole.UserRole, topic.identifier)
            item.setData(_TAGS_ROLE, topic.tags)
            item.setToolTip(" ".join(f"#{name}" for name in topic.tags))
            self.topic_list.addItem(item)
            if topic.identifier == selected_id:
                self.topic_list.setCurrentItem(item)
        if self.topic_list.currentItem() is None and self.topic_list.count():
            self.topic_list.setCurrentRow(0)
        self.topic_list.blockSignals(False)
        current = self._topic(self.current_topic_id)
        if current is not None:
            self._show(current)
        else:
            self._shown_topic = None
            self.browser.setHtml(self._text(
                "<h1>검색 결과 없음</h1><p>다른 검색어를 입력하거나 태그 필터를 ‘모든 태그’로 바꿔 보세요.</p>",
                "<h1>No results</h1><p>Try a different search term, or set the tag filter to ‘All tags’.</p>",
            ))

    # -- rendering -------------------------------------------------------------------------

    def _item_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        topic = self._topic(current.data(Qt.ItemDataRole.UserRole)) if current is not None else None
        if topic is not None:
            self._show(topic)

    def _reading_width(self) -> int:
        return max(320, self.browser.viewport().width() - 2 * 18 - 8)

    def _image_html(self, name: str) -> str:
        korean = self.korean
        key = (name, korean)
        if key not in self._image_cache:
            path = help_image_path(name, korean)
            image = QImage(str(path)) if path is not None else QImage()
            self._image_cache[key] = None if image.isNull() else image
        image = self._image_cache[key]
        if image is None:
            return ""
        width = min(image.width(), self._reading_width())
        ratio = max(1.0, self.devicePixelRatioF())
        url = f"{_IMAGE_SCHEME}://{'ko' if korean else 'en'}/{name}/{width}"
        if url not in self.browser.images:
            pixels = min(image.width(), round(width * ratio))
            self.browser.images[url] = image.scaledToWidth(pixels, Qt.TransformationMode.SmoothTransformation)
        height = round(image.height() * width / image.width())
        # The body's proportional line height would stretch the image's line too.
        return (f"<p align='center' style='line-height:100%; margin:8px 0 14px 0;'>"
                f"<img src='{url}' width='{width}' height='{height}'></p>")

    def _show(self, topic: HelpTopic) -> None:
        tab = self._tab(topic.tab)
        crumb = html.escape(tab.title if tab is not None else topic.tab)
        tags = " &nbsp; ".join(
            f"<a href='tag:{html.escape(name)}'>#{html.escape(name)}</a>" for name in topic.tags
        )
        body = _IMAGE_MARKER.sub(lambda match: self._image_html(match.group(1)), topic.body)
        related = [self._topic(identifier) for identifier in topic.related]
        links = " &nbsp;·&nbsp; ".join(
            f"<a href='topic:{entry.identifier}'>{html.escape(entry.title)}</a>"
            for entry in related if entry is not None
        )
        related_html = (
            f"<h3>{self._text('관련 항목', 'Related topics')}</h3><p>{links}</p>" if links else ""
        )
        keep_scroll = topic.identifier == self._shown_topic
        scroll = self.browser.verticalScrollBar().value()
        self._rendered_width = self._reading_width()
        self.browser.setHtml(
            f"<p class='crumb'>{crumb} ›</p><h1>{html.escape(topic.title)}</h1>"
            f"<p class='tags'>{tags}</p>{body}{related_html}"
        )
        self._shown_topic = topic.identifier
        self.browser.verticalScrollBar().setValue(scroll if keep_scroll else 0)
        self._highlight_terms()

    def _highlight_terms(self) -> None:
        document: QTextDocument = self.browser.document()
        highlight = QTextCharFormat()
        highlight.setBackground(QColor(121, 199, 180, 90))
        selections = []
        for term in self._terms():
            if term.startswith("#"):
                continue
            cursor = QTextCursor(document)
            while True:
                cursor = document.find(term, cursor)
                if cursor.isNull():
                    break
                selection = QTextEdit.ExtraSelection()
                selection.cursor = cursor
                selection.format = highlight
                selections.append(selection)
                if len(selections) > 400:
                    break
        self.browser.setExtraSelections(selections)

    def _rerender_for_width(self) -> None:
        topic = self._topic(self._shown_topic)
        if topic is None or not topic_images(topic):
            return
        if abs(self._reading_width() - self._rendered_width) > 12:
            self._show(topic)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if watched is self.browser.viewport() and event.type() == QEvent.Type.Resize:
            self._resize_timer.start()
        return super().eventFilter(watched, event)

    def _open_link(self, url: QUrl) -> None:
        text = url.toString()
        if text.startswith("topic:"):
            self.select_topic(text.removeprefix("topic:"))
        elif text.startswith("tag:"):
            self.set_tag_filter(QUrl.fromPercentEncoding(text.removeprefix("tag:").encode()))
