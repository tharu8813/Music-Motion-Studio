"""Fast, side-effect-free hardware advice for the export encoder."""

from __future__ import annotations

import os
import subprocess

from app.utils.subprocess_utils import hidden_process_kwargs


AUTO_VIDEO_ENCODER = "auto"
CPU_H264_ENCODER = "libx264"
NVIDIA_H264_ENCODER = "h264_nvenc"
NATIVE_NVENC_PRESETS = ("p1", "p3", "p5")
DEFAULT_NATIVE_NVENC_PRESET = "p3"


class VideoEncoderAdvisor:
    """Choose a safe H.264 encoder from locally detected display hardware."""

    @classmethod
    def automatic_encoder(cls) -> str:
        return NVIDIA_H264_ENCODER if cls.has_nvidia_gpu() else CPU_H264_ENCODER

    @classmethod
    def has_nvidia_gpu(cls) -> bool:
        """Detect NVIDIA adapters without opening a console or blocking on CIM."""
        if os.name == "nt" and cls._windows_registry_has_nvidia():
            return True
        # The executable is normally present only with an installed NVIDIA
        # display driver. Keep this as a short cross-platform/fallback check.
        try:
            completed = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=3, check=False,
                **hidden_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0 and bool(completed.stdout.strip())

    @staticmethod
    def _windows_registry_has_nvidia() -> bool:
        try:
            import winreg

            root_path = r"SYSTEM\CurrentControlSet\Control\Video"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path) as root:
                adapter_index = 0
                while True:
                    try:
                        adapter_key_name = winreg.EnumKey(root, adapter_index)
                    except OSError:
                        break
                    adapter_index += 1
                    for instance in ("0000", "0001"):
                        try:
                            with winreg.OpenKey(
                                root, f"{adapter_key_name}\\{instance}"
                            ) as adapter:
                                for value_name in (
                                    "DriverDesc", "HardwareInformation.AdapterString",
                                ):
                                    try:
                                        value, _kind = winreg.QueryValueEx(
                                            adapter, value_name
                                        )
                                    except OSError:
                                        continue
                                    if "nvidia" in str(value).lower():
                                        return True
                        except OSError:
                            continue
        except (ImportError, OSError):
            return False
        return False
