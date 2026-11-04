"""Opt-in, bounded wall-time diagnostics without UI, frame data or media paths.

Set PLAYLIST_CANVAS_PROFILE to an output JSON path *before starting Python*.
Disabled decorators return the original function: no timer/lock on hot paths.
Durations are inclusive wall time, not GPU execution time or additive stages.
"""

from __future__ import annotations

import atexit
from collections import deque
from dataclasses import dataclass, field
from functools import wraps
import json
import logging
import math
import os
from pathlib import Path
import threading
import sys
from time import perf_counter
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
OUTPUT = os.environ.get("PLAYLIST_CANVAS_PROFILE", "").strip()
ENABLED = bool(OUTPUT)


@dataclass(slots=True)
class _Samples:
    count: int = 0
    total: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=1024))


class PerformanceProfile:
    """Fixed-name counters with exact totals and a bounded recent percentile window."""

    def __init__(self) -> None:
        self._samples: dict[str, _Samples] = {}
        self._lock = threading.Lock()

    def observe(self, name: str, value: float) -> None:
        if not math.isfinite(value):
            return
        with self._lock:
            samples = self._samples.get(name)
            if samples is None:
                samples = self._samples[name] = _Samples()
            samples.count += 1
            samples.total += value
            samples.minimum = min(samples.minimum, value)
            samples.maximum = max(samples.maximum, value)
            samples.recent.append(value)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._lock:
            result = {}
            for name, samples in self._samples.items():
                recent = sorted(samples.recent)
                result[name] = {
                    "count": samples.count, "total": samples.total,
                    "mean": samples.total / samples.count,
                    "min": samples.minimum, "max": samples.maximum,
                    "last": samples.recent[-1], "recent_count": len(recent),
                    "recent_p50": recent[math.ceil(len(recent) * 0.50) - 1],
                    "recent_p95": recent[math.ceil(len(recent) * 0.95) - 1],
                }
            return result

    def clear(self) -> None:
        """Start a fresh measurement window after benchmark warm-up."""
        with self._lock:
            self._samples.clear()


PROFILE = PerformanceProfile()


def resident_set_bytes() -> int | None:
    """Current process RSS including Qt/NumPy allocations, without psutil."""
    if sys.platform == "win32":
        import ctypes
        class MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                *((name, ctypes.c_size_t) for name in (
                    "peak", "rss", "paged_peak", "paged", "nonpaged_peak",
                    "nonpaged", "pagefile", "pagefile_peak",
                )),
            ]
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(MemoryCounters), ctypes.c_ulong,
        )
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return int(counters.rss)
    elif sys.platform.startswith("linux"):
        try:
            return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            pass
    return None


def timed(name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Record successful, failed and cancelled calls; preserve return/exception behavior."""
    def decorate(function: Callable[P, T]) -> Callable[P, T]:
        if not ENABLED:
            return function

        @wraps(function)
        def measured(*args: P.args, **kwargs: P.kwargs) -> T:
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                PROFILE.observe(name, perf_counter() - started)
        return measured
    return decorate


def save() -> None:
    """Write once on normal exit; diagnostics must never prevent application shutdown."""
    if not ENABLED:
        return
    try:
        output = Path(OUTPUT)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({
            "schema": 1, "pid": os.getpid(), "metrics": PROFILE.snapshot(),
        }, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
    except OSError:
        logging.getLogger(__name__).warning("Could not save performance diagnostics", exc_info=True)


if ENABLED:
    atexit.register(save)
