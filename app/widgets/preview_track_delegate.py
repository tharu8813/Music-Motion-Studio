"""Preview track-list painting and metadata formatting.

This widget does not own media playback, audio workers, or export state.
The full preview dialog re-exports the original names for callers.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from app.models.playlist import PlaylistTrack
from app.ui.design_system import COLORS


def _track_details(track: PlaylistTrack, separator: str) -> str:
    """Artist and album, else the file name: the "Unknown ..." placeholders are stored defaults, not information."""
    return separator.join(
        value for value, placeholder in ((track.artist, "Unknown Artist"), (track.album, "Unknown Album"))
        if value and value != placeholder
    ) or Path(track.file_path).name


TRACK_ROW_ROLE = Qt.ItemDataRole.UserRole + 2
"""(detail, duration) text of a Preview track-list row; its title is the item text."""


class PreviewTrackDelegate(QStyledItemDelegate):
    """A Preview track row: cover or number tile, title over details, duration on the right."""

    def paint(self, painter: QPainter, option, index) -> None:  # type: ignore[override]
        palette = option.palette
        accent = palette.highlight().color()
        rect = QRectF(option.rect).adjusted(0, 1, -2, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected or hovered:
            fill = QColor(accent) if selected else palette.alternateBase().color()
            if selected:
                fill.setAlpha(46)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 5, 5)
        if index.data(Qt.ItemDataRole.UserRole + 3):
            painter.setBrush(accent)
            painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 8, 3, rect.height() - 16), 1.5, 1.5)
        tile = QRectF(rect.left() + 10, rect.center().y() - 18, 36, 36)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        number = f"{index.row() + 1:02d}"
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, tile.toRect())
        else:
            painter.setBrush(QColor(COLORS["selection"]) if selected else palette.alternateBase())
            painter.drawRoundedRect(tile, 5, 5)
            painter.setPen(accent if selected else palette.placeholderText().color())
            painter.drawText(tile, Qt.AlignmentFlag.AlignCenter, number)
        detail, duration = index.data(TRACK_ROW_ROLE) or ("", "")
        metrics = painter.fontMetrics()
        duration_width = metrics.horizontalAdvance(duration) + 4
        text_left = tile.right() + 10
        title_rect = QRectF(text_left, rect.top() + 8, rect.right() - text_left - duration_width - 10, 18)
        bold = QFont(option.font)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setPen(palette.text().color())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(bold).elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "",
                                                       Qt.TextElideMode.ElideRight, int(title_rect.width())))
        painter.setFont(option.font)
        painter.setPen(palette.placeholderText().color())
        painter.drawText(QRectF(rect.right() - duration_width - 8, title_rect.top(), duration_width, 18),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, duration)
        detail_rect = QRectF(text_left, title_rect.bottom() + 1, rect.right() - text_left - 8, 16)
        painter.drawText(detail_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(detail, Qt.TextElideMode.ElideRight, int(detail_rect.width())))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # type: ignore[override]
        return QSize(0, 54)
