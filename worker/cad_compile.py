"""
Local CAD compilers for Buildplate.

The *agent* authors geometry (OpenSCAD / CadQuery / trimesh Python).
This module only compiles that source to STL on the user's machine.

Engines (first available wins for engine=auto):
  - openscad  — OpenSCAD CLI if installed
  - cadquery  — CadQuery / OpenCASCADE if pip-installed
  - trimesh   — always available (trimesh + manifold3d CSG)
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("buildplate-worker")

_ALLOWED_IMPORTS = frozenset({"trimesh", "numpy", "np", "math", "cadquery", "cq"})


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if root not in _ALLOWED_IMPORTS and name not in _ALLOWED_IMPORTS:
        raise ImportError(f"CAD sandbox blocks import of {name!r}")
    return __import__(name, globals, locals, fromlist, level)


# Restrict agent-executed Python to geometry helpers (local MCP trust model).
_SAFE_BUILTINS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "range": range,
    "len": len,
    "float": float,
    "int": int,
    "bool": bool,
    "str": str,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "enumerate": enumerate,
    "zip": zip,
    "sum": sum,
    "print": print,
    "isinstance": isinstance,
    "type": type,
    "hasattr": hasattr,
    "__import__": _safe_import,
    "True": True,
    "False": False,
    "None": None,
}


@dataclass
class CadCompileResult:
    path: Path
    engine: str
    seconds: float
    meta: dict[str, Any]


def find_openscad() -> str | None:
    env = os.environ.get("BUILDPLATE_OPENSCAD", "").strip()
    if env and Path(env).is_file():
        return env
    for name in ("openscad", "OpenSCAD"):
        found = shutil.which(name)
        if found:
            return found
    mac_candidates = [
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
        "/Applications/OpenSCAD-2021.01.app/Contents/MacOS/OpenSCAD",
        "/Applications/OpenSCAD-2024.12.app/Contents/MacOS/OpenSCAD",
    ]
    for p in mac_candidates:
        if Path(p).is_file():
            return p
    return None


def cadquery_available() -> bool:
    try:
        import cadquery  # noqa: F401

        return True
    except Exception:
        return False


def trimesh_cad_available() -> bool:
    try:
        import trimesh  # noqa: F401
        import manifold3d  # noqa: F401

        return True
    except Exception:
        return False


def available_engines() -> list[str]:
    engines: list[str] = []
    if find_openscad():
        engines.append("openscad")
    if cadquery_available():
        engines.append("cadquery")
    if trimesh_cad_available():
        engines.append("trimesh")
    return engines


def cad_ready() -> bool:
    return len(available_engines()) > 0


def compile_cad(
    *,
    out_dir: Path,
    openscad: str | None = None,
    cadquery: str | None = None,
    trimesh_code: str | None = None,
    engine: str | None = None,
    prompt: str | None = None,
) -> CadCompileResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    stl_path = out_dir / "model.stl"
    choice = (engine or "auto").strip().lower()
    t0 = time.time()

    sources = {
        "openscad": (openscad or "").strip() or None,
        "cadquery": (cadquery or "").strip() or None,
        "trimesh": (trimesh_code or "").strip() or None,
    }

    if choice == "auto":
        # Prefer the engine that has source; else prefer openscad → cadquery → trimesh.
        for name in ("openscad", "cadquery", "trimesh"):
            if sources[name]:
                choice = name
                break
        else:
            raise ValueError(
                "CAD mode needs agent-authored source: openscad, cadquery, or trimesh_code"
            )

    if choice == "openscad":
        if not sources["openscad"]:
            raise ValueError("openscad source required for engine=openscad")
        _compile_openscad(sources["openscad"], stl_path, out_dir)
    elif choice == "cadquery":
        if not sources["cadquery"]:
            raise ValueError("cadquery source required for engine=cadquery")
        _compile_cadquery(sources["cadquery"], stl_path, out_dir)
    elif choice == "trimesh":
        if not sources["trimesh"]:
            raise ValueError("trimesh_code required for engine=trimesh")
        _compile_trimesh(sources["trimesh"], stl_path, out_dir)
    else:
        raise ValueError(f"Unknown CAD engine: {choice}")

    if not stl_path.is_file() or stl_path.stat().st_size < 84:
        raise RuntimeError(f"CAD compile produced empty STL ({choice})")

    # Preview still
    try:
        import trimesh
        from pipeline import _save_mesh_still

        mesh = trimesh.load(stl_path, force="mesh")
        if mesh is not None:
            _save_mesh_still(mesh, out_dir / "preview.png")
    except Exception as err:
        logger.warning("CAD preview failed: %s", err)

    # Persist sources for debugging / iteration
    if sources["openscad"]:
        (out_dir / "model.scad").write_text(sources["openscad"], encoding="utf-8")
    if sources["cadquery"]:
        (out_dir / "model_cq.py").write_text(sources["cadquery"], encoding="utf-8")
    if sources["trimesh"]:
        (out_dir / "model_trimesh.py").write_text(sources["trimesh"], encoding="utf-8")

    elapsed = time.time() - t0
    return CadCompileResult(
        path=stl_path,
        engine=choice,
        seconds=round(elapsed, 2),
        meta={
            "backend": "cad",
            "engine": choice,
            "prompt": prompt,
            "seconds": round(elapsed, 2),
            "engines_available": available_engines(),
        },
    )


def _compile_openscad(source: str, stl_path: Path, out_dir: Path) -> None:
    binary = find_openscad()
    if not binary:
        raise RuntimeError(
            "OpenSCAD CLI not found. Install via brew (`brew install --cask openscad`) "
            "or set BUILDPLATE_OPENSCAD, or use engine=trimesh / cadquery instead."
        )
    scad = out_dir / "model.scad"
    scad.write_text(source, encoding="utf-8")
    cmd = [binary, "-o", str(stl_path), str(scad)]
    logger.info("OpenSCAD: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not stl_path.is_file():
        err = (proc.stderr or proc.stdout or "openscad failed").strip()
        raise RuntimeError(f"OpenSCAD compile failed: {err[:2000]}")


def _compile_cadquery(source: str, stl_path: Path, out_dir: Path) -> None:
    if not cadquery_available():
        raise RuntimeError(
            "CadQuery not installed. Run: pip install cadquery  (in worker/.venv) "
            "or use engine=trimesh / openscad."
        )
    import cadquery as cq

    ns: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "cq": cq,
        "cadquery": cq,
        "result": None,
    }
    try:
        exec(compile(source, "<cadquery>", "exec"), ns, ns)
    except Exception as err:
        raise RuntimeError(f"CadQuery exec failed: {err}") from err

    result = ns.get("result")
    if result is None:
        raise RuntimeError(
            "CadQuery source must assign `result` to a Workplane / Shape / Assembly"
        )

    # Normalize to exportable solid
    solid = result
    if hasattr(result, "val"):
        solid = result.val()
    if hasattr(solid, "toCompound"):
        solid = solid.toCompound()

    tmp = out_dir / "_cq_export.stl"
    try:
        if hasattr(solid, "exportStl"):
            solid.exportStl(str(tmp))
        else:
            cq.exporters.export(result, str(tmp))
    except Exception:
        cq.exporters.export(result, str(tmp))

    if not tmp.is_file():
        raise RuntimeError("CadQuery export produced no STL")
    tmp.replace(stl_path)


def _compile_trimesh(source: str, stl_path: Path, out_dir: Path) -> None:
    import numpy as np
    import trimesh

    ns: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "trimesh": trimesh,
        "np": np,
        "numpy": np,
        "result": None,
    }
    try:
        exec(compile(source, "<trimesh_cad>", "exec"), ns, ns)
    except Exception as err:
        raise RuntimeError(f"trimesh CAD exec failed: {err}") from err

    result = ns.get("result")
    if result is None:
        raise RuntimeError(
            "trimesh_code must assign `result` to a trimesh.Trimesh (or Scene)"
        )

    if isinstance(result, trimesh.Scene):
        mesh = result.dump(concatenate=True)
    elif isinstance(result, trimesh.Trimesh):
        mesh = result
    else:
        raise RuntimeError(f"result must be Trimesh/Scene, got {type(result)}")

    if mesh.is_empty:
        raise RuntimeError("trimesh CAD result is empty")

    mesh.export(stl_path)
