"""Windows GPU Engine PDH telemetry filtered to selected process IDs.

Values are engine busy percentages, not total GPU device utilization.
No other users' processes are included in the recorded measurements.
"""
from __future__ import annotations

from collections import defaultdict
import ctypes
from ctypes import wintypes
import math
import re
import sys

_COUNTER_PATH = r"\GPU Engine(*)\Utilization Percentage"
_PDH_FMT_DOUBLE = 0x00000200
_PDH_FMT_NOCAP100 = 0x00008000
_PDH_MORE_DATA = 0x800007D2
_PID_PATTERN = re.compile(r"(?:^|_)pid_(\d+)(?:_|$)", re.IGNORECASE)
_ENGINE_PATTERN = re.compile(r"(?:^|_)engtype_([a-z0-9]+)(?:_|$)", re.IGNORECASE)


class _CounterValueUnion(ctypes.Union):
    _fields_ = [
        ("longValue", wintypes.LONG),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("wideStringValue", wintypes.LPWSTR),
    ]


class _CounterValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("value", _CounterValueUnion)]


class _CounterValueItem(ctypes.Structure):
    _fields_ = [("name", wintypes.LPWSTR), ("value", _CounterValue)]


def parse_engine_instance(name: str) -> tuple[int, str] | None:
    """Extract the Windows-assigned PID and engine type."""
    pid = _PID_PATTERN.search(name)
    engine = _ENGINE_PATTERN.search(name)
    if pid is None or engine is None:
        return None
    return int(pid.group(1)), engine.group(1).lower()


def aggregate_engine_rows(
    rows: list[tuple[str, float]], process_ids: set[int],
) -> dict[str, dict[str, float]]:
    """Filter first, then group per PID and engine (never a single GPU %)."""
    result: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for name, utilization in rows:
        parsed = parse_engine_instance(name)
        if parsed is None or parsed[0] not in process_ids:
            continue
        if not isinstance(utilization, (int, float)) or not math.isfinite(utilization) or utilization < 0:
            continue
        pid, engine = parsed
        result[pid][engine] += utilization
    return {
        str(pid): {name: round(value, 3) for name, value in sorted(engines.items())}
        for pid, engines in sorted(result.items())
    }


class WindowsGpuProcessCounters:
    """Long-lived PDH wildcard query sampled on the profiling thread."""

    def __init__(self) -> None:
        self.available = False
        self.unavailable_reason: str | None = None
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        self._api = None
        if sys.platform != "win32":
            self.unavailable_reason = "Windows GPU Engine performance counters require Windows"
            return
        try:
            api = ctypes.WinDLL("pdh.dll")
            api.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            api.PdhOpenQueryW.restype = wintypes.DWORD
            api.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            api.PdhAddEnglishCounterW.restype = wintypes.DWORD
            api.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            api.PdhCollectQueryData.restype = wintypes.DWORD
            api.PdhGetFormattedCounterArrayW.argtypes = [
                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            ]
            api.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
            api.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            api.PdhCloseQuery.restype = wintypes.DWORD
            status = api.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
            if status != 0:
                raise OSError(f"PdhOpenQueryW returned {status:#x}")
            self._api = api
            status = api.PdhAddEnglishCounterW(
                self._query, _COUNTER_PATH, 0, ctypes.byref(self._counter),
            )
            if status != 0:
                raise OSError(f"PdhAddEnglishCounterW returned {status:#x}")
            # Prime rate counters before the first live sample.
            status = api.PdhCollectQueryData(self._query)
            if status != 0:
                raise OSError(f"PdhCollectQueryData returned {status:#x}")
            self.available = True
        except (OSError, AttributeError, TypeError) as exc:
            self.unavailable_reason = str(exc)
            self.close()

    def sample(self, process_ids: set[int]) -> dict[str, dict[str, float]] | None:
        if not self.available or self._api is None:
            return None
        status = self._api.PdhCollectQueryData(self._query)
        if status != 0:
            self.unavailable_reason = f"PdhCollectQueryData returned {status:#x}"
            return None
        if not process_ids:
            return {}
        for _ in range(3):
            required = wintypes.DWORD(0)
            count = wintypes.DWORD(0)
            status = self._api.PdhGetFormattedCounterArrayW(
                self._counter, _PDH_FMT_DOUBLE | _PDH_FMT_NOCAP100,
                ctypes.byref(required), ctypes.byref(count), None,
            )
            if status == 0 and count.value == 0:
                return {}
            if status != _PDH_MORE_DATA or not 0 < required.value <= 16 * 1024 * 1024:
                self.unavailable_reason = f"GPU Engine query returned {status:#x}"
                return None
            buffer = ctypes.create_string_buffer(required.value)
            status = self._api.PdhGetFormattedCounterArrayW(
                self._counter, _PDH_FMT_DOUBLE | _PDH_FMT_NOCAP100,
                ctypes.byref(required), ctypes.byref(count),
                ctypes.cast(buffer, ctypes.c_void_p),
            )
            if status == _PDH_MORE_DATA:
                continue
            if status != 0:
                self.unavailable_reason = f"GPU Engine read returned {status:#x}"
                return None
            items = ctypes.cast(buffer, ctypes.POINTER(_CounterValueItem))
            rows = [
                (items[i].name, items[i].value.value.doubleValue)
                for i in range(count.value)
                if items[i].value.CStatus in (0, 1) and items[i].name
            ]
            return aggregate_engine_rows(rows, process_ids)
        self.unavailable_reason = "GPU Engine instances changed during sampling"
        return None

    def close(self) -> None:
        if self._api is not None and self._query.value:
            self._api.PdhCloseQuery(self._query)
        self._query = ctypes.c_void_p()
        self.available = False
