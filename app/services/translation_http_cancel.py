"""Abort in-flight loopback HTTP reads when translation is cancelled.

Only used for locally hosted models over HTTP, never for remote HTTPS.
Shutting down the connected socket interrupts blocking response reads without
launching a second (potentially billable) request or leaving a worker behind.
"""
from __future__ import annotations

from http.client import HTTPConnection
from socket import SHUT_RDWR
from threading import Event, Thread
from urllib.request import HTTPHandler


class CancelableLocalHTTPHandler(HTTPHandler):
    """Drop the current local model socket when its cancellation event is set."""

    def __init__(self, cancel: Event, finished: Event):
        super().__init__()
        self.cancel = cancel
        self.finished = finished

    def http_open(self, request):
        cancel, finished = self.cancel, self.finished

        class CancelableConnection(HTTPConnection):
            def connect(self):
                super().connect()
                sock = self.sock

                def watch_cancel():
                    # The HTTP response may take 240 seconds to finish, but a
                    # cancellation should not wait for the socket read timeout.
                    while not finished.is_set():
                        if cancel.wait(0.05):
                            if finished.is_set():
                                return
                            try:
                                sock.shutdown(SHUT_RDWR)
                            except OSError:
                                pass
                            try:
                                sock.close()
                            except OSError:
                                pass
                            return

                Thread(target=watch_cancel, daemon=True,
                       name="local-lyrics-http-cancel").start()

        return self.do_open(CancelableConnection, request)
