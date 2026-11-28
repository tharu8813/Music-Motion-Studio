"""Estimate power changes from the same per-track EQ the renderer applies."""

from functools import lru_cache
from math import cos, pi, sin, sinh, sqrt, log

import numpy as np

from app.automix.renderer import HIGH_CROSSOVER_HZ, LOW_CROSSOVER_HZ, SAMPLE_RATE
from app.models.playlist import EQ_SHELF_Q, EQ_WIDTH_OCTAVES, PlaylistTrack, track_eq_filters


BandGains = tuple[float, float, float]
UNITY_POWER: BandGains = (1.0, 1.0, 1.0)
_BAND_LIMITS = (30, LOW_CROSSOVER_HZ, HIGH_CROSSOVER_HZ, 18000)
_FREQUENCIES = np.array([np.geomspace(lo, hi, 9) for lo, hi in zip(_BAND_LIMITS, _BAND_LIMITS[1:])])


def track_band_gains(track: PlaylistTrack) -> BandGains:
    volume = float(f"{track.volume_db:.2f}") if abs(track.volume_db) >= 0.05 else 0.0
    return _band_gains(volume, track_eq_filters(track.eq_db, track.bass_db))


@lru_cache(maxsize=256)
def _band_gains(volume: float, filters: tuple[tuple[str, int, float], ...]) -> BandGains:
    # We only know how much energy each band contains, so average the filter's
    # power response at log-spaced frequencies within that band.
    power = np.full(_FREQUENCIES.shape, 10 ** (volume / 10))
    for kind, frequency, db in filters:
        power *= eq_power_response(kind, frequency, db, _FREQUENCIES)
    low, mid, high = power.mean(axis=1)
    return float(low), float(mid), float(high)


def eq_power_response(kind: str, frequency: int, db: float, frequencies: np.ndarray) -> np.ndarray:
    """Biquad magnitude squared at nominal mix rate (W3C Audio EQ Cookbook)."""
    gain = 10 ** (db / 40)
    omega = 2 * pi * frequency / SAMPLE_RATE
    sine, cosine = sin(omega), cos(omega)
    if kind == "equalizer":
        alpha = sine * sinh(log(2) * EQ_WIDTH_OCTAVES / 2 * omega / sine)
        numerator = (1 + alpha * gain, -2 * cosine, 1 - alpha * gain)
        denominator = (1 + alpha / gain, -2 * cosine, 1 - alpha / gain)
    else:
        direction = 1 if kind == "lowshelf" else -1
        cosine *= direction  # the high shelf mirrors the low-shelf polynomial
        plus, minus = gain + 1, gain - 1
        slope = sqrt(gain) * sine / EQ_SHELF_Q
        numerator = (gain * (plus - minus * cosine + slope), 2 * direction * gain * (minus - plus * cosine),
                     gain * (plus - minus * cosine - slope))
        denominator = (plus + minus * cosine + slope, -2 * direction * (minus + plus * cosine),
                       plus + minus * cosine - slope)
    z = np.exp(-2j * np.pi * frequencies / SAMPLE_RATE)
    return np.abs(np.polynomial.polynomial.polyval(z, numerator)
                  / np.polynomial.polynomial.polyval(z, denominator)) ** 2
