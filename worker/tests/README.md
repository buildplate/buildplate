# Upright regression bank

Orientation used to get fixed for one subject and broken for the next. These tests freeze real jobs plus synthetic shapes and assert they stay **photo-up** (figurines) or **sit correctly** (hats / CAD).

```bash
cd worker
~/buildplate/venv/bin/python -m unittest tests.test_upright -v
```

## Add a fixture

From a job in `~/buildplate/out/<name>` that has `model.stl` and (for mesh) `composited.png`:

```bash
~/buildplate/venv/bin/python tests/pack_fixtures.py charmander cowboy-hat-mesh
```

Or drop files by hand under `fixtures/upright/<id>/`:

| file | purpose |
| --- | --- |
| `mesh.npz` | `vertices` float32, `faces` int32 (decimated is fine) |
| `ref.png` | standing reference (omit for CAD) |
| `expect.json` | `kind`: `figurine` \| `hat` \| `cad` |

`kind=figurine` — after `orient_mesh`, silhouette must match the photo better than a 90° knock-over.
`kind=hat` — brim wider than tall, crown up.
`kind=cad` — longest AABB axis is Y, sits on y=0.
