# Buildplate

Local 3D for agents. Your machine compiles / reconstructs. The agent thinks (CAD vs mesh, photos, solids). Talks over **MCP** — Cursor, Claude, Codex, or anything that speaks stdio MCP.

<p align="center">
  <img src="docs/charmander.png" alt="Charmander generated in Buildplate, standing on the Bambu build plate" width="560">
</p>
<p align="center"><sub>Photo → mesh → Open in Bambu</sub></p>

| | |
|--|--|
| **CAD** | Agent-authored trimesh / OpenSCAD / CadQuery → STL |
| **Mesh** | Photo → Hunyuan (quality) or TripoSR (fast) → remesh → PBR albedo → GLB |
| **Preview** | [http://buildplate.localhost](http://buildplate.localhost) — orbit, Export STL, Open in Bambu (or other installed slicers) |

Optional: `brew install --cask openscad`, or `~/buildplate/venv/bin/pip install cadquery`.

## Hardware

| | Minimum | Recommended |
|--|--|--|
| **Apple Silicon** | M1/M2, 16 GB unified — `quality=fast` (TripoSR) | M1 Pro/Max or M2+, **32 GB** — `quality=quality` (Hunyuan) |
| **NVIDIA** | 8 GB VRAM — `quality=fast` | **12 GB+** VRAM — `quality=quality` |
| **OS** | macOS 14+, Ubuntu 22.04+, Windows 11 | macOS on Apple Silicon |
| **Node** | 20+ | 20 LTS |
| **Python** | 3.10–3.13 | **3.12** (3.14 has no torch wheels yet) |

CAD (trimesh CSG) is CPU-only and light. Mesh reconstruction needs the GPU/unified memory above. CPU-only mesh is possible and very slow — not recommended.

NVIDIA **CUDA 12.9+** (Windows/Linux): `npx buildplate setup` does not need you to patch Torch. It passes `USE_SYSTEM_NVTX` for torchmcubes, and falls back to CPU marching cubes if the CUDA extension still fails. Apple Silicon uses Metal and never takes that path.

## Install

No git clone. The npm package is the product: `npx buildplate` is the CLI and the MCP server. Python venv, model vendors, and job files live in **`~/buildplate`**.

Need **Node 20+** and **Python 3.10–3.13** (3.12 best).

### Option 1 — Agent

Paste the block into Cursor, Claude Code, Claude Desktop, Codex, or any MCP agent. It installs Buildplate **and** wires MCP into whichever client you are using. Expand, then use GitHub’s copy button on the fence.

<details>
<summary><strong>Show copy-paste</strong></summary>

```
Set up Buildplate (local 3D MCP) on this machine, then add it to THIS product as an MCP server.

Do NOT git clone. Install the npm package via npx.

## Install (run; skip a step if already done)
Need Node 20+ and Python 3.10–3.13 (3.12 best).
  npx -y buildplate setup    # once — venv, PyTorch, TripoSR, Hunyuan into ~/buildplate
  npx -y buildplate start    # worker :8081 + preview http://buildplate.localhost

Env: BUILDPLATE_PREVIEW_URL=http://buildplate.localhost
The MCP auto-starts the worker if it is not up; preview still wants `npx buildplate start` (or `npx buildplate preview`).

## Add MCP to this client (detect which you are; do not ask the user to click around)
Always: command=npx, args=["-y", "buildplate"]. No clone path. No ${workspaceFolder}.

Cursor:
  Merge ~/.cursor/mcp.json (user-level, all workspaces) — create the file if missing:
  {
    "mcpServers": {
      "buildplate": {
        "command": "npx",
        "args": ["-y", "buildplate"],
        "env": { "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost" }
      }
    }
  }
  Project .cursor/mcp.json with the same snippet also works. Reload MCP.

Claude Code:
  claude mcp add --transport stdio --env BUILDPLATE_PREVIEW_URL=http://buildplate.localhost buildplate -- npx -y buildplate

Claude Desktop:
  Merge into mcpServers:
  macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
  Windows: %APPDATA%\Claude\claude_desktop_config.json
  {
    "mcpServers": {
      "buildplate": {
        "command": "npx",
        "args": ["-y", "buildplate"],
        "env": { "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost" }
      }
    }
  }
  Fully quit and relaunch Claude Desktop.

Codex (CLI / IDE):
  codex mcp add buildplate --env BUILDPLATE_PREVIEW_URL=http://buildplate.localhost -- npx -y buildplate
  Or merge into ~/.codex/config.toml:
  [mcp_servers.buildplate]
  command = "npx"
  args = ["-y", "buildplate"]
  env = { BUILDPLATE_PREVIEW_URL = "http://buildplate.localhost" }

Any other MCP client: stdio, command=npx, args=["-y", "buildplate"], same env.

Then call health. Tools: health, save_reference, generate, refine, export_stl, preview.

## How to use the tools
CAD — brackets, boxes, enclosures, mm / holes / flats:
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
  quality=fast is TripoSR (softer, quicker). Text-only paints an SDXL-Turbo reference first. Do not use mesh for hard-edged products.

If generate is incomplete it returns a retry recipe — follow it. Then preview.

Follow-ups: color/material → refine({ job_id, prompt, color }) keeps the mesh.
  e.g. refine({ job_id, prompt: "make it green instead of yellow", color: "green" })
Shape changes (longer ears, extra parts) → new generate, or edit CAD source and re-generate.
```

In-repo, agents can also read [`AGENTS.md`](./AGENTS.md).

</details>

---

### Option 2 — Install yourself

<details>
<summary><strong>Show install steps</strong></summary>

```bash
npx -y buildplate setup
npx -y buildplate start
```

`setup` is once (venv + PyTorch + TripoSR + Hunyuan into `~/buildplate`). `start` runs the worker on `:8081` and preview at **http://buildplate.localhost**.

First `start` may ask for your Mac password so preview can bind port 80 on localhost only (not the network). Allow it once.

Then add MCP — same snippet for every client, no clone path:

```json
{
  "mcpServers": {
    "buildplate": {
      "command": "npx",
      "args": ["-y", "buildplate"],
      "env": { "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost" }
    }
  }
}
```

#### Cursor

[![Add to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](cursor://anysphere.cursor-deeplink/mcp/install?name=buildplate&config=eyJjb21tYW5kIjoibnB4IiwiYXJncyI6WyIteSIsImJ1aWxkcGxhdGUiXSwiZW52Ijp7IkJVSUxEUExBVEVfUFJFVklFV19VUkwiOiJodHRwOi8vYnVpbGRwbGF0ZS5sb2NhbGhvc3QifX0=)

Or merge that JSON into `~/.cursor/mcp.json` (all workspaces) or project `.cursor/mcp.json`.

#### Claude Code

```bash
claude mcp add --transport stdio --env BUILDPLATE_PREVIEW_URL=http://buildplate.localhost buildplate -- npx -y buildplate
```

#### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Merge, then fully quit and relaunch.

#### Codex

```bash
codex mcp add buildplate --env BUILDPLATE_PREVIEW_URL=http://buildplate.localhost -- npx -y buildplate
```

Or `~/.codex/config.toml`:

```toml
[mcp_servers.buildplate]
command = "npx"
args = ["-y", "buildplate"]
env = { BUILDPLATE_PREVIEW_URL = "http://buildplate.localhost" }
```

</details>

---

## Usage

Once the worker is up and MCP is connected, talk to your agent in plain language. **Mesh** is for characters, toys, and anything that should look like a photo. **CAD** is for hard-edged parts with real millimeters — boxes, cabinets, mounts, holes.

Attach a photo when you want a look-alike mesh. Skip the photo for CAD and spell out sizes.

### Mesh

> Make me a mesh Charmander figurine from this photo, full body, standing.

> Generate a mesh Grey Fox mask from Metal Gear Solid. Use a reference image.

> Turn this photo of my cowboy hat into a 3D-printable mesh.

> Make a toy-style Pikachu, full body, standing — mesh, quality.

### CAD

> Generate a CAD cabinet, 400 mm wide, 600 mm tall, 350 mm deep, two doors, one 20 mm shelf.

> CAD a phone stand: 70 mm wide, 15° tilt, 4 mm walls.

> Make a 40 × 30 × 12 mm mounting plate with M3 holes at the corners, CAD.

> CAD a simple enclosure, 80 × 50 × 25 mm, 2 mm walls, snap-on lid.

Preview opens at [http://buildplate.localhost](http://buildplate.localhost). From there: **Export STL** or **Open in Bambu**.

## Developing

Working on Buildplate itself:

```bash
git clone https://github.com/buildplate/buildplate.git
cd buildplate
npm install
npm run setup
npm start
```

`npm start` is `npx buildplate start` against this checkout. MCP for local hacks: `node cli.mjs` (stdio) or point a client at that CLI.

## License

Code: MIT. Model weights (TripoSR, SDXL-Turbo, Hunyuan3D-2mini) have their own licenses and download on first use.
