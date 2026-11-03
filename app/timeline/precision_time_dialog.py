"""A local timing draft; no project signals are emitted until Apply."""
from decimal import Decimal
import math
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QLineEdit, QVBoxLayout)


def parse_time(text: str) -> float:
    """Seconds, MM:SS or HH:MM:SS, without quantizing the project float."""
    text = text.strip()
    if not re.fullmatch(r"[0-9]+(?::[0-9]{2}){0,2}(?:\.[0-9]+)?", text):
        raise ValueError("format")
    parts = [Decimal(p) for p in text.split(":")]
    if len(parts) > 1 and any(p >= 60 for p in parts[1:]):
        raise ValueError("format")
    total = Decimal(0)
    for part in parts:
        total = total * 60 + part
    value = float(total)
    if not math.isfinite(value):
        raise ValueError("range")
    return value


def time_text(value: float) -> str:
    # repr round-trips floats; fixed-point avoids scientific notation in inputs.
    return format(Decimal(str(value)), "f")


class PrecisionTimeDialog(QDialog):
    def __init__(self, name, start, duration, *, track=False, full=False,
                 minimum=0, playlist_end=0, korean=True, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.korean, self.track = korean, track
        self.start, self.duration = start, duration
        self.minimum, self.playlist_end = minimum, playlist_end
        self._dirty = set()
        self.setWindowTitle("시간 정밀 편집" if korean else "Edit precise timing")
        self.setMinimumWidth(350)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        layout = QVBoxLayout(self)
        title = QLabel(name)
        title.setWordWrap(True)
        layout.addWidget(title)
        form = QFormLayout()
        self.fields = {}
        for key, label in (("start", "시작 시간" if korean else "Start"),
                           ("end", "종료 시간" if korean else "End"),
                           ("duration", ("재생 길이" if track else "지속 시간") if korean else "Duration")):
            field = QLineEdit()
            field.setAccessibleName(label)
            field.setReadOnly(track and key != "start")
            field.textEdited.connect(lambda _text, k=key: self._dirty.add(k))
            field.editingFinished.connect(lambda k=key: self.update_draft(k))
            self.fields[key] = field
            form.addRow(label, field)
        layout.addLayout(form)
        self.full = QCheckBox("곡 끝까지 표시" if korean else "Show to playlist end")
        self.full.setVisible(not track)
        self.full.setChecked(full)
        self.full.toggled.connect(self.toggle_full)
        layout.addWidget(self.full)
        hint = QLabel(("시작 변경: 길이 유지 · 종료/길이 변경: 시작 유지\n입력: 12.50 / 01:23.450 / 01:02:03.500" if korean else
                       "Start keeps duration; end/duration keeps start.\nInput: 12.50 / 01:23.450 / 01:02:03.500"))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setText("적용" if korean else "Apply")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소" if korean else "Cancel")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setAutoDefault(False)
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.fill()
        self.fields["start"].setFocus()
        self.fields["start"].selectAll()

    def fill(self):
        end = max(self.start, self.playlist_end) if self.full.isChecked() else float(Decimal(str(self.start)) + Decimal(str(self.duration)))
        values = dict(start=self.start, end=end, duration=max(0, end-self.start) if self.full.isChecked() else self.duration)
        for key, field in self.fields.items():
            if key not in self._dirty:
                field.setText(time_text(values[key]))
            field.setEnabled(key == "start" or not self.full.isChecked())

    def toggle_full(self, checked):
        if not checked and self.duration == 0:
            self.duration = max(.1, self.playlist_end-self.start)
        self._dirty.difference_update({"end", "duration"})
        self.fill()

    def update_draft(self, key):
        if key not in self._dirty:
            return True
        try:
            value = parse_time(self.fields[key].text())
            start, duration = self.start, self.duration
            if key == "start":
                if value < self.minimum:
                    raise ValueError("minimum")
                start = value
            else:
                duration = float(Decimal(str(value))-Decimal(str(self.start))) if key == "end" else value
                if duration <= 0:
                    raise ValueError("duration")
            if not math.isfinite(start + duration):
                raise ValueError("range")
            if not self.track and not self.full.isChecked() and start + duration <= start:
                raise ValueError("duration")
        except (ValueError, OverflowError):
            self.error.setText((f"0 이상의 시간을 입력하세요. 시작은 {time_text(self.minimum)}초 이상, 종료는 시작보다 커야 합니다.\n형식: 12.50 / 01:23.450 / 01:02:03.500" if self.korean else
                f"Enter a nonnegative time. Start must be at least {time_text(self.minimum)} s; end must follow start.\nFormat: 12.50 / 01:23.450 / 01:02:03.500"))
            return False
        self.start, self.duration = start, duration
        self._dirty.discard(key)
        self.error.clear()
        self.fill()
        return True

    @property
    def timing(self):
        return self.start, 0.0 if self.full.isChecked() else self.duration

    def accept(self):
        for key in ("start", "end", "duration"):
            if not self.update_draft(key):
                self.fields[key].setFocus()
                return
        super().accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.accept()
            event.accept()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Validation text can grow a popup opened near the bottom of a screen.
        screen = self.screen().availableGeometry()
        point = self.pos()
        point.setX(max(screen.left(), min(point.x(), screen.right()-self.width()+1)))
        point.setY(max(screen.top(), min(point.y(), screen.bottom()-self.height()+1)))
        if point != self.pos():
            self.move(point)
