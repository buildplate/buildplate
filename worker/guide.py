"""
Agent guidance for Buildplate — mesh vs CAD routing.

The MCP `generate` tool description mirrors this. The worker also returns it from
/health and /v1/guide so agents get the same rules when a call is incomplete.
"""

from __future__ import annotations

from typing import Any

TRIMESH_HINT = (
    "trimesh_code must assign `result` to a trimesh.Trimesh or Scene. "
    "Available: trimesh, np/numpy, math. "
    "Use trimesh.creation.box/cylinder/… and .difference/.union/.intersection."
)

PLAYBOOK = """Buildplate — YOU (the agent) do the thinking; the worker only compiles/reconstructs.

## Choose backend (do this yourself — no separate tool)

| backend | Use when | You must supply |
|---------|----------|-----------------|
| cad | Mechanical: brackets, boxes, enclosures, plates, mounts, adapters, gears, mm/holes/flats | openscad OR cadquery OR trimesh_code (you author it) |
| mesh | Organic: character, toy, figurine, creature — or anything that should look like a photo | image_path via save_reference (user attach or web photo) |

Neural mesh (TripoSR) is NOT CAD. Hard-edged products from photos become soft blobs → use cad and approximate with solids.

## CAD call shape
generate({
  "backend": "cad",
  "prompt": "L-bracket 40x30mm two M4 holes",
  "trimesh_code": "import trimesh\\n...\\nresult = ...",
  "format": "stl"
})
Prefer trimesh_code (always available). Use openscad if OpenSCAD CLI is installed; cadquery if pip-installed.

trimesh sketch:
  import trimesh
  box = trimesh.creation.box(extents=[40, 30, 12])
  hole = trimesh.creation.cylinder(radius=3, height=20)
  hole.apply_translation([10, 0, 0])
  result = box.difference(hole)

## Mesh call shape
1) save_reference(source_path=...) → path
2) generate({ "backend": "mesh", "prompt": "...", "image_path": "<path>", "quality": "quality" })
quality=quality → Hunyuan3D-2mini + remesh + PBR albedo. quality=fast → TripoSR + remesh + albedo.
Text-only mesh: allow_text_only=true (SDXL-Turbo reference, then image→mesh).

## After
Preview; iterate CAD by editing source. Color follow-ups on a mesh: refine({job_id, prompt}) — keeps geometry.
"""

CAD_HINTS = (
    "bracket",
    "enclosure",
    "case",
    "plate",
    "mount",
    "adapter",
    "hinge",
    "gear",
    "box",
    "mm",
    "hole",
    "screw",
    "m3",
    "m4",
    "flange",
    "spacer",
    "washer",
    "rail",
    "slot",
    "parametric",
    "openscad",
    "cad",
    "lid",
    "tray",
    "stand",
)

MESH_HINTS = (
    "character",
    "figurine",
    "toy",
    "creature",
    "animal",
    "person",
    "organic",
    "statue",
    "sculpture",
    "pokemon",
    "pikachu",
    "bust",
    "mascot",
)


def recommend(
    *,
    intent: str,
    has_image: bool = False,
    has_cad_source: bool = False,
    wants_precise_mm: bool = False,
) -> dict[str, Any]:
    text = (intent or "").lower()
    cad_score = sum(1 for h in CAD_HINTS if h in text) + (3 if wants_precise_mm else 0)
    mesh_score = sum(1 for h in MESH_HINTS if h in text) + (1 if has_image else 0)

    if has_cad_source:
        backend = "cad"
        reason = "CAD source already provided."
    elif mesh_score > cad_score:
        backend = "mesh"
        reason = "Looks organic/character-like — use image→TripoSR mesh."
    elif cad_score > 0:
        backend = "cad"
        reason = "Mechanical / dimensional cues — author OpenSCAD or trimesh CSG."
    elif has_image:
        backend = "mesh"
        reason = "Reference image present without strong CAD cues — mesh reconstruction."
    else:
        backend = "cad"
        reason = "Defaulting to CAD for hard-edged printable geometry; agent authors the solids."

    if backend == "cad":
        next_steps = [
            "Author openscad OR trimesh_code (prefer trimesh_code if OpenSCAD is not installed).",
            'Re-call generate({ backend: "cad", prompt, trimesh_code|openscad|cadquery, format: "stl" }).',
            "Preview; edit your source and re-run if needed.",
        ]
        example = {
            "backend": "cad",
            "prompt": intent or "part",
            "trimesh_code": (
                "import trimesh\n"
                "box = trimesh.creation.box(extents=[40, 30, 12])\n"
                "hole = trimesh.creation.cylinder(radius=3, height=20)\n"
                "hole.apply_translation([10, 0, 0])\n"
                "result = box.difference(hole)\n"
            ),
            "format": "stl",
        }
    else:
        next_steps = [
            (
                "save_reference if needed, then generate({ backend: \"mesh\", image_path, prompt })."
                if has_image
                else "Fetch or ask for a clean reference photo → save_reference → generate(backend=\"mesh\", image_path)."
            ),
            "Do not expect CAD-clean edges from mesh mode.",
        ]
        example = {
            "backend": "mesh",
            "prompt": intent or "subject",
            "image_path": "/absolute/path/from/save_reference.png",
        }

    return {
        "backend": backend,
        "reason": reason,
        "next": next_steps,
        "example_generate_args": example,
        "trimesh_hint": TRIMESH_HINT,
        "playbook": PLAYBOOK,
    }
