"""Mesh cleanup: strip relief backing, snap upright for print/preview."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger("buildplate-worker")


def postprocess_mesh(mesh: Any, *, strip_relief: bool = True) -> Any:
    if mesh is None:
        return mesh

    mesh = _keep_best_component(mesh)
    if strip_relief:
        mesh = _strip_relief_backing(mesh)
    mesh = _remove_large_planar_facets(mesh)
    mesh = _pca_upright(mesh)
    mesh = _trim_horizontal_brims(mesh)
    mesh = _keep_best_component(mesh)
    return mesh


def orient_mesh(mesh: Any, image: Any | None = None) -> Any:
    """Plant feet (not the tail) and yaw to face the reference photo."""
    if mesh is None:
        return mesh
    planted = _plant_feet_and_head(mesh)
    if planted is not None:
        mesh = planted
    if image is not None:
        mesh = _yaw_to_image(mesh, image)
    return _sit_on_ground(mesh)


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
        if thin_ratio > 0.16:
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


def _euler(pitch_deg: float, yaw_deg: float, roll_deg: float) -> np.ndarray:
    p, y, r = np.deg2rad([pitch_deg, yaw_deg, roll_deg])
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    cr, sr = np.cos(r), np.sin(r)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cp, -sp], [0.0, sp, cp]])
    ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]])
    rz = np.array([[cr, -sr, 0.0], [sr, cr, 0.0], [0.0, 0.0, 1.0]])
    return ry @ rx @ rz


def _apply_rot(mesh: Any, rot: np.ndarray) -> Any:
    out = mesh.copy()
    center = np.asarray(out.vertices, dtype=float).mean(axis=0)
    out.vertices = (np.asarray(out.vertices, dtype=float) - center) @ rot.T
    return out


def _rot_align(frm: np.ndarray, to: np.ndarray) -> np.ndarray:
    a = frm / (np.linalg.norm(frm) + 1e-12)
    b = to / (np.linalg.norm(to) + 1e-12)
    cr = np.cross(a, b)
    n = np.linalg.norm(cr)
    if n < 1e-8:
        return np.eye(3) if float(np.dot(a, b)) > 0 else np.diag([1.0, -1.0, 1.0])
    cr = cr / n
    ang = np.arctan2(n, float(np.dot(a, b)))
    k = np.array([[0.0, -cr[2], cr[1]], [cr[2], 0.0, -cr[0]], [-cr[1], cr[0], 0.0]])
    return np.eye(3) + np.sin(ang) * k + (1.0 - np.cos(ang)) * (k @ k)


def _cluster_points(pts: np.ndarray, eps: float) -> list[dict]:
    cls: list[dict] = []
    for p in pts:
        hit = None
        for cl in cls:
            if np.linalg.norm(p - cl["sum"] / cl["n"]) < eps:
                hit = cl
                break
        if hit is None:
            cls.append({"sum": p.astype(float).copy(), "n": 1, "pts": [p]})
        else:
            hit["sum"] += p
            hit["n"] += 1
            hit["pts"].append(p)
    return cls


def _protrusions(verts: np.ndarray) -> list[dict]:
    com = verts.mean(axis=0)
    centered = verts - com
    _, _, vh = np.linalg.svd(centered, full_matrices=False)
    coords = centered @ vh.T
    radius = np.linalg.norm(coords / (coords.std(axis=0) + 1e-8), axis=1)
    scale = float(np.linalg.norm(verts.max(axis=0) - verts.min(axis=0)))
    pts = verts[radius > 2.2]
    if len(pts) < 40:
        return []
    raw = _cluster_points(pts, eps=0.10 * scale)
    feats: list[dict] = []
    for cl in raw:
        if cl["n"] < 20:
            continue
        p = np.stack(cl["pts"])
        mean = p.mean(axis=0)
        off = mean - com
        dist = float(np.linalg.norm(off)) + 1e-12
        _, s, _ = np.linalg.svd(p - mean, full_matrices=False)
        aniso = float(s[0] / (s[-1] + 1e-8))
        feats.append(
            {
                "n": cl["n"],
                "off": off,
                "mean": mean,
                "aniso": aniso,
                "dist": dist,
                "u": off / dist,
                "pts": list(p),
            }
        )
    merged: list[dict] = []
    for feat in sorted(feats, key=lambda x: -x["n"]):
        hit = None
        for other in merged:
            if float(np.dot(feat["u"], other["u"])) > 0.85:
                hit = other
                break
        if hit is None:
            merged.append(feat)
            continue
        hit["pts"].extend(feat["pts"])
        hit["n"] += feat["n"]
        p = np.stack(hit["pts"])
        hit["mean"] = p.mean(axis=0)
        hit["off"] = hit["mean"] - com
        hit["dist"] = float(np.linalg.norm(hit["off"])) + 1e-12
        hit["u"] = hit["off"] / hit["dist"]
    return merged


def _plant_feet_and_head(mesh: Any) -> Any | None:
    """Up = ears minus feet. Tail is the longest thin stick and must not be the base."""
    verts = np.asarray(mesh.vertices, dtype=float)
    try:
        feats = _protrusions(verts)
    except Exception as err:
        logger.debug("protrusions failed: %s", err)
        return None
    sticks = [f for f in feats if f["aniso"] > 6]
    blobs = [f for f in feats if f["aniso"] <= 6]
    if len(sticks) < 2 or len(blobs) < 2:
        logger.info("orient: not enough limbs (sticks=%d blobs=%d)", len(sticks), len(blobs))
        return None
    tail = max(sticks, key=lambda f: f["dist"])
    ears = [f for f in sticks if float(np.dot(f["u"], tail["u"])) < 0.7]
    if len(ears) < 1:
        return None
    ear_mid = np.mean([f["mean"] for f in ears[:2]], axis=0)
    com = verts.mean(axis=0)
    ear_dir = ear_mid - com
    ear_dir = ear_dir / (np.linalg.norm(ear_dir) + 1e-12)
    feet = sorted(blobs, key=lambda f: float(np.dot(f["off"], ear_dir)))[:2]
    foot_mid = np.mean([f["mean"] for f in feet], axis=0)
    up = ear_mid - foot_mid
    if np.linalg.norm(up) < 1e-8:
        return None
    rot = _rot_align(up, np.array([0.0, 1.0, 0.0]))
    out = _apply_rot(mesh, rot)
    # Confirm tail is not the lowest feature
    def ymin(pts: np.ndarray) -> float:
        return float(((pts - com) @ rot.T)[:, 1].min())

    tail_y = ymin(np.stack(tail["pts"]))
    foot_y = min(ymin(np.stack(f["pts"])) for f in feet)
    logger.info(
        "orient plant feet_y=%.3f tail_y=%.3f ears=%d",
        foot_y,
        tail_y,
        len(ears),
    )
    if tail_y < foot_y - 0.02:
        logger.info("orient plant rejected (tail still lower than feet)")
        return None
    return out


def _yaw_to_image(mesh: Any, image: Any) -> Any:
    """Rotate around Y only so the front matches the reference photo."""
    from texture import _image_arrays, _iou, _occupancy, _preview_mesh, _resize_mask

    _rgb, mask = _image_arrays(image)
    target = _resize_mask(mask, 80)
    target_prof = target.mean(axis=1)
    verts = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces, dtype=int)
    v_s, f_s = _preview_mesh(verts, faces, 3500)
    center = v_s.mean(axis=0)
    best = (-1e9, np.eye(3))
    for yaw in range(0, 360, 15):
        rot = _euler(0.0, yaw, 0.0)
        vv = (v_s - center) @ rot.T
        occ = _occupancy(vv, f_s, az=0, el=12, size=80)
        iou = _iou(occ, target)
        iou_m = _iou(np.fliplr(occ), target)
        corr = _corr(occ.mean(axis=1), target_prof)
        corr_m = _corr(np.fliplr(occ).mean(axis=1), target_prof)
        s = iou + 0.4 * corr
        s_m = iou_m + 0.4 * corr_m
        if s > best[0]:
            best = (s, rot)
        if s_m > best[0]:
            best = (s_m, np.diag([-1.0, 1.0, 1.0]) @ rot)
    logger.info("orient yaw-to-image score=%.3f", best[0])
    return _apply_rot(mesh, best[1])


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    if a.size != b.size or a.size < 4:
        return 0.0
    a = a - a.mean()
    b = b - b.mean()
    den = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-8
    return float(np.dot(a, b) / den)


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
