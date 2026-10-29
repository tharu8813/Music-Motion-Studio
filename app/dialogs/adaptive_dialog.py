"""Scrollable dialog bodies with persistent actions on small desktop screens."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QScrollArea, QVBoxLayout, QWidget


class AdaptiveDialog(QDialog):
    """Keep actions reachable even when the form is taller than the screen."""

    def build_scrollable_body(self) -> tuple[QVBoxLayout, QVBoxLayout]:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        body = QWidget()
        body.setObjectName("dialogBody")
        self.body_scroll.setWidget(body)
        content = QVBoxLayout(body)
        content.setContentsMargins(0, 0, 8, 0)
        outer.addWidget(self.body_scroll, 1)
        return content, outer

    def fit_to_screen(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        frame_width = max(0, self.frameGeometry().width() - self.width())
        frame_height = max(0, self.frameGeometry().height() - self.height())
        width = max(1, available.width() - frame_width - 24)
        height = max(1, available.height() - frame_height - 24)
        self.setMinimumSize(min(self.minimumWidth(), width), min(self.minimumHeight(), height))
        self.resize(min(self.width(), width), min(self.height(), height))
        frame = self.frameGeometry()
        self.move(
            max(available.left(), min(frame.left(), available.right() - frame.width() + 1)),
            max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1)),
        )

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.fit_to_screen()
