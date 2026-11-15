"""Selects an AnalysisProvider by id.

Both real AutoMix analysis call sites -- the interactive playlist-badge
analysis (app/controllers/automix_analysis_controller.py) and Preview/
Export rendering (app/renderer/ffmpeg_renderer.py) -- route through
``create_analysis_provider("auto", ...)``, so they can never resolve to a
different analyzer for the same playlist.

"auto" is Beat This! run by ONNX Runtime ("beat_this_onnx", shipped in the
installer: ~35 MB, no PyTorch) and the light basic analyzer (librosa) only
when onnxruntime or the model file is missing. The PyTorch analyzers (Beat
This!, Demucs vocals) made the installer ~1 GB and pinned the CPU for
minutes on a first analysis, so they are not shipped; "beat_this" stays
available by explicit id for tooling such as tools/automix_listening_report.py.

Imports stay lazy and per-branch: constructing a provider never imports
torch/onnxruntime itself (the providers only do that inside analyze()).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.automix.analysis.provider import AnalysisProvider

BASIC_PROVIDER_ID = "basic"
BEAT_THIS_PROVIDER_ID = "beat_this"
BEAT_THIS_ONNX_PROVIDER_ID = "beat_this_onnx"
AUTO_PROVIDER_ID = "auto"


def create_analysis_provider(provider_id: str, ffmpeg_executable: Path | None) -> AnalysisProvider:
    """Construct the AnalysisProvider named by ``provider_id``.

    Raises ValueError for anything other than "basic", "beat_this",
    "beat_this_onnx", or "auto" -- an unrecognized id is far more likely a
    settings typo or a wiring bug than an intentional choice, and silently
    degrading to "basic" would hide it (roadmap: "설정 typo나 wiring bug를
    숨길 수 있다").
    """
    if provider_id == AUTO_PROVIDER_ID:
        from app.automix.analysis.beat_this_onnx import onnx_beats_available

        provider_id = BEAT_THIS_ONNX_PROVIDER_ID if onnx_beats_available() else BASIC_PROVIDER_ID
    if provider_id == BEAT_THIS_ONNX_PROVIDER_ID:
        from app.automix.analysis.beat_this_onnx import BeatThisOnnxAnalysisProvider
        return BeatThisOnnxAnalysisProvider(ffmpeg_executable)
    if provider_id == BEAT_THIS_PROVIDER_ID:
        from app.automix.analysis.beat_this import BeatThisAnalysisProvider
        return BeatThisAnalysisProvider(ffmpeg_executable)
    if provider_id == BASIC_PROVIDER_ID:
        from app.automix.analysis.basic import BasicAnalysisProvider
        return BasicAnalysisProvider(ffmpeg_executable)
    raise ValueError(f"Unknown AutoMix analysis provider_id: {provider_id!r}")


def beat_this_available() -> bool:
    """Whether the optional Beat This! engine could be imported (explicit use only).

    Uses importlib.util.find_spec, which checks installed packages without
    importing them (so it never loads torch's runtime).
    """
    return (
        importlib.util.find_spec("torch") is not None
        and importlib.util.find_spec("beat_this") is not None
    )
