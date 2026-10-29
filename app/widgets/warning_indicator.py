"""Status warnings sharing the activity popup's hover and pin lifecycle."""
from PySide6.QtWidgets import QLabel
from app.widgets.activity_progress import ActivityProgressWidget, ActivityDetailsPopup, ActivityState


class WarningDetailsPopup(ActivityDetailsPopup):
    def sync(self, states, korean):
        super().sync(states, korean)
        self.heading.setText(f"확인이 필요한 항목 {len(states)}개" if korean else f"{len(states)} warnings")
        for row in self.rows.values():
            row.bar.hide()
            row.percent.hide()
            if not row.property("warningDetails"):
                detail = QLabel(row.detail.text())
                detail.setWordWrap(True)
                detail.setObjectName("activityPopupDetail")
                row.layout().replaceWidget(row.detail, detail)
                row.detail.deleteLater()
                row.detail = detail
                row.setProperty("warningDetails", True)


class WarningIndicator(ActivityProgressWidget):
    def __init__(self, korean=True, parent=None):
        super().__init__(korean, parent)
        self.popup.deleteLater()
        self.popup = WarningDetailsPopup(self)
        self.popup.pin_button.toggled.connect(self.set_pinned)
        self.progress_bar.hide()
        self.setMinimumWidth(0)
        self.setFixedWidth(42)
        self.label.setStyleSheet("color: #E6B86A; font-size: 18px;")

    def set_warnings(self, entries):
        self._activities = {key: ActivityState(key, title, detail=detail) for key, title, detail in entries}
        self._refresh()

    def title(self):
        return "확인 필요" if self._korean else "Warnings"

    def _refresh(self):
        super()._refresh()
        if self._activities:
            self.label.setText("⚠")
            self.setAccessibleName(f"{self.title()} {len(self._activities)}")
            self.setAccessibleDescription("\n".join(f"{s.label}: {s.detail}" for s in self._activities.values()))
