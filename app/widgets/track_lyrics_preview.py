"""Responsive lyrics preview with per-word TTML timing, isolated from export UI.

The label keeps its original plain text through QLabel.text() for accessibility,
copying, and legacy dialog tests. It overlays timed highlights during paint only,
without converting a timed TTML cue into a line-timed LRC approximation.
"""
from __future__ import annotations

from math import ceil, floor

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFontMetrics, QPainter, QRegion, QTextCharFormat,
    QTextCursor, QTextDocument, QTextOption,
)
from PySide6.QtWidgets import QLabel, QWidget

from app.services.lyrics_service import LyricsService
from app.ui.design_system import COLORS


def _estimate_text_height(metrics: QFontMetrics, text: str, width: int) -> int:
    """Measure actual wrapped text, including TTML physical line breaks."""
    if not text:
        return 0
    rect = metrics.boundingRect(
        QRect(0, 0, max(48, width), 10_000),
        Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter,
        text,
    )
    return max(metrics.lineSpacing(), rect.height()) + 7


def fit_context_lyrics(
    previous: list[str],
    following: list[str],
    *,
    available_height: int,
    available_width: int,
    metrics: QFontMetrics,
    maximum_lines: int = 10,
) -> tuple[list[str], list[str]]:
    """Fill both sides of the active group with whole, non-clipped cue rows.

    The previous list is ordered nearest first, and the following list is
    ordered in chronological order. Physical text wrapping is accounted for,
    so a long TTML line consumes more vertical space than a short LRC line.
    Spare room on one side is reused by the other. The active cue never loses
    its reserved center area.
    """
    room = max(0, available_height)
    previous = previous[:maximum_lines]
    following = following[:maximum_lines]
    widths = max(48, available_width)
    costs_before = [_estimate_text_height(metrics, text, widths) for text in previous]
    costs_after = [_estimate_text_height(metrics, text, widths) for text in following]

    def consume(costs: list[int], budget: int) -> tuple[int, int]:
        count = 0
        used = 0
        for cost in costs:
            if used + cost > budget:
                break
            used += cost
            count += 1
        return count, used

    left_count, left_cost = consume(costs_before, room // 2)
    right_count, right_cost = consume(costs_after, room - left_cost)
    left_count, left_cost = consume(costs_before, room - right_cost)
    # Use any remaining space on the upcoming side once earlier rows settle.
    right_count, right_cost = consume(costs_after, room - left_cost)
    return list(reversed(previous[:left_count])), following[:right_count]


class KaraokeLyricLabel(QLabel):
    """Centered lyric label with precise TTML word/character fill regions.

    QMediaPlayer.positionChanged supplies a clock in milliseconds. We use
    LyricsService.word_highlight_ranges so overlapping words retain separate
    timings, including partial words. A dim text document is painted first,
    then the identical light document is clipped to the sung glyphs.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._timed_lines: tuple[tuple[tuple[float, float], ...], ...] | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.setWordWrap(True)

    @property
    def has_word_timing(self) -> bool:
        return self._timed_lines is not None

    def clear_word_timing(self) -> None:
        self.set_word_ranges(None)

    def set_word_ranges(
        self,
        ranges: tuple[tuple[tuple[float, float], ...], ...] | None,
    ) -> None:
        if ranges == self._timed_lines:
            return
        self._timed_lines = ranges
        self.update()

    @staticmethod
    def ranges_for_cues(
        cues: list[dict],
        lyric_seconds: float,
    ) -> tuple[tuple[tuple[float, float], ...], ...] | None:
        """Only actual word-timed TTML receives the karaoke treatment.

        Translation lines are displayed after the original but remain dim,
        rather than inventing timing for translated words.
        """
        if not cues or not any(
            cue.get("format") == "ttml" and cue.get("words") for cue in cues
        ):
            return None
        merged: list[tuple[tuple[float, float], ...]] = []
        for cue in cues:
            display = LyricsService.decode_line_breaks(cue.get("text", "")).strip()
            if cue.get("format") == "ttml" and cue.get("words"):
                timed = LyricsService.word_highlight_ranges(cue, lyric_seconds)
            else:
                timed = ()
            physical = display.split("\n") if display else [""]
            merged.extend(
                timed[i] if i < len(timed) else ()
                for i in range(len(physical))
            )
            if cue is not cues[-1]:
                # Adjacent TTML cues are shown as separate paragraphs.
                pass
        return tuple(merged)

    def _document(self, color: str, width: int) -> QTextDocument:
        document = QTextDocument()
        document.setDefaultFont(self.font())
        document.setDocumentMargin(0)
        option = QTextOption()
        option.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        document.setDefaultTextOption(option)
        document.setPlainText(self.text())
        document.setTextWidth(float(width))
        cursor = QTextCursor(document)
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.mergeCharFormat(fmt)
        return document

    @staticmethod
    def _position_at(line, position: float) -> float:
        low = max(line.textStart(), min(line.textStart() + line.textLength(), floor(position)))
        high = max(low, min(line.textStart() + line.textLength(), ceil(position)))
        fraction = min(1.0, max(0.0, position - low))
        x0 = float(line.cursorToX(low))
        x1 = float(line.cursorToX(high))
        return x0 + (x1 - x0) * fraction

    def _highlight_region(self, document: QTextDocument) -> QRegion:
        region = QRegion()
        if self._timed_lines is None:
            return region
        block = document.begin()
        block_index = 0
        while block.isValid() and block_index < len(self._timed_lines):
            ranges = self._timed_lines[block_index]
            layout = block.layout()
            if layout is not None:
                for line_index in range(layout.lineCount()):
                    line = layout.lineAt(line_index)
                    line_start = line.textStart()
                    line_end = line_start + line.textLength()
                    for begin, finish in ranges:
                        start = max(float(line_start), float(begin))
                        end = min(float(line_end), float(finish))
                        if end <= start:
                            continue
                        x0 = self._position_at(line, start)
                        x1 = self._position_at(line, end)
                        rectangle = QRectF(
                            min(x0, x1),
                            layout.position().y() + line.y(),
                            max(1.0, abs(x1 - x0)),
                            line.height(),
                        )
                        region = region.united(QRegion(rectangle.toAlignedRect()))
            block = block.next()
            block_index += 1
        return region

    def paintEvent(self, event) -> None:
        if self._timed_lines is None or not self.text():
            super().paintEvent(event)
            return

        inner_width = max(40, self.width() - 20)
        inactive = self._document("#83928E", inner_width)
        active = self._document(COLORS["text"], inner_width)
        content_height = inactive.size().height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.translate(
            (self.width() - inner_width) / 2,
            max(0.0, (self.height() - content_height) / 2),
        )
        inactive.drawContents(painter)
        region = self._highlight_region(inactive)
        if not region.isEmpty():
            painter.save()
            painter.setClipRegion(region)
            active.drawContents(painter)
            painter.restore()
        painter.end()
