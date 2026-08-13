"""Shared image preprocess: rembg cutout + white composite."""

from __future__ import annotations

import logging

from PIL import Image

logger = logging.getLogger("buildplate-worker")

_session = None
_session_tried = False


def rembg_session():
    global _session, _session_tried
    if _session_tried:
        return _session
    _session_tried = True
    try:
        from rembg import new_session

        _session = new_session("u2net")
    except Exception as err:
        logger.warning("rembg unavailable (%s) — skipping bg removal", err)
        _session = None
    return _session


def remove_bg(image: Image.Image) -> Image.Image:
    session = rembg_session()
    if session is None:
        return image.convert("RGBA")
    from rembg import remove
    import numpy as np
    from scipy import ndimage

    cut = remove(image.convert("RGBA"), session=session)
    arr = np.array(cut)
    alpha = arr[:, :, 3]
    alpha = (alpha >= 40).astype(np.uint8) * 255
    labeled, n = ndimage.label(alpha > 0)
    if n > 1:
        sizes = ndimage.sum(alpha > 0, labeled, index=range(1, n + 1))
        keep = int(np.argmax(sizes)) + 1
        alpha = np.where(labeled == keep, alpha, 0).astype(np.uint8)
        logger.info("rembg blobs=%d kept=%d", n, int(np.max(sizes)))
    mask = alpha > 0
    mask = ndimage.binary_erosion(mask, iterations=2)
    arr[:, :, 3] = np.where(mask, 255, 0).astype(np.uint8)
    arr[arr[:, :, 3] == 0, :3] = 255

    ys, xs = np.where(arr[:, :, 3] > 0)
    if len(xs) == 0:
        return Image.fromarray(arr, mode="RGBA")
    pad = 24
    x0, x1 = max(0, xs.min() - pad), min(arr.shape[1], xs.max() + pad + 1)
    y0, y1 = max(0, ys.min() - pad), min(arr.shape[0], ys.max() + pad + 1)
    cropped = arr[y0:y1, x0:x1]
    side = int(max(cropped.shape[0], cropped.shape[1]) * 1.4)
    side = max(side, 256)
    canvas = np.zeros((side, side, 4), dtype=np.uint8)
    canvas[:, :, :3] = 255
    oy = (side - cropped.shape[0]) // 2
    ox = (side - cropped.shape[1]) // 2
    canvas[oy : oy + cropped.shape[0], ox : ox + cropped.shape[1]] = cropped
    return Image.fromarray(canvas, mode="RGBA")


def composite_white(cutout: Image.Image) -> Image.Image:
    cutout = cutout.convert("RGBA")
    bg = Image.new("RGBA", cutout.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, cutout).convert("RGB")
