"""Pixel-exact particle output and deterministic cache behavior for export."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

import app.utils.particle_painter as particle


def _paint(
    *, cached: bool, style: str, time_seconds: float,
    seed: int = 9031, density: int = 160,
    rect: QRectF | None = None, glow: float = 0.55,
) -> bytes:
    image = QImage(320, 200, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        with patch.object(particle, "_DISABLE_PARTICLE_STATIC_CACHE", not cached):
            particle.paint_particles(
                painter, rect or QRectF(7.5, 3.25, 304.0, 184.0),
                style=style, color="#00aaff",
                secondary_color="#ff44aa", density=density,
                speed=1.6, minimum_size=1.5, maximum_size=11.0,
                particle_opacity=0.72, direction=33.0, drift=0.75,
                twinkle=0.8, glow=glow, seed=seed,
                time_seconds=time_seconds,
            )
    finally:
        painter.end()
    return memoryview(image.constBits()).tobytes()


class ParticleStaticCacheTests(TestCase):
    def test_all_styles_match_uncached_output_byte_for_byte(self):
        for style in ("noise", "snow", "stars", "bokeh", "confetti", "neon", "unknown"):
            for seconds in (0.0, 0.033333, 3.25):
                with self.subTest(style=style, seconds=seconds):
                    self.assertEqual(
                        _paint(cached=True, style=style, time_seconds=seconds),
                        _paint(cached=False, style=style, time_seconds=seconds),
                    )

    def test_seed_density_and_rectangle_variants_are_exact(self):
        for seed, density, rect in (
            (0, 4, QRectF(0, 0, 320, 200)),
            (-5, 31, QRectF(16, 28, 80.5, 90.25)),
            (2**31 - 1, 500, QRectF(0, 0, 320, 200)),
        ):
            with self.subTest(seed=seed, density=density):
                self.assertEqual(
                    _paint(cached=True, style="stars", time_seconds=14.2,
                           seed=seed, density=density, rect=rect),
                    _paint(cached=False, style="stars", time_seconds=14.2,
                           seed=seed, density=density, rect=rect),
                )

    def test_immutable_cache_hit_and_bounded_storage(self):
        particle._particle_static_values.cache_clear()
        first = particle._particle_static_values(50, 17)
        second = particle._particle_static_values(50, 17)
        self.assertIs(first, second)
        self.assertEqual(len(first), 50)
        self.assertIsInstance(first, tuple)
        self.assertIsInstance(first[0], tuple)
        self.assertGreaterEqual(particle._particle_static_values.cache_info().hits, 1)
        for seed in range(70):
            particle._particle_static_values(6, seed)
        self.assertLessEqual(particle._particle_static_values.cache_info().currsize, 64)

    def test_cached_version_never_calls_random_during_warmed_paint(self):
        particle._particle_static_values.cache_clear()
        particle._particle_static_values(15, 301)
        with patch.object(particle, "_DISABLE_PARTICLE_STATIC_CACHE", False), patch.object(
            particle, "_random_unit", side_effect=AssertionError("unexpected random calculation"),
        ):
            self.assertEqual(
                len(_paint(cached=True, style="noise", time_seconds=9,
                           seed=301, density=15)),
                320 * 200 * 4,
            )


if __name__ == "__main__":
    import unittest
    unittest.main()
