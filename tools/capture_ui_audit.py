"""Render every application dialog and editor surface without touching user data.

Run ``python tools/capture_ui_audit.py``. PNGs and the coverage manifest go to
build/ui-refresh/audit/. No downloads, audio analysis or export jobs are started.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QTabWidget


def main() -> int:
    output = ROOT / "build/ui-refresh/audit"
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="ui-audit-", dir=output) as raw:
        data = Path(raw)
        os.environ["LOCALAPPDATA"] = str(data)
        os.environ["PLAYLIST_CANVAS_PRESET_DIR"] = str(data / "presets")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(data))
        app = QApplication.instance() or QApplication([])
        app.setOrganizationName("MusicMotionAudit")
        app.setApplicationName("UIAudit")
        app.setWindowIcon(QIcon(str(ROOT / "app/resources/app_icon.svg")))
        QSettings().setValue("language", "ko")
        from app.ui.main_window import MainWindow
        from app.models.playlist import PlaylistTrack
        from app.models.source import Source, SourceType
        from app.services.playlist_service import AudioImportCandidate
        from app.services.theme_service import Theme
        from app.services.update_service import ReleaseInfo
        from app.dialogs.welcome_dialog import MAIN_GUIDE, MAIN_GUIDE_TITLE

        window = MainWindow()
        window.resize(1440, 900)
        window.show()
        QTest.qWait(350)
        translator = window.translator
        audio = data / "Evening.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\0\0" * 16000)
        tracks = [PlaylistTrack(str(audio), title, artist="Music Motion", duration_seconds=2)
                  for title in ("Evening Walk", "Soft Focus", "After the Rain")]
        cues = [{"start": 0.0, "end": 1.0, "text": "오늘의 장면을 음악으로 남겨요"},
                {"start": 1.0, "end": 2.0, "text": "A quiet moment, a moving canvas"}]
        source = Source(name="가사", source_type=SourceType.LYRICS)
        release = ReleaseInfo("0.0.0", "v0.0.0", "UI preview", "", "", "https://example.com")
        candidate = AudioImportCandidate(tracks[0])
        image_path = data / "canvas.png"
        window.canvas.grab().save(str(image_path))
        settings = window.settings_service.current
        factories = {
            "AboutDialog": (translator, None, data),
            "AudioMetadataDialog": ([candidate], translator),
            "AutoMixEditorDialog": (window,),
            "ColorEditorDialog": (QColor("#B4CDC3"), source, translator, "색상"),
            "ContentPreviewDialog": (str(image_path), "image", True),
            "CrashReportDialog": ("OSError", "파일을 열 수 없습니다.", "Example details", None, True),
            "ExportCompleteDialog": (data / "video.mp4", translator),
            "ExportPreviewDialog": (window.canvas.scene_model, tracks, translator),
            "ExportProgressDialog": (),
            "ExportSettingsDialog": (settings, 3, 360.0, translator, data / "video.mp4"),
            "FFmpegInstallProgressDialog": (True,),
            "HelpDialog": (translator,),
            "LrcGeneratorDialog": ([], translator),
            "LrcLivePreviewDialog": None,
            "LrcShortcutsDialog": (translator,),
            "LyricsAnimationDialog": (source, translator),
            "LyricsCompareDialog": (cues, cues, "current.lrc", "incoming.lrc", translator),
            "LyricsLineStyleDialog": (source, translator),
            "LyricsSearchDialog": (tracks, translator),
            "M3uImportDialog": ([candidate], data / "playlist.m3u8", [], translator),
            "MissingMediaDialog": ([], translator),
            "Mp3MetadataEditorDialog": (translator,),
            "NewProjectDialog": (translator,),
            "PlaylistExportDialog": (data, translator),
            "DesignPresetDialog": (translator,),
            "PreviewLoadingDialog": (["캔버스 준비", "오디오 준비", "미리보기 열기"], True),
            "ProjectCrashReportDialog": dict(project_path="Example.pvsproj", stage="open", exception_type="OSError",
                exception_message="파일을 열 수 없습니다.", cause_type="OSError", cause_message="Example",
                guidance="파일 경로를 확인하세요.", report_text="Example details", log_path=str(data), korean=True),
            "ProjectSettingsDialog": (window.project_settings, translator, window.canvas.grab()),
            "SettingsDialog": (settings, translator.language, Theme.DARK, translator),
            "ShortcutsDialog": (translator,),
            "StartupDialog": (translator, window.recent_projects),
            "TextEditorDialog": ("제목\n{track_title}", translator),
            "TrackDetailsDialog": (tracks[0], translator),
            "TrackOrderDialog": (tracks, translator),
            "TtmlRowsDialog": (cues, translator),
            "UpdateAvailableDialog": (release, "0.0.0", True, False),
            "UpdateDownloadDialog": (release, True),
            "VideoSourceDialog": (Source(name="영상", source_type=SourceType.VIDEO), True),
            "GuideDialog": (MAIN_GUIDE, MAIN_GUIDE_TITLE, True),
        }

        def save(widget, name):
            widget.show()
            QTest.qWait(70)
            if not widget.grab().save(str(output / f"{name}.png")):
                raise RuntimeError(f"Could not save {name}")
            results.append({"screen": name, "size": [widget.width(), widget.height()]})

        save(window, "editor-empty")
        from app.ui.launch_splash import LaunchSplash
        splash = LaunchSplash("Music Motion Studio", "UI audit", app.windowIcon())
        splash.set_status("작업 공간을 준비하는 중…", "Preparing the workspace…", 75)
        save(splash, "launch-splash")
        splash.close()
        window.playlist_service.add_tracks(tracks)
        for index in range(window.left_tabs.count()):
            window.left_tabs.setCurrentIndex(index)
            save(window, f"editor-left-{index}")
        window.left_tabs.setCurrentIndex(0)
        window.bottom_tabs.setCurrentIndex(1)
        save(window, "editor-timeline")
        window.bottom_tabs.setCurrentIndex(0)
        window.resize(1100, 720)
        save(window, "editor-small")
        window.resize(1440, 900)
        for source_type in SourceType:
            sample = Source(name=source_type.value, source_type=source_type)
            window.store.add(sample)
            window.store.select(sample.id)
            for index in range(window.inspector.property_tabs.count()):
                window.inspector.property_tabs.setCurrentIndex(index)
                save(window.inspector, f"inspector-{source_type.value}-{index}")
            window.store.remove(sample.id)

        from PySide6.QtMultimedia import QMediaPlayer
        player = QMediaPlayer()
        factories["LrcLivePreviewDialog"] = (player, lambda: cues, translator)
        captured = set()
        for path in sorted((ROOT / "app/dialogs").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = [node.name for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name in factories]
            if not names:
                continue
            module = importlib.import_module(f"app.dialogs.{path.stem}")
            for name in names:
                dialog = None
                try:
                    cls = getattr(module, name)
                    args = factories[name]
                    kwargs = args if isinstance(args, dict) else {}
                    if name == "ExportPreviewDialog":
                        kwargs = {"preferred_backend": "cpu"}
                    dialog = cls(**kwargs) if isinstance(args, dict) else cls(*args, **kwargs)
                    save(dialog, name)
                    if name == "StartupDialog":
                        from app.services.project_service import ProjectService
                        project_path = data / "Evening.project.json"
                        ProjectService.save(project_path, window._project_document())
                        window.recent_projects.add(project_path)
                        save(dialog, "StartupDialog-recent")
                    for tabs in dialog.findChildren(QTabWidget):
                        if tabs.isVisible() and tabs.count() > 1:
                            for index in range(tabs.count()):
                                tabs.setCurrentIndex(index)
                                save(dialog, f"{name}-{tabs.objectName() or 'tabs'}-{index}")
                    captured.add(name)
                except Exception as error:
                    results.append({"screen": name, "error": f"{type(error).__name__}: {error}"})
                finally:
                    if dialog is not None:
                        if name == "ExportPreviewDialog":
                            dialog.close()  # releases preview workers in closeEvent
                        else:
                            dialog.reject()
                        dialog.deleteLater()
                    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    app.processEvents()
        window.project_session.dirty = False
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        expected = set()
        for path in (ROOT / "app/dialogs").glob("*.py"):
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.ClassDef) and node.name != "AdaptiveDialog" and any(
                    ast.unparse(base) in {"QDialog", "AdaptiveDialog"} for base in node.bases
                ):
                    expected.add(node.name)
        report = {"captures": results, "dialogs": len(captured),
                  "missing": sorted(expected - captured)}
        (output / "manifest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"images": len(results), "dialogs": len(captured), "missing": report["missing"]}))
    return 1 if report["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
