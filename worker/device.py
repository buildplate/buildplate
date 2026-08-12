"""Device selection for Buildplate worker — Apple Silicon, NVIDIA, or CPU."""

from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceInfo:
    kind: str  # "mps" | "cuda" | "cpu"
    torch_device: str  # e.g. "mps", "cuda:0", "cpu"
    label: str
    recommended: bool


def pick_device() -> DeviceInfo:
    """Prefer Metal on Apple Silicon, else CUDA, else CPU."""
    try:
        import torch
    except ImportError:
        return DeviceInfo("cpu", "cpu", "cpu (torch not installed)", recommended=False)

    # Apple Silicon Metal
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return DeviceInfo(
            kind="mps",
            torch_device="mps",
            label=f"Apple Metal ({platform.machine()})",
            recommended=True,
        )

    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        return DeviceInfo(
            kind="cuda",
            torch_device="cuda:0",
            label=f"CUDA ({name})",
            recommended=True,
        )

    return DeviceInfo(
        kind="cpu",
        torch_device="cpu",
        label="CPU (slow — expect long runtimes)",
        recommended=False,
    )
