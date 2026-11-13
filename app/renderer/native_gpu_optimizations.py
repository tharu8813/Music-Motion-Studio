"""Immutable per-export native GPU optimization settings.

The GUI passes these through its own Qt frame bridge and rendering worker.
CLI diagnostics with no explicit settings continue honoring the legacy
PLAYLIST_CANVAS_NATIVE_* experiment environment variables. This avoids
changing process-global environment state during a running GUI export.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NativeGpuOptimizations:
    """Enable only experimentally verified independent data-path features."""

    pipelined: bool = False
    partial_bands: bool = False
    compact_bands: bool = False
    reuse_layers: bool = False
    parallel_bgra: bool = False
    # Automatic mode offloads only sufficiently large, already-painted
    # QImages and never blocks the GUI waiting for converter slots.
    adaptive_bgra: bool = False
    # Only in an explicit developer CLI diagnostic: time actual Qt paint()
    # invocations by SourceType, without modifying normal source items.
    profile_canvas_source_paint: bool = False
    shared_packet_transport: bool = False
    parallel_packet_validation: bool = False
    reuse_canvas_samples: bool = False
    gpu_canvas_translation: bool = False
    gpu_album_cover_motion: bool = False
    gpu_particles: bool = False
    gpu_text: bool = False
    canvas_layer_limit: int = 8
    gpu_video: bool = False
    # Developer diagnostics until trimming improves total export time.
    trim_transparent_bands: bool = False
    # Exact bytes equality before SHA-256; developer P1 A/B only until timed.
    reuse_equal_pixels: bool = False
    gpu_radial: bool = False

    @classmethod
    def for_profile(cls, profile: str) -> "NativeGpuOptimizations":
        if profile == "standard":
            return cls()
        if profile in {"accelerated", "cpu_parallel", "automatic"}:
            automatic = profile == "automatic"
            return cls(
                pipelined=True,
                partial_bands=True,
                compact_bands=True,
                reuse_layers=True,
                # Separate, opt-in A/B profile. Do not silently change the
                # previously tested accelerated mode on existing installs.
                parallel_bgra=profile in {"cpu_parallel", "automatic"},
                adaptive_bgra=automatic,
                shared_packet_transport=automatic,
                parallel_packet_validation=automatic,
                reuse_canvas_samples=automatic,
                gpu_canvas_translation=automatic,
                gpu_album_cover_motion=automatic,
                gpu_particles=automatic,
                gpu_radial=automatic,
                gpu_video=automatic,
            )
        raise ValueError(f"Unknown native GPU optimization profile: {profile!r}")
