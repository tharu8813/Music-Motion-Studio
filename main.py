"""Application entry point for Playlist Canvas."""

from __future__ import annotations

import sys
from pathlib import Path
import shutil
import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSettings, QStandardPaths, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app import LEGACY_PRODUCT_NAME, PRODUCT_NAME, __version__
from app.ui.launch_splash import LaunchSplash
from app.utils.logging_setup import configure_logging, install_exception_hook

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow


def project_path_from_arguments(arguments: list[str]) -> Path | None:
    """Return the first existing project passed by Explorer or the command line."""
    for argument in arguments:
        if not argument or argument.startswith("-"):
            continue
        candidate = Path(argument).expanduser()
        name = candidate.name.lower()
        if not (
            candidate.suffix.lower() == ".pvsproj"
            or name.endswith(".project.json")
            or candidate.suffix.lower() == ".json"
        ):
            continue
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def open_initial_workspace(
    window: "MainWindow", launch_project: Path | None,
    *, update_recovery: Path | None = None,
) -> bool:
    """Open an Explorer target directly or fall back to the startup chooser."""
    if update_recovery is not None:
        window._offer_recovery(snapshot_path=update_recovery, confirm=False)
        return True
    if launch_project is not None:
        # Even a failed load keeps the initialized editor open; MainWindow shows
        # its recoverable project crash report and preserves the workspace.
        window.open_project_path(launch_project)
        return True
    return window.show_startup_dialog()


def _configure_product_identity(application: QApplication) -> None:
    """Apply the new brand and migrate settings/recoveries from the old name."""
    application.setApplicationName(LEGACY_PRODUCT_NAME)
    application.setOrganizationName(LEGACY_PRODUCT_NAME)
    legacy_data = Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    ))

    application.setApplicationName(PRODUCT_NAME)
    application.setApplicationDisplayName(PRODUCT_NAME)
    application.setOrganizationName(PRODUCT_NAME)
    current_data = Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    ))

    current = QSettings()
    migration_key = "migration/playlist_canvas_brand"
    if not current.value(migration_key, False, bool):
        legacy = QSettings(LEGACY_PRODUCT_NAME, LEGACY_PRODUCT_NAME)
        for key in legacy.allKeys():
            if not current.contains(key):
                current.setValue(key, legacy.value(key))
        current.setValue(migration_key, True)
        current.sync()

    legacy_recoveries = legacy_data / "recoveries"
    current_recoveries = current_data / "recoveries"
    if legacy_recoveries.is_dir() and legacy_recoveries != current_recoveries:
        try:
            current_recoveries.mkdir(parents=True, exist_ok=True)
            for snapshot in legacy_recoveries.glob("*.recovery.json"):
                target = current_recoveries / snapshot.name
                if not target.exists():
                    shutil.copy2(snapshot, target)
        except OSError:
            # The legacy recovery remains untouched and can be copied manually.
            pass


def main() -> int:
    """Create and run the Qt application."""
    configure_logging()
    launch_project = project_path_from_arguments(sys.argv[1:])
    update_recovery = None
    if "--resume-update" in sys.argv:
        index = sys.argv.index("--resume-update")
        if index + 1 < len(sys.argv):
            update_recovery = Path(sys.argv[index + 1])
    application = QApplication(sys.argv)
    _configure_product_identity(application)
    application.setApplicationVersion(__version__)
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    icon_path = resource_root / "app" / "resources" / "app_icon.ico"
    icon = QIcon(str(icon_path)) if icon_path.is_file() else None
    if icon is not None:
        application.setWindowIcon(icon)
    # Paint a loading window before the heavy UI modules are even imported,
    # so launching gives immediate feedback; it closes once the editor is up.
    splash = LaunchSplash(PRODUCT_NAME, __version__, icon)
    splash.show()
    splash.set_status("프로그램을 불러오는 중…", "Loading the application…", 10)
    install_exception_hook()
    from app.utils.temp_cleanup import sweep_stale_temp_dirs

    threading.Thread(target=sweep_stale_temp_dirs, daemon=True).start()
    splash.set_status("분석 환경을 준비하는 중…", "Preparing audio analysis…", 30)
    from app.automix.cache import cache_directories, prune_caches

    # Imports stay on this thread: a daemon thread still importing when the
    # app quits hung interpreter shutdown. The thread only touches files.
    threading.Thread(target=prune_caches, args=(cache_directories(),), daemon=True).start()
    splash.set_status("편집 도구를 불러오는 중…", "Loading editing tools…", 50)
    from app.ui.main_window import MainWindow

    splash.set_status("작업 공간을 준비하는 중…", "Preparing the workspace…", 75)
    window = MainWindow()
    splash.set_status("편집 화면을 여는 중…", "Opening the editor…", 90)
    window.show()
    splash.set_status("준비 완료", "Ready", 100)
    splash.finish(window)
    if "--smoke-test" in sys.argv:
        QTimer.singleShot(0, application.quit)
        return application.exec()
    from app.dialogs.welcome_dialog import MAIN_GUIDE, MAIN_GUIDE_TITLE, WELCOME_KEY, GuideDialog, first_time

    if update_recovery is None and first_time(WELCOME_KEY):
        GuideDialog(MAIN_GUIDE, MAIN_GUIDE_TITLE, window.translator.is_korean, window).exec()
    if not open_initial_workspace(window, launch_project, update_recovery=update_recovery):
        window.close()
        # No exec() will run its cleanup, so delete the closed window here, not at exit.
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return 0
    window.check_ffmpeg_availability()
    window.schedule_automatic_update_check()
    return application.exec()


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()
    raise SystemExit(main())
