"""Low-resolution video proxies used only by interactive preview playback."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

from PySide6.QtCore import QStandardPaths, QThread, Signal

from app.utils.subprocess_utils import hidden_process_kwargs


LOGGER = logging.getLogger(__name__)


class PreviewProxyError(RuntimeError):
    """Raised when one preview-only proxy cannot be prepared."""


class PreviewProxyCancelled(PreviewProxyError):
    """Raised after preview teardown cancels proxy preparation."""


@dataclass(frozen=True, slots=True)
class PreviewProxySpec:
    """Stable preview limits included in every cache identity."""

    max_width: int = 1280
    max_height: int = 720
    crf: int = 23
    version: int = 2

    @property
    def signature(self) -> str:
        return (
            f"v{self.version}-{self.max_width}x{self.max_height}-"
            f"source-fps-crf{self.crf}"
        )


class PreviewProxyCache:
    """Content-addressed, size-bounded cache that never mutates source media."""

    def __init__(
        self, root: Path | None = None, spec: PreviewProxySpec | None = None,
        maximum_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self.spec = spec or PreviewProxySpec()
        self.root = root or self.default_root()
        self.maximum_bytes = max(64 * 1024 * 1024, int(maximum_bytes))

    @staticmethod
    def default_root() -> Path:
        data_root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        base = Path(data_root) if data_root else Path.cwd() / ".app-data"
        return base / "preview-proxies" / "v1"

    def proxy_path(self, source_path: Path) -> Path:
        source = source_path.resolve()
        stat = source.stat()
        identity = "\0".join((
            os.path.normcase(str(source)),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            self.spec.signature,
        ))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.mp4"

    def cached_path(self, source_path: Path) -> Path | None:
        try:
            candidate = self.proxy_path(source_path)
            if candidate.is_file() and candidate.stat().st_size > 0:
                # File modification time doubles as a cheap LRU access marker.
                candidate.touch(exist_ok=True)
                return candidate
        except OSError:
            return None
        return None

    def temporary_path(self, target: Path) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return target.with_name(f".{target.stem}-{uuid.uuid4().hex}.part.mp4")

    def prune(self, *, protected: set[Path] | None = None) -> None:
        """Remove oldest proxy files until the configured cache budget is met."""
        protected_paths = {
            path.resolve() for path in (protected or set())
        }
        try:
            root = self.root.resolve()
            all_proxies = [
                path for path in root.iterdir()
                if path.is_file() and path.suffix.lower() == ".mp4"
                and not path.name.startswith(".")
            ]
        except OSError:
            return
        entries: list[tuple[int, int, Path]] = []
        total = 0
        for path in all_proxies:
            try:
                stat = path.stat()
            except OSError:
                continue
            total += stat.st_size
            if path.resolve() not in protected_paths:
                entries.append((stat.st_mtime_ns, stat.st_size, path))
        for _mtime, size, path in sorted(entries):
            if total <= self.maximum_bytes:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue


class PreviewProxyWorker(QThread):
    """Probe and prepare one low-priority preview proxy off the GUI thread."""

    ready = Signal(str, str, bool)
    failed = Signal(str, str)
    cancelled = Signal(str)

    def __init__(
        self, ffmpeg_executable: Path, source_path: Path,
        cache: PreviewProxyCache, parent=None,
    ) -> None:
        super().__init__(parent)
        self.ffmpeg_executable = Path(ffmpeg_executable)
        self.source_path = Path(source_path)
        self.cache = cache
        self._cancel_event = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._source_fps = 60.0

    def cancel(self) -> None:
        self._cancel_event.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(self) -> None:
        original = str(self.source_path)
        temporary: Path | None = None
        try:
            if not self.source_path.is_file():
                raise PreviewProxyError(f"Video file does not exist: {self.source_path}")
            cached = self.cache.cached_path(self.source_path)
            if cached is not None:
                self.ready.emit(original, str(cached), True)
                return
            if not self._needs_proxy():
                self.ready.emit(original, original, False)
                return
            target = self.cache.proxy_path(self.source_path)
            temporary = self.cache.temporary_path(target)
            errors: list[str] = []
            for codec_arguments in (
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.cache.spec.crf),
                 "-enc_time_base:v", "demux"],
                # MPEG-4 Part 2 limits the time-base denominator to 65535.
                ["-c:v", "mpeg4", "-q:v", "5", "-enc_time_base:v", "1:60000"],
            ):
                temporary.unlink(missing_ok=True)
                result = self._execute([
                    str(self.ffmpeg_executable), "-hide_banner", "-loglevel", "error",
                    "-nostdin", "-i", str(self.source_path),
                    "-map", "0:v:0", "-an", "-sn", "-dn",
                    "-vf", self._scale_filter(),
                    "-fps_mode", "passthrough",
                    "-filter_threads", "1", "-threads", "2",
                    *codec_arguments,
                    "-pix_fmt", "yuv420p", "-g", str(self._gop_size()),
                    "-keyint_min", str(self._gop_size()),
                    "-sc_threshold", "0", "-map_metadata", "-1",
                    "-movflags", "+faststart", "-y", str(temporary),
                ])
                if (
                    result.returncode == 0 and temporary.is_file()
                    and temporary.stat().st_size > 0
                ):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary.replace(target)
                    self.cache.prune(protected={target})
                    self.ready.emit(original, str(target), True)
                    return
                errors.append(result.stderr.strip())
            raise PreviewProxyError(
                "FFmpeg could not create the preview proxy. "
                + " | ".join(error for error in errors if error)
            )
        except PreviewProxyCancelled:
            self.cancelled.emit(original)
        except Exception as error:
            LOGGER.warning("Preview proxy generation failed for %s", original, exc_info=True)
            self.failed.emit(original, str(error) or error.__class__.__name__)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _scale_filter(self) -> str:
        spec = self.cache.spec
        return (
            f"scale=w='min({spec.max_width},iw)':h='min({spec.max_height},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )

    def _gop_size(self) -> int:
        return max(1, round(self._source_fps))

    def _needs_proxy(self) -> bool:
        probe = self._ffprobe_executable()
        if probe is None:
            return True
        result = self._execute([
            str(probe), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,pix_fmt",
            "-of", "json", str(self.source_path),
        ])
        if result.returncode != 0:
            return True
        try:
            stream = json.loads(result.stdout)["streams"][0]
            width = int(stream["width"])
            height = int(stream["height"])
            fps = float(Fraction(str(stream.get("avg_frame_rate", "0/1"))))
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            return True
        pixel_format = str(stream.get("pix_fmt", "")).lower()
        if self._pixel_format_has_alpha(pixel_format):
            return False
        spec = self.cache.spec
        if fps > 0.0:
            self._source_fps = fps
        return width > spec.max_width or height > spec.max_height

    @staticmethod
    def _pixel_format_has_alpha(pixel_format: str) -> bool:
        return pixel_format.startswith((
            "yuva", "gbrap", "rgba", "bgra", "argb", "abgr", "ya",
        )) or pixel_format == "pal8"

    def _ffprobe_executable(self) -> Path | None:
        sibling_name = (
            "ffprobe.exe"
            if self.ffmpeg_executable.suffix.lower() == ".exe" else "ffprobe"
        )
        sibling = self.ffmpeg_executable.with_name(sibling_name)
        if sibling.is_file():
            return sibling
        discovered = shutil.which(sibling_name) or shutil.which("ffprobe")
        return Path(discovered) if discovered else None

    def _execute(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if self._cancel_event.is_set():
            raise PreviewProxyCancelled("Preview proxy generation was cancelled.")
        process_kwargs = hidden_process_kwargs()
        if os.name == "nt":
            process_kwargs["creationflags"] = int(
                process_kwargs.get("creationflags", 0)
            ) | int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        process = subprocess.Popen(
            arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", **process_kwargs,
        )
        with self._process_lock:
            self._process = process
        try:
            while process.poll() is None:
                if self._cancel_event.wait(0.05):
                    try:
                        process.terminate()
                        process.wait(timeout=1.0)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            process.kill()
                        except OSError:
                            pass
                    raise PreviewProxyCancelled(
                        "Preview proxy generation was cancelled."
                    )
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(
                arguments, int(process.returncode or 0), stdout, stderr,
            )
        finally:
            with self._process_lock:
                self._process = None
