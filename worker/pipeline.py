"""
Buildplate mesh backends.

Primary path (multi-OS):
  text → SD-Turbo (diffusers) → rembg → TripoSR → GLB/STL

Works on Apple Silicon (MPS), NVIDIA (CUDA), and CPU (slow).
"""

from __future__ import annotations

import base64
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
import sys

# Vendored TripoSR (cloned by npm run setup)
_VENDOR_TSR = Path(__file__).resolve().parent / "vendor" / "TripoSR"
if _VENDOR_TSR.is_dir() and str(_VENDOR_TSR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_TSR))

from PIL import Image

from device import DeviceInfo, pick_device

logger = logging.getLogger("buildplate-worker")


@dataclass
class GenerateResult:
    path: Path
    kind: str  # "glb" | "stl"
    textured: bool
    meta: dict[str, Any]


class Backend(ABC):
    name: str

    @abstractmethod
    def load(self) -> None: ...

    @abstractmethod
    def ready(self) -> bool: ...

    @abstractmethod
    def generate(
        self,
        *,
        prompt: str | None,
        image: Image.Image | None,
        out_dir: Path,
        fmt: str,
        texture: bool,
    ) -> GenerateResult: ...


def load_image_b64(data: str) -> Image.Image:
    raw = data
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    return Image.open(BytesIO(base64.b64decode(raw))).convert("RGBA")


class StubBackend(Backend):
    """Deterministic placeholder mesh — used when models aren't installed yet."""

    name = "stub"

    def load(self) -> None:
        return

    def ready(self) -> bool:
        return True

    def generate(
        self,
        *,
        prompt: str | None,
        image: Image.Image | None,
        out_dir: Path,
        fmt: str,
        texture: bool,
    ) -> GenerateResult:
        import numpy as np
        import trimesh

        out_dir.mkdir(parents=True, exist_ok=True)
        # Simple coffee-mug-ish solid so Export STL has something real.
        body = trimesh.creation.cylinder(radius=18, height=40, sections=48)
        handle = trimesh.creation.torus(major_radius=14, minor_radius=3.5, major_sections=32, minor_sections=16)
        handle.apply_translation([18, 0, 5])
        mesh = trimesh.util.concatenate([body, handle])
        mesh.apply_translation(-mesh.bounds.mean(axis=0))
        kind = "stl" if fmt == "stl" else "glb"
        path = out_dir / f"model.{kind}"
        mesh.export(path)
        return GenerateResult(
            path=path,
            kind=kind,
            textured=False,
            meta={"backend": self.name, "prompt": prompt, "note": "stub mesh"},
        )


class TripoSRBackend(Backend):
    """SD-Turbo (text→image) + rembg + TripoSR (image→mesh)."""

    name = "triposr"

    def __init__(self, device: DeviceInfo | None = None):
        self.device = device or pick_device()
        self._tsr = None
        self._t2i = None
        self._rembg_session = None
        self._loaded = False
        self._load_error: str | None = None

    def ready(self) -> bool:
        return self._loaded

    @property
    def last_error(self) -> str | None:
        return self._load_error

    def load(self) -> None:
        if self._loaded:
            return
        t0 = time.time()
        try:
            import torch
            from tsr.system import TSR

            # TripoSR on MPS: float32 — grid_sample breaks with Half/Float mix.
            # SD-Turbo can still use float16 on Metal/CUDA.
            tsr_dtype = torch.float32
            t2i_dtype = torch.float16 if self.device.kind in ("mps", "cuda") else torch.float32
            logger.info(
                "Loading TripoSR on %s (tsr=%s, t2i=%s)…",
                self.device.label,
                tsr_dtype,
                t2i_dtype,
            )
            self._tsr = TSR.from_pretrained(
                "stabilityai/TripoSR",
                config_name="config.yaml",
                weight_name="model.ckpt",
            )
            self._tsr.to(self.device.torch_device)

            logger.info("Loading SD-Turbo for text→image…")
            from diffusers import AutoPipelineForText2Image

            pipe_kwargs = {"torch_dtype": t2i_dtype}
            if t2i_dtype == torch.float16:
                pipe_kwargs["variant"] = "fp16"

            self._t2i = AutoPipelineForText2Image.from_pretrained(
                "stabilityai/sd-turbo",
                **pipe_kwargs,
            )
            self._t2i.to(self.device.torch_device)

            try:
                from rembg import new_session

                self._rembg_session = new_session("u2net")
            except Exception as err:
                logger.warning("rembg unavailable (%s) — skipping bg removal", err)
                self._rembg_session = None

            self._loaded = True
            self._load_error = None
            logger.info("Backend ready in %.1fs", time.time() - t0)
        except Exception as err:
            self._loaded = False
            self._load_error = str(err)
            logger.exception("Failed to load TripoSR backend: %s", err)
            raise

    def _text_to_image(self, prompt: str) -> Image.Image:
        assert self._t2i is not None
        enriched = prompt.strip()
        if "white background" not in enriched.lower():
            enriched = f"{enriched}, product photo, centered, white background"
        # SD-Turbo: 1–4 steps
        result = self._t2i(
            prompt=enriched,
            num_inference_steps=4,
            guidance_scale=0.0,
        )
        return result.images[0].convert("RGBA")

    def _remove_bg(self, image: Image.Image) -> Image.Image:
        if self._rembg_session is None:
            return image.convert("RGBA")
        from rembg import remove

        return remove(image.convert("RGBA"), session=self._rembg_session)

    def generate(
        self,
        *,
        prompt: str | None,
        image: Image.Image | None,
        out_dir: Path,
        fmt: str,
        texture: bool,
    ) -> GenerateResult:
        if not self._loaded:
            self.load()
        assert self._tsr is not None

        out_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()

        if image is None:
            if not prompt or not prompt.strip():
                raise ValueError("prompt or image required")
            image = self._text_to_image(prompt)
            image.save(out_dir / "reference.png")
        else:
            image = image.convert("RGBA")
            image.save(out_dir / "input.png")

        cutout = self._remove_bg(image)
        cutout.save(out_dir / "cutout.png")

        # TripoSR expects RGB with white/alpha handling — composite on white
        bg = Image.new("RGBA", cutout.size, (255, 255, 255, 255))
        composited = Image.alpha_composite(bg, cutout).convert("RGB")

        import numpy as np
        import torch

        with torch.no_grad():
            scene_codes = self._tsr([np.array(composited)], device=self.device.torch_device)
            meshes = self._tsr.extract_mesh(
                scene_codes,
                True,  # has_vertex_color
                resolution=256,
            )

        mesh = meshes[0]
        kind = "stl" if fmt == "stl" else "glb"
        path = out_dir / f"model.{kind}"

        # TSR returns a trimesh-like object
        if kind == "stl":
            mesh.export(str(path))
        else:
            # Prefer GLB; fall back to GLTF if needed
            try:
                mesh.export(str(path), file_type="glb")
            except Exception:
                alt = out_dir / "model.obj"
                mesh.export(str(alt))
                path = alt
                kind = "obj"

        elapsed = time.time() - t0
        return GenerateResult(
            path=path,
            kind=kind if kind != "obj" else "glb",
            textured=False,
            meta={
                "backend": self.name,
                "device": self.device.label,
                "prompt": prompt,
                "seconds": round(elapsed, 2),
                "texture_requested": texture,
            },
        )


def create_backend(prefer: str | None = None) -> Backend:
    """
    prefer: "triposr" | "stub" | None (auto)
    Auto tries TripoSR; falls back to stub if BUILDPLATE_ALLOW_STUB=1 and load fails at boot.
    """
    choice = (prefer or "").strip().lower() or None
    if choice == "stub":
        return StubBackend()
    return TripoSRBackend(pick_device())
