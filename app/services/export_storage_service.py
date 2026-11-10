"""Storage estimates and low-impact live disk accounting for video export."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import shutil
import threading

from PySide6.QtCore import QThread, Signal


MIB = 1024 * 1024
GIB = 1024 * MIB
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExportStorageEstimate:
    """Conservative byte estimates shown before an export starts."""

    visual_files: int
    processing_files: int
    result_low: int
    result_high: int
    peak_temporary: int


@dataclass(frozen=True, slots=True)
class ExportStorageSnapshot:
    """One live view of files owned by the active export."""

    categories: dict[str, int] = field(default_factory=dict)
    temporary_total: int = 0
    output_in_progress: int = 0
    disk_total: int = 0
    disk_free: int = 0


def parse_bitrate(value: str, default_kbps: int = 192) -> int:
    """Return a bitrate label such as ``192k`` as bits per second."""
    text = str(value).strip().lower()
    try:
        if text.endswith("k"):
            return max(1, int(float(text[:-1]) * 1000))
        if text.endswith("m"):
            return max(1, int(float(text[:-1]) * 1_000_000))
        return max(1, int(float(text)))
    except (TypeError, ValueError):
        return default_kbps * 1000


def estimate_export_storage(
    width: int,
    height: int,
    fps: int,
    duration_seconds: float,
    crf: int,
    audio_bitrate: str,
    layer_count: int = 1,
    piped: bool = False,
) -> ExportStorageEstimate:
    """Estimate working and result sizes without pretending CRF is exact.

    CRF targets visual quality rather than a fixed bitrate, so the final MP4 is
    deliberately expressed as a range.  Temporary estimates use the same
    lossless-intermediate assumptions as the export preflight and include room
    for audio normalization, manifests and the final file being written.
    ``piped`` exports capture the Canvas straight into the final encoder, so
    only a few still images per layer are staged.
    """
    width = max(2, int(width))
    height = max(2, int(height))
    fps = max(1, int(fps))
    seconds = max(0.0, float(duration_seconds))
    layers = max(1, int(layer_count))
    raw_rate = width * height * 3 * fps

    # Canvas lossless streams/PNGs typically occupy far less than raw RGB, but
    # highly textured images and several independent layers can be larger.
    visual_files = (
        int(width * height * 3 * 0.40 * 4 * layers)
        if piped else int(raw_rate * seconds * 0.40 * layers)
    )
    audio_work = int(seconds * (1_536_000 / 8))  # stereo PCM-like safety budget
    processing_files = max(8 * MIB, int(audio_work * 1.25)) if seconds else 0

    # A 1080p30 CRF 18 H.264 export commonly lands around this broad baseline.
    # Scale with pixel throughput and CRF, then retain a wide content-dependent
    # range instead of presenting a misleading single value.
    throughput_scale = (width * height * fps) / (1920 * 1080 * 30)
    quality_scale = 2 ** ((18 - max(0, min(51, int(crf)))) / 6.0)
    video_bps = max(500_000.0, 10_000_000.0 * throughput_scale * quality_scale)
    audio_bps = float(parse_bitrate(audio_bitrate))
    midpoint = seconds * (video_bps + audio_bps) / 8.0
    result_low = int(midpoint * 0.55)
    result_high = int(midpoint * 1.65)
    peak_temporary = int((visual_files + processing_files) * 1.30 + result_high)
    return ExportStorageEstimate(
        visual_files=visual_files,
        processing_files=processing_files,
        result_low=result_low,
        result_high=max(result_low, result_high),
        peak_temporary=peak_temporary,
    )


def format_bytes(count: int) -> str:
    """Format byte counts with compact binary units suitable for the UI."""
    value = float(max(0, int(count)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def estimate_audio_export_storage(
    duration_seconds: float, format_name: str, audio_bitrate: str,
) -> ExportStorageEstimate:
    """Budget audio processing without allocating any Canvas intermediates."""
    seconds = max(0.0, duration_seconds)
    pcm_size = seconds * 48_000 * 2 * 2
    result = pcm_size if format_name in {"wav", "flac"} else seconds * parse_bitrate(audio_bitrate) / 8
    processing = int(pcm_size * 3 + 8 * MIB) if seconds else 0
    return ExportStorageEstimate(
        0, processing, int(result * (0.4 if format_name == "flac" else 0.95)),
        int(result * 1.1 + MIB), int(processing * 1.3 + result * 1.1 + MIB),
    )


class ExportStorageMonitor(QThread):
    """Scan export-owned paths away from the GUI thread at a low frequency."""

    snapshot_ready = Signal(object)

    def __init__(self, output_path: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._output_path = Path(output_path).expanduser().resolve()
        self._frame_root: Path | None = None
        self._audio_root: Path | None = None
        self._render_root: Path | None = None
        self._output_staging: Path | None = None
        self._paths_lock = threading.Lock()
        self._stop_event = threading.Event()

    def set_path(self, kind: str, path: str | Path | None) -> None:
        """Update a monitored path safely from either Qt thread."""
        value = Path(path).resolve() if path else None
        with self._paths_lock:
            if kind == "frames":
                self._frame_root = value
            elif kind == "audio":
                self._audio_root = value
            elif kind == "render":
                self._render_root = value
            elif kind == "output":
                self._output_staging = value

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.snapshot_ready.emit(self._snapshot())
            self._stop_event.wait(2.0)
        # Deliver one final reading before temporary directories are discarded.
        self.snapshot_ready.emit(self._snapshot())

    def _snapshot(self) -> ExportStorageSnapshot:
        with self._paths_lock:
            frame_root = self._frame_root
            audio_root = self._audio_root
            render_root = self._render_root
            output_staging = self._output_staging
        categories = {
            "visuals": self._tree_size(frame_root),
            "audio": self._tree_size(audio_root),
            "effects": 0,
            "processing": 0,
        }
        if render_root is not None and render_root.is_dir():
            try:
                for root, _directories, files in os.walk(render_root):
                    for filename in files:
                        path = Path(root) / filename
                        size = self._file_size(path)
                        lower = filename.lower()
                        if lower.startswith(("track_", "silence_", "playlist_audio")):
                            categories["audio"] += size
                        elif lower.startswith("python_visualizer_"):
                            categories["effects"] += size
                        elif lower.startswith(("canvas_", "layer_")):
                            categories["visuals"] += size
                        else:
                            categories["processing"] += size
            except OSError:
                LOGGER.debug("Could not scan export render files", exc_info=True)
        # Final MP4s are muxed into an export-owned staging *directory*.
        # The prior accounting only handled files, incorrectly showing 0 B
        # for the result throughout the mux step.
        output_size = (
            self._tree_size(output_staging)
            if output_staging is not None and output_staging.is_dir()
            else self._file_size(output_staging)
        )
        try:
            usage = shutil.disk_usage(self._output_path.parent)
            disk_total, disk_free = usage.total, usage.free
        except OSError:
            disk_total = disk_free = 0
        return ExportStorageSnapshot(
            categories=categories,
            temporary_total=sum(categories.values()) + output_size,
            output_in_progress=output_size,
            disk_total=disk_total,
            disk_free=disk_free,
        )

    @classmethod
    def _tree_size(cls, root: Path | None) -> int:
        if root is None or not root.is_dir():
            return 0
        total = 0
        try:
            for directory, _directories, files in os.walk(root):
                for filename in files:
                    total += cls._file_size(Path(directory) / filename)
        except OSError:
            LOGGER.debug("Could not scan export frame files", exc_info=True)
        return total

    @staticmethod
    def _file_size(path: Path | None) -> int:
        if path is None:
            return 0
        try:
            return path.stat().st_size if path.is_file() else 0
        except OSError:
            return 0
