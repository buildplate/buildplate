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
    mesh = _orient_feet_down(mesh)  # head/ears wide tip → top; feet compact → bottom
    mesh = _trim_horizontal_brims(mesh)
    mesh = _keep_best_component(mesh)
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
    """Drop the flat card behind a TripoSR relief; keep the protruding subject."""
    try:
        verts = np.asarray(mesh.vertices, dtype=float)
        faces = np.asarray(mesh.faces, dtype=int)
        if len(verts) < 100 or len(faces) < 100:
            return mesh

        center = verts.mean(axis=0)
        centered = verts - center
        _, s, vh = np.linalg.svd(centered, full_matrices=False)
        thin_ratio = float(s[-1] / (s[0] + 1e-8))
        if thin_ratio > 0.45:
            return mesh

        normal = vh[-1]
        depths = centered @ normal
        dmin, dmax = float(depths.min()), float(depths.max())
        span = dmax - dmin + 1e-8
        lo_count = int(np.sum(depths < dmin + 0.2 * span))
        hi_count = int(np.sum(depths > dmax - 0.2 * span))
        if lo_count >= hi_count:
            cutoff = dmin + 0.28 * span
            keep_vert = depths >= cutoff
        else:
            cutoff = dmax - 0.28 * span
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
            if float(areas[facet_idx]) / total < 0.08:
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
            if thin < 0.12:
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
    """Align most variance → Y, least → Z."""
    verts = np.asarray(mesh.vertices, dtype=float)
    center = verts.mean(axis=0)
    centered = verts - center
    try:
        _, _, vh = np.linalg.svd(centered, full_matrices=False)
    except Exception:
        return mesh

    x_axis = vh[1]
    y_axis = vh[0]
    z_axis = vh[2]
    if np.dot(np.cross(x_axis, y_axis), z_axis) < 0:
        z_axis = -z_axis
    rot = np.eye(4)
    rot[:3, :3] = np.stack([x_axis, y_axis, z_axis], axis=0)

    out = mesh.copy()
    out.apply_transform(rot)
    logger.info("pca upright applied")
    return out


def _orient_feet_down(mesh: Any) -> Any:
    """
    Prefer the orientation whose *bottom contact patch* is smaller.
    Feet make a compact footprint; an upside-down head/ear-brim does not.
    """
    def prepare(m: Any, flip: bool) -> Any:
        out = m.copy()
        if flip:
            out.vertices[:, 1] *= -1
        out.vertices[:, 1] -= out.vertices[:, 1].min()
        return out

    def bottom_contact(m: Any) -> float:
        v = np.asarray(m.vertices, dtype=float)
        ymin = float(v[:, 1].min())
        span = float(np.ptp(v[:, 1])) + 1e-8
        bot = v[v[:, 1] <= ymin + 0.1 * span]
        if len(bot) < 12:
            return 1e9
        try:
            from scipy.spatial import ConvexHull

            return float(ConvexHull(bot[:, [0, 2]]).volume)  # 2D area
        except Exception:
            return float(np.ptp(bot[:, 0]) * np.ptp(bot[:, 2]))

    a = prepare(mesh, flip=False)
    b = prepare(mesh, flip=True)
    ca, cb = bottom_contact(a), bottom_contact(b)
    logger.info("orient contact upright=%.5f flipped=%.5f", ca, cb)
    if cb < ca * 0.92:
        logger.info("orient: using flipped (smaller foot contact)")
        return b
    logger.info("orient: keeping current")
    return a


def _trim_horizontal_brims(mesh: Any) -> Any:
    """
    Remove flat fins/halos around ears: nearly-horizontal faces on the outer
    XZ ring, especially near the top (ears) or bottom.
    """
    try:
        verts = np.asarray(mesh.vertices, dtype=float)
        faces = np.asarray(mesh.faces, dtype=int)
        if len(faces) < 100:
            return mesh

        v0 = verts[faces[:, 0]]
        v1 = verts[faces[:, 1]]
        v2 = verts[faces[:, 2]]
        normals = np.cross(v1 - v0, v2 - v0)
        nlen = np.linalg.norm(normals, axis=1, keepdims=True) + 1e-12
        normals = normals / nlen
        horiz = np.abs(normals[:, 1]) > 0.72

        center_xz = verts[:, [0, 2]].mean(axis=0)
        face_c = (v0 + v1 + v2) / 3.0
        radii = np.linalg.norm(face_c[:, [0, 2]] - center_xz, axis=1)
        r_cut = float(np.percentile(radii, 55))

        ymin, ymax = float(verts[:, 1].min()), float(verts[:, 1].max())
        span = ymax - ymin + 1e-8
        # Ears / head zone (upper 40%) and accidental bottom skirts
        near_end = (face_c[:, 1] > ymin + 0.55 * span) | (face_c[:, 1] < ymin + 0.22 * span)

        suspect = horiz & (radii > r_cut) & near_end
        n_drop = int(np.sum(suspect))
        if n_drop < 15:
            return mesh
        # Don't delete more than a third of the mesh
        if n_drop > 0.33 * len(faces):
            # Stricter: outer 30% only
            r_cut2 = float(np.percentile(radii, 70))
            suspect = horiz & (radii > r_cut2) & near_end
            n_drop = int(np.sum(suspect))
            if n_drop < 15 or n_drop > 0.33 * len(faces):
                return mesh

        keep = np.where(~suspect)[0]
        logger.info("trimmed brim faces=%d/%d", n_drop, len(faces))
        return mesh.submesh([keep], append=True)
    except Exception as err:
        logger.debug("brim trim skipped: %s", err)
        return mesh


def _sit_on_ground(mesh: Any) -> Any:
    mesh = mesh.copy()
    c = mesh.bounds.mean(axis=0)
    mesh.vertices[:, 0] -= c[0]
    mesh.vertices[:, 2] -= c[2]
    mesh.vertices[:, 1] -= mesh.bounds[0][1]
    return mesh
