"""Small policy boundary for export decisions shared by the UI and renderer."""

from __future__ import annotations

from pathlib import Path

from app.services.export_validation_service import (
    ExportValidationResult,
    select_export_work_mode,
    validate_export_output,
)


class ExportPolicy:
    """Stateless export decisions (work mode, output validation) shared by UI and renderer."""

    @staticmethod
    def choose_work_mode(width: int, height: int, fps: int) -> str:
        return select_export_work_mode(width, height, fps)

    @staticmethod
    def validate_output(
        output_path: str | Path,
        *,
        width: int,
        height: int,
        fps: int,
        duration_seconds: float,
        ffmpeg_executable: str | Path | None,
    ) -> ExportValidationResult:
        return validate_export_output(
            output_path,
            expected_width=width,
            expected_height=height,
            expected_fps=fps,
            expected_duration_seconds=duration_seconds,
            ffmpeg_executable=ffmpeg_executable,
        )
