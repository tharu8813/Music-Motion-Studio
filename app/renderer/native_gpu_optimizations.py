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
    # Developer-only experiment: skip an identity keyframe Source context
    # when it cannot change pixels, transforms, or image filter state.
    skip_empty_keyframe_context: bool = False

    @classmethod
    def for_profile(cls, profile: str) -> "NativeGpuOptimizations":
        if profile == "standard":
            return cls()
        if profile in {"accelerated", "cpu_parallel", "automatic"}:
            return cls(
                pipelined=True,
                partial_bands=True,
                compact_bands=True,
                reuse_layers=True,
                # Separate, opt-in A/B profile. Do not silently change the
                # previously tested accelerated mode on existing installs.
                parallel_bgra=profile in {"cpu_parallel", "automatic"},
                adaptive_bgra=profile == "automatic",
            )
        raise ValueError(f"Unknown native GPU optimization profile: {profile!r}")
