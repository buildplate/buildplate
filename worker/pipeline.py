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
from mesh_ops import postprocess_mesh

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
        subject = prompt.strip().rstrip(".")
        # SD-Turbo loves inventing floors/second subjects — be forceful.
        enriched = (
            f"single {subject}, one character only, 3D vinyl toy figurine, "
            f"full body centered, plain pure white background, "
            f"no floor, no ground, no shadow, no base, no scenery, no second figure"
        )
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
        import numpy as np
        from scipy import ndimage

        cut = remove(image.convert("RGBA"), session=self._rembg_session)
        arr = np.array(cut)
        alpha = arr[:, :, 3]
        alpha = (alpha >= 40).astype(np.uint8) * 255
        # Keep only the largest connected opaque blob (drop floor scraps / 2nd subject)
        labeled, n = ndimage.label(alpha > 0)
        if n > 1:
            sizes = ndimage.sum(alpha > 0, labeled, index=range(1, n + 1))
            keep = int(np.argmax(sizes)) + 1
            alpha = np.where(labeled == keep, alpha, 0).astype(np.uint8)
            logger.info("rembg blobs=%d kept=%d", n, int(np.max(sizes)))
        # Slight erode to shave fuzzy halo that becomes a sheet
        mask = alpha > 0
        mask = ndimage.binary_erosion(mask, iterations=1)
        arr[:, :, 3] = np.where(mask, 255, 0).astype(np.uint8)
        arr[arr[:, :, 3] == 0, :3] = 255

        ys, xs = np.where(arr[:, :, 3] > 0)
        if len(xs) == 0:
            return Image.fromarray(arr, mode="RGBA")
        pad = 24
        x0, x1 = max(0, xs.min() - pad), min(arr.shape[1], xs.max() + pad + 1)
        y0, y1 = max(0, ys.min() - pad), min(arr.shape[0], ys.max() + pad + 1)
        cropped = arr[y0:y1, x0:x1]
        side = int(max(cropped.shape[0], cropped.shape[1]) * 1.4)
        side = max(side, 256)
        canvas = np.zeros((side, side, 4), dtype=np.uint8)
        canvas[:, :, :3] = 255
        oy = (side - cropped.shape[0]) // 2
        ox = (side - cropped.shape[1]) // 2
        canvas[oy : oy + cropped.shape[0], ox : ox + cropped.shape[1]] = cropped
        return Image.fromarray(canvas, mode="RGBA")

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

        bg = Image.new("RGBA", cutout.size, (255, 255, 255, 255))
        composited = Image.alpha_composite(bg, cutout).convert("RGB")
        composited.save(out_dir / "composited.png")

        import numpy as np
        import torch

        with torch.no_grad():
            scene_codes = self._tsr([np.array(composited)], device=self.device.torch_device)
            # Slightly higher threshold → less floaty sheet geometry
            meshes = self._tsr.extract_mesh(
                scene_codes,
                True,
                resolution=256,
                threshold=40.0,
            )
            # Neural preview frames for chat / debugging
            try:
                renders = self._tsr.render(
                    scene_codes,
                    n_views=4,
                    elevation_deg=15.0,
                    return_type="pil",
                )
                for i, img in enumerate(renders[0]):
                    img.save(out_dir / f"render_{i:02d}.png")
                renders[0][0].save(out_dir / "preview.png")
            except Exception as err:
                logger.warning("preview render failed: %s", err)

        mesh = postprocess_mesh(meshes[0])
        kind = "stl" if fmt == "stl" else "glb"
        path = out_dir / f"model.{kind}"

        if kind == "stl":
            mesh.export(str(path))
        else:
            try:
                mesh.export(str(path), file_type="glb")
            except Exception:
                alt = out_dir / "model.obj"
                mesh.export(str(alt))
                path = alt
                kind = "obj"

        # Also bake a simple shaded still from the cleaned mesh (orientation-correct)
        try:
            _save_mesh_still(mesh, out_dir / "preview.png")
        except Exception as err:
            logger.warning("mesh still failed: %s", err)

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
                "preview": str(out_dir / "preview.png"),
            },
        )


def _save_mesh_still(mesh, path: Path, size: int = 512) -> None:
    """Orientation-correct PNG using matplotlib (no pyglet needed)."""
    import numpy as np
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    m = mesh.copy()
    if len(m.faces) > 20_000:
        try:
            m = m.simplify_quadric_decimation(20_000)
        except Exception:
            # Random subsample faces if simplify unavailable
            idx = np.random.choice(len(m.faces), 20_000, replace=False)
            m = m.submesh([idx], append=True)

    verts = m.vertices
    # Y-up mesh → Z-up for matplotlib
    plot_tris = np.stack(
        [verts[m.faces][:, :, 0], verts[m.faces][:, :, 2], verts[m.faces][:, :, 1]],
        axis=-1,
    )

    fig = plt.figure(figsize=(size / 100, size / 100), dpi=100)
    ax = fig.add_subplot(111, projection="3d")
    coll = Poly3DCollection(plot_tris, linewidths=0.02, alpha=1.0)
    coll.set_facecolor((0.78, 0.82, 0.88, 1.0))
    coll.set_edgecolor((0.3, 0.35, 0.4, 0.12))
    ax.add_collection3d(coll)

    c = verts.mean(axis=0)
    span = float((verts.max(axis=0) - verts.min(axis=0)).max() / 2 * 1.15)
    ax.set_xlim(c[0] - span, c[0] + span)
    ax.set_ylim(c[2] - span, c[2] + span)
    ax.set_zlim(c[1] - span, c[1] + span)
    ax.view_init(elev=20, azim=-60)
    ax.set_axis_off()
    fig.patch.set_facecolor("#12171c")
    plt.tight_layout(pad=0)
    fig.savefig(
        path,
        dpi=100,
        facecolor=fig.get_facecolor(),
        bbox_inches="tight",
        pad_inches=0.05,
    )
    plt.close(fig)


def create_backend(prefer: str | None = None) -> Backend:
    """
    prefer: "triposr" | "stub" | None (auto)
    Auto tries TripoSR; falls back to stub if BUILDPLATE_ALLOW_STUB=1 and load fails at boot.
    """
    choice = (prefer or "").strip().lower() or None
    if choice == "stub":
        return StubBackend()
    return TripoSRBackend(pick_device())
