"""Remove export samples that cannot emit another output CFR frame.

This is an opt-in transport optimization for independent spawned Canvas workers.
The expanded-frame target is stamped onto every retained sample, so the writer
does not have to infer the output boundary from regrouped floating-point sums.

All source timestamps and the complete timeline duration are retained. The
scene and its expensive state computations are never asked to draw a sample
that would not produce another encoded frame.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from math import floor

from app.renderer.export_timeline import ExportFrameSample


def uniform_cfr_frame_step(
    samples: Iterable[ExportFrameSample],
    output_fps: int,
    *,
    min_pipe_fps: int = 10,
) -> int:
    """Prove that every newly emitted picture occupies the same CFR frame count.

    Return an integer factor >=2 only if the *entire* lazily generated source
    schedule uses one exactly uniform repeat interval. Zero-output states are
    ignored; the first frame must start on the same cadence. The final frame
    count must also be divisible by the factor.

    This is deliberately conservative: mixed animation rates, track junctions,
    opacity changes between grid points, or sub-frame boundary shifts return 1
    and retain the existing full-FPS pipe. Checking the original cumulative
    clock (rather than grouping durations) preserves the writer's rounding.
    """
    if output_fps <= 0:
        raise ValueError("Output FPS must be positive.")
    elapsed = 0.0
    frame_count = 0
    frame_step = 0
    pictures = 0
    for sample in samples:
        if not (0.0 < sample.duration_seconds < float("inf")):
            return 1
        elapsed += sample.duration_seconds
        target_count = max(1, floor(elapsed * output_fps + 0.5))
        if target_count <= frame_count:
            continue
        produced = target_count - frame_count
        if frame_step == 0:
            if (produced <= 1
                    or output_fps % produced != 0
                    or output_fps // produced < min_pipe_fps):
                return 1
            frame_step = produced
        elif produced != frame_step:
            return 1
        frame_count = target_count
        pictures += 1
    return frame_step if pictures >= 2 and frame_count % frame_step == 0 else 1


@dataclass(slots=True)
class CfrPruningStats:
    """Stream-local counts for the parent process's profiler and UI logs."""

    input_samples: int = 0
    forwarded_samples: int = 0


def iter_cfr_emitting_samples(
    samples: Iterable[ExportFrameSample], fps: int,
    stats: CfrPruningStats | None = None,
) -> Iterator[ExportFrameSample]:
    """Yield only states that emit a CFR frame, with exact integer frame targets.

    The writer's original policy emits at least the first frame, then rounds
    the cumulative duration with floor(seconds * fps + 0.5). State selection
    follows the original sample-by-sample sum; stamped integer targets prevent
    regrouped floating point durations from moving half-frame boundaries.

    For every discarded state, accumulate its duration into the *next*
    producing state, because that next state owns the newly emitted pixels.
    Any trailing non-producing durations extend the last producing state.
    """
    if fps <= 0:
        raise ValueError("Output FPS must be positive.")

    cumulative = 0.0
    emitted_count = 0
    skipped_duration = 0.0
    pending: ExportFrameSample | None = None

    for sample in samples:
        if stats is not None:
            stats.input_samples += 1
        if sample.duration_seconds <= 0.0:
            raise ValueError("Every Canvas sample must have a positive duration.")
        cumulative += sample.duration_seconds
        target_count = max(1, floor(cumulative * fps + 0.5))

        if pending is None:
            pending = replace(sample, cfr_target_frame_count=target_count)
            emitted_count = target_count
        elif target_count > emitted_count:
            if stats is not None:
                stats.forwarded_samples += 1
            yield pending
            pending = replace(
                sample,
                duration_seconds=skipped_duration + sample.duration_seconds,
                cfr_target_frame_count=target_count,
            )
            skipped_duration = 0.0
            emitted_count = target_count
        else:
            skipped_duration += sample.duration_seconds

    if pending is not None:
        if skipped_duration:
            pending = replace(
                pending, duration_seconds=pending.duration_seconds + skipped_duration,
            )
        if stats is not None:
            stats.forwarded_samples += 1
        yield pending
