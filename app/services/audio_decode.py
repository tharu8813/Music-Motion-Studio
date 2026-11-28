"""Read local audio as frame-major float32 PCM for analysis."""

import math
import struct
import sys
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event

import numpy as np

from app.video.duration_probe import m4a_audio_edit


class AudioDecodeError(RuntimeError):
    """The in-process decoder could not read the requested audio."""


def _windows_audio(path: Path) -> bool:
    return sys.platform == "win32" and path.suffix.lower() in {".aac", ".m4a"}


def _decode_windows(path: Path, start: float, duration: float | None, cancel: Event | None = None):
    try:
        from music_motion_native import decode_windows_audio

        is_m4a = path.suffix.lower() == ".m4a"
        # Media Foundation's MP4 source exposes the presentation timeline.
        # The edit's media offset is not an additional presentation-time seek:
        # adding it again can discard AAC priming twice and shorten the track.
        # Keep the edit's duration to trim trailing encoder padding.
        _media_offset, length = m4a_audio_edit(path) if is_m4a else (0.0, None)
        if length is not None:
            remaining = max(0.0, length - start)
            duration = remaining if duration is None else min(duration, remaining)
        rate, channels, frames, pcm = decode_windows_audio(
            path.resolve(), start, duration, cancel.is_set if cancel is not None else None,
        )
        if length is not None:
            frames = round(length * rate)
        elif is_m4a and frames is not None:
            pcm = pcm[:max(0, frames - round(start * rate)) * channels * 4]
        return rate, channels, frames, pcm
    except InterruptedError as error:
        raise CancelledError() from error
    except (ImportError, OSError, RuntimeError, ValueError, struct.error) as error:
        raise AudioDecodeError(str(error)) from error


def pcm_info(path: Path) -> tuple[int, int | None, int]:
    """Return the local decoder's sample rate, frame count, and channel count."""
    try:
        import soundfile

        info = soundfile.info(path)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        if not _windows_audio(path):
            raise AudioDecodeError(str(error)) from error
        rate, channels, frames, _ = _decode_windows(path, 0, 0)
        if path.suffix.lower() == ".aac":
            frames = None  # The ADTS source estimates duration from bitrate.
        return rate, frames, channels
    return info.samplerate, info.frames, info.channels


def _convert_channels(pcm: np.ndarray, channels: int) -> np.ndarray:
    # Match FFmpeg's default mono/stereo mix levels.
    if pcm.shape[1] == channels:
        return pcm
    if channels == 1:
        return pcm.mean(axis=1, keepdims=True, dtype=np.float32)
    return np.repeat(pcm * np.float32(math.sqrt(0.5)), channels, axis=1)


def read_pcm(
    path: Path, cancel_event: Event, *, sample_rate: int, channels: int,
    start: float = 0.0, duration: float | None = None,
) -> np.ndarray:
    """Read mono or stereo PCM, with an optional window in source seconds."""
    if sample_rate <= 0 or channels not in (1, 2):
        raise ValueError("PCM requires a positive sample rate and one or two channels.")
    if not math.isfinite(start) or start < 0:
        raise ValueError("The PCM start time must be finite and non-negative.")
    if duration is not None and (not math.isfinite(duration) or duration < 0):
        raise ValueError("The PCM duration must be finite and non-negative.")
    if cancel_event.is_set():
        raise CancelledError()
    if not path.is_file():
        raise AudioDecodeError(f"Audio file is missing: {path}")

    try:
        import soundfile
        import soxr

        with soundfile.SoundFile(path) as source:
            if source.channels not in (1, 2):
                raise AudioDecodeError("The in-process decoder expects mono or stereo audio.")
            first = min(source.frames, round(start * source.samplerate))
            stop = (source.frames if duration is None else
                    min(source.frames, round((start + duration) * source.samplerate)))
            if stop <= first:
                return np.empty((0, channels), dtype=np.float32)
            if first:
                source.seek(first)
            resampler = (None if source.samplerate == sample_rate else
                         soxr.ResampleStream(source.samplerate, sample_rate, channels))
            blocks = []
            remaining = stop - first
            while remaining:
                if cancel_event.is_set():
                    raise CancelledError()
                count = min(remaining, 65_536)
                block = source.read(count, dtype="float32", always_2d=True)
                if len(block) != count or not np.isfinite(block).all():
                    raise AudioDecodeError("The audio file contains incomplete or invalid samples.")
                remaining -= count
                block = _convert_channels(block, channels)
                if resampler is not None:
                    block = resampler.resample_chunk(block, last=remaining == 0)
                blocks.append(block)
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        if _windows_audio(path):
            return _read_windows_pcm(path, cancel_event, sample_rate, channels, start, duration)
        if isinstance(error, AudioDecodeError):
            raise
        raise AudioDecodeError(f"Could not read audio: {error}") from error
    if cancel_event.is_set():
        raise CancelledError()
    return np.concatenate(blocks, axis=0)


def _read_windows_pcm(
    path: Path, cancel: Event, sample_rate: int, channels: int,
    start: float, duration: float | None,
) -> np.ndarray:
    rate, source_channels, _frames, raw = _decode_windows(path, start, duration, cancel)
    pcm = np.frombuffer(raw, dtype="<f4").reshape(-1, source_channels)
    if not np.isfinite(pcm).all():
        raise AudioDecodeError("The audio file contains invalid samples.")
    pcm = _convert_channels(pcm, channels)
    if rate != sample_rate and len(pcm):
        try:
            import soxr

            pcm = soxr.resample(pcm, rate, sample_rate)
        except (ImportError, RuntimeError, ValueError) as error:
            raise AudioDecodeError(f"Could not resample Windows audio: {error}") from error
    if cancel.is_set():
        raise CancelledError()
    return pcm if pcm.flags.writeable else pcm.copy()
