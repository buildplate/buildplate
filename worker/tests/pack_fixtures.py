#!/usr/bin/env python3
"""Decimate a job mesh + reference into tests/fixtures/upright/<id>/."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "upright"
OUT = Path.home() / "buildplate" / "out"
TARGET_FACES = 2500


def _kind_for(name: str, has_photo: bool) -> str:
    n = name.lower()
    if "hat" in n:
        return "hat"
    if not has_photo or "cabinet" in n or "box" in n:
        return "cad"
    return "figurine"


def pack_one(src: Path, dest_name: str | None = None) -> Path:
    import trimesh

    name = dest_name or src.name
    stl = src / "model.stl"
    glb = src / "model.glb"
    mesh_path = stl if stl.is_file() else glb
    if not mesh_path.is_file():
        raise FileNotFoundError(f"no model.stl/glb in {src}")

    mesh = trimesh.load(str(mesh_path), force="mesh")
    if not isinstance(mesh, trimesh.Trimesh):
        mesh = mesh.dump(concatenate=True)
    if len(mesh.faces) > TARGET_FACES:
        try:
            mesh = mesh.simplify_quadric_decimation(TARGET_FACES)
        except Exception:
            rng = np.random.default_rng(0)
            idx = rng.choice(len(mesh.faces), TARGET_FACES, replace=False)
            mesh = mesh.submesh([idx], append=True)

    dest = FIXTURES / name
    dest.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        dest / "mesh.npz",
        vertices=np.asarray(mesh.vertices, dtype=np.float32),
        faces=np.asarray(mesh.faces, dtype=np.int32),
    )

    photo = None
    for cand in ("composited.png", "cutout.png", "input.png", "reference.png"):
        p = src / cand
        if p.is_file():
            photo = p
            break
    if photo is not None:
        im = Image.open(photo).convert("RGBA")
        im.thumbnail((256, 256))
        im.save(dest / "ref.png")

    expect = dest / "expect.json"
    if not expect.is_file():
        expect.write_text(
            json.dumps(
                {
                    "kind": _kind_for(name, photo is not None),
                    "source": str(src),
                },
                indent=2,
            )
            + "\n"
        )
    print(f"packed {name}: faces={len(mesh.faces)} photo={photo is not None}")
    return dest


def main(argv: list[str]) -> int:
    names = argv[1:] or [
        "charmander",
        "cowboy-hat-mesh",
        "liquid-snake",
        "grey-fox-mask",
        "cabinet",
    ]
    for name in names:
        src = Path(name)
        if not src.is_dir():
            src = OUT / name
        dest_name = "cowboy-hat" if src.name == "cowboy-hat-mesh" else src.name
        pack_one(src, dest_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
