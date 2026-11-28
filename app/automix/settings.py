"""Runtime settings for AutoMix analysis infrastructure.

Deliberately not wired into AppSettings/project JSON yet -- Phase 1 has no
AutoMix UI and no new persisted project schema (roadmap 1.2, 1.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from app.automix.overrides import TransitionOverride, pair_key, parse_overrides


@dataclass(frozen=True, slots=True)
class AutoMixAnalysisSettings:
    """How AnalysisService should run, independent of which provider it uses."""

    provider_id: str = "basic"
    use_cache: bool = True
    max_workers: int | None = None


@dataclass(frozen=True, slots=True)
class AutoMixTransitionSettings:
    """Conservative defaults for transition candidate generation (Phase 3).

    ``enabled`` gates whether a planner should attempt AutoMix transitions
    at all -- kept here rather than as an implicit "settings object
    exists" convention, so a caller can hold one disabled settings
    instance around without it doing anything.
    """

    enabled: bool = False
    preferred_bars: int = 8
    max_tempo_change_percent: float = 8.0
    max_bridge_tempo_percent: float = 16.0
    """Two reliable beat grids further apart than ``max_tempo_change_percent`` but
    within this are still beat-matched, with a longer ramp (planner.BRIDGE_RAMP_BARS)."""
    min_transition_seconds: float = 2.0
    max_transition_seconds: float = 20.0
    allow_half_double_tempo: bool = True
    fallback_crossfade_seconds: float = 3.0
    overrides: tuple[tuple[str, TransitionOverride], ...] = ()
    """Manually set junctions, as (``pair_key``, override) pairs; every other
    junction stays automatic. A tuple so the settings stay hashable."""
    allow_pitch_shift: bool = True
    allow_tape_stop: bool = True
    """Automatic effects available to the selected FFmpeg build."""

    def override_for(self, outgoing_track_id: str, incoming_track_id: str) -> TransitionOverride | None:
        key = pair_key(outgoing_track_id, incoming_track_id)
        return next((override for pair, override in self.overrides if pair == key), None)

    def with_overrides(self, overrides: Mapping[str, object] | None) -> "AutoMixTransitionSettings":
        """A copy planning ``overrides`` (TransitionOverride values or their saved dicts)."""
        pairs = []
        for key, value in (overrides or {}).items():
            if isinstance(value, TransitionOverride):
                pairs.append((key, value))
            else:
                pairs.extend(parse_overrides({key: value}).items())
        ordered = tuple(sorted(pairs, key=lambda pair: pair[0]))
        # Unchanged overrides keep this very object: a project with no manual
        # junction plans with AUTOMIX_SETTINGS itself.
        return self if ordered == self.overrides else replace(self, overrides=ordered)


AUTOMIX_SETTINGS = AutoMixTransitionSettings(enabled=True)
"""The one tuned set Preview and Export plan every AutoMix project with. Projects
used to pick a listening preset (smooth/energetic/dj); the style is now always
automatic, and a project saved with another preset loads as this one."""


def automix_settings_for(project_settings: object) -> AutoMixTransitionSettings:
    """AUTOMIX_SETTINGS plus the project's manual junctions -- what Preview and Export plan with."""
    return AUTOMIX_SETTINGS.with_overrides(getattr(project_settings, "automix_overrides", None))
