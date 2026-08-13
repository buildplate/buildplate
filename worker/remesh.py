"""CPU remesh / cleanup stage — vendor-agnostic (TripoSR, Hunyuan, CAD skip).

Repair (pymeshfix hole-fill) + optional quadric decimation. Not Meshy Smart
Topology, but closes the worst neural holes without a GPU.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger("buildplate-worker")

DEFAULT_TARGET_FACES = 40_000


def remesh_mesh(mesh: Any, target_faces: int = DEFAULT_TARGET_FACES) -> Any:
    if mesh is None:
        return mesh
    before = int(len(mesh.faces)) if hasattr(mesh, "faces") else 0
    mesh = _repair(mesh)
    mesh = _watertight(mesh)
    if target_faces and int(len(mesh.faces)) > target_faces:
        mesh = _decimate(mesh, target_faces)
        mesh = _repair(mesh)
        mesh = _watertight(mesh)
    try:
        mesh.fix_normals()
    except Exception:
        pass
    after = int(len(mesh.faces)) if hasattr(mesh, "faces") else 0
    logger.info("remesh faces %d → %d (target=%d watertight=%s)", before, after, target_faces, getattr(mesh, "is_watertight", "?"))
    return mesh


def _repair(mesh: Any) -> Any:
    try:
        mesh.merge_vertices()
    except Exception:
        pass
    for fn in (
        "remove_degenerate_faces",
        "remove_duplicate_faces",
        "remove_infinite_values",
        "remove_unreferenced_vertices",
    ):
        try:
            getattr(mesh, fn)()
        except Exception:
            pass
    try:
        mesh.fill_holes()
    except Exception:
        pass
    try:
        if hasattr(mesh, "process"):
            mesh.process(validate=False)
    except Exception:
        pass
    return mesh


def _watertight(mesh: Any) -> Any:
    """Fill remaining holes with MeshFix. Keeps small parts (ears/tail)."""
    try:
        if bool(getattr(mesh, "is_watertight", False)):
            return mesh
    except Exception:
        pass
    try:
        import pymeshfix
        import trimesh

        verts = np.asarray(mesh.vertices, dtype=np.float64)
        faces = np.asarray(mesh.faces, dtype=np.int64)
        if len(faces) < 32:
            return mesh
        mf = pymeshfix.MeshFix(verts, faces)
        mf.repair(joincomp=True, remove_smallest_components=False)
        if mf.faces is None or len(mf.faces) < 32:
            return mesh
        out = trimesh.Trimesh(vertices=mf.points, faces=mf.faces, process=False)
        logger.info(
            "pymeshfix faces %d → %d watertight=%s",
            len(faces),
            len(out.faces),
            out.is_watertight,
        )
        return out
    except Exception as err:
        logger.debug("pymeshfix skipped: %s", err)
        return mesh


def _decimate(mesh: Any, target_faces: int) -> Any:
    try:
        out = mesh.simplify_quadric_decimation(int(target_faces))
        if out is not None and len(out.faces) > 0:
            return out
    except Exception as err:
        logger.debug("trimesh quadric decimate failed: %s", err)
    try:
        import fast_simplification
        import trimesh

        v, f = fast_simplification.simplify(
            mesh.vertices,
            mesh.faces,
            target_count=int(target_faces),
        )
        return trimesh.Trimesh(vertices=v, faces=f, process=False)
    except Exception as err:
        logger.debug("fast_simplification failed: %s", err)
    return mesh
