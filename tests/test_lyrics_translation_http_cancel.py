"""Real loopback-server tests for stopping pending Ollama/LM Studio HTTP reads."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from unittest.mock import Mock, MagicMock, patch

from app.services.lyrics_translation_service import (
    TranslationCancelled, request_json,
)


class CancelableLocalHTTPTests(unittest.TestCase):
    def make_server(self, *, hold_headers=False):
        started, release = threading.Event(), threading.Event()
        output = json.dumps({"ok": True}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                started.set()
                if hold_headers:
                    release.wait(5)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(output)))
                    self.end_headers()
                    self.wfile.flush()
                    if not hold_headers:
                        release.wait(5)
                    self.wfile.write(output)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (release.set(), server.shutdown(),
                                 server.server_close(), thread.join(2)))
        url = f"http://127.0.0.1:{server.server_address[1]}/api/chat"
        return url, started, release

    def test_cancel_interrupts_local_http_before_response_headers(self):
        url, started, release = self.make_server(hold_headers=True)
        self.assert_cancelled_fast(url, started, release)

    def test_cancel_interrupts_local_http_response_body_read(self):
        url, started, release = self.make_server(hold_headers=False)
        self.assert_cancelled_fast(url, started, release)

    def assert_cancelled_fast(self, url, started, release):
        cancel, result = threading.Event(), []
        def work():
            try:
                request_json(url, {"model": "test"}, cancel=cancel, timeout=10)
                result.append("unexpected-success")
            except Exception as error:
                result.append(error)
        worker = threading.Thread(target=work)
        worker.start()
        try:
            self.assertTrue(started.wait(2), "Loopback request did not reach the server")
            begin = monotonic()
            cancel.set()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "Cancellation stayed blocked on HTTP I/O")
            self.assertLess(monotonic() - begin, 2)
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], TranslationCancelled)
        finally:
            release.set()
            worker.join(4)

    def test_cancelable_local_http_also_completes_normally(self):
        url, started, release = self.make_server(hold_headers=False)
        release.set()
        self.assertEqual(request_json(url, {"model": "test"}, cancel=threading.Event(),
                                      timeout=10), {"ok": True})
        self.assertTrue(started.is_set())

    def test_remote_https_does_not_install_local_socket_handler(self):
        opener = Mock()
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener.open.return_value = response
        with patch("app.services.lyrics_translation_service.build_opener", return_value=opener) as make:
            self.assertEqual(request_json("https://api.openai.com/v1/responses",
                                          {"model": "test"}, cancel=threading.Event()), {"ok": True})
        self.assertEqual(len(make.call_args.args), 1)


if __name__ == "__main__":
    unittest.main()
