"""Section timbre: what each second of a track sounds like, for choosing a transition.

Three 0..1 curves at TIMBRE_HOP_SECONDS, computed with numpy from the mono
signal the structure provider already decoded (no extra decode):

- ``bass``: the share of spectral energy below BASS_CUTOFF_HZ -- a bass-heavy
  stretch muddies when it overlaps another one.
- ``brightness``: spectral centroid over BRIGHTNESS_CEILING_HZ.
- ``percussive``: onset strength -- how much each octave band's level rises
  from frame to frame, in dB -- scaled so a steady drum groove sits near 1 and
  a held pad or a lone voice near 0. In dB, so it compares across tracks.
"""

from __future__ import annotations

import numpy as np

from app.automix.models import BAND_POWER_HOP_SECONDS, HIGH_CROSSOVER_HZ, LOW_CROSSOVER_HZ

TIMBRE_HOP_SECONDS = 1.0
FRAME_SIZE = 2048
FRAME_HOP = 512
BASS_CUTOFF_HZ = 150.0
BRIGHTNESS_CEILING_HZ = 5000.0
ONSET_BAND_EDGES_HZ = (100.0, 200.0, 400.0, 800.0, 1600.0, 3200.0, 6400.0)
ONSET_CAP_DB = 12.0
"""One band's rise per frame counts up to this, so a single hit cannot dominate a second."""
PERCUSSIVE_FLOOR_DB = 0.4
PERCUSSIVE_SPAN_DB = 1.0
"""Mean onset strength (dB per frame) maps FLOOR -> 0 and FLOOR + SPAN -> 1. On 30
real pop tracks it spans 0.3 (acoustic outros) to 1.9 (dense drum grooves)."""
SILENCE_POWER = 1e-8


def band_power_curve(signal: np.ndarray, sample_rate: int) -> tuple[tuple[float, float, float], ...]:
    """Use each block's measured power and spectrum to estimate energy in the mix bands."""
    hop = max(1, round(BAND_POWER_HOP_SECONDS * sample_rate))
    fft_size = 1 << (hop - 1).bit_length()
    window = np.hanning(hop).astype(np.float32)
    frequencies = np.fft.rfftfreq(fft_size, 1 / sample_rate)
    low, high = np.searchsorted(frequencies, (LOW_CROSSOVER_HZ, HIGH_CROSSOVER_HZ))
    if low == high or high == len(frequencies):
        return ()
    blocks = []
    batch_size = 128 * hop
    for start in range(0, len(signal), batch_size):
        chunk = signal[start:start + batch_size]
        sample_count = len(chunk)
        if sample_count % hop:
            chunk = np.pad(chunk, (0, hop - sample_count % hop))
        frames = chunk.reshape(-1, hop)
        lengths = np.full(len(frames), hop)
        lengths[-1] = sample_count - (len(frames) - 1) * hop
        power = np.sum(np.square(frames), axis=1, dtype=np.float64) / lengths
        spectrum = np.abs(np.fft.rfft(frames * window, n=fft_size, axis=1)) ** 2
        spectrum[:, 0] *= 0.5
        spectrum[:, -1] *= 0.5
        band_energy = np.add.reduceat(spectrum, (0, low, high), axis=1)
        band_power = band_energy * (power / np.maximum(1e-20, band_energy.sum(axis=1)))[:, None]
        blocks.extend(tuple(row) for row in np.round(band_power, 9).tolist())
    return tuple(blocks)


def timbre_curves(signal: np.ndarray, sample_rate: int) -> tuple[tuple[float, ...], ...]:
    """(bass, brightness, percussive) curves, one value per TIMBRE_HOP_SECONDS.

    Processed one hop at a time: a whole track's spectrogram would be ~170 MB.
    """
    signal = np.asarray(signal, dtype=np.float32)
    hop_samples = max(FRAME_HOP, round(TIMBRE_HOP_SECONDS * sample_rate))
    window = np.hanning(FRAME_SIZE).astype(np.float32)
    frequencies = np.fft.rfftfreq(FRAME_SIZE, 1.0 / sample_rate)
    low = frequencies < BASS_CUTOFF_HZ
    edges = [0.0, *ONSET_BAND_EDGES_HZ, sample_rate / 2 + 1]
    bands = np.stack([(frequencies >= a) & (frequencies < b) for a, b in zip(edges, edges[1:])], axis=1).astype(float)
    bass, brightness, percussive = [], [], []
    previous = None
    for start in range(0, len(signal) - FRAME_SIZE + 1, hop_samples):
        chunk = signal[start:min(len(signal), start + hop_samples + FRAME_SIZE - FRAME_HOP)]
        count = (len(chunk) - FRAME_SIZE) // FRAME_HOP + 1
        frames = np.lib.stride_tricks.as_strided(
            chunk, shape=(count, FRAME_SIZE), strides=(chunk.strides[0] * FRAME_HOP, chunk.strides[0]),
        ) * window
        power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        total = power.sum(axis=1)
        audible = total > SILENCE_POWER * FRAME_SIZE
        safe = np.maximum(total, 1e-20)
        # Onset strength: rises of each octave band's level in dB (loudness-independent).
        # A band is floored at -40 dB under the frame: a nearly empty band's dB
        # value is noise, and read a held tone as drums.
        shape = 10.0 * np.log10(np.maximum(power @ bands, safe[:, None] * 1e-4))
        stacked = shape if previous is None else np.vstack([previous, shape])
        flux = np.minimum(ONSET_CAP_DB, np.maximum(0.0, np.diff(stacked, axis=0))).mean(axis=1)
        flux = flux[-count:] if previous is not None else np.concatenate([[0.0], flux])
        flux[~audible] = 0.0
        previous = shape[-1:]
        if audible.any():
            bass.append(float((power[audible][:, low].sum(axis=1) / safe[audible]).mean()))
            brightness.append(float(min(1.0, ((power[audible] * frequencies).sum(axis=1) / safe[audible]).mean()
                                        / BRIGHTNESS_CEILING_HZ)))
        else:
            bass.append(0.0)
            brightness.append(0.0)
        percussive.append(float(np.clip((flux.mean() - PERCUSSIVE_FLOOR_DB) / PERCUSSIVE_SPAN_DB, 0.0, 1.0)))
    return tuple(tuple(round(value, 4) for value in curve) for curve in (bass, brightness, percussive))


def mean_over(curve: tuple[float, ...], start: float, end: float) -> float | None:
    """``curve``'s mean over ``[start, end)`` seconds (None: no data there)."""
    first = max(0, int(start / TIMBRE_HOP_SECONDS))
    if first >= len(curve):
        return None
    last = min(len(curve), max(first + 1, int(np.ceil(end / TIMBRE_HOP_SECONDS))))
    return float(np.mean(curve[first:last]))
