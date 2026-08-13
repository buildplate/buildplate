"""Single-view albedo bake → PBR GLB.

Official Hunyuan-Paint needs a CUDA custom rasterizer and does not run on
Apple Silicon. This stage unwraps with xatlas, projects the reference photo
onto the mesh from the best-matching orthographic view, inpaints the back,
and attaches a glTF PBR material (albedo + constant roughness).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger("buildplate-worker")

_ATLAS = 1024
_AZIMUTHS = tuple(range(0, 360, 20))
_ELEVATIONS = (6, 16)


def bake_reference_pbr(mesh: Any, image: Image.Image, out_dir: Path | None = None) -> Any:
    import trimesh
    from trimesh.visual.material import PBRMaterial
    from trimesh.visual.texture import TextureVisuals

    rgb, mask = _image_arrays(image)
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if len(verts) < 8 or len(faces) < 8:
        raise ValueError("mesh too small to texture")

    az, el, mirror = _best_view(verts, faces, mask)
    logger.info("texture view az=%d el=%d mirror=%s", az, el, mirror)
    if mirror:
        rgb = np.ascontiguousarray(rgb[:, ::-1])
        mask = np.ascontiguousarray(mask[:, ::-1])

    colors = _project_vertex_colors(verts, faces, rgb, mask, az, el)
    colors = _fill_vertex_colors(faces, colors)

    new_verts, new_faces, uvs, vmapping = _unwrap(verts, faces)
    vert_colors = colors[vmapping]
    atlas = _pad_atlas(_rasterize_atlas(uvs, new_faces, vert_colors, _ATLAS))

    if out_dir is not None:
        Image.fromarray(atlas).save(out_dir / "albedo.png")

    mat = PBRMaterial(
        baseColorTexture=Image.fromarray(atlas, mode="RGB"),
        metallicFactor=0.0,
        roughnessFactor=0.55,
        doubleSided=True,
    )
    out = trimesh.Trimesh(vertices=new_verts, faces=new_faces, process=False)
    out.visual = TextureVisuals(uv=uvs, image=Image.fromarray(atlas, mode="RGB"), material=mat)
    return out


def _image_arrays(image: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    im = image.convert("RGBA")
    arr = np.asarray(im)
    rgb = arr[:, :, :3].astype(np.float32) / 255.0
    alpha = arr[:, :, 3]
    if np.mean(alpha) < 250:
        mask = alpha > 40
    else:
        # white-composited RGB — subject is not near-white
        mask = np.any(arr[:, :, :3] < 245, axis=2)
    if int(mask.sum()) < 64:
        mask = np.ones(mask.shape, dtype=bool)
        mask[:2, :] = False
        mask[-2:, :] = False
        mask[:, :2] = False
        mask[:, -2:] = False
    return rgb, mask


def _best_view(verts: np.ndarray, faces: np.ndarray, mask: np.ndarray) -> tuple[int, int, bool]:
    target = _resize_mask(mask, 96)
    target_prof = target.mean(axis=1)
    v_s, f_s = _preview_mesh(verts, faces, 4000)
    best = (-1e9, 0, 16, False)
    for el in _ELEVATIONS:
        for az in _AZIMUTHS:
            occ = _occupancy(v_s, f_s, az, el, 96)
            iou = _iou(occ, target)
            iou_m = _iou(np.fliplr(occ), target)
            corr = _profile_corr(occ, target_prof)
            corr_m = _profile_corr(np.fliplr(occ), target_prof)
            s = iou + 0.5 * corr
            s_m = iou_m + 0.5 * corr_m
            if s > best[0]:
                best = (s, az, el, False)
            if s_m > best[0]:
                best = (s_m, az, el, True)
    return int(best[1]), int(best[2]), bool(best[3])


def _profile_corr(occ: np.ndarray, target_prof: np.ndarray) -> float:
    a = occ.mean(axis=1).astype(np.float64)
    b = np.asarray(target_prof, dtype=np.float64)
    if a.size != b.size:
        return 0.0
    a = a - a.mean()
    b = b - b.mean()
    den = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-8
    return float(np.dot(a, b) / den)


def _preview_mesh(verts: np.ndarray, faces: np.ndarray, target: int) -> tuple[np.ndarray, np.ndarray]:
    if len(faces) <= target:
        return verts, faces
    try:
        import trimesh

        m = trimesh.Trimesh(verts, faces, process=False)
        m = m.simplify_quadric_decimation(int(target))
        return np.asarray(m.vertices), np.asarray(m.faces)
    except Exception:
        rng = np.random.default_rng(0)
        idx = rng.choice(len(faces), size=target, replace=False)
        return verts, faces[idx]


def _resize_mask(mask: np.ndarray, size: int) -> np.ndarray:
    import cv2

    img = (mask.astype(np.uint8) * 255)
    out = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    return out > 127


def _camera_basis(az_deg: float, el_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    az = np.deg2rad(az_deg)
    el = np.deg2rad(el_deg)
    forward = np.array(
        [np.sin(az) * np.cos(el), np.sin(el), np.cos(az) * np.cos(el)],
        dtype=np.float64,
    )
    world_up = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    right = np.cross(world_up, forward)
    n = np.linalg.norm(right)
    if n < 1e-8:
        right = np.array([1.0, 0.0, 0.0], dtype=np.float64)
    else:
        right /= n
    up = np.cross(forward, right)
    up /= np.linalg.norm(up) + 1e-12
    return right, up, forward


def _to_camera(verts: np.ndarray, az: float, el: float) -> np.ndarray:
    right, up, forward = _camera_basis(az, el)
    centered = verts - verts.mean(axis=0)
    return np.stack(
        [centered @ right, centered @ up, centered @ forward],
        axis=1,
    )


def _occupancy(verts: np.ndarray, faces: np.ndarray, az: int, el: int, size: int) -> np.ndarray:
    import cv2

    cam = _to_camera(verts, az, el)
    xy = cam[:, :2]
    lo = xy.min(axis=0)
    span = np.maximum(xy.max(axis=0) - lo, 1e-8)
    scale = (size - 3) / float(span.max())
    pts = (xy - lo) * scale + 1.0
    # Camera +Y is up; image / OpenCV +Y is down. Put the head at the top of the mask.
    pts[:, 1] = (size - 1) - pts[:, 1]
    pts = np.round(pts).astype(np.int32)
    occ = np.zeros((size, size), dtype=np.uint8)
    tris = pts[faces]
    for tri in tris:
        cv2.fillConvexPoly(occ, tri, 1)
    return occ.astype(bool)


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union + 1e-8)


def _vertex_normals(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    nrm = np.zeros_like(verts)
    np.add.at(nrm, faces[:, 0], fn)
    np.add.at(nrm, faces[:, 1], fn)
    np.add.at(nrm, faces[:, 2], fn)
    lens = np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
    return nrm / lens


def _subject_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    pad = 4
    x0 = max(int(xs.min()) - pad, 0)
    x1 = min(int(xs.max()) + pad + 1, mask.shape[1])
    y0 = max(int(ys.min()) - pad, 0)
    y1 = min(int(ys.max()) + pad + 1, mask.shape[0])
    return x0, x1, y0, y1


def _project_vertex_colors(
    verts: np.ndarray,
    faces: np.ndarray,
    rgb: np.ndarray,
    mask: np.ndarray,
    az: int,
    el: int,
) -> np.ndarray:
    """Project photo onto the mesh: world-Y → image-Y so the face stays on the head."""
    h, w = mask.shape
    azr = np.deg2rad(az)
    # Horizontal from yaw around Y; vertical locked to world Y (feet→head).
    x_h = verts[:, 0] * np.cos(azr) + verts[:, 2] * np.sin(azr)
    depth = -verts[:, 0] * np.sin(azr) + verts[:, 2] * np.cos(azr)
    y_w = verts[:, 1]

    _, _, forward = _camera_basis(az, 28)
    vn = _vertex_normals(verts, faces)
    facing = vn @ forward
    front = facing > 0.08

    x0, x1, y0, y1 = _subject_bbox(mask)
    img_w, img_h = float(x1 - x0), float(y1 - y0)
    use = front
    if not np.any(use):
        use = np.ones(len(verts), dtype=bool)
    x_span = max(float(np.ptp(x_h[use])), 1e-8)
    y_lo = float(np.percentile(y_w, 8))
    y_hi = float(np.percentile(y_w, 88))
    y_span = max(y_hi - y_lo, 1e-8)
    scale = img_w / x_span
    cx = (x0 + x1) * 0.5
    x_mid = float(x_h[use].mean())

    px = cx + (x_h - x_mid) * scale
    # Photo top → just below ear tips; photo bottom → feet. Stops the face sliding onto the belly.
    py = y1 - (np.clip(y_w, y_lo, y_hi) - y_lo) / y_span * (y1 - y0)

    ix = np.clip(np.round(px).astype(int), 0, w - 1)
    iy = np.clip(np.round(py).astype(int), 0, h - 1)
    in_frame = (px >= 0) & (px < w) & (py >= 0) & (py < h)

    # Depth test: only the closest vertex at each pixel receives the photo.
    pix = iy * w + ix
    best = np.full(h * w, -np.inf, dtype=np.float64)
    vis_idx = np.where(front & in_frame)[0]
    if len(vis_idx):
        np.maximum.at(best, pix[vis_idx], depth[vis_idx])
    visible = front & in_frame & (depth >= best[pix] - 0.02 * y_span) & mask[iy, ix]

    colors = np.full((len(verts), 3), np.nan, dtype=np.float64)
    colors[visible] = rgb[iy[visible], ix[visible]]
    return colors


def _fill_vertex_colors(faces: np.ndarray, colors: np.ndarray) -> np.ndarray:
    valid = np.isfinite(colors).all(axis=1)
    out = np.where(np.isfinite(colors), colors, 0.0)
    if not valid.any():
        return np.full_like(out, 0.65)

    n = len(out)
    ii = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2], faces[:, 0], faces[:, 1], faces[:, 2]])
    jj = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0], faces[:, 2], faces[:, 0], faces[:, 1]])
    ones = np.ones(len(ii), dtype=np.float64)
    import scipy.sparse as sp

    adj = sp.coo_matrix((ones, (ii, jj)), shape=(n, n)).tocsr()
    adj.setdiag(0)
    adj.eliminate_zeros()

    # Few hops only — long fills smear ear-tip black across the face.
    for _ in range(12):
        if valid.all():
            break
        src = out * valid[:, None]
        deg = np.asarray(adj.dot(valid.astype(np.float64))).ravel()
        acc = adj.dot(src)
        take = (~valid) & (deg > 0)
        if not np.any(take):
            break
        out[take] = acc[take] / deg[take, None]
        valid[take] = True
    if valid.any():
        med = np.median(out[valid], axis=0)
        out[~valid] = med
    return np.clip(out, 0.0, 1.0)


def _unwrap(verts: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    import xatlas

    vmapping, indices, uvs = xatlas.parametrize(verts, faces)
    vmapping = np.asarray(vmapping, dtype=np.int64)
    return verts[vmapping], np.asarray(indices, dtype=np.int64), np.asarray(uvs, dtype=np.float64), vmapping


def _rasterize_atlas(uvs: np.ndarray, faces: np.ndarray, colors: np.ndarray, size: int) -> np.ndarray:
    atlas = np.zeros((size, size, 3), dtype=np.float64)
    weight = np.zeros((size, size), dtype=np.float64)
    uvp = np.clip(uvs, 0.0, 1.0) * (size - 1)
    # glTF UV origin is bottom-left; image origin is top-left
    uvp[:, 1] = (size - 1) - uvp[:, 1]

    for i0, i1, i2 in faces:
        p0, p1, p2 = uvp[i0], uvp[i1], uvp[i2]
        c0, c1, c2 = colors[i0], colors[i1], colors[i2]
        minx = max(int(np.floor(min(p0[0], p1[0], p2[0]))), 0)
        maxx = min(int(np.ceil(max(p0[0], p1[0], p2[0]))), size - 1)
        miny = max(int(np.floor(min(p0[1], p1[1], p2[1]))), 0)
        maxy = min(int(np.ceil(max(p0[1], p1[1], p2[1]))), size - 1)
        if maxx < minx or maxy < miny:
            continue
        v0 = p1 - p0
        v1 = p2 - p0
        den = v0[0] * v1[1] - v1[0] * v0[1]
        if abs(den) < 1e-12:
            continue
        xs = np.arange(minx, maxx + 1) + 0.5
        ys = np.arange(miny, maxy + 1) + 0.5
        gx, gy = np.meshgrid(xs, ys)
        v2x = gx - p0[0]
        v2y = gy - p0[1]
        a = (v2x * v1[1] - v1[0] * v2y) / den
        b = (v0[0] * v2y - v2x * v0[1]) / den
        c = 1.0 - a - b
        hit = (a >= -1e-4) & (b >= -1e-4) & (c >= -1e-4)
        if not np.any(hit):
            continue
        col = c[..., None] * c0 + a[..., None] * c1 + b[..., None] * c2
        sl = atlas[miny : maxy + 1, minx : maxx + 1]
        wt = weight[miny : maxy + 1, minx : maxx + 1]
        sl[hit] += col[hit]
        wt[hit] += 1.0

    nz = weight > 0
    atlas[nz] /= weight[nz, None]
    return np.clip(atlas, 0.0, 1.0)


def _pad_atlas(atlas: np.ndarray) -> np.ndarray:
    import cv2

    u8 = (atlas * 255.0).astype(np.uint8)
    filled = (np.any(atlas > 0, axis=2)).astype(np.uint8)
    if filled.sum() == 0:
        return u8
    kernel = np.ones((3, 3), dtype=np.uint8)
    near = cv2.dilate(filled, kernel, iterations=6)
    hole = ((near > 0) & (filled == 0)).astype(np.uint8) * 255
    if hole.any():
        u8 = cv2.inpaint(u8, hole, 3, cv2.INPAINT_TELEA)
    return u8
