"""Stream opaque or alpha-preserving Canvas frames into lossless video."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from math import floor
from pathlib import Path
import subprocess
import threading

from PySide6.QtGui import QImage

from app.renderer.bounded_pipeline import (
    BoundedExportPipeline,
    ExportPipelineCancelledError,
    ExportPipelineError,
)
from app.utils.subprocess_utils import hidden_process_kwargs


# Colour streams are visually lossless, not bit-exact: at 720p30, CRF 12 keeps
# PSNR 46-56 dB (far above the final H.264/HEVC pass) while cutting the
# intermediate 40% (grainy moving photos, 44 -> 26 GB per 15 min) to 70% (clean
# graphics) at the same ultrafast speed. Alpha stays lossless ffv1 so edges
# never shift.
INTERMEDIATE_CRF = "12"


class StaticVideoStreamError(RuntimeError):
    """Raised when the lossless Canvas stream cannot be encoded."""


@dataclass(frozen=True, slots=True)
class StaticVideoStreamResult:
    path: Path
    duration_seconds: float
    width: int
    height: int
    fps: int
    has_alpha_stream: bool
    frame_count: int
    peak_buffered_frames: int


@dataclass(frozen=True, slots=True)
class DirectVideoEncodingProfile:
    """Encode an opaque Canvas stream directly with the selected final codec."""

    width: int
    height: int
    codec: str
    arguments: tuple[str, ...] = ()
    pixel_format: str = "yuv420p"


@dataclass(frozen=True, slots=True)
class _QueuedCanvasFrame:
    image: QImage
    duration_seconds: float


class StaticVideoStreamEncoder:
    """Encode planned Canvas states while retaining only a bounded frame queue."""

    def __init__(
        self,
        executable: Path,
        output_path: Path,
        fps: int,
        *,
        queue_capacity: int = 3,
        preserve_alpha: bool = False,
        producer_cancel_event: threading.Event | None = None,
        producer_wait_callback: Callable[[], None] | None = None,
        direct_profile: DirectVideoEncodingProfile | None = None,
    ) -> None:
        if fps <= 0:
            raise ValueError("Static video stream FPS must be greater than zero.")
        self.executable = Path(executable)
        self.output_path = Path(output_path)
        self.fps = fps
        self.queue_capacity = queue_capacity
        self.preserve_alpha = preserve_alpha
        self.producer_cancel_event = producer_cancel_event
        self.producer_wait_callback = producer_wait_callback
        if preserve_alpha and direct_profile is not None:
            raise ValueError("A transparent stream cannot use direct final encoding.")
        if direct_profile is not None and (
            direct_profile.width <= 0 or direct_profile.height <= 0
            or not direct_profile.codec.strip()
        ):
            raise ValueError("Direct video encoding requires valid output settings.")
        self.direct_profile = direct_profile
        self._pipeline: BoundedExportPipeline[_QueuedCanvasFrame] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._stderr_thread: threading.Thread | None = None
        self._process_stop_lock = threading.Lock()
        self._width = 0
        self._height = 0
        self._duration_seconds = 0.0
        self._frame_count = 0
        self._finished = False

    def submit(self, image: QImage, duration_seconds: float) -> None:
        """Queue one state; pixel conversion and FFmpeg writes run off-thread."""
        if self._finished:
            raise StaticVideoStreamError("Static video stream has already finished.")
        if image.isNull():
            raise StaticVideoStreamError("Cannot stream an empty Canvas frame.")
        if duration_seconds <= 0.0:
            raise StaticVideoStreamError("Canvas frame duration must be greater than zero.")
        if self._pipeline is None:
            self._start(image.width(), image.height())
        if image.width() != self._width or image.height() != self._height:
            raise StaticVideoStreamError(
                "All streamed Canvas frames must use one resolution."
            )
        assert self._pipeline is not None
        try:
            self._pipeline.submit(
                _QueuedCanvasFrame(QImage(image), duration_seconds),
                producer_cancel_event=self.producer_cancel_event,
                producer_wait_callback=self.producer_wait_callback,
            )
        except ExportPipelineCancelledError as error:
            self.cancel()
            raise StaticVideoStreamError("Static Canvas streaming was cancelled.") from error
        except ExportPipelineError as error:
            self.cancel()
            raise StaticVideoStreamError(str(error)) from error

    @property
    def frame_count(self) -> int:
        """Return the number of CFR frames already written to FFmpeg."""
        return self._frame_count

    def finish(self) -> StaticVideoStreamResult:
        """Drain the queue, close FFmpeg stdin, and validate the intermediate file."""
        if self._finished:
            raise StaticVideoStreamError("Static video stream has already finished.")
        self._finished = True
        if self._pipeline is None or self._process is None:
            raise StaticVideoStreamError("No Canvas frames were submitted for streaming.")
        pipeline = self._pipeline
        try:
            # A single coalesced Canvas state can represent several minutes of
            # output.  The consumer still has to repeat and encode those CFR
            # frames, so an unconditional join here made the Qt main thread look
            # frozen until the entire stream drained.  Reuse the producer wait
            # callback while draining and while FFmpeg flushes its encoder.
            pipeline.finish(wait_callback=self.producer_wait_callback)
            assert self._process.stdin is not None
            self._process.stdin.close()
            while True:
                if (
                    self.producer_cancel_event is not None
                    and self.producer_cancel_event.is_set()
                ):
                    self.cancel()
                    raise StaticVideoStreamError(
                        "Static Canvas streaming was cancelled."
                    )
                try:
                    return_code = self._process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    if self.producer_wait_callback is not None:
                        self.producer_wait_callback()
            self._join_stderr_thread()
            self._close_process_pipes()
            if return_code != 0:
                raise StaticVideoStreamError(
                    "".join(self._stderr_lines).strip()
                    or "Could not encode the lossless Canvas stream."
                )
            if not self.output_path.is_file():
                raise StaticVideoStreamError(
                    "FFmpeg did not create the lossless Canvas stream."
                )
        except ExportPipelineError as error:
            self.cancel()
            raise StaticVideoStreamError(str(error)) from error
        except (BrokenPipeError, OSError) as error:
            self.cancel()
            raise StaticVideoStreamError(
                "Could not finalize the lossless Canvas stream."
            ) from error
        return StaticVideoStreamResult(
            self.output_path,
            self._duration_seconds,
            self.direct_profile.width if self.direct_profile else self._width,
            self.direct_profile.height if self.direct_profile else self._height,
            self.fps,
            self.preserve_alpha,
            self._frame_count,
            pipeline.peak_buffered_items,
        )

    def cancel(self) -> None:
        """Stop queue activity and terminate the intermediate FFmpeg process."""
        self._finished = True
        pipeline = self._pipeline
        if pipeline is not None:
            pipeline.cancel()
        self._stop_process()
        if pipeline is not None:
            try:
                pipeline.finish(timeout_seconds=5.0)
            except ExportPipelineError:
                pass
        self._join_stderr_thread()

    def _start(self, width: int, height: int) -> None:
        self._width = width
        self._height = height
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.executable),
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", (
                "rgba" if self.preserve_alpha else "bgra"
            ),
            "-video_size", f"{width}x{height}",
            "-framerate", str(self.fps), "-i", "pipe:0",
        ]
        if self.direct_profile is not None:
            profile = self.direct_profile
            command.extend([
                "-vf",
                (
                    f"fps={self.fps},scale={profile.width}:{profile.height}:"
                    "force_original_aspect_ratio=decrease,"
                    f"pad={profile.width}:{profile.height}:"
                    "(ow-iw)/2:(oh-ih)/2:color=black"
                ),
                "-fps_mode", "cfr", "-r", str(self.fps),
                "-an", "-c:v", profile.codec,
                *profile.arguments,
                "-pix_fmt", profile.pixel_format,
            ])
        elif self.preserve_alpha:
            command.extend([
                "-filter_complex",
                "[0:v]split=2[color][withalpha];"
                "[color]format=bgr0[colorout];"
                "[withalpha]alphaextract,format=gray[alphaout]",
                "-map", "[colorout]",
                "-map", "[alphaout]",
                "-an",
                "-c:v:0", "libx264rgb", "-preset:v:0", "ultrafast",
                "-crf:v:0", INTERMEDIATE_CRF, "-pix_fmt:v:0", "bgr0",
                "-c:v:1", "ffv1", "-level:v:1", "3",
                "-coder:v:1", "1", "-pix_fmt:v:1", "gray",
            ])
        else:
            command.extend([
                "-an", "-c:v", "libx264rgb", "-preset", "ultrafast",
                "-crf", INTERMEDIATE_CRF, "-pix_fmt", "bgr0",
            ])
        command.extend(["-f", "matroska", str(self.output_path)])
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **hidden_process_kwargs(),
            )
        except OSError as error:
            raise StaticVideoStreamError(
                f"Could not start lossless Canvas streaming: {error}"
            ) from error
        assert self._process.stderr is not None
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="static-video-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        self._pipeline = BoundedExportPipeline(
            self._write_frame,
            capacity=self.queue_capacity,
            name="static-video-writer",
            on_cancel=self._stop_process,
        )
        self._pipeline.start()

    def _write_frame(self, frame: _QueuedCanvasFrame) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise StaticVideoStreamError("Lossless Canvas encoder is not running.")
        self._duration_seconds += frame.duration_seconds
        target_count = max(1, floor(self._duration_seconds * self.fps + 0.5))
        repeat_count = max(0, target_count - self._frame_count)
        if repeat_count == 0:
            return
        # Format conversion can be a significant per-frame cost at 4K. Keep it
        # in the bounded consumer so the Canvas thread can capture the next state.
        prepared = frame.image.convertToFormat(
            QImage.Format.Format_RGBA8888
            if self.preserve_alpha else QImage.Format.Format_RGB32
        )
        pixels = prepared.constBits()
        pipeline = self._pipeline
        cancel_event = self.producer_cancel_event
        for _index in range(repeat_count):
            # A single coalesced Canvas state can expand to tens of thousands of
            # output frames. Without this check a cancel during that expansion
            # kept feeding FFmpeg — the intermediate file and disk use kept
            # growing for seconds after the user pressed Cancel.
            if (cancel_event is not None and cancel_event.is_set()) or (
                pipeline is not None and pipeline.cancel_event.is_set()
            ):
                return
            process.stdin.write(pixels)
            # Publish progress incrementally.  A coalesced still can represent
            # tens of thousands of output frames, and updating only after the
            # whole run made the preparation UI appear permanently stalled.
            self._frame_count += 1

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw_line in iter(process.stderr.readline, b""):
            self._stderr_lines.append(raw_line.decode("utf-8", "replace"))

    def _stop_process(self) -> None:
        with self._process_stop_lock:
            process = self._process
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=3.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                except OSError:
                    pass
            self._close_process_pipes()

    def _join_stderr_thread(self) -> None:
        thread = self._stderr_thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=1.0)

    def _close_process_pipes(self) -> None:
        process = self._process
        if process is None:
            return
        for pipe in (process.stdin, process.stderr):
            if pipe is None or getattr(pipe, "closed", False):
                continue
            try:
                pipe.close()
            except OSError:
                pass
