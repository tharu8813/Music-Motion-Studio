"""Shared test-process isolation.

Qt's test mode redirects ``QStandardPaths`` data locations to a disposable
temporary root. Keep this at package import time so it runs before any test
class creates ``QSettings`` or an application service.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile
from tempfile import mkdtemp

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QStandardPaths
from PySide6.QtWidgets import QApplication


# Windows runners can supply 8.3 temp paths; keep every fixture on the canonical path.
_TEST_LOCAL_APP_DATA = str(Path(mkdtemp(prefix="playlist-canvas-tests-")).resolve())
atexit.register(shutil.rmtree, _TEST_LOCAL_APP_DATA, ignore_errors=True)
# Assigned, not setdefault: Windows always defines LOCALAPPDATA, so setdefault
# left the AutoMix cache, presets, language packs and logs writing into the
# developer's real %LOCALAPPDATA%\PlaylistCanvas during test runs.
os.environ["LOCALAPPDATA"] = _TEST_LOCAL_APP_DATA
# Native Windows QSettings uses the registry, outside this disposable data root.
# Give tests a writable, isolated store without requiring registry permissions.
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(
    QSettings.Format.IniFormat, QSettings.Scope.UserScope,
    os.path.join(_TEST_LOCAL_APP_DATA, "settings"),
)
# Same for the temp folder: extracted .pvsproj caches and other app temp data
# landed in the real %TEMP% (2.4 GB of project-cache in two weeks) and every
# package load then rescanned them all. One folder per test process, removed
# at exit; child processes (FFmpeg) inherit it through TEMP/TMP.
_TEST_TEMP = str(Path(mkdtemp(prefix="playlist-canvas-tests-tmp-")).resolve())
atexit.register(shutil.rmtree, _TEST_TEMP, ignore_errors=True)
os.environ["TEMP"] = os.environ["TMP"] = os.environ["TMPDIR"] = _TEST_TEMP
tempfile.tempdir = _TEST_TEMP
QStandardPaths.setTestModeEnabled(True)


@atexit.register  # registered after PySide's own exit hook, so it runs first
def _delete_leftover_windows() -> None:
    """Delete the dialogs/widgets tests left open while Qt and Python are still whole.

    Left alone, PySide destroys them during interpreter shutdown, which could
    end the run with a fail-fast (0xC0000409) after every test passed.
    """
    if QCoreApplication.instance() is None:
        return
    for widget in QApplication.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
