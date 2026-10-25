"""Python-rendered, audio-reactive visualizer overlay generation."""

from __future__ import annotations

from app.utils.performance import timed

import math
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

from app.animation.curves import (
    AnimationPose, animation_pose, ease_in_out_cubic, loop_pose,
)
from app.utils.level_meter_painter import paint_level_meter
from app.utils.mask_shapes import mask_path
from app.utils.particle_painter import paint_particles
from app.utils.radial_visualizer import paint_radial_bars
from app.utils.subprocess_utils import hidden_process_kwargs
from app.renderer.canvas_pipe import CanvasPipeError, PipedCanvasStream, piped_export_supported


class PythonVisualizerError(RuntimeError):
    """Raised when Python visualizer analysis or layer encoding fails."""


class PythonVisualizerRenderer:
    """Analyze playlist audio and draw transparent layers for export and preview."""

    sample_rate = 8_000
    fft_size = 512

    def __init__(self, ffmpeg_executable: Path) -> None:
        self.ffmpeg_executable = ffmpeg_executable

    def _audio_levels(
        self, audio_path: Path, overlays: Sequence[object], fps: int,
        cancel_event: threading.Event,
        progress_callback: Callable[[float, str], None] | None,
    ) -> tuple[np.ndarray, np.ndarray | None, tuple[np.ndarray, np.ndarray] | None]:
        self._report(progress_callback, 0.0, "Decoding audio for visualizers")
        needs_stereo = any(getattr(item, "kind", "") == "level_meter" for item in overlays)
        stereo = self._decode_stereo_audio(audio_path, cancel_event) if needs_stereo else None
        samples = (
            np.mean(stereo, axis=1, dtype=np.float32)
            if stereo is not None else self._decode_mono_audio(audio_path, cancel_event)
        )
        needs_spectrum = any(getattr(item, "kind", "visualizer") not in {
            "particles", "level_meter", "waveform",
        } for item in overlays)
        if needs_spectrum:
            self._report(progress_callback, 0.04, "Analyzing visualizer frequency levels")
        bands = max(
            max(4, min(96, int(getattr(item, "bar_count", 32))))
            for item in overlays
        )
        levels = (
            self._analyze_levels(samples, fps, bands, cancel_event)
            if needs_spectrum
            else np.zeros((max(1, math.ceil(len(samples) * fps / self.sample_rate)), 1), dtype=np.float32)
        )
        waveform_levels = None
        if any(getattr(item, "kind", "") == "waveform" for item in overlays):
            self._report(progress_callback, 0.08, "Analyzing waveform samples")
            waveform_levels = self._analyze_waveform(
                samples, fps, max(32, max(int(getattr(item, "bar_count", 32)) for item in overlays)), cancel_event
            )
        stereo_levels: tuple[np.ndarray, np.ndarray] | None = None
        if stereo is not None:
            self._report(progress_callback, 0.11, "Analyzing stereo level meter channels")
            stereo_levels = (
                self._analyze_rms(stereo[:, 0], fps, cancel_event),
                self._analyze_rms(stereo[:, 1], fps, cancel_event),
            )
        return levels, waveform_levels, stereo_levels

    def render_layers(
        self,
        audio_path: Path,
        overlays: Sequence[object],
        fps: int,
        directory: Path,
        cancel_event: threading.Event,
        progress_callback: Callable[[float, str], None] | None = None,
        track_windows: Sequence[tuple[float, ...]] = (),
    ) -> list[Path]:
        """Create one alpha-preserving video layer for every configured source."""
        if not overlays:
            return []
        levels, waveform_levels, stereo_levels = self._audio_levels(
            audio_path, overlays, fps, cancel_event, progress_callback,
        )
        paths: list[Path | None] = [None] * len(overlays)
        layer_progress = [0.0] * len(overlays)
        layer_frames = [0] * len(overlays)
        layer_frame_totals = [len(levels)] * len(overlays)
        progress_lock = threading.Lock()

        def encode_layer(index: int, overlay: object) -> tuple[int, Path]:
            if cancel_event.is_set():
                raise PythonVisualizerError("Rendering was cancelled.")
            path = directory / f"python_visualizer_{index:02d}.mov"
            source_levels = waveform_levels if getattr(overlay, "kind", "") == "waveform" else levels
            if source_levels is None:
                source_levels = levels

            def report_layer(
                local: float, completed: int, total: int,
            ) -> None:
                # Retain every concurrent layer's latest state and publish one
                # combined message instead of letting worker messages overwrite
                # one another in the progress dialog.
                with progress_lock:
                    layer_progress[index] = max(
                        layer_progress[index], min(1.0, max(0.0, local)),
                    )
                    layer_frames[index] = max(layer_frames[index], completed)
                    layer_frame_totals[index] = max(1, total)
                    aggregate = sum(layer_progress) / len(layer_progress)
                    message = self._combined_layer_progress_message(
                        layer_frames, layer_frame_totals,
                    )
                self._report(
                    progress_callback, 0.15 + 0.85 * aggregate, message,
                )

            self._encode_layer(
                path, overlay, source_levels, fps, cancel_event, report_layer,
                stereo_levels, track_windows,
            )
            return index, path

        worker_count = min(
            len(overlays), max(1, min(2, (os.cpu_count() or 2) // 2)),
        )
        self._report(
            progress_callback, 0.15,
            f"Preparing {len(overlays)} visualizer layer(s)",
        )
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="visualizer-layer",
        ) as executor:
            futures = [
                executor.submit(encode_layer, index, overlay)
                for index, overlay in enumerate(overlays)
            ]
            for future in as_completed(futures):
                index, path = future.result()
                paths[index] = path
        self._report(progress_callback, 1.0, "Visualizer frames complete")
        return [path for path in paths if path is not None]

    @contextmanager
    def stream_layers(
        self, audio_path: Path, overlays: Sequence[object], fps: int,
        directory: Path, cancel_event: threading.Event,
        progress_callback: Callable[[float, str], None] | None = None,
        track_windows: Sequence[tuple[float, ...]] = (),
    ) -> Iterator[list[list[str]]]:
        """Draw bounded RGBA streams while the final encoder consumes them."""
        if not piped_export_supported():
            yield [["-i", str(path)] for path in self.render_layers(
                audio_path, overlays, fps, directory, cancel_event,
                progress_callback, track_windows,
            )]
            return
        if not overlays:
            yield []
            return
        levels, waveform, stereo = self._audio_levels(
            audio_path, overlays, fps, cancel_event, progress_callback,
        )
        pipes: list[PipedCanvasStream] = []
        workers: list[threading.Thread] = []
        errors: list[Exception] = []

        def produce(pipe: PipedCanvasStream, frames: Iterator[QImage]) -> None:
            try:
                for image in frames:
                    pipe.submit(image, 1.0 / fps)
                pipe.finish()
            except Exception as error:
                errors.append(error)
                pipe.cancel()

        try:
            for index, overlay in enumerate(overlays):
                source_levels = waveform if getattr(overlay, "kind", "") == "waveform" else levels
                frames = self._layer_frames(
                    overlay, source_levels if source_levels is not None else levels,
                    fps, cancel_event, None, stereo, track_windows,
                )
                pipe = PipedCanvasStream(
                    directory, f"visualizer:{index}", fps, preserve_alpha=True,
                    queue_capacity=2, producer_cancel_event=cancel_event,
                )
                pipes.append(pipe)
                pipe.submit(next(frames), 1.0 / fps)
                # Every input needs its own producer: a pool smaller than the
                # input count can deadlock while FFmpeg opens the remaining pipes.
                worker = threading.Thread(
                    target=produce, args=(pipe, frames),
                    name=f"visualizer-stream-{index}",
                )
                worker.start()
                workers.append(worker)
            yield [pipe.input_arguments() for pipe in pipes]
            for worker in workers:
                worker.join()
            if errors:
                raise PythonVisualizerError(str(errors[0])) from errors[0]
        except CanvasPipeError as error:
            raise PythonVisualizerError(str(error)) from error
        finally:
            for pipe in pipes:
                pipe.cancel()
            for worker in workers:
                worker.join()

    @staticmethod
    def _report(
        callback: Callable[[float, str], None] | None, fraction: float, message: str,
    ) -> None:
        if callback is not None:
            callback(max(0.0, min(1.0, fraction)), message)

    def _decode_mono_audio(self, audio_path: Path, cancel_event: threading.Event) -> np.ndarray:
        """Decode compact PCM for analysis; FFmpeg does no visualizer rendering here."""
        values = self._decode_pcm(audio_path, 1, cancel_event)
        return values if len(values) else np.zeros(1, dtype=np.float32)

    def _decode_stereo_audio(self, audio_path: Path, cancel_event: threading.Event) -> np.ndarray:
        """Decode true left/right PCM through the same cancellable streaming path."""
        values = self._decode_pcm(audio_path, 2, cancel_event)
        if len(values) < 2:
            return np.zeros((1, 2), dtype=np.float32)
        return values[:len(values) // 2 * 2].reshape(-1, 2)

    def _decode_pcm(self, audio_path: Path, channels: int,
                    cancel_event: threading.Event) -> np.ndarray:
        """Stream compact PCM without an uncancellable full-output subprocess call."""
        command = [
            str(self.ffmpeg_executable), "-hide_banner", "-loglevel", "error", "-i", str(audio_path),
            "-vn", "-ac", str(channels), "-ar", str(self.sample_rate), "-f", "s16le", "-",
        ]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **hidden_process_kwargs(),
            )
        except OSError as error:
            raise PythonVisualizerError(f"Could not start FFmpeg audio decoding: {error}") from error
        raw = bytearray()
        assert process.stdout is not None
        while True:
            if cancel_event.is_set():
                self._stop_process(process)
                process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                raise PythonVisualizerError("Rendering was cancelled.")
            # ``read`` may wait for the full requested byte count on Windows.
            # ``read1`` returns currently available pipe data, keeping preview
            # cancellation responsive even for long mono tracks.
            block = process.stdout.read1(1024 * 16)
            if not block:
                break
            raw.extend(block)
        stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
        return_code = process.wait()
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if return_code != 0:
            raise PythonVisualizerError(stderr.strip() or "Could not decode audio for the visualizer.")
        if not raw:
            return np.zeros(0, dtype=np.float32)
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        samples /= 32768.0
        return samples

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        """Terminate and reap a helper process, escalating only when necessary."""
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    @timed("audio.rms_seconds")
    def _analyze_rms(self, samples: np.ndarray, fps: int,
                     cancel_event: threading.Event) -> np.ndarray:
        """Return one actual channel RMS value per rendered frame."""
        frame_count = max(1, math.ceil(len(samples) * fps / self.sample_rate))
        result = np.zeros(frame_count, dtype=np.float32)
        for index in range(frame_count):
            if cancel_event.is_set():
                raise PythonVisualizerError("Rendering was cancelled.")
            start = round(index * self.sample_rate / fps)
            end = min(len(samples), round((index + 1) * self.sample_rate / fps))
            window = samples[start:end]
            result[index] = float(np.sqrt(np.mean(window * window))) if len(window) else 0.0
        return result

    @timed("audio.waveform_seconds")
    def _analyze_waveform(self, samples: np.ndarray, fps: int, points: int,
                          cancel_event: threading.Event) -> np.ndarray:
        """Sample signed PCM windows for a genuine time-domain waveform."""
        frame_count = max(1, math.ceil(len(samples) * fps / self.sample_rate))
        result = np.zeros((frame_count, points), dtype=np.float32)
        for index in range(frame_count):
            if cancel_event.is_set():
                raise PythonVisualizerError("Rendering was cancelled.")
            start = round(index * self.sample_rate / fps)
            end = min(len(samples), round((index + 1) * self.sample_rate / fps))
            window = samples[start:end]
            if len(window) > 1:
                result[index] = np.interp(
                    np.linspace(0, len(window) - 1, points), np.arange(len(window)), window
                )
        return result

    def preview_levels(self, audio_path: Path, seconds: float, bands: int) -> np.ndarray:
        """Analyze a short real-audio window for an export-preview frame."""
        command = [
            str(self.ffmpeg_executable), "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, seconds):.3f}", "-t", "0.18", "-i", str(audio_path),
            "-vn", "-ac", "1", "-ar", str(self.sample_rate), "-f", "s16le", "-",
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, check=False, timeout=10,
                **hidden_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            # Full Preview must remain recoverable when a damaged/network audio
            # file or a broken FFmpeg process stops responding. The idle level
            # is deliberately nonzero so the source remains visible for repair.
            return np.full(max(4, bands), 0.08, dtype=np.float32)
        if completed.returncode != 0 or not completed.stdout:
            return np.full(max(4, bands), 0.08, dtype=np.float32)
        samples = np.frombuffer(completed.stdout, dtype="<i2").astype(np.float32) / 32768.0
        window = np.hanning(self.fft_size).astype(np.float32)
        segment = np.zeros(self.fft_size, dtype=np.float32)
        length = min(len(samples), self.fft_size)
        segment[:length] = samples[:length]
        frequencies = np.fft.rfftfreq(self.fft_size, 1 / self.sample_rate)
        edges = np.geomspace(35.0, self.sample_rate / 2, max(4, bands) + 1)
        spectrum = np.abs(np.fft.rfft(segment * window)) / (self.fft_size / 2)
        bins, probes = self._frequency_band_layout(frequencies, edges)
        return self._frequency_band_values(spectrum, frequencies, bins, probes)

    @staticmethod
    def music_envelope(levels: np.ndarray, fps: float, profile: tuple,
                       cancel_event: threading.Event | None = None) -> np.ndarray:
        """Source-specific response, evaluated once on the track clock."""
        values = np.asarray(levels, dtype=np.float32)
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.size == 0:
            return np.zeros(1, dtype=np.float32)
        band, attack_seconds, release_seconds, sensitivity, threshold, curve = profile
        count = values.shape[1]
        # FFT bands are logarithmic, from 35 Hz to Nyquist.
        bounds = {"bass": (0, max(1, count // 6)),
                  "mid": (count // 6, max(count // 6 + 1, count * 2 // 3)),
                  "treble": (count * 2 // 3, count), "full": (0, count)}
        start, end = bounds[band]
        energy = np.mean(values[:, start:end], axis=1)
        floor, ceiling = np.percentile(energy, (20, 95))
        normalized = np.clip((energy - floor) / max(1e-6, ceiling - floor), 0, 1)
        target = np.clip((normalized * sensitivity - threshold) / (1 - threshold), 0, 1)
        target = target ** {"soft": 0.6, "linear": 1.0, "punchy": 2.0}[curve]
        rates = [1.0 if seconds <= 0 else 1 - math.exp(-math.log(10) / (max(1, fps) * seconds))
                 for seconds in (attack_seconds, release_seconds)]
        result = np.zeros_like(target, dtype=np.float32)
        previous = 0.0
        for index, value in enumerate(target):
            if cancel_event is not None and index % 1024 == 0 and cancel_event.is_set():
                raise PythonVisualizerError("Music reaction analysis cancelled.")
            previous += (float(value) - previous) * rates[0 if value > previous else 1]
            result[index] = previous
        return result

    @staticmethod
    def music_reaction_level(envelope: object, fps: float, elapsed: float, offset: float = 0) -> float:
        if envelope is None or elapsed - offset < 0:
            return 0.0
        try:
            frame = round((elapsed - offset) * fps)
            return float(envelope[frame]) if frame < len(envelope) else 0.0
        except (IndexError, TypeError, ValueError):
            return 0.0

    @staticmethod
    def bass_envelope(levels: np.ndarray, fps: float = 30) -> np.ndarray:
        """Return a smooth, track-normalized 0..1 low-frequency envelope."""
        values = np.asarray(levels, dtype=np.float32)
        if values.ndim == 1:
            values = values.reshape(1, -1)
        if values.size == 0:
            return np.zeros(1, dtype=np.float32)
        low = np.mean(values[:, :max(1, values.shape[1] // 6)], axis=1)
        floor = float(np.percentile(low, 20))
        ceiling = float(np.percentile(low, 95))
        target = np.clip((low - floor) / max(1e-6, ceiling - floor), 0.0, 1.0)
        result = np.zeros_like(target, dtype=np.float32)
        step = 30.0 / max(1.0, fps)
        attack, release = 1.0 - 0.28 ** step, 1.0 - 0.82 ** step
        previous = 0.0
        for index, value in enumerate(target):
            rate = attack if value > previous else release
            previous += (float(value) - previous) * rate
            result[index] = previous
        return result

    @staticmethod
    def preview_image(width: int, height: int, overlay: object, levels: np.ndarray,
                      frame_index: int, processed: bool = False,
                      frame_rate: int = 60,
                      channel_values: tuple[float, ...] | None = None,
                      peak_values: tuple[float, ...] | None = None) -> QImage:
        """Create one overlay image with the same drawing rules used for export."""
        count = max(4, min(96, int(getattr(overlay, "bar_count", 32))))
        if processed:
            values = PythonVisualizerRenderer._resample_levels(levels, count)
        else:
            values = PythonVisualizerRenderer.process_level_sequence(
                np.asarray(levels, dtype=np.float32).reshape(1, -1), overlay, count,
            )[0]
        return PythonVisualizerRenderer._draw_frame(
            width, height, overlay, values, frame_index,
            channel_values=channel_values, frame_rate=frame_rate,
            peak_values=peak_values,
        )

    @timed("audio.fft_levels_seconds")
    def _analyze_levels(self, samples: np.ndarray, fps: int, bands: int,
                        cancel_event: threading.Event) -> np.ndarray:
        """Return smooth logarithmic-frequency levels for every output frame."""
        frame_count = max(1, math.ceil(len(samples) * fps / self.sample_rate))
        window = np.hanning(self.fft_size).astype(np.float32)
        frequencies = np.fft.rfftfreq(self.fft_size, 1 / self.sample_rate)
        edges = np.geomspace(35.0, self.sample_rate / 2, bands + 1)
        bins, probes = self._frequency_band_layout(frequencies, edges)
        result = np.zeros((frame_count, bands), dtype=np.float32)
        # Construct and transform several independent FFT windows at once.
        # Keeping the established per-spectrum band reducer preserves the exact
        # float32 output while removing thousands of Python-level FFT calls.
        frame_offsets = (
            np.arange(self.fft_size, dtype=np.int64) - self.fft_size // 2
        )
        batch_size = 256
        for batch_start in range(0, frame_count, batch_size):
            if cancel_event.is_set():
                raise PythonVisualizerError("Rendering was cancelled.")
            batch_end = min(frame_count, batch_start + batch_size)
            frame_indices = np.arange(batch_start, batch_end, dtype=np.int64)
            centers = frame_indices * self.sample_rate // max(1, fps)
            sample_indices = centers[:, None] + frame_offsets[None, :]
            valid = (sample_indices >= 0) & (sample_indices < len(samples))
            segments = np.zeros(sample_indices.shape, dtype=np.float32)
            segments[valid] = samples[sample_indices[valid]]
            # Normalize FFT magnitude by its window size.  Raw FFT values are much
            # larger than audible amplitude and previously saturated almost every
            # bar, making quiet passages look like a solid rectangle.
            spectra = (
                np.abs(np.fft.rfft(segments * window, axis=1))
                / (self.fft_size / 2)
            )
            for local_index, spectrum in enumerate(spectra):
                values = self._frequency_band_values(
                    spectrum, frequencies, bins, probes,
                )
                result[batch_start + local_index] = np.clip(
                    values, 0.0, 2.0,
                )
        return result

    @staticmethod
    def _frequency_band_layout(
        frequencies: np.ndarray, edges: np.ndarray,
    ) -> tuple[tuple[np.ndarray, ...], np.ndarray]:
        """Prepare logarithmic bands without assuming each contains an FFT bin.

        At 8 kHz with a 512-point FFT, low-frequency bins are 15.625 Hz apart.
        A 36–96 band logarithmic layout has several narrower bands, so assigning
        only whole FFT bins leaves deterministic holes. Five logarithmic probes
        per band let those ranges interpolate the neighboring measured bins while
        the real in-band maximum still preserves sharp musical peaks.
        """
        bins = tuple(
            np.flatnonzero(
                (frequencies >= edges[index])
                & (frequencies < edges[index + 1])
            )
            for index in range(len(edges) - 1)
        )
        probes = np.stack([
            np.geomspace(edges[index], edges[index + 1], 5)
            for index in range(len(edges) - 1)
        ]).astype(np.float32, copy=False)
        return bins, probes

    @staticmethod
    def _frequency_band_values(
        spectrum: np.ndarray,
        frequencies: np.ndarray,
        bins: tuple[np.ndarray, ...],
        probes: np.ndarray,
    ) -> np.ndarray:
        """Measure every band using real peaks plus interpolated FFT samples."""
        interpolated = np.interp(
            probes.reshape(-1), frequencies, spectrum,
        ).reshape(probes.shape)
        values = np.max(interpolated, axis=1)
        for index, band in enumerate(bins):
            if len(band):
                values[index] = max(values[index], float(np.max(spectrum[band])))
        return values.astype(np.float32, copy=False)

    def _encode_layer(
        self,
        path: Path,
        overlay: object,
        all_levels: np.ndarray,
        fps: int,
        cancel_event: threading.Event,
        progress_callback: Callable[[float, int, int], None] | None,
        stereo_levels: tuple[np.ndarray, np.ndarray] | None = None,
        track_windows: Sequence[tuple[float, ...]] = (),
    ) -> None:
        """Stream local RGBA frames into an alpha-capable MOV file."""
        width = max(8, int(getattr(overlay, "width")))
        height = max(8, int(getattr(overlay, "height")))
        command = [
            str(self.ffmpeg_executable), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgba", "-video_size", f"{width}x{height}",
            "-framerate", str(fps), "-i", "-", "-an", "-c:v", "qtrle", "-pix_fmt", "argb", str(path),
        ]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                **hidden_process_kwargs(),
            )
        except OSError as error:
            raise PythonVisualizerError(f"Could not start Python visualizer encoding: {error}") from error
        assert process.stdin is not None
        try:
            for image in self._layer_frames(
                overlay, all_levels, fps, cancel_event, progress_callback,
                stereo_levels, track_windows,
            ):
                process.stdin.write(image.constBits())
            process.stdin.close()
            stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            return_code = process.wait()
            if return_code != 0:
                raise PythonVisualizerError(stderr.strip() or "Could not encode the Python visualizer layer.")
        except (BrokenPipeError, OSError) as error:
            self._stop_process(process)
            stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise PythonVisualizerError(
                stderr.strip() or f"Visualizer encoder stopped unexpectedly: {error}"
            ) from error
        finally:
            self._stop_process(process)
            for stream in (process.stdin, process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _layer_frames(
        self, overlay: object, all_levels: np.ndarray, fps: int,
        cancel_event: threading.Event,
        progress_callback: Callable[[float, int, int], None] | None,
        stereo_levels: tuple[np.ndarray, np.ndarray] | None = None,
        track_windows: Sequence[tuple[float, ...]] = (),
    ) -> Iterator[QImage]:
        """Share exactly the same drawing, timing and alpha on both output paths."""
        width = max(8, int(getattr(overlay, "width")))
        height = max(8, int(getattr(overlay, "height")))
        bar_count = max(4, min(96, int(getattr(overlay, "bar_count", 32))))
        processed_levels = (
            np.zeros((len(all_levels), 1), dtype=np.float32)
            if getattr(overlay, "kind", "") in {"particles", "level_meter"}
            else self.process_level_sequence(all_levels, overlay, bar_count)
        )
        meter_levels: np.ndarray | None = None
        meter_peaks: np.ndarray | None = None
        if getattr(overlay, "kind", "") == "level_meter":
            if stereo_levels is not None:
                raw_meter = np.column_stack(stereo_levels)
            else:
                raw_meter = np.mean(np.maximum(all_levels, 0.0), axis=1, keepdims=True)
            meter_levels, meter_peaks = self.process_meter_sequence(
                raw_meter, overlay, fps,
            )
        frame_buffer = QImage()
        colored_overlay = overlay
        colored_track_index = -2
        for frame_index, selected in enumerate(processed_levels):
            if cancel_event.is_set():
                raise PythonVisualizerError("Rendering was cancelled.")
            channel_values = None
            peak_values = None
            if meter_levels is not None and meter_peaks is not None:
                meter_index = min(frame_index, len(meter_levels) - 1)
                channel_values = tuple(float(value) for value in meter_levels[meter_index])
                peak_values = tuple(float(value) for value in meter_peaks[meter_index])
            timeline_start = max(0.0, float(getattr(overlay, "timeline_start", 0.0)))
            timeline_duration = max(0.0, float(getattr(overlay, "timeline_duration", 0.0)))
            frame_seconds = frame_index / max(1, fps)
            personal_colors = tuple(getattr(overlay, "personal_colors", ()))
            track_index = self._track_index_at(frame_seconds, track_windows)
            if personal_colors and track_index != colored_track_index:
                selected_color = (
                    personal_colors[track_index]
                    if 0 <= track_index < len(personal_colors)
                    else str(getattr(overlay, "color", "#FFFFFF"))
                )
                colored_overlay = replace(
                    overlay,
                    color=selected_color,
                    particle_secondary_color=selected_color,
                    level_meter_low_color=selected_color,
                    level_meter_mid_color=selected_color,
                    level_meter_high_color=selected_color,
                )
                colored_track_index = track_index
            visible = not (
                frame_seconds < timeline_start
                or (timeline_duration > 0.0
                    and frame_seconds >= timeline_start + timeline_duration)
            )
            if visible:
                frame_buffer = self._draw_frame(
                    width, height, colored_overlay, selected, frame_index,
                    channel_values=channel_values, image_buffer=frame_buffer,
                    frame_rate=fps, peak_values=peak_values,
                )
                frame_buffer = self.transform_layer(
                    frame_buffer, overlay, frame_seconds, track_windows,
                )
            else:
                image_format = QImage.Format.Format_RGBA8888
                if (frame_buffer.width() != width or frame_buffer.height() != height
                        or frame_buffer.format() != image_format):
                    frame_buffer = QImage(width, height, image_format)
                frame_buffer.fill(0)
            yield frame_buffer
            completed_in_layer = frame_index + 1
            if progress_callback and (
                completed_in_layer == 1
                or completed_in_layer % 12 == 0
                or completed_in_layer == len(processed_levels)
            ):
                progress_callback(
                    completed_in_layer / len(processed_levels),
                    completed_in_layer,
                    len(processed_levels),
                )

    @staticmethod
    def _animation_state(
        seconds: float,
        track_windows: Sequence[tuple[float, ...]],
        overlay: object,
    ) -> tuple[str, float, bool] | None:
        """Resolve one reactive source's per-track entrance or exit state.

        A window may carry ``(start, duration, mix_in, mix_out)``: the audio mix
        after its start / before its end, which ``animation_fit_mix`` animations
        last instead of their own durations (see frame_state.mix_animation).
        """
        if not track_windows:
            return None
        animation_in = str(getattr(overlay, "animation_in", "none"))
        animation_out = str(getattr(overlay, "animation_out", "none"))
        configured_in = max(0.001, float(getattr(overlay, "animation_in_duration", 0.45)))
        configured_out = max(0.001, float(getattr(overlay, "animation_out_duration", 0.45)))
        fit_mix = bool(getattr(overlay, "animation_fit_mix", False))
        for index, window in enumerate(track_windows):
            start, duration = window[0], window[1]
            mix_in, mix_out = (window[2], window[3]) if len(window) > 3 else (0.0, 0.0)
            in_duration = mix_in if fit_mix and mix_in > 0.0 else configured_in
            out_duration = mix_out if fit_mix and mix_out > 0.0 else configured_out
            end = start + duration
            if seconds < start:
                if index == 0 and animation_in != "none":
                    return animation_in, 0.0, True
                if index > 0 and animation_out != "none":
                    return animation_out, 1.0, False
                return None
            if seconds < end:
                if animation_in != "none" and seconds < start + in_duration:
                    return animation_in, min(1.0, (seconds - start) / in_duration), True
                if animation_out != "none" and seconds >= end - out_duration:
                    return animation_out, min(1.0, (seconds - (end - out_duration)) / out_duration), False
                return None
        if animation_out != "none":
            return animation_out, 1.0, False
        return None

    @staticmethod
    def _track_index_at(
        seconds: float, track_windows: Sequence[tuple[float, ...]],
    ) -> int:
        """Return the active track index, or -1 while the timeline is in a gap."""
        for index, (start, duration, *_mix) in enumerate(track_windows):
            if start <= seconds < start + duration:
                return index
        return -1

    @classmethod
    def transform_layer(
        cls, image: QImage, overlay: object, seconds: float,
        track_windows: Sequence[tuple[float, ...]], size: tuple[float, float] | None = None,
    ) -> QImage:
        """Apply the source's entrance/exit and its looping motion at ``seconds``.

        ``size`` is the layer size the motion is scaled to (default: the overlay's).
        """
        width, height = size or (
            float(getattr(overlay, "width", image.width())),
            float(getattr(overlay, "height", image.height())),
        )
        loop = loop_pose(
            str(getattr(overlay, "loop_motion", "none")), seconds,
            float(getattr(overlay, "loop_motion_period", 4.0)),
            float(getattr(overlay, "loop_motion_amount", 1.0)), width, height,
        )
        state = cls._animation_state(seconds, track_windows, overlay)
        if state is None and loop.is_identity:
            return image
        style, progress, entering = state or ("none", 1.0, True)
        return cls._apply_animation(image, style, progress, entering, width, height, loop)

    @staticmethod
    def _apply_animation(
        image: QImage, style: str, raw_progress: float, entering: bool,
        source_width: float | None = None, source_height: float | None = None,
        loop: AnimationPose = AnimationPose(),
    ) -> QImage:
        """Apply the Canvas animation pose (plus any loop motion) to a reactive layer."""
        width = float(image.width())
        height = float(image.height())
        pose = animation_pose(
            style, raw_progress, entering,
            source_width if source_width is not None else width,
            source_height if source_height is not None else height,
        )
        if style != "none":
            # Reactive layers keep a balanced fade: their motion is clipped to
            # the layer box, so the fade carries more of the transition.
            raw_progress = max(0.0, min(1.0, raw_progress))
            pose = replace(pose, opacity=(
                ease_in_out_cubic(raw_progress)
                if entering else 1.0 - ease_in_out_cubic(raw_progress)
            ))
        pose = pose.combined(loop)
        if pose.opacity <= 0.0:
            transparent = QImage(image.size(), QImage.Format.Format_RGBA8888)
            transparent.fill(0)
            return transparent
        result = QImage(image.size(), QImage.Format.Format_RGBA8888)
        result.fill(0)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(pose.opacity)
        painter.translate(width / 2.0 + pose.dx, height / 2.0 + pose.dy)
        painter.rotate(pose.rotation)
        painter.scale(pose.scale * pose.scale_x, pose.scale)
        painter.translate(-width / 2.0, -height / 2.0)
        painter.drawImage(QPointF(0.0, 0.0), image)
        painter.end()
        return result

    @staticmethod
    def _frame_progress_message(
        layer_number: int, layer_count: int, frame_number: int, layer_frames: int,
        completed_frames: int, total_frames: int, elapsed_seconds: float,
    ) -> str:
        """Build a detailed and stable progress description for the export dialog."""
        percent = 100.0 * completed_frames / max(1, total_frames)
        message = (
            f"Visualizer {layer_number}/{layer_count} · frame "
            f"{frame_number:,}/{layer_frames:,} · {percent:.1f}%"
        )
        if completed_frames >= 12 and elapsed_seconds > 0.2:
            rate = completed_frames / elapsed_seconds
            remaining = max(0.0, total_frames - completed_frames) / max(0.001, rate)
            message += f" · about {PythonVisualizerRenderer._format_duration(remaining)} remaining"
        return message

    @staticmethod
    def _combined_layer_progress_message(
        completed_frames: Sequence[int], total_frames: Sequence[int],
    ) -> str:
        """Show every parallel visualizer's latest state in one stable message."""
        layer_count = len(total_frames)
        lines = []
        for index, (completed, total) in enumerate(
            zip(completed_frames, total_frames, strict=True),
        ):
            bounded_total = max(1, total)
            bounded_completed = min(bounded_total, max(0, completed))
            percent = 100.0 * bounded_completed / bounded_total
            lines.append(
                f"Visualizer {index + 1}/{layer_count} · frame "
                f"{bounded_completed:,}/{bounded_total:,} · {percent:.1f}%"
            )
        completed_total = sum(max(0, value) for value in completed_frames)
        frame_total = sum(max(1, value) for value in total_frames)
        overall_percent = 100.0 * completed_total / max(1, frame_total)
        lines.append(
            f"All visualizers · frame {completed_total:,}/{frame_total:,}"
            f" · {overall_percent:.1f}%"
        )
        return "\n".join(lines)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, round(seconds))
        minutes, seconds_part = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds_part:02d}"
        return f"{minutes:02d}:{seconds_part:02d}"

    @staticmethod
    def _resample_levels(values: np.ndarray, count: int) -> np.ndarray:
        positions = np.linspace(0, len(values) - 1, count)
        return np.interp(positions, np.arange(len(values)), values)

    @staticmethod
    def process_level_sequence(all_levels: np.ndarray, overlay: object,
                               count: int) -> np.ndarray:
        """Apply gain, gate, dynamics, smoothing, and temporal response controls."""
        levels = np.asarray(all_levels, dtype=np.float32)
        if levels.ndim == 1:
            levels = levels.reshape(1, -1)
        count = max(4, min(96, int(count)))
        kind = str(getattr(overlay, "kind", "visualizer"))
        waveform = kind == "waveform"
        sensitivity = max(0.25, min(3.0, float(getattr(overlay, "sensitivity", 1.2))))
        gate = max(0.0, min(0.1, float(getattr(overlay, "noise_gate", 0.003))))
        minimum = max(0.0, min(0.5, float(getattr(overlay, "min_level", 0.0))))
        maximum = max(minimum, min(1.0, float(getattr(overlay, "max_level", 0.96))))
        curve = max(0.25, min(3.0, float(getattr(overlay, "curve", 0.9))))
        spatial = max(0.0, min(1.0, float(getattr(overlay, "smoothing", 0.18))))

        # Overall response remains a useful master control.  Attack and release
        # then independently shape upward and downward motion around its legacy
        # default of 0.22.
        reactivity = max(0.05, min(0.8, float(getattr(overlay, "reactivity", 0.22))))
        response_scale = reactivity / 0.22
        attack = max(0.01, min(1.0, float(getattr(overlay, "attack", 0.55))))
        release = max(0.01, min(1.0, float(getattr(overlay, "release", 0.16))))
        attack_alpha = 1.0 - (1.0 - attack) ** response_scale
        release_alpha = 1.0 - (1.0 - release) ** response_scale

        result = np.zeros((len(levels), count), dtype=np.float32)
        previous = np.zeros(count, dtype=np.float32)
        if not waveform:
            previous.fill(minimum)
        for frame_index, raw_values in enumerate(levels):
            values = PythonVisualizerRenderer._resample_levels(raw_values, count).astype(
                np.float32, copy=False,
            )
            values *= sensitivity
            values[np.abs(values) < gate] = 0.0
            if waveform:
                values = np.sign(values) * np.power(
                    np.clip(np.abs(values), 0.0, 1.0), curve,
                ) * maximum
            else:
                values = np.log1p(np.maximum(values, 0.0) * 30.0) / math.log(31.0)
                values = np.power(np.clip(values, 0.0, 1.0), curve)
                values = minimum + values * (maximum - minimum)
            if spatial > 0.0 and count > 2:
                padded = np.pad(values, (1, 1), mode="edge")
                neighbours = (padded[:-2] + 2.0 * padded[1:-1] + padded[2:]) / 4.0
                values = values * (1.0 - spatial) + neighbours * spatial
            rising = np.abs(values) > np.abs(previous) if waveform else values > previous
            alpha = np.where(rising, attack_alpha, release_alpha).astype(np.float32)
            previous = previous + (values - previous) * alpha
            result[frame_index] = previous
        return result

    @staticmethod
    def process_meter_sequence(raw_levels: np.ndarray, overlay: object,
                               fps: int) -> tuple[np.ndarray, np.ndarray]:
        """Apply true RMS gain, ballistics, and peak hold to meter channels."""
        raw = np.asarray(raw_levels, dtype=np.float32)
        if raw.ndim == 1:
            raw = raw.reshape(-1, 1)
        mode = str(getattr(overlay, "level_meter_mode", "stereo"))
        if mode == "mono" and raw.shape[1] > 1:
            raw = np.mean(raw, axis=1, keepdims=True)
        elif mode != "mono" and raw.shape[1] == 1:
            raw = np.repeat(raw, 2, axis=1)
        sensitivity = max(
            0.25, min(4.0, float(getattr(overlay, "level_meter_sensitivity", 1.2)))
        )
        minimum = max(
            0.0, min(0.5, float(getattr(overlay, "level_meter_min_level", 0.0)))
        )
        maximum = max(
            minimum, min(1.0, float(getattr(overlay, "level_meter_max_level", 1.0)))
        )
        target = np.sqrt(np.clip(raw * sensitivity * 1.8, 0.0, 1.0))
        target = minimum + target * (maximum - minimum)
        attack = max(0.01, min(1.0, float(getattr(overlay, "level_meter_attack", 0.65))))
        release = max(0.01, min(1.0, float(getattr(overlay, "level_meter_release", 0.18))))
        hold_frames = max(
            0, round(float(getattr(overlay, "level_meter_peak_hold", 0.35)) * max(1, fps))
        )
        peak_decay = max(
            0.05, min(3.0, float(getattr(overlay, "level_meter_peak_decay", 0.7)))
        ) / max(1, fps)
        levels = np.zeros_like(target, dtype=np.float32)
        peaks = np.zeros_like(target, dtype=np.float32)
        previous = np.full(target.shape[1], minimum, dtype=np.float32)
        peak = previous.copy()
        hold = np.zeros(target.shape[1], dtype=np.int32)
        for frame_index, values in enumerate(target):
            alpha = np.where(values > previous, attack, release).astype(np.float32)
            previous = previous + (values - previous) * alpha
            levels[frame_index] = previous
            raised = previous >= peak
            peak = np.where(raised, previous, peak)
            hold = np.where(raised, hold_frames, np.maximum(0, hold - 1))
            peak = np.where(hold > 0, peak, np.maximum(previous, peak - peak_decay))
            peaks[frame_index] = peak
        return levels, peaks

    @staticmethod
    def _draw_frame(width: int, height: int, overlay: object, levels: np.ndarray,
                    frame_index: int = 0, channel_values: tuple[float, ...] | None = None,
                    image_buffer: QImage | None = None, frame_rate: int = 60,
                    peak_values: tuple[float, ...] | None = None) -> QImage:
        image_format = QImage.Format.Format_RGBA8888
        if (image_buffer is not None and image_buffer.width() == width
                and image_buffer.height() == height and image_buffer.format() == image_format):
            image = image_buffer
        else:
            image = QImage(width, height, image_format)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, width, height)
        color = QColor(str(getattr(overlay, "color", "#FFFFFF")))
        color.setAlphaF(max(0.0, min(1.0, float(getattr(overlay, "opacity", 1.0)))))
        style = str(getattr(overlay, "style", "bars"))
        kind = str(getattr(overlay, "kind", "visualizer"))
        effect_style = str(getattr(overlay, "effect_style", style))
        line_width = max(1.0, float(getattr(overlay, "line_width", 3.0)))
        count = len(levels)
        gap = max(1.0, rect.width() * 0.012 / count)
        bar_width = max(1.0, (rect.width() - gap * (count - 1)) / count)
        painter.setPen(QPen(color, line_width))
        mask_shape = str(getattr(overlay, "mask_shape", "none"))
        if mask_shape != "none":
            painter.setClipPath(mask_path(rect, mask_shape))
        if kind == "waveform":
            path = QPainterPath(QPointF(rect.left(), rect.center().y()))
            for index, level in enumerate(levels):
                x = rect.left() + index * rect.width() / max(1, count - 1)
                amplitude = float(level) * rect.height() * 0.45
                if effect_style == "filled":
                    y = rect.center().y() - amplitude
                else:
                    y = rect.center().y() - amplitude
                path.lineTo(x, y)
            if effect_style == "filled":
                path.lineTo(rect.right(), rect.bottom())
                path.lineTo(rect.left(), rect.bottom())
                path.closeSubpath()
                fill = QColor(color)
                fill.setAlpha(max(32, color.alpha() // 2))
                painter.fillPath(path, fill)
            painter.drawPath(path)
        elif kind == "level_meter":
            meter_values = channel_values or (
                (float(np.percentile(levels, 80)),)
                if str(getattr(overlay, "level_meter_mode", "stereo")) == "mono" else
                (float(np.percentile(levels[::2], 80)), float(np.percentile(levels[1::2], 80)))
            )
            painter.save()
            painter.setOpacity(max(
                0.0, min(1.0, float(getattr(overlay, "opacity", 1.0)))
            ))
            paint_level_meter(
                painter,
                rect,
                meter_values,
                peak_values,
                style=str(getattr(overlay, "level_meter_style", effect_style)),
                orientation=str(getattr(overlay, "level_meter_orientation", "vertical")),
                segments=int(getattr(overlay, "level_meter_segments", 16)),
                gap=float(getattr(overlay, "level_meter_gap", 4.0)),
                track_color=str(getattr(overlay, "level_meter_track_color", "#263244")),
                low_color=str(getattr(overlay, "level_meter_low_color", "#22C55E")),
                mid_color=str(getattr(overlay, "level_meter_mid_color", "#FACC15")),
                high_color=str(getattr(overlay, "level_meter_high_color", "#EF4444")),
                show_peak=bool(getattr(overlay, "level_meter_show_peak", True)),
            )
            painter.restore()
        elif kind == "particles":
            paint_particles(
                painter,
                rect,
                style=effect_style,
                color=str(getattr(overlay, "color", "#FFFFFF")),
                secondary_color=str(getattr(overlay, "particle_secondary_color", "#7DD3FC")),
                density=int(getattr(overlay, "density", 42)),
                speed=float(getattr(overlay, "speed", 1.0)),
                minimum_size=float(getattr(overlay, "particle_min_size", 1.0)),
                maximum_size=float(getattr(overlay, "particle_max_size", 4.0)),
                particle_opacity=(
                    float(getattr(overlay, "particle_opacity", 0.62))
                    * float(getattr(overlay, "opacity", 1.0))
                ),
                direction=float(getattr(overlay, "particle_direction", -90.0)),
                drift=float(getattr(overlay, "particle_drift", 0.25)),
                twinkle=float(getattr(overlay, "particle_twinkle", 0.25)),
                glow=float(getattr(overlay, "particle_glow", 0.2)),
                seed=int(getattr(overlay, "particle_seed", 17)),
                time_seconds=frame_index / max(1, frame_rate),
            )
        elif style in {"line", "wave"}:
            path = QPainterPath(QPointF(rect.left(), rect.center().y()))
            for index, level in enumerate(levels):
                x = rect.left() + index * rect.width() / max(1, count - 1)
                y = rect.center().y() - (float(level) - 0.5) * rect.height() * 0.82
                if style == "wave":
                    y = rect.center().y() - math.sin(index * 0.42) * float(level) * rect.height() * 0.36
                path.lineTo(x, y)
            painter.drawPath(path)
        elif style == "radial":
            paint_radial_bars(
                painter, rect, [float(level) for level in levels], color, line_width,
                float(getattr(overlay, "inner_radius", 0.55)),
            )
        elif style == "arc":
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for index, level in enumerate(levels):
                inset = index * min(rect.width(), rect.height()) / max(1, count * 3.2)
                arc_rect = rect.adjusted(inset, inset, -inset, -inset)
                arc_color = QColor(color)
                arc_color.setAlpha(max(45, int(color.alpha() * float(level))))
                painter.setPen(QPen(arc_color, max(1.0, line_width * 0.75)))
                painter.drawArc(arc_rect, 210 * 16, int(120 * 16 * float(level)))
        else:
            painter.setPen(QPen(color, 0))
            for index, level in enumerate(levels):
                value = float(level)
                x = rect.left() + index * (bar_width + gap)
                draw_color = QColor.fromHsv(int(300 * index / max(1, count - 1)), 210, 245) if style == "spectrum" else color
                painter.setBrush(draw_color)
                if style == "led":
                    segments = 8
                    segment_gap = max(1.0, rect.height() * 0.025)
                    segment_height = (rect.height() - segment_gap * (segments - 1)) / segments
                    active = max(1, round(value * segments))
                    for segment in range(active):
                        y = rect.bottom() - (segment + 1) * segment_height - segment * segment_gap
                        led_color = QColor(color)
                        led_color.setAlpha(max(80, color.alpha() - segment * 9))
                        painter.setBrush(led_color)
                        painter.drawRoundedRect(QRectF(x, y, bar_width, segment_height), 2, 2)
                elif style == "center":
                    bar_height = rect.height() * value
                    painter.drawRoundedRect(QRectF(x, rect.bottom() - bar_height, bar_width, bar_height), bar_width / 2, bar_width / 2)
                elif style == "mirror":
                    bar_height = rect.height() * value * 0.48
                    painter.drawRoundedRect(QRectF(x, rect.center().y() - bar_height, bar_width, bar_height), bar_width / 2, bar_width / 2)
                    painter.drawRoundedRect(QRectF(x, rect.center().y(), bar_width, bar_height), bar_width / 2, bar_width / 2)
                elif style == "dots":
                    dot_size = max(3.0, min(bar_width * 1.35, rect.height() * 0.16))
                    for dot_index in range(max(1, int(value * 7))):
                        y = rect.bottom() - dot_size - dot_index * (dot_size + 3)
                        painter.drawEllipse(QRectF(x, y, dot_size, dot_size))
                else:
                    bar_height = rect.height() * value
                    draw_width = bar_width * 0.62 if style == "capsule" else bar_width
                    radius = draw_width / 2
                    painter.drawRoundedRect(QRectF(x + (bar_width - draw_width) / 2, rect.center().y() - bar_height / 2, draw_width, bar_height), radius, radius)
        painter.end()
        return image
