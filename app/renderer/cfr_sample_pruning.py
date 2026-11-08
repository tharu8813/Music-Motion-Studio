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
