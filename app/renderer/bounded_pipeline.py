"""Bounded producer-consumer foundation for streamed export frames."""

from __future__ import annotations

from collections.abc import Callable
from queue import Empty, Full, Queue
import threading
from time import monotonic
from typing import Generic, TypeVar


T = TypeVar("T")
_END_OF_STREAM = object()


class ExportPipelineError(RuntimeError):
    """Base error raised by a bounded export pipeline."""


class ExportPipelineClosedError(ExportPipelineError):
    """Raised when a producer submits after the input side was closed."""


class ExportPipelineCancelledError(ExportPipelineError):
    """Raised when queued export work was cancelled cooperatively."""


class ExportPipelineConsumerError(ExportPipelineError):
    """Wrap an exception raised by the consumer thread."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"Export pipeline consumer failed: {cause}")
        self.cause = cause


class ExportPipelineTimeoutError(ExportPipelineError):
    """Raised when a consumer does not stop within the requested timeout."""


class BoundedExportPipeline(Generic[T]):
    """Deliver ordered items to one consumer with strictly bounded buffering.

    The producer may run on the Qt UI thread while the consumer writes frames to
    FFmpeg on a worker thread. No more than ``capacity`` pending items are retained.
    Consumer failures and cancellation are checked while a producer is waiting for
    queue space, preventing an indefinitely blocked export preparation loop.
    """

    def __init__(
        self,
        consumer: Callable[[T], None],
        *,
        capacity: int,
        name: str = "export-frame-consumer",
        on_cancel: Callable[[], None] | None = None,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if capacity <= 0:
            raise ValueError("Export pipeline capacity must be greater than zero.")
        if poll_interval_seconds <= 0.0:
            raise ValueError("Export pipeline poll interval must be greater than zero.")
        self._consumer = consumer
        self._on_cancel = on_cancel
        self._capacity = capacity
        self._poll_interval = poll_interval_seconds
        self._queue: Queue[object] = Queue(maxsize=capacity)
        self._cancel_event = threading.Event()
        self._consumer_done = threading.Event()
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._consume,
            name=name,
            daemon=True,
        )
        self._started = False
        self._input_closed = False
        self._cancel_callback_called = False
        self._failure: BaseException | None = None
        self._peak_buffered_items = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @property
    def peak_buffered_items(self) -> int:
        with self._state_lock:
            return self._peak_buffered_items

    @property
    def cancel_event(self) -> threading.Event:
        """Expose the shared event to cooperative FFmpeg consumer code."""
        return self._cancel_event

    def start(self) -> None:
        """Start the single consumer thread exactly once."""
        with self._state_lock:
            if self._started:
                raise ExportPipelineError("Export pipeline has already been started.")
            if self._input_closed:
                raise ExportPipelineClosedError("Export pipeline input is closed.")
            self._started = True
        self._thread.start()

    def submit(
        self,
        item: T,
        *,
        producer_cancel_event: threading.Event | None = None,
        producer_wait_callback: Callable[[], None] | None = None,
    ) -> None:
        """Queue one item, applying backpressure when the bounded queue is full."""
        self._ensure_can_submit()
        while True:
            if producer_cancel_event is not None and producer_cancel_event.is_set():
                self.cancel()
            self._raise_terminal_state()
            try:
                self._queue.put(item, timeout=self._poll_interval)
            except Full:
                if producer_wait_callback is not None:
                    producer_wait_callback()
                continue
            with self._state_lock:
                self._peak_buffered_items = max(
                    self._peak_buffered_items,
                    self._queue.qsize(),
                )
            # Cancellation or a concurrent consumer failure wins over a late
            # successful put. The queued item is never reported as accepted.
            if producer_cancel_event is not None and producer_cancel_event.is_set():
                self.cancel()
            self._raise_terminal_state()
            return

    def finish(
        self, timeout_seconds: float | None = None, *,
        wait_callback: Callable[[], None] | None = None,
    ) -> None:
        """Close producer input, drain queued items, and propagate consumer state."""
        with self._state_lock:
            if not self._started:
                raise ExportPipelineError("Export pipeline has not been started.")
            already_closed = self._input_closed
            self._input_closed = True
        if not already_closed and not self._cancel_event.is_set():
            self._enqueue_end_of_stream(wait_callback)
        deadline = (
            monotonic() + timeout_seconds
            if timeout_seconds is not None else None
        )
        while self._thread.is_alive():
            remaining = (
                None if deadline is None else max(0.0, deadline - monotonic())
            )
            if remaining == 0.0:
                break
            self._thread.join(
                min(self._poll_interval, remaining)
                if remaining is not None else self._poll_interval
            )
            if wait_callback is not None and self._thread.is_alive():
                wait_callback()
        if self._thread.is_alive():
            raise ExportPipelineTimeoutError(
                "Export pipeline consumer did not stop before the timeout."
            )
        self._raise_terminal_state()

    def cancel(self) -> None:
        """Stop accepting input and request cooperative consumer shutdown."""
        callback: Callable[[], None] | None = None
        with self._state_lock:
            self._input_closed = True
            if not self._cancel_event.is_set():
                self._cancel_event.set()
            if not self._cancel_callback_called:
                self._cancel_callback_called = True
                callback = self._on_cancel
        if callback is not None:
            try:
                callback()
            except BaseException as error:
                self._record_failure(error)

    def _ensure_can_submit(self) -> None:
        with self._state_lock:
            if not self._started:
                raise ExportPipelineError("Export pipeline has not been started.")
            if self._input_closed:
                if self._cancel_event.is_set():
                    raise ExportPipelineCancelledError(
                        "Export pipeline was cancelled."
                    )
                raise ExportPipelineClosedError("Export pipeline input is closed.")

    def _enqueue_end_of_stream(
        self, wait_callback: Callable[[], None] | None = None,
    ) -> None:
        while True:
            self._raise_terminal_state()
            if self._consumer_done.is_set():
                self._raise_terminal_state()
                return
            try:
                self._queue.put(_END_OF_STREAM, timeout=self._poll_interval)
            except Full:
                # The producer can be the Qt main thread. A slow consumer may
                # keep the bounded queue full while finish() is trying to append
                # its sentinel, so pump the same UI/cancellation callback here
                # as in the subsequent thread-join loop.
                if wait_callback is not None:
                    wait_callback()
                continue
            return

    def _consume(self) -> None:
        try:
            while not self._cancel_event.is_set():
                try:
                    item = self._queue.get(timeout=self._poll_interval)
                except Empty:
                    continue
                try:
                    if item is _END_OF_STREAM:
                        return
                    self._consumer(item)  # type: ignore[arg-type]
                finally:
                    self._queue.task_done()
                # Do not retain the previous image while waiting for more input.
                del item
        except BaseException as error:
            self._record_failure(error)
        finally:
            self._consumer_done.set()

    def _record_failure(self, error: BaseException) -> None:
        with self._state_lock:
            if self._failure is None:
                self._failure = error
            self._input_closed = True

    def _raise_terminal_state(self) -> None:
        with self._state_lock:
            failure = self._failure
            cancelled = self._cancel_event.is_set()
        if failure is not None:
            raise ExportPipelineConsumerError(failure) from failure
        if cancelled:
            raise ExportPipelineCancelledError("Export pipeline was cancelled.")
