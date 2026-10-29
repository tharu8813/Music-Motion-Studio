"""The "Analysis" tab of Track information/settings: run one track's analysis and read it.

Display only: MainWindow runs the analysis (AutoMixAnalysisController) and feeds
this panel its step reports and results, whatever the project's transition mode.
"""

from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.automix.analysis.key import key_to_camelot
from app.automix.analysis.provider import (
    STEP_BARS, STEP_BEAT_MODEL, STEP_DECODE, STEP_KEY_ENERGY, STEP_RHYTHM, STEP_VOCALS,
)
from app.automix.analysis.service import STEP_CACHED
from app.automix.models import TrackAnalysis
from app.automix.structure.models import TrackStructureAnalysis
from app.controllers.automix_analysis_controller import STRUCTURE_STAGE

STEPS = (STEP_DECODE, STEP_RHYTHM, STEP_BARS, STEP_KEY_ENERGY, STEP_BEAT_MODEL, STEP_VOCALS, STRUCTURE_STAGE)
_RHYTHM_STEPS = STEPS[:-1]
STEP_NAMES = {
    STEP_DECODE: ("오디오 읽기", "Reading audio"),
    STEP_RHYTHM: ("템포·비트 찾기", "Tempo and beats"),
    STEP_BARS: ("마디 나누기", "Bars"),
    STEP_KEY_ENERGY: ("키·에너지·장르·분위기", "Key, energy, genre, mood"),
    STEP_BEAT_MODEL: ("비트 모델(Beat This!)", "Beat model (Beat This!)"),
    STEP_VOCALS: ("보컬 감지", "Vocal detection"),
    STRUCTURE_STAGE: ("곡 구조(인트로·구간·에너지)", "Song structure (intro, sections, energy)"),
}
_STATE_MARKS = {"pending": "○", "running": "●", "done": "✓", "cached": "✓", "skipped": "–", "failed": "!"}


def clock(seconds: float | None) -> str:
    """``m:ss`` (``h:mm:ss`` past an hour); ``—`` when unknown."""
    if seconds is None:
        return "—"
    whole = max(0, int(round(seconds)))
    minutes, second = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{second:02d}" if hours else f"{minutes}:{second:02d}"


def _percent(value: float) -> str:
    return f"{round(value * 100)}%"


def _seconds(value: float, korean: bool) -> str:
    return f"{value:.1f}초" if korean else f"{value:.1f} s"


def summary_cards(analysis: TrackAnalysis, korean: bool) -> list[tuple[str, str, str]]:
    """(caption, value, detail) for the six headline cards."""
    t = (lambda ko, en: ko if korean else en)
    cards = []
    if analysis.bpm is None:
        cards.append((t("템포", "Tempo"), "—", t("리듬을 찾지 못함", "No detectable rhythm")))
    else:
        cards.append((t("템포", "Tempo"), f"{analysis.bpm:.1f} BPM",
                      t(f"신뢰도 {_percent(analysis.bpm_confidence)}", f"{_percent(analysis.bpm_confidence)} confidence")))
    if analysis.key:
        camelot = key_to_camelot(analysis.key)
        cards.append((t("키", "Key"), analysis.key + (f" · {camelot}" if camelot else ""),
                      t(f"신뢰도 {_percent(analysis.key_confidence)}", f"{_percent(analysis.key_confidence)} confidence")))
    else:
        cards.append((t("키", "Key"), "—", t("알 수 없음", "Unknown")))
    if analysis.meter_numerator and analysis.meter_denominator:
        cards.append((t("박자", "Meter"), f"{analysis.meter_numerator}/{analysis.meter_denominator}",
                      t(f"마디 신뢰도 {_percent(analysis.meter_confidence)}",
                        f"{_percent(analysis.meter_confidence)} bar confidence")))
    else:
        cards.append((t("박자", "Meter"), "—", t("마디를 찾지 못함", "No bar structure found")))
    if analysis.energy is None:
        cards.append((t("에너지", "Energy"), "—", t("알 수 없음", "Unknown")))
    else:
        level = (t("높음", "High") if analysis.energy >= 0.66 else
                 t("보통", "Medium") if analysis.energy >= 0.33 else t("낮음", "Low"))
        cards.append((t("에너지", "Energy"), f"{analysis.energy:.2f}", level))
    cards.append((t("보컬", "Vocals"), *_vocal_card(analysis, korean)))
    quality = analysis.beat_alignment_quality()
    cards.append((t("믹스 준비도", "Mix readiness"), *{
        "reliable": (t("좋음", "Good"), t("마디에 맞춰 섞을 수 있음", "Can mix on the bar grid")),
        "bpm_only": (t("보통", "Fair"), t("템포만 확인됨", "Tempo only")),
        "insufficient": (t("낮음", "Low"), t("기본 크로스페이드로 연결", "Plain crossfade")),
    }[quality]))
    return cards


def _vocal_card(analysis: TrackAnalysis, korean: bool) -> tuple[str, str]:
    t = (lambda ko, en: ko if korean else en)
    measured = analysis.vocal_coverage
    if measured is None and not analysis.vocal_activity or measured == ():
        return t("알 수 없음", "Unknown"), t("보컬 감지를 하지 않음", "Vocals were not measured")
    if not analysis.vocal_activity:
        return t("없음", "None"), t("곡의 앞·뒤에서 보컬을 찾지 못함", "No singing at the start or end")
    first, last = analysis.vocal_activity[0][0], analysis.vocal_activity[-1][1]
    return (t(f"{clock(first)}부터", f"From {clock(first)}"),
            t(f"마지막 보컬 {clock(last)}", f"Last vocal at {clock(last)}"))


def analysis_facts(
    analysis: TrackAnalysis, structure: TrackStructureAnalysis | None, korean: bool,
) -> list[tuple[str, str]]:
    """(name, value) rows for the details list under the timeline."""
    t = (lambda ko, en: ko if korean else en)
    duration = analysis.duration_seconds
    bars = len(analysis.downbeats)
    rows = [(t("비트 / 마디", "Beats / bars"),
             t(f"비트 {len(analysis.beats)}개 · {bars}마디", f"{len(analysis.beats)} beats · {bars} bars"))]
    if analysis.audible_start_seconds is not None:
        rows.append((t("시작 무음", "Leading silence"), _seconds(analysis.audible_start_seconds, korean)))
    if analysis.audible_end_seconds is not None:
        tail = max(0.0, duration - analysis.audible_end_seconds)
        rows.append((t("소리 끝", "Sound ends"),
                     t(f"{clock(analysis.audible_end_seconds)} (끝 무음 {_seconds(tail, True)})",
                       f"{clock(analysis.audible_end_seconds)} ({_seconds(tail, False)} of trailing silence)")))
    if analysis.decay_start_seconds is not None:
        end = analysis.audible_end_seconds if analysis.audible_end_seconds is not None else duration
        rows.append((t("여운 시작", "Fade-out starts"),
                     t(f"{clock(analysis.decay_start_seconds)} (여운 {_seconds(max(0.0, end - analysis.decay_start_seconds), True)})",
                       f"{clock(analysis.decay_start_seconds)} ({_seconds(max(0.0, end - analysis.decay_start_seconds), False)} tail)")))
    if analysis.vocal_coverage:
        spans = ", ".join(f"{clock(start)}–{clock(end)}" for start, end in analysis.vocal_coverage)
        rows.append((t("보컬 측정 구간", "Vocals measured in"), spans))
    if structure is not None:
        if structure.intro_end_seconds is not None:
            rows.append((t("인트로 끝", "Intro ends"), clock(structure.intro_end_seconds)))
        if structure.outro_start_seconds is not None:
            rows.append((t("아웃트로 시작", "Outro starts"), clock(structure.outro_start_seconds)))
        if structure.sections:
            starts = " · ".join(clock(section.start_seconds) for section in structure.sections)
            rows.append((t("구간", "Sections"), t(f"{len(structure.sections)}개 · {starts}",
                                                   f"{len(structure.sections)} · {starts}")))
    analyzers = analysis.analyzer_id or "—"
    if structure is not None and structure.analyzer_id:
        analyzers += f" · {structure.analyzer_id}"
    rows.append((t("분석기", "Analyzers"), analyzers))
    return rows


class TrackAnalysisTimeline(QWidget):
    """The whole track at a glance: energy, sections, intro/outro, silence, vocals, bars."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.analysis: TrackAnalysis | None = None
        self.structure: TrackStructureAnalysis | None = None
        self.empty_text = ""
        self.setMinimumHeight(128)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, analysis: TrackAnalysis | None, structure: TrackStructureAnalysis | None) -> None:
        self.analysis, self.structure = analysis, structure
        self.update()

    @staticmethod
    def colors(palette: QPalette) -> dict[str, QColor]:
        accent = palette.color(QPalette.ColorRole.Highlight)
        text = palette.color(QPalette.ColorRole.Text)
        return {
            "energy": accent,
            "vocal": QColor("#E7A13C"),
            "edge": QColor("#9B8CFF"),  # apart from the theme's teal accent used for energy
            "silence": QColor(text.red(), text.green(), text.blue(), 40),
            "grid": QColor(text.red(), text.green(), text.blue(), 60),
            "text": text,
        }

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = self.colors(self.palette())
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(colors["grid"]))
        painter.setBrush(self.palette().color(QPalette.ColorRole.Base))
        painter.drawRoundedRect(frame, 7, 7)
        analysis = self.analysis
        duration = analysis.duration_seconds if analysis is not None else 0.0
        if analysis is None or duration <= 0.0:
            painter.setPen(colors["text"])
            painter.drawText(frame, Qt.AlignmentFlag.AlignCenter, self.empty_text)
            return
        plot = frame.adjusted(10, 16, -10, -30)
        vocal_lane = QRectF(plot.left(), plot.bottom() + 6, plot.width(), 7)

        def x_at(seconds: float) -> float:
            return plot.left() + plot.width() * max(0.0, min(1.0, seconds / duration))

        def band(start: float, end: float, color: QColor, rect: QRectF = plot) -> None:
            if end > start:
                painter.fillRect(QRectF(x_at(start), rect.top(), x_at(end) - x_at(start), rect.height()), color)

        structure = self.structure
        edge = QColor(colors["edge"])
        edge.setAlpha(45)
        if structure is not None:
            if structure.intro_end_seconds:
                band(0.0, structure.intro_end_seconds, edge)
            if structure.outro_start_seconds is not None:
                band(structure.outro_start_seconds, duration, edge)
        if analysis.audible_start_seconds:
            band(0.0, analysis.audible_start_seconds, colors["silence"])
        if analysis.audible_end_seconds is not None:
            band(analysis.audible_end_seconds, duration, colors["silence"])

        if structure is not None and structure.energy_curve and structure.energy_curve_hop_seconds:
            peak = max(structure.energy_curve) or 1.0
            path = QPainterPath(QPointF(plot.left(), plot.bottom()))
            for index, value in enumerate(structure.energy_curve):
                path.lineTo(x_at(index * structure.energy_curve_hop_seconds),
                            plot.bottom() - plot.height() * value / peak)
            path.lineTo(x_at(len(structure.energy_curve) * structure.energy_curve_hop_seconds), plot.bottom())
            path.closeSubpath()
            fill = QColor(colors["energy"])
            fill.setAlpha(90)
            painter.fillPath(path, fill)
            painter.setPen(QPen(colors["energy"], 1.4))
            painter.drawPath(path)
        elif analysis.energy is not None:  # no curve: the track's overall level as a flat line
            y = plot.bottom() - plot.height() * analysis.energy
            painter.setPen(QPen(colors["energy"], 1.4, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        painter.setPen(QPen(colors["grid"], 1))
        for downbeat in analysis.downbeats:  # bar ticks along the top edge
            x = x_at(downbeat)
            painter.drawLine(QPointF(x, plot.top() - 6), QPointF(x, plot.top() - 2))
        if structure is not None:
            painter.setPen(QPen(colors["text"], 1, Qt.PenStyle.DotLine))
            for section in structure.sections[1:]:
                x = x_at(section.start_seconds)
                painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        if analysis.decay_start_seconds is not None:
            painter.setPen(QPen(colors["edge"], 1.2, Qt.PenStyle.DashLine))
            x = x_at(analysis.decay_start_seconds)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

        measured = QColor(colors["vocal"])
        measured.setAlpha(55)
        for start, end in analysis.vocal_coverage or ():
            band(start, end, measured, vocal_lane)
        for start, end in analysis.vocal_activity:
            band(start, end, colors["vocal"], vocal_lane)

        painter.setPen(colors["text"])
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        painter.setFont(font)
        labels = QRectF(plot.left(), vocal_lane.bottom() + 2, plot.width(), 14)
        painter.drawText(labels, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "0:00")
        painter.drawText(labels, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, clock(duration / 2))
        painter.drawText(labels, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, clock(duration))


class TrackAnalysisPanel(QWidget):
    """Analyze button, live step-by-step progress, and the analysis read-out."""

    analyze_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.korean = True
        self.analysis: TrackAnalysis | None = None
        self.structure: TrackStructureAnalysis | None = None
        self.running = False
        self.error = ""
        self.step_states: dict[str, str] = {step: "pending" for step in STEPS}
        self._fraction = 0.0
        self._elapsed = QElapsedTimer()
        self._elapsed_seconds = 0.0
        self._tick = QTimer(self)
        self._tick.setInterval(200)
        self._tick.timeout.connect(self._refresh_progress)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        header = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("mutedLabel")
        self.analyze_button = QPushButton()
        self.analyze_button.setProperty("primary", True)
        self.analyze_button.clicked.connect(self.analyze_requested)
        header.addWidget(self.status_label, 1)
        header.addWidget(self.analyze_button, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(header)

        self.progress_box = QFrame()
        self.progress_box.setObjectName("analysisProgressBox")
        progress_layout = QVBoxLayout(self.progress_box)
        progress_layout.setContentsMargins(10, 8, 10, 8)
        progress_layout.setSpacing(6)
        bar_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.elapsed_label = QLabel()
        self.elapsed_label.setObjectName("mutedLabel")
        bar_row.addWidget(self.progress_bar, 1)
        bar_row.addWidget(self.elapsed_label)
        progress_layout.addLayout(bar_row)
        steps_grid = QGridLayout()
        steps_grid.setHorizontalSpacing(18)
        steps_grid.setVerticalSpacing(3)
        self.step_labels: dict[str, QLabel] = {}
        for index, step in enumerate(STEPS):
            label = QLabel()
            steps_grid.addWidget(label, index % 4, index // 4)
            self.step_labels[step] = label
        progress_layout.addLayout(steps_grid)
        root.addWidget(self.progress_box)

        self.results = QWidget()
        results_layout = QVBoxLayout(self.results)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(10)
        cards = QGridLayout()
        cards.setSpacing(8)
        self.cards: list[tuple[QLabel, QLabel, QLabel]] = []
        for index in range(6):
            card = QFrame()
            card.setObjectName("analysisCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(1)
            caption, value, detail = QLabel(), QLabel(), QLabel()
            caption.setObjectName("mutedLabel")
            value.setObjectName("analysisCardValue")
            detail.setObjectName("mutedLabel")
            detail.setWordWrap(True)
            for label in (caption, value, detail):
                card_layout.addWidget(label)
            cards.addWidget(card, index // 3, index % 3)
            self.cards.append((caption, value, detail))
        results_layout.addLayout(cards)
        self.timeline = TrackAnalysisTimeline()
        results_layout.addWidget(self.timeline)
        self.legend = QLabel()
        self.legend.setObjectName("mutedLabel")
        self.legend.setWordWrap(True)
        results_layout.addWidget(self.legend)
        self.facts_form = QFormLayout()
        self.facts_form.setHorizontalSpacing(14)
        self.facts_form.setVerticalSpacing(4)
        results_layout.addLayout(self.facts_form)
        root.addWidget(self.results)
        root.addStretch(1)
        self._render()

    # -- state fed by MainWindow --------------------------------------------

    def set_results(self, analysis: TrackAnalysis | None, structure: TrackStructureAnalysis | None = None) -> None:
        self.analysis = analysis
        if structure is not None or analysis is None:
            self.structure = structure
        self._render()

    def set_structure(self, structure: TrackStructureAnalysis | None) -> None:
        self.structure = structure
        self._render()

    def begin_analysis(self) -> None:
        self.running = True
        self.error = ""
        self.step_states = {step: "pending" for step in STEPS}
        self._fraction = 0.0
        self._elapsed.start()
        self._tick.start()
        self._render()

    def set_step(self, step: str, fraction: float) -> None:
        if not self.running:
            return
        if step == STEP_CACHED:
            for name in _RHYTHM_STEPS:
                self.step_states[name] = "cached"
        elif step in self.step_states:
            index = STEPS.index(step)
            for name in STEPS[:index]:
                state = self.step_states[name]
                self.step_states[name] = "done" if state == "running" else "skipped" if state == "pending" else state
            self.step_states[step] = "running"
        self._fraction = 0.9 if step == STRUCTURE_STAGE else max(self._fraction, 0.85 * fraction)
        self._render()

    def finish_analysis(self, error: str = "") -> None:
        if not self.running:
            return
        self.running = False
        self.error = error
        self._elapsed_seconds = self._elapsed.elapsed() / 1000.0
        self._tick.stop()
        for name, state in self.step_states.items():
            if state == "running":
                self.step_states[name] = "failed" if error else "done"
            elif state == "pending":
                self.step_states[name] = "skipped"
        self._fraction = 1.0
        self._render()

    def retranslate(self, korean: bool) -> None:
        self.korean = korean
        self._render()

    # -- presentation ---------------------------------------------------------

    def _t(self, korean: str, english: str) -> str:
        return korean if self.korean else english

    def _render(self) -> None:
        t = self._t
        analysis = self.analysis
        if self.running:
            status = t("분석하는 중입니다. 창을 닫아도 분석은 계속되고, 결과는 플레이리스트에 반영됩니다.",
                       "Analyzing. Closing this window does not stop it; the result still reaches the Playlist.")
        elif self.error:
            status = t(f"분석하지 못했습니다: {self.error}", f"Analysis failed: {self.error}")
        elif analysis is None:
            status = t("아직 분석하지 않았습니다. AutoMix를 쓰지 않아도 이 곡만 바로 분석할 수 있고, "
                       "결과는 저장되어 AutoMix 미리보기와 내보내기에서도 다시 쓰입니다.",
                       "Not analyzed yet. Analyze just this track now, AutoMix or not; the result is "
                       "stored and reused by AutoMix Preview and Export.")
        else:
            status = t("분석 결과입니다. 오디오 파일이 바뀌면 다시 분석하세요.",
                       "Analysis result. Analyze again if the audio file changed.")
        self.status_label.setText(status)
        self.analyze_button.setEnabled(not self.running)
        self.analyze_button.setText(
            t("분석 중…", "Analyzing…") if self.running else
            t("이 곡 분석", "Analyze this track") if analysis is None else t("다시 분석", "Analyze again")
        )
        self.analyze_button.setToolTip(t(
            "파일이 그대로면 저장된 결과를 바로 불러오고, 바뀌었으면 새로 분석합니다.",
            "An unchanged file loads its stored result at once; a changed one is analyzed again.",
        ))
        self.progress_box.setVisible(self.running or self.step_states != {step: "pending" for step in STEPS})
        for step, label in self.step_labels.items():
            state = self.step_states[step]
            name = STEP_NAMES[step][0 if self.korean else 1]
            suffix = {"cached": t(" (저장된 결과)", " (stored result)"),
                      "skipped": t(" (해당 없음)", " (not needed)")}.get(state, "")
            label.setText(f"{_STATE_MARKS[state]}  {name}{suffix}")
            label.setObjectName("" if state in ("running", "done", "cached") else "mutedLabel")
            label.setStyleSheet("font-weight: 700;" if state == "running" else "")
            label.style().unpolish(label)
            label.style().polish(label)
        self._refresh_progress()

        self.results.setVisible(analysis is not None)
        self.timeline.empty_text = t("분석 결과가 없습니다", "No analysis yet")
        self.timeline.set_data(analysis, self.structure)
        if analysis is None:
            return
        for (caption, value, detail), card in zip(self.cards, summary_cards(analysis, self.korean)):
            caption.setText(card[0])
            value.setText(card[1])
            detail.setText(card[2])
        colors = TrackAnalysisTimeline.colors(self.palette())
        legend = [
            (colors["energy"], t("에너지 곡선", "Energy")),
            (colors["vocal"], t("보컬", "Vocals")),
            (colors["edge"], t("인트로·아웃트로 / 여운 시작(점선)", "Intro, outro / fade-out start (dashed)")),
            (colors["grid"], t("무음 · 위쪽 눈금은 마디", "Silence · ticks on top are bars")),
        ]
        self.legend.setText("   ".join(
            f"<span style='color:{color.name()}'>■</span> {name}" for color, name in legend
        ))
        while self.facts_form.rowCount():
            self.facts_form.removeRow(0)
        for name, value in analysis_facts(analysis, self.structure, self.korean):
            value_label = QLabel(value)
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            name_label = QLabel(name)
            name_label.setObjectName("mutedLabel")
            self.facts_form.addRow(name_label, value_label)

    def _refresh_progress(self) -> None:
        seconds = self._elapsed.elapsed() / 1000.0 if self.running else self._elapsed_seconds
        self.progress_bar.setValue(round(1000 * self._fraction))
        if self.running:
            self.elapsed_label.setText(self._t(f"경과 {seconds:.1f}초", f"{seconds:.1f} s elapsed"))
        elif self.error:
            self.elapsed_label.setText(self._t("실패", "Failed"))
        else:
            self.elapsed_label.setText(self._t(f"완료 · {seconds:.1f}초", f"Done · {seconds:.1f} s"))
