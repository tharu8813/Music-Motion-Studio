"""Lightweight, cancellable audio-amplitude envelopes for the playback seek ruler.

This is display-only: it never changes the preview's decoded audio, AutoMix
schedule, or export pipeline. Each track is decoded sequentially off the GUI
thread with a bounded PCM buffer.
"""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess

import numpy as np
from PySide6.QtCore import QThread, Signal

from app.utils.subprocess_utils import background_work, hidden_process_kwargs

LOGGER = logging.getLogger(__name__)

SAMPLE_RATE = 4000
SAMPLES_PER_BIN = 400  # 100 ms, or ten amplitude bars per second.
READ_BYTES = SAMPLES_PER_BIN * 2 * 128


def pcm_peak_bins(raw: bytes, samples_per_bin: int = SAMPLES_PER_BIN) -> np.ndarray:
    """Return peak absolute PCM16 amplitude per time bin, including the last short bin."""
    if samples_per_bin <= 0:
        raise ValueError("samples_per_bin must be positive")
    samples = np.frombuffer(raw[: len(raw) & ~1], dtype="<i2")
    if samples.size == 0:
        return np.empty(0, dtype=np.float32)
    whole, remainder = divmod(samples.size, samples_per_bin)
    result = np.empty(whole + bool(remainder), dtype=np.float32)
    if whole:
        # Convert before abs(): abs(int16(-32768)) otherwise overflows.
        frames = samples[:whole * samples_per_bin].astype(np.int32).reshape(whole, samples_per_bin)
        result[:whole] = np.max(np.abs(frames), axis=1) / 32768.0
    if remainder:
        result[whole] = np.max(np.abs(samples[whole * samples_per_bin:].astype(np.int32))) / 32768.0
    return result


class TimelineWaveformWorker(QThread):
    """Send compact amplitude envelopes one track at a time without blocking playback."""

    ready = Signal(int, object)

    def __init__(self, executable: Path, tracks: list[tuple[int, str]], parent=None) -> None:
        super().__init__(parent)
        self.executable = Path(executable)
        self.tracks = tuple(tracks)
        self._process: subprocess.Popen | None = None

    def cancel(self) -> None:
        self.requestInterruption()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def run(self) -> None:
        # Keep playback and the UI compositor ahead of this optional analysis.
        with background_work():
            for index, path in self.tracks:
                if self.isInterruptionRequested():
                    return
                if not Path(path).is_file():
                    continue
                peaks = self._decode(path)
                if self.isInterruptionRequested():
                    return
                if peaks is not None and peaks.size:
                    self.ready.emit(index, peaks)

    def _decode(self, path: str) -> np.ndarray | None:
        process = None
        chunks: list[np.ndarray] = []
        pending = b""
        try:
            process = subprocess.Popen(
                [str(self.executable), "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-threads", "1", "-i", path, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                 "-f", "s16le", "pipe:1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                **hidden_process_kwargs(),
            )
            self._process = process
            assert process.stdout is not None
            while not self.isInterruptionRequested():
                data = process.stdout.read(READ_BYTES)
                if not data:
                    break
                pending += data
                complete = len(pending) // (2 * SAMPLES_PER_BIN) * (2 * SAMPLES_PER_BIN)
                if complete:
                    chunks.append(pcm_peak_bins(pending[:complete]))
                    pending = pending[complete:]
            if self.isInterruptionRequested() or process.wait(timeout=2) != 0:
                return None
            if pending:
                chunks.append(pcm_peak_bins(pending))
            return np.concatenate(chunks) if chunks else None
        except (OSError, subprocess.SubprocessError) as error:
            if not self.isInterruptionRequested():
                LOGGER.debug("Timeline waveform unavailable for %s: %s", path, error)
            return None
        finally:
            self._process = None
            if process is not None:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                if process.stdout is not None:
                    process.stdout.close()
                try:
                    process.wait(timeout=2)
                except subprocess.SubprocessError:
                    pass
