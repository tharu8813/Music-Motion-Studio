"""Interruptible loopback HTTP response reads for local lyric models.

Unlike closing a socket from a second thread, select-based polling works across
Windows and Unix. Only a model request's I/O thread reads the TCP socket.
"""
from __future__ import annotations

from http.client import HTTPConnection, HTTPResponse
from io import BufferedReader, RawIOBase
from select import select
from threading import Event
from urllib.request import HTTPHandler


class CancelableLocalHTTPHandler(HTTPHandler):
    """Check the cancellation event before every local HTTP socket read."""

    def __init__(self, cancel: Event):
        super().__init__()
        self.cancel = cancel

    def http_open(self, request):
        cancel = self.cancel

        class CancelableSocketReader(RawIOBase):
            def __init__(self, sock, keepalive):
                super().__init__()
                self.sock = sock
                # HTTPConnection closes its socket handle for responses whose
                # headers say Connection: close. Retain the original makefile
                # reference so the underlying FD remains readable until EOF.
                self.keepalive = keepalive

            def readable(self):
                return True

            def readinto(self, buffer):
                while True:
                    if cancel.is_set():
                        # request_json converts this to TranslationCancelled.
                        raise InterruptedError("Local translation cancelled")
                    readable, _, _ = select([self.sock], [], [], 0.1)
                    if not readable:
                        continue
                    try:
                        return self.sock.recv_into(buffer)
                    except OSError:
                        if cancel.is_set():
                            raise InterruptedError("Local translation cancelled") from None
                        raise

            def close(self):
                try:
                    super().close()
                finally:
                    self.keepalive.close()

        class CancelableHTTPResponse(HTTPResponse):
            def __init__(self, sock, *args, **kwargs):
                super().__init__(sock, *args, **kwargs)
                # Retain http.client's original makefile as the socket's
                # lifetime guard, but use our cancellable buffered reader.
                self.fp = BufferedReader(CancelableSocketReader(sock, self.fp))

        class CancelableHTTPConnection(HTTPConnection):
            response_class = CancelableHTTPResponse

        return self.do_open(CancelableHTTPConnection, request)
