"""Pure localized preview video-proxy status for GUI performance labels.

This module must not import Qt or touch decoding; the owning dialog supplies
its current queue/cache state. Priority is pending > active > failed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def format_video_proxy_status(
    *, paths: Mapping[str, str], queued_count: int,
    worker_active: bool, has_failures: bool, korean: bool,
) -> str:
    """Describe proxy preparation or fallback without media path disclosure."""
    active = sum(
        1 for original, preview in paths.items()
        if os.path.normcase(original) != os.path.normcase(preview)
    )
    pending = queued_count + int(worker_active)
    if pending:
        return f" · 프록시 준비 {pending}" if korean else f" · Preparing proxies {pending}"
    if active:
        return f" · 프록시 {active}" if korean else f" · Proxies {active}"
    if has_failures:
        return " · 원본 사용" if korean else " · Original media"
    return ""
