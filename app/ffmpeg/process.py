"""Bounded FFmpeg process IO shared by export and AutoMix; no Qt objects."""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from collections.abc import Callable

from app.utils.subprocess_utils import hidden_process_kwargs


class FFmpegProcessError(RuntimeError):
    """FFmpeg failed to start, report progress, or complete."""


class FFmpegProcessCancelled(FFmpegProcessError):
    """The process has stopped after a cancellation request."""


def run_ffmpeg(
    command: list[str], progress_parser: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    capture_stderr: list[str] | None = None,
    stderr_parser: Callable[[str], None] | None = None,
) -> None:
    """Drain both pipes, retain bounded diagnostics, and reap on every exit path.

    Progress callbacks run on the stdout reader, as in the existing renderers.
    A callback failure stops the process and reaches the caller instead of
    silently killing the reader and potentially filling FFmpeg's stdout pipe.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise FFmpegProcessCancelled("Rendering was cancelled.")
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", **hidden_process_kwargs(),
        )
    except OSError as error:
        raise FFmpegProcessError(f"Could not start FFmpeg: {error}") from error

    stderr_lines: deque[str] = deque(maxlen=400)
    reader_errors: list[Exception] = []
    reader_failed = threading.Event()

    def drain(stream, *, progress: bool) -> None:
        try:
            for line in iter(stream.readline, ""):
                if not progress:
                    stderr_lines.append(line)
                    if stderr_parser is not None:
                        stderr_parser(line)
                elif progress_parser is not None and not reader_failed.is_set():
                    progress_parser(line.strip())
        except Exception as error:
            reader_errors.append(error)
            reader_failed.set()

    readers = [
        threading.Thread(target=drain, args=(process.stdout,), kwargs={"progress": True}, daemon=True),
        threading.Thread(target=drain, args=(process.stderr,), kwargs={"progress": False}, daemon=True),
    ]
    started: list[threading.Thread] = []
    cancelled = False
    try:
        for reader in readers:
            reader.start()
            started.append(reader)
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if reader_failed.is_set():
                break
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
    finally:
        try:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass  # The process can exit between poll and terminate.
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait(timeout=3.0)
        finally:
            for reader in started:
                reader.join(timeout=1.0)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            for reader in started:
                if reader.is_alive():
                    reader.join(timeout=1.0)
            if capture_stderr is not None:
                capture_stderr.extend(stderr_lines)
    if cancelled:
        raise FFmpegProcessCancelled("Rendering was cancelled.")
    if reader_errors:
        raise FFmpegProcessError(f"Could not process FFmpeg output: {reader_errors[0]}") from reader_errors[0]
    if process.returncode != 0:
        raise FFmpegProcessError("".join(stderr_lines).strip() or "FFmpeg returned an unknown error.")


def parse_progress_seconds(line: str) -> float | None:
    """Decode FFmpeg's microsecond fields (including its legacy out_time_ms)."""
    if line.startswith(("out_time_us=", "out_time_ms=")):
        try:
            return float(line.split("=", 1)[1]) / 1_000_000
        except ValueError:
            return None
    if line.startswith("out_time="):
        try:
            hours, minutes, seconds = line.split("=", 1)[1].split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except ValueError:
            return None
    return None
