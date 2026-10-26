"""Stream captured Canvas frames straight into the final FFmpeg process.

Each capture stream (the opaque base and every transparent Z band) gets its own
named pipe: a Windows ``\\\\.\\pipe\\...`` server or a POSIX FIFO. FFmpeg opens
the pipe as an ordinary rawvideo input, so the final encoder composites the
Canvas while it is being captured and no intermediate video reaches the disk.

Why one pipe per stream instead of one composited stream: Python visualizers
and video clips sit *between* Z bands, so the bands cannot be flattened before
FFmpeg without re-implementing its decoding and filters. stdin carries only one
input, and a loopback TCP socket can trigger firewall prompts or be reached by
another local process first.

Deadlock: every stream has its own writer thread and bounded queue. The Qt
thread captures all streams in timeline order, so when it blocks on one full
queue every other stream already holds data up to that moment and FFmpeg can
always advance. A reader that stops consuming is detected by
:attr:`PipedCanvasStream.last_activity` (see the export session's watchdog).
"""

from __future__ import annotations

from app.utils.performance import timed

from collections.abc import Callable
from dataclasses import dataclass
import errno
import logging
from math import floor
import os
from pathlib import Path
import secrets
import select
import threading
from time import monotonic, sleep

from PySide6.QtGui import QImage

from app.renderer.bounded_pipeline import (
    BoundedExportPipeline,
    ExportPipelineCancelledError,
    ExportPipelineError,
)


# FFmpeg's per-input packet queue. Each packet is one raw frame (3.7 MB at
# 720p, 33 MB at 4K), so keep it small; independent writers need no slack.
PIPE_THREAD_QUEUE_SIZE = 4
_WINDOWS_PIPE_BUFFER_BYTES = 1 << 20
_CONNECT_POLL_SECONDS = 0.02
_WRITE_POLL_MILLISECONDS = 50

ENVIRONMENT_DISABLE = "PLAYLIST_CANVAS_DISABLE_PIPED_EXPORT"
LOGGER = logging.getLogger(__name__)


class CanvasPipeError(RuntimeError):
    """Raised when a Canvas pipe cannot be created or fed."""


class CanvasPipeCancelledError(CanvasPipeError):
    """Raised when a Canvas pipe stopped because its stream was cancelled."""


class _ReaderClosedError(CanvasPipeError):
    """The reading FFmpeg process closed its end of the pipe."""


def piped_export_supported() -> bool:
    """Return whether this platform can feed FFmpeg through named pipes."""
    if os.environ.get(ENVIRONMENT_DISABLE, "").strip() not in {"", "0"}:
        return False
    if os.name == "nt":
        try:
            import _winapi  # noqa: F401
        except ImportError:
            return False
        return True
    return hasattr(os, "mkfifo")


class _PipeEndpoint:
    """The writing end of one named pipe."""

    path: str

    def connect(self, cancelled: Callable[[], bool]) -> None:
        raise NotImplementedError

    def write(self, data: memoryview, cancelled: Callable[[], bool]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _PosixFifoEndpoint(_PipeEndpoint):
    def __init__(self, directory: Path, name: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = str(directory / f"{name}.fifo")
        try:
            os.mkfifo(self.path, 0o600)
        except OSError as error:
            raise CanvasPipeError(f"Could not create a Canvas pipe: {error}") from error
        self._descriptor: int | None = None
        self._lock = threading.Lock()

    def connect(self, cancelled: Callable[[], bool]) -> None:
        while True:
            if cancelled():
                raise CanvasPipeCancelledError("Canvas pipe was cancelled.")
            try:
                # Non-blocking open fails with ENXIO until FFmpeg opens the
                # reading side, which keeps the wait cancellable.
                descriptor = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise CanvasPipeError(
                        f"Could not open a Canvas pipe: {error}"
                    ) from error
                sleep(_CONNECT_POLL_SECONDS)
                continue
            # Stay non-blocking: writes wait in select() so cancellation is
            # noticed even while FFmpeg is not reading.
            with self._lock:
                self._descriptor = descriptor
            return

    def write(self, data: memoryview, cancelled: Callable[[], bool]) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            raise CanvasPipeError("Canvas pipe is not connected.")
        offset = 0
        total = len(data)
        while offset < total:
            if cancelled():
                raise CanvasPipeCancelledError("Canvas pipe was cancelled.")
            try:
                _readable, writable, _errors = select.select(
                    [], [descriptor], [], _WRITE_POLL_MILLISECONDS / 1000.0,
                )
                if not writable:
                    continue
                offset += os.write(descriptor, data[offset:])
            except BlockingIOError:
                continue
            except BrokenPipeError as error:
                raise _ReaderClosedError("FFmpeg closed the Canvas pipe.") from error
            except OSError as error:
                raise CanvasPipeError(f"Could not write a Canvas frame: {error}") from error

    def close(self) -> None:
        with self._lock:
            descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


class _WindowsNamedPipeEndpoint(_PipeEndpoint):
    """An outbound, overlapped, single-instance, local-only named pipe server."""

    # Win32 values, used when a Python build's ``_winapi`` lacks the constant.
    _PIPE_ACCESS_OUTBOUND = 0x00000002
    _FILE_FLAG_OVERLAPPED = 0x40000000
    _FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    _PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    _WAIT_OBJECT_0 = 0x00000000
    _ERROR_BROKEN_PIPE = 109
    _ERROR_NO_DATA = 232
    _ERROR_PIPE_CONNECTED = 535
    _ERROR_OPERATION_ABORTED = 995
    _ERROR_IO_PENDING = 997

    def __init__(self, name: str) -> None:
        import _winapi

        self._winapi = _winapi
        self.path = (
            rf"\\.\pipe\playlist-canvas-{os.getpid()}-"
            f"{secrets.token_hex(8)}-{name}"
        )
        try:
            self._handle = _winapi.CreateNamedPipe(
                self.path,
                self._constant("PIPE_ACCESS_OUTBOUND")
                | self._constant("FILE_FLAG_OVERLAPPED")
                | self._constant("FILE_FLAG_FIRST_PIPE_INSTANCE"),
                # PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT are all 0.
                self._PIPE_REJECT_REMOTE_CLIENTS,
                1,
                _WINDOWS_PIPE_BUFFER_BYTES,
                _WINDOWS_PIPE_BUFFER_BYTES,
                0,
                0,
            )
        except OSError as error:
            raise CanvasPipeError(f"Could not create a Canvas pipe: {error}") from error
        self._lock = threading.Lock()
        self._closed = False

    def _constant(self, name: str) -> int:
        return int(getattr(self._winapi, name, getattr(self, f"_{name}")))

    def _raise_for(self, error_code: int) -> None:
        if error_code in (0, self._ERROR_PIPE_CONNECTED):
            return
        if error_code in (self._ERROR_BROKEN_PIPE, self._ERROR_NO_DATA):
            raise _ReaderClosedError("FFmpeg closed the Canvas pipe.")
        if error_code == self._ERROR_OPERATION_ABORTED or self._closed:
            raise CanvasPipeCancelledError("Canvas pipe was cancelled.")
        raise CanvasPipeError(f"Canvas pipe I/O failed (Windows error {error_code}).")

    def _result(self, overlapped: object) -> int:
        try:
            transferred, error_code = overlapped.GetOverlappedResult(True)
        except OSError as error:
            self._raise_for(int(getattr(error, "winerror", -1) or -1))
            raise CanvasPipeError(f"Canvas pipe I/O failed: {error}") from error
        self._raise_for(error_code)
        return transferred

    def _wait(self, overlapped: object, cancelled: Callable[[], bool]) -> int:
        winapi = self._winapi
        while True:
            result = winapi.WaitForMultipleObjects(
                [overlapped.event], False, _WRITE_POLL_MILLISECONDS,
            )
            if result == self._WAIT_OBJECT_0:
                return self._result(overlapped)
            if cancelled() or self._closed:
                try:
                    overlapped.cancel()
                except OSError:
                    pass
                try:
                    overlapped.GetOverlappedResult(True)
                except OSError:
                    pass
                raise CanvasPipeCancelledError("Canvas pipe was cancelled.")

    def connect(self, cancelled: Callable[[], bool]) -> None:
        try:
            # _winapi signals the event itself when FFmpeg already connected
            # (ERROR_PIPE_CONNECTED), so one wait covers both orders.
            overlapped = self._winapi.ConnectNamedPipe(self._handle, overlapped=True)
        except OSError as error:
            if getattr(error, "winerror", None) == self._ERROR_PIPE_CONNECTED:
                return  # FFmpeg opened the pipe first; it is already connected.
            raise CanvasPipeError(f"Could not open a Canvas pipe: {error}") from error
        self._wait(overlapped, cancelled)

    def write(self, data: memoryview, cancelled: Callable[[], bool]) -> None:
        offset = 0
        total = len(data)
        while offset < total:
            if cancelled():
                raise CanvasPipeCancelledError("Canvas pipe was cancelled.")
            chunk = data[offset:offset + _WINDOWS_PIPE_BUFFER_BYTES]
            try:
                overlapped, error_code = self._winapi.WriteFile(
                    self._handle, chunk, overlapped=True,
                )
            except OSError as error:
                self._raise_for(int(getattr(error, "winerror", -1) or -1))
                raise CanvasPipeError(f"Could not write a Canvas frame: {error}") from error
            if error_code not in (0, self._ERROR_IO_PENDING):
                self._raise_for(error_code)
            written = self._wait(overlapped, cancelled)
            if written <= 0:
                raise _ReaderClosedError("FFmpeg closed the Canvas pipe.")
            offset += written

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._winapi.CloseHandle(self._handle)
        except OSError:
            pass


def _create_endpoint(directory: Path, name: str) -> _PipeEndpoint:
    if os.name == "nt":
        return _WindowsNamedPipeEndpoint(name)
    return _PosixFifoEndpoint(directory, name)


@dataclass(frozen=True, slots=True)
class _QueuedCanvasFrame:
    image: QImage
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class PipedCanvasStreamResult:
    frame_count: int
    duration_seconds: float
    reader_closed_early: bool
    peak_buffered_frames: int


class PipedCanvasStream:
    """Feed one Canvas stream to FFmpeg as constant-frame-rate raw video.

    The first submitted frame fixes the resolution, which is when
    :meth:`input_arguments` becomes available. Frames are converted and
    repeated to CFR on the writer thread, exactly like the intermediate
    encoder did, so timing is unchanged.
    """

    def __init__(
        self,
        directory: Path,
        stream_key: str,
        fps: int,
        *,
        preserve_alpha: bool,
        queue_capacity: int = 3,
        producer_cancel_event: threading.Event | None = None,
        producer_wait_callback: Callable[[], None] | None = None,
    ) -> None:
        if fps <= 0:
            raise ValueError("Canvas pipe FPS must be greater than zero.")
        self.stream_key = stream_key
        self.fps = fps
        self.preserve_alpha = preserve_alpha
        self.queue_capacity = queue_capacity
        self.producer_cancel_event = producer_cancel_event
        self.producer_wait_callback = producer_wait_callback
        safe_name = stream_key.replace(":", "-")
        self._endpoint = _create_endpoint(directory, f"canvas-{safe_name}")
        self._pipeline: BoundedExportPipeline[_QueuedCanvasFrame] | None = None
        self._width = 0
        self._height = 0
        self._submitted_seconds = 0.0
        self._written_seconds = 0.0
        self._frame_count = 0
        self._connected = threading.Event()
        self._reader_closed = False
        self._finished = False
        self._stop = threading.Event()
        self.last_activity = monotonic()
        self._submitted_states = 0
        self._first_submit_at: float | None = None
        self._connect_seconds: float | None = None

    # -- producer side (Qt thread) ------------------------------------

    @property
    def path(self) -> str:
        return self._endpoint.path

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def started(self) -> bool:
        return self._pipeline is not None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def reader_closed(self) -> bool:
        return self._reader_closed

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def pending_frames(self) -> int:
        return self._pipeline.pending_count if self._pipeline is not None else 0

    def diagnostics(self) -> dict[str, object]:
        """What this stream did, for logs and bug reports; no frames or paths."""
        pipeline = self._pipeline
        return {
            "stream": self.stream_key,
            "size": f"{self._width}x{self._height}",
            "submitted_states": self._submitted_states,
            "submitted_seconds": round(self._submitted_seconds, 3),
            "written_frames": self._frame_count,
            "pending": self.pending_frames,
            "peak_pending": pipeline.peak_buffered_items if pipeline is not None else 0,
            "capacity": self.queue_capacity,
            "connect_seconds": (
                None if self._connect_seconds is None else round(self._connect_seconds, 3)
            ),
            "idle_seconds": round(monotonic() - self.last_activity, 3),
            "reader_closed": self._reader_closed,
        }

    def input_arguments(self) -> list[str]:
        """FFmpeg input options that read this pipe as raw CFR video."""
        if not self.started:
            raise CanvasPipeError("A Canvas pipe has no frames yet.")
        return [
            "-thread_queue_size", str(PIPE_THREAD_QUEUE_SIZE),
            # The raw format is fully described below; never wait for more
            # than one frame while FFmpeg opens its inputs one after another.
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "rawvideo",
            "-pix_fmt", "rgba" if self.preserve_alpha else "bgr0",
            "-video_size", f"{self._width}x{self._height}",
            "-framerate", str(self.fps),
            "-i", self.path,
        ]

    @timed("export.frame_queue_seconds")
    def submit(self, image: QImage, duration_seconds: float) -> None:
        """Queue one captured state; conversion and pipe writes run off-thread."""
        if self._finished:
            raise CanvasPipeError("Canvas pipe has already finished.")
        if image.isNull():
            raise CanvasPipeError("Cannot stream an empty Canvas frame.")
        if duration_seconds <= 0.0:
            raise CanvasPipeError("Canvas frame duration must be greater than zero.")
        if self._pipeline is None:
            self._width = image.width()
            self._height = image.height()
            self._pipeline = BoundedExportPipeline(
                self._write_frame,
                capacity=self.queue_capacity,
                name=f"canvas-pipe-{self.stream_key}",
                on_cancel=self._stop.set,
            )
            self._pipeline.start()
        if image.width() != self._width or image.height() != self._height:
            raise CanvasPipeError("All streamed Canvas frames must use one resolution.")
        if self._first_submit_at is None:
            self._first_submit_at = monotonic()
        self._submitted_states += 1
        self._submitted_seconds += duration_seconds
        try:
            self._pipeline.submit(
                _QueuedCanvasFrame(QImage(image), duration_seconds),
                producer_cancel_event=self.producer_cancel_event,
                producer_wait_callback=self.producer_wait_callback,
            )
        except ExportPipelineCancelledError as error:
            self.cancel()
            raise CanvasPipeCancelledError("Canvas pipe was cancelled.") from error
        except ExportPipelineError as error:
            self.cancel()
            raise CanvasPipeError(str(getattr(error, "cause", error))) from error

    def finish(self) -> PipedCanvasStreamResult:
        """Drain the queue and close the pipe so FFmpeg sees end of input."""
        if self._finished:
            raise CanvasPipeError("Canvas pipe has already finished.")
        self._finished = True
        pipeline = self._pipeline
        if pipeline is None:
            self._endpoint.close()
            raise CanvasPipeError("No Canvas frames were submitted to the pipe.")
        try:
            pipeline.finish(wait_callback=self.producer_wait_callback)
        except ExportPipelineCancelledError as error:
            self.cancel()
            raise CanvasPipeCancelledError("Canvas pipe was cancelled.") from error
        except ExportPipelineError as error:
            self.cancel()
            raise CanvasPipeError(str(getattr(error, "cause", error))) from error
        self._endpoint.close()
        LOGGER.info("Canvas pipe finished: %s", self.diagnostics())
        return PipedCanvasStreamResult(
            self._frame_count,
            self._written_seconds,
            self._reader_closed,
            pipeline.peak_buffered_items,
        )

    def cancel(self) -> None:
        """Stop the writer, abandon queued frames, and close the pipe."""
        if not self._stop.is_set():
            LOGGER.info("Canvas pipe cancelled: %s", self.diagnostics())
        self._finished = True
        self._stop.set()
        pipeline = self._pipeline
        if pipeline is not None:
            pipeline.cancel()
            try:
                pipeline.finish(timeout_seconds=5.0)
            except ExportPipelineError:
                pass
        # Close only after the writer noticed the stop, so no I/O is pending
        # on the handle.
        self._endpoint.close()

    # -- consumer side (writer thread) --------------------------------

    def _cancelled(self) -> bool:
        return self._stop.is_set() or (
            self.producer_cancel_event is not None
            and self.producer_cancel_event.is_set()
        )

    @timed("export.frame_delivery_seconds")
    def _write_frame(self, frame: _QueuedCanvasFrame) -> None:
        self._written_seconds += frame.duration_seconds
        if self._reader_closed:
            # FFmpeg reached the timeline end (``-t``) a frame early and
            # stopped reading. Its exit status decides success; just drain.
            return
        target_count = max(1, floor(self._written_seconds * self.fps + 0.5))
        repeat_count = max(0, target_count - self._frame_count)
        if repeat_count == 0:
            return
        if not self._connected.is_set():
            try:
                self._endpoint.connect(self._cancelled)
            except CanvasPipeCancelledError:
                return
            self._connected.set()
            self.last_activity = monotonic()
            if self._first_submit_at is not None:
                self._connect_seconds = self.last_activity - self._first_submit_at
        prepared = frame.image.convertToFormat(
            QImage.Format.Format_RGBA8888
            if self.preserve_alpha else QImage.Format.Format_RGB32
        )
        pixels = memoryview(prepared.constBits()).cast("B")
        for _index in range(repeat_count):
            if self._cancelled():
                return
            try:
                self._endpoint.write(pixels, self._cancelled)
            except CanvasPipeCancelledError:
                return
            except _ReaderClosedError:
                self._reader_closed = True
                return
            self._frame_count += 1
            self.last_activity = monotonic()
