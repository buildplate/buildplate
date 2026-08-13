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
    mesh = _pca_oblate_only(mesh)
    mesh = _keep_best_component(mesh)
    return mesh


def orient_mesh(mesh: Any, image: Any | None = None) -> Any:
    """Feet on the ground, face matching the reference photo when we have one.

    A photo's up is the source of truth for figurines. Longest-axis-up is only
    used when there is no photo (it stands tails on end).
    """
    if mesh is None:
        return mesh
    kind = _shape_kind(mesh)
    if kind == "oblate":
        mesh = _pca_oblate_only(mesh)
        mesh = _wide_end_down(mesh)
        if image is not None:
            mesh = _yaw_photo_to_front(mesh, image)
        return _sit_on_ground(mesh)

    if kind != "chunky":
        mesh = _keep_thin_axis_as_depth(mesh)
    if image is not None:
        # Photo up first (roll). Yaw/pitch after that — but not on pancakes;
        # pose-to-image lays relief cards on their side.
        mesh = _upright_silhouette(mesh, image)
        if kind != "pancake":
            mesh = _pose_to_image(mesh, image, pitch_span=45)
            mesh = _upright_silhouette(mesh, image)
    else:
        planted = _plant_feet_and_head(mesh)
        if planted is not None:
            mesh = planted
        else:
            mesh = _flip_if_head_down(mesh)
        if kind != "chunky":
            mesh = _keep_thin_axis_as_depth(mesh)
        if kind != "pancake":
            mesh = _feet_end_down(mesh)
            mesh = _align_longest_to_up(mesh)
            mesh = _keep_thin_axis_as_depth(mesh)
    if kind not in ("chunky", "pancake"):
        mesh = _trim_horizontal_brims(mesh)
    return _sit_on_ground(mesh)


def _shape_kind(mesh: Any) -> str:
    """pancake = relief card; oblate = hat/plate/bowl; chunky = helmet/mask; upright = figurine."""
    verts = np.asarray(mesh.vertices, dtype=float)
    if len(verts) < 32:
        return "upright"
    try:
        _, s, _ = np.linalg.svd(verts - verts.mean(axis=0), full_matrices=False)
    except Exception:
        return "upright"
    r_mid = float(s[1] / (s[0] + 1e-8))
    r_thin = float(s[2] / (s[0] + 1e-8))
    if r_thin < 0.22:
        return "pancake"
    if r_mid > 0.85 and r_thin < 0.70:
        return "oblate"
    if r_thin > 0.60:
        return "chunky"
    return "upright"


def _contact_span(mesh: Any, flip: bool = False) -> float:
    v = np.asarray(mesh.vertices, dtype=float).copy()
    if flip:
        v[:, 1] *= -1
    ymin = float(v[:, 1].min())
    span = float(np.ptp(v[:, 1])) + 1e-8
    bot = v[v[:, 1] <= ymin + 0.12 * span]
    if len(bot) < 12:
        return 0.0
    return float(np.ptp(bot[:, 0]) * np.ptp(bot[:, 2]))


def _align_longest_to_up(mesh: Any) -> Any:
    """Stand the figure: the longest 3D axis is head-to-feet, not a lean in XY."""
    verts = np.asarray(mesh.vertices, dtype=float)
    if len(verts) < 32:
        return mesh
    center = verts.mean(axis=0)
    try:
        _, _, vh = np.linalg.svd(verts - center, full_matrices=False)
    except Exception:
        return mesh
    axis = vh[0]
    if float(axis[1]) < 0:
        axis = -axis
    tilt = float(np.degrees(np.arccos(np.clip(float(axis[1]), -1.0, 1.0))))
    if tilt < 8.0:
        return mesh
    logger.info("orient align longest→Y tilt=%.1f°", tilt)
    return _apply_rot(mesh, _rot_align(axis, np.array([0.0, 1.0, 0.0])))


def _wide_end_down(mesh: Any) -> Any:
    """Hats/plates: the wide face is the base, not the crown."""
    upright = _contact_span(mesh, False)
    flipped = _contact_span(mesh, True)
    if flipped > upright * 1.15:
        logger.info("orient wide-end-down contact upright=%.4f flipped=%.4f", upright, flipped)
        return _apply_rot(mesh, _euler(180.0, 0.0, 0.0))
    return mesh


def _keep_thin_axis_as_depth(mesh: Any) -> Any:
    """Single-view meshes are pancakes. Keep the flat face as the back, not a side cut."""
    verts = np.asarray(mesh.vertices, dtype=float)
    if len(verts) < 32:
        return mesh
    center = verts.mean(axis=0)
    try:
        _, _, vh = np.linalg.svd(verts - center, full_matrices=False)
    except Exception:
        return mesh
    thin = vh[-1]
    if abs(float(thin[0])) <= abs(float(thin[2])):
        return mesh
    logger.info("rotate 90° so thin axis is depth (thin x=%.2f z=%.2f)", thin[0], thin[2])
    return _apply_rot(mesh, _euler(0.0, 90.0, 0.0))


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


def _pca_oblate_only(mesh: Any) -> Any:
    """Hats/plates: shortest → Y so they sit on the large face. Leave figurines alone."""
    verts = np.asarray(mesh.vertices, dtype=float)
    center = verts.mean(axis=0)
    centered = verts - center
    try:
        _, s, vh = np.linalg.svd(centered, full_matrices=False)
    except Exception:
        return mesh

    r_mid = float(s[1] / (s[0] + 1e-8))
    r_thin = float(s[2] / (s[0] + 1e-8))
    if r_mid > 0.85 and 0.22 <= r_thin < 0.70:
        return _pca_oblate_sit(mesh, vh)
    return mesh


def _pca_oblate_sit(mesh: Any, vh: np.ndarray) -> Any:
    """Map the short axis to Y and keep the wider end on the ground."""
    up = vh[2]
    best = None
    best_score = -1.0
    for sgn in (1.0, -1.0):
        y_axis = up * sgn
        x_axis = vh[1]
        z_axis = np.cross(x_axis, y_axis)
        if np.linalg.norm(z_axis) < 1e-8:
            x_axis = vh[0]
            z_axis = np.cross(x_axis, y_axis)
        z_axis = z_axis / (np.linalg.norm(z_axis) + 1e-12)
        x_axis = np.cross(y_axis, z_axis)
        x_axis = x_axis / (np.linalg.norm(x_axis) + 1e-12)
        if np.dot(np.cross(x_axis, y_axis), z_axis) < 0:
            z_axis = -z_axis
        rot = np.eye(4)
        rot[:3, :3] = np.stack([x_axis, y_axis, z_axis], axis=0)
        cand = mesh.copy()
        cand.apply_transform(rot)
        score = _contact_span(cand)
        if score > best_score:
            best, best_score = cand, score
    logger.info("pca upright oblate short→up contact=%.4f", best_score)
    return best if best is not None else mesh


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


def _pose_to_image(mesh: Any, image: Any, *, pitch_span: int = 30) -> Any:
    """Yaw + pitch so the photo's up/silhouette match the mesh."""
    from texture import _image_arrays, _iou, _occupancy, _preview_mesh, _resize_mask

    _rgb, mask = _image_arrays(image)
    target = _resize_mask(mask, 80)
    target_prof = target.mean(axis=1)
    verts = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces, dtype=int)
    v_s, f_s = _preview_mesh(verts, faces, 3500)
    center = v_s.mean(axis=0)
    best = (-1e9, np.eye(3), False, 0.0)
    for flip in (False, True):
        base = _euler(180.0, 0.0, 0.0) if flip else np.eye(3)
        for pitch in range(-int(pitch_span), int(pitch_span) + 1, 15):
            for yaw in range(0, 360, 15):
                rot = _euler(float(pitch), float(yaw), 0.0) @ base
                vv = (v_s - center) @ rot.T
                occ = _occupancy(vv, f_s, az=0, el=12, size=80)
                iou = _iou(occ, target)
                iou_m = _iou(np.fliplr(occ), target)
                corr = _corr(occ.mean(axis=1), target_prof)
                corr_m = _corr(np.fliplr(occ).mean(axis=1), target_prof)
                # Vertical profile outweighs IoU: an inverted character still overlaps a lot.
                # Prefer small pitch so round helmets don't nod into the floor.
                s = iou + 1.2 * corr - 0.004 * abs(pitch)
                s_m = iou_m + 1.2 * corr_m - 0.004 * abs(pitch)
                if s > best[0]:
                    best = (s, rot, flip, float(pitch))
                if s_m > best[0]:
                    best = (s_m, np.diag([-1.0, 1.0, 1.0]) @ rot, flip, float(pitch))
    logger.info(
        "orient pose-to-image score=%.3f flip=%s pitch=%.0f",
        best[0],
        best[2],
        best[3],
    )
    mesh = _apply_rot(mesh, best[1])
    vv = (v_s - center) @ best[1].T
    occ = _occupancy(vv, f_s, az=0, el=12, size=80)
    if _corr(np.flipud(occ).mean(axis=1), target_prof) > _corr(occ.mean(axis=1), target_prof) + 0.04:
        logger.info("orient extra 180° — occupancy still inverted vs photo")
        mesh = _apply_rot(mesh, _euler(180.0, 0.0, 0.0))
    return mesh


def _yaw_photo_to_front(mesh: Any, image: Any) -> Any:
    """Turn the photo-matching face toward +Z so preview/Bambu see the front."""
    from texture import _best_view, _image_arrays

    _rgb, mask = _image_arrays(image)
    verts = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces, dtype=int)
    az, el, _mirror = _best_view(verts, faces, mask)
    delta = 180.0 - float(az)
    if delta > 180.0:
        delta -= 360.0
    if delta < -180.0:
        delta += 360.0
    if abs(delta) < 12.0:
        return mesh
    logger.info("orient yaw photo to +Z front az=%d Δ=%.0f el=%d", az, delta, el)
    return _apply_rot(mesh, _euler(0.0, delta, 0.0))


def _upright_silhouette(mesh: Any, image: Any) -> Any:
    """Roll around the view so the figure stands in the photo's up, not on a diagonal."""
    from texture import _image_arrays, _iou, _occupancy, _preview_mesh, _resize_mask

    _rgb, mask = _image_arrays(image)
    target = _resize_mask(mask, 80)
    target_prof = target.mean(axis=1)
    ty, tx = np.nonzero(target)
    photo_tall = (
        (float(ty.max() - ty.min()) + 1.0) / (float(tx.max() - tx.min()) + 1.0)
        if len(ty) and len(tx)
        else 1.0
    )
    verts = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces, dtype=int)
    v_s, f_s = _preview_mesh(verts, faces, 2500)
    center = v_s.mean(axis=0)
    best = (-1e9, np.eye(3), 0)
    for roll in range(-90, 91, 5):
        rot = _euler(0.0, 0.0, float(roll))
        vv = (v_s - center) @ rot.T
        occ = _occupancy(vv, f_s, az=0, el=12, size=80)
        iou = _iou(occ, target)
        corr = _corr(occ.mean(axis=1), target_prof)
        r, c = np.nonzero(occ)
        tall = (
            (float(r.max() - r.min()) + 1.0) / (float(c.max() - c.min()) + 1.0)
            if len(r) and len(c)
            else 1.0
        )
        s = iou + 1.6 * corr - 0.5 * abs(tall - photo_tall)
        if s > best[0]:
            best = (s, rot, roll)
    logger.info("orient upright silhouette roll=%d score=%.3f", best[2], best[0])
    return _apply_rot(mesh, best[1])


def _flip_if_head_down(mesh: Any) -> Any:
    """Without a photo: prefer the end whose bottom contact is two compact feet, not a head."""
    def contact_width(flip: bool) -> float:
        v = np.asarray(mesh.vertices, dtype=float).copy()
        if flip:
            v[:, 1] *= -1
        ymin = float(v[:, 1].min())
        span = float(np.ptp(v[:, 1])) + 1e-8
        bot = v[v[:, 1] <= ymin + 0.12 * span]
        if len(bot) < 12:
            return 1e9
        return float(np.ptp(bot[:, 0]) * np.ptp(bot[:, 2]))

    upright = contact_width(False)
    flipped = contact_width(True)
    if flipped + 1e-8 < upright * 0.72:
        logger.info("orient flip head-down contact upright=%.3f flipped=%.3f", upright, flipped)
        return _apply_rot(mesh, _euler(180.0, 0.0, 0.0))
    return mesh


def _end_clusters(verts: np.ndarray, *, low: bool) -> int:
    y = verts[:, 1]
    ymin = float(y.min())
    span = float(np.ptp(y)) + 1e-8
    sl = verts[y <= ymin + 0.15 * span] if low else verts[y >= ymin + 0.85 * span]
    if len(sl) < 24:
        return 0
    scale = float(np.linalg.norm(verts.max(axis=0) - verts.min(axis=0)))
    raw = _cluster_points(sl[:, [0, 2]], eps=0.11 * scale)
    return sum(1 for c in raw if c["n"] >= max(30, int(0.04 * len(sl))))


def _feet_end_down(mesh: Any) -> Any:
    """Hat/head is one blob; feet are two. If the ground has one blob and the sky has two, flip."""
    verts = np.asarray(mesh.vertices, dtype=float)
    if len(verts) < 64:
        return mesh
    bot = _end_clusters(verts, low=True)
    top = _end_clusters(verts, low=False)
    if bot <= 1 and top >= 2:
        logger.info("orient feet-end-down (ground blobs=%d sky blobs=%d)", bot, top)
        return _apply_rot(mesh, _euler(180.0, 0.0, 0.0))
    return mesh


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
