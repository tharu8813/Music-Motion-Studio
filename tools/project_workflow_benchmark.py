"""Cold Qt editor startup and repeated portable-project I/O, with isolated data.

The project contains 100 generated PNGs and 100 track records referencing one
silent WAV. This measures persistence, not media decode or UI project restoration.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter
import wave
from unittest.mock import patch

STARTED = perf_counter()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from app.utils.performance import resident_set_bytes


def workspace_runs(app, root, images, audio):
    """Time the normal open command and its GUI restoration separately."""
    from dataclasses import replace
    from PySide6.QtCore import QEvent
    from app.animation.custom_motion import CustomMotion
    from app.models.project import ProjectDocument, ProjectSettings
    from app.models.playlist import PlaylistTrack
    from app.models.source import Source, SourceType
    from app.services.project_service import ProjectService
    from app.ui.main_window import MainWindow
    from app.utils.subprocess_utils import hidden_process_kwargs

    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("FFmpeg is required to build the video fixture")
    video = root / "fixture.mp4"
    subprocess.run([executable, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24",
                    "-t", "1", "-c:v", "mpeg4", "-y", str(video)], check=True,
                   **hidden_process_kwargs())
    settings = ProjectSettings(content_mode="reference")
    tracks = [PlaylistTrack(str(audio), f"Track {index}", duration_seconds=1) for index in range(100)]
    video_sources = [Source(SourceType.VIDEO, f"Video {index}", video_paths=[str(video)], z_index=100 + index)
                     for index in range(3)]
    animated = [replace(source, animation_in="custom", custom_intro_keyframes=CustomMotion.default())
                for source in images]
    for track in tracks:
        track.lyrics = [{"start": index * .1, "end": (index + 1) * .1,
                         "text": f"{index} 한영 가사\nBilingual lyric"} for index in range(10)]
    fixtures = {
        "empty": ProjectDocument(settings=settings),
        "typical": ProjectDocument(sources=images[:5] + video_sources[:1], playlist=tracks[:5], settings=settings),
        "heavy": ProjectDocument(sources=animated + video_sources + [Source(SourceType.LYRICS, "Lyrics", z_index=110)],
                                 playlist=tracks, settings=settings),
    }
    window = MainWindow()
    window.show()
    app.processEvents()
    result = {}
    try:
        for name, fixture in fixtures.items():
            path = root / f"{name}.pvsproj"
            ProjectService.save(path, fixture)
            runs = []
            for _ in range(3):
                stages = {}

                def measure(label, function):
                    def recorded(*args, **kwargs):
                        start = perf_counter()
                        try:
                            return function(*args, **kwargs)
                        finally:
                            stages[label] = perf_counter() - start
                    return recorded

                with (patch.object(window, "_resolve_project_media", measure("validation", window._resolve_project_media)),
                      patch.object(window, "_apply_project", measure("apply_workspace", window._apply_project)),
                      patch.object(window.project_controller, "_load_document_off_gui_thread",
                                   measure("read_package", window.project_controller._load_document_off_gui_thread))):
                    start = perf_counter()
                    if not window.project_controller.load_path(path):
                        raise RuntimeError(f"Unable to open the {name} fixture")
                    app.processEvents()
                    stages["total"] = perf_counter() - start
                assert len(window.store.sources()) == len(fixture.sources)
                assert len(window.playlist_service.tracks) == len(fixture.playlist)
                runs.append(dict(seconds=stages, rss=resident_set_bytes()))
            result[name] = dict(sources=len(fixture.sources), tracks=len(fixture.playlist), runs=runs,
                                medians={key: statistics.median(run["seconds"][key] for run in runs)
                                         for key in runs[0]["seconds"]})
    finally:
        window.project_session.dirty = False
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup-child", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workspace", action="store_true", help="Also measure normal GUI restoration")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    if args.startup_child:
        from app.ui.main_window import MainWindow
        window = MainWindow()
        window.show()
        app.processEvents()
        result = dict(seconds=perf_counter() - STARTED, ready=window.project_session.ready,
                      rss=resident_set_bytes())
        window.project_session.dirty = False
        window.close()
        app.processEvents()
        print(json.dumps(result))
        return
    if not args.output:
        parser.error("--output is required")
    startup = []
    for _ in range(3):
        started = perf_counter()
        command = [sys.executable, str(Path(__file__).resolve()), "--startup-child"]
        child = subprocess.run(command,
                               capture_output=True, text=True, check=True, timeout=60)
        result = json.loads(child.stdout.strip().splitlines()[-1])
        result["launch_through_exit_seconds"] = perf_counter() - started
        startup.append(result)
    from app.models.project import ProjectDocument
    from app.models.playlist import PlaylistTrack
    from app.models.source import Source, SourceType
    from app.services.project_service import ProjectService
    result = dict(startup_runs=startup,
                  startup_median_seconds=statistics.median(run["seconds"] for run in startup))
    with tempfile.TemporaryDirectory(prefix="project-perf-") as directory:
        root = Path(directory)
        image = QImage(256, 256, QImage.Format.Format_RGB32)
        sources = []
        for index in range(100):
            path = root / f"image-{index}.png"
            image.fill(0xff000000 | index * 123456)
            assert image.save(str(path))
            sources.append(Source(SourceType.IMAGE, f"Image {index}", content_path=str(path)))
        audio = root / "silence.wav"
        with wave.open(str(audio), "wb") as stream:
            stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 8000)
        document = ProjectDocument(sources=sources, playlist=[
            PlaylistTrack(str(audio), f"Track {index}", duration_seconds=1) for index in range(100)])
        path = root / "fixture.pvsproj"
        rss = [resident_set_bytes()]
        runs = []
        for _ in range(10):
            started = perf_counter()
            ProjectService.save(path, document)
            saved = perf_counter() - started
            started = perf_counter()
            loaded = ProjectService.load(path)
            opened = perf_counter() - started
            assert len(loaded.sources) == 100 and len(loaded.playlist) == 100
            runs.append(dict(save_seconds=saved, open_seconds=opened))
            del loaded
            app.processEvents()
            rss.append(resident_set_bytes())
        result.update(project_runs=runs, project_bytes=path.stat().st_size,
                      open_median_seconds=statistics.median(run["open_seconds"] for run in runs),
                      save_median_seconds=statistics.median(run["save_seconds"] for run in runs),
                      rss_after_each_iteration=rss,
                      limitation="Synthetic small assets; persistence only. Startup excludes Python process launch; launch-through-exit recorded separately.")
        if args.workspace:
            result["workspace"] = workspace_runs(app, root, sources, audio)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
