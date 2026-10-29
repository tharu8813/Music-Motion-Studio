"""Dedicated playlist reordering dialog with an audio preview player."""

from __future__ import annotations

from app.ui.design_system import COLORS

from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QSize, Signal
from PySide6.QtGui import QColor, QCursor, QDrag, QDropEvent, QPainter, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.models.playlist import PlaylistTrack
from app.preview.album_art import track_cover_thumbnail
from app.services.preview_audio_settings import preview_volume, save_preview_volume
from app.utils.i18n import Translator
from app.utils.time_format import format_clock

_COVER_PX = 46


def _clock(milliseconds: int) -> str:
    return format_clock(milliseconds // 1000, hours=False)


class _ReorderList(QListWidget):
    """Internal-move list that reports the resulting id order on every drop.

    ``QListWidget`` reconstructs the moved items from mime data on an internal
    move (losing their setItemWidget rows) and never emits ``rowsMoved``, so the
    drop order is computed here from the pointer and handed back for a rebuild.
    """

    order_changed = Signal(list)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        """Drag a real preview of the grabbed row(s) instead of an empty box.

        ``setItemWidget`` rows are painted by the widget, not the delegate, so
        Qt's default drag renderer produces a blank selection-coloured
        rectangle.  Grab the row widget instead.
        """
        items = self.selectedItems()
        if not items:
            return
        anchor = min(items, key=self.row)
        widget = self.itemWidget(anchor)
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData(
            [self.indexFromItem(item) for item in items]
        ))
        if widget is not None:
            pixmap = widget.grab()
            if len(items) > 1:
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setBrush(QColor(COLORS["accent"]))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(pixmap.width() - 30, 4, 24, 18, 6, 6)
                painter.setPen(QColor(COLORS["window"]))
                painter.drawText(
                    pixmap.width() - 30, 4, 24, 18,
                    Qt.AlignmentFlag.AlignCenter, f"{len(items)}",
                )
                painter.end()
            drag.setPixmap(pixmap)
            offset = self.viewport().mapFromGlobal(QCursor.pos()) - (
                self.visualItemRect(anchor).topLeft()
            )
            drag.setHotSpot(offset)
        drag.exec(supported_actions, Qt.DropAction.MoveAction)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.source() is not self:
            event.ignore()
            return
        order = [
            str(self.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.count())
        ]
        selected = sorted(self.row(item) for item in self.selectedItems())
        moving = [order[row] for row in selected if 0 <= row < len(order)]
        if not moving:
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        if target is None:
            insert_at = len(order)
        else:
            insert_at = self.row(target)
            rect = self.visualItemRect(target)
            if event.position().toPoint().y() > rect.center().y():
                insert_at += 1
        moving_set = set(moving)
        insert_at -= sum(
            1 for row, ident in enumerate(order)
            if ident in moving_set and row < insert_at
        )
        remaining = [ident for ident in order if ident not in moving_set]
        new_order = remaining[:insert_at] + moving + remaining[insert_at:]
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if new_order != order:
            self.order_changed.emit(new_order)


class _TrackRow(QWidget):
    """One reorder row: position, cover thumbnail, and track metadata."""

    def __init__(self, track: PlaylistTrack, korean: bool) -> None:
        super().__init__()
        self.setObjectName("trackOrderRow")
        self.track_id = track.id
        # The list owns selection, double-click and drag; the row and its labels
        # must not swallow the press or a drag started on the title never begins.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 10, 5)
        layout.setSpacing(10)
        self.number_label = QLabel()
        self.number_label.setObjectName("trackOrderNumber")
        self.number_label.setFixedWidth(30)
        self.number_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        cover_label = QLabel()
        cover_label.setFixedSize(_COVER_PX, _COVER_PX)
        cover_label.setScaledContents(True)
        pixmap = track_cover_thumbnail(track.file_path, track.cover_path)
        if not pixmap.isNull():
            cover_label.setPixmap(pixmap.scaled(
                _COVER_PX, _COVER_PX, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            cover_label.setObjectName("trackOrderCoverEmpty")
            cover_label.setText("♪")
            cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title = escape(track.title)
        self._subtitle = (
            f"<span style='color:{COLORS['muted']}'>{escape(track.artist)}"
            f" · {escape(track.album)}</span>"
        )
        self.meta_label = QLabel()
        self.meta_label.setTextFormat(Qt.TextFormat.RichText)
        self._render_meta(playing=False)
        self.duration_label = QLabel(track.duration_label)
        self.duration_label.setObjectName("trackOrderDuration")
        layout.addWidget(self.number_label)
        layout.addWidget(cover_label)
        layout.addWidget(self.meta_label, 1)
        layout.addWidget(self.duration_label)
        self.tooltip_text = (
            f"{track.title}\n{track.artist} · {track.album}\n{Path(track.file_path)}"
        )

    def _render_meta(self, playing: bool) -> None:
        marker = f"<span style='color:{COLORS['accent']}'>▶ </span>" if playing else ""
        self.meta_label.setText(
            f"<b>{marker}{self._title}</b><br>{self._subtitle}"
        )

    def set_position(self, number: int) -> None:
        self.number_label.setText(f"{number:02d}")

    def set_now_playing(self, playing: bool) -> None:
        self.setProperty("nowPlaying", playing)
        self.style().unpolish(self)
        self.style().polish(self)
        self._render_meta(playing)


class TrackOrderDialog(QDialog):
    """Reorder the playlist with drag-and-drop, buttons, and an audio preview.

    ``exec()`` returns ``Accepted`` only after the user confirms the change
    summary; the resulting order is then available as :attr:`new_order`.
    """

    def __init__(
        self, tracks: list[PlaylistTrack], translator: Translator,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("canvas", "playlist"), translator)
        self.translator = translator
        korean = translator.is_korean
        self._tracks_by_id: dict[str, PlaylistTrack] = {t.id: t for t in tracks}
        self._original_ids: list[str] = [t.id for t in tracks]
        self.new_order: list[str] = list(self._original_ids)
        self._playing_id: str | None = None

        self.setWindowTitle("트랙 순서 편집기" if korean else "Track order editor")
        self.setModal(True)
        self.setMinimumSize(560, 460)
        self.resize(680, 620)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(preview_volume() / 100.0)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.positionChanged.connect(self._on_position_changed)
        self.media_player.durationChanged.connect(self._on_duration_changed)
        self.media_player.playbackStateChanged.connect(self._on_playback_state)
        self.media_player.mediaStatusChanged.connect(self._on_media_status)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        hint = QLabel(
            "드래그하거나 선택 후 버튼으로 순서를 바꾸고, 트랙을 더블클릭하면 미리 듣습니다."
            if korean else
            "Drag rows or select and use the buttons to reorder. "
            "Double-click a track to preview it."
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(10)
        self.list_widget = _ReorderList()
        self.list_widget.setObjectName("trackOrderList")
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.list_widget.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove
        )
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setDragEnabled(True)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_widget.setSpacing(2)
        self.list_widget.order_changed.connect(self._apply_new_order)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        body.addWidget(self.list_widget, 1)

        button_column = QVBoxLayout()
        button_column.setSpacing(6)
        self.top_button = QPushButton("맨 위로" if korean else "To top")
        self.up_button = QPushButton("위로" if korean else "Up")
        self.down_button = QPushButton("아래로" if korean else "Down")
        self.bottom_button = QPushButton("맨 아래로" if korean else "To bottom")
        for button, delta in (
            (self.top_button, "top"), (self.up_button, -1),
            (self.down_button, 1), (self.bottom_button, "bottom"),
        ):
            button.clicked.connect(lambda _c=False, d=delta: self._move_selected(d))
            button_column.addWidget(button)
        button_column.addStretch(1)
        body.addLayout(button_column)
        root.addLayout(body, 1)

        transport = QHBoxLayout()
        transport.setSpacing(8)
        self.prev_button = QPushButton()
        self.play_button = QPushButton()
        self.next_button = QPushButton()
        for button, icon, label in (
            (self.prev_button, QStyle.StandardPixmap.SP_MediaSkipBackward, "이전 곡" if korean else "Previous track"),
            (self.next_button, QStyle.StandardPixmap.SP_MediaSkipForward, "다음 곡" if korean else "Next track"),
        ):
            button.setIcon(self.style().standardIcon(icon))
            button.setAccessibleName(label)
            button.setToolTip(label)
        self._on_playback_state(self.media_player.playbackState())
        for button in (self.prev_button, self.play_button, self.next_button):
            button.setFixedWidth(44)
            button.setIconSize(QSize(20, 20))
        self.prev_button.clicked.connect(lambda: self._step_playing(-1))
        self.next_button.clicked.connect(lambda: self._step_playing(1))
        self.play_button.clicked.connect(self._toggle_playback)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self.media_player.setPosition)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("mutedLabel")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(preview_volume())
        self.volume_slider.setAccessibleName("음량" if korean else "Volume")
        self.volume_slider.valueChanged.connect(self._set_volume)
        self.volume_label = QLabel()
        self.volume_label.setPixmap(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume).pixmap(18, 18))
        self.volume_label.setAccessibleName("음량" if korean else "Volume")
        self.volume_label.setToolTip("음량" if korean else "Volume")
        transport.addWidget(self.prev_button)
        transport.addWidget(self.play_button)
        transport.addWidget(self.next_button)
        transport.addWidget(self.position_slider, 1)
        transport.addWidget(self.time_label)
        transport.addWidget(self.volume_label)
        transport.addWidget(self.volume_slider)
        root.addLayout(transport)

        self.buttons = QDialogButtonBox()
        self.save_button = self.buttons.addButton(
            "저장" if korean else "Save",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.save_button.setObjectName("primaryButton")
        self.cancel_button = self.buttons.addButton(
            QDialogButtonBox.StandardButton.Cancel
        )
        self.cancel_button.setText("취소" if korean else "Cancel")
        self.save_button.clicked.connect(self._save)
        self.cancel_button.clicked.connect(self.reject)
        root.addWidget(self.buttons)

        self._rebuild_rows()

    # ---- row management -------------------------------------------------

    def _rebuild_rows(self) -> None:
        korean = self.translator.is_korean
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for track_id in self.new_order:
            track = self._tracks_by_id[track_id]
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, track_id)
            row_widget = _TrackRow(track, korean)
            item.setToolTip(row_widget.tooltip_text)
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, row_widget)
            item.setSizeHint(QSize(0, row_widget.sizeHint().height()))
        self.list_widget.blockSignals(False)
        self._renumber()

    def _renumber(self) -> None:
        for row in range(self.list_widget.count()):
            widget = self.list_widget.itemWidget(self.list_widget.item(row))
            if isinstance(widget, _TrackRow):
                widget.set_position(row + 1)
                widget.set_now_playing(widget.track_id == self._playing_id)

    def _current_ids(self) -> list[str]:
        return [
            str(self.list_widget.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.list_widget.count())
        ]

    def _adopt_order(self, new_order: list[str], keep_selected: set[str]) -> None:
        """Rebuild rows for a new order and restore selection by id."""
        self.new_order = [str(i) for i in new_order]
        self._rebuild_rows()
        for row, track_id in enumerate(self.new_order):
            if track_id in keep_selected:
                self.list_widget.item(row).setSelected(True)

    def _apply_new_order(self, ordered_ids: list) -> None:
        """Adopt a drag-computed order and rebuild the (widget-less) rows."""
        moving = {
            str(item.data(Qt.ItemDataRole.UserRole))
            for item in self.list_widget.selectedItems()
        }
        self._adopt_order([str(i) for i in ordered_ids], moving)

    def _move_selected(self, delta: object) -> None:
        order = self._current_ids()
        selected_rows = sorted(
            self.list_widget.row(item)
            for item in self.list_widget.selectedItems()
        )
        if not selected_rows:
            return
        moving = [order[row] for row in selected_rows]
        moving_set = set(moving)
        remaining = [ident for ident in order if ident not in moving_set]
        if delta == "top":
            insert_at = 0
        elif delta == "bottom":
            insert_at = len(remaining)
        else:
            # The selected tracks travel together as one block, shifted by one
            # slot relative to the tracks that stay put.
            anchor = selected_rows[0]
            non_selected_before = sum(
                1 for row in range(anchor) if order[row] not in moving_set
            )
            insert_at = max(
                0, min(len(remaining), non_selected_before + int(delta))
            )
        new_order = remaining[:insert_at] + moving + remaining[insert_at:]
        if new_order != order:
            self._adopt_order(new_order, moving_set)

    # ---- playback ----------------------------------------------------------

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        self._play_track(str(item.data(Qt.ItemDataRole.UserRole)))

    def _play_track(self, track_id: str) -> None:
        track = self._tracks_by_id.get(track_id)
        if track is None:
            return
        path = Path(track.file_path)
        self._playing_id = track_id
        self._renumber()
        if not path.is_file():
            self.media_player.stop()
            self.time_label.setText(
                "파일 없음" if self.translator.is_korean
                else "File missing"
            )
            return
        self.media_player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.media_player.play()

    def _step_playing(self, direction: int) -> None:
        order = self._current_ids()
        if not order:
            return
        if self._playing_id in order:
            index = order.index(self._playing_id) + direction
        else:
            index = 0 if direction > 0 else len(order) - 1
        if 0 <= index < len(order):
            self._play_track(order[index])

    def _toggle_playback(self) -> None:
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
        elif self._playing_id is not None:
            self.media_player.play()
        else:
            self._step_playing(1)

    def _set_volume(self, value: int) -> None:
        self.audio_output.setVolume(save_preview_volume(value) / 100.0)

    def _on_position_changed(self, position_ms: int) -> None:
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(position_ms)
        self.time_label.setText(
            f"{_clock(position_ms)} / {_clock(self.position_slider.maximum())}"
        )

    def _on_duration_changed(self, duration_ms: int) -> None:
        self.position_slider.setRange(0, max(0, duration_ms))

    def _on_playback_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        ))
        korean = self.translator.is_korean
        label = ("일시정지" if korean else "Pause") if playing else ("재생" if korean else "Play")
        self.play_button.setAccessibleName(label)
        self.play_button.setToolTip(label)

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._step_playing(1)

    # ---- save ------------------------------------------------------------

    def _save(self) -> None:
        order = self._current_ids()
        self.new_order = order
        korean = self.translator.is_korean
        if order == self._original_ids:
            self.reject()
            return
        original_index = {tid: i for i, tid in enumerate(self._original_ids)}
        moved = [
            (self._tracks_by_id[tid].title, original_index[tid] + 1, new_index + 1)
            for new_index, tid in enumerate(order)
            if original_index.get(tid) != new_index
        ]
        lines = "\n".join(
            f"· {title}: {before}번 → {after}번" if korean
            else f"· {title}: {before} → {after}"
            for title, before, after in moved
        )
        message = (
            f"다음 {len(moved)}개 곡의 순서가 바뀝니다:\n\n{lines}\n\n계속할까요?"
            if korean else
            f"{len(moved)} track(s) will move:\n\n{lines}\n\nApply these changes?"
        )
        box = QMessageBox(self)
        box.setWindowTitle("변경 사항 확인" if korean else "Confirm changes")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(message)
        continue_button = box.addButton(
            "계속하기" if korean else "Continue", QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton(
            "취소" if korean else "Cancel", QMessageBox.ButtonRole.RejectRole
        )
        box.exec()
        if box.clickedButton() is continue_button:
            self.accept()

    def reject(self) -> None:  # noqa: D102
        self.media_player.stop()
        super().reject()

    def done(self, result: int) -> None:  # noqa: D102
        self.media_player.stop()
        super().done(result)
