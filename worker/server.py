"""
Buildplate local mesh worker — multi-OS (macOS Metal / NVIDIA CUDA / CPU).

HTTP contract (localhost):
  GET  /health
  POST /v1/generate  → binary GLB or STL
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

from device import pick_device
from pipeline import Backend, create_backend, load_image_b64

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("BUILDPLATE_CACHE", str(HERE / "cache")))
CACHE.mkdir(parents=True, exist_ok=True)
LOG_PATH = CACHE / "worker.log"

logger = logging.getLogger("buildplate-worker")

WORKER_SECRET = os.environ.get("BUILDPLATE_WORKER_SECRET", os.environ.get("WORKER_SECRET", "")).strip()
# Localhost-first: secret optional unless BUILDPLATE_REQUIRE_SECRET=1
REQUIRE_SECRET = os.environ.get("BUILDPLATE_REQUIRE_SECRET", "").strip() in ("1", "true", "yes")

BUSY = threading.Lock()
BOOT_ID = uuid.uuid4().hex[:12]
BOOT_TS = time.time()
STATE: dict = {
    "ready": False,
    "busy": False,
    "model": None,
    "device": None,
    "last_error": None,
    "jobs_done": 0,
    "backend": None,
}

backend: Backend | None = None


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


app = FastAPI(title="Buildplate Worker", version="0.2.0")
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


@app.get("/v1/jobs/{job_id}/preview.png")
def job_preview(job_id: str):
    path = CACHE / "jobs" / job_id / "preview.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="preview not found")
    return FileResponse(path, media_type="image/png")


@app.get("/health")
def health():
    device = pick_device()
    return {
        "service": "buildplate-worker",
        "ready": STATE["ready"],
        "busy": STATE["busy"],
        "model": STATE["model"],
        "backend": STATE["backend"],
        "device": STATE["device"] or device.label,
        "device_kind": device.kind,
        "last_error": STATE["last_error"],
        "jobs_done": STATE["jobs_done"],
        "boot_id": BOOT_ID,
        "uptime_s": round(time.time() - BOOT_TS, 1),
    }


@app.post("/v1/generate")
def generate(
    body: GenerateBody,
    x_worker_secret: str | None = Header(default=None),
):
    require_auth(x_worker_secret)
    if not STATE["ready"] or backend is None:
        raise HTTPException(status_code=503, detail=STATE["last_error"] or "worker not ready")

    if not BUSY.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="worker busy — one job at a time")

    STATE["busy"] = True
    job_id = uuid.uuid4().hex[:10]
    out_dir = CACHE / "jobs" / job_id
    try:
        image = load_image_b64(body.image) if body.image else None
        prompt = (body.prompt or "").strip() or None
        if not image and not prompt:
            raise HTTPException(status_code=400, detail="prompt or image required")

        fmt = "stl" if (body.type or "glb").lower() == "stl" else "glb"
        result = backend.generate(
            prompt=prompt,
            image=image,
            out_dir=out_dir,
            fmt=fmt,
            texture=body.texture,
        )
        STATE["jobs_done"] += 1
        STATE["last_error"] = None

        media = "model/stl" if result.kind == "stl" else "model/gltf-binary"
        preview = out_dir / "preview.png"
        headers = {
            "X-Job-Id": job_id,
            "X-Textured": "1" if result.textured else "0",
            "X-Backend": str(result.meta.get("backend", "")),
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


def boot_backend(prefer: str | None) -> None:
    global backend
    device = pick_device()
    STATE["device"] = device.label
    allow_stub = os.environ.get("BUILDPLATE_ALLOW_STUB", "").strip() in ("1", "true", "yes")
    prefer = prefer or os.environ.get("BUILDPLATE_BACKEND", "").strip() or None

    try:
        backend = create_backend(prefer)
        STATE["backend"] = backend.name
        STATE["model"] = backend.name
        backend.load()
        STATE["ready"] = backend.ready()
        if not STATE["ready"]:
            raise RuntimeError("backend loaded but not ready")
        logger.info("Ready — backend=%s device=%s", backend.name, device.label)
    except Exception as err:
        STATE["last_error"] = str(err)
        STATE["ready"] = False
        if allow_stub or prefer == "stub":
            logger.warning("Falling back to stub backend (%s)", err)
            backend = create_backend("stub")
            backend.load()
            STATE["backend"] = "stub"
            STATE["model"] = "stub"
            STATE["ready"] = True
            STATE["last_error"] = f"using stub: {err}"
        else:
            logger.error("Backend failed to load: %s", err)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Buildplate local mesh worker")
    parser.add_argument("--host", default=os.environ.get("BUILDPLATE_WORKER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BUILDPLATE_WORKER_PORT", "8081")))
    parser.add_argument("--backend", default=None, help="triposr | stub")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--lazy",
        action="store_true",
        help="Start HTTP before models finish loading (load on first request)",
    )
    args = parser.parse_args()
    configure_logging(args.verbose)

    device = pick_device()
    logger.info("Buildplate worker — device=%s", device.label)
    logger.info("Cache: %s", CACHE)
    logger.info("Log:   %s", LOG_PATH)

    if args.lazy:
        # Mark not-ready until first generate loads; health still responds.
        def _bg():
            try:
                boot_backend(args.backend)
            except Exception:
                pass

        threading.Thread(target=_bg, name="buildplate-load", daemon=True).start()
    else:
        boot_backend(args.backend)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
