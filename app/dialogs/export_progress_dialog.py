"""Modeless export progress dialog with clear status, ETA, log, and cancellation."""

from __future__ import annotations

from app.ui.design_system import COLORS

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import re
from time import monotonic

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QListWidget,
    QVBoxLayout,
    QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.services.export_storage_service import (
    ExportStorageEstimate,
    ExportStorageSnapshot,
    format_bytes,
)
from app.renderer.progress_text import audio_progress_text
from app.utils.time_format import format_clock


@dataclass(frozen=True, slots=True)
class _EtaSample:
    """One monotonic export-progress observation."""

    timestamp: float
    fraction: float
    stage: str


class ExportEtaEstimator:
    """Estimate completion from stable overall and recent stage-local rates.

    Export progress is deliberately weighted rather than proportional to wall
    time. Canvas capture, audio work, visualizers, and the final encoder can
    therefore run at very different speeds. A whole-export average alone makes
    the ETA jump at every stage boundary; a recent rate alone incorrectly
    assumes that the current stage represents all remaining work. Blend both
    signals and smooth the predicted finish time instead.
    """

    _WINDOW_SECONDS = 30.0
    _MAX_SAMPLES = 80

    def __init__(self, started_at: float) -> None:
        self.started_at = float(started_at)
        self._samples: deque[_EtaSample] = deque(maxlen=self._MAX_SAMPLES)
        self._samples.append(_EtaSample(self.started_at, 0.0, ""))
        self._last_fraction = 0.0
        self._last_observed_at = self.started_at
        self._predicted_finish_at: float | None = None

    def update(self, stage: str, fraction: float, now: float) -> float | None:
        """Return a smoothed remaining duration for one progress observation."""
        now = max(self.started_at, float(now))
        fraction = max(0.0, min(1.0, float(fraction)))
        if fraction >= 1.0:
            self._last_fraction = 1.0
            self._predicted_finish_at = now
            return 0.0

        # Ignore duplicate callbacks for rate calculation, but keep counting
        # down the last stable prediction while a subprocess reports one value.
        if fraction <= self._last_fraction + 1e-6:
            self._last_observed_at = now
            return self.remaining(now)

        sample = _EtaSample(now, fraction, stage)
        self._samples.append(sample)
        self._last_fraction = fraction
        previous_observed_at = self._last_observed_at
        self._last_observed_at = now

        elapsed = now - self.started_at
        if elapsed <= 0.0 or fraction <= 0.02:
            return None
        overall_rate = fraction / elapsed
        if overall_rate <= 0.0:
            return None

        stage_samples = [
            entry for entry in self._samples
            if entry.stage == stage and now - entry.timestamp <= self._WINDOW_SECONDS
        ]
        stage_rate: float | None = None
        stage_elapsed = 0.0
        stage_delta = 0.0
        if len(stage_samples) >= 2:
            first = stage_samples[0]
            stage_elapsed = now - first.timestamp
            stage_delta = fraction - first.fraction
            if stage_elapsed >= 0.5 and stage_delta >= 0.002:
                stage_rate = stage_delta / stage_elapsed

        rate = overall_rate
        if stage_rate is not None:
            # Require several seconds and measurable progress before trusting a
            # stage-local speed. Near completion it receives more weight because
            # the active encoder normally owns nearly all remaining work.
            confidence = min(0.70, stage_elapsed / 12.0, stage_delta / 0.06)
            if fraction >= 0.80:
                confidence = min(0.82, confidence + 0.12)
            bounded_stage_rate = max(
                overall_rate * 0.20,
                min(overall_rate * 5.0, stage_rate),
            )
            rate = overall_rate * (1.0 - confidence) + bounded_stage_rate * confidence

        raw_remaining = (1.0 - fraction) / max(rate, 1e-9)
        raw_finish_at = now + raw_remaining
        if self._predicted_finish_at is None:
            self._predicted_finish_at = raw_finish_at
        else:
            # Smooth the completion timestamp, not the duration. This lets the
            # displayed ETA count down naturally between slightly noisy samples.
            delta_seconds = max(0.0, now - previous_observed_at)
            alpha = min(0.42, max(0.16, 0.18 + delta_seconds / 30.0))
            self._predicted_finish_at = (
                self._predicted_finish_at * (1.0 - alpha)
                + raw_finish_at * alpha
            )
        return self.remaining(now)

    def remaining(self, now: float) -> float | None:
        """Count down the last prediction while progress is temporarily busy."""
        if self._predicted_finish_at is None:
            return None
        remaining = self._predicted_finish_at - float(now)
        if remaining <= 0.0 and self._last_fraction < 1.0:
            # A stalled stage invalidates an expired prediction. Showing
            # ``00:00`` indefinitely is more misleading than recalculating.
            return None
        return max(0.0, remaining)


class ExportDetailsList(QListWidget):
    """Readable status history presented as UI rows instead of a console."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("exportDetailsList")
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMaximumHeight(190)
        self.setAlternatingRowColors(True)

    def add_detail(self, text: str) -> None:
        if not text or (self.count() and self.item(self.count() - 1).text() == text):
            return
        self.addItem(text)
        while self.count() > 80:
            self.takeItem(0)
        self.scrollToBottom()

    def toPlainText(self) -> str:
        return "\n".join(self.item(index).text() for index in range(self.count()))


class ExportProgressDialog(QDialog):
    """Display a comprehensible in-flight FFmpeg export and allow safe cancellation."""

    cancel_requested = Signal()
    minimize_requested = Signal()
    _EXPORT_STEPS = ("visuals", "audio", "effects", "encode", "complete")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        install_help_shortcut(self, ("other", "export_process"))
        self.setModal(False)
        # Grow the window to fit its content instead of letting QVBoxLayout
        # crush word-wrapped labels and cards when a status line wraps to more
        # rows or a collapsible section is shown.
        self.resize(700, 620)
        self._started_at = monotonic()
        self._eta_estimator = ExportEtaEstimator(self._started_at)
        self._cancelling = False
        self._allow_close = False
        self._last_log = ""
        self._korean = False
        self._audio_only = False
        self._cancel_title = "Cancel export"
        self._cancel_message = (
            "Cancel the current export?\n"
            "Prepared temporary frames and the active render will be discarded."
        )
        self._export_track_count = 0
        self._export_duration_seconds = 0.0
        self._export_settings_summary = ""
        self._export_output_path = ""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        # Never shrink below what the current content needs; a wrapped status
        # line or an expanded section enlarges the dialog rather than
        # compressing the cards above it.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.steps_heading = QLabel("Export steps")
        self.steps_heading.setObjectName("panelTitle")
        self.steps_widget = QFrame()
        self.steps_widget.setObjectName("settingsStatusCard")
        self.steps_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
        )
        steps_layout = QHBoxLayout(self.steps_widget)
        steps_layout.setContentsMargins(14, 10, 14, 10)
        steps_layout.setSpacing(5)
        self.step_labels: list[QLabel] = []
        for index, _step in enumerate(self._EXPORT_STEPS):
            step_label = QLabel()
            step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            step_label.setMinimumWidth(88)
            step_label.setTextFormat(Qt.TextFormat.RichText)
            self.step_labels.append(step_label)
            steps_layout.addWidget(step_label, 1)
            if index < len(self._EXPORT_STEPS) - 1:
                separator = QLabel("—")
                separator.setObjectName("mutedLabel")
                separator.setAlignment(Qt.AlignmentFlag.AlignCenter)
                steps_layout.addWidget(separator)
        self.export_settings_heading = QLabel("Export settings")
        self.export_settings_heading.setObjectName("panelTitle")
        self.export_settings_label = QLabel()
        self.export_settings_label.setObjectName("infoCallout")
        self.export_settings_label.setWordWrap(True)
        self.export_settings_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum,
        )
        self.export_settings_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.stage_label = QLabel("Preparing export")
        self.stage_label.setObjectName("panelTitle")
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("panelTitle")
        self.detail_label = QLabel("Preparing temporary files")
        self.detail_label.setObjectName("mutedLabel")
        self.detail_label.setWordWrap(True)
        # Reserve two lines so a status message that wraps mid-export does not
        # reflow the whole dialog on every progress tick.
        self.detail_label.setMinimumHeight(
            round(self.detail_label.fontMetrics().lineSpacing() * 2.2)
        )
        self.detail_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.time_label = QLabel("Elapsed 00:00 · Calculating remaining time")
        self.time_label.setObjectName("mutedLabel")
        self.storage_heading = QLabel("Storage use")
        self.storage_heading.setObjectName("panelTitle")
        self.storage_widget = QFrame()
        self.storage_widget.setObjectName("settingsStatusCard")
        self.storage_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed,
        )
        storage_layout = QGridLayout(self.storage_widget)
        storage_layout.setContentsMargins(14, 10, 14, 10)
        storage_layout.setHorizontalSpacing(14)
        storage_layout.setVerticalSpacing(4)
        self.storage_rows: dict[str, tuple[QLabel, QLabel]] = {}
        for row, key in enumerate(("visuals", "audio", "effects", "processing", "output")):
            name_label = QLabel()
            name_label.setObjectName("mutedLabel")
            value_label = QLabel("0 B")
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.storage_rows[key] = (name_label, value_label)
            storage_layout.addWidget(name_label, row, 0)
            storage_layout.addWidget(value_label, row, 1)
        self.storage_total_label = QLabel()
        self.storage_total_label.setObjectName("panelTitle")
        self.storage_total_value = QLabel("0 B")
        self.storage_total_value.setObjectName("panelTitle")
        self.storage_total_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        storage_layout.addWidget(self.storage_total_label, 5, 0)
        storage_layout.addWidget(self.storage_total_value, 5, 1)
        self.storage_estimate_hint = QLabel()
        self.storage_estimate_hint.setObjectName("mutedLabel")
        self.storage_estimate_hint.setWordWrap(True)
        self.storage_disk_label = QLabel()
        self.storage_disk_label.setObjectName("mutedLabel")
        self.storage_disk_bar = QProgressBar()
        self.storage_disk_bar.setRange(0, 1000)
        self.storage_disk_bar.setTextVisible(False)
        self.storage_disk_bar.setFixedHeight(8)
        storage_layout.addWidget(self.storage_estimate_hint, 6, 0, 1, 2)
        storage_layout.addWidget(self.storage_disk_label, 7, 0, 1, 2)
        storage_layout.addWidget(self.storage_disk_bar, 8, 0, 1, 2)
        storage_layout.setColumnStretch(0, 1)
        self._storage_estimate: ExportStorageEstimate | None = None
        self.log_heading = QLabel("Activity")
        self.log_heading.setObjectName("panelTitle")
        self.log_output = ExportDetailsList()
        self.log_heading.hide()
        self.log_output.hide()
        self.storage_button = QPushButton("Hide storage use")
        self.storage_button.setCheckable(True)
        self.storage_button.setChecked(True)
        self.storage_button.setToolTip(
            "Show or hide the live storage-use breakdown."
        )
        self.storage_button.toggled.connect(self._set_storage_visible)
        self.details_button = QPushButton("Show detailed status")
        self.details_button.setCheckable(True)
        self.details_button.setToolTip(
            "Show the detailed export status history."
        )
        self.details_button.toggled.connect(self._set_details_visible)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.minimize_button = self.buttons.addButton(
            "Minimize", QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.minimize_button.clicked.connect(self.minimize_requested.emit)
        self.cancel_button.clicked.connect(self.request_cancel)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress_bar, 1)
        progress_row.addWidget(self.percent_label)
        layout.addWidget(self.steps_heading)
        layout.addWidget(self.steps_widget)
        layout.addWidget(self.export_settings_heading)
        layout.addWidget(self.export_settings_label)
        layout.addWidget(self.stage_label)
        layout.addLayout(progress_row)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.time_label)
        layout.addWidget(self.storage_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.storage_heading)
        layout.addWidget(self.storage_widget)
        layout.addWidget(self.details_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.log_heading)
        layout.addWidget(self.log_output, 1)
        # When technical details are collapsed, this spacer alone consumes
        # additional window height. Without it, QVBoxLayout can distribute the
        # spare height among ordinary cards and labels, making every control
        # appear to drift apart as the user resizes the dialog. When details
        # are shown, the log view becomes the only expanding content instead.
        self.content_spacer = QSpacerItem(
            0, 0,
            QSizePolicy.Policy.Minimum,
            QSizePolicy.Policy.Expanding,
        )
        layout.addItem(self.content_spacer)
        layout.addWidget(self.buttons)
        self.setWindowTitle("Export progress")
        self._refresh_steps("Preparing visual frames")

    def set_korean(self, korean: bool) -> None:
        """Localize the export-specific labels without changing app-wide language."""
        self._korean = korean
        if korean:
            self._cancel_title = "내보내기 취소"
            self._cancel_message = (
                "현재 내보내기를 취소할까요?\n"
                "준비된 임시 프레임과 진행 중인 렌더링이 중단됩니다."
            )
            self.setWindowTitle("내보내기 진행 상황")
            self.steps_heading.setText("내보내기 단계")
            self.export_settings_heading.setText("내보내기 설정")
            self.log_heading.setText("상세 현황")
            self.storage_button.setText(
                "저장 공간 사용량 숨기기" if self.storage_button.isChecked()
                else "저장 공간 사용량 보기"
            )
            self.storage_button.setToolTip(
                "실시간 저장 공간 사용량 표시를 켜거나 끕니다."
            )
            self.details_button.setText(
                "상세 현황 숨기기" if self.details_button.isChecked()
                else "상세 현황 보기"
            )
            self.details_button.setToolTip(
                "내보내기 단계별 상세 현황을 표시합니다."
            )
            self.minimize_button.setText("최소화")
            self.cancel_button.setText("취소")
            self.stage_label.setText("내보내기 준비")
            self.time_label.setText("경과 00:00 · 남은 시간 계산 중")
        else:
            self._cancel_title = "Cancel export"
            self._cancel_message = (
                "Cancel the current export?\n"
                "Prepared temporary frames and the active render will be discarded."
            )
            self.setWindowTitle("Export progress")
            self.steps_heading.setText("Export steps")
            self.export_settings_heading.setText("Export settings")
            self.log_heading.setText("Detailed status")
            self.storage_button.setText(
                "Hide storage use" if self.storage_button.isChecked()
                else "Show storage use"
            )
            self.storage_button.setToolTip(
                "Show or hide the live storage-use breakdown."
            )
            self.details_button.setText(
                "Hide detailed status" if self.details_button.isChecked()
                else "Show detailed status"
            )
            self.details_button.setToolTip(
                "Show the detailed export status history."
            )
            self.minimize_button.setText("Minimize")
            self.cancel_button.setText("Cancel")
            self.stage_label.setText("Preparing export")
            self.time_label.setText("Elapsed 00:00 · Calculating remaining time")
        self._refresh_storage_labels()
        self._refresh_steps("Preparing visual frames")

    def set_storage_estimate(self, estimate: ExportStorageEstimate) -> None:
        """Show the expected peak and result range until live files take over."""
        self._storage_estimate = estimate
        self._refresh_storage_labels()

    def update_storage_snapshot(self, snapshot: ExportStorageSnapshot) -> None:
        """Apply one background disk-accounting sample without scanning in UI."""
        values = dict(snapshot.categories)
        values["output"] = snapshot.output_in_progress
        for key, (_name, value) in self.storage_rows.items():
            value.setText(format_bytes(values.get(key, 0)))
        self.storage_total_value.setText(format_bytes(snapshot.temporary_total))
        if snapshot.disk_total > 0:
            used = 1.0 - snapshot.disk_free / snapshot.disk_total
            self.storage_disk_bar.setValue(round(max(0.0, min(1.0, used)) * 1000))
            self.storage_disk_label.setText(
                f"컴퓨터 남은 저장 공간: {format_bytes(snapshot.disk_free)} / {format_bytes(snapshot.disk_total)}"
                if self._korean else
                f"Computer storage free: {format_bytes(snapshot.disk_free)} / {format_bytes(snapshot.disk_total)}"
            )
        else:
            self.storage_disk_bar.setValue(0)

    def _refresh_storage_labels(self) -> None:
        names = (
            {
                "visuals": "화면·프레임 파일",
                "audio": "오디오 작업 파일",
                "effects": "음악 반응 효과 파일",
                "processing": "기타 작업 파일",
                "output": "생성 중인 결과 오디오" if self._audio_only else "생성 중인 결과 영상",
            }
            if self._korean else
            {
                "visuals": "Visual/frame files",
                "audio": "Audio working files",
                "effects": "Music-reactive effect files",
                "processing": "Other working files",
                "output": "Result audio in progress" if self._audio_only else "Result video in progress",
            }
        )
        self.storage_heading.setText(
            "저장 공간 사용량" if self._korean else "Storage use"
        )
        for key, (name, _value) in self.storage_rows.items():
            name.setText(names[key])
        self.storage_total_label.setText(
            "내보내기 작업 중 사용된 파일 합계"
            if self._korean else "Total files used by this export"
        )
        if self._storage_estimate is not None:
            estimate = self._storage_estimate
            self.storage_estimate_hint.setText(
                (
                    f"예상 최대 약 {format_bytes(estimate.peak_temporary)}"
                    f" · {'결과 오디오' if self._audio_only else '결과 영상'} 약 {format_bytes(estimate.result_low)}"
                    f" ~ {format_bytes(estimate.result_high)}"
                )
                if self._korean else
                (
                    f"Estimated peak ~{format_bytes(estimate.peak_temporary)}"
                    f" · result ~{format_bytes(estimate.result_low)}"
                    f"–{format_bytes(estimate.result_high)}"
                )
            )
            self.storage_widget.setToolTip(
                (
                    f"예상 최대 작업 공간 약 {format_bytes(estimate.peak_temporary)}\n"
                    f"{'예상 결과 오디오' if self._audio_only else '예상 결과 영상'} {format_bytes(estimate.result_low)}"
                    f" ~ {format_bytes(estimate.result_high)}\n"
                    "실제 값은 파일이 생성·삭제되면서 달라집니다."
                )
                if self._korean else
                (
                    f"Estimated peak working space ~{format_bytes(estimate.peak_temporary)}\n"
                    f"Estimated result {format_bytes(estimate.result_low)}"
                    f"–{format_bytes(estimate.result_high)}\n"
                    "Live values change as working files are created and removed."
                )
            )

    def set_cancel_confirmation(self, title: str, message: str) -> None:
        """Customize confirmation text when the dialog tracks a non-export task."""
        self._cancel_title = title
        self._cancel_message = message

        self._refresh_export_details()

    def set_export_details(
        self, track_count: int, duration_seconds: float, summary: str,
        output_path: str | Path | None = None,
    ) -> None:
        """Keep the selected render configuration visible throughout export."""
        self._export_track_count = max(0, int(track_count))
        self._export_duration_seconds = max(0.0, float(duration_seconds))
        self._export_settings_summary = summary.strip()
        self._export_output_path = str(output_path or "").strip()
        self._refresh_export_details()

    def _refresh_export_details(self) -> None:
        """Render the persistent settings card in the active dialog language."""
        if not self._export_settings_summary and not self._export_output_path:
            self.export_settings_label.clear()
            self.export_settings_label.hide()
            return
        prefix = (
            f"곡 {self._export_track_count}개 · 총 재생 시간 {self._format_time(self._export_duration_seconds)}"
            if self._korean else
            f"{self._export_track_count} track(s) · {self._format_time(self._export_duration_seconds)}"
        )
        lines = [prefix]
        if self._export_settings_summary:
            lines.append(self._export_settings_summary)
        if self._export_output_path:
            lines.append(
                f"저장 위치 · {self._export_output_path}"
                if self._korean else f"Output · {self._export_output_path}"
            )
        display = "\n".join(lines)
        self.export_settings_label.setText(display)
        self.export_settings_label.setToolTip(display)
        self.export_settings_label.show()

    def set_busy(self, stage: str, message: str) -> None:
        """Show activity before a measurable FFmpeg progress stream exists."""
        display_message = self._detail_text(message)
        self.stage_label.setText(self._stage_text(stage))
        self._refresh_steps(stage)
        self.progress_bar.setRange(0, 0)
        self.percent_label.setText("…")
        self.detail_label.setText(display_message)
        now = monotonic()
        self._update_time_label(
            self._eta_estimator.remaining(now), now - self._started_at,
        )
        if display_message and display_message != self._last_log:
            self.log_output.add_detail(display_message)
            self._last_log = display_message

    def update_progress(self, stage: str, fraction: float, message: str) -> None:
        """Apply worker progress and calculate an approximate time remaining."""
        percent = round(max(0.0, min(1.0, fraction)) * 100)
        now = monotonic()
        elapsed = now - self._started_at
        display_message = self._detail_text(message)
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
        self.stage_label.setText(self._stage_text(stage))
        self._refresh_steps(stage)
        self.progress_bar.setValue(percent)
        self.percent_label.setText(f"{percent}%")
        self.detail_label.setText(display_message)
        if display_message and display_message != self._last_log:
            self.log_output.add_detail(display_message)
            self._last_log = display_message
        remaining = self._eta_estimator.update(stage, fraction, now)
        self._update_time_label(remaining, elapsed)

    def _set_storage_visible(self, visible: bool) -> None:
        """Let the user collapse the storage-use card when they don't need it."""
        self.storage_heading.setVisible(visible)
        self.storage_widget.setVisible(visible)
        if self.layout() is not None:
            self.layout().invalidate()
            self.layout().activate()
        if self._korean:
            self.storage_button.setText(
                "저장 공간 사용량 숨기기" if visible else "저장 공간 사용량 보기"
            )
        else:
            self.storage_button.setText(
                "Hide storage use" if visible else "Show storage use"
            )

    def _set_details_visible(self, visible: bool) -> None:
        """Toggle the structured status history without exposing console output."""
        self.log_heading.setVisible(visible)
        self.log_output.setVisible(visible)
        self.content_spacer.changeSize(
            0, 0,
            QSizePolicy.Policy.Minimum,
            (
                QSizePolicy.Policy.Fixed
                if visible else QSizePolicy.Policy.Expanding
            ),
        )
        if self.layout() is not None:
            self.layout().invalidate()
            self.layout().activate()
        if self._korean:
            self.details_button.setText(
                "상세 현황 숨기기" if visible else "상세 현황 보기"
            )
        else:
            self.details_button.setText(
                "Hide detailed status" if visible else "Show detailed status"
            )

    def _active_step(self, stage: str) -> int:
        try:
            return self._EXPORT_STEPS.index(self._stage_key(stage))
        except ValueError:
            return 0

    def set_audio_only(self, enabled: bool) -> None:
        """Show only audio preparation, saving, and completion for an audio export."""
        self._audio_only = enabled
        layout = self.steps_widget.layout()
        for index in (0, 1, 4, 5):
            layout.itemAt(index).widget().setVisible(not enabled)
        self._refresh_steps("Preparing audio" if enabled else "Preparing visual frames")
        self._refresh_storage_labels()

    def _step_names(self) -> tuple[str, ...]:
        if self._audio_only:
            return (("", "오디오 준비", "", "오디오 저장", "완료") if self._korean else
                    ("", "Audio", "", "Save audio", "Done"))
        return (
            ("화면 준비", "오디오 준비", "효과 준비", "영상 만들기", "완료")
            if self._korean else
            ("Visuals", "Audio", "Effects", "Create video", "Done")
        )

    def status_line(self, stage: str, message: str) -> str:
        """``stage · message`` in the dialog's language, for the status-bar progress details."""
        return f"{self._stage_text(stage)} · {self._detail_text(message)}"

    def step_progress(self, stage: str) -> list[tuple[str, float | None]]:
        """The phases before "Done" as (name, progress): 1 done, None running, 0 waiting.

        The same steps the dialog shows, for the status-bar progress details."""
        active_index = self._active_step(stage)
        return [
            (name, 1.0 if index < active_index else None if index == active_index else 0.0)
            for index, name in enumerate(self._step_names()[:-1])
            if name
        ]

    def _refresh_steps(self, stage: str) -> None:
        """Show completed, active, and upcoming export phases at a glance."""
        active_index = self._active_step(stage)
        names = self._step_names()
        for index, (label, name) in enumerate(zip(self.step_labels, names, strict=True)):
            if index < active_index:
                symbol, color, state = "✓", COLORS["success"], "completed"
                status = "완료" if self._korean else "completed"
            elif index == active_index:
                symbol, color, state = "●", COLORS["accent"], "active"
                status = "진행 중" if self._korean else "in progress"
            else:
                symbol, color, state = "○", COLORS["muted"], "pending"
                status = "대기" if self._korean else "waiting"
            label.setProperty("stepState", state)
            label.setText(
                f"<span style='color:{color};font-size:18px;font-weight:700'>"
                f"{symbol}</span><br><span style='font-weight:600'>{name}</span>"
            )
            label.setToolTip(f"{name} · {status}")
            label.setAccessibleName(f"{name}, {status}")

    @staticmethod
    def _stage_key(stage: str) -> str:
        return {
            "Preparing visual frames": "visuals",
            "Preparing export": "visuals",
            "Preparing visual layers": "visuals",
            "Preparing audio": "audio",
            "Combining audio": "audio",
            "Preparing visualizers": "effects",
            "Encoding video": "encode",
            "Encoding audio": "encode",
            "Capturing and encoding": "encode",
            "Finalizing export": "encode",
            "Complete": "complete",
        }.get(stage, "visuals")

    def _update_time_label(
        self, remaining: float | None, elapsed: float | None = None,
    ) -> None:
        """Keep the export ETA in its single dedicated label."""
        elapsed = monotonic() - self._started_at if elapsed is None else elapsed
        if remaining is None:
            self.time_label.setText(
                f"경과 {self._format_time(elapsed)} · 남은 시간 계산 중"
                if self._korean else
                f"Elapsed {self._format_time(elapsed)} · Calculating remaining time"
            )
            return
        self.time_label.setText(
            f"경과 {self._format_time(elapsed)} · 남은 시간 약 {self._format_time(remaining)}"
            if self._korean else
            f"Elapsed {self._format_time(elapsed)} · Remaining ~{self._format_time(remaining)}"
        )

    def request_cancel(self) -> bool:
        """Confirm and emit a cancellation request exactly once."""
        if self._cancelling:
            return False
        response = QMessageBox.question(
            self,
            self._cancel_title,
            self._cancel_message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return False
        self._cancelling = True
        self.cancel_button.setEnabled(False)
        self.stage_label.setText("내보내기를 안전하게 취소하는 중..." if self._korean else "Cancelling export safely...")
        self.detail_label.setText(
            "현재 프레임 준비 또는 FFmpeg 작업을 안전하게 중단하는 중" if self._korean
            else "Safely stopping the current frame preparation or FFmpeg operation"
        )
        self.log_output.add_detail("취소를 요청했습니다" if self._korean else "Cancellation requested")
        self.cancel_requested.emit()
        return True

    @property
    def is_cancelling(self) -> bool:
        """Return whether a confirmed cancellation is already in progress."""
        return self._cancelling

    def complete(self, accepted: bool) -> None:
        """Close the dialog after the worker has reached a terminal state."""
        self._allow_close = True
        if accepted:
            self.accept()
        else:
            self.reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Treat a window-close attempt as a safe cancellation request."""
        if self._allow_close:
            event.accept()
            return
        if not self._cancelling:
            self.request_cancel()
        event.ignore()

    @staticmethod
    def _format_time(seconds: float) -> str:
        return format_clock(round(seconds))

    def _stage_text(self, stage: str) -> str:
        """Translate known renderer stages while preserving diagnostic custom stages."""
        if not self._korean:
            return stage
        return {
            "Preparing visual frames": "화면 준비",
            "Preparing export": "내보내기 준비",
            "Preparing audio": "오디오 준비",
            "Combining audio": "오디오 결합",
            "Preparing visualizers": "음악 반응 효과 준비",
            "Preparing visual layers": "추가 화면 준비",
            "Encoding video": "최종 영상 만들기",
            "Encoding audio": "오디오 저장",
            "Capturing and encoding": "화면 캡처와 영상 만들기",
            "Finalizing export": "내보내기 마무리",
            "Preparing download": "다운로드 준비",
            "Downloading FFmpeg": "FFmpeg 다운로드",
            "Extracting": "압축 해제 및 설치",
            "Complete": "완료",
        }.get(stage, stage)

    def _detail_text(self, message: str) -> str:
        """Translate visualizer progress details while retaining their live numbers."""
        if self._korean and message.startswith("Saving audio"):
            return message.replace("Saving audio", "오디오 저장 중", 1)
        # Visualizer workers can provide a stage-local ETA, while this dialog
        # calculates the overall export ETA. Showing both in different rows was
        # visually inconsistent and the two estimates represented different
        # scopes. Keep all remaining-time text in ``time_label`` only.
        message = re.sub(
            r"\s*·\s*about\s+[0-9:]+\s+remaining\s*$", "", message,
            flags=re.IGNORECASE,
        )
        message = re.sub(r"\s*·\s*약\s+[0-9:]+\s+남음\s*$", "", message)
        korean_capture = re.search(
            r"화면 준비\s+(\d+)%.*?캡처\s+([\d,]+/[\d,]+)", message,
        )
        if korean_capture:
            return (
                "장면을 영상으로 준비하는 중 · "
                f"전체 {korean_capture.group(1)}% · "
                f"장면 {korean_capture.group(2)}"
            )
        korean_buffer = re.search(
            r"인코더 버퍼 처리 중\s*·\s*([\d,]+/[\d,]+)\s*프레임"
            r"\s*·\s*(\d+)%",
            message,
        )
        if korean_buffer:
            return (
                "화면 구성 요소를 영상으로 변환하는 중 · "
                f"{korean_buffer.group(1)} 프레임 · {korean_buffer.group(2)}%"
            )
        english_capture = re.search(
            r"visual preparation\s+(\d+)%.*?capture\s+([\d,]+/[\d,]+)",
            message,
            flags=re.IGNORECASE,
        )
        if english_capture:
            return (
                "Preparing scenes for the video · "
                f"{english_capture.group(1)}% overall · "
                f"{english_capture.group(2)} scenes"
            )
        english_buffer = re.search(
            r"encoder buffer\s*·\s*([\d,]+/[\d,]+)\s*frames"
            r"\s*·\s*(\d+)%",
            message,
            flags=re.IGNORECASE,
        )
        if english_buffer:
            return (
                "Converting visual elements into video · "
                f"{english_buffer.group(1)} frames · {english_buffer.group(2)}%"
            )
        audio = audio_progress_text(message, self._korean)
        if audio is not None:
            return audio
        if not self._korean:
            return message
        translated = {
            "Preparing temporary files": "임시 파일 준비 중",
            "Concatenating normalized tracks": "정규화된 오디오 트랙 결합 중",
            "Analyzing audio and rendering Python visualizer frames":
                "오디오를 분석하고 비주얼라이저 프레임을 생성하는 중",
            "Rendering Python visualizer frames": "비주얼라이저 프레임 생성 중",
            "Decoding audio for visualizers": "비주얼라이저용 오디오를 분석하는 중",
            "Analyzing visualizer frequency levels": "비주얼라이저 주파수 레벨 분석 중",
            "Analyzing waveform samples": "파형 샘플 분석 중",
            "Analyzing stereo level meter channels": "스테레오 레벨 미터 분석 중",
            "Visualizer frames complete": "비주얼라이저 프레임 준비 완료",
            "Rendering the final video": "최종 영상 렌더링 중",
            "Capturing the Canvas into the final video":
                "화면을 캡처하면서 바로 최종 영상으로 만드는 중",
            "Muxing prepared video and audio": "준비된 영상과 오디오를 결합하는 중",
            "Finishing the MP4 file": "마지막 프레임을 정리하고 MP4 파일을 마무리하는 중",
            "Audio normalization complete": "오디오 정규화 완료",
            "Export completed": "내보내기 완료",
            "Moving the completed video to the selected location":
                "완성된 영상을 선택한 위치로 이동하는 중",
            "Reading the verified release manifest": "검증된 FFmpeg 배포 정보 확인 중",
            "Using the existing verified FFmpeg version": "기존에 검증된 FFmpeg 사용 중",
            "Downloading the checksum manifest": "SHA-256 체크섬 정보 다운로드 중",
            "Checksum found; starting FFmpeg download":
                "체크섬 확인 완료 · FFmpeg 다운로드 시작",
            "Checksum verified; extracting archive safely":
                "체크섬 검증 완료 · 안전하게 압축 해제 중",
            "FFmpeg was installed and verified": "FFmpeg 설치 및 검증 완료",
            "GitHub API unavailable; using the official latest release links":
                "GitHub API를 사용할 수 없어 공식 최신 배포 주소를 사용합니다",
        }.get(message)
        if translated is not None:
            return translated
        if message.startswith("Preparing visualizer layer "):
            return message.replace(
                "Preparing visualizer layer ", "비주얼라이저 레이어 준비 중 ", 1
            )
        if message.startswith("Visualizer "):
            return (
                message.replace("Visualizer ", "비주얼라이저 ")
                .replace("All visualizers", "전체 비주얼라이저")
                .replace(" · frame ", " · 프레임 ")
            )
        if message.startswith("Normalizing audio "):
            return (
                message.replace("Normalizing audio ", "오디오 정규화 중 · ", 1)
                .replace(" complete", " 완료")
                .replace(" total", " 전체")
            )
        parallel_audio = re.fullmatch(
            r"Normalizing (\d+) independent track\(s\) with "
            r"(\d+) parallel worker\(s\)",
            message,
        )
        if parallel_audio:
            return (
                f"독립 오디오 {parallel_audio.group(1)}곡 정규화 준비 · "
                f"병렬 작업 {parallel_audio.group(2)}개"
            )
        normalized_audio = re.fullmatch(
            r"Normalized (\d+)/(\d+) tracks", message,
        )
        if normalized_audio:
            return (
                f"오디오 정규화 완료 "
                f"{normalized_audio.group(1)}/{normalized_audio.group(2)}곡"
            )
        if message.startswith("Normalizing "):
            return "오디오 정규화 중 · " + message.removeprefix("Normalizing ")
        if message.startswith("Combining audio "):
            return message.replace("Combining audio ", "오디오 결합 중 · ", 1)
        if message.startswith("Creating silence "):
            return (
                message.replace("Creating silence ", "무음 구간 생성 중 · ", 1)
                .replace(" total", " 전체")
            )
        if message.startswith("Validating visual frames "):
            return message.replace("Validating visual frames ", "화면 프레임 검사 중 · ", 1)
        if message.startswith("Preparing visual layers "):
            return message.replace("Preparing visual layers ", "시각 레이어 준비 중 · ", 1)
        if message.startswith("Preparing visual layer "):
            return (
                message.replace("Preparing visual layer ", "시각 레이어 준비 중 · ", 1)
                .replace(" · frame ", " · 프레임 ")
            )
        if message.startswith("Checking visual layer "):
            return (
                message.replace("Checking visual layer ", "시각 레이어 검사 중 · ", 1)
                .replace(" · frame ", " · 프레임 ")
            )
        if message.startswith("Inserted ") and message.endswith(" of silence"):
            duration = message.removeprefix("Inserted ").removesuffix(" of silence")
            return f"무음 구간 {duration} 추가"
        if message.startswith("Capturing and encoding ") and " / " in message:
            return message.replace(
                "Capturing and encoding ", "화면 캡처·영상 인코딩 중 · ", 1,
            )
        if message.startswith("Encoding ") and " / " in message:
            return message.replace("Encoding ", "영상 인코딩 중 · ", 1)
        if message.startswith("Muxing ") and " / " in message:
            return message.replace("Muxing ", "영상·오디오 결합 중 · ", 1)
        if message.startswith("Downloaded "):
            return message.replace("Downloaded ", "다운로드됨 · ", 1)
        return message
