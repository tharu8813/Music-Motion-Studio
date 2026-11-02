"""Refresh motion-editor and inspector guide images without touching user settings."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtWidgets import QApplication
from app.ui.design_system import apply_studio_style
from app.dialogs.custom_motion_dialog import CustomMotionDialog
from app.dialogs.motion_editor_guide import MOTION_GUIDE, MOTION_GUIDE_KEY
from app.dialogs.welcome_dialog import GuideDialog
from app.models.source import SourceType
from tools.capture_help_images import Session, build_sample_media, grab, save, settle


def main():
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for the sample project")
    with TemporaryDirectory(prefix="motion-help-", dir=ROOT / "work/audit-temp") as raw:
        folder = Path(raw)
        os.environ["LOCALAPPDATA"] = str(folder / "local")
        QStandardPaths.setTestModeEnabled(True)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(folder / "settings"))
        app = QApplication([])
        app.setOrganizationName("MusicMotionHelpCapture")
        app.setApplicationName("MotionHelpCapture")
        apply_studio_style(app)
        for key in ("guides/welcome_seen", "guides/automix_editor_seen", MOTION_GUIDE_KEY):
            QSettings().setValue(key, True)
        media = folder / "media"
        media.mkdir()
        songs = build_sample_media(media)
        for language in ("ko", "en"):
            session = Session(app, language, songs, Path(ffmpeg), analyze=False)
            window = session.window
            source = next(s for s in window.store.sources() if s.source_type == SourceType.ALBUM_COVER)
            window.store.select(source.id)
            session.refresh()
            save(grab(window.inspector_stack), language, "canvas_inspector")
            dialog = CustomMotionDialog(source, window.canvas.scene_model, session.tracks, window.translator, parent=window)
            dialog.show()
            dialog.seek(.7)
            dialog.edit_pose(dx=-120, scale=.9, opacity=1)
            dialog.select_time("dx", .7)
            dialog.render_now()
            settle(app)
            save(grab(dialog), language, "motion_editor")
            guide = GuideDialog(MOTION_GUIDE, ("키프레임 편집 시작하기", "Getting started with keyframes"), language == "ko", dialog)
            guide.show()
            settle(app)
            guide.grab().save(str(ROOT / f"work/motion-start-guide-{language}.png"))
            guide.reject()
            guide.deleteLater()
            dialog.reject()
            dialog.deleteLater()
            session.close()
        QSettings().clear()


if __name__ == "__main__":
    main()
