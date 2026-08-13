"""
Buildplate local worker — mesh (TripoSR) + CAD (agent-authored OpenSCAD/CadQuery/trimesh).

HTTP contract (localhost):
  GET  /health
  POST /v1/generate  → binary GLB or STL
  POST /v1/refine    → binary GLB (retint existing job; geometry unchanged)
  GET  /v1/jobs/{id}/preview.png
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from cad_compile import available_engines, cad_ready, compile_cad
from device import pick_device
from guide import PLAYBOOK, recommend
from hunyuan_backend import hunyuan_available
from pipeline import Backend, GenerateResult, create_backend, load_image_b64
from refine import find_job_dir, latest_job_dir, refine_job
from remesh import DEFAULT_TARGET_FACES

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("BUILDPLATE_CACHE", str(HERE / "cache")))
CACHE.mkdir(parents=True, exist_ok=True)
LOG_PATH = CACHE / "worker.log"

logger = logging.getLogger("buildplate-worker")

WORKER_SECRET = os.environ.get("BUILDPLATE_WORKER_SECRET", os.environ.get("WORKER_SECRET", "")).strip()
REQUIRE_SECRET = os.environ.get("BUILDPLATE_REQUIRE_SECRET", "").strip() in ("1", "true", "yes")

BUSY = threading.Lock()
BOOT_ID = uuid.uuid4().hex[:12]
BOOT_TS = time.time()
STATE: dict = {
    "ready": False,
    "mesh_ready": False,
    "cad_ready": False,
    "cad_engines": [],
    "mesh_vendors": {},
    "busy": False,
    "model": None,
    "device": None,
    "last_error": None,
    "jobs_done": 0,
    "backend": None,
}

mesh_backends: dict[str, Backend] = {}


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    sh = logging.StreamHandler()
    sh.setLevel(level)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    for name in ("uvicorn.access", "httpx", "urllib3", "filelock", "httpcore", "diffusers", "transformers"):
        logging.getLogger(name).setLevel(logging.WARNING)


def require_auth(x_worker_secret: str | None) -> None:
    if not WORKER_SECRET and not REQUIRE_SECRET:
        return
    if not WORKER_SECRET:
        return
    if (x_worker_secret or "").strip() != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="invalid worker secret")


def refresh_cad_state() -> None:
    engines = available_engines()
    STATE["cad_engines"] = engines
    STATE["cad_ready"] = len(engines) > 0
    STATE["ready"] = bool(STATE["mesh_ready"] or STATE["cad_ready"])


app = FastAPI(title="Buildplate Worker", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateBody(BaseModel):
    prompt: str | None = None
    image: str | None = Field(default=None, description="Base64 image (optionally data-URL)")
    type: str = "glb"
    texture: bool = True
    seed: int | None = None
    # mesh | cad | auto — agent should usually set this deliberately
    backend: str = "auto"
    # openscad | cadquery | trimesh | auto
    engine: str | None = None
    openscad: str | None = Field(default=None, description="Agent-authored OpenSCAD source")
    cadquery: str | None = Field(default=None, description="Agent-authored CadQuery Python (sets result=)")
    trimesh_code: str | None = Field(
        default=None,
        description="Agent-authored trimesh Python CSG (sets result=Trimesh)",
    )
    # mesh vendor: fast=triposr, quality=hunyuan
    quality: str | None = Field(default=None, description="fast | quality")
    vendor: str | None = Field(default=None, description="triposr | hunyuan")
    remesh: bool = True
    target_faces: int | None = None


class RefineBody(BaseModel):
    prompt: str
    job_id: str | None = None
    color: str | None = Field(default=None, description="Target color name or #hex")
    keep_mesh: bool = True


def resolve_backend(body: GenerateBody) -> str:
    """Pick mesh vs cad. Agent should set backend; auto is a safety net."""
    requested = (body.backend or "auto").strip().lower()
    has_cad_src = bool(
        (body.openscad or "").strip()
        or (body.cadquery or "").strip()
        or (body.trimesh_code or "").strip()
    )
    has_image = bool(body.image)
    prompt = (body.prompt or "").lower()

    if requested in ("mesh", "triposr"):
        return "mesh"
    if requested == "cad":
        return "cad"

    # auto
    if has_cad_src:
        return "cad"
    if has_image:
        return "mesh"

    cad_hints = (
        "bracket",
        "enclosure",
        "case",
        "plate",
        "box",
        "mount",
        "adapter",
        "hinge",
        "gear",
        "mm",
        "hole",
        "screw",
        "cad",
        "openscad",
        "parametric",
        "flange",
        "spacer",
        "washer",
        "rail",
        "slot",
    )
    mesh_hints = (
        "character",
        "figurine",
        "toy",
        "creature",
        "animal",
        "person",
        "organic",
        "statue",
        "sculpture",
        "pokemon",
        "pikachu",
    )
    if any(h in prompt for h in mesh_hints):
        return "mesh"
    if any(h in prompt for h in cad_hints):
        return "cad"
    # Default: mesh text path only if agent explicitly allowed it upstream
    return "mesh"


def resolve_mesh_vendor(body: GenerateBody) -> str:
    vendor = (body.vendor or "").strip().lower()
    quality = (body.quality or "").strip().lower()
    if vendor in ("triposr", "hunyuan"):
        return vendor
    if quality == "fast":
        return "triposr"
    if quality == "quality":
        return "hunyuan"
    if hunyuan_available():
        return "hunyuan"
    return "triposr"


def ensure_mesh_vendor(name: str) -> Backend:
    existing = mesh_backends.get(name)
    if existing is not None and existing.ready():
        return existing
    logger.info("Loading mesh vendor=%s", name)
    backend = create_backend(name)
    backend.load()
    if not backend.ready():
        raise RuntimeError(f"mesh vendor {name} loaded but not ready")
    mesh_backends[name] = backend
    STATE["mesh_vendors"][name] = "ready"
    STATE["mesh_ready"] = True
    STATE["model"] = ",".join(sorted(mesh_backends.keys()))
    STATE["backend"] = "mesh:" + "+".join(sorted(mesh_backends.keys())) + "+cad"
    return backend


@app.get("/v1/jobs/{job_id}/preview.png")
def job_preview(job_id: str):
    path = CACHE / "jobs" / job_id / "preview.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="preview not found")
    return FileResponse(path, media_type="image/png")


@app.get("/health")
def health():
    refresh_cad_state()
    device = pick_device()
    return {
        "service": "buildplate-worker",
        "ready": STATE["ready"],
        "mesh_ready": STATE["mesh_ready"],
        "cad_ready": STATE["cad_ready"],
        "cad_engines": STATE["cad_engines"],
        "mesh_vendors": STATE.get("mesh_vendors") or {},
        "hunyuan_vendored": hunyuan_available(),
        "busy": STATE["busy"],
        "model": STATE["model"],
        "backend": STATE["backend"],
        "device": STATE["device"] or device.label,
        "device_kind": device.kind,
        "last_error": STATE["last_error"],
        "jobs_done": STATE["jobs_done"],
        "boot_id": BOOT_ID,
        "uptime_s": round(time.time() - BOOT_TS, 1),
        "tip": "generate for a new mesh/CAD. refine({job_id, prompt}) retints an existing mesh (e.g. green instead of yellow). Shape changes need a new generate.",
    }


@app.get("/v1/guide")
def guide(prompt: str = "", has_image: bool = False, wants_precise_mm: bool = False):
    """Recommend mesh vs cad for a prompt — same rules baked into MCP generate."""
    refresh_cad_state()
    rec = recommend(
        intent=prompt,
        has_image=has_image,
        wants_precise_mm=wants_precise_mm,
    )
    rec["cad_engines"] = STATE["cad_engines"]
    rec["mesh_ready"] = STATE["mesh_ready"]
    rec["cad_ready"] = STATE["cad_ready"]
    return rec


@app.post("/v1/generate")
def generate(
    body: GenerateBody,
    x_worker_secret: str | None = Header(default=None),
):
    require_auth(x_worker_secret)
    refresh_cad_state()

    has_cad_src = bool(
        (body.openscad or "").strip()
        or (body.cadquery or "").strip()
        or (body.trimesh_code or "").strip()
    )
    mode = resolve_backend(body)

    # Incomplete CAD calls → 400 with routing guide (agent should enrich and retry)
    if mode == "cad" and not has_cad_src:
        raise HTTPException(
            status_code=400,
            detail=recommend(
                intent=body.prompt or "",
                has_image=bool(body.image),
                has_cad_source=False,
                wants_precise_mm=True,
            ),
        )
    if mode == "mesh" and not body.image and not (body.prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt or image required for mesh")

    if mode == "cad":
        if not STATE["cad_ready"]:
            raise HTTPException(
                status_code=503,
                detail="CAD engines unavailable (need trimesh+manifold3d, OpenSCAD, or CadQuery)",
            )
    else:
        vendor = resolve_mesh_vendor(body)
        try:
            shape_backend = ensure_mesh_vendor(vendor)
        except Exception as err:
            if vendor == "hunyuan":
                logger.warning("Hunyuan unavailable (%s) — falling back to TripoSR", err)
                STATE["mesh_vendors"]["hunyuan"] = f"error: {err}"
                shape_backend = ensure_mesh_vendor("triposr")
                vendor = "triposr"
            else:
                raise HTTPException(
                    status_code=503,
                    detail=STATE["last_error"] or f"mesh vendor {vendor} not ready: {err}",
                ) from err
        if not STATE["mesh_ready"]:
            raise HTTPException(
                status_code=503,
                detail=STATE["last_error"] or "mesh backend not ready",
            )

    if not BUSY.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="worker busy — one job at a time")

    STATE["busy"] = True
    job_id = uuid.uuid4().hex[:10]
    out_dir = CACHE / "jobs" / job_id
    try:
        prompt = (body.prompt or "").strip() or None

        if mode == "cad":
            cad = compile_cad(
                out_dir=out_dir,
                openscad=body.openscad,
                cadquery=body.cadquery,
                trimesh_code=body.trimesh_code,
                engine=body.engine,
                prompt=prompt,
            )
            result = GenerateResult(
                path=cad.path,
                kind="stl",
                textured=False,
                meta=cad.meta,
            )
        else:
            image = load_image_b64(body.image) if body.image else None
            if not image and not prompt:
                raise HTTPException(status_code=400, detail="prompt or image required for mesh")
            fmt = "stl" if (body.type or "glb").lower() == "stl" else "glb"
            faces = body.target_faces if body.target_faces else DEFAULT_TARGET_FACES
            result = shape_backend.generate(
                prompt=prompt,
                image=image,
                out_dir=out_dir,
                fmt=fmt,
                texture=body.texture,
                remesh=body.remesh,
                target_faces=faces,
            )

        STATE["jobs_done"] += 1
        STATE["last_error"] = None

        media = "model/stl" if result.kind == "stl" else "model/gltf-binary"
        preview = out_dir / "preview.png"
        headers = {
            "X-Job-Id": job_id,
            "X-Textured": "1" if result.textured else "0",
            "X-Backend": str(result.meta.get("backend", mode)),
            "X-Engine": str(result.meta.get("engine", result.meta.get("backend", ""))),
        }
        if preview.is_file():
            headers["X-Preview-Path"] = str(preview)

        return FileResponse(
            path=result.path,
            media_type=media,
            filename=result.path.name,
            headers=headers,
        )
    except HTTPException:
        raise
    except Exception as err:
        logger.exception("generate failed")
        STATE["last_error"] = str(err)
        raise HTTPException(status_code=500, detail=str(err)) from err
    finally:
        STATE["busy"] = False
        BUSY.release()


@app.post("/v1/refine")
def refine(
    body: RefineBody,
    x_worker_secret: str | None = Header(default=None),
):
    require_auth(x_worker_secret)
    if not body.keep_mesh:
        raise HTTPException(
            status_code=400,
            detail=(
                "refine keeps geometry. For shape changes (longer ears, extra parts) "
                "call generate with a new photo or edited CAD source."
            ),
        )
    prompt = (body.prompt or "").strip()
    if not prompt and not (body.color or "").strip():
        raise HTTPException(status_code=400, detail="prompt or color required")

    src = find_job_dir(body.job_id) if body.job_id else latest_job_dir()
    if src is None:
        raise HTTPException(
            status_code=404,
            detail=f"No mesh job found for job_id={body.job_id or '(latest)'}",
        )

    if not BUSY.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="worker busy — one job at a time")

    STATE["busy"] = True
    job_id = uuid.uuid4().hex[:10]
    out_dir = CACHE / "jobs" / job_id
    try:
        result = refine_job(
            src_dir=src,
            out_dir=out_dir,
            prompt=prompt or (body.color or ""),
            color=body.color,
        )
        STATE["jobs_done"] += 1
        STATE["last_error"] = None
        preview = out_dir / "preview.png"
        headers = {
            "X-Job-Id": job_id,
            "X-Textured": "1" if result.textured else "0",
            "X-Backend": "refine",
            "X-Engine": "refine",
            "X-Parent-Job": src.name,
        }
        if preview.is_file():
            headers["X-Preview-Path"] = str(preview)
        return FileResponse(
            path=result.path,
            media_type="model/gltf-binary",
            filename=result.path.name,
            headers=headers,
        )
    except HTTPException:
        raise
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except FileNotFoundError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except Exception as err:
        logger.exception("refine failed")
        STATE["last_error"] = str(err)
        raise HTTPException(status_code=500, detail=str(err)) from err
    finally:
        STATE["busy"] = False
        BUSY.release()


def boot_mesh_backend(prefer: str | None) -> None:
    device = pick_device()
    STATE["device"] = device.label
    allow_stub = os.environ.get("BUILDPLATE_ALLOW_STUB", "").strip() in ("1", "true", "yes")
    prefer = prefer or os.environ.get("BUILDPLATE_BACKEND", "").strip() or None
    STATE["mesh_vendors"]["hunyuan"] = "vendored" if hunyuan_available() else "missing"

    boot_name = "triposr"
    if prefer in ("stub", "hunyuan", "triposr"):
        boot_name = prefer

    try:
        ensure_mesh_vendor(boot_name)
        logger.info("Mesh ready — vendor=%s device=%s hunyuan=%s", boot_name, device.label, STATE["mesh_vendors"].get("hunyuan"))
    except Exception as err:
        STATE["last_error"] = str(err)
        STATE["mesh_ready"] = False
        if allow_stub or prefer == "stub":
            logger.warning("Falling back to stub mesh backend (%s)", err)
            ensure_mesh_vendor("stub")
            STATE["last_error"] = f"using stub mesh: {err}"
        else:
            logger.error("Mesh backend failed to load: %s", err)
    finally:
        refresh_cad_state()


def main() -> None:
    parser = argparse.ArgumentParser(description="Buildplate local mesh+CAD worker")
    parser.add_argument("--host", default=os.environ.get("BUILDPLATE_WORKER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BUILDPLATE_WORKER_PORT", "8081")))
    parser.add_argument("--backend", default=None, help="triposr | hunyuan | stub (boot this mesh vendor)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--lazy",
        action="store_true",
        help="Start HTTP before mesh models finish loading (CAD works immediately)",
    )
    args = parser.parse_args()
    configure_logging(args.verbose)

    device = pick_device()
    logger.info("Buildplate worker — device=%s", device.label)
    logger.info("Cache: %s", CACHE)
    logger.info("Log:   %s", LOG_PATH)

    refresh_cad_state()
    logger.info("CAD engines: %s", STATE["cad_engines"] or "(none)")

    if args.lazy:
        # CAD ready now; mesh loads in background
        STATE["ready"] = STATE["cad_ready"]

        def _bg():
            try:
                boot_mesh_backend(args.backend)
            except Exception:
                pass

        threading.Thread(target=_bg, name="buildplate-load", daemon=True).start()
    else:
        boot_mesh_backend(args.backend)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
