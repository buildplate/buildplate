# Buildplate — for the agent

Local MCP. You think; the worker only compiles / reconstructs.

Tools: `health`, `save_reference`, `generate`, `refine`, `export_stl`, `preview`.

## CAD (mechanical)

Brackets, boxes, enclosures, plates, mounts, mm / holes / flats.

You author geometry. Prefer `trimesh_code` (always on). Must assign `result=`.

```
generate({ backend: "cad", prompt, trimesh_code, format: "stl" })
```

```python
import trimesh
box = trimesh.creation.box(extents=[40, 30, 12])
hole = trimesh.creation.cylinder(radius=3, height=20)
hole.apply_translation([10, 0, 0])
result = box.difference(hole)
```

OpenSCAD / CadQuery are optional if those CLIs are installed.

## Mesh (organic)

Characters, toys, figurines. **Requires a photo.**

```
save_reference → generate({ backend: "mesh", image_path, prompt, quality: "quality" })
```

`quality=fast` is TripoSR (softer, quicker). Text-only mesh: `allow_text_only=true` (SDXL-Turbo paints a reference first).

Do **not** use mesh for hard-edged products (printers, enclosures, etc.).

## After

Preview. Incomplete `generate` calls return a retry recipe — follow it.

Color / material follow-ups on a mesh: `refine({ job_id, prompt, color })` — keeps the mesh, retints albedo.

```
refine({ job_id, prompt: "I want it to be green instead of yellow", color: "green" })
```

Shape follow-ups (longer ears, extra parts): new `generate` with a new photo, or edit CAD source and re-generate.
