"""Preview video-proxy progress labels retain localized status precedence."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.dialogs.export_preview_dialog import ExportPreviewDialog
from app.preview.proxy_status import format_video_proxy_status


class PreviewProxyStatusTests(unittest.TestCase):
    def test_pending_precedes_active_and_failed_in_both_languages(self):
        for korean, expected in ((True, " · 프록시 준비 3"), (False, " · Preparing proxies 3")):
            with self.subTest(korean=korean):
                self.assertEqual(format_video_proxy_status(
                    paths={"original.mp4": "preview.mp4"}, queued_count=2,
                    worker_active=True, has_failures=True, korean=korean,
                ), expected)

    def test_successful_proxy_count_and_original_fallback(self):
        self.assertEqual(format_video_proxy_status(
            paths={"a.mp4": "a-proxy.mp4", "b.mp4": "b-proxy.mp4"},
            queued_count=0, worker_active=False, has_failures=False, korean=False,
        ), " · Proxies 2")
        for korean, text in ((True, " · 원본 사용"), (False, " · Original media")):
            self.assertEqual(format_video_proxy_status(
                paths={"a.mp4": "a.mp4"}, queued_count=0,
                worker_active=False, has_failures=True, korean=korean,
            ), text)
        self.assertEqual(format_video_proxy_status(
            paths={}, queued_count=0, worker_active=False,
            has_failures=False, korean=True,
        ), "")

    def test_same_original_and_preview_path_does_not_count_as_proxy(self):
        self.assertEqual(format_video_proxy_status(
            paths={"C:/video/a.mp4": "C:/video/a.mp4"},
            queued_count=0, worker_active=False, has_failures=False, korean=False,
        ), "")

    def test_existing_dialog_proxy_label_wrapper_uses_shared_logic(self):
        class PreviewState:
            _video_proxy_paths = {"original.mp4": "proxy.mp4"}
            _video_proxy_queue = ["waiting.mp4"]
            _video_proxy_worker = object()
            _video_proxy_failures = {"bad.mp4": "failed"}

        state = PreviewState()
        self.assertEqual(ExportPreviewDialog._video_proxy_status_text(state, True),
                         " · 프록시 준비 2")
        state._video_proxy_worker = None
        state._video_proxy_queue = []
        self.assertEqual(ExportPreviewDialog._video_proxy_status_text(state, False),
                         " · Proxies 1")


if __name__ == "__main__":
    unittest.main()
