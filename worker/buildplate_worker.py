"""
Buildplate local GPU worker — text/image → textured GLB (Hunyuan shape + paint).

Uses Hunyuan3D-2mini for shape, optional HunyuanDiT text→image, optional
Hunyuan3D-Paint for UV textures. Auth: X-Worker-Secret header.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

# WinPortable layout: hy3dgen lives under BUILDPLATE_HY3D_ROOT\Hunyuan3D-2
_HY3D_ROOT = Path(
    os.environ.get(
        "BUILDPLATE_HY3D_ROOT",
        r"C:\buildplate-worker\Hunyuan3D2_WinPortable\Hunyuan3D2_WinPortable",
    )
)
_HY3D_CODE = _HY3D_ROOT / "Hunyuan3D-2"
if _HY3D_CODE.is_dir() and str(_HY3D_CODE) not in sys.path:
    sys.path.insert(0, str(_HY3D_CODE))

# custom_rasterizer_kernel needs torch\lib (+ CUDA bin) on PATH before import.
_TORCH_LIB = _HY3D_ROOT / "python_standalone" / "Lib" / "site-packages" / "torch" / "lib"
_path_bits = []
if _TORCH_LIB.is_dir():
    _path_bits.append(str(_TORCH_LIB))
_cuda_home = os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH")
if not _cuda_home:
    _guess = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.9")
    if (_guess / "bin").is_dir():
        _cuda_home = str(_guess)
        os.environ.setdefault("CUDA_HOME", _cuda_home)
if _cuda_home:
    _path_bits.append(str(Path(_cuda_home) / "bin"))
if _path_bits:
    os.environ["PATH"] = os.pathsep.join(_path_bits + [os.environ.get("PATH", "")])

import torch
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from PIL import Image

from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
SAVE_DIR = Path(os.environ.get("BUILDPLATE_CACHE", str(HERE / "cache")))
SAVE_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = SAVE_DIR / "worker.log"

logger = logging.getLogger("buildplate-worker")


def configure_logging(verbose: bool = False) -> None:
    """Console + rotating file under worker/cache/worker.log."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Always quiet chatty deps; --verbose is for buildplate-worker + hy3dgen.
    for name in ("uvicorn.access", "httpx", "urllib3", "filelock", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("buildplate-worker").setLevel(level)
    logging.getLogger("hy3dgen").setLevel(level)
    logger.info("Logging to %s (level=%s)", LOG_PATH, logging.getLevelName(level))

WORKER_SECRET = os.environ.get("WORKER_SECRET", "").strip()
BUSY = threading.Lock()
BOOT_ID = uuid.uuid4().hex[:12]
BOOT_TS = time.time()
STATE = {
    "ready": False,
    "busy": False,
    "model": None,
    "device": "cuda",
    "last_error": None,
    "jobs_done": 0,
    "texgen": False,
    "texgen_loaded": False,
}


def require_auth(x_worker_secret: str | None):
    if not WORKER_SECRET:
        return
    if (x_worker_secret or "").strip() != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="invalid worker secret")


def enrich_prompt(prompt: str) -> str:
    """Pass-through with light quality cues. Lambda LLM rewrites first."""
    base = prompt.strip()
    lower = base.lower()
    if "white background" in lower or "textured" in lower:
        return base
    return f"{base}, white background, centered, multi-color textured 3D asset"


def load_image_b64(data: str) -> Image.Image:
    raw = data
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    return Image.open(BytesIO(base64.b64decode(raw))).convert("RGBA")


class _FixedHunyuanDiT:
    """HunyuanDiT for 16GB cards: CPU-offloaded so it doesn't share VRAM with shape gen."""

    def __init__(
        self,
        model_path: str = "Tencent-Hunyuan/HunyuanDiT-v1.1-Diffusers-Distilled",
        device: str = "cuda",
    ):
        from diffusers import AutoPipelineForText2Image

        self.device = device
        self.pipe = AutoPipelineForText2Image.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            enable_pag=True,
            pag_applied_layers=["blocks.(16|17|18|19)"],
        )
        self.pipe.enable_model_cpu_offload()
        self.pos_txt = ", white background, 3D style, best quality, multi-color textured"
        self.neg_txt = (
            "text, close-up, cropped, out of frame, worst quality, low quality, "
            "JPEG artifacts, blurry, deformed, extra limbs, watermark"
        )

    @torch.inference_mode()
    def __call__(self, prompt: str, seed: int = 0) -> Image.Image:
        generator = torch.Generator(device="cpu").manual_seed(int(seed))
        brief = prompt[:120]
        out = self.pipe(
            prompt=(brief + self.pos_txt),
            negative_prompt=self.neg_txt,
            num_inference_steps=20,
            pag_scale=1.3,
            width=768,
            height=768,
            generator=generator,
            return_dict=False,
        )[0][0]
        torch.cuda.empty_cache()
        return out


class MeshBrain:
    def __init__(
        self,
        model_path: str,
        subfolder: str,
        device: str,
        enable_t23d: bool,
        enable_texgen: bool,
        paint_model: str,
    ):
        self.device = device
        self.enable_t23d = enable_t23d
        self.enable_texgen = enable_texgen
        self.paint_model = paint_model
        self.pipeline_t2i = None
        self.pipeline_texgen = None

        logger.info("Loading rembg (CPU)…")
        from rembg import new_session
        from hy3dgen.rembg import BackgroundRemover as _BFR

        class _CpuRembg(_BFR):
            def __init__(self):
                self.session = new_session(providers=["CPUExecutionProvider"])

        self.rembg = _CpuRembg()

        logger.info("Loading shape model %s / %s …", model_path, subfolder)
        self.pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
            model_path,
            subfolder=subfolder,
            use_safetensors=True,
            device=device,
        )
        try:
            self.pipeline.enable_flashvdm(mc_algo="mc")
        except Exception as e:
            logger.warning("enable_flashvdm skipped: %s", e)

        if enable_t23d:
            logger.info("Loading HunyuanDiT text→image (CPU offload)…")
            self.pipeline_t2i = _FixedHunyuanDiT(
                "Tencent-Hunyuan/HunyuanDiT-v1.1-Diffusers-Distilled",
                device=device,
            )

        STATE["model"] = f"{model_path}:{subfolder}"
        STATE["device"] = device
        STATE["texgen"] = bool(enable_texgen)
        STATE["ready"] = True
        logger.info(
            "Mesh brain ready (texgen=%s). VRAM free≈%s",
            enable_texgen,
            _vram_info(),
        )

    def _ensure_texgen(self):
        if self.pipeline_texgen is not None:
            return True
        if not self.enable_texgen:
            return False
        try:
            logger.info("Loading Hunyuan3D-Paint (%s)… vram=%s", self.paint_model, _vram_info())
            torch.cuda.empty_cache()
            # Rasterizer import probe — empty/missing CUDA ext often looks like a silent hang later.
            try:
                import custom_rasterizer  # type: ignore  # noqa: F401

                logger.info("custom_rasterizer import ok")
            except Exception as e:
                logger.warning("custom_rasterizer import failed (paint may hang/CPU-fall): %s", e)

            from hy3dgen.texgen import Hunyuan3DPaintPipeline

            self.pipeline_texgen = Hunyuan3DPaintPipeline.from_pretrained(self.paint_model)
            # Do NOT enable_model_cpu_offload here: under torch.inference_mode it
            # raises "Inference tensors do not track version counter" in CLIP.
            # Upstream leaves the multiview DiffusionPipeline on CPU — move to CUDA.
            _tune_paint_pipeline(self.pipeline_texgen)
            _ensure_paint_on_cuda(self.pipeline_texgen)
            STATE["texgen_loaded"] = True
            logger.info("Paint pipeline ready. vram=%s", _vram_info())
            return True
        except Exception as e:
            logger.exception("Paint pipeline unavailable — returning untextured mesh: %s", e)
            STATE["last_error"] = f"texgen load failed: {e}"
            self.enable_texgen = False
            STATE["texgen"] = False
            return False

    def generate(
        self,
        *,
        prompt: str | None,
        image: Image.Image | None,
        seed: int,
        octree_resolution: int,
        num_inference_steps: int,
        guidance_scale: float,
        out_type: str,
        texture: bool,
    ) -> Path:
        # Shape under inference_mode; paint uses no_grad only — Hunyuan paint/CLIP
        # mutates tensors in ways that break inference_mode.
        with torch.inference_mode():
            if image is None:
                if not prompt:
                    raise ValueError("Provide prompt and/or image")
                if not self.pipeline_t2i:
                    raise ValueError(
                        "Text mode disabled — send an image, or start with --enable_t23d"
                    )
                text = enrich_prompt(prompt)
                logger.info("text→image: %s (vram=%s)", text[:120], _vram_info())
                image = self.pipeline_t2i(text, seed=seed)
                ref_path = SAVE_DIR / f"ref-{uuid.uuid4().hex[:8]}.png"
                image.save(ref_path)
                logger.info("saved reference %s; vram after t2i=%s", ref_path, _vram_info())
            elif prompt:
                logger.info("image→3D (prompt ignored for conditioning): %s", prompt[:80])

            logger.info("rembg…")
            image = self.rembg(image)

            params = {
                "image": image,
                "generator": torch.Generator(device="cpu").manual_seed(seed),
                "octree_resolution": octree_resolution,
                "num_inference_steps": num_inference_steps,
                "guidance_scale": guidance_scale,
                "mc_algo": "mc",
            }
            t0 = time.time()
            logger.info("shape gen… vram=%s", _vram_info())
            mesh = self.pipeline(**params)[0]
            logger.info("shape gen %.1fs", time.time() - t0)
            torch.cuda.empty_cache()

        textured = False
        if texture and self._ensure_texgen():
            with torch.no_grad():
                mesh, textured = _paint_with_guards(self.pipeline_texgen, mesh, image)

        uid = uuid.uuid4().hex[:12]
        stl_path = SAVE_DIR / f"{uid}.stl"
        glb_path = SAVE_DIR / f"{uid}{'-tex' if textured else ''}.glb"
        # Always write GLB (textures live here). STL is geometry-only fallback/print.
        logger.info("export %s (textured=%s)…", glb_path.name, textured)
        mesh.export(str(glb_path))
        try:
            mesh.export(str(stl_path))
        except Exception as e:
            logger.warning("STL export skipped: %s", e)

        if out_type == "stl" and stl_path.is_file():
            return stl_path
        return glb_path


def _mesh_stats(mesh) -> dict:
    faces = getattr(mesh, "faces", None)
    verts = getattr(mesh, "vertices", None)
    n_faces = int(len(faces)) if faces is not None else None
    n_verts = int(len(verts)) if verts is not None else None
    return {"faces": n_faces, "verts": n_verts}


def _decimate_for_paint(mesh, max_faces: int):
    """
    Windows + xatlas often hangs indefinitely on high-poly UV unwrap
    (Tencent-Hunyuan/Hunyuan3D-2#362). Decimate before paint.
    """
    stats = _mesh_stats(mesh)
    logger.info("mesh before paint: %s", stats)
    n_faces = stats.get("faces") or 0
    if n_faces <= max_faces:
        return mesh
    logger.info("decimating mesh for paint: %s faces → target %s", n_faces, max_faces)
    t0 = time.time()

    # 1) trimesh → needs `fast_simplification` in newer trimesh builds
    try:
        if hasattr(mesh, "simplify_quadric_decimation"):
            out = mesh.simplify_quadric_decimation(int(max_faces))
            logger.info("decimate(trimesh) %.1fs → %s", time.time() - t0, _mesh_stats(out))
            return out
    except Exception as e:
        logger.warning("trimesh decimate failed: %s", e)

    # 2) open3d if present in WinPortable
    try:
        import numpy as np
        import open3d as o3d
        import trimesh

        o3m = o3d.geometry.TriangleMesh()
        o3m.vertices = o3d.utility.Vector3dVector(np.asarray(mesh.vertices))
        o3m.triangles = o3d.utility.Vector3iVector(np.asarray(mesh.faces))
        o3m = o3m.simplify_quadric_decimation(target_number_of_triangles=int(max_faces))
        out = trimesh.Trimesh(
            vertices=np.asarray(o3m.vertices),
            faces=np.asarray(o3m.triangles),
            process=False,
        )
        logger.info("decimate(open3d) %.1fs → %s", time.time() - t0, _mesh_stats(out))
        return out
    except Exception as e:
        logger.warning("open3d decimate failed: %s", e)

    # 3) voxel remesh — coarser but prevents xatlas hang
    try:
        import numpy as np
        import trimesh

        extents = float(np.max(mesh.extents)) if hasattr(mesh, "extents") else 1.0
        pitch = max(extents / 48.0, 1e-4)
        for _ in range(4):
            vox = mesh.voxelized(pitch=pitch)
            out = vox.marching_cubes
            if out is None or len(getattr(out, "faces", [])) == 0:
                pitch *= 1.4
                continue
            n = len(out.faces)
            logger.info(
                "decimate(voxel pitch=%.5f) %.1fs → %s",
                pitch,
                time.time() - t0,
                _mesh_stats(out),
            )
            if n <= max_faces:
                return out
            pitch *= 1.35
        return out
    except Exception as e:
        logger.exception("all decimate paths failed — painting full mesh: %s", e)
        return mesh


def _stage(name: str):
    """Context manager: log paint sub-stage start/end + VRAM."""

    class _Ctx:
        def __enter__(self):
            self.t0 = time.time()
            logger.info("paint stage ▶ %s  vram=%s", name, _vram_info())
            return self

        def __exit__(self, exc_type, exc, tb):
            dt = time.time() - self.t0
            if exc_type:
                logger.error(
                    "paint stage ✖ %s failed after %.1fs: %s  vram=%s",
                    name,
                    dt,
                    exc,
                    _vram_info(),
                )
            else:
                logger.info(
                    "paint stage ✔ %s %.1fs  vram=%s",
                    name,
                    dt,
                    _vram_info(),
                )
            return False

    return _Ctx()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "off", "no", "")


def _tune_paint_pipeline(pipeline) -> None:
    """Shrink bake resolution for 16GB cards; optional via env."""
    render_size = int(os.environ.get("BUILDPLATE_PAINT_RENDER_SIZE", "1024"))
    texture_size = int(os.environ.get("BUILDPLATE_PAINT_TEXTURE_SIZE", "1024"))
    # MeshRender.texture_size must be a (H, W) tuple — setattr(int) breaks bake:
    # torch.zeros(self.texture_size + (channel,)) → TypeError int + tuple.
    tex_hw = (texture_size, texture_size)
    render_hw = (render_size, render_size)
    cfg_tex = getattr(pipeline.config, "texture_size", None)
    if cfg_tex != texture_size and cfg_tex != tex_hw:
        logger.info(
            "paint config render_size %s→%s texture_size %s→%s",
            getattr(pipeline.config, "render_size", "?"),
            render_size,
            cfg_tex,
            texture_size,
        )
    pipeline.config.render_size = render_size
    pipeline.config.texture_size = texture_size
    render = getattr(pipeline, "render", None)
    if render is not None:
        if hasattr(render, "set_default_texture_resolution"):
            render.set_default_texture_resolution(texture_size)
        else:
            render.texture_size = tex_hw
        if hasattr(render, "set_default_render_resolution"):
            render.set_default_render_resolution(render_size)
        else:
            render.default_resolution = render_hw
        logger.info(
            "MeshRender texture_size=%s default_resolution=%s",
            getattr(render, "texture_size", "?"),
            getattr(render, "default_resolution", "?"),
        )

def _pipe_device_str(pipe) -> str:
    try:
        return str(getattr(pipe, "device", "?"))
    except Exception:
        return "?"


def _ensure_paint_on_cuda(pipeline_texgen) -> str:
    """
    Upstream Hunyuan3DTexGenConfig hardcodes device='cpu' and never calls
    pipeline.to(cuda) (see hy3dgen/texgen/utils/multiview_utils.py). Without
    this, multiview diffusion crawls on CPU and balloons host RAM (~18GB).
    """
    if not torch.cuda.is_available():
        logger.warning("CUDA unavailable — paint stays on CPU")
        return "cpu"

    cfg = getattr(pipeline_texgen, "config", None)
    if cfg is not None:
        try:
            cfg.device = "cuda"
        except Exception:
            pass

    models = getattr(pipeline_texgen, "models", {}) or {}
    target = torch.device("cuda")

    def _move(name: str) -> str | None:
        mod = models.get(name)
        if mod is None:
            return None
        try:
            mod.device = "cuda"
        except Exception:
            pass
        pipe = getattr(mod, "pipeline", None)
        if pipe is None:
            return None
        before = _pipe_device_str(pipe)
        if "cuda" in before.lower():
            logger.info("paint %s already on %s", name, before)
            return before
        logger.info(
            "Moving paint %s %s→cuda … vram=%s",
            name,
            before,
            _vram_info(),
        )
        try:
            pipe.to(target, dtype=torch.float16)
        except TypeError:
            pipe.to(target)
        try:
            torch.cuda.synchronize()
        except Exception:
            pass
        after = _pipe_device_str(pipe)
        logger.info("paint %s now on %s vram=%s", name, after, _vram_info())
        return after

    # Multiview is the slow diffusion step — must be on GPU.
    mv_dev = _move("multiview_model")
    # Delight only if we actually run it (default skip).
    if not _env_flag("BUILDPLATE_PAINT_SKIP_DELIGHT", True):
        _move("delight_model")

    return mv_dev or "cuda"

def _paint_pipeline_staged(pipeline, mesh, image, stage_holder, *, deadline: float):
    """
    Same steps as Hunyuan3DPaintPipeline.__call__, with per-stage logs.

    Important: must run on the request thread (not a ThreadPoolExecutor worker).
    CUDA paint from a side thread hangs on Windows with flat VRAM — that was
    the delight "timeout" we kept seeing.
    """
    from hy3dgen.texgen.utils.uv_warp_utils import mesh_uv_wrap
    import numpy as np

    if not isinstance(image, list):
        images = [image]
    else:
        images = list(image)

    skip_delight = _env_flag("BUILDPLATE_PAINT_SKIP_DELIGHT", True)

    def run(name, fn):
        if time.time() > deadline:
            raise TimeoutError(f"paint deadline hit before stage={name}")
        stage_holder["name"] = name
        with _stage(name):
            return fn()

    images_prompt = run(
        "recenter",
        lambda: [
            pipeline.recenter_image(Image.open(im) if isinstance(im, str) else im)
            for im in images
        ],
    )

    # Ensure RGB for multiview when skipping delight (recenter may leave RGBA).
    def _as_rgb(im):
        return im.convert("RGB") if getattr(im, "mode", None) != "RGB" else im

    if skip_delight:
        logger.info(
            "paint stage ▶ delight SKIPPED "
            "(BUILDPLATE_PAINT_SKIP_DELIGHT=1 — web refs are already clean; "
            "also avoids InstructPix2Pix hang)"
        )
        stage_holder["name"] = "delight_skipped"
        images_prompt = [_as_rgb(im) for im in images_prompt]
    else:

        def do_delight():
            # Fewer steps — default 50 is slow; 20 is usually enough for de-light.
            steps = int(os.environ.get("BUILDPLATE_PAINT_DELIGHT_STEPS", "20"))
            out = []
            for im in images_prompt:
                model = pipeline.models["delight_model"]
                # Prefer going through the underlying diffusers pipe with fewer steps
                # when available; fall back to model.__call__.
                try:
                    pipe = model.pipeline
                    work = im.resize((512, 512)).convert("RGB")
                    result = pipe(
                        prompt="",
                        image=work,
                        generator=torch.manual_seed(42),
                        height=512,
                        width=512,
                        num_inference_steps=steps,
                        image_guidance_scale=getattr(model, "cfg_image", 1.5),
                        guidance_scale=getattr(model, "cfg_text", 1.0),
                    ).images[0]
                    out.append(result)
                except Exception:
                    logger.exception("fast delight path failed; using model.__call__")
                    out.append(model(im))
            return out

        try:
            images_prompt = run("delight", do_delight)
        except Exception as e:
            logger.warning("delight failed (%s) — continuing with raw reference", e)
            images_prompt = [_as_rgb(im) for im in images_prompt]

    def do_uv():
        # Windows hang magnet (xatlas) on huge meshes — we decimate first.
        out = mesh_uv_wrap(mesh)
        logger.info("uv_wrap mesh stats → %s", _mesh_stats(out))
        return out

    mesh = run("uv_wrap", do_uv)
    run("load_mesh", lambda: pipeline.render.load_mesh(mesh) or True)

    elevs = pipeline.config.candidate_camera_elevs
    azims = pipeline.config.candidate_camera_azims
    weights = pipeline.config.candidate_view_weights

    def do_render():
        normals = pipeline.render_normal_multiview(
            elevs, azims, use_abs_coor=True
        )
        positions = pipeline.render_position_multiview(elevs, azims)
        return normals, positions

    normal_maps, position_maps = run("render_maps", do_render)

    def do_multiview():
        camera_info = [
            (((azim // 30) + 9) % 12)
            // {-20: 1, 0: 1, 20: 1, -90: 3, 90: 3}[elev]
            + {-20: 0, 0: 12, 20: 24, -90: 36, 90: 40}[elev]
            for azim, elev in zip(azims, elevs)
        ]
        views = _run_multiview(
            pipeline.models["multiview_model"],
            images_prompt,
            normal_maps + position_maps,
            camera_info,
        )
        for i in range(len(views)):
            views[i] = views[i].resize(
                (pipeline.config.render_size, pipeline.config.render_size)
            )
        return views

    multiviews = run("multiview", do_multiview)
    # Multiview leaves VRAM nearly full — free activations before bake/inpaint.
    torch.cuda.empty_cache()
    _tune_paint_pipeline(pipeline)  # re-assert MeshRender (H,W) tuple sizes

    def do_bake():
        texture, mask = pipeline.bake_from_multiview(
            multiviews,
            elevs,
            azims,
            weights,
            method=pipeline.config.merge_method,
        )
        mask_np = (mask.squeeze(-1).cpu().numpy() * 255).astype(np.uint8)
        return texture, mask_np

    texture, mask_np = run("bake", do_bake)
    texture = run("inpaint", lambda: pipeline.texture_inpaint(texture, mask_np))

    def do_export():
        pipeline.render.set_texture(texture)
        return pipeline.render.save_mesh()

    return run("export_textured", do_export)


def _run_multiview(multiview_model, input_images, control_images, camera_info):
    """
    Same as Multiview_Diffusion_Net.__call__ but with fewer turbo steps + logging.
    Upstream hardcodes num_inference_steps=30 even for LCM turbo.
    """
    steps = int(os.environ.get("BUILDPLATE_PAINT_MULTIVIEW_STEPS", "8"))
    view_size = int(getattr(multiview_model, "view_size", 512) or 512)
    pipe = multiview_model.pipeline
    # Last-chance: upstream Multiview_Diffusion_Net never .to(cuda)'s the pipe.
    if torch.cuda.is_available() and "cuda" not in _pipe_device_str(pipe).lower():
        logger.warning(
            "multiview pipe still on %s at infer time — forcing CUDA",
            _pipe_device_str(pipe),
        )
        try:
            pipe.to(torch.device("cuda"), dtype=torch.float16)
        except TypeError:
            pipe.to("cuda")
        try:
            multiview_model.device = "cuda"
        except Exception:
            pass
    if hasattr(multiview_model, "seed_everything"):
        try:
            multiview_model.seed_everything(0)
        except Exception:
            pass

    if not isinstance(input_images, list):
        input_images = [input_images]
    input_images = [
        im.convert("RGB").resize((view_size, view_size)) for im in input_images
    ]
    controls = []
    for im in control_images:
        c = im.resize((view_size, view_size))
        if c.mode == "L":
            c = c.point(lambda x: 255 if x > 1 else 0, mode="1")
        controls.append(c)

    num_view = len(controls) // 2
    normal_image = [[controls[i] for i in range(num_view)]]
    position_image = [[controls[i + num_view] for i in range(num_view)]]
    device = pipe.device
    gen_device = (
        "cuda"
        if (torch.cuda.is_available() and "cuda" in str(device).lower())
        else "cpu"
    )
    kwargs = dict(
        generator=torch.Generator(device=gen_device).manual_seed(0),
        width=view_size,
        height=view_size,
        num_in_batch=num_view,
        camera_info_gen=[camera_info],
        camera_info_ref=[[0]],
        normal_imgs=normal_image,
        position_imgs=position_image,
    )

    logger.info(
        "multiview infer steps=%s views=%s size=%s device=%s pipe=%s vram=%s",
        steps,
        num_view,
        view_size,
        device,
        getattr(getattr(multiview_model, "pipeline", None), "__class__", type(pipe)).__name__,
        _vram_info(),
    )
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    t0 = time.time()
    # Prefer turbo LCM path when available.
    try:
        if hasattr(pipe, "set_turbo"):
            pipe.set_turbo(True)
    except Exception:
        logger.exception("set_turbo failed (continuing)")

    images = pipe(input_images, num_inference_steps=steps, **kwargs).images
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    logger.info(
        "multiview infer done %.1fs n_images=%s vram=%s",
        time.time() - t0,
        len(images) if images is not None else 0,
        _vram_info(),
    )
    return images


def _paint_with_guards(pipeline_texgen, mesh, image):
    """
    Paint with mesh prep + heartbeats.

    Must run on the dedicated GPU worker thread (see GpuWorker). asyncio.to_thread
    / random threadpool threads cause multiview diffusion to hang with flat VRAM
    on Windows after models were loaded on another thread.

    Soft deadline is checked between stages; a single hung native call can still
    block, so we also skip delight by default and keep meshes small for xatlas.

    Returns (mesh, textured).
    """
    max_faces = int(os.environ.get("BUILDPLATE_PAINT_MAX_FACES", "60000"))
    timeout_s = float(os.environ.get("BUILDPLATE_PAINT_TIMEOUT_S", "360"))
    _tune_paint_pipeline(pipeline_texgen)
    # Re-assert CUDA each job (idempotent). Upstream config defaults to cpu.
    paint_dev = _ensure_paint_on_cuda(pipeline_texgen)
    logger.info("paint device check → %s vram=%s", paint_dev, _vram_info())
    torch.cuda.empty_cache()
    mesh_in = _decimate_for_paint(mesh, max_faces=max_faces)

    stop = threading.Event()
    t1 = time.time()
    deadline = t1 + timeout_s
    stage_holder = {"name": "starting"}

    def _heartbeat():
        while not stop.wait(15.0):
            logger.info(
                "paint still running… stage=%s elapsed=%.0fs vram=%s",
                stage_holder.get("name"),
                time.time() - t1,
                _vram_info(),
            )

    threading.Thread(target=_heartbeat, name="paint-heartbeat", daemon=True).start()
    logger.info(
        "paint/texture… max_faces=%s soft_deadline=%.0fs skip_delight=%s vram=%s",
        max_faces,
        timeout_s,
        _env_flag("BUILDPLATE_PAINT_SKIP_DELIGHT", True),
        _vram_info(),
    )

    try:
        try:
            out = _paint_pipeline_staged(
                pipeline_texgen,
                mesh_in,
                image,
                stage_holder,
                deadline=deadline,
            )
            logger.info(
                "paint %.1fs ok (last_stage=%s)",
                time.time() - t1,
                stage_holder.get("name"),
            )
            return out, True
        except TimeoutError as e:
            logger.error(
                "Paint soft-deadline after %.0fs during stage=%s — %s; "
                "returning untextured mesh",
                timeout_s,
                stage_holder.get("name"),
                e,
            )
            STATE["last_error"] = (
                f"texgen timeout after {timeout_s:.0f}s at stage="
                f"{stage_holder.get('name')}; returned untextured"
            )
            return mesh, False
        except torch.cuda.OutOfMemoryError as e:
            logger.error("Paint OOM — falling back to untextured mesh: %s", e)
            torch.cuda.empty_cache()
            STATE["last_error"] = "texgen OOM; returned untextured"
            return mesh, False
        except Exception as e:
            logger.exception("Paint failed — falling back to untextured mesh: %s", e)
            STATE["last_error"] = f"texgen failed: {e}"
            return mesh, False
    finally:
        stop.set()
        torch.cuda.empty_cache()


brain: MeshBrain | None = None


class GpuWorker:
    """
    Single thread that owns CUDA context + model load + all generate() calls.

    Windows + PyTorch: loading on the main thread then running diffusion via
    asyncio.to_thread (random pool thread) hangs in multiview with flat VRAM.
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name="buildplate-gpu", daemon=True
        )
        self._ready = threading.Event()
        self._brain_kwargs: dict[str, Any] = {}
        self.thread_ident: int | None = None

    def start(self, **brain_kwargs: Any) -> None:
        self._brain_kwargs = brain_kwargs
        self._thread.start()
        # Model load can take 1–2 minutes.
        if not self._ready.wait(timeout=600):
            raise RuntimeError("GPU worker thread failed to become ready")

    def _loop(self) -> None:
        global brain
        self.thread_ident = threading.get_ident()
        logger.info("GPU worker thread started (tid=%s)", self.thread_ident)
        if torch.cuda.is_available():
            torch.cuda.init()
            torch.cuda.set_device(0)
            # Force a context on THIS thread before loading heavy models.
            torch.zeros(1, device="cuda").item()
            torch.cuda.synchronize()
            logger.info("CUDA context bound on GPU worker thread; vram=%s", _vram_info())

        brain = MeshBrain(**self._brain_kwargs)
        # Eager-load paint on the same thread so first job doesn't migrate it.
        if brain.enable_texgen:
            try:
                brain._ensure_texgen()
            except Exception:
                logger.exception("eager texgen load failed")
        self._ready.set()

        while True:
            item = self._q.get()
            if item is None:
                break
            fut, fn, args, kwargs = item
            try:
                if threading.get_ident() != self.thread_ident:
                    logger.error("GPU job running on wrong thread!")
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 — forward to waiter
                fut.set_exception(exc)

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        fut: Future = Future()
        self._q.put((fut, fn, args, kwargs))
        return fut

    async def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.wrap_future(self.submit(fn, *args, **kwargs))


gpu_worker = GpuWorker()
app = FastAPI(title="Shapeful GPU Worker", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "ready": STATE["ready"],
        "busy": STATE["busy"] or BUSY.locked(),
        "model": STATE["model"],
        "device": STATE["device"],
        "t23d": bool(brain and brain.pipeline_t2i),
        "texgen": STATE["texgen"],
        "texgen_loaded": STATE["texgen_loaded"],
        "jobs_done": STATE["jobs_done"],
        "last_error": STATE["last_error"],
        "vram": _vram_info(),
        "pid": os.getpid(),
        "boot_id": BOOT_ID,
        "uptime_s": round(time.time() - BOOT_TS),
        "head": _git_head(),
    }


def _vram_info():
    if not torch.cuda.is_available():
        return None
    free, total = torch.cuda.mem_get_info()
    return {
        "free_mb": round(free / (1024 * 1024)),
        "total_mb": round(total / (1024 * 1024)),
    }


@app.post("/v1/generate")
async def generate(
    request: Request,
    x_worker_secret: str | None = Header(default=None),
):
    require_auth(x_worker_secret)
    if not STATE["ready"] or brain is None:
        raise HTTPException(status_code=503, detail="worker not ready")

    body = await request.json()
    prompt = (body.get("prompt") or body.get("text") or "").strip() or None
    image_b64 = body.get("image")
    out_type = (body.get("type") or body.get("format") or "glb").lower()
    if out_type not in ("stl", "glb"):
        raise HTTPException(status_code=400, detail="type must be stl or glb")

    # Default: texture when requesting GLB and paint is enabled on the worker.
    texture = body.get("texture")
    if texture is None:
        texture = out_type == "glb"
    texture = bool(texture)

    image = load_image_b64(image_b64) if image_b64 else None
    if not prompt and image is None:
        raise HTTPException(status_code=400, detail="prompt or image required")

    if not BUSY.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="worker busy — one job at a time")

    STATE["busy"] = True
    STATE["last_error"] = None
    try:
        # MUST stay on the dedicated GPU thread — not asyncio.to_thread.
        path = await gpu_worker.run(
            brain.generate,
            prompt=prompt,
            image=image,
            seed=int(body.get("seed", 1234)),
            octree_resolution=int(body.get("octree_resolution", 128)),
            num_inference_steps=int(body.get("num_inference_steps", 5)),
            guidance_scale=float(body.get("guidance_scale", 5.0)),
            out_type=out_type,
            texture=texture,
        )
        STATE["jobs_done"] += 1
        media = "model/stl" if path.suffix.lower() == ".stl" else "model/gltf-binary"
        return FileResponse(
            path,
            media_type=media,
            filename=path.name,
            headers={
                "X-Job-Id": path.stem.replace("-tex", ""),
                "X-Textured": "1" if "-tex" in path.stem else "0",
            },
        )
    except ValueError as e:
        STATE["last_error"] = str(e)
        logger.warning("generate bad request: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except torch.cuda.CudaError as e:
        STATE["last_error"] = str(e)
        logger.exception("CUDA error during generate")
        raise HTTPException(status_code=500, detail=f"CUDA error: {e}") from e
    except Exception as e:
        STATE["last_error"] = str(e)
        logger.exception("generate failed")
        raise HTTPException(status_code=500, detail=str(e)) from e
    finally:
        STATE["busy"] = False
        BUSY.release()


def _git_head() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
        return out.strip() or None
    except Exception:
        return None


def _append_relaunch_log(msg: str) -> None:
    """Best-effort breadcrumb so remote can see schedule even if PS spawn fails."""
    path = SAVE_DIR / "relaunch.log"
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        logger.exception("could not write relaunch.log")


def _exit_soon(delay_s: float = 1.5, code: int = 99) -> None:
    """Exit after response flush so Funnel clients see HTTP 200 before downtime."""

    def _die():
        time.sleep(delay_s)
        logger.warning("Admin exit (code=%s) — expecting relaunch-worker.ps1 to respawn", code)
        os._exit(code)

    threading.Thread(target=_die, name="admin-exit", daemon=True).start()


def _schedule_relaunch(*, pull: bool, ref: str = "main") -> Path:
    """
    Spawn PowerShell relaunch helper that kills this process (by ParentPid / port),
    optionally pulls, then starts start-worker.bat and waits for /health ready.
    """
    script = HERE / "relaunch-worker.ps1"
    if not script.is_file():
        raise RuntimeError(f"missing {script}")
    parent = os.getpid()
    args = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Repo",
        str(REPO_ROOT),
        "-Ref",
        ref,
        "-ParentPid",
        str(parent),
    ]
    if pull:
        args.append("-Pull")

    _append_relaunch_log(
        f"python schedule relaunch pull={pull} ref={ref} parent={parent} head={_git_head()}"
    )
    logger.warning("Scheduling relaunch: %s", " ".join(args))

    # Do NOT use DETACHED_PROCESS + close_fds=True — that combo often fails to
    # actually start PowerShell under Funnel/uvicorn, leaving relaunch.log empty.
    spawn_log = SAVE_DIR / "relaunch-spawn.log"
    spawn_log.parent.mkdir(parents=True, exist_ok=True)
    spawn_fh = open(spawn_log, "a", encoding="utf-8")  # noqa: SIM115 — lives with child
    spawn_fh.write(
        f"\n--- spawn {time.strftime('%Y-%m-%d %H:%M:%S')} pull={pull} ref={ref} ---\n"
    )
    spawn_fh.flush()

    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW — survive parent exit, no console flash.
        creationflags = 0x00000200 | 0x08000000
    proc = subprocess.Popen(
        args,
        cwd=str(HERE),
        creationflags=creationflags,
        close_fds=False,
        stdin=subprocess.DEVNULL,
        stdout=spawn_fh,
        stderr=subprocess.STDOUT,
    )
    _append_relaunch_log(f"python spawned powershell pid={proc.pid}")
    logger.warning("Relaunch PowerShell pid=%s (log=%s)", proc.pid, spawn_log)
    return script


def _admin_bounce(*, pull: bool, ref: str, action: str) -> dict:
    """Shared path: schedule relaunch, then always exit so we never leave a zombie."""
    before = _git_head()
    logger.warning(
        "Admin %s requested (ref=%s, head=%s, pid=%s, boot=%s)",
        action,
        ref,
        before,
        os.getpid(),
        BOOT_ID,
    )
    try:
        _schedule_relaunch(pull=pull, ref=ref)
    except Exception as e:
        logger.exception("%s failed to schedule relaunch.ps1", action)
        _append_relaunch_log(f"python schedule FAILED: {e}")
        raise HTTPException(status_code=500, detail=f"schedule failed: {e}") from e
    _exit_soon(1.5)
    return {
        "ok": True,
        "action": action,
        "ref": ref,
        "head_before": before,
        "pid": os.getpid(),
        "boot_id": BOOT_ID,
        "note": "relaunch scheduled; process exiting — poll /health until ready (new boot_id)",
    }


@app.post("/v1/admin/restart")
async def admin_restart(x_worker_secret: str | None = Header(default=None)):
    """Restart the worker process (no git pull). Always exits after scheduling relaunch."""
    require_auth(x_worker_secret)
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="WORKER_SECRET unset — admin disabled")
    return _admin_bounce(pull=False, ref="main", action="restart")


@app.post("/v1/admin/hard-restart")
async def admin_hard_restart(x_worker_secret: str | None = Header(default=None)):
    """Pull + restart + exit (same robust path as /update)."""
    require_auth(x_worker_secret)
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="WORKER_SECRET unset — admin disabled")
    return _admin_bounce(pull=True, ref="main", action="hard-restart")


@app.post("/v1/admin/update")
async def admin_update(
    request: Request,
    x_worker_secret: str | None = Header(default=None),
):
    """git pull + restart + exit. Body optional: {\"ref\":\"main\"}."""
    require_auth(x_worker_secret)
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="WORKER_SECRET unset — admin disabled")
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    ref = (body.get("ref") or "main").strip() or "main"
    return _admin_bounce(pull=True, ref=ref, action="update")


@app.get("/v1/admin/relaunch-log")
async def admin_relaunch_log(
    x_worker_secret: str | None = Header(default=None),
    lines: int = 100,
):
    """Tail worker/cache/relaunch.log (remote update/restart). Auth: X-Worker-Secret."""
    require_auth(x_worker_secret)
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="WORKER_SECRET unset — admin disabled")
    path = SAVE_DIR / "relaunch.log"
    n = max(1, min(int(lines), 2000))
    if not path.is_file():
        return {"ok": True, "path": str(path), "lines": [], "note": "no relaunch.log yet"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "path": str(path), "lines": text[-n:]}


@app.get("/v1/admin/logs")
async def admin_logs(
    x_worker_secret: str | None = Header(default=None),
    lines: int = 200,
):
    """Tail worker/cache/worker.log. Auth: X-Worker-Secret."""
    require_auth(x_worker_secret)
    if not WORKER_SECRET:
        raise HTTPException(status_code=503, detail="WORKER_SECRET unset — admin disabled")
    n = max(1, min(int(lines), 2000))
    if not LOG_PATH.is_file():
        return {"ok": True, "path": str(LOG_PATH), "lines": [], "note": "no log file yet"}
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True, "path": str(LOG_PATH), "lines": text[-n:]}


@app.get("/")
async def root():
    return {
        "service": "buildplate-gpu-worker",
        "endpoints": [
            "/health",
            "/v1/generate",
            "/v1/admin/restart",
            "/v1/admin/hard-restart",
            "/v1/admin/update",
            "/v1/admin/logs",
            "/v1/admin/relaunch-log",
        ],
        "ready": STATE["ready"],
        "texgen": STATE["texgen"],
        "pid": os.getpid(),
        "boot_id": BOOT_ID,
        "head": _git_head(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--model_path", default="tencent/Hunyuan3D-2mini")
    parser.add_argument("--subfolder", default="hunyuan3d-dit-v2-mini-turbo")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--enable_t23d", action="store_true")
    parser.add_argument(
        "--enable_texgen",
        action="store_true",
        help="Enable Hunyuan3D-Paint (UV textures → GLB). Needs paint weights + CUDA rasterizer.",
    )
    parser.add_argument("--paint_model", default="tencent/Hunyuan3D-2")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG logs to console + worker/cache/worker.log",
    )
    args = parser.parse_args()
    configure_logging(verbose=args.verbose)

    logger.info(
        "Starting dedicated GPU worker thread (load + all CUDA jobs stay there)"
    )
    gpu_worker.start(
        model_path=args.model_path,
        subfolder=args.subfolder,
        device=args.device,
        enable_t23d=args.enable_t23d,
        enable_texgen=args.enable_texgen,
        paint_model=args.paint_model,
    )
    if brain is None or not STATE["ready"]:
        raise RuntimeError("GPU worker started but brain is not ready")
    logger.info(
        "GPU worker ready tid=%s head=%s vram=%s",
        gpu_worker.thread_ident,
        _git_head(),
        _vram_info(),
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="debug" if args.verbose else "info",
    )


if __name__ == "__main__":
    main()
