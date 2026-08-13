"""Hunyuan3D-2mini shape backend (quality mesh path) + view-projected PBR albedo."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from PIL import Image

from device import DeviceInfo, pick_device
from dirs import vendor_dir
from mesh_ops import postprocess_mesh
from preprocess import composite_white, remove_bg
from remesh import DEFAULT_TARGET_FACES

logger = logging.getLogger("buildplate-worker")

_VENDOR = vendor_dir() / "Hunyuan3D-2"


def hunyuan_available() -> bool:
    return (_VENDOR / "hy3dgen" / "shapegen" / "pipelines.py").is_file()


class HunyuanBackend:
    """Image → Hunyuan3D-DiT mini (shape) → remesh → view-projected PBR."""

    name = "hunyuan"

    def __init__(self, device: DeviceInfo | None = None):
        self.device = device or pick_device()
        self._pipe = None
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
        if not hunyuan_available():
            raise RuntimeError(
                "Hunyuan3D-2 vendor missing. Run: npx -y github:buildplate/buildplate setup"
            )
        vendor = str(_VENDOR)
        if vendor not in sys.path:
            sys.path.insert(0, vendor)

        t0 = time.time()
        import torch
        from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

        model = os.environ.get("BUILDPLATE_HUNYUAN_MODEL", "tencent/Hunyuan3D-2mini").strip()
        subfolder = os.environ.get(
            "BUILDPLATE_HUNYUAN_SUBFOLDER",
            "hunyuan3d-dit-v2-mini",
        ).strip()

        if self.device.kind == "cuda":
            dtype = torch.float16
            device = self.device.torch_device
        elif self.device.kind == "mps":
            dtype = torch.float32
            device = "mps"
        else:
            dtype = torch.float32
            device = "cpu"

        logger.info(
            "Loading Hunyuan shape %s/%s on %s dtype=%s…",
            model,
            subfolder,
            self.device.label,
            dtype,
        )
        self._pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
            model,
            subfolder=subfolder,
            device=device,
            dtype=dtype,
            use_safetensors=True,
            variant="fp16",
        )
        if self.device.kind == "cuda":
            try:
                self._pipe.enable_flashvdm(enabled=True)
            except Exception as err:
                logger.warning("FlashVDM skipped: %s", err)

        self._loaded = True
        self._load_error = None
        logger.info("Hunyuan shape ready in %.1fs", time.time() - t0)

    def generate(
        self,
        *,
        prompt: str | None,
        image: Image.Image | None,
        out_dir: Path,
        fmt: str,
        texture: bool,
        remesh: bool = True,
        target_faces: int = DEFAULT_TARGET_FACES,
    ):
        from pipeline import finish_generated_mesh

        if not self._loaded:
            self.load()
        assert self._pipe is not None
        out_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        if image is None:
            if not prompt or not prompt.strip():
                raise ValueError("prompt or image required")
            from text2img import render_subject, unload as unload_t2i

            image = render_subject(prompt, size=768)
            image.save(out_dir / "reference.png")
            unload_t2i()
        image = image.convert("RGBA")
        image.save(out_dir / "input.png")
        cutout = remove_bg(image)
        cutout.save(out_dir / "cutout.png")
        composited = composite_white(cutout)
        composited.save(out_dir / "composited.png")

        steps = 50
        if self.device.kind == "cuda":
            steps = 5
        logger.info("Hunyuan infer steps=%d…", steps)
        outputs = self._pipe(
            image=composited,
            num_inference_steps=steps,
        )
        mesh = postprocess_mesh(outputs[0], strip_relief=False)
        return finish_generated_mesh(
            mesh,
            out_dir=out_dir,
            fmt=fmt,
            texture=texture,
            image=composited,
            remesh=remesh,
            target_faces=target_faces,
            prompt=prompt,
            backend_name=self.name,
            device_label=self.device.label,
            extra_meta={"seconds": round(time.time() - t0, 2)},
        )
