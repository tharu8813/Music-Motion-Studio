"""Small, bounded-output subprocess runner for export metadata probes."""
from __future__ import annotations

from concurrent.futures import CancelledError
import subprocess
from threading import Event, Thread
from time import monotonic

from app.utils.subprocess_utils import hidden_process_kwargs


def run_probe(arguments: list[str], *, cancel_event: Event, timeout: float = 180,
              output_limit: int = 65536) -> subprocess.CompletedProcess:
    """Drain both pipes, retain bounded tails, and reap on cancellation/timeout."""
    if cancel_event.is_set():
        raise CancelledError()
    buffers = [bytearray(), bytearray()]

    def drain(pipe, buffer):
        with pipe:
            while chunk := pipe.read(4096):
                buffer.extend(chunk)
                if len(buffer) > output_limit:
                    del buffer[:-output_limit]

    with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          **hidden_process_kwargs()) as process:
        readers = [Thread(target=drain, args=(pipe, buffer), daemon=True)
                   for pipe, buffer in zip((process.stdout, process.stderr), buffers)]
        for reader in readers:
            reader.start()
        try:
            deadline = monotonic() + timeout
            while process.poll() is None:
                if cancel_event.is_set():
                    raise CancelledError()
                if monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(arguments, timeout)
                cancel_event.wait(0.05)
            if cancel_event.is_set():
                raise CancelledError()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for reader in readers:
                reader.join()
        return subprocess.CompletedProcess(arguments, process.returncode,
                                           bytes(buffers[0]), bytes(buffers[1]))
