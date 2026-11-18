"""Crash diagnostics and repeat detection, kept independent of the editor UI."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from hashlib import sha256
import os
import platform
from pathlib import Path
import sys
import time
from uuid import uuid4

from app import PRODUCT_NAME, __version__

_SESSION_STARTED = time.monotonic()
_MAX_TRACEBACK_CHARS = 120_000


class CrashTracker:
    """Track actual consecutive reports in a rolling time window.

    Counting happens before opening a modal dialog, so nested GUI messages
    cannot silently disappear from the repetition counter.
    """

    def __init__(self, window_seconds: float = 90.0, clock=None) -> None:
        self.window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._events: deque[float] = deque()

    def record(self) -> int:
        now = self._clock()
        while self._events and now - self._events[0] > self.window_seconds:
            self._events.popleft()
        self._events.append(now)
        return len(self._events)


def _memory_description() -> str:
    """Avoid system commands, heavy GPU probes or network calls in a crash path."""
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stats = MEMORYSTATUSEX()
            stats.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stats)):
                gib = 1024 ** 3
                return (f"{stats.ullTotalPhys / gib:.1f} GiB total, "
                        f"{stats.ullAvailPhys / gib:.1f} GiB available")
        else:
            # Linux and other developer environments: best-effort only.
            if sys.platform == "linux":
                data = Path("/proc/meminfo").read_text(encoding="utf-8")
                values = {}
                for line in data.splitlines():
                    key, _, value = line.partition(":")
                    if key in {"MemTotal", "MemAvailable"}:
                        values[key] = int(value.strip().split()[0]) / (1024 * 1024)
                if "MemTotal" in values:
                    return (f"{values['MemTotal']:.1f} GiB total, "
                            f"{values.get('MemAvailable', 0):.1f} GiB available")
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return "unavailable"


def _windows_hardware() -> list[str]:
    """Best-effort native registry queries; never launch a diagnostic subprocess."""
    if os.name != "nt":
        return []
    results: list[str] = []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            results.append(f"CPU model: {winreg.QueryValueEx(key, 'ProcessorNameString')[0]}")
    except (OSError, ImportError):
        pass
    try:
        import winreg

        display_key = (
            r"SYSTEM\CurrentControlSet\Control\Class"
            r"\{4d36e968-e325-11ce-bfc1-08002be10318}"
        )
        found: set[str] = set()
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, display_key) as root:
            for index in range(32):
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                if not (len(name) == 4 and name.isdigit()):
                    continue
                try:
                    with winreg.OpenKey(root, name) as adapter:
                        label = str(winreg.QueryValueEx(adapter, "DriverDesc")[0])
                        try:
                            driver = str(winreg.QueryValueEx(adapter, "DriverVersion")[0])
                        except OSError:
                            driver = "unknown"
                    if label and label not in found:
                        results.append(f"Display adapter: {label} (driver {driver})")
                        found.add(label)
                    if len(found) >= 4:
                        break
                except OSError:
                    continue
    except (OSError, ImportError):
        pass
    return results


def collect_environment(window=None) -> str:
    """Capture actionable environment facts without dumping secrets or env vars."""
    now = datetime.now(timezone.utc)
    lines = [
        f"Timestamp (UTC): {now.isoformat()}",
        f"Product: {PRODUCT_NAME} {__version__}",
        f"OS: {platform.platform()}",
        f"OS details: {platform.uname().system} {platform.uname().release} "
        f"{platform.uname().version}",
        f"Architecture: {platform.machine()} / {platform.architecture()[0]}",
        f"CPU: {platform.processor() or 'unavailable'} ({os.cpu_count() or '?'} logical cores)",
        f"Memory: {_memory_description()}",
        f"Python: {sys.version.splitlines()[0]}",
        f"Executable: {sys.executable}",
        f"Packaged build: {bool(getattr(sys, 'frozen', False))}",
        f"Process ID: {os.getpid()}",
        f"Process uptime: {max(0, int(time.monotonic() - _SESSION_STARTED))} seconds",
        f"Thread: {__import__('threading').current_thread().name}",
    ]
    lines.extend(_windows_hardware())
    if window is not None:
        project_path = getattr(window, "current_project_path", None)
        lines.append(f"Project: {project_path or '(none)'}")
        session = getattr(window, "project_session", None)
        lines.append(f"Unsaved changes: {getattr(session, 'dirty', 'unknown')}")
        controller = getattr(window, "project_controller", None)
        lines.append(f"Project save running: {getattr(controller, 'saving', 'unknown')}")
        settings = getattr(getattr(window, "settings_service", None), "current", None)
        lines.append(f"FFmpeg setting: {getattr(settings, 'ffmpeg_path', '') or '(auto-detect)'}")
        lines.append(
            f"Export preparation active: "
            f"{getattr(window, '_export_preparation_cancel', None) is not None}"
        )
        try:
            worker = getattr(window, "_render_worker", None)
            lines.append(f"Export worker running: {bool(worker and worker.isRunning())}")
        except (AttributeError, RuntimeError):
            lines.append("Export worker running: unknown")
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion
        from PySide6.QtWidgets import QApplication

        lines.extend((f"PySide6: {pyside_version}", f"Qt: {qVersion()}"))
        app = QApplication.instance()
        if app is not None:
            lines.append(f"Qt platform: {app.platformName()}")
            screens = app.screens()
            lines.append(f"Displays: {len(screens)}")
            for index, screen in enumerate(screens[:6], 1):
                geometry = screen.geometry()
                lines.append(
                    f"Display {index}: {screen.name()} "
                    f"{geometry.width()}x{geometry.height()}, "
                    f"DPR={screen.devicePixelRatio():.2f}"
                )
    except Exception as error:
        lines.append(f"Qt environment: unavailable ({type(error).__name__})")
    return "\n".join(lines)


def recent_log_lines(path: Path | None, max_lines: int = 35) -> str:
    """Read only the end of the existing app log, not arbitrary user files."""
    if path is None:
        return "(application log unavailable)"
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 16_384))
            data = stream.read(16_384).decode("utf-8", errors="replace")
        lines = data.splitlines()
        if size > 16_384 and lines:
            lines = lines[1:]
        return "\n".join(lines[-max_lines:]) or "(no recent log entries)"
    except OSError:
        return "(application log unavailable)"


def build_crash_report(
    exception_type: str,
    message: str,
    traceback_text: str,
    log_path: Path | None,
    *,
    window=None,
    repeat_count: int = 1,
) -> str:
    """Construct one complete, copyable, plain-text diagnostic report."""
    report_id = uuid4().hex[:12]
    stack = (traceback_text or "(no Python traceback available)")[:_MAX_TRACEBACK_CHARS]
    return (
        f"{PRODUCT_NAME} crash report\n"
        f"Report ID: {report_id}\n"
        f"Repeated reports within 90 seconds: {repeat_count}\n"
        f"Error signature: {crash_signature(exception_type, traceback_text)}\n"
        f"Exception: {exception_type}: {message or '(no exception message)'}\n\n"
        f"ENVIRONMENT\n{collect_environment(window)}\n\n"
        f"TRACEBACK / CONTEXT\n{stack}\n\n"
        f"RECENT APPLICATION LOG (last 35 lines)\n"
        f"{recent_log_lines(log_path)}\n\n"
        "PRIVACY NOTICE: This report can contain local file paths, project "
        "names and recent log messages. Review it before sharing. "
        "Environment variables are not collected; recent logs could contain "
        "sensitive data, including credentials if an earlier component logged them.\n"
    )


def save_crash_report(report: str, directory: Path) -> Path:
    """Persist reports outside the project, without overwriting old reports."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    destination = directory / f"crash-{stamp}-{uuid4().hex[:10]}.txt"
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(report)
    # Bound disk usage. Retention failures must not suppress the actual report.
    try:
        old_reports = sorted(directory.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime)
        for old in old_reports[:-50]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return destination


def crash_signature(exception_type: str, traceback_text: str) -> str:
    """Short stable signature for comparing otherwise lengthy error reports."""
    nonempty = [line.strip() for line in traceback_text.splitlines() if line.strip()]
    origin = next((line for line in reversed(nonempty) if line.startswith('File ')), "")
    payload = f"{exception_type}|{origin}".encode("utf-8", errors="replace")
    return sha256(payload).hexdigest()[:12]
