"""Bounded production Qt BGRA -> Rust NVENC frame-stream transport.

The actual Canvas/visualizer producer passes a LAZY iterable of complete
NativeCanvasFrame objects. This adapter stages at most one frame and
waits for the native "MMS_FRAME_ACCEPTED" acknowledgement before unlinking
its .bgra planes. The native worker owns persistent D3D11/NVENC resources;
the finalizer reuses the application's existing audio + MP4 mux path.

It is intentionally not selected automatically by the Export dialog yet.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import tempfile
from threading import Event, Thread
from time import monotonic

from app.preview.native_canvas_layer_packets import NativeCanvasFrame, save_native_canvas_frame
from app.renderer.ffmpeg_renderer import (
    ExportMetadata, FFmpegRenderer, RenderCancelledError, RenderError,
    RenderResult, RenderSettings,
)
from app.renderer.native_gpu_export import finalize_native_gpu_mp4


def _abort_worker(worker: subprocess.Popen) -> None:
    if worker.poll() is not None:
        return
    worker.terminate()
    try:
        worker.wait(timeout=4)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=4)


def _frame_acknowledgement(
    events: Queue[str | None], worker: subprocess.Popen, index: int,
    cancel: Event, timeout: float,
) -> None:
    until = monotonic() + timeout
    expected = f"MMS_FRAME_ACCEPTED {index}"
    while True:
        if cancel.is_set():
            raise RenderCancelledError("Native GPU export was cancelled.")
        if monotonic() >= until:
            raise RenderError(f"Timed out waiting for native GPU frame {index} acceptance.")
        try:
            value = events.get(timeout=min(0.2, max(0.01, until - monotonic())))
        except Empty:
            if worker.poll() is not None:
                raise RenderError(f"Native GPU encoder stopped before accepting frame {index}.")
            continue
        if value is None:
            raise RenderError(f"Native GPU encoder closed its frame acceptance channel at {index}.")
        if value.rstrip("\r\n") != expected:
            raise RenderError(f"Unexpected native GPU frame acknowledgement: {value[:150]!r}")
        return


def stream_native_canvas_h264(
    frames: Iterable[NativeCanvasFrame], *,
    executable: Path, video_path: Path, report_path: Path,
    width: int, height: int, fps: int, duration_seconds: float,
    expected_frames: int, slots: int = 2, cancel_event: Event | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    acknowledgement_timeout: float = 90.0,
) -> dict:
    """Stream every production CFR frame with bounded disk and CPU memory.

    Frames are never gathered into a list. Each frame's straight-BGRA
    files are deleted after *actual native texture submission* and before
    the next frame is requested. A crashed/killed worker can leave a
    temporary .h264.part file; the caller must not treat it as a result.
    """
    cancel = cancel_event or Event()
    exe = Path(executable)
    video = Path(video_path)
    report = Path(report_path)
    if cancel.is_set():
        raise RenderCancelledError("Native GPU export was cancelled.")
    if not exe.is_file() or exe.is_symlink():
        raise RenderError("The installed native D3D11 NVENC export executable is missing.")
    if os.name != "nt":
        raise RenderError("Native D3D11 GPU export requires Windows.")
    if not (1 <= fps <= 240 and 64 <= width <= 3840 and 64 <= height <= 2160
            and width % 2 == 0 and height % 2 == 0 and 2 <= slots <= 8
            and isinstance(expected_frames, int)
            and isinstance(duration_seconds, (int, float))
            and math.isfinite(duration_seconds) and duration_seconds > 0
            and math.ceil(duration_seconds * fps - 1e-9) == expected_frames):
        raise RenderError("Native GPU export settings do not match its complete CFR timeline.")
    if video.suffix.lower() != ".h264" or report.suffix.lower() != ".json":
        raise RenderError("Native export requires .h264 and .json output paths.")
    if video.exists() or report.exists() or video == report:
        raise RenderError("Native export refuses to replace existing output or report.")
    with tempfile.TemporaryDirectory(prefix="mms-native-frame-spool-") as scratch:
        directory = Path(scratch)
        stderr_path = directory / "native-stderr.log"
        args = [
            str(exe), "--width", str(width), "--height", str(height),
            "--fps", str(fps), "--expected-frames", str(expected_frames),
            "--duration-seconds", format(duration_seconds, ".12g"),
            "--slots", str(slots), "--video-output", str(video),
            "--output", str(report),
        ]
        log = stderr_path.open("wb")
        try:
            worker = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log, text=True, encoding="utf-8", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log.close()
            raise RenderError(f"Could not launch native GPU exporter: {exc}") from exc
        events: Queue[str | None] = Queue()

        def collect_stdout() -> None:
            try:
                assert worker.stdout is not None
                for line in worker.stdout:
                    events.put(line)
            finally:
                events.put(None)

        Thread(target=collect_stdout, daemon=True, name="mms-native-gpu-output").start()
        try:
            source = iter(frames)
            assert worker.stdin is not None
            for index in range(expected_frames):
                if cancel.is_set():
                    raise RenderCancelledError("Native GPU export was cancelled.")
                try:
                    frame = next(source)
                except StopIteration as exc:
                    raise RenderError(
                        f"Canvas frame producer stopped at {index}/{expected_frames}"
                    ) from exc
                if not isinstance(frame, NativeCanvasFrame) or (
                    frame.width, frame.height
                ) != (width, height):
                    raise RenderError(f"Native Canvas frame {index} has wrong geometry or type.")
                prefix = f"native-frame-{index:08d}"
                manifest = save_native_canvas_frame(frame, directory, stem=prefix)
                del frame
                try:
                    worker.stdin.write(str(manifest) + "\n")
                    worker.stdin.flush()
                    _frame_acknowledgement(
                        events, worker, index, cancel, acknowledgement_timeout,
                    )
                except (OSError, BrokenPipeError) as exc:
                    raise RenderError(
                        f"Native D3D11/NVENC channel failed while submitting frame {index}."
                    ) from exc
                finally:
                    # Only remove after an ACK on success; if the process
                    # failed, terminating it below makes file deletion safe.
                    if worker.poll() is None and not cancel.is_set():
                        pass
                for path in directory.glob(prefix + "*"):
                    path.unlink(missing_ok=True)
                if progress_callback is not None:
                    progress_callback(index + 1, expected_frames)
            try:
                next(source)
            except StopIteration:
                pass
            else:
                raise RenderError("Canvas producer generated more frames than expected.")
            worker.stdin.close()
            until = monotonic() + 180.0
            while worker.poll() is None:
                if cancel.is_set():
                    raise RenderCancelledError("Native GPU export was cancelled.")
                if monotonic() >= until:
                    raise RenderError("Native GPU worker did not complete after end of frames.")
                cancel.wait(0.1)
            if worker.returncode:
                raise RenderError(
                    "Native GPU export failed: "
                    + stderr_path.read_text(encoding="utf-8", errors="replace")[-2200:]
                )
            if not video.is_file() or not report.is_file():
                raise RenderError("Native GPU encoder exited without both final outputs.")
            try:
                result = json.loads(report.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise RenderError("Native GPU encoder wrote an unreadable final report.") from exc
            if (result.get("status") != "measured"
                    or result.get("production_export_scope") != "production_complete_playlist"
                    or result.get("frames_encoded") != expected_frames):
                raise RenderError("Native GPU producer did not complete the entire timeline.")
            return result
        finally:
            _abort_worker(worker)
            log.close()


def render_native_gpu_playlist(
    renderer: FFmpegRenderer, frames: Iterable[NativeCanvasFrame], *,
    executable: Path, tracks: list, compiled_plan, prepared_audio_path: Path,
    output_path: Path, settings: RenderSettings, metadata: ExportMetadata | None = None,
    cancel_event: Event | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> RenderResult:
    """Full native production hand-off: lazy frames -> H264 -> final AAC MP4.

    The Export dialog should call this only after its existing renderer
    has supplied full-timeline frames in original Z order.
    """
    count = math.ceil(compiled_plan.duration_seconds * settings.fps - 1e-9)
    with tempfile.TemporaryDirectory(prefix="mms-native-complete-") as staging:
        root = Path(staging)
        video = root / "complete.h264"
        report = root / "complete.json"
        stream_native_canvas_h264(
            frames, executable=executable, video_path=video, report_path=report,
            width=settings.output_width, height=settings.output_height,
            fps=settings.fps, duration_seconds=compiled_plan.duration_seconds,
            expected_frames=count, cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        ffprobe = Path(renderer.executable).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe",
        )
        return finalize_native_gpu_mp4(
            renderer, h264_path=video, gpu_report_path=report,
            prepared_audio_path=prepared_audio_path, tracks=tracks,
            compiled_plan=compiled_plan, output_path=output_path,
            settings=settings, ffprobe=ffprobe,
            metadata=metadata, cancel_event=cancel_event,
        )
