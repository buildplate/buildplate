"""Text → reconstruction-friendly reference photo.

SD-Turbo (512, 4 steps, cfg=0) paints grainy gray sketches that TripoSR
reads as rocks. SDXL-Turbo is the same speed class with actual color and
a usable silhouette.
"""

from __future__ import annotations

import logging
import re

import numpy as np
from PIL import Image

from device import pick_device

logger = logging.getLogger("buildplate-worker")

_pipe = None
_pipe_device: str | None = None

_BOILERPLATE = re.compile(
    r"\b(one character only|plain pure white background|no floor|no ground|"
    r"no shadow|no base|no scenery|no second figure|3d vinyl toy figurine)\b",
    re.I,
)


def render_subject(prompt: str, *, attempts: int = 3, size: int = 512) -> Image.Image:
    """One subject, white background, colorful enough to reconstruct."""
    pipe = _ensure()
    import torch

    device = pick_device()
    best: tuple[float, Image.Image] | None = None
    base = _prompt_for(prompt)
    for i in range(max(1, attempts)):
        text = base if i == 0 else f"vibrant colorful {base}"
        kwargs: dict = {
            "prompt": text,
            "num_inference_steps": 4,
            "guidance_scale": 0.0,
            "width": size,
            "height": size,
        }
        seed = 7 + i * 17
        try:
            kwargs["generator"] = torch.Generator(device="cpu").manual_seed(seed)
        except Exception:
            pass
        image = pipe(**kwargs).images[0].convert("RGB")
        score = _colorfulness(image)
        logger.info("t2i try=%d seed=%d colorfulness=%.1f prompt=%s", i, seed, score, text[:80])
        if best is None or score > best[0]:
            best = (score, image)
        if score >= 18.0:
            return image
    assert best is not None
    if best[0] < 8.0:
        logger.warning("t2i still near-grayscale (colorfulness=%.1f)", best[0])
    return best[1]


def unload() -> None:
    """Free SDXL-Turbo before Hunyuan / TripoSR so 32 GB machines survive."""
    global _pipe, _pipe_device
    if _pipe is None:
        return
    try:
        import torch

        del _pipe
        _pipe = None
        _pipe_device = None
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        logger.info("t2i unloaded")
    except Exception as err:
        logger.debug("t2i unload: %s", err)
        _pipe = None
        _pipe_device = None


def _ensure():
    global _pipe, _pipe_device
    device = pick_device()
    if _pipe is not None and _pipe_device == device.torch_device:
        return _pipe
    unload()
    import torch
    from diffusers import AutoPipelineForText2Image

    dtype = torch.float16 if device.kind in ("mps", "cuda") else torch.float32
    logger.info("Loading SDXL-Turbo on %s dtype=%s…", device.label, dtype)
    kwargs: dict = {"torch_dtype": dtype}
    if dtype == torch.float16:
        kwargs["variant"] = "fp16"
    pipe = AutoPipelineForText2Image.from_pretrained("stabilityai/sdxl-turbo", **kwargs)
    if getattr(pipe, "safety_checker", None) is not None:
        pipe.safety_checker = None
    pipe.set_progress_bar_config(disable=True)
    pipe.to(device.torch_device)
    _pipe = pipe
    _pipe_device = device.torch_device
    return pipe


def _prompt_for(prompt: str) -> str:
    core = " ".join(prompt.strip().split())
    core = _BOILERPLATE.sub(" ", core)
    core = re.sub(r"\s+", " ", core).strip(" ,.")
    if len(core) > 140:
        core = core[:140].rsplit(" ", 1)[0]
    return f"{core}, colorful vinyl toy figurine, one subject only, full body, studio light, plain white background, nothing else in the frame"


def _colorfulness(image: Image.Image) -> float:
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    return float((rgb.max(axis=2) - rgb.min(axis=2)).mean())
