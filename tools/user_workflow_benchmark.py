"""Repeatable Qt user-flow simulation, with disposable settings and muted audio.

python tools/user_workflow_benchmark.py --output work/workflow.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tests  # isolate settings, caches and recovery files from the user's data
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.dialogs.audio_metadata_dialog import AudioMetadataDialog
from app.models.playlist import PlaylistTrack
from app.services.playlist_service import AudioImportCandidate
from app.ui.main_window import MainWindow
from app.widgets.playlist_editor import TrackRow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    application = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1280, 800)
    window.show()
    application.processEvents()
    editor = window.playlist_editor
    tracks = [PlaylistTrack(f"missing-{i}.wav", f"Song {i:04d}", duration_seconds=60)
              for i in range(300)]
    window.playlist_service.replace(tracks)
    application.processEvents()
    timings = []
    with patch("app.widgets.playlist_editor.TrackRow", wraps=TrackRow) as rows:
        for query in ("Song 01", "Song 02", "missing", "zzz", "") * 3:
            editor.search_edit.setText(query)
            editor._search_timer.stop()
            start = time.perf_counter()
            editor.refresh()
            timings.append((time.perf_counter() - start) * 1000)
        rebuilt_rows = rows.call_count

    heartbeats = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: heartbeats.append(time.perf_counter()))
    timer.start()
    caller_thread = threading.get_ident()
    inspection_threads = []

    def slow_inspect(paths, **_options):
        inspection_threads.append(threading.get_ident())
        time.sleep(0.8)  # reproducible slow filesystem/metadata backend
        return [AudioImportCandidate(PlaylistTrack(str(paths[0]), "Imported", duration_seconds=3))]

    start = time.perf_counter()
    with patch.object(window.playlist_service, "inspect_files", side_effect=slow_inspect):
        added, _, _ = window._import_audio_files(["missing-import.wav"])
    end = time.perf_counter()
    timer.stop()
    gaps = [b - a for a, b in zip([start, *heartbeats], [*heartbeats, end])]
    assert added == 1
    metadata = AudioMetadataDialog([AudioImportCandidate(tracks[0], ("artist",))], window.translator)
    metadata.resize(900, 480)
    result = {
        "track_count": len(tracks), "search_refreshes": len(timings),
        "search_median_ms": round(statistics.median(timings), 2),
        "search_max_ms": round(max(timings), 2), "rebuilt_search_rows": rebuilt_rows,
        "simulated_metadata_delay_ms": 800, "import_heartbeat_count": len(heartbeats),
        "import_max_heartbeat_gap_ms": round(max(gaps) * 1000, 2),
        "inspection_off_gui_thread": all(t != caller_thread for t in inspection_threads),
        "import_added": added,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    window.grab().save(str(args.output.with_suffix(".png")))
    metadata.grab().save(str(args.output.with_name(args.output.stem + "-metadata.png")))
    metadata.close()
    window._project_dirty = False
    window.close()
    application.processEvents()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
