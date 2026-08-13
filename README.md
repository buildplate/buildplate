# Buildplate

Local 3D for agents. Your Mac or PC does the work. Cursor talks to it over **MCP**.

You (the agent) pick **CAD vs mesh**, write solids or fetch a photo. Buildplate only compiles / reconstructs.

## 1. Install

Needs **Node 20+** and **Python 3.10–3.13** (3.12 is best). Apple Silicon 16 GB+ or NVIDIA 8 GB+ VRAM.

```bash
git clone https://github.com/jordan-homan/buildplate.git
cd buildplate
npm install
npm run setup    # once — venv, PyTorch, TripoSR, Hunyuan
npm start        # worker :8081 + preview :3920
```

Open this folder in Cursor, then add the MCP:

[![Add to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](cursor://anysphere.cursor-deeplink/mcp/install?name=buildplate&config=eyJjb21tYW5kIjoibm9kZSIsImFyZ3MiOlsiJHt3b3Jrc3BhY2VGb2xkZXJ9L21jcC9zZXJ2ZXIubWpzIl0sImVudiI6eyJCVUlMRFBMQVRFX1BSRVZJRVdfVVJMIjoiaHR0cDovLzEyNy4wLjAuMTozOTIwIn19)

Or paste into `.cursor/mcp.json` (GitHub’s copy button on the block works too):

```json
{
  "mcpServers": {
    "buildplate": {
      "command": "node",
      "args": ["${workspaceFolder}/mcp/server.mjs"],
      "env": { "BUILDPLATE_PREVIEW_URL": "http://127.0.0.1:3920" }
    }
  }
}
```

If Buildplate is a **subfolder** of your workspace, use `${workspaceFolder}/buildplate/mcp/server.mjs`. The MCP starts the worker if it isn’t already up.

## 2. Copy to your agent

Paste this into the chat (or keep [`AGENTS.md`](./AGENTS.md) in the repo — Cursor will read it):

```
You have Buildplate MCP (local 3D). Tools: health, save_reference, generate, export_stl, preview.

CAD — brackets, boxes, enclosures, anything with mm / holes / flats:
  You author geometry. Prefer trimesh_code (always on). Must set result=.
  generate({ backend: "cad", prompt, trimesh_code, format: "stl" })
  Example:
    import trimesh
    box = trimesh.creation.box(extents=[40, 30, 12])
    hole = trimesh.creation.cylinder(radius=3, height=20)
    hole.apply_translation([10, 0, 0])
    result = box.difference(hole)

Mesh — characters, toys, organic / look-like-a-photo:
  save_reference (user photo or a clean web image)
  generate({ backend: "mesh", image_path, prompt, quality: "quality" })
  quality=fast is TripoSR (softer, quicker). Do not use mesh for hard-edged products.

If generate is incomplete it returns a retry recipe — follow it. Then preview.
```

## What you get

| | |
|--|--|
| **CAD** | Agent-authored trimesh / OpenSCAD / CadQuery → STL |
| **Mesh** | Photo → Hunyuan (quality) or TripoSR (fast) → remesh → PBR albedo → GLB |
| **Preview** | http://127.0.0.1:3920 — orbit + Export STL |

Optional: `brew install --cask openscad`, or `worker/.venv/bin/pip install cadquery`.

## License

Code: MIT. Model weights (TripoSR, SD-Turbo, Hunyuan3D-2mini) have their own licenses and download on first use.
