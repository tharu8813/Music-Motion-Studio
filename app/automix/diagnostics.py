"""AutoMix transition diagnostics: what was planned, and how the mix measures.

``transition_rows`` reads only a CompiledRenderPlan (the planner's
``dsp_reasons``/``details``), so Preview's details panel, the diagnostics
export and ``tools/automix_listening_report.py`` all describe exactly the plan
that was rendered -- nothing is re-analyzed here.

``window_metrics`` is an objective proxy for listening tests on a rendered
mix: how a transition window's level and low end compare with the solo audio
around it. It flags energy holes/spikes and low-end build-up (two bass lines
at once); it cannot judge taste, so it complements listening, never replaces it.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping

import numpy as np

from app.automix.models import HANDOFF_LEVEL_BLOCK_SECONDS
from app.automix.renderer import transition_dsp_style
from app.timeline.render_plan import CompiledRenderPlan

_GAP_EPSILON = 1e-6


def transition_rows(plan: CompiledRenderPlan, titles: Mapping[str, str] | None = None) -> list[dict[str, object]]:
    """One row per junction between consecutive clips, transition or not."""
    titles = titles or {}
    clips = plan.audio.clips
    by_pair = {(t.clip_a, t.clip_b): t for t in plan.audio.transitions}
    rows: list[dict[str, object]] = []
    for index, (outgoing, incoming) in enumerate(zip(clips, clips[1:]), start=1):
        transition = by_pair.get((outgoing.clip_id, incoming.clip_id))
        row: dict[str, object] = {
            "index": index,
            "from": titles.get(outgoing.track_id, outgoing.track_id),
            "to": titles.get(incoming.track_id, incoming.track_id),
        }
        if transition is None:
            gap = incoming.timeline_start - outgoing.timeline_end
            row.update({
                "timeline_start": incoming.timeline_start, "duration": 0.0,
                "type": "gap" if gap > _GAP_EPSILON else "sequential",
                "dsp": "", "incoming_rate": incoming.playback_rate, "reasons": "",
            })
        else:
            style = transition_dsp_style(transition)
            row.update({
                "timeline_start": transition.timeline_start, "duration": transition.duration,
                "type": transition.type.value, "dsp": ("eq" if style is not None and transition.band_windows is not None
                        and dict(transition.details).get("manual_style") == "eq"
                        else style.value if style else "legacy"),
                "incoming_rate": incoming.playback_rate,
                **dict(transition.details),
                "reasons": "; ".join(reason.strip() for reason in transition.dsp_reasons),
            })
        rows.append(row)
    return rows


def _plain(value: object) -> object:
    if isinstance(value, float):
        return round(value, 4)
    return value


def rows_to_json(rows: list[dict[str, object]]) -> str:
    return json.dumps([{key: _plain(value) for key, value in row.items()} for row in rows],
                      indent=2, ensure_ascii=False)


def rows_to_csv(rows: list[dict[str, object]]) -> str:
    fields: list[str] = []
    for row in rows:
        fields.extend(key for key in row if key not in fields)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if value is None else _plain(value) for key, value in row.items()})
    return buffer.getvalue()


BLOCK_SECONDS = HANDOFF_LEVEL_BLOCK_SECONDS
CONTEXT_SECONDS = 8.0
LOW_BAND_HZ = 150.0


def window_metrics(
    samples: np.ndarray, sample_rate: int, start: float, duration: float,
    context: float = CONTEXT_SECONDS,
) -> dict[str, float]:
    """Level/low-end of ``[start, start + duration)`` in dB relative to the solo context around it.

    ``samples`` is mono float PCM. ``level_dip_db``/``level_peak_db`` are the
    quietest/loudest block (up to 0.5 s) against the context's mean power;
    ``level_mean_db`` the whole window; ``low_excess_db`` the power
    below 150 Hz against the context's (positive: low-end build-up). NaN when
    there is no context to compare with. A cut samples 25 ms on either side.
    ``handoff_dip_db`` uses a half-second window even across a short cut.
    """
    if duration == 0.0:
        start -= 0.025
        duration = 0.05
    first = max(0, round(start * sample_rate))
    last = min(len(samples), round((start + duration) * sample_rate))
    block = max(1, min(int(BLOCK_SECONDS * sample_rate), last - first))
    frequencies = np.fft.rfftfreq(block, 1.0 / sample_rate)
    low_mask = frequencies <= LOW_BAND_HZ

    def blocks(begin: float, end: float) -> tuple[np.ndarray, np.ndarray]:
        first, last = max(0, round(begin * sample_rate)), min(len(samples), round(end * sample_rate))
        count = max(0, (last - first) // block)
        if count == 0:
            return np.empty(0), np.empty(0)
        frames = samples[first:first + count * block].reshape(count, block).astype(np.float64)
        spectrum = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        return np.mean(frames ** 2, axis=1), np.sum(spectrum[:, low_mask], axis=1) / block

    window_power, window_low = blocks(start, start + duration)
    before = blocks(start - context, start)
    after = blocks(start + duration, start + duration + context)
    context_power = np.concatenate([before[0], after[0]])
    context_low = np.concatenate([before[1], after[1]])
    nan = float("nan")
    if len(window_power) == 0 or len(context_power) == 0:
        return {"level_dip_db": nan, "level_peak_db": nan, "level_mean_db": nan, "low_excess_db": nan,
                "handoff_dip_db": nan}

    def db(value: float) -> float:
        return float(10.0 * np.log10(max(value, 1e-12)))

    reference = db(float(np.mean(context_power)))
    # Sliding half-second windows detect a lull even when it falls between
    # the fixed blocks, or mostly outside a 50 ms downbeat cut.
    padding = max(0.0, (BLOCK_SECONDS - duration) / 2)
    handoff = samples[max(0, round((start - padding) * sample_rate)):
                      min(len(samples), round((start + duration + padding) * sample_rate))]
    width = min(len(handoff), max(1, round(BLOCK_SECONDS * sample_rate)))
    prefix = np.insert(np.cumsum(np.square(handoff, dtype=np.float64)), 0, 0.0)
    handoff_min = float(np.min((prefix[width:] - prefix[:-width]) / width))
    return {
        "level_dip_db": db(float(np.min(window_power))) - reference,
        "level_peak_db": db(float(np.max(window_power))) - reference,
        "level_mean_db": db(float(np.mean(window_power))) - reference,
        "low_excess_db": db(float(np.mean(window_low))) - db(float(np.mean(context_low))),
        "handoff_dip_db": db(handoff_min) - reference,
    }
