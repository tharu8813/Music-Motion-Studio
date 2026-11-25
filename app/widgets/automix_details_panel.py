"""Preview's mix status line and the "Transition details" entry point.

Read-only: it holds ``app.automix.diagnostics.transition_rows`` of the plan
Preview is playing (and the plan itself) for TransitionInspectorWindow, which
draws them, and never analyzes, plans or renders anything itself.
"""

from __future__ import annotations

import math

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from PySide6.QtCore import Qt, Signal

from app.automix.diagnostics import transition_rows
from app.utils.i18n import Language, Translator

_STYLE_LABELS = {
    "beat_roll": ("비트 롤", "Beat roll"),
    "lowpass_out": ("로우패스 아웃", "Lowpass out"),
    "bass_swap": ("베이스 스왑", "Bass swap"),
    "vocal_safe_eq": ("보컬 보호 EQ", "Vocal-safe EQ"),
    "filter_blend": ("필터 블렌드", "Filter blend"),
    "short_fade": ("크로스페이드", "Crossfade"),
    "filter_sweep": ("필터 스윕", "Filter sweep"),
    "drop_in": ("여운 위로 시작", "Drop in over the tail"),
    "echo_out": ("에코 아웃", "Echo out"),
    "tape_stop": ("테이프 스톱", "Tape stop"),
    "downbeat_cut": ("컷", "Cut"),
    "legacy": ("크로스페이드", "Crossfade"),
    "eq": ("EQ 직접 설정", "Custom EQ"),
    "sequential": ("이어서 재생", "Back to back"),
    "gap": ("간격", "Gap"),
}
_DETAIL_FIELDS = (
    # (row key, Korean label, English label)
    ("strategy", "전략", "Strategy"),
    ("bars", "마디", "Bars"),
    ("score", "후보 점수", "Candidate score"),
    ("acoustic_cost", "음향 충돌 예측 비용", "Predicted acoustic cost"),
    ("source_level_delta_db", "전환 전후 예측 음량 차이(dB)", "Predicted level change (dB)"),
    ("source_bass_delta_db", "전환 전후 예측 저역 차이(dB)", "Predicted low-band change (dB)"),
    ("overlap_beat_offset_ms", "전환 구간 박자 어긋남(ms, 90백분위)", "Overlap beat offset (ms, 90th percentile)"),
    ("predicted_rhythm_overlap_seconds", "박자가 어긋난 리듬의 예측 겹침(초 환산)", "Predicted misaligned rhythm overlap (weighted seconds)"),
    ("missing_analysis", "분석 누락으로 기본 크로스페이드 적용", "Basic crossfade used because analysis was missing"),
    ("dsp_fallback", "효과 대신 기본 크로스페이드 적용", "Basic crossfade used because effects were unavailable"),
    ("pitch_unavailable", "FFmpeg에서 음정 보정 미지원", "FFmpeg could not apply the pitch shift"),
    ("tape_stop_unavailable", "테이프 스톱 대신 짧은 페이드 적용", "Short fade used because tape stop was unavailable"),
    ("predicted_hole_db", "예측 음량 공백(dB)", "Predicted level deficit (dB)"),
    ("predicted_weakest_block_db", "앞뒤 구간 대비 최저 예측 음량(dB)", "Predicted quietest block relative to surrounding audio (dB)"),
    ("predicted_mid_high_dip_db", "중·고역 페이드로 인한 최대 예측 음량 저하(dB)", "Predicted largest level drop from mid/high fades (dB)"),
    ("predicted_low_excess_db", "앞뒤 구간 대비 예측 저역 수준(dB)", "Predicted bass level relative to surrounding audio (dB)"),
    ("continuity_tiebreak_db", "기존 후보의 최저 예측 음량(dB)", "Predicted quietest block of the original candidate (dB)"),
    ("obscured_vocal_seconds", "가려지는 보컬 예측(초)", "Predicted obscured vocals (s)"),
    ("voice_collision_seconds", "동시 보컬 예측(초)", "Predicted voice collision (s)"),
    ("skipped_vocal_seconds", "생략되는 첫 보컬(초)", "Skipped opening vocals (s)"),
    ("landing_distance_seconds", "진입점 착지 오차(초)", "Entry landing distance (s)"),
    ("outgoing_bpm", "나가는 곡 BPM", "Outgoing BPM"),
    ("incoming_bpm", "들어오는 곡 BPM", "Incoming BPM"),
    ("target_bpm", "목표 BPM", "Target BPM"),
    ("outgoing_rate", "나가는 곡 속도(전환 중)", "Outgoing rate (in overlap)"),
    ("tempo_ramp_seconds", "템포 맞춤 구간(초)", "Tempo ramp (s)"),
    ("tempo_delta_percent", "템포 차이 %", "Tempo delta %"),
    ("key_shift_semitones", "키 맞춤(반음)", "Key shift (semitones)"),
    ("skipped_seconds", "생략한 곡 끝(초)", "Skipped ending (s)"),
    ("outgoing_cue", "나가는 곡 큐(초)", "Outgoing cue (s)"),
    ("incoming_cue", "들어오는 곡 큐(초)", "Incoming cue (s)"),
    ("outgoing_vocal_outro_start", "나가는 곡 마지막 보컬 끝(초)", "Outgoing last vocal ends (s)"),
    ("incoming_vocal_intro_end", "들어오는 곡 첫 보컬 시작(초)", "Incoming first vocal starts (s)"),
    ("outgoing_structure_anchor", "구조 큐: 아웃트로", "Structure cue: outro"),
    ("incoming_structure_anchor", "구조 큐: 인트로 끝", "Structure cue: intro end"),
    ("energy_delta", "에너지 차이", "Energy delta"),
    ("outgoing_genres", "나가는 구간 장르", "Outgoing region genres"),
    ("incoming_genres", "들어오는 구간 장르", "Incoming region genres"),
    ("outgoing_moods", "나가는 구간 분위기", "Outgoing region moods"),
    ("incoming_moods", "들어오는 구간 분위기", "Incoming region moods"),
    ("vocal_overlap", "보컬 겹침", "Vocal overlap"),
    ("key_clash", "키 충돌", "Key clash"),
    ("outgoing_downbeat_confidence", "나가는 곡 다운비트 신뢰도", "Outgoing downbeat confidence"),
    ("incoming_downbeat_confidence", "들어오는 곡 다운비트 신뢰도", "Incoming downbeat confidence"),
    ("outgoing_analyzer", "나가는 곡 분석기", "Outgoing analyzer"),
    ("incoming_analyzer", "들어오는 곡 분석기", "Incoming analyzer"),
)


def _value_text(value: object, korean: bool) -> str:
    if value is None:
        return "알 수 없음" if korean else "unknown"
    if isinstance(value, bool):
        return ("예" if value else "아니요") if korean else ("yes" if value else "no")
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _clock(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60:d}:{whole % 60:02d}"


class AutoMixDetailsPanel(QFrame):
    """The mix status line plus a "Transition details" button; the data source of
    TransitionInspectorWindow.

    ``changed`` fires whenever the plan, status or language changes,
    ``playhead_changed`` on every playhead update, and ``open_requested`` asks
    Preview to show the window.
    """

    changed = Signal()
    playhead_changed = Signal(float)
    open_requested = Signal()

    def __init__(
        self, translator: Translator, parent: QWidget | None = None, *, automix: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("automixDetailsPanel")
        self.translator = translator
        # Only AutoMix plans can fall back; in crossfade mode "legacy" is the plan itself.
        self.automix = automix
        self.plan = None
        self.tracks: list = []
        self.rows: list[dict[str, object]] = []
        self._windows: list[tuple[float, float]] = []
        self._current = -1
        self._state = ("waiting", None)
        self._progress_message = ""
        self._failed = False
        self.title_label = QLabel()
        self.title_label.setObjectName("panelTitle")
        self.open_window_button = QPushButton()
        self.open_window_button.clicked.connect(self.open_requested)
        self.status_label = QLabel()
        self.status_label.setObjectName("mutedLabel")
        self.status_label.setWordWrap(True)
        self.progress_label = QLabel()
        self.explanation_label = QLabel()
        for label in (self.progress_label, self.explanation_label):
            label.setObjectName("mutedLabel")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title_label)
        header.addStretch(1)
        layout.addLayout(header)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.explanation_label)
        layout.addWidget(self.open_window_button)
        self.retranslate()

    # -- state ---------------------------------------------------------------

    def set_plan(self, plan, tracks, *, state: str, ready_through: int | None = None) -> None:
        """``state``: "waiting" (no mix yet), "provisional" (partial mix) or "final"."""
        titles = {track.id: track.title or track.id for track in tracks}
        self.plan, self.tracks = plan, list(tracks)
        self.rows = transition_rows(plan, titles)
        self._windows = [
            (float(row["timeline_start"]), float(row["timeline_start"]) + float(row["duration"]))
            for row in self.rows
        ]
        self._state = (state, ready_through)
        if state == "final":
            self._failed = False
            self._progress_message = ""
        self._current = -1
        self._update_status()

    def set_progress_message(self, message: str) -> None:
        """Show preparation progress alongside the plan currently playing."""
        if message == self._progress_message and not self._failed:
            return
        self._failed = False
        self._progress_message = message or ""
        self._update_status()

    def set_failed(self) -> None:
        """The mix could not be prepared: Preview keeps playing what it already has."""
        self._failed = True
        self._progress_message = ""
        self._update_status()

    def set_playhead(self, seconds: float) -> None:
        """Mark the transition sounding at ``seconds`` (cheap: called every frame)."""
        self._current = next((index for index, (start, end) in enumerate(self._windows)
                              if start <= seconds < end), -1)
        self.playhead_changed.emit(seconds)

    @property
    def current_index(self) -> int:
        return self._current

    def as_text(self) -> str:
        korean = self._korean()
        return "\n\n".join(self.detail_text(index, korean) for index in range(len(self.rows)))

    # -- presentation ----------------------------------------------------------

    def _korean(self) -> bool:
        return self.translator.language is Language.KOREAN

    def _style(self, row: dict[str, object]) -> str:
        key = str(row["dsp"] or row["type"])
        korean, english = _STYLE_LABELS.get(key, (key, key))
        return korean if self._korean() else english

    def detail_text(self, index: int, korean: bool) -> str:
        """Every recorded fact of one junction as plain text (copy text, "All values")."""
        row = self.rows[index]
        lines = [f"{row['from']} → {row['to']}",
                 f"{'시작' if korean else 'Start'}: {_clock(float(row['timeline_start']))}"
                 f" · {'길이' if korean else 'Length'}: {float(row['duration']):.1f}s"
                 f" · {self._style(row)}"]
        for key, korean_label, english_label in _DETAIL_FIELDS:
            if key in row:
                lines.append(f"{korean_label if korean else english_label}: {_value_text(row[key], korean)}")
        reasons = str(row.get("reasons") or "")
        if reasons:
            lines.append("근거:" if korean else "Why:")
            lines.extend(f"  {reason}" for reason in reasons.split("; "))
        elif row["duration"]:
            lines.append("선택 근거 정보가 없는 전환입니다." if korean else "No selection reasons recorded for this transition.")
        elif self.automix and row["type"] == "sequential":
            lines.append(self._back_to_back_reason(korean))
        return "\n".join(lines)

    @staticmethod
    def _back_to_back_reason(korean: bool) -> str:
        return ("분석하지 못했거나 섞을 길이가 부족한 곡은 섞지 않고, "
                "앞 곡의 마지막 소리 바로 뒤에 다음 곡을 이어 재생합니다." if korean
                else "A track without analysis, or too short to blend, is not mixed: "
                     "the next track starts right after its last sound.")

    def _update_status(self) -> None:
        """One status line, symbol + text (never color alone): ● preparing, ◐ partial, ✓ final, ! fallback."""
        korean = self._korean()
        state, ready_through = self._state
        mixed = sum(1 for row in self.rows if float(row["duration"]) > 0.0)
        plain = sum(1 for row in self.rows if row["dsp"] == "legacy") if self.automix else 0
        # Planned back-to-back junctions (a provisional plan's unanalyzed tail is not planned yet).
        planned = len(self.rows) if state == "final" else (ready_through or 1) - 1
        joined = sum(1 for row in self.rows if row["type"] == "sequential" and int(row["index"]) <= planned
                     ) if self.automix else 0
        if self._failed and state == "final":
            text = ("! 새 믹스를 준비하지 못했습니다 · 이전 믹스를 계속 재생합니다" if korean
                    else "! Could not update the mix · keeping the previous mix")
        elif self._failed and state != "final":
            if state == "provisional":
                text = (f"! {ready_through}번째 곡까지만 AutoMix 적용 · 이후는 곡을 차례로 재생합니다" if korean
                        else f"! AutoMix only through track {ready_through} · then tracks play back to back")
            else:
                text = ("! 믹스를 준비하지 못해 곡을 차례로 재생합니다" if korean
                        else "! Could not prepare the mix · playing tracks back to back")
        elif state == "provisional":
            text = (f"◐ 임시 계획 · {ready_through}번째 곡까지 AutoMix 적용됨 · 전환 {mixed}개" if korean
                    else f"◐ Provisional · AutoMix through track {ready_through} · {mixed} transition(s)")
        elif not self.rows and self.plan is not None:
            text = ("전환할 곡이 없습니다 · 재생할 곡을 2개 이상 추가하세요" if korean
                    else "No transitions · add at least two playable tracks")
        elif state == "final":
            text = f"✓ 최종 계획 · 전환 {mixed}개" if korean else f"✓ Final plan · {mixed} transition(s)"
        elif self._progress_message:
            text = f"● {self._progress_message}"
        else:
            text = ("● 믹스 준비 중 · 곡을 차례로 재생합니다" if korean
                    else "● Preparing the mix · playing tracks back to back")
        if plain and state in {"provisional", "final"}:
            text += (f" · {plain}개는 기본 크로스페이드" if korean
                     else f" · {plain} as plain crossfade")
        if joined and state in {"provisional", "final"}:
            text += f" · {joined}개는 바로 이어 재생" if korean else f" · {joined} back to back"
        self.status_label.setText(text)
        explanation = " ".join(part for part in (
            ("미리보기 믹스를 만들지 못했습니다. 원인은 로그에 기록됩니다." if korean
             else "The preview mix could not be rendered; the cause is in the log.") if self._failed else "",
            ("기본 크로스페이드는 분석이 부족하거나 템포가 맞지 않는 곡 사이에 AutoMix 대신 쓰였습니다." if korean
             else "Plain crossfades replace AutoMix where analysis was incomplete or tempos did not match.")
            if plain else "",
            self._back_to_back_reason(korean) if joined else "",
            ("미리보기는 계속 재생할 수 있습니다." if korean else "Preview keeps playing.")
            if plain or self._failed else "",
        ) if part)
        self.status_label.setToolTip(f"{text}\n{explanation}".strip())
        self.status_label.setAccessibleName(("믹스 상태: " if korean else "Mix status: ") + text.lstrip("●◐✓! "))
        self.status_label.setAccessibleDescription(explanation)
        self.explanation_label.setText(explanation)
        self.explanation_label.setVisible(bool(explanation))
        self.progress_label.setText(self._progress_message)
        self.progress_label.setVisible(bool(self._progress_message) and state != "waiting" and not self._failed)
        self.open_window_button.setEnabled(bool(self.rows))
        self.changed.emit()

    def retranslate(self) -> None:
        korean = self._korean()
        self.title_label.setText("AutoMix" if self.automix else ("믹스" if korean else "Mix"))
        self.open_window_button.setText("AutoMix 전환 자세히 보기" if korean and self.automix else
                                        "AutoMix transition details" if self.automix else
                                        "전환 상세" if korean else "Transition details")
        self.open_window_button.setToolTip(
            "새 창에서 전체 믹스와 각 전환의 위치·방식·선택 근거, 대역별 음량 변화, 화면 전환 지점을 "
            "그래프로 보고 재생할 수 있습니다." if korean
            else "Open a window that graphs the whole mix and each transition -- where it sits, how it is "
                 "mixed and why, per-band levels and the Canvas switch -- with playback controls."
        )
        self._update_status()


def ready_through(plan, covered_until: float) -> int:
    """How many leading clips a partial mix covering ``covered_until`` seconds fully contains."""
    if math.isinf(covered_until):
        return len(plan.audio.clips)
    return sum(1 for clip in plan.audio.clips if clip.timeline_end <= covered_until + 1e-6)
