"""Refine an existing mesh job without re-running shape.

Appearance edits (color) keep the mesh and retint albedo / re-bake from a
recolored reference. Shape edits are a new generate — this module does not
morph geometry.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from dirs import cache_dir, out_dir
from pipeline import GenerateResult, _export_mesh, _save_mesh_still

logger = logging.getLogger("buildplate-worker")

# HSV hue in 0..1
_NAMED = {
    "red": 0.00,
    "orange": 0.08,
    "yellow": 0.14,
    "lime": 0.22,
    "green": 0.33,
    "teal": 0.45,
    "cyan": 0.50,
    "blue": 0.62,
    "purple": 0.75,
    "magenta": 0.83,
    "pink": 0.90,
    "brown": 0.08,
}


def find_job_dir(job_id: str) -> Path | None:
    job_id = (job_id or "").strip()
    if not job_id or ".." in job_id or "/" in job_id or "\\" in job_id:
        return None
    cache = cache_dir()
    out = out_dir()
    for root in (cache / "jobs" / job_id, out / job_id, cache / job_id):
        if root.is_dir() and (_mesh_path(root) is not None):
            return root
    return None


def latest_job_dir() -> Path | None:
    cache = cache_dir()
    out = out_dir()
    cands: list[Path] = []
    for root in (cache / "jobs", out):
        if not root.is_dir():
            continue
        cands.extend(p for p in root.iterdir() if p.is_dir() and _mesh_path(p))
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


def refine_job(
    *,
    src_dir: Path,
    out_dir: Path,
    prompt: str,
    color: str | None = None,
) -> GenerateResult:
    """Keep geometry; retint appearance from prompt/color."""
    import trimesh

    out_dir.mkdir(parents=True, exist_ok=True)
    target_h, source_h = parse_recolor(prompt, color)
    if target_h is None:
        raise ValueError(
            "Could not tell what color to apply. Pass color='green' (or a hex like #22aa44)."
        )

    mesh_path = _mesh_path(src_dir)
    if mesh_path is None:
        raise FileNotFoundError(f"No model.glb/stl in {src_dir}")

    loaded = trimesh.load(str(mesh_path), force="mesh")
    if isinstance(loaded, trimesh.Scene):
        geoms = list(loaded.geometry.values())
        if not geoms:
            raise ValueError("empty scene")
        mesh = geoms[0]
    else:
        mesh = loaded

    shifted_ref = None
    for name in ("composited.png", "cutout.png", "input.png", "albedo.png"):
        p = src_dir / name
        if p.is_file():
            img = Image.open(p).convert("RGBA")
            img = recolor_image(img, target_h, source_h)
            img.save(out_dir / name)
            if shifted_ref is None and name != "albedo.png":
                shifted_ref = img.convert("RGB")

    mesh = _retint_mesh(mesh, target_h, source_h, shifted_ref, out_dir)
    path, kind = _export_mesh(mesh, out_dir, "glb")
    try:
        trimesh.Trimesh(mesh.vertices, mesh.faces, process=False).export(str(out_dir / "model.stl"))
    except Exception:
        pass
    try:
        _save_mesh_still(mesh, out_dir / "preview.png")
    except Exception as err:
        logger.warning("refine still failed: %s", err)

    return GenerateResult(
        path=path,
        kind=kind,
        textured=True,
        meta={
            "backend": "refine",
            "parent": src_dir.name,
            "prompt": prompt,
            "color_hue": round(float(target_h), 3),
            "keep_mesh": True,
            "preview": str(out_dir / "preview.png"),
            "note": "appearance only — geometry unchanged",
        },
    )


def parse_recolor(prompt: str, color: str | None) -> tuple[float | None, float | None]:
    text = f"{color or ''} {prompt or ''}".strip().lower()
    source = None
    m = re.search(r"(?:instead of|from|not)\s+([a-z#][a-z0-9#]*)", text)
    if m:
        source = _hue_from_token(m.group(1))
        text = (text[: m.start()] + " " + text[m.end() :]).strip()
    if source is None:
        m = re.search(r"([a-z#][a-z0-9#]*)\s+(?:to|→|->)\s+([a-z#][a-z0-9#]*)", text)
        if m:
            source = _hue_from_token(m.group(1))
            if color is None:
                color = m.group(2)
            text = (text[: m.start()] + " " + m.group(2) + " " + text[m.end() :]).strip()
    target = _hue_from_token(color) if color else None
    if target is None:
        target = _first_named_hue(text)
    return target, source


def recolor_image(image: Image.Image, target_h: float, source_h: float | None) -> Image.Image:
    rgba = image.convert("RGBA")
    arr = np.asarray(rgba).astype(np.float32) / 255.0
    rgb = arr[:, :, :3]
    hsv = _rgb_to_hsv(rgb)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    chroma = (s > 0.18) & (v > 0.12) & (v < 0.97)
    if source_h is not None:
        dist = np.minimum(np.abs(h - source_h), 1.0 - np.abs(h - source_h))
        chroma = chroma & (dist < 0.12)
    if not np.any(chroma):
        chroma = (s > 0.12) & (v > 0.12)
    if not np.any(chroma):
        return rgba

    if source_h is None:
        # circular mean of current chromatic hue
        ang = h[chroma] * 2 * np.pi
        mean = float((np.arctan2(np.sin(ang).mean(), np.cos(ang).mean()) / (2 * np.pi)) % 1.0)
        delta = (target_h - mean + 0.5) % 1.0 - 0.5
    else:
        delta = (target_h - source_h + 0.5) % 1.0 - 0.5

    h2 = h.copy()
    h2[chroma] = (h2[chroma] + delta) % 1.0
    # nudge saturation a bit toward a readable color
    s2 = s.copy()
    s2[chroma] = np.clip(s2[chroma] * 1.05, 0, 1)
    out = _hsv_to_rgb(np.stack([h2, s2, v], axis=-1))
    arr[:, :, :3] = out
    return Image.fromarray(np.clip(arr * 255.0, 0, 255).astype(np.uint8), mode="RGBA")


def _retint_mesh(mesh: Any, target_h: float, source_h: float | None, ref: Image.Image | None, out_dir: Path) -> Any:
    vis = getattr(mesh, "visual", None)
    tex = None
    if vis is not None:
        mat = getattr(vis, "material", None)
        tex = getattr(mat, "baseColorTexture", None) if mat is not None else None
        if tex is None:
            tex = getattr(vis, "image", None)
    if tex is not None:
        img = tex if isinstance(tex, Image.Image) else Image.fromarray(np.asarray(tex))
        new_img = recolor_image(img.convert("RGBA"), target_h, source_h).convert("RGB")
        from texture import cartoonize_image

        new_img = cartoonize_image(new_img)
        new_img.save(out_dir / "albedo.png")
        try:
            vis.material.baseColorTexture = new_img
            vis.material.doubleSided = True
        except Exception:
            from trimesh.visual.material import PBRMaterial
            from trimesh.visual.texture import TextureVisuals

            uv = getattr(vis, "uv", None)
            mesh.visual = TextureVisuals(
                uv=uv,
                image=new_img,
                material=PBRMaterial(
                    baseColorTexture=new_img,
                    metallicFactor=0.0,
                    roughnessFactor=0.55,
                    doubleSided=True,
                ),
            )
        return mesh
    if ref is not None:
        from texture import bake_reference_pbr

        return bake_reference_pbr(mesh, ref, out_dir=out_dir)
    raise ValueError("Job has no albedo texture or reference image to recolor")


def _mesh_path(root: Path) -> Path | None:
    for name in ("model.glb", "model.stl"):
        p = root / name
        if p.is_file():
            return p
    return None


def _hue_from_token(token: str | None) -> float | None:
    if not token:
        return None
    t = token.strip().lower()
    if t in _NAMED:
        return _NAMED[t]
    if t.startswith("#") and len(t) in (4, 7):
        h = t[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        try:
            r, g, b = int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0
        except ValueError:
            return None
        hsv = _rgb_to_hsv(np.array([[[r, g, b]]], dtype=np.float32))
        return float(hsv[0, 0, 0])
    return None


def _first_named_hue(text: str) -> float | None:
    # skip filler words; prefer the last named color ("yellow to green")
    found = None
    for word in re.findall(r"[a-z#][a-z0-9#]*", text):
        if word in ("instead", "of", "from", "to", "make", "it", "be", "want", "please"):
            continue
        h = _hue_from_token(word)
        if h is not None:
            found = h
    return found


def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    v = maxc
    s = np.where(maxc > 1e-8, (maxc - minc) / (maxc + 1e-8), 0.0)
    rc = (maxc - r) / (maxc - minc + 1e-8)
    gc = (maxc - g) / (maxc - minc + 1e-8)
    bc = (maxc - b) / (maxc - minc + 1e-8)
    h = np.zeros_like(maxc)
    h = np.where((maxc == r) & (maxc != minc), (bc - gc) / 6.0, h)
    h = np.where((maxc == g) & (maxc != minc), (2.0 + rc - bc) / 6.0, h)
    h = np.where((maxc == b) & (maxc != minc), (4.0 + gc - rc) / 6.0, h)
    h = h % 1.0
    return np.stack([h, s, v], axis=-1)


def _hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    i = np.floor(h * 6.0).astype(int) % 6
    f = h * 6.0 - np.floor(h * 6.0)
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    out = np.zeros_like(hsv)
    for idx, (c0, c1, c2) in enumerate(
        ((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q))
    ):
        m = i == idx
        out[m, 0], out[m, 1], out[m, 2] = c0[m], c1[m], c2[m]
    return out
