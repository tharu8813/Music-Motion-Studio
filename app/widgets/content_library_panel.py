"""Project-scoped reusable media list."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from stat import S_ISREG

from PySide6.QtCore import QItemSelectionModel, QMimeData, QPoint, QPointF, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QCursor, QDrag, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListView,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QToolButton,
    QVBoxLayout, QWidget,
)

from app.services.project_content_service import ProjectContentService
from app.dialogs.content_preview_dialog import ContentPreviewDialog
from app.utils.i18n import Translator


class ContentListWidget(QListWidget):
    """Expose library paths as file URLs so dropping is the only add gesture."""

    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:  # noqa: N802
        """Build a standard local-file payload for Qt's native drag lifecycle."""
        mime = QMimeData()
        paths = [
            Path(str(item.data(Qt.ItemDataRole.UserRole + 1))).resolve()
            for item in items
            if Path(str(item.data(Qt.ItemDataRole.UserRole + 1))).is_file()
        ]
        if paths:
            mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        return mime

    def supportedDropActions(self) -> Qt.DropAction:  # noqa: N802
        """Project content is copied into an editor target, never moved."""
        return Qt.DropAction.CopyAction

    def startDrag(self, _supported_actions: Qt.DropAction) -> None:
        """Start a real external file drag with an explicit copy action.

        Relying on QListWidget/QAbstractItemView.startDrag() can leave the drag
        action to the internal item model.  That is fragile across Qt versions
        (notably around QListWidget.supportedDragActions), and can result in no
        external drag being started even though mimeData() contains valid URLs.
        Build QDrag ourselves so Canvas/Playlist always receive file URLs.
        """
        items = self.selectedItems()
        if not items:
            return

        mime = self.mimeData(items)
        if mime is None or not mime.hasUrls():
            return

        drag = QDrag(self)
        drag.setMimeData(mime)

        current = self.currentItem()
        if current is not None and not current.icon().isNull():
            pixmap = current.icon().pixmap(self.iconSize())
            if not pixmap.isNull():
                drag.setPixmap(pixmap)

        # Returning to Project Content after leaving it is an explicit cancel
        # gesture. Poll the global pointer during QDrag's nested event loop so
        # the drag ends immediately instead of waiting for a redundant self-drop.
        origin_panel = self.parentWidget()
        has_left_origin = False
        monitor = QTimer(self)
        monitor.setInterval(20)

        def cancel_if_returned() -> None:
            nonlocal has_left_origin
            if origin_panel is None:
                return
            inside_origin = origin_panel.rect().contains(
                origin_panel.mapFromGlobal(QCursor.pos())
            )
            if not inside_origin:
                has_left_origin = True
                return
            if has_left_origin:
                monitor.stop()
                QDrag.cancel()

        monitor.timeout.connect(cancel_if_returned)
        monitor.start()
        try:
            drag.exec(Qt.DropAction.CopyAction, Qt.DropAction.CopyAction)
        finally:
            monitor.stop()
            monitor.deleteLater()


class ContentLibraryPanel(QWidget):
    """Import project content and reuse it without browsing repeatedly."""

    add_requested = Signal(str, str)
    VIEW_MODES = ("list", "grid", "compact")

    def __init__(
        self, service: ProjectContentService, translator: Translator,
        parent: QWidget | None = None,
        *, used_keys_provider: Callable[[], set[str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.translator = translator
        self._used_keys_provider = used_keys_provider
        # The "Added" marker depends on the playlist and Canvas, which change
        # far more often than the library itself; coalesce those refreshes.
        self._used_refresh_timer = QTimer(self)
        self._used_refresh_timer.setSingleShot(True)
        self._used_refresh_timer.setInterval(160)
        self._used_refresh_timer.timeout.connect(self.refresh)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(8)
        self.help_label = QLabel()
        self.help_label.setObjectName("mutedLabel")
        self.help_label.setWordWrap(True)
        layout.addWidget(self.help_label)
        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        self.filter_label = QLabel()
        self.filter_label.setObjectName("mutedLabel")
        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("projectContentFilter")
        for value in ("all", "image", "video", "audio", "lyrics", "font"):
            self.filter_combo.addItem(value, value)
        self.filter_count_label = QLabel()
        self.filter_count_label.setObjectName("mutedLabel")
        self._filter_count_full_text = ""
        filter_row.addWidget(self.filter_label)
        filter_row.addWidget(self.filter_combo, 1)
        filter_row.addWidget(self.filter_count_label)
        layout.addLayout(filter_row)
        view_row = QHBoxLayout()
        view_row.setContentsMargins(0, 0, 0, 0)
        view_row.setSpacing(4)
        self.view_label = QLabel()
        self.view_label.setObjectName("mutedLabel")
        view_row.addWidget(self.view_label)
        view_row.addStretch()
        self.view_button_group = QButtonGroup(self)
        self.view_button_group.setExclusive(True)
        self.view_buttons: dict[str, QToolButton] = {}
        for mode in self.VIEW_MODES:
            button = QToolButton(self)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setProperty("contentViewButton", True)
            button.clicked.connect(
                lambda _checked=False, selected=mode: self._set_view_mode(selected)
            )
            self.view_button_group.addButton(button)
            self.view_buttons[mode] = button
            view_row.addWidget(button)
        layout.addLayout(view_row)
        self.list = ContentListWidget()
        self.list.setObjectName("projectContentList")
        self.list.setIconSize(QSize(38, 38))
        self.list.setSpacing(2)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setAutoScroll(True)
        self.list.setAutoScrollMargin(36)
        self.list.setDragEnabled(True)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.list.setDefaultDropAction(Qt.DropAction.CopyAction)
        # Qt 6.10+ exposes drag actions separately from drop actions.  Keep the
        # source action explicit while remaining compatible with older PySide6.
        set_drag_actions = getattr(self.list, "setSupportedDragActions", None)
        if callable(set_drag_actions):
            set_drag_actions(Qt.DropAction.CopyAction)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.itemDoubleClicked.connect(lambda _item: self._preview_selected())
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.import_button = QPushButton()
        self.remove_button = QPushButton()
        row.addWidget(self.import_button)
        row.addStretch()
        row.addWidget(self.remove_button)
        layout.addLayout(row)
        self.import_button.clicked.connect(self._import_files)
        self.remove_button.clicked.connect(self._remove_selected)
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        self.service.changed.connect(self.refresh)
        self.translator.language_changed.connect(self.retranslate)
        saved_view = str(QSettings().value("project_content/view_mode", "list"))
        self._view_mode = saved_view if saved_view in self.VIEW_MODES else "list"
        saved_filter = str(QSettings().value("project_content/filter", "all"))
        self._filter = (
            saved_filter if saved_filter in {"all", "image", "video", "audio", "lyrics", "font"}
            else "all"
        )
        self.filter_combo.setCurrentIndex(
            max(0, self.filter_combo.findData(self._filter))
        )
        self._apply_view_mode()
        self.retranslate()
        self.refresh()

    def schedule_used_refresh(self) -> None:
        """Re-evaluate the 'Added' markers after a playlist or Canvas change."""
        self._used_refresh_timer.start()

    @property
    def view_mode(self) -> str:
        return self._view_mode

    @property
    def content_filter(self) -> str:
        return self._filter

    def retranslate(self) -> None:
        korean = self.translator.is_korean
        self.help_label.setText(
            "프로젝트에서 재사용할 이미지·영상·음원·폰트·가사 파일입니다. "
            "이미지·영상은 캔버스로, 음원은 플레이리스트로 드래그하세요. "
            "가사는 적용할 곡 위에 놓을 수 있으며 더블클릭하면 미리봅니다."
            if korean else
            "Reusable images, videos, audio, fonts, and lyrics. Drag images/videos "
            "onto the Canvas, audio onto the playlist, and lyrics onto a track. "
            "Double-click to preview."
        )
        self.view_label.setText("보기" if korean else "View")
        self.filter_label.setText("필터" if korean else "Filter")
        filter_text = {
            "all": "전체" if korean else "All",
            "image": "이미지" if korean else "Images",
            "video": "영상" if korean else "Videos",
            "audio": "오디오" if korean else "Audio",
            "lyrics": "가사 / 자막" if korean else "Lyrics / subtitles",
            "font": "폰트" if korean else "Fonts",
        }
        for index in range(self.filter_combo.count()):
            value = str(self.filter_combo.itemData(index))
            self.filter_combo.setItemText(index, filter_text[value])
        view_text = {
            "list": "목록" if korean else "List",
            "grid": "격자" if korean else "Grid",
            "compact": "간단" if korean else "Compact",
        }
        view_help = {
            "list": "파일 유형과 상세 정보를 함께 표시합니다."
            if korean else "Show file type and details.",
            "grid": "큰 썸네일을 격자로 표시합니다."
            if korean else "Show large thumbnails in a grid.",
            "compact": "더 많은 콘텐츠를 한 화면에 표시합니다."
            if korean else "Fit more content on screen.",
        }
        for mode, button in self.view_buttons.items():
            button.setText(view_text[mode])
            button.setToolTip(view_help[mode])
            button.setAccessibleName(view_text[mode])
        self.import_button.setText("콘텐츠 가져오기" if korean else "Import content")
        self.remove_button.setText("목록에서 제거" if korean else "Remove")
        self.refresh()

    def _filter_changed(self, _index: int) -> None:
        selected = str(self.filter_combo.currentData() or "all")
        if selected not in {"all", "image", "video", "audio", "lyrics", "font"}:
            selected = "all"
        self._filter = selected
        settings = QSettings()
        settings.setValue("project_content/filter", selected)
        settings.sync()
        self.refresh()

    def _set_view_mode(self, mode: str, *, persist: bool = True) -> None:
        """Switch presentation without replacing the list model or its selection."""
        if mode not in self.VIEW_MODES:
            return
        selected_id = self._selected_id()
        self._view_mode = mode
        self._apply_view_mode()
        if persist:
            settings = QSettings()
            settings.setValue("project_content/view_mode", mode)
            settings.sync()
        self.refresh()
        if selected_id:
            self._select_content_id(selected_id)

    def _apply_view_mode(self) -> None:
        """Apply QListView geometry for detailed, thumbnail, or dense browsing."""
        mode = self._view_mode
        self.view_buttons[mode].setChecked(True)
        if mode == "grid":
            self.list.setViewMode(QListView.ViewMode.IconMode)
            self.list.setFlow(QListView.Flow.LeftToRight)
            self.list.setWrapping(True)
            self.list.setResizeMode(QListView.ResizeMode.Adjust)
            self.list.setMovement(QListView.Movement.Static)
            self.list.setWordWrap(True)
            self.list.setUniformItemSizes(True)
            self.list.setIconSize(QSize(72, 72))
            self.list.setGridSize(QSize(116, 112))
            self.list.setSpacing(5)
        else:
            self.list.setViewMode(QListView.ViewMode.ListMode)
            self.list.setFlow(QListView.Flow.TopToBottom)
            self.list.setWrapping(False)
            self.list.setResizeMode(QListView.ResizeMode.Adjust)
            self.list.setMovement(QListView.Movement.Static)
            self.list.setWordWrap(False)
            self.list.setUniformItemSizes(True)
            self.list.setGridSize(QSize())
            self.list.setIconSize(QSize(22, 22) if mode == "compact" else QSize(38, 38))
            self.list.setSpacing(0 if mode == "compact" else 2)

        # QListView view/movement changes may reset drag/drop-related state.
        # Re-apply the source-only drag policy after presentation settings.
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.list.setDragEnabled(True)
        self.list.setAcceptDrops(False)
        self.list.setDefaultDropAction(Qt.DropAction.CopyAction)
        set_drag_actions = getattr(self.list, "setSupportedDragActions", None)
        if callable(set_drag_actions):
            set_drag_actions(Qt.DropAction.CopyAction)

    def refresh(self) -> None:
        selected_ids = set(self._selected_ids())
        selected_id = self._selected_id()
        scroll_position = self.list.verticalScrollBar().value()
        korean = self.translator.is_korean
        labels = {
            "image": "이미지" if korean else "Image",
            "video": "영상" if korean else "Video",
            "audio": "오디오" if korean else "Audio",
            "font": "폰트" if korean else "Font",
            "lyrics": "가사 / 자막" if korean else "Lyrics / subtitles",
        }
        items = self.service.items
        visible_items = [
            content for content in items
            if self._filter == "all" or content.media_type == self._filter
        ]
        try:
            used_keys = (
                self._used_keys_provider() if self._used_keys_provider else set()
            )
        except Exception:  # a stale project reference must not break the list
            used_keys = set()
        added_label = "추가됨" if korean else "Added"
        used_count = 0
        listed = [self.list.item(row).data(Qt.ItemDataRole.UserRole)
                  for row in range(self.list.count())]
        in_place = listed == [content.id for content in visible_items][:len(listed)]
        if not in_place:
            self.list.clear()
        for index, content in enumerate(visible_items):
            path = Path(content.path)
            try:
                stat = path.stat()
            except OSError:
                stat = None
            available = stat is not None and S_ISREG(stat.st_mode)
            in_project = self.service._key(content.path) in used_keys
            if in_project:
                used_count += 1
            type_label = labels.get(content.media_type, content.media_type)
            extension = path.suffix.removeprefix(".").upper()
            detail = f"{type_label} · {extension}" if extension else type_label
            if not available:
                detail += " · " + ("파일 없음" if korean else "Missing file")
            # The "Added" marker reads best pinned to the left of each line and
            # tinted, rather than trailing a long detail string.
            name_line = f"✓ {content.name}" if in_project else content.name
            if self._view_mode == "compact":
                item_text = (
                    f"{name_line}  ·  {added_label}  ·  {detail}" if in_project
                    else f"{name_line}  ·  {detail}"
                )
                item_height = 32
            elif self._view_mode == "grid":
                item_text = name_line
                item_height = 104
            else:
                detail_line = f"{added_label} · {detail}" if in_project else detail
                item_text = f"{name_line}\n{detail_line}"
                item_height = 54
            existing = in_place and index < len(listed)
            item = self.list.item(index) if existing else QListWidgetItem()
            item.setText(item_text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
            item.setData(Qt.ItemDataRole.UserRole + 3, in_project)
            if in_project:
                item.setForeground(QColor("#2F9E44"))
            else:
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
            icon_key = (content.path, content.media_type, self._view_mode, available,
                        (stat.st_mtime_ns, stat.st_size) if stat is not None else None)
            if item.data(Qt.ItemDataRole.UserRole + 4) != icon_key:
                item.setIcon(self._content_item_icon(path, content.media_type, available))
                item.setData(Qt.ItemDataRole.UserRole + 4, icon_key)
            item.setSizeHint(
                QSize(108, item_height) if self._view_mode == "grid"
                else QSize(0, item_height)
            )
            item.setData(Qt.ItemDataRole.UserRole, content.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, content.path)
            item.setData(Qt.ItemDataRole.UserRole + 2, content.media_type)
            accessible_type = type_label + (
                "; 파일 없음" if korean and not available else ""
            )
            if not korean and not available:
                accessible_type += "; missing file"
            if in_project:
                accessible_type += f"; {added_label}"
            item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"{content.name}; {accessible_type}",
            )
            item.setToolTip(
                f"{type_label}\n{content.path}"
                + (
                    ("\n파일을 찾을 수 없습니다." if korean else "\nFile not found.")
                    if not available else ""
                )
                + (
                    ("\n이미 이 프로젝트에 추가되어 있습니다." if korean
                     else "\nAlready used in this project.")
                    if in_project else ""
                )
            )
            if not existing:
                self.list.addItem(item)
                if content.id == selected_id:
                    self.list.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
                if content.id in selected_ids:
                    item.setSelected(True)
        count_text = f"{len(visible_items)} / {len(items)}"
        if used_count:
            count_text += (
                f" · {added_label} {used_count}"
            )
        self._set_filter_count_text(count_text)
        self._update_buttons()
        QTimer.singleShot(0, self, lambda: self.list.verticalScrollBar().setValue(scroll_position))

    def _set_filter_count_text(self, text: str) -> None:
        """Elide the count label instead of letting it clip against the panel edge."""
        self._filter_count_full_text = text
        self.filter_count_label.setToolTip(text)
        metrics = QFontMetrics(self.filter_count_label.font())
        available = self.filter_count_label.width()
        if available < 40:
            # Not laid out yet (e.g. still on another tab); resizeEvent/showEvent
            # will re-elide once the real width is known.
            available = 9999
        self.filter_count_label.setText(
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, available)
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._filter_count_full_text:
            self._set_filter_count_text(self._filter_count_full_text)

    def showEvent(self, event) -> None:  # noqa: N802
        # A tab page already has its final layout geometry while hidden, so
        # switching to this tab shows it without a resize; re-elide here too,
        # since the very first refresh() can run before any geometry exists.
        super().showEvent(event)
        if self._filter_count_full_text:
            self._set_filter_count_text(self._filter_count_full_text)

    def _select_content_id(self, content_id: str) -> None:
        for index in range(self.list.count()):
            item = self.list.item(index)
            if str(item.data(Qt.ItemDataRole.UserRole)) == content_id:
                self.list.setCurrentItem(item)
                return

    def _content_item_icon(
        self, path: Path, media_type: str, available: bool,
    ) -> QIcon:
        """Use real image thumbnails in grid view and category icons elsewhere."""
        if self._view_mode == "grid" and media_type == "image" and available:
            preview = QIcon(str(path))
            if not preview.isNull():
                return preview
        return self._content_icon(media_type, available)

    def _content_icon(self, media_type: str, available: bool) -> QIcon:
        """Create a crisp, theme-safe icon for a project content category."""
        colors = {
            "image": QColor("#36A2EB"),
            "video": QColor("#EC4899"),
            "audio": QColor("#8B5CF6"),
            "font": QColor("#F59E0B"),
            "lyrics": QColor("#14B8A6"),
        }
        accent = colors.get(media_type, QColor("#64748B"))
        if not available:
            accent = QColor("#E05252")

        pixmap = QPixmap(40, 40)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        background = QColor(accent)
        background.setAlpha(42)
        painter.setPen(QPen(accent, 1.4))
        painter.setBrush(background)
        painter.drawRoundedRect(QRectF(1.5, 1.5, 37.0, 37.0), 9.0, 9.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(
            accent, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
        ))

        if media_type == "image":
            painter.drawRoundedRect(QRectF(9.0, 10.0, 22.0, 20.0), 3.0, 3.0)
            painter.drawEllipse(QPointF(25.0, 15.0), 2.2, 2.2)
            painter.drawPolyline(QPolygonF([
                QPointF(11.0, 27.0), QPointF(17.0, 20.0),
                QPointF(21.0, 24.0), QPointF(24.0, 21.0),
                QPointF(30.0, 27.0),
            ]))
        elif media_type == "video":
            painter.drawRoundedRect(QRectF(8.0, 11.0, 19.0, 18.0), 3.0, 3.0)
            painter.drawPolygon(QPolygonF([
                QPointF(29.0, 16.0), QPointF(35.0, 13.0),
                QPointF(35.0, 27.0), QPointF(29.0, 24.0),
            ]))
        elif media_type == "audio":
            painter.drawLine(QPointF(19.0, 12.0), QPointF(19.0, 26.0))
            painter.drawLine(QPointF(19.0, 12.0), QPointF(29.0, 10.0))
            painter.drawLine(QPointF(29.0, 10.0), QPointF(29.0, 23.0))
            painter.setBrush(accent)
            painter.drawEllipse(QPointF(15.5, 27.0), 4.0, 3.2)
            painter.drawEllipse(QPointF(25.5, 24.0), 4.0, 3.2)
        elif media_type == "font":
            font = QFont(self.font())
            font.setBold(True)
            font.setPixelSize(16)
            painter.setFont(font)
            painter.drawText(QRectF(5.0, 7.0, 30.0, 27.0), Qt.AlignmentFlag.AlignCenter, "Aa")
        else:
            for y, width in ((12.0, 19.0), (18.0, 23.0), (24.0, 16.0), (30.0, 21.0)):
                painter.drawLine(QPointF(8.5, y), QPointF(8.5 + width, y))

        if not available:
            painter.setPen(QPen(
                QColor("#FFFFFF"), 1.8, Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            ))
            painter.setBrush(accent)
            painter.drawEllipse(QPointF(31.0, 9.0), 6.0, 6.0)
            painter.drawLine(QPointF(31.0, 5.7), QPointF(31.0, 9.0))
            painter.drawPoint(QPointF(31.0, 12.0))

        painter.end()
        return QIcon(pixmap)

    def _import_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "콘텐츠 가져오기" if self.translator.is_korean else "Import content",
            "",
            "Supported content (*.jpg *.jpeg *.png *.webp *.svg *.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mp3 *.wav *.flac "
            "*.aac *.m4a *.ogg *.ttf *.otf *.woff *.woff2 *.lrc *.srt *.vtt)",
        )
        if paths:
            self.service.add_paths(paths)

    def _selected_id(self) -> str:
        item = self.list.currentItem()
        if item is None and self.list.selectedItems():
            item = self.list.selectedItems()[0]
        return str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else ""

    def _selected_ids(self) -> list[str]:
        return [
            str(item.data(Qt.ItemDataRole.UserRole))
            for item in self.list.selectedItems()
        ]

    def _add_selected(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            self.add_requested.emit(
                str(item.data(Qt.ItemDataRole.UserRole + 1)),
                str(item.data(Qt.ItemDataRole.UserRole + 2)),
            )

    def _preview_selected(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        dialog = ContentPreviewDialog(
            str(item.data(Qt.ItemDataRole.UserRole + 1)),
            str(item.data(Qt.ItemDataRole.UserRole + 2)),
            self.translator.is_korean,
            self,
        )
        dialog.exec()

    def _remove_selected(self) -> None:
        content_ids = self._selected_ids()
        if not content_ids:
            content_id = self._selected_id()
            content_ids = [content_id] if content_id else []
        # Snapshot the IDs first: each service mutation may synchronously refresh
        # the list and replace its QListWidgetItem objects.
        for content_id in content_ids:
            self.service.remove(content_id)

    def _show_context_menu(self, position: QPoint) -> None:
        """Open a localized menu for an item or the list's empty area."""
        item = self.list.itemAt(position)
        if item is not None:
            self.list.setCurrentItem(item)
        menu = self._create_context_menu(item)
        menu.exec(self.list.viewport().mapToGlobal(position))

    def _create_context_menu(self, item: QListWidgetItem | None) -> QMenu:
        """Build the content menu separately so its availability is testable."""
        korean = self.translator.is_korean
        menu = QMenu(self)
        if item is not None:
            preview_action = menu.addAction(
                "미리보기" if korean else "Preview",
                self._preview_selected,
            )
            preview_action.setData("preview")

            remove_action = menu.addAction(
                "목록에서 제거" if korean else "Remove from list",
                self._remove_selected,
            )
            remove_action.setData("remove")

            info_action = menu.addAction(
                "정보" if korean else "Information",
                self._show_selected_information,
            )
            info_action.setData("information")
            menu.addSeparator()

        import_action = menu.addAction(
            "콘텐츠 가져오기…" if korean else "Import content…",
            self._import_files,
        )
        import_action.setData("import")
        return menu

    def _show_selected_information(self) -> None:
        """Show useful, read-only file details for the selected content."""
        item = self.list.currentItem()
        if item is None:
            return
        korean = self.translator.is_korean
        path = Path(str(item.data(Qt.ItemDataRole.UserRole + 1)))
        media_type = str(item.data(Qt.ItemDataRole.UserRole + 2))
        type_names = {
            "image": "이미지" if korean else "Image",
            "audio": "오디오" if korean else "Audio",
            "font": "폰트" if korean else "Font",
            "lyrics": "가사 / 자막" if korean else "Lyrics / subtitles",
        }
        available = path.is_file()
        size = self._format_file_size(path.stat().st_size) if available else "-"
        lines = (
            [
                f"이름: {path.stem}",
                f"유형: {type_names.get(media_type, media_type)}",
                f"상태: {'사용 가능' if available else '파일 없음'}",
                f"크기: {size}",
                f"경로: {path}",
            ]
            if korean else
            [
                f"Name: {path.stem}",
                f"Type: {type_names.get(media_type, media_type)}",
                f"Status: {'Available' if available else 'Missing file'}",
                f"Size: {size}",
                f"Path: {path}",
            ]
        )
        QMessageBox.information(
            self,
            "콘텐츠 정보" if korean else "Content information",
            "\n".join(lines),
        )

    @staticmethod
    def _format_file_size(size: int) -> str:
        value = float(max(0, size))
        units = ("B", "KB", "MB", "GB")
        unit = units[0]
        for unit in units:
            if value < 1024.0 or unit == units[-1]:
                break
            value /= 1024.0
        return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"

    def _update_buttons(self) -> None:
        enabled = bool(self.list.selectedItems()) or self.list.currentItem() is not None
        self.remove_button.setEnabled(enabled)
