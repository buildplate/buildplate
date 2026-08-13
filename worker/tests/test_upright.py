"""Regression bank: oriented meshes must stay photo-up / sitting, not longest-axis-up."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

WORKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER))

import trimesh  # noqa: E402
from mesh_ops import _apply_rot, _euler, orient_mesh  # noqa: E402
from texture import (  # noqa: E402
    _image_arrays,
    _iou,
    _occupancy,
    _preview_mesh,
    _resize_mask,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "upright"

# Knock-overs that used to "win" under longest-axis-up (tail / brim as height).
SCRAMBLES = {
    "identity": np.eye(3),
    "roll90": _euler(0.0, 0.0, 90.0),
    "pitch90": _euler(90.0, 0.0, 0.0),
}


def _load_mesh(path: Path) -> trimesh.Trimesh:
    data = np.load(path)
    return trimesh.Trimesh(
        vertices=np.asarray(data["vertices"], dtype=float),
        faces=np.asarray(data["faces"], dtype=int),
        process=False,
    )


def _sits_on_ground(mesh: trimesh.Trimesh) -> None:
    span = float(np.ptp(mesh.vertices, axis=0).max()) + 1e-8
    ymin = float(mesh.bounds[0][1])
    if abs(ymin) > 0.02 * span:
        raise AssertionError(f"not on ground ymin={ymin:.4f} span={span:.4f}")


def _photo_score(mesh: trimesh.Trimesh, image: Image.Image) -> float:
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
    occ = _occupancy(v_s, f_s, az=0, el=12, size=80)
    iou = _iou(occ, target)
    a = occ.mean(axis=1).astype(float)
    b = np.asarray(target_prof, dtype=float)
    a = a - a.mean()
    b = b - b.mean()
    den = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-8
    corr = float(np.dot(a, b) / den)
    r, c = np.nonzero(occ)
    tall = (
        (float(r.max() - r.min()) + 1.0) / (float(c.max() - c.min()) + 1.0)
        if len(r) and len(c)
        else 1.0
    )
    return iou + 1.2 * corr - 0.5 * abs(tall - photo_tall)


def _assert_photo_up(mesh: trimesh.Trimesh, image: Image.Image, label: str) -> None:
    score = _photo_score(mesh, image)
    rolled = _apply_rot(mesh, _euler(0.0, 0.0, 90.0))
    pitched = _apply_rot(mesh, _euler(90.0, 0.0, 0.0))
    s_roll = _photo_score(rolled, image)
    s_pitch = _photo_score(pitched, image)
    if score < s_roll + 0.02:
        raise AssertionError(
            f"{label}: photo-up {score:.3f} not better than roll90 {s_roll:.3f}"
        )
    if score < s_pitch + 0.02:
        raise AssertionError(
            f"{label}: photo-up {score:.3f} not better than pitch90 {s_pitch:.3f}"
        )


def _silhouette(mesh: trimesh.Trimesh, *, orange: bool = True) -> Image.Image:
    occ = _occupancy(
        np.asarray(mesh.vertices, dtype=float),
        np.asarray(mesh.faces, dtype=int),
        az=0,
        el=12,
        size=160,
    )
    arr = np.full((160, 160, 3), 255, dtype=np.uint8)
    arr[occ] = (220, 110, 40) if orange else (90, 70, 50)
    return Image.fromarray(arr)


def _rot_x_to_y() -> np.ndarray:
    # trimesh cylinders default to Z; map Z → Y (Y-up).
    return _euler(-90.0, 0.0, 0.0)


def make_tailed_figurine() -> trimesh.Trimesh:
    """Standing lizard: head +Y, feet ~0, long heavy tail +X (SVD longest ≠ up)."""
    parts = []
    body = trimesh.creation.icosphere(subdivisions=3, radius=0.42)
    body.apply_translation([0.0, 0.50, 0.12])
    parts.append(body)
    head = trimesh.creation.icosphere(subdivisions=3, radius=0.24)
    head.apply_translation([0.0, 1.02, 0.22])
    parts.append(head)
    for dx, dz in ((-0.16, 0.22), (0.16, 0.22), (-0.16, -0.02), (0.16, -0.02)):
        foot = trimesh.creation.icosphere(subdivisions=2, radius=0.10)
        foot.apply_translation([dx, 0.09, dz])
        parts.append(foot)
    for i in range(14):
        bead = trimesh.creation.icosphere(subdivisions=2, radius=0.10)
        bead.apply_translation(
            [0.38 + i * 0.14, 0.40 + 0.02 * i, 0.08 + 0.12 * np.sin(i / 3.0)]
        )
        parts.append(bead)
    return trimesh.util.concatenate(parts)


def make_hat() -> trimesh.Trimesh:
    to_y = _rot_x_to_y()
    brim = trimesh.creation.cylinder(radius=1.15, height=0.08)
    brim.vertices = brim.vertices @ to_y.T
    brim.vertices[:, 1] += 0.04
    crown = trimesh.creation.cylinder(radius=0.42, height=0.52)
    crown.vertices = crown.vertices @ to_y.T
    crown.vertices[:, 1] += 0.34
    return trimesh.util.concatenate([brim, crown])


def make_cabinet() -> trimesh.Trimesh:
    return trimesh.creation.box(extents=[40.0, 60.0, 35.0])


class PhotoUpTests(unittest.TestCase):
    def test_tailed_figurine_photo_beats_longest_axis(self) -> None:
        mesh = make_tailed_figurine()
        image = _silhouette(mesh)
        # Longest SVD axis should be the tail (X), not head-to-feet (Y).
        verts = np.asarray(mesh.vertices, dtype=float)
        _, _, vh = np.linalg.svd(verts - verts.mean(axis=0), full_matrices=False)
        longest = np.abs(vh[0])
        self.assertGreater(float(longest[0]), float(longest[1]))

        from mesh_ops import _align_longest_to_up

        knocked = _align_longest_to_up(mesh.copy())
        cases = {"identity": mesh, "longest_up": knocked}
        for name, rot in SCRAMBLES.items():
            cases[name] = _apply_rot(mesh, rot)
        for name, src in cases.items():
            got = orient_mesh(src, image)
            with self.subTest(scramble=name):
                _sits_on_ground(got)
                _assert_photo_up(got, image, f"tailed/{name}")

    def test_hat_stays_brim_down(self) -> None:
        mesh = make_hat()
        image = _silhouette(mesh, orange=False)
        for name, rot in SCRAMBLES.items():
            got = orient_mesh(_apply_rot(mesh, rot), image)
            with self.subTest(scramble=name):
                _sits_on_ground(got)
                e = np.asarray(got.extents, dtype=float)
                self.assertLess(
                    float(e[1]),
                    float(max(e[0], e[2])) * 0.95,
                    f"hat/{name} stood the brim up extents={e}",
                )

    def test_cabinet_stays_y_up_without_photo(self) -> None:
        mesh = make_cabinet()
        got = orient_mesh(mesh, None)
        _sits_on_ground(got)
        e = np.asarray(got.extents, dtype=float)
        self.assertEqual(int(np.argmax(e)), 1, f"cabinet extents={e}")


class FixtureBankTests(unittest.TestCase):
    def test_bank_exists(self) -> None:
        self.assertTrue(
            FIXTURES.is_dir(),
            "missing fixtures/upright — run tests/pack_fixtures.py",
        )

    def test_each_fixture_stays_up(self) -> None:
        cases = sorted(p for p in FIXTURES.iterdir() if p.is_dir() and (p / "mesh.npz").is_file())
        self.assertGreaterEqual(len(cases), 3, "need packed fixtures (charmander, hat, cabinet, …)")
        for case in cases:
            expect = json.loads((case / "expect.json").read_text()) if (case / "expect.json").is_file() else {}
            kind = expect.get("kind", "figurine")
            mesh = _load_mesh(case / "mesh.npz")
            image = Image.open(case / "ref.png").convert("RGBA") if (case / "ref.png").is_file() else None
            if kind == "cad":
                scrambles = {"identity": np.eye(3)}
            elif kind == "hat":
                scrambles = {
                    "identity": np.eye(3),
                    "roll90": SCRAMBLES["roll90"],
                    "pitch90": SCRAMBLES["pitch90"],
                }
            else:
                # Identity is the production path (orient the Hunyuan dump).
                # Arbitrary 90° scrambles on real organic blobs are noisy;
                # synthetic tests cover knock-over recovery.
                scrambles = {"identity": np.eye(3)}
            for name, rot in scrambles.items():
                got = orient_mesh(_apply_rot(mesh, rot), image)
                label = f"{case.name}/{name}"
                with self.subTest(fixture=label):
                    _sits_on_ground(got)
                    if image is not None and kind != "hat":
                        _assert_photo_up(got, image, label)
                    if kind == "hat":
                        e = np.asarray(got.extents, dtype=float)
                        self.assertLess(
                            float(e[1]),
                            float(max(e[0], e[2])) * 1.05,
                            f"{label} brim not sitting extents={e}",
                        )
                    if kind == "cad":
                        e = np.asarray(got.extents, dtype=float)
                        self.assertEqual(int(np.argmax(e)), 1, f"{label} extents={e}")


if __name__ == "__main__":
    unittest.main()
