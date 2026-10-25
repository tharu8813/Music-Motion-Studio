"""Capture every planned Canvas state and encode the export's visual streams."""

from __future__ import annotations

from app.utils.performance import timed

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import partial
from heapq import heappop, heappush
from pathlib import Path
from time import monotonic
import threading

from PySide6.QtGui import QImage

from app.models.playlist import PlaylistTrack
from app.models.source import SourceType
from app.services.lyrics_intro_service import request_instrumental_analysis
from app.preview.export_canvas_capture import ExportCanvasCapturer
from app.preview.export_plan import ExportPlan
from app.renderer.export_timeline import ExportFrameSample, ExportFrameSchedule
from app.renderer.python_visualizer import PythonVisualizerRenderer
from app.renderer.canvas_pipe import (
    CanvasPipeCancelledError,
    CanvasPipeError,
    PipedCanvasStream,
)
from app.renderer.ffmpeg_renderer import (
    PipedStaticOverlayLayer,
    PipedVideoInput,
    PreparedStaticOverlayLayer,
    PreparedVideoInput,
    RenderCancelledError,
    RenderError,
    RenderFrame,
    RenderSettings,
    StaticOverlayLayer,
)
from app.renderer.static_video_stream import (
    StaticVideoStreamEncoder,
    StaticVideoStreamError,
)

LOGGER = logging.getLogger(__name__)

# Fraction of the export progress bar reserved for Canvas preparation; the
# remainder belongs to FFmpeg encoding. Mirrors the main window constant.
PREPARATION_PROGRESS_WEIGHT = 0.25

# A live pipe with queued frames that FFmpeg has not read for this long is
# treated as a stalled encoder; the caller falls back to intermediate files.
PIPE_STALL_SECONDS = 120.0


class FinalRenderStoppedError(RenderError):
    """The final FFmpeg failed while the Canvas was still being captured."""


@dataclass(frozen=True, slots=True)
class ExportArtifacts:
    """The prepared visual inputs handed to :class:`FFmpegRenderer`."""

    frames: list[RenderFrame] | PreparedVideoInput
    static_layers: list


@dataclass(frozen=True, slots=True)
class PngStaging:
    """Window-owned disk-backed PNG frame staging, injected for testability.

    ``start_pipeline`` takes the queue capacity; the others take nothing.
    """

    stage_frame: Callable[[QImage, float, str], RenderFrame]
    start_pipeline: Callable[[int], None]
    finish_pipeline: Callable[[], None]
    cancel_pipeline: Callable[[], None]
    pending_frames: Callable[[], int]
    queue_capacity: int


class ExportSession:
    """Run the Canvas capture loop and lossless stream encoding for one export.

    Everything that touches the temporary directory, the progress dialog, or the
    main window's PNG staging is injected. The session produces exactly the two
    things the renderer needs: the base ``frames`` and any ``static_layers``.
    """

    def __init__(
        self,
        *,
        scene: object,
        renderer: object,
        plan: ExportPlan,
        render_settings: RenderSettings,
        active_tracks: Sequence[PlaylistTrack],
        stream_root: Path | None,
        preparation_cancel: threading.Event,
        korean: bool,
        staging: PngStaging,
        layer_worker_count: Callable[[int], int],
        report_progress: Callable[[float, str], None],
        pump_ui: Callable[[], None],
        bass_envelopes: dict[object, object] | None = None,
    ) -> None:
        self._scene = scene
        self._renderer = renderer
        self._plan = plan
        self._settings = render_settings
        self._tracks = list(active_tracks)
        self._stream_root = stream_root
        self._cancel = preparation_cancel
        self._korean = korean
        self._staging = staging
        self._layer_worker_count = layer_worker_count
        self._report_progress = report_progress
        self._pump_ui = pump_ui

        self._active_encoders: dict[str, StaticVideoStreamEncoder] = {}
        self._sparse_stream_frames: dict[str, list[RenderFrame]] = {}
        self._capture_count = 0
        self._captured_seconds: dict[str, float] = {}
        self._stream_timeline_samples = dict(plan.stream_timeline_samples)
        self._invariant_stream_keys: set[str] = set()
        self._sparse_invariant_stream_keys: set[str] = set()
        self._stream_wait_last_update: dict[str, float] = {}
        self._capturer: ExportCanvasCapturer | None = None
        self._piped = False
        self._pipes: dict[str, PipedCanvasStream] = {}
        self._live_encoder_reached = False
        self._encoder_reached_at: float | None = None
        self._final_render_stopped: Callable[[], str | None] = lambda: None
        self._bass_envelopes = bass_envelopes

    @property
    def capture_count(self) -> int:
        return self._capture_count

    @property
    def bass_envelopes(self) -> dict[object, object] | None:
        """Completed music analysis, safe to reuse when this export falls back."""
        return self._bass_envelopes

    @property
    def live_encoder_reached(self) -> bool:
        """Whether the final FFmpeg opened at least one Canvas pipe."""
        return self._live_encoder_reached or any(
            pipe.connected for pipe in self._pipes.values()
        )

    def cancel_streams(self) -> None:
        """Stop every active encoder and the PNG pipeline (cleanup on failure)."""
        for encoder in tuple(self._active_encoders.values()):
            encoder.cancel()
        self._active_encoders.clear()
        for pipe in tuple(self._pipes.values()):
            self._live_encoder_reached |= pipe.connected
            pipe.cancel()
        self._pipes.clear()
        self._staging.cancel_pipeline()

    # -- capture loop -----------------------------------------------------

    @timed("export.capture_session_seconds")
    def run(self) -> ExportArtifacts:
        plan = self._plan
        stream_specs, stream_keys = self._prepare_capture(piped=False)
        if plan.use_streamed_visuals:
            artifacts = self._run_streamed(stream_specs, stream_keys)
        else:
            artifacts = self._run_png(stream_keys)

        self._finalize_progress(len(stream_keys))
        self._log_partial_render_metrics()
        if self._cancel.is_set():
            raise RenderCancelledError("Export preparation was cancelled.")
        return artifacts

    def _prepare_capture(self, *, piped: bool) -> tuple[list, list[str]]:
        """Create the capturer and coalesce every stream's sample schedule."""
        plan = self._plan
        self._piped = piped
        z_bands = plan.z_bands
        stream_timeline_samples = self._stream_timeline_samples
        playlist_duration = plan.playlist_duration

        stream_specs = [] if piped else self._build_stream_specs()
        # Finish vocal detection before coalescing: every export frame sees one
        # immutable analysis result, even if the live preview was still analyzing.
        scene_items = getattr(self._scene, "items", lambda: ())
        sources = [item.source.resolved_lyrics() for item in scene_items()
                   if hasattr(item, "source") and item.source.visible
                   and item.source.source_type is SourceType.LYRICS]
        futures = {future for track in self._tracks for source in sources
                   if (future := request_instrumental_analysis(track, source)) is not None}
        if futures:
            self._report_progress(0, "가사 공백의 보컬을 분석하고 있습니다." if self._korean else
                                  "Analyzing vocals in lyric gaps.")
        while futures:
            self._ensure_not_cancelled()
            _done, futures = wait(futures, timeout=0.05)
            self._pump_ui()

        capturer = ExportCanvasCapturer(
            self._scene,
            self._tracks,
            playlist_duration,
            plan.dynamic_visualizer_ids,
            z_bands,
            self._stage_frame,
            self._ensure_not_cancelled,
            self._after_capture,
            retain_static_frames=not (plan.use_streamed_visuals or piped),
            output_scale=plan.canvas_render_scale,
            bass_envelopes=self._prepare_bass_envelopes(),
            bass_fps=self._settings.fps,
        )
        self._capturer = capturer
        self._invariant_stream_keys = capturer.invariant_stream_keys
        # A live pipe gains nothing from repeating one image for the whole
        # timeline, so piped exports keep every invariant stream as one PNG.
        self._sparse_invariant_stream_keys = (
            set(self._invariant_stream_keys)
            if piped or not plan.direct_final_stream else set()
        )
        if (
            (plan.use_streamed_visuals or piped)
            and self._sparse_invariant_stream_keys
        ):
            self._staging.start_pipeline(self._staging.queue_capacity)
            stream_specs = [
                spec for spec in stream_specs
                if spec[0] not in self._sparse_invariant_stream_keys
            ]

        stream_keys = [
            "base", *(f"layer:{index}" for index in range(len(z_bands) - 1)),
        ]
        self._captured_seconds = dict.fromkeys(stream_keys, 0.0)
        for stream_key in stream_keys:
            if stream_key not in self._invariant_stream_keys:
                stream_timeline_samples[stream_key] = ExportFrameSchedule(partial(
                    capturer.iter_coalesced_samples, stream_timeline_samples[stream_key], stream_key,
                    self._pipe_wait if piped else self._pump_ui,
                ))
        LOGGER.info("Canvas lazy timelines: streams=%s", ",".join(stream_keys))
        return stream_specs, stream_keys

    @timed("export.bass_prepare_seconds")
    def _prepare_bass_envelopes(self) -> dict[object, object]:
        """Prepare music responses off the GUI thread, keeping cancel/paint alive."""
        self._ensure_not_cancelled()
        if self._bass_envelopes is not None:
            return self._bass_envelopes
        scene_items = getattr(self._scene, "items", None)
        reactive = callable(scene_items) and any(
            bool(getattr(getattr(item, "source", None), "uses_bass_reaction", False))
            and bool(getattr(getattr(item, "source", None), "visible", False))
            for item in scene_items()
        )
        if not reactive:
            self._bass_envelopes = {}
            return self._bass_envelopes
        analyzer = PythonVisualizerRenderer(self._renderer.executable)
        profiles = {
            item.source.music_reaction_profile for item in scene_items()
            if getattr(item, "source", None) is not None and item.source.visible
            and item.source.source_type in {SourceType.TEXT, SourceType.LYRICS}
            and item.source.uses_bass_reaction
        }

        def analyze(path: Path) -> object:
            self._ensure_not_cancelled()
            samples = analyzer._decode_mono_audio(path, self._cancel)
            levels = analyzer._analyze_levels(samples, self._settings.fps, 24, self._cancel)
            return (analyzer.bass_envelope(levels, self._settings.fps), {
                profile: analyzer.music_envelope(levels, self._settings.fps, profile, self._cancel)
                for profile in profiles
            })

        envelopes = {}
        analyzed_paths = {}
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="export-bass") as executor:
            for index, track in enumerate(self._tracks, 1):
                self._ensure_not_cancelled()
                self._report_progress(0.0, (
                    f"음악 반응 준비 중 · 곡 {index}/{len(self._tracks)}"
                    if self._korean else
                    f"Preparing music reaction · track {index}/{len(self._tracks)}"
                ))
                path = Path(track.file_path).resolve()
                if path in analyzed_paths:
                    bass, music = analyzed_paths[path]
                    envelopes[track.id] = bass
                    envelopes.update({(track.id, profile): values for profile, values in music.items()})
                    continue
                future = executor.submit(analyze, path)
                try:
                    while not future.done():
                        self._pump_ui()
                        # Keep pumping even after cancel until FFmpeg/FFT exits;
                        # staging files must outlive the worker using them.
                        wait((future,), timeout=0.01)
                finally:
                    if not future.done():
                        self._cancel.set()
                self._ensure_not_cancelled()
                try:
                    bass, music = future.result()
                    analyzed_paths[path] = (bass, music)
                    envelopes[track.id] = bass
                    envelopes.update({(track.id, profile): values for profile, values in music.items()})
                except RenderCancelledError:
                    raise
                except Exception as error:
                    LOGGER.warning("Music reaction analysis failed: %s", error)
        self._ensure_not_cancelled()
        self._bass_envelopes = envelopes
        return self._bass_envelopes

    # -- streamed lossless path ----------------------------------------

    def _run_streamed(
        self, stream_specs: list, stream_keys: list[str],
    ) -> ExportArtifacts:
        plan = self._plan
        settings = self._settings
        playlist_duration = plan.playlist_duration
        stream_timeline_samples = self._stream_timeline_samples
        duration_tolerance = 1e-6 * max(1.0, playlist_duration)
        streamed_results: dict[str, object] = {}

        layer_parallelism = self._layer_worker_count(len(stream_specs))
        LOGGER.info(
            "Canvas layer preparation: streams=%d parallel_encoders=%d "
            "sparse_invariant=%d",
            len(stream_specs), layer_parallelism,
            len(self._sparse_invariant_stream_keys),
        )
        for stream_key in stream_keys:
            if stream_key in self._sparse_invariant_stream_keys:
                self._capturer.capture_invariant_stream(
                    next(iter(stream_timeline_samples[stream_key])),
                    stream_key,
                    playlist_duration,
                )
        for batch_start in range(0, len(stream_specs), layer_parallelism):
            batch = stream_specs[batch_start:batch_start + layer_parallelism]
            try:
                for stream_key, output_path, preserve_alpha, queue_capacity in batch:
                    self._active_encoders[stream_key] = StaticVideoStreamEncoder(
                        self._renderer.executable,
                        output_path,
                        settings.fps,
                        queue_capacity=queue_capacity,
                        preserve_alpha=preserve_alpha,
                        producer_cancel_event=self._cancel,
                        producer_wait_callback=(
                            lambda key=stream_key: self._stream_wait(key)
                        ),
                        direct_profile=(
                            self._renderer.direct_encoding_profile(settings)
                            if plan.direct_final_stream and stream_key == "base"
                            else None
                        ),
                    )
                for stream_key, _path, _alpha, _capacity in batch:
                    if stream_key in self._invariant_stream_keys:
                        self._capturer.capture_invariant_stream(
                            next(iter(stream_timeline_samples[stream_key])),
                            stream_key,
                            playlist_duration,
                        )
                pending_samples: list[
                    tuple[float, int, int, str, ExportFrameSample]
                ] = []
                batch_keys = [item[0] for item in batch]
                iterators = {key: iter(stream_timeline_samples[key]) for key in batch_keys}
                for stream_order, stream_key in enumerate(batch_keys):
                    if stream_key in self._invariant_stream_keys:
                        continue
                    first = next(iterators[stream_key])
                    heappush(pending_samples, (
                        first.timeline_seconds, stream_order, 0, stream_key, first,
                    ))
                while pending_samples:
                    (
                        _timeline_seconds, stream_order, sample_index,
                        stream_key, sample,
                    ) = heappop(pending_samples)
                    self._capturer.capture_stream(sample, stream_key)
                    next_index = sample_index + 1
                    next_sample = next(iterators[stream_key], None)
                    if next_sample is not None:
                        heappush(pending_samples, (
                            next_sample.timeline_seconds, stream_order,
                            next_index, stream_key, next_sample,
                        ))
                for stream_key, _path, _alpha, _capacity in batch:
                    encoder = self._active_encoders[stream_key]
                    LOGGER.info(
                        "Draining Canvas stream: key=%s written_frames=%d "
                        "expected_frames=%d",
                        stream_key,
                        int(getattr(encoder, "frame_count", 0)),
                        max(1, round(playlist_duration * settings.fps)),
                    )
                    streamed = encoder.finish()
                    self._active_encoders.pop(stream_key, None)
                    if abs(
                        streamed.duration_seconds - playlist_duration
                    ) > duration_tolerance:
                        difference = streamed.duration_seconds - playlist_duration
                        raise RenderError(
                            "A streamed Canvas timeline does not match the playlist "
                            f"duration (expected {playlist_duration:.6f}s, got "
                            f"{streamed.duration_seconds:.6f}s, difference "
                            f"{difference:+.6f}s)."
                        )
                    expected_alpha = stream_key != "base"
                    if streamed.has_alpha_stream != expected_alpha:
                        raise RenderError(
                            "A streamed Canvas layer has an invalid alpha configuration."
                        )
                    streamed_results[stream_key] = streamed
            except StaticVideoStreamError as error:
                self.cancel_streams()
                if self._cancel.is_set():
                    raise RenderCancelledError(
                        "Export preparation was cancelled."
                    ) from error
                raise RenderError(str(error)) from error
            except Exception:
                self.cancel_streams()
                raise

        if self._sparse_invariant_stream_keys:
            self._staging.finish_pipeline()
        if "base" in self._sparse_invariant_stream_keys:
            frames: list[RenderFrame] | PreparedVideoInput = (
                self._sparse_stream_frames["base"]
            )
            LOGGER.info(
                "Sparse invariant Canvas base: images=%d duration=%.3fs",
                len(frames), playlist_duration,
            )
        else:
            streamed = streamed_results["base"]
            frames = PreparedVideoInput(
                streamed.path,
                playlist_duration,
                streamed.width,
                streamed.height,
                streamed.fps,
                ready_for_mux=plan.direct_final_stream,
                encoded_codec=(
                    settings.video_codec if plan.direct_final_stream else ""
                ),
            )
            try:
                stream_bytes = streamed.path.stat().st_size
            except OSError:
                stream_bytes = -1
            LOGGER.info(
                "Streamed Canvas summary: frames=%d duration=%.3fs "
                "resolution=%dx%d queue_peak=%d file_bytes=%d",
                streamed.frame_count, streamed.duration_seconds,
                streamed.width, streamed.height,
                streamed.peak_buffered_frames, stream_bytes,
            )
        static_layers = [
            (
                StaticOverlayLayer(
                    z_min if z_min is not None else -10_000.0,
                    self._sparse_stream_frames[f"layer:{index}"],
                    *self._capturer.stream_origin(f"layer:{index}"),
                )
                if f"layer:{index}" in self._sparse_invariant_stream_keys else
                PreparedStaticOverlayLayer(
                    z_min if z_min is not None else -10_000.0,
                    PreparedVideoInput(
                        streamed_results[f"layer:{index}"].path,
                        playlist_duration,
                        streamed_results[f"layer:{index}"].width,
                        streamed_results[f"layer:{index}"].height,
                        streamed_results[f"layer:{index}"].fps,
                    ),
                    *self._capturer.stream_origin(f"layer:{index}"),
                )
            )
            for index, (z_min, _z_max) in enumerate(plan.z_bands[1:])
        ]
        return ExportArtifacts(frames, static_layers)

    # -- live piped path -----------------------------------------------

    @timed("export.piped_capture_session_seconds")
    def run_piped(
        self,
        start_final_render: Callable[[ExportArtifacts], None],
        final_render_stopped: Callable[[], str | None],
    ) -> ExportArtifacts | None:
        """Capture every stream straight into an already-running final FFmpeg.

        Returns ``None`` without side effects beyond capturer setup when every
        stream is invariant (nothing would be piped). Otherwise
        ``start_final_render`` receives the pipe inputs as soon as each stream
        has its first frame (which fixes its resolution), and capture
        continues in timeline order while FFmpeg encodes. ``final_render_stopped``
        returns an error message once the final render has failed.
        """
        plan = self._plan
        if self._stream_root is None:
            raise RenderError("Piped Canvas export needs a working folder.")
        self._final_render_stopped = final_render_stopped
        _specs, stream_keys = self._prepare_capture(piped=True)
        piped_keys = [
            key for key in stream_keys
            if key not in self._sparse_invariant_stream_keys
        ]
        if not piped_keys:
            if self._sparse_invariant_stream_keys:
                self._staging.cancel_pipeline()
            return None
        samples_by_stream = self._stream_timeline_samples
        playlist_duration = plan.playlist_duration
        pipe_root = self._stream_root / "pipes"
        try:
            for stream_key in piped_keys:
                self._pipes[stream_key] = PipedCanvasStream(
                    pipe_root,
                    stream_key,
                    self._settings.fps,
                    preserve_alpha=stream_key != "base",
                    queue_capacity=3 if stream_key == "base" else 2,
                    producer_cancel_event=self._cancel,
                    producer_wait_callback=self._pipe_wait,
                )
            LOGGER.info(
                "Canvas live capture: piped=%s sparse_invariant=%d",
                ",".join(piped_keys), len(self._sparse_invariant_stream_keys),
            )
            for stream_key in stream_keys:
                if stream_key in self._sparse_invariant_stream_keys:
                    self._capturer.capture_invariant_stream(
                        next(iter(samples_by_stream[stream_key])),
                        stream_key,
                        playlist_duration,
                    )
            if self._sparse_invariant_stream_keys:
                # The final FFmpeg reads these PNGs when it opens its inputs.
                self._staging.finish_pipeline()
            # One frame per pipe fixes every input's resolution before FFmpeg
            # starts; each fits in its queue, so nothing can block yet.
            pending: list[tuple[float, int, int, str, ExportFrameSample]] = []
            iterators = {key: iter(samples_by_stream[key]) for key in piped_keys}
            for stream_order, stream_key in enumerate(piped_keys):
                self._capturer.capture_stream(next(iterators[stream_key]), stream_key)
                second = next(iterators[stream_key], None)
                if second is not None:
                    heappush(pending, (
                        second.timeline_seconds, stream_order, 1, stream_key, second,
                    ))
            artifacts = self._piped_artifacts()
            start_final_render(artifacts)
            while pending:
                (
                    _timeline_seconds, stream_order, sample_index,
                    stream_key, sample,
                ) = heappop(pending)
                self._capturer.capture_stream(sample, stream_key)
                next_index = sample_index + 1
                next_sample = next(iterators[stream_key], None)
                if next_sample is not None:
                    heappush(pending, (
                        next_sample.timeline_seconds, stream_order,
                        next_index, stream_key, next_sample,
                    ))
            duration_tolerance = 1e-6 * max(1.0, playlist_duration)
            for stream_key in piped_keys:
                result = self._pipes[stream_key].finish()
                self._live_encoder_reached |= self._pipes[stream_key].connected
                self._pipes.pop(stream_key, None)
                LOGGER.info(
                    "Canvas pipe drained: key=%s frames=%d duration=%.3fs "
                    "reader_closed_early=%s queue_peak=%d",
                    stream_key, result.frame_count, result.duration_seconds,
                    result.reader_closed_early, result.peak_buffered_frames,
                )
                if abs(result.duration_seconds - playlist_duration) > duration_tolerance:
                    raise RenderError(
                        "A streamed Canvas timeline does not match the playlist "
                        f"duration (expected {playlist_duration:.6f}s, got "
                        f"{result.duration_seconds:.6f}s)."
                    )
        except CanvasPipeCancelledError as error:
            self.cancel_streams()
            if self._cancel.is_set():
                raise RenderCancelledError(
                    "Export preparation was cancelled."
                ) from error
            raise CanvasPipeError(str(error)) from error
        except CanvasPipeError:
            self.cancel_streams()
            raise
        except Exception:
            self.cancel_streams()
            raise
        self._log_partial_render_metrics()
        if self._cancel.is_set():
            raise RenderCancelledError("Export preparation was cancelled.")
        return artifacts

    def _piped_artifacts(self) -> ExportArtifacts:
        plan = self._plan
        duration = plan.playlist_duration

        def piped_input(stream_key: str) -> PipedVideoInput:
            pipe = self._pipes[stream_key]
            return PipedVideoInput(
                tuple(pipe.input_arguments()), duration,
                pipe.width, pipe.height, self._settings.fps,
            )

        if "base" in self._sparse_invariant_stream_keys:
            frames: list[RenderFrame] | PipedVideoInput = (
                self._sparse_stream_frames["base"]
            )
        else:
            frames = piped_input("base")
        static_layers: list = []
        for index, (z_min, _z_max) in enumerate(plan.z_bands[1:]):
            stream_key = f"layer:{index}"
            z_index = z_min if z_min is not None else -10_000.0
            origin = self._capturer.stream_origin(stream_key)
            if stream_key in self._sparse_invariant_stream_keys:
                static_layers.append(StaticOverlayLayer(
                    z_index, self._sparse_stream_frames[stream_key], *origin,
                ))
            else:
                static_layers.append(PipedStaticOverlayLayer(
                    z_index, piped_input(stream_key), *origin,
                ))
        return ExportArtifacts(frames, static_layers)

    def _stage_piped_frame(
        self, image: QImage, duration_seconds: float, stream_key: str,
    ) -> RenderFrame:
        pipe = self._pipes.get(stream_key)
        if pipe is None:
            raise RenderError("Invalid piped Canvas frame configuration.")
        pipe.submit(image, duration_seconds)
        return RenderFrame(Path(pipe.path), max(0.001, duration_seconds))

    def _check_piped_state(self) -> None:
        """Abort capture when the final render failed or stopped reading."""
        if (
            self._encoder_reached_at is None
            and any(pipe.connected for pipe in self._pipes.values())
        ):
            self._live_encoder_reached = True
            self._encoder_reached_at = monotonic()
        failure = self._final_render_stopped()
        if failure is not None:
            raise FinalRenderStoppedError(failure)
        if self._encoder_reached_at is None:
            # FFmpeg has not opened its inputs yet (audio or visualizers are
            # still being prepared); its own failure is reported above.
            return
        busy = [
            pipe for pipe in self._pipes.values()
            if pipe.started and not pipe.reader_closed and pipe.pending_frames > 0
        ]
        if not busy:
            return
        # Judge each blocked pipe on its own: another stream that is still
        # being read must not hide one FFmpeg stopped reading (or never
        # opened, measured from when FFmpeg reached its first pipe).
        now = monotonic()
        idle_seconds = max(
            now - max(pipe.last_activity, self._encoder_reached_at)
            for pipe in busy
        )
        if idle_seconds > PIPE_STALL_SECONDS:
            raise CanvasPipeError(
                "The final encoder stopped reading the Canvas for "
                f"{idle_seconds:.0f}s (pending: "
                + ", ".join(
                    f"{pipe.stream_key}={pipe.pending_frames}" for pipe in busy
                )
                + ")."
            )

    def _pipe_wait(self) -> None:
        """Producer wait callback while a pipe queue is full."""
        self._pump_ui()
        if self._cancel.is_set():
            return
        self._check_piped_state()

    # -- PNG fallback path -------------------------------------------

    def _run_png(self, stream_keys: list[str]) -> ExportArtifacts:
        plan = self._plan
        stream_timeline_samples = self._stream_timeline_samples
        self._staging.start_pipeline(self._staging.queue_capacity)
        captured_stream_frames: dict[str, list[RenderFrame]] = {}
        for stream_key in stream_keys:
            samples = stream_timeline_samples[stream_key]
            if stream_key in self._invariant_stream_keys:
                captured_stream_frames[stream_key] = [
                    self._capturer.capture_invariant_stream(
                        next(iter(samples)), stream_key, plan.playlist_duration,
                    )
                ]
            else:
                captured_stream_frames[stream_key] = [
                    self._capturer.capture_stream(sample, stream_key)
                    for sample in samples
                ]
        frames = captured_stream_frames["base"]
        self._report_progress(
            PREPARATION_PROGRESS_WEIGHT,
            "남은 화면 프레임을 저장하고 있습니다."
            if self._korean else
            "Finishing the remaining background frame writes.",
        )
        self._staging.finish_pipeline()
        return ExportArtifacts(frames, self._capturer.static_layers())

    # -- helpers -----------------------------------------------------

    def _build_stream_specs(self) -> list[tuple[str, Path, bool, int]]:
        if not self._plan.use_streamed_visuals:
            return []
        assert self._stream_root is not None
        root = self._stream_root
        specs: list[tuple[str, Path, bool, int]] = [
            ("base", root / "canvas-base.mkv", False, 3),
        ]
        specs.extend(
            (
                f"layer:{index}",
                root / f"canvas-layer-{index:02d}.mkv",
                True,
                2,
            )
            for index, _band in enumerate(self._plan.z_bands[1:])
        )
        return specs

    def _ensure_not_cancelled(self) -> None:
        if self._cancel.is_set():
            raise RenderCancelledError("Export preparation was cancelled.")

    def _stage_frame(
        self, image: QImage, duration_seconds: float, stream_key: str,
    ) -> RenderFrame:
        self._captured_seconds[stream_key] = self._captured_seconds.get(stream_key, 0.0) + duration_seconds
        if not self._plan.use_streamed_visuals and not self._piped:
            return self._staging.stage_frame(image, duration_seconds, stream_key)
        # Do not expand one invariant Canvas image into every CFR frame through a
        # CPU-only intermediate encoder. Preserve the single lossless image and
        # duration; the final graph applies the output FPS.
        if (
            (self._plan.use_streamed_visuals or self._piped)
            and stream_key in self._sparse_invariant_stream_keys
        ):
            rendered = self._staging.stage_frame(image, duration_seconds, stream_key)
            self._sparse_stream_frames.setdefault(stream_key, []).append(rendered)
            return rendered
        return self._stage_streamed_frame(image, duration_seconds, stream_key)

    def _stage_streamed_frame(
        self, image: QImage, duration_seconds: float, stream_key: str,
    ) -> RenderFrame:
        if self._piped:
            return self._stage_piped_frame(image, duration_seconds, stream_key)
        encoder = self._active_encoders.get(stream_key)
        if encoder is None:
            raise RenderError("Invalid streamed Canvas frame configuration.")
        try:
            encoder.submit(image, duration_seconds)
        except StaticVideoStreamError as error:
            if self._cancel.is_set():
                raise RenderCancelledError(
                    "Export preparation was cancelled."
                ) from error
            raise RenderError(str(error)) from error
        return RenderFrame(encoder.output_path, max(0.001, duration_seconds))

    def _capture_fraction(self) -> float:
        duration = self._plan.playlist_duration * max(1, len(self._plan.z_bands))
        return min(1.0, sum(self._captured_seconds.values()) / max(0.001, duration))

    def _after_capture(self, track_number: int, _stream_key: str) -> None:
        """Capturer callback after each staged Canvas state."""
        self._capture_count += 1
        if self._piped:
            # The final encoder reports the combined progress; keep the UI
            # responsive and notice a failed encoder between captures.
            if self._capture_count % 4 == 0:
                self._pump_ui()
            self._check_piped_state()
            if self._cancel.is_set():
                raise RenderCancelledError("Export preparation was cancelled.")
            return
        completed = self._capture_count
        preparation_fraction = self._capture_fraction()
        if completed % 4 != 0 and preparation_fraction < 1.0:
            return
        overall = preparation_fraction * PREPARATION_PROGRESS_WEIGHT
        percent = round(preparation_fraction * 100)
        position = f"{track_number}/{len(self._tracks)}"
        if self._plan.use_streamed_visuals:
            detail = (
                f"장면을 영상으로 준비하는 중 · {position}번 곡 · "
                f"전체 {percent}% · 장면 "
                f"{completed:,}"
                if self._korean else
                f"Preparing scenes for the video · track {position} · "
                f"{percent}% overall · "
                f"{completed:,} scenes"
            )
        else:
            pending = self._staging.pending_frames()
            detail = (
                f"장면을 영상으로 준비하는 중 · {position}번 곡 · "
                f"전체 {percent}% · 장면 "
                f"{completed:,} · 저장 대기 {pending}개"
                if self._korean else
                f"Preparing scenes for the video · track {position} · "
                f"{percent}% overall · "
                f"{completed:,} scenes · "
                f"{pending} waiting to save"
            )
        self._report_progress(overall, detail)
        self._pump_ui()
        if self._cancel.is_set():
            raise RenderCancelledError("Export preparation was cancelled.")

    def _stream_wait(self, stream_key: str) -> None:
        """Encoder producer-wait callback: repaint and show drain progress."""
        self._pump_ui()
        if self._cancel.is_set():
            return
        now = monotonic()
        if now - self._stream_wait_last_update.get(stream_key, 0.0) < 0.15:
            return
        self._stream_wait_last_update[stream_key] = now
        encoder = self._active_encoders.get(stream_key)
        if encoder is None:
            return
        expected = max(1, round(self._plan.playlist_duration * self._settings.fps))
        encoded = min(int(getattr(encoder, "frame_count", 0)), expected)
        percent = round(encoded / expected * 100)
        capture_fraction = self._capture_fraction()
        overall = capture_fraction * PREPARATION_PROGRESS_WEIGHT
        detail = (
            "화면 구성 요소를 영상으로 변환하는 중 · "
            f"{encoded:,}/{expected:,} 프레임 · {percent}%"
            if self._korean else
            "Converting visual elements into video · "
            f"{encoded:,}/{expected:,} frames · {percent}%"
        )
        self._report_progress(overall, detail)

    def _finalize_progress(self, stream_count: int) -> None:
        prepared = max(1, stream_count)
        detail = (
            f"화면 스트림 {prepared}/{prepared} · 준비 100%"
            if self._korean else
            f"Visual streams {prepared}/{prepared} · 100% prepared"
        )
        self._report_progress(PREPARATION_PROGRESS_WEIGHT, detail)

    def _log_partial_render_metrics(self) -> None:
        capturer = self._capturer
        if capturer is None:
            return
        avoided = max(
            0,
            capturer.full_frame_source_pixels - capturer.scene_render_source_pixels,
        )
        avoided_percent = avoided / max(1, capturer.full_frame_source_pixels) * 100.0
        LOGGER.info(
            "Canvas partial-region rendering: captures=%d scene_pixels=%d "
            "full_frame_pixels=%d avoided=%.1f%%",
            capturer.partial_render_capture_count,
            capturer.scene_render_source_pixels,
            capturer.full_frame_source_pixels,
            avoided_percent,
        )
