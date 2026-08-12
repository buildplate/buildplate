"""Mesh cleanup: strip relief backing, snap upright for print/preview."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger("buildplate-worker")


def postprocess_mesh(mesh: Any) -> Any:
    if mesh is None:
        return mesh

    mesh = _keep_best_component(mesh)
    mesh = _strip_relief_backing(mesh)
    mesh = _remove_large_planar_facets(mesh)
    mesh = _pca_upright(mesh)
    mesh = _sit_on_ground(mesh)
    return mesh


def _keep_best_component(mesh: Any) -> Any:
    try:
        parts = mesh.split(only_watertight=False)
    except Exception:
        return mesh
    if not parts or len(parts) == 1:
        return mesh

    def score(m: Any) -> float:
        extents = np.sort(np.asarray(m.extents, dtype=float))
        thin = float(extents[0] / (extents[2] + 1e-8))
        if thin < 0.05:
            return -1.0
        try:
            vol = float(m.volume) if m.is_volume else 0.0
        except Exception:
            vol = 0.0
        return vol * 10.0 + float(len(m.faces)) * thin

    best = max(parts, key=score)
    logger.info("components=%d kept faces=%d", len(parts), len(best.faces))
    return best


def _strip_relief_backing(mesh: Any) -> Any:
    """
    TripoSR often returns a character embossed on a flat card.
    Drop faces sitting on the back plane; keep the protruding subject.
    """
    try:
        verts = np.asarray(mesh.vertices, dtype=float)
        faces = np.asarray(mesh.faces, dtype=int)
        if len(verts) < 100 or len(faces) < 100:
            return mesh

        center = verts.mean(axis=0)
        centered = verts - center
        _, s, vh = np.linalg.svd(centered, full_matrices=False)
        # If not flat-ish, skip
        thin_ratio = float(s[-1] / (s[0] + 1e-8))
        if thin_ratio > 0.35:
            return mesh

        normal = vh[-1]
        depths = centered @ normal
        dmin, dmax = float(depths.min()), float(depths.max())
        span = dmax - dmin + 1e-8
        # Back plane = the side with more vertices packed near the extreme
        lo_count = int(np.sum(depths < dmin + 0.2 * span))
        hi_count = int(np.sum(depths > dmax - 0.2 * span))
        if lo_count >= hi_count:
            # back is low side; keep upper protrusion
            cutoff = dmin + 0.22 * span
            keep_vert = depths >= cutoff
        else:
            cutoff = dmax - 0.22 * span
            keep_vert = depths <= cutoff

        face_keep = keep_vert[faces].any(axis=1)
        n_keep = int(np.sum(face_keep))
        if n_keep < 80 or n_keep == len(faces):
            return mesh
        logger.info(
            "strip relief thin=%.3f kept_faces=%d/%d",
            thin_ratio,
            n_keep,
            len(faces),
        )
        return mesh.submesh([np.where(face_keep)[0]], append=True)
    except Exception as err:
        logger.debug("strip relief skipped: %s", err)
        return mesh


def _remove_large_planar_facets(mesh: Any) -> Any:
    try:
        if not hasattr(mesh, "facets") or mesh.facets is None or len(mesh.facets) == 0:
            return mesh
        areas = np.asarray(mesh.facets_area, dtype=float)
        total = float(np.sum(areas)) + 1e-8
        drop: list[int] = []
        for facet_idx, facet in enumerate(mesh.facets):
            if float(areas[facet_idx]) / total < 0.10:
                continue
            verts = mesh.vertices[np.unique(mesh.faces[facet].reshape(-1))]
            if len(verts) < 9:
                continue
            centered = verts - verts.mean(axis=0)
            try:
                _, s, _ = np.linalg.svd(centered, full_matrices=False)
                thin = float(s[-1] / (s[0] + 1e-8))
            except Exception:
                continue
            if thin < 0.1:
                drop.extend(int(i) for i in facet)
        if not drop:
            return mesh
        drop_set = set(drop)
        keep = [i for i in range(len(mesh.faces)) if i not in drop_set]
        if len(keep) < 50 or len(keep) == len(mesh.faces):
            return mesh
        logger.info("removed %d planar faces", len(drop_set))
        return mesh.submesh([keep], append=True)
    except Exception:
        return mesh


def _pca_upright(mesh: Any) -> Any:
    """
    Align principal axes:
      most variance → Y (height)
      least variance → Z (depth)
    """
    verts = np.asarray(mesh.vertices, dtype=float)
    center = verts.mean(axis=0)
    centered = verts - center
    try:
        _, _, vh = np.linalg.svd(centered, full_matrices=False)
    except Exception:
        return mesh

    # vh[0] = direction of most variance, vh[2] = least
    # Build rotation that maps vh[0]→Y, vh[2]→Z, vh[1]→X
    x_axis = vh[1]
    y_axis = vh[0]
    z_axis = vh[2]
    # Ensure right-handed
    if np.dot(np.cross(x_axis, y_axis), z_axis) < 0:
        z_axis = -z_axis
    rot = np.eye(4)
    # World basis from these axes: columns are where unit axes go...
    # We want: new = R @ old, such that vh[0] becomes (0,1,0)
    basis = np.stack([x_axis, y_axis, z_axis], axis=0)  # rows
    rot[:3, :3] = basis

    out = mesh.copy()
    out.apply_transform(rot)
    # Prefer heavy bottom
    mid = 0.5 * (out.vertices[:, 1].min() + out.vertices[:, 1].max())
    low = int(np.sum(out.vertices[:, 1] <= mid))
    high = int(np.sum(out.vertices[:, 1] > mid))
    if high > low * 1.1:
        out.vertices[:, 1] *= -1
        logger.info("pca upright: flipped Y")
    logger.info("pca upright applied")
    return out


def _sit_on_ground(mesh: Any) -> Any:
    mesh = mesh.copy()
    c = mesh.bounds.mean(axis=0)
    mesh.vertices[:, 0] -= c[0]
    mesh.vertices[:, 2] -= c[2]
    mesh.vertices[:, 1] -= mesh.bounds[0][1]
    return mesh
