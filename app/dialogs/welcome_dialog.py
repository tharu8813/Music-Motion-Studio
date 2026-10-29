"""Illustrated first-run guides: the main window's welcome tour and the AutoMix editor's.

Each page pairs a short text with a drawing of the real layout (painted, not a
screenshot, so it stays sharp, follows the language and never goes stale with
a restyle). Each guide shows once; ``first_time`` records it in QSettings, so
Settings → Maintenance → Reset brings them back.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.design_system import COLORS
from app.widgets.transition_inspector import INCOMING_COLOR, OUTGOING_COLOR

WELCOME_KEY = "guides/welcome_seen"
AUTOMIX_GUIDE_KEY = "guides/automix_editor_seen"


def first_time(key: str) -> bool:
    """True exactly once per key (the guide is marked seen as soon as it opens)."""
    settings = QSettings()
    if settings.value(key, False, bool):
        return False
    settings.setValue(key, True)
    return True


@dataclass(frozen=True)
class GuidePage:
    title: tuple[str, str]
    body: tuple[str, str]
    """(Korean, English) rich text."""
    art: Callable[[QPainter, QRectF, bool], None]


# -- drawing helpers ------------------------------------------------------------------------


def _color(name: str, alpha: int = 255) -> QColor:
    color = QColor(COLORS.get(name, name))
    color.setAlpha(alpha)
    return color


def _sub(rect: QRectF, x: float, y: float, w: float, h: float) -> QRectF:
    """A rectangle placed by fractions of ``rect``."""
    return QRectF(rect.left() + rect.width() * x, rect.top() + rect.height() * y,
                  rect.width() * w, rect.height() * h)


def _box(p: QPainter, rect: QRectF, *, lit: bool = False, fill: str = "panel", radius: float = 5) -> None:
    p.setPen(QPen(_color("accent") if lit else _color("border"), 2 if lit else 1))
    p.setBrush(_color(fill))
    p.drawRoundedRect(rect, radius, radius)


def _text(p: QPainter, rect: QRectF, text: str, color: str = "muted", size: float = 8.5,
          bold: bool = False, align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignCenter) -> None:
    font = QFont(p.font())
    font.setPointSizeF(size)
    font.setBold(bold)
    p.setFont(font)
    p.setPen(_color(color))
    p.drawText(rect, align, text)


def _pill(p: QPainter, rect: QRectF, text: str = "", *, accent: bool = False, size: float = 7.5) -> None:
    p.setPen(QPen(_color("accent") if accent else _color("border"), 1))
    p.setBrush(_color("accent") if accent else _color("button"))
    p.drawRoundedRect(rect, rect.height() / 2.6, rect.height() / 2.6)
    if text:
        _text(p, rect, text, "window" if accent else "text", size, accent)


def _line(p: QPainter, a: QPointF, b: QPointF, color: QColor, width: float = 1.0, dashed: bool = False) -> None:
    pen = QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.drawLine(a, b)


def _arrow(p: QPainter, a: QPointF, b: QPointF, color: QColor, dashed: bool = False) -> None:
    _line(p, a, b, color, 2, dashed)
    angle = math.atan2(b.y() - a.y(), b.x() - a.x())
    head = QPolygonF([b] + [QPointF(b.x() - 9 * math.cos(angle + turn), b.y() - 9 * math.sin(angle + turn))
                            for turn in (0.45, -0.45)])
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPolygon(head)


def _bars(p: QPainter, rect: QRectF, color: QColor, count: int, phase: float = 0.0) -> None:
    """A stylized waveform: rounded bars whose heights follow two sines."""
    step = rect.width() / count
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for index in range(count):
        height = rect.height() * (0.2 + 0.8 * abs(math.sin(index * 0.55 + phase) * math.cos(index * 0.17 + phase)))
        p.drawRoundedRect(QRectF(rect.left() + index * step + step * 0.2, rect.center().y() - height / 2,
                                 step * 0.6, height), step * 0.3, step * 0.3)


def _badge(p: QPainter, center: QPointF, number: int) -> None:
    p.setPen(QPen(_color("window"), 2))
    p.setBrush(_color("accent"))
    p.drawEllipse(center, 11, 11)
    _text(p, QRectF(center.x() - 11, center.y() - 11, 22, 22), str(number), "window", 8.5, True)


def _dim(p: QPainter, rect: QRectF) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_color("window", 165))
    p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 5, 5)


def _mini_canvas(p: QPainter, rect: QRectF) -> QRectF:
    """A 16:9 video frame with an album cover, title, visualizer and progress bar."""
    width = min(rect.width(), rect.height() * 16 / 9)
    height = width * 9 / 16
    frame = QRectF(rect.center().x() - width / 2, rect.center().y() - height / 2, width, height)
    gradient = QLinearGradient(frame.topLeft(), frame.bottomRight())
    gradient.setColorAt(0, QColor("#2E2552"))
    gradient.setColorAt(1, QColor("#15383B"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(gradient)
    p.drawRoundedRect(frame, 3, 3)
    cover = QRectF(frame.left() + frame.width() * 0.08, frame.top() + frame.height() * 0.18,
                   frame.height() * 0.46, frame.height() * 0.46)
    cover_gradient = QLinearGradient(cover.topLeft(), cover.bottomRight())
    cover_gradient.setColorAt(0, _color("accent"))
    cover_gradient.setColorAt(1, QColor("#5B4BB7"))
    p.setBrush(cover_gradient)
    p.drawRoundedRect(cover, 2, 2)
    left = cover.right() + frame.width() * 0.06
    p.setBrush(QColor(255, 255, 255, 215))
    p.drawRoundedRect(QRectF(left, cover.top() + cover.height() * 0.1, frame.width() * 0.4, frame.height() * 0.07), 2, 2)
    p.setBrush(QColor(255, 255, 255, 110))
    p.drawRoundedRect(QRectF(left, cover.top() + cover.height() * 0.35, frame.width() * 0.26, frame.height() * 0.05), 2, 2)
    _bars(p, QRectF(left, cover.top() + cover.height() * 0.55, frame.width() * 0.42, cover.height() * 0.45),
          QColor(121, 199, 180, 200), 16, 0.8)
    track = QRectF(frame.left() + frame.width() * 0.08, frame.bottom() - frame.height() * 0.16,
                   frame.width() * 0.84, max(2.0, frame.height() * 0.025))
    p.setBrush(QColor(255, 255, 255, 60))
    p.drawRoundedRect(track, 1, 1)
    p.setBrush(_color("accent"))
    p.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * 0.62, track.height()), 1, 1)
    return frame


def _file_card(p: QPainter, rect: QRectF, label: str, color: QColor) -> None:
    corner = rect.width() * 0.28
    path = QPainterPath(rect.topLeft())
    path.lineTo(rect.right() - corner, rect.top())
    path.lineTo(rect.right(), rect.top() + corner)
    path.lineTo(rect.bottomRight())
    path.lineTo(rect.bottomLeft())
    path.closeSubpath()
    p.setPen(QPen(color, 1.5))
    p.setBrush(_color("panel"))
    p.drawPath(path)
    _text(p, rect.adjusted(0, rect.height() * 0.35, 0, 0), label, "text", 8, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(rect.left() + rect.width() * 0.2, rect.top() + rect.height() * 0.22,
                             rect.width() * 0.4, rect.height() * 0.1), 2, 2)


# -- the main window, drawn -------------------------------------------------------------------

_WORKSPACE = {  # region: (x, y, w, h) inside the window body
    "toolbar": (0, 0, 1, 0.11),
    "sources": (0, 0.14, 0.2, 0.23),
    "layers": (0, 0.39, 0.2, 0.23),
    "canvas": (0.22, 0.14, 0.54, 0.48),
    "inspector": (0.78, 0.14, 0.22, 0.48),
    "playlist": (0, 0.65, 1, 0.35),
}


def _workspace(p: QPainter, r: QRectF, korean: bool, lit: tuple[str, ...] = (),
               numbers: dict[str, int] | None = None) -> dict[str, QRectF]:
    def t(ko: str, en: str) -> str:
        return ko if korean else en

    _box(p, r, fill="window", radius=8)
    body = r.adjusted(8, 8, -8, -8)
    rects = {name: _sub(body, *geometry) for name, geometry in _WORKSPACE.items()}
    for name, rect in rects.items():
        _box(p, rect, lit=name in lit)
        inner = rect.adjusted(6, 4, -6, -4)
        if name == "toolbar":
            h = inner.height() * 0.8
            y = inner.center().y() - h / 2
            for index in range(3):
                _pill(p, QRectF(inner.left() + index * (h * 2.1 + 5), y, h * 2.1, h))
            _pill(p, QRectF(inner.right() - h * 9.2, y, h * 4.4, h), t("미리보기", "Preview"))
            _pill(p, QRectF(inner.right() - h * 4.4, y, h * 4.4, h), t("내보내기", "Export"), accent=True)
        elif name == "sources":
            _text(p, _sub(inner, 0, 0, 1, 0.3), t("캔버스에 추가", "Add to canvas"), "text", 7.5, True,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            _pill(p, _sub(inner, 0, 0.36, 1, 0.26), t("+ 이미지", "+ Image"), size=7)
            _pill(p, _sub(inner, 0, 0.7, 1, 0.26), t("+ 텍스트", "+ Text"), size=7)
        elif name == "layers":
            _text(p, _sub(inner, 0, 0, 1, 0.3), t("레이어", "Layers"), "text", 7.5, True,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            for index in range(3):
                row = _sub(inner, 0, 0.36 + index * 0.21, 1, 0.15)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_color("accent", 90) if index == 0 else _color("hover"))
                p.drawRoundedRect(row, 2, 2)
        elif name == "canvas":
            _mini_canvas(p, inner.adjusted(4, 4, -4, -4))
        elif name == "inspector":
            _text(p, _sub(inner, 0, 0, 1, 0.14), t("속성", "Inspector"), "text", 7.5, True,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            for index in range(5):
                row = _sub(inner, 0, 0.2 + index * 0.16, 1, 0.1)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_color("hover"))
                p.drawRoundedRect(QRectF(row.left(), row.top(), row.width() * 0.35, row.height()), 2, 2)
                p.setBrush(_color("field"))
                p.setPen(QPen(_color("border"), 1))
                p.drawRoundedRect(QRectF(row.left() + row.width() * 0.42, row.top(), row.width() * 0.58, row.height()), 2, 2)
        elif name == "playlist":
            _text(p, _sub(inner, 0, 0, 0.5, 0.22), t("플레이리스트", "Playlist"), "text", 7.5, True,
                  Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            _pill(p, _sub(inner, 0.84, 0, 0.16, 0.2), t("+ 음악 추가", "+ Add music"), accent=name in lit, size=7)
            for index in range(3):
                row = _sub(inner, 0, 0.3 + index * 0.24, 1, 0.19)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_color("alternate"))
                p.drawRoundedRect(row, 3, 3)
                p.setBrush((OUTGOING_COLOR, INCOMING_COLOR, _color("accent"))[index])
                p.drawRoundedRect(QRectF(row.left() + 4, row.top() + 3, row.height() - 6, row.height() - 6), 2, 2)
                p.setBrush(_color("muted", 150))
                p.drawRoundedRect(QRectF(row.left() + row.height() + 6, row.center().y() - 2,
                                         row.width() * (0.3 - index * 0.05), 4), 2, 2)
        if lit and name not in lit:
            _dim(p, rect)
    for name, number in (numbers or {}).items():
        rect = rects[name]
        corner = name not in ("toolbar", "playlist")  # those two have buttons at the right
        _badge(p, QPointF(rect.right() - 14, rect.top() + 14) if corner
               else QPointF(rect.center().x(), rect.top() + min(14.0, rect.height() / 2)), number)
    return rects


def _art_welcome(p: QPainter, r: QRectF, korean: bool) -> None:
    _bars(p, _sub(r, 0, 0.18, 1, 0.5), _color("accent", 70), 48, 0.3)
    screen = _sub(r, 0.29, 0.03, 0.42, 0.8)
    screen = _mini_canvas(p, screen)
    p.setPen(QPen(_color("accent"), 2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(screen.adjusted(-3, -3, 3, 3), 5, 5)
    center = screen.center()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 120))
    p.drawEllipse(center, 22, 22)
    p.setBrush(QColor("#FFFFFF"))
    p.drawPolygon(QPolygonF([QPointF(center.x() - 7, center.y() - 11), QPointF(center.x() - 7, center.y() + 11),
                             QPointF(center.x() + 12, center.y())]))
    _text(p, _sub(r, 0, 0.84, 1, 0.16), "Music Motion Studio", "text", 13, True)


def _art_layout(p: QPainter, r: QRectF, korean: bool) -> None:
    _workspace(p, r, korean, numbers={"layers": 1, "canvas": 2, "inspector": 3, "playlist": 4, "toolbar": 5})


def _art_music(p: QPainter, r: QRectF, korean: bool) -> None:
    rects = _workspace(p, r, korean, lit=("playlist",))
    canvas = rects["canvas"]
    first = QRectF(canvas.center().x() - 70, canvas.top() + 16, 46, 58)
    second = first.translated(58, 12)
    _file_card(p, first, ".mp3", OUTGOING_COLOR)
    _file_card(p, second, ".lrc", INCOMING_COLOR)
    target = rects["playlist"]
    _arrow(p, QPointF(second.center().x(), second.bottom() + 6),
           QPointF(second.center().x(), target.top() + target.height() * 0.45), _color("accent"), dashed=True)


def _art_design(p: QPainter, r: QRectF, korean: bool) -> None:
    rects = _workspace(p, r, korean, lit=("sources", "layers", "canvas", "inspector"))
    source = rects["sources"]
    canvas = rects["canvas"]
    _arrow(p, QPointF(source.right() - 4, source.center().y()),
           QPointF(canvas.left() + canvas.width() * 0.2, canvas.top() + canvas.height() * 0.3), _color("accent"), dashed=True)


def _crossfade(p: QPainter, r: QRectF, korean: bool, *, labels: bool = True) -> tuple[QRectF, QRectF, QRectF]:
    """Two lanes overlapping: the outgoing song fades out while the incoming one fades in."""
    top = _sub(r, 0, 0.14, 0.66, 0.3)
    bottom = _sub(r, 0.34, 0.56, 0.66, 0.3)
    overlap = QRectF(bottom.left(), r.top() + 4, top.right() - bottom.left(), r.height() - 8)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_color("accent", 28))
    p.drawRoundedRect(overlap, 6, 6)
    for lane, color, phase in ((top, OUTGOING_COLOR, 0.2), (bottom, INCOMING_COLOR, 1.4)):
        fill = QColor(color)
        fill.setAlpha(38)
        p.setPen(QPen(color, 1))
        p.setBrush(fill)
        p.drawRoundedRect(lane, 5, 5)
        bar_color = QColor(color)
        bar_color.setAlpha(190)
        _bars(p, lane.adjusted(6, 6, -6, -6), bar_color, 34, phase)
    for lane, color, falling in ((top, OUTGOING_COLOR, True), (bottom, INCOMING_COLOR, False)):
        start = QPointF(overlap.left(), lane.top() + 3 if falling else lane.bottom() - 3)
        end = QPointF(overlap.right(), lane.bottom() - 3 if falling else lane.top() + 3)
        curve = QPainterPath(start)
        curve.cubicTo(QPointF(start.x() + overlap.width() * 0.5, start.y()),
                      QPointF(end.x() - overlap.width() * 0.5, end.y()), end)
        p.setPen(QPen(QColor("#FFFFFF"), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(curve)
    if labels:
        _text(p, QRectF(top.left(), r.top(), top.width(), top.top() - r.top()),
              "곡 A · 끝나는 곡" if korean else "Song A · ending", "text", 8, True,
              Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        _text(p, QRectF(bottom.left(), bottom.bottom(), bottom.width(), r.bottom() - bottom.bottom()),
              "곡 B · 이어지는 곡" if korean else "Song B · starting", "text", 8, True,
              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _text(p, QRectF(overlap.left(), top.bottom(), overlap.width(), bottom.top() - top.bottom()),
              "전환" if korean else "Transition", "accent", 9, True)
    return top, bottom, overlap


def _art_automix(p: QPainter, r: QRectF, korean: bool) -> None:
    _crossfade(p, _sub(r, 0.04, 0, 0.92, 1), korean)


def _art_export(p: QPainter, r: QRectF, korean: bool) -> None:
    def t(ko: str, en: str) -> str:
        return ko if korean else en

    stations = [_sub(r, x, 0.12, 0.26, 0.6) for x in (0.0, 0.37, 0.74)]
    _box(p, stations[0], fill="window")
    _mini_canvas(p, stations[0].adjusted(8, 8, -8, -8))
    _box(p, stations[1], lit=True)
    inner = stations[1].adjusted(14, 14, -14, -14)
    _text(p, _sub(inner, 0, 0, 1, 0.45), "FFmpeg", "text", 12, True)
    track = _sub(inner, 0, 0.62, 1, 0.12)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_color("field"))
    p.drawRoundedRect(track, 3, 3)
    p.setBrush(_color("accent"))
    p.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * 0.7, track.height()), 3, 3)
    file_rect = QRectF(stations[2].center().x() - stations[2].height() * 0.36, stations[2].top(),
                       stations[2].height() * 0.72, stations[2].height())
    _file_card(p, file_rect, "MP4", _color("accent"))
    for index in range(2):
        _arrow(p, QPointF(stations[index].right() + 8, stations[index].center().y()),
               QPointF(stations[index + 1].left() - 8, stations[index].center().y()), _color("accent"))
    for station, caption in zip(stations, (t("캔버스 + 음악", "Canvas + music"), t("인코딩", "Encoding"),
                                           t("완성 영상", "Finished video"))):
        _text(p, QRectF(station.left() - 10, station.bottom() + 8, station.width() + 20, 22), caption, "text", 9, True)


# -- the AutoMix editor, drawn ----------------------------------------------------------------

def _art_editor_header(p: QPainter, r: QRectF, korean: bool) -> None:
    def t(ko: str, en: str) -> str:
        return ko if korean else en

    _box(p, r, fill="window", radius=8)
    body = r.adjusted(8, 8, -8, -8)
    header, tools = _sub(body, 0, 0, 1, 0.17), _sub(body, 0, 0.2, 1, 0.1)
    timeline, properties = _sub(body, 0, 0.33, 0.72, 0.45), _sub(body, 0.74, 0.33, 0.26, 0.45)
    transport = _sub(body, 0, 0.81, 1, 0.19)
    _box(p, header, lit=True)
    inner = header.adjusted(8, 6, -8, -6)
    h = inner.height()
    _text(p, QRectF(inner.left(), inner.top(), 70, h), "AutoMix", "text", 9.5, True,
          Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    x = inner.left() + 76
    _pill(p, QRectF(x, inner.top(), h, h), "‹", size=9)
    _pill(p, QRectF(x + h + 5, inner.top(), h * 6, h), "✎ 1 → 2", size=8)
    _pill(p, QRectF(x + h * 7 + 10, inner.top(), h, h), "›", size=9)
    _pill(p, QRectF(inner.right() - h * 7, inner.top(), h * 3.8, h), t("기본 편집", "Basic"), accent=True, size=7.5)
    _pill(p, QRectF(inner.right() - h * 3.2, inner.top(), h * 3.2, h), t("고급", "Advanced"), size=7.5)
    for rect in (tools, timeline, properties, transport):
        _box(p, rect)
    _crossfade(p, timeline.adjusted(8, 4, -8, -4), korean, labels=False)
    for index in range(4):
        row = _sub(properties.adjusted(8, 8, -8, -8), 0, index * 0.26, 1, 0.16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_color("hover"))
        p.drawRoundedRect(row, 2, 2)
    for rect in (tools, timeline, properties, transport):
        _dim(p, rect)
    _badge(p, QPointF(x + h * 3.5 + 5, header.bottom() + 2), 1)
    _badge(p, QPointF(inner.right() - h * 3.3, header.bottom() + 2), 2)


def _art_editor_timeline(p: QPainter, r: QRectF, korean: bool) -> None:
    area = _sub(r, 0.02, 0.06, 0.96, 0.88)
    for index in range(25):  # the beat grid; every fourth line is a bar
        x = area.left() + area.width() * index / 24
        _line(p, QPointF(x, area.top()), QPointF(x, area.bottom()), _color("border", 255 if index % 4 == 0 else 110))
    _top, _bottom, overlap = _crossfade(p, area, korean, labels=False)
    for x in (overlap.left(), overlap.right()):
        _line(p, QPointF(x, overlap.top() + 10), QPointF(x, overlap.bottom()), _color("accent"), 2.5)
        p.setPen(QPen(_color("window"), 2))
        p.setBrush(_color("accent"))
        p.drawEllipse(QPointF(x, overlap.top() + 10), 7, 7)
    y = overlap.top() + 10
    _arrow(p, QPointF(overlap.left() - 10, y), QPointF(overlap.left() - 44, y), _color("accent"))
    _arrow(p, QPointF(overlap.left() + 10, y), QPointF(overlap.left() + 44, y), _color("accent"))
    _pill(p, QRectF(area.right() - 96, area.top(), 92, 22), "박자 스냅 ✓" if korean else "Snap ✓", accent=True)


def _art_editor_listen(p: QPainter, r: QRectF, korean: bool) -> None:
    window = _sub(r, 0.1, 0.02, 0.8, 0.44)
    _box(p, window, fill="window")
    _crossfade(p, window.adjusted(10, 6, -10, -6), korean, labels=False)
    playhead = window.left() + window.width() * 0.46
    _line(p, QPointF(playhead, window.top() - 4), QPointF(playhead, window.bottom() + 4), QColor("#FFFFFF"), 2)
    keys = (("Space", "재생 / 일시정지", "Play / pause"), ("L", "구간 반복", "Loop"), ("B", "A/B 자동과 비교", "A/B vs automatic"))
    width = r.width() / len(keys)
    for index, (key, ko, en) in enumerate(keys):
        cell = QRectF(r.left() + index * width, r.top() + r.height() * 0.56, width, r.height() * 0.44)
        cap = QRectF(cell.center().x() - (46 if key == "Space" else 22), cell.top(), 92 if key == "Space" else 44, 40)
        p.setPen(QPen(_color("border"), 1))
        p.setBrush(_color("button"))
        p.drawRoundedRect(cap.translated(0, 3), 7, 7)
        p.setPen(QPen(_color("accent"), 1.5))
        p.setBrush(_color("hover"))
        p.drawRoundedRect(cap, 7, 7)
        _text(p, cap, key, "text", 11, True)
        _text(p, QRectF(cell.left(), cap.bottom() + 6, cell.width(), 22), ko if korean else en, "text", 9)


# -- the pages ---------------------------------------------------------------------------------

MAIN_GUIDE_TITLE = ("Music Motion Studio 시작하기", "Getting started with Music Motion Studio")
MAIN_GUIDE = (
    GuidePage(
        ("Music Motion Studio에 오신 것을 환영합니다", "Welcome to Music Motion Studio"),
        ("음악·가사·앨범 커버·비주얼라이저를 하나의 캔버스에 자유롭게 배치해 "
         "<b>플레이리스트 영상(MP4)</b>을 만드는 편집기입니다.<br><br>"
         "이 안내에서 화면 구성과 첫 영상을 만드는 기본 흐름을 짧게 소개합니다. "
         "언제든 <b>건너뛰기</b>로 닫을 수 있습니다.",
         "An editor that arranges your music, lyrics, album art and visualizers on one canvas "
         "and turns them into a <b>playlist video (MP4)</b>.<br><br>"
         "This short tour shows the workspace and the basic steps to your first video. "
         "You can <b>Skip</b> it at any time."),
        _art_welcome,
    ),
    GuidePage(
        ("화면 구성", "The workspace"),
        ("① <b>캔버스에 추가 · 레이어</b> — 요소를 추가하고 순서·표시·잠금을 관리합니다<br>"
         "② <b>캔버스</b> — 영상 화면입니다. 요소를 끌어 배치합니다<br>"
         "③ <b>속성</b> — 선택한 요소의 세부 설정을 바꿉니다<br>"
         "④ <b>플레이리스트 · 타임라인</b> — 곡 목록과 요소가 보이는 시간<br>"
         "⑤ <b>미리보기 · 내보내기</b> — 결과를 확인하고 MP4로 만듭니다",
         "① <b>Add to canvas · Layers</b> — add elements; manage order, visibility and locks<br>"
         "② <b>Canvas</b> — the video frame; drag elements into place<br>"
         "③ <b>Inspector</b> — detailed settings of the selected element<br>"
         "④ <b>Playlist · Timeline</b> — the songs and when each element shows<br>"
         "⑤ <b>Preview · Export</b> — check the result and make the MP4"),
        _art_layout,
    ),
    GuidePage(
        ("1단계 · 음악 추가", "Step 1 · Add music"),
        ("아래 <b>플레이리스트</b>에서 <b>+ 음악 추가</b>를 누르거나 음악 파일을 그대로 끌어다 놓으세요.<br><br>"
         "같은 폴더에 이름이 같은 <b>.lrc / .srt / .vtt / .ttml</b> 파일이 있으면 가사로 자동 연결됩니다. "
         "곡 순서는 ↑ ↓ 버튼으로 바꿀 수 있습니다.",
         "In the <b>Playlist</b> below, select <b>+ Add music</b> or simply drop music files there.<br><br>"
         "A <b>.lrc / .srt / .vtt / .ttml</b> file with the same name in the same folder is attached as lyrics "
         "automatically. Reorder songs with the ↑ ↓ buttons."),
        _art_music,
    ),
    GuidePage(
        ("2단계 · 화면 꾸미기", "Step 2 · Design the screen"),
        ("왼쪽 <b>캔버스에 추가</b>에서 이미지·텍스트·앨범 커버·가사·재생 진행률·비주얼라이저 등을 추가합니다.<br><br>"
         "캔버스에서 끌어 위치와 크기를 조정하고, 오른쪽 <b>속성</b>에서 색·글꼴·애니메이션을 바꿉니다. "
         "겹치는 순서와 잠금은 <b>레이어</b>에서 관리합니다.",
         "From <b>Add to canvas</b> on the left, add images, text, album art, lyrics, a progress bar, "
         "visualizers and more.<br><br>"
         "Drag on the canvas to move and resize, and change colors, fonts and animations in the "
         "<b>Inspector</b> on the right. Stacking order and locks live in <b>Layers</b>."),
        _art_design,
    ),
    GuidePage(
        ("곡 전환과 AutoMix", "Transitions and AutoMix"),
        ("<b>프로젝트 설정</b>에서 곡 사이 전환 방식을 고를 수 있습니다. <b>AutoMix(베타)</b>를 켜면 "
         "박자와 보컬을 분석해 곡과 곡을 템포에 맞춰 자연스럽게 이어 줍니다.<br><br>"
         "자동 전환을 직접 다듬고 싶다면 <b>도구 → AutoMix 편집기</b>를 여세요.",
         "Choose how songs join in <b>Project settings</b>. Turn on <b>AutoMix (beta)</b> and it analyzes "
         "beats and vocals to blend each song into the next in tempo.<br><br>"
         "To fine-tune a transition yourself, open <b>Tools → AutoMix Editor</b>."),
        _art_automix,
    ),
    GuidePage(
        ("3단계 · 미리보기와 내보내기", "Step 3 · Preview and export"),
        ("상단 <b>미리보기</b>로 음악과 함께 결과를 확인하고, <b>내보내기</b>로 MP4 영상을 만듭니다.<br><br>"
         "내보내기에는 <b>FFmpeg</b>가 필요합니다. 처음 한 번 <b>도구 → 설정 → FFmpeg</b>에서 권장 버전을 "
         "설치하세요. 작업은 자동 저장되며, 미디어까지 담은 <b>.pvsproj</b> 파일 하나로 저장할 수 있습니다.",
         "Check the result with music using <b>Preview</b> at the top, then make the MP4 with <b>Export</b>.<br><br>"
         "Export needs <b>FFmpeg</b>: install the recommended version once from <b>Tools → Settings → FFmpeg</b>. "
         "Your work is autosaved, and a project can be saved with its media as a single <b>.pvsproj</b> file."),
        _art_export,
    ),
)

AUTOMIX_GUIDE_TITLE = ("AutoMix 편집기 안내", "AutoMix editor guide")
AUTOMIX_GUIDE = (
    GuidePage(
        ("AutoMix 편집기", "The AutoMix editor"),
        ("곡과 곡 사이의 <b>전환</b>을 두 곡 타임라인에서 직접 다듬는 곳입니다. "
         f"<span style='color:{OUTGOING_COLOR.name()}'>주황</span>은 끝나는 곡, "
         f"<span style='color:{INCOMING_COLOR.name()}'>파랑</span>은 이어지는 곡입니다.<br><br>"
         "분석이 정한 자동 전환에서 출발해 원하는 부분만 고치면 됩니다. "
         "모든 변경은 프로젝트에 바로 반영되고 Ctrl+Z로 되돌릴 수 있습니다.",
         "Fine-tune the <b>transition</b> between two songs on a two-track timeline. "
         f"<span style='color:{OUTGOING_COLOR.name()}'>Orange</span> is the song ending, "
         f"<span style='color:{INCOMING_COLOR.name()}'>blue</span> the one starting.<br><br>"
         "Start from the automatic transition analysis chose and change only what you want. "
         "Every edit goes into the project at once and Ctrl+Z undoes it."),
        _art_automix,
    ),
    GuidePage(
        ("전환 고르기와 편집 모드", "Pick a transition and a mode"),
        ("① 위쪽 <b>‹ ›</b> 버튼이나 목록에서 편집할 전환을 고릅니다. ✎ 표시는 직접 설정한 전환입니다.<br><br>"
         "② <b>기본 편집</b>은 스타일·길이·핵심 큐만, <b>고급</b>은 정밀 큐·템포·대역(EQ) 레인까지 보여 줍니다. "
         "같은 편집을 다르게 보여 줄 뿐이라 모드를 바꿔도 값은 그대로입니다.",
         "① Pick the transition to edit with <b>‹ ›</b> or the list at the top. ✎ marks one set by hand.<br><br>"
         "② <b>Basic</b> shows style, length and key cues; <b>Advanced</b> adds precise cues, tempo and "
         "EQ band lanes. Both show the same edit, so switching never changes a value."),
        _art_editor_header,
    ),
    GuidePage(
        ("타임라인에서 끌어 조정", "Drag on the timeline"),
        ("타임라인의 <b>핸들</b>을 끌어 전환 시작점·길이·큐를 옮깁니다. <b>박자 스냅</b>이 켜져 있으면 "
         "마디·박자에 붙고, Shift를 누른 채 끌면 잠시 반대로 동작합니다.<br><br>"
         "정확한 값은 오른쪽 <b>속성</b> 패널에 직접 입력하세요. Ctrl+휠로 확대·축소하고, "
         "<b>맞춤</b>(Ctrl+0)으로 전환 전체를 봅니다.",
         "Drag the timeline's <b>handles</b> to move the transition's start, length and cues. With "
         "<b>Snap</b> on they click onto bars and beats; hold Shift while dragging to invert it.<br><br>"
         "Type exact values in the <b>Properties</b> panel on the right. Ctrl+wheel zooms, and "
         "<b>Fit</b> (Ctrl+0) shows the whole transition."),
        _art_editor_timeline,
    ),
    GuidePage(
        ("듣고 비교하기", "Listen and compare"),
        ("<b>Space</b>로 선택한 전환 구간만 재생하고, <b>L</b>로 구간 반복, <b>B</b>로 자동 버전과 번갈아 "
         "비교합니다. 미리듣기에는 FFmpeg가 필요합니다.<br><br>"
         "<b>복사 · 붙여넣기 · 프리셋</b>으로 설정을 다른 전환에 재사용하고, Ctrl+Backspace로 자동 전환으로 "
         "되돌립니다. <b>F1</b>은 도움말, <b>Shift+F1</b>은 전체 단축키입니다.",
         "<b>Space</b> plays just the selected transition, <b>L</b> loops it and <b>B</b> switches to the "
         "automatic version to compare. Listening needs FFmpeg.<br><br>"
         "Reuse settings on other transitions with <b>Copy · Paste · Presets</b>, and Ctrl+Backspace puts a "
         "transition back on automatic. <b>F1</b> opens the help and <b>Shift+F1</b> lists every shortcut."),
        _art_editor_listen,
    ),
)


# -- the dialog --------------------------------------------------------------------------------


class _Art(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(270)
        self.page: GuidePage | None = None
        self.korean = True

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API name
        if self.page is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(_color("border"), 1))
        p.setBrush(_color("field"))
        p.drawRoundedRect(area, 10, 10)
        self.page.art(p, area.adjusted(26, 20, -26, -20), self.korean)


class GuideDialog(QDialog):
    """Pages of illustration + text with Back / Next; Skip closes it."""

    def __init__(self, pages: tuple[GuidePage, ...], title: tuple[str, str], korean: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("guideDialog")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self.setMinimumSize(640, 520)
        self.resize(720, 560)
        self.pages = pages
        self.korean = korean
        self.index = 0
        self.setWindowTitle(title[0] if korean else title[1])
        self.art = _Art(self)
        self.art.korean = korean
        self.title_label = QLabel()
        self.title_label.setObjectName("dialogTitle")
        self.body_label = QLabel()
        self.body_label.setWordWrap(True)
        self.body_label.setTextFormat(Qt.TextFormat.RichText)
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        font = self.body_label.font()
        font.setPointSizeF(font.pointSizeF() + 1)
        self.body_label.setFont(font)
        self.steps_label = QLabel()
        self.steps_label.setTextFormat(Qt.TextFormat.RichText)
        self.skip_button = QPushButton("건너뛰기" if korean else "Skip")
        self.skip_button.setFlat(True)
        self.back_button = QPushButton("이전" if korean else "Back")
        self.next_button = QPushButton()
        self.next_button.setObjectName("primaryButton")
        self.next_button.setDefault(True)
        self.next_button.setMinimumWidth(110)
        self.skip_button.clicked.connect(self.reject)
        self.back_button.clicked.connect(lambda: self._show(self.index - 1))
        self.next_button.clicked.connect(self._next)

        buttons = QHBoxLayout()
        buttons.addWidget(self.steps_label)
        buttons.addStretch(1)
        buttons.addWidget(self.skip_button)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(14)
        layout.addWidget(self.art, 5)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label, 3)
        layout.addLayout(buttons)
        self._show(0)

    def _next(self) -> None:
        if self.index + 1 < len(self.pages):
            self._show(self.index + 1)
        else:
            self.accept()

    def _show(self, index: int) -> None:
        self.index = max(0, min(len(self.pages) - 1, index))
        page = self.pages[self.index]
        last = self.index == len(self.pages) - 1
        self.art.page = page
        self.art.update()
        self.title_label.setText(page.title[0] if self.korean else page.title[1])
        self.body_label.setText(page.body[0] if self.korean else page.body[1])
        self.steps_label.setText(" ".join(
            f"<span style='color:{COLORS['accent'] if step == self.index else COLORS['border']};"
            f"font-size:20px'>●</span>" for step in range(len(self.pages))))
        self.back_button.setEnabled(self.index > 0)
        self.skip_button.setVisible(not last)
        self.next_button.setText(("시작하기" if self.korean else "Get started") if last
                                 else ("다음" if self.korean else "Next"))
