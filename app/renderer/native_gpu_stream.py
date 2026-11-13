"""Production Qt BGRA -> Rust NVENC binary transport, one frame per ACK.

No per-frame manifest or BGRA files are written. The native worker owns
persistent GPU resources; finalization reuses the existing audio/mux path.

The optional one-frame lookahead allows the *caller-owned Qt thread* to paint
frame N+1 while a dedicated I/O thread writes/ACKs frame N. Native frames
remain ordered, and Qt methods never run on that sender thread.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import json
import logging
import math
import mmap
import os
from pathlib import Path
from queue import Empty, Full, Queue
import subprocess
import tempfile
from threading import Event, Thread, Lock
from time import monotonic
from uuid import uuid4

from app.preview.native_canvas_layer_packets import DeferredNativeCanvasFrame, NativeCanvasFrame
from app.renderer.native_frame_transport import (AdaptiveCompressionBudget, FrameReuseState,
                                                 SHARED_PACKET_CAPACITY, write_frame,
                                                 supports_lz4_transport)
from app.renderer.ffmpeg_renderer import (
    ExportMetadata, FFmpegRenderer, RenderCancelledError, RenderError,
    RenderResult, RenderSettings,
)
from app.renderer.native_gpu_export import finalize_native_gpu_mp4
from app.renderer.native_gpu_optimizations import NativeGpuOptimizations
from app.services.video_encoder_service import DEFAULT_NATIVE_NVENC_PRESET, NATIVE_NVENC_PRESETS
from app.utils.cancellable_subprocess import run_probe


_DEFAULT_FRAME_SLOTS = 2


class NativeGpuPreflightError(RenderError):
    """The native worker cannot start an encoder with the selected settings."""


def _encoding_arguments(width, height, fps, slots, preset, cq) -> list[str]:
    if preset not in NATIVE_NVENC_PRESETS or type(cq) is not int or not 0 <= cq <= 51:
        raise NativeGpuPreflightError("Invalid Rust GPU NVENC preset or quality value.")
    args = ["--width", str(width), "--height", str(height), "--fps", str(fps), "--slots", str(slots)]
    if preset != DEFAULT_NATIVE_NVENC_PRESET:
        args.extend(("--nvenc-preset", preset))
    if cq:
        args.extend(("--nvenc-cq", str(cq)))
    return args


def probe_native_gpu(executable: Path, settings: RenderSettings, *, cancel_event: Event,
                     required_canvas_schema: int = 2, capabilities: dict | None = None,
                     video_sources=(), query_radial: bool = False) -> int:
    """Open the requested GPU pipeline and return its Windows adapter identity."""
    arguments = [str(executable), "--probe", "1", *_encoding_arguments(
        settings.output_width, settings.output_height, settings.fps, _DEFAULT_FRAME_SLOTS,
        settings.native_nvenc_preset, settings.native_nvenc_cq,
    )]
    for source in video_sources:
        arguments.extend(("--video-source", str(source.path)))
    try:
        result = run_probe(arguments, cancel_event=cancel_event, timeout=10, output_limit=8192)
    except CancelledError as exc:
        raise RenderCancelledError("Native GPU export was cancelled.") from exc
    except subprocess.TimeoutExpired as exc:
        raise NativeGpuPreflightError("Native GPU compatibility check timed out.") from exc
    except OSError as exc:
        raise NativeGpuPreflightError(f"Could not launch the native GPU compatibility check: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise NativeGpuPreflightError(detail or f"GPU compatibility check exited with code {result.returncode}.")
    try:
        report = json.loads(result.stdout)
        adapter = report["adapter_luid"]
        if report["status"] == "ready" and type(adapter) is int and 0 <= adapter < 2**64:
            if required_canvas_schema not in report.get("canvas_packet_schemas", (1, 2)):
                if required_canvas_schema == 5:
                    raise NativeGpuPreflightError("Native GPU worker needs an update for video decoding.")
                if required_canvas_schema == 4:
                    raise NativeGpuPreflightError("Native GPU worker needs an update for text drawing.")
                raise NativeGpuPreflightError("Native GPU worker needs an update for particle drawing.")
            if capabilities is not None:
                limit = report.get("canvas_layer_limit")
                if type(limit) is int and limit > 0:
                    capabilities["canvas_layer_limit"] = limit
                elif not query_radial or required_canvas_schema >= 4:
                    raise NativeGpuPreflightError("Native GPU worker did not report its Canvas layer limit.")
                if query_radial:
                    capabilities["gpu_radial"] = 6 in report.get("canvas_packet_schemas", ())
            return adapter
    except (ValueError, KeyError, TypeError):
        pass
    raise NativeGpuPreflightError("Native GPU worker returned an invalid compatibility result.")


def _abort_worker(worker: subprocess.Popen) -> None:
    if worker.poll() is not None:
        return
    try:
        worker.terminate()
    except ProcessLookupError:
        return
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
    frames: Iterable[NativeCanvasFrame | DeferredNativeCanvasFrame], *,
    executable: Path, video_path: Path, report_path: Path,
    width: int, height: int, fps: int, duration_seconds: float,
    expected_frames: int, slots: int = _DEFAULT_FRAME_SLOTS, cancel_event: Event | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    acknowledgement_timeout: float = 90.0,
    optimizations: NativeGpuOptimizations | None = None,
    native_nvenc_preset: str = DEFAULT_NATIVE_NVENC_PRESET,
    native_nvenc_cq: int = 0,
    adapter_luid: int | None = None,
    video_sources=(),
    encoder_flush_callback: Callable[[], None] | None = None,
) -> dict:
    """Stream CFR frames with bounded memory and an upload/submission ACK.

    A writer pump allows cancellation even when a worker stops reading stdin.
    The worker is terminated before pipe handles and source frames are released.
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
    encoding_args = _encoding_arguments(width, height, fps, slots, native_nvenc_preset, native_nvenc_cq)
    if not (1 <= fps <= 240 and 64 <= width <= 3840 and 64 <= height <= 2160
            and width % 2 == 0 and height % 2 == 0 and 2 <= slots <= 8
            and isinstance(expected_frames, int)
            and isinstance(duration_seconds, (int, float))
            and math.isfinite(duration_seconds) and 0 < duration_seconds <= 86400
            and math.isfinite(acknowledgement_timeout) and acknowledgement_timeout > 0
            and math.ceil(duration_seconds * fps - 1e-9) == expected_frames):
        raise RenderError("Native GPU export settings do not match its complete CFR timeline.")
    if video.suffix.lower() != ".h264" or report.suffix.lower() != ".json":
        raise RenderError("Native export requires .h264 and .json output paths.")
    if video == report or any(path.exists() or path.is_symlink()
                              for path in (video, report, video.with_suffix(".h264.part"))):
        raise RenderError("Native export refuses to replace existing output or report.")
    with tempfile.TemporaryDirectory(prefix="mms-native-stream-"), ExitStack() as resources:
        stream_started = monotonic()
        reuse_enabled = (
            optimizations.reuse_layers if optimizations is not None
            else os.environ.get(
                "PLAYLIST_CANVAS_NATIVE_LAYER_REUSE", "",
            ).strip().lower() in {"1", "true", "yes"}
        )
        reuse_state = FrameReuseState() if reuse_enabled else None
        # One automatic path: fast per-layer compression if the bundled LZ4
        # wheel is present, otherwise preserve tested binary-v2 semantics.
        compressed_transport = reuse_enabled and supports_lz4_transport()
        input_format = ("binary-v3" if compressed_transport
                        else "binary-v2" if reuse_enabled else "binary-v1")
        args = [
            str(exe), *encoding_args, "--expected-frames", str(expected_frames),
            "--duration-seconds", format(duration_seconds, ".17g"),
            "--video-output", str(video),
            "--output", str(report), "--input-format",
            input_format,
        ]
        if adapter_luid is not None:
            args.extend(("--adapter-luid", str(adapter_luid)))
        for source in video_sources:
            args.extend(("--video-source", str(source.path)))
        shared_packet = None
        if reuse_enabled and optimizations is not None and optimizations.parallel_packet_validation:
            args.extend(("--parallel-packet-validation", "1"))
        if optimizations is not None and optimizations.shared_packet_transport:
            mapping_name = f"Local\\MMS-Canvas-{uuid4().hex}"
            try:
                shared_packet = resources.enter_context(
                    mmap.mmap(-1, SHARED_PACKET_CAPACITY, tagname=mapping_name),
                )
            except OSError:
                logging.getLogger(__name__).warning("Shared Canvas packet mapping unavailable; using the pipe")
            else:
                args.extend(("--shared-packet-map", mapping_name))
        try:
            worker = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise RenderError(f"Could not launch native GPU exporter: {exc}") from exc
        events: Queue[str | None] = Queue(maxsize=2)
        reader_stop = Event()
        error_tail = ""
        error_lock = Lock()

        def collect_stderr() -> None:
            nonlocal error_tail
            assert worker.stderr is not None
            while chunk := worker.stderr.read(1024):
                with error_lock:
                    error_tail = (error_tail + chunk.decode("utf-8", "replace"))[-4096:]

        def enqueue(value: str | None) -> None:
            while not reader_stop.is_set():
                try:
                    events.put(value, timeout=0.1)
                    return
                except Full:
                    pass

        def collect_stdout() -> None:
            try:
                assert worker.stdout is not None
                while not reader_stop.is_set():
                    line = worker.stdout.readline(256)
                    if not line:
                        break
                    enqueue(line.decode("utf-8", "replace"))
            finally:
                enqueue(None)

        output_reader = Thread(target=collect_stdout, daemon=True,
                               name="mms-native-gpu-output")
        output_reader.start()
        error_reader = Thread(target=collect_stderr, daemon=True,
                              name="mms-native-gpu-errors")
        error_reader.start()
        source = None
        sender = None
        transport_bytes = 0
        transport_seconds = 0.0
        frame_production_seconds = 0.0
        frame_production_max_seconds = 0.0
        frame_pipe_write_seconds = 0.0
        cpu_parallel_bgra_frames = 0
        frame_ack_wait_seconds = 0.0
        frame_write_timings: dict[str, float | int | dict] = {}
        # Only immutable BGRA bytes run on this bounded CPU pool. No Qt calls
        # leave the thread that owns each QImage.
        packet_executor = ThreadPoolExecutor(max_workers=3,
                                             thread_name_prefix="mms-native-layer")
        compression_budget = AdaptiveCompressionBudget() if compressed_transport else None

        def send_packet(resolved: NativeCanvasFrame, index: int) -> int:
            # Always emit v3 when negotiated. It can send lossless raw planes
            # after the per-export latency probe rejects expensive compression.
            compress = (compression_budget.use_compression(index)
                        if compression_budget is not None else False)
            raw_before = frame_write_timings.get("raw_source_bytes", 0)
            begun = monotonic()
            written = write_frame(
                worker.stdin, resolved, index, reuse_state=reuse_state,
                timings=frame_write_timings, compress_planes=compress,
                v3_wire=compressed_transport, executor=packet_executor,
                shared_packet=shared_packet,
            )
            if compression_budget is not None:
                compression_budget.record(
                    compress, monotonic() - begun,
                    frame_write_timings.get("raw_source_bytes", 0) - raw_before,
                )
            return written
        # Experimental, rollback-friendly optimization. A frame is never
        # painted on the sender thread, and at most two packets coexist.
        overlap_enabled = (
            optimizations.pipelined if optimizations is not None
            else os.environ.get(
                "PLAYLIST_CANVAS_NATIVE_PIPELINED", "",
            ).strip().lower() in {"1", "true", "yes"}
        )
        completed = False
        pending: tuple[int, Queue, Thread, float] | None = None

        def finish_pending() -> None:
            """Wait for previous native ACK without calling any Qt paint code."""
            nonlocal pending, transport_bytes, transport_seconds
            nonlocal frame_pipe_write_seconds, frame_ack_wait_seconds
            if pending is None:
                return
            accepted_index, completion, running_sender, send_started = pending
            while True:
                if cancel.is_set():
                    raise RenderCancelledError("Native GPU export was cancelled.")
                if monotonic() - send_started >= acknowledgement_timeout:
                    raise RenderError(
                        f"Timed out writing/acknowledging native GPU frame {accepted_index}."
                    )
                try:
                    written = completion.get(timeout=0.1)
                    break
                except Empty:
                    if worker.poll() is not None:
                        raise RenderError("Native GPU worker stopped during frame transfer.")
            if isinstance(written, Exception):
                if isinstance(written, (OSError, ValueError)):
                    raise RenderError(
                        f"Native D3D11/NVENC channel failed while submitting frame {accepted_index}."
                    ) from written
                raise written
            written_bytes, write_elapsed, ack_elapsed, frame_elapsed = written
            running_sender.join()
            transport_bytes += written_bytes
            frame_pipe_write_seconds += write_elapsed
            frame_ack_wait_seconds += ack_elapsed
            transport_seconds += frame_elapsed
            pending = None
            if progress_callback is not None:
                # Maintain the original callback's caller-thread affinity.
                progress_callback(accepted_index + 1, expected_frames)

        try:
            source = iter(frames)
            assert worker.stdin is not None
            for index in range(expected_frames):
                if cancel.is_set():
                    raise RenderCancelledError("Native GPU export was cancelled.")
                production_started = monotonic()
                try:
                    frame = next(source)
                except StopIteration as exc:
                    raise RenderError(
                        f"Canvas frame producer stopped at {index}/{expected_frames}"
                    ) from exc
                production_elapsed = monotonic() - production_started
                frame_production_seconds += production_elapsed
                frame_production_max_seconds = max(frame_production_max_seconds, production_elapsed)
                if cancel.is_set():
                    raise RenderCancelledError("Native GPU export was cancelled.")
                if not isinstance(frame, (NativeCanvasFrame, DeferredNativeCanvasFrame)) or (
                    frame.width, frame.height
                ) != (width, height):
                    raise RenderError(f"Native Canvas frame {index} has wrong geometry or type.")
                if isinstance(frame, DeferredNativeCanvasFrame):
                    cpu_parallel_bgra_frames += 1
                if overlap_enabled:
                    # Produce frame N+1 on the Qt owner thread while the sender
                    # writes frame N. Before scheduling another send, wait for
                    # its ACK, which caps live packet memory at two frames.
                    finish_pending()
                    completion: Queue = Queue(maxsize=1)
                    started = monotonic()

                    def send_and_ack(
                        frame=frame, index=index, started=started, completion=completion,
                    ) -> None:
                        try:
                            assert worker.stdin is not None
                            # Converters only touch their own immutable
                            # QImages; Rust input must receive complete,
                            # strictly ordered and validated BGRA packets.
                            resolved = (
                                frame.resolve() if isinstance(frame, DeferredNativeCanvasFrame)
                                else frame
                            )
                            written_bytes = send_packet(resolved, index)
                            wrote_at = monotonic()
                            _frame_acknowledgement(
                                events, worker, index, cancel,
                                max(0.001, acknowledgement_timeout - (wrote_at - started)),
                            )
                            accepted_at = monotonic()
                            completion.put((
                                written_bytes, wrote_at - started,
                                accepted_at - wrote_at, accepted_at - started,
                            ))
                        except Exception as exc:
                            completion.put(exc)

                    sender = Thread(
                        target=send_and_ack, name="mms-native-gpu-input", daemon=True,
                    )
                    pending = (index, completion, sender, started)
                    sender.start()
                    continue

                sent: Queue = Queue(maxsize=1)
                def send_frame(frame=frame, index=index):
                    try:
                        resolved = (
                            frame.resolve() if isinstance(frame, DeferredNativeCanvasFrame)
                            else frame
                        )
                        sent.put(send_packet(resolved, index))
                    except Exception as exc:
                        sent.put(exc)
                started = monotonic()
                sender = Thread(target=send_frame, name="mms-native-gpu-input", daemon=True)
                sender.start()
                try:
                    while True:
                        if cancel.is_set():
                            raise RenderCancelledError("Native GPU export was cancelled.")
                        if monotonic() - started >= acknowledgement_timeout:
                            raise RenderError(f"Timed out writing native GPU frame {index}.")
                        try:
                            written = sent.get(timeout=0.1)
                            break
                        except Empty:
                            if worker.poll() is not None:
                                raise RenderError("Native GPU worker stopped during frame transfer.")
                    if isinstance(written, Exception):
                        raise written
                    transport_bytes += written
                    sender.join()
                    frame_pipe_write_seconds += monotonic() - started
                    del send_frame
                    del frame
                    ack_started = monotonic()
                    _frame_acknowledgement(
                        events, worker, index, cancel,
                        max(0.001, acknowledgement_timeout - (monotonic() - started)),
                    )
                    frame_ack_wait_seconds += monotonic() - ack_started
                    transport_seconds += monotonic() - started
                except (OSError, ValueError) as exc:
                    raise RenderError(
                        f"Native D3D11/NVENC channel failed while submitting frame {index}."
                    ) from exc
                if progress_callback is not None:
                    progress_callback(index + 1, expected_frames)
            if overlap_enabled:
                finish_pending()
            if encoder_flush_callback is not None:
                encoder_flush_callback()
            try:
                next(source)
            except StopIteration:
                pass
            else:
                raise RenderError("Canvas producer generated more frames than expected.")
            worker.stdin.close()
            finalize_started = monotonic()
            until = monotonic() + 180.0
            while worker.poll() is None:
                if cancel.is_set():
                    raise RenderCancelledError("Native GPU export was cancelled.")
                if monotonic() >= until:
                    raise RenderError("Native GPU worker did not complete after end of frames.")
                cancel.wait(0.1)
            if worker.returncode:
                error_reader.join(timeout=2)
                raise RenderError(
                    "Native GPU export failed: "
                    + error_tail[-2200:]
                )
            if not video.is_file() or not report.is_file():
                raise RenderError("Native GPU encoder exited without both final outputs.")
            try:
                result = json.loads(report.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise RenderError("Native GPU encoder wrote an unreadable final report.") from exc
            if (not isinstance(result, dict) or result.get("status") != "measured"
                    or result.get("production_export_scope") != "production_complete_playlist"
                    or result.get("frames_encoded") != expected_frames):
                raise RenderError("Native GPU producer did not complete the entire timeline.")
            if adapter_luid is not None and result.get("adapter_luid") != adapter_luid:
                raise RenderError("Native GPU worker used a different adapter than the compatibility check.")
            if cancel.is_set():
                raise RenderCancelledError("Native GPU export was cancelled.")
            # Phase counters measure wall-clock time, not hardware load.
            # When overlap is enabled, production and transport intentionally
            # run concurrently; their sums can exceed total elapsed time.
            stream_elapsed = monotonic() - stream_started
            result.update(
                frame_transport=input_format,
                frame_reuse_enabled=reuse_enabled,
                native_optimization_profile=(
                    "automatic" if optimizations is not None and optimizations.adaptive_bgra
                    else "cpu_parallel" if optimizations is not None and optimizations.parallel_bgra
                    else "accelerated" if optimizations is not None and optimizations == NativeGpuOptimizations.for_profile("accelerated")
                    else "standard" if optimizations is not None else "environment"
                ),
                cpu_parallel_bgra_frames=cpu_parallel_bgra_frames,
                frame_reused_planes=reuse_state.reused_planes if reuse_state is not None else 0,
                frame_reused_bytes=reuse_state.saved_bytes if reuse_state is not None else 0,
                frame_transport_bytes=transport_bytes,
                frame_transport_seconds=transport_seconds, bgra_disk_bytes=0,
                frame_production_seconds=frame_production_seconds,
                frame_production_max_seconds=frame_production_max_seconds,
                frame_pipe_write_seconds=frame_pipe_write_seconds,
                frame_sha256_seconds=frame_write_timings.get("sha256_seconds", 0.0),
                frame_sha256_wall_seconds=frame_write_timings.get("sha256_wall_seconds", 0.0),
                frame_sha256_bytes=frame_write_timings.get("sha256_bytes", 0),
                frame_sha256_reused_bytes=frame_write_timings.get("sha256_reused_bytes", 0),
                frame_pipe_syswrite_seconds=frame_write_timings.get("pipe_syswrite_seconds", 0.0),
                frame_pipe_syswrite_calls=frame_write_timings.get("pipe_syswrite_calls", 0),
                frame_shared_packet_enabled=shared_packet is not None,
                frame_shared_packet_bytes=frame_write_timings.get("shared_packet_bytes", 0),
                frame_shared_packet_frames=frame_write_timings.get("shared_packet_frames", 0),
                frame_shared_packet_write_seconds=frame_write_timings.get("shared_packet_write_seconds", 0.0),
                frame_shared_packet_fallback_frames=frame_write_timings.get("shared_packet_fallback_frames", 0),
                frame_lz4_cpu_seconds=frame_write_timings.get("lz4_cpu_seconds", 0.0),
                frame_lz4_wall_seconds=frame_write_timings.get("lz4_wall_seconds", 0.0),
                frame_lz4_planes=frame_write_timings.get("lz4_planes", 0),
                frame_lz4_layer_cost=frame_write_timings.get("lz4_layer_cost", {}),
                frame_lz4_auto_choice=(
                    compression_budget.decision if compression_budget else "not_available"
                ),
                frame_lz4_probe_samples=(
                    {str(key).lower(): len(values)
                     for key, values in compression_budget.samples.items()}
                    if compression_budget else {}
                ),
                frame_raw_source_bytes=frame_write_timings.get("raw_source_bytes", 0),
                frame_wire_payload_bytes=frame_write_timings.get("wire_payload_bytes", 0),
                frame_wire_saved_bytes=max(0,
                    frame_write_timings.get("raw_source_bytes", 0) -
                    frame_write_timings.get("wire_payload_bytes", 0)),
                frame_layer_transport=frame_write_timings.get("layer_transport", {}),
                frame_ack_wait_seconds=frame_ack_wait_seconds,
                native_worker_finalize_seconds=monotonic() - finalize_started,
                native_stream_elapsed_seconds=stream_elapsed,
                native_stream_wall_fps=expected_frames / stream_elapsed,
                timing_scope=(
                    "one_frame_qt_production_overlaps_native_write_and_ack"
                    if overlap_enabled else
                    "serial_qt_frame_production_pipe_write_and_native_ack"
                ),
                frame_pipeline_enabled=overlap_enabled,
                max_outstanding_frame_packets=2 if overlap_enabled else 1,
            )
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            logging.getLogger(__name__).info("Native frame transport: %d bytes in %.3fs (including ACK)",
                                             transport_bytes, transport_seconds)
            completed = True
            return result
        except RenderCancelledError:
            raise
        except RenderError as exc:
            _abort_worker(worker)
            error_reader.join(timeout=2)
            with error_lock:
                detail = error_tail[-2200:].strip()
            if detail:
                raise RenderError(f"{exc}\nNative worker: {detail}") from exc
            raise
        finally:
            reader_stop.set()
            _abort_worker(worker)
            if sender is not None:
                sender.join(timeout=4)
            packet_executor.shutdown(wait=True, cancel_futures=True)
            if worker.stdin is not None and not worker.stdin.closed:
                try:
                    worker.stdin.close()
                except OSError:
                    pass
            # The worker is no longer running; the output pump must see EOF
            # before its pipe closes, avoiding Windows handle leaks.
            output_reader.join(timeout=2)
            error_reader.join(timeout=2)
            if worker.stdout is not None and not worker.stdout.closed:
                worker.stdout.close()
            if worker.stderr is not None:
                worker.stderr.close()
            if source is not None:
                close = getattr(source, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logging.getLogger(__name__).exception("Cannot close native frame producer")
            if not completed:
                video.with_suffix(".h264.part").unlink(missing_ok=True)


def render_native_gpu_playlist(
    renderer: FFmpegRenderer, frames: Iterable[NativeCanvasFrame], *,
    executable: Path, tracks: list, compiled_plan, prepared_audio_path: Path,
    output_path: Path, settings: RenderSettings, metadata: ExportMetadata | None = None,
    cancel_event: Event | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    mux_progress_callback: Callable[[str, float, str], None] | None = None,
    storage_path_callback=None,
    optimizations: NativeGpuOptimizations | None = None,
    adapter_luid: int | None = None,
    video_sources=(),
) -> RenderResult:
    """Full native production hand-off: lazy frames -> H264 -> final AAC MP4.

    The Export dialog should call this only after its existing renderer
    has supplied full-timeline frames in original Z order.
    """
    count = math.ceil(compiled_plan.duration_seconds * settings.fps - 1e-9)

    def report_encoder_flush():
        if mux_progress_callback is not None:
            mux_progress_callback("Finishing GPU encoding", 0.0,
                                  "Writing the last encoded frames to H.264")

    with tempfile.TemporaryDirectory(prefix="mms-native-complete-") as staging:
        root = Path(staging)
        if storage_path_callback is not None:
            storage_path_callback("render", root)
        video = root / "complete.h264"
        report = root / "complete.json"
        native_report = stream_native_canvas_h264(
            frames, executable=executable, video_path=video, report_path=report,
            width=settings.output_width, height=settings.output_height,
            fps=settings.fps, duration_seconds=compiled_plan.duration_seconds,
            expected_frames=count, cancel_event=cancel_event,
            progress_callback=progress_callback,
            optimizations=optimizations,
            native_nvenc_preset=settings.native_nvenc_preset,
            native_nvenc_cq=settings.native_nvenc_cq,
            adapter_luid=adapter_luid,
            encoder_flush_callback=report_encoder_flush,
            **({"video_sources": video_sources} if video_sources else {}),
        )
        ffprobe = Path(renderer.executable).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe",
        )
        result = finalize_native_gpu_mp4(
            renderer, h264_path=video, gpu_report_path=report,
            prepared_audio_path=prepared_audio_path, tracks=tracks,
            compiled_plan=compiled_plan, output_path=output_path,
            settings=settings, ffprobe=ffprobe,
            metadata=metadata, cancel_event=cancel_event,
            progress_callback=mux_progress_callback,
            storage_path_callback=storage_path_callback,
        )
        return replace(result, native_report=native_report)
