"""Loading window shown the moment the app starts, before the heavy UI imports.

Deliberately Qt-only (no app.ui / app.services imports): it must be cheap
enough to paint before MainWindow and everything it pulls in are imported.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSettings, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QProgressBar, QSplashScreen

_WIDTH, _HEIGHT = 560, 310


def launch_is_korean() -> bool:
    """Same saved language the Translator reads later (Korean by default)."""
    return str(QSettings().value("language", "ko") or "ko").lower().startswith("ko")


class LaunchSplash(QSplashScreen):
    """Lightweight startup card; progress advances only at real startup milestones."""

    def __init__(self, product_name: str, version: str, icon: QIcon | None = None) -> None:
        self.korean = launch_is_korean()
        super().__init__(self._card(product_name, version, icon, self.korean))
        self.setWindowTitle(product_name)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setGeometry(36, 230, _WIDTH - 72, 8)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setAccessibleName("시작 준비 진행률" if self.korean else "Startup progress")
        self.progress_bar.setStyleSheet(
            "QProgressBar { background: #25282A; border: 0; border-radius: 4px; }"
            "QProgressBar::chunk { background: #B4CDC3; border-radius: 4px; }"
        )

    @staticmethod
    def _card(product_name: str, version: str, icon: QIcon | None, korean: bool) -> QPixmap:
        ratio = QApplication.primaryScreen().devicePixelRatio() if QApplication.primaryScreen() else 1.0
        pixmap = QPixmap(round(_WIDTH * ratio), round(_HEIGHT * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QColor("#161819"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#3C4246"))
        painter.drawRect(QRectF(0.5, 0.5, _WIDTH - 1, _HEIGHT - 1))
        painter.fillRect(QRectF(1, 1, _WIDTH - 2, 3), QColor("#B4CDC3"))
        if icon is not None and not icon.isNull():
            painter.drawPixmap(36, 48, icon.pixmap(68, 68))
        title = QFont("Segoe UI", 23, QFont.Weight.DemiBold)
        painter.setFont(title)
        painter.setPen(QColor("#ECEEED"))
        painter.drawText(QRectF(124, 48, _WIDTH - 160, 42), Qt.AlignmentFlag.AlignVCenter, product_name)
        caption = QFont("Segoe UI", 10)
        painter.setFont(caption)
        painter.setPen(QColor("#ADB3B5"))
        painter.drawText(QRectF(124, 92, _WIDTH - 160, 24), Qt.AlignmentFlag.AlignVCenter,
                         "음악과 화면을 하나의 흐름으로" if korean else "Music and visuals, in one flow")
        painter.setPen(QColor("#3C4246"))
        painter.drawLine(36, 152, _WIDTH - 36, 152)
        painter.setPen(QColor("#ADB3B5"))
        painter.drawText(QRectF(36, 254, _WIDTH - 72, 24), Qt.AlignmentFlag.AlignVCenter,
                         "편집 환경을 준비하고 있습니다" if korean else "Getting your editing tools ready")
        painter.drawText(QRectF(36, 254, _WIDTH - 72, 24),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"v{version}")
        painter.end()
        return pixmap

    def set_status(self, korean: str, english: str, progress: int) -> None:
        """Show the current real startup step (repaints immediately)."""
        self.progress_bar.setValue(max(0, min(100, progress)))
        self.showMessage(korean if self.korean else english, Qt.AlignmentFlag.AlignLeft, QColor("#ECEEED"))
        QApplication.processEvents()

    def drawContents(self, painter: QPainter) -> None:  # noqa: N802 - Qt override
        """Readable status and percentage above the native progress bar."""
        painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        painter.setPen(QColor("#ECEEED"))
        painter.drawText(QRectF(36, 186, _WIDTH - 132, 28), Qt.AlignmentFlag.AlignVCenter, self.message())
        painter.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
        painter.setPen(QColor("#B4CDC3"))
        painter.drawText(QRectF(_WIDTH - 96, 186, 60, 28),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                         f"{self.progress_bar.value()}%")
