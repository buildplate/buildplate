# Buildplate

Local 3D for agents. Your machine compiles / reconstructs. The agent thinks (CAD vs mesh, photos, solids). Talks over **MCP** — Cursor, Claude, Codex, or anything that speaks stdio MCP.

| | |
|--|--|
| **CAD** | Agent-authored trimesh / OpenSCAD / CadQuery → STL |
| **Mesh** | Photo → Hunyuan (quality) or TripoSR (fast) → remesh → PBR albedo → GLB |
| **Preview** | [http://buildplate.localhost](http://buildplate.localhost) — orbit + Export STL |

Optional: `brew install --cask openscad`, or `worker/.venv/bin/pip install cadquery`.

## Hardware

| | Minimum | Recommended |
|--|--|--|
| **Apple Silicon** | M1/M2, 16 GB unified — `quality=fast` (TripoSR) | M1 Pro/Max or M2+, **32 GB** — `quality=quality` (Hunyuan) |
| **NVIDIA** | 8 GB VRAM — `quality=fast` | **12 GB+** VRAM — `quality=quality` |
| **OS** | macOS 14+, Ubuntu 22.04+, Windows 11 (WSL2) | macOS on Apple Silicon |
| **Node** | 20+ | 20 LTS |
| **Python** | 3.10–3.13 | **3.12** (3.14 has no torch wheels yet) |

CAD (trimesh CSG) is CPU-only and light. Mesh reconstruction needs the GPU/unified memory above. CPU-only mesh is possible and very slow — not recommended.

## Install

### Option 1 — Agent

Paste the block into Cursor, Claude Code, Claude Desktop, Codex, or any MCP agent. It installs Buildplate **and** wires MCP into whichever client you are using. Expand, then use GitHub’s copy button on the fence.

<details>
<summary><strong>Show copy-paste</strong></summary>

```
Set up Buildplate (local 3D MCP) on this machine, then add it to THIS product as an MCP server.

## Install (run; skip a step if already done)
Need Node 20+ and Python 3.10–3.13 (3.12 best).
  git clone https://github.com/buildplate/buildplate.git
  cd buildplate
  npm install
  npm run setup    # once — venv, PyTorch, TripoSR, Hunyuan
  npm start        # worker :8081 + preview http://buildplate.localhost

REPO is the absolute path to the clone (pwd after cd). MCP entrypoint: $REPO/mcp/server.mjs
Env: BUILDPLATE_PREVIEW_URL=http://buildplate.localhost
The MCP auto-starts the worker if it is not up; preview still wants `npm start` or `npm run preview`.

## Add MCP to this client (detect which you are; do not ask the user to click around)

Cursor:
  Write/merge .cursor/mcp.json in the workspace:
  {
    "mcpServers": {
      "buildplate": {
        "command": "node",
        "args": ["${workspaceFolder}/mcp/server.mjs"],
        "env": { "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost" }
      }
    }
  }
  If Buildplate is a subfolder of the workspace, args is
  ["${workspaceFolder}/buildplate/mcp/server.mjs"]. Reload MCP.

Claude Code:
  claude mcp add --transport stdio --env BUILDPLATE_PREVIEW_URL=http://buildplate.localhost buildplate -- node $REPO/mcp/server.mjs

Claude Desktop:
  Merge into mcpServers (absolute path, not ${workspaceFolder}):
  macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
  Windows: %APPDATA%\Claude\claude_desktop_config.json
  {
    "mcpServers": {
      "buildplate": {
        "command": "node",
        "args": ["$REPO/mcp/server.mjs"],
        "env": { "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost" }
      }
    }
  }
  Fully quit and relaunch Claude Desktop.

Codex (CLI / IDE):
  codex mcp add buildplate --env BUILDPLATE_PREVIEW_URL=http://buildplate.localhost -- node $REPO/mcp/server.mjs
  Or merge into ~/.codex/config.toml:
  [mcp_servers.buildplate]
  command = "node"
  args = ["$REPO/mcp/server.mjs"]
  env = { BUILDPLATE_PREVIEW_URL = "http://buildplate.localhost" }

Any other MCP client: stdio, command=node, args=[$REPO/mcp/server.mjs], same env.

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
  quality=fast is TripoSR (softer, quicker). Do not use mesh for hard-edged products.

If generate is incomplete it returns a retry recipe — follow it. Then preview.

Follow-ups: color/material → refine({ job_id, prompt, color }) keeps the mesh.
  e.g. refine({ job_id, prompt: "make it green instead of yellow", color: "green" })
Shape changes (longer ears, extra parts) → new generate, or edit CAD source and re-generate.
```

In-repo, agents can also read [`AGENTS.md`](./AGENTS.md).

</details>

### Option 2 — Install yourself

```bash
git clone https://github.com/buildplate/buildplate.git
cd buildplate
npm install
npm run setup
npm start
```

First `npm start` may ask for your Mac password so preview can use **http://buildplate.localhost** (port 80 on localhost only, not the network). Allow it once.

Then add MCP for your client. Replace `/ABS/PATH/buildplate` with the clone path.

#### Cursor

[![Add to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](cursor://anysphere.cursor-deeplink/mcp/install?name=buildplate&config=eyJjb21tYW5kIjoibm9kZSIsImFyZ3MiOlsiJHt3b3Jrc3BhY2VGb2xkZXJ9L21jcC9zZXJ2ZXIubWpzIl0sImVudiI6eyJCVUlMRFBMQVRFX1BSRVZJRVdfVVJMIjoiaHR0cDovL2J1aWxkcGxhdGUubG9jYWxob3N0In19)

Or `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "buildplate": {
      "command": "node",
      "args": ["${workspaceFolder}/mcp/server.mjs"],
      "env": { "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost" }
    }
  }
}
```

Subfolder workspace: `${workspaceFolder}/buildplate/mcp/server.mjs`.

#### Claude Code

```bash
claude mcp add --transport stdio --env BUILDPLATE_PREVIEW_URL=http://buildplate.localhost buildplate -- node /ABS/PATH/buildplate/mcp/server.mjs
```

#### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows). Merge, then fully quit and relaunch:

```json
{
  "mcpServers": {
    "buildplate": {
      "command": "node",
      "args": ["/ABS/PATH/buildplate/mcp/server.mjs"],
      "env": { "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost" }
    }
  }
}
```

#### Codex

```bash
codex mcp add buildplate --env BUILDPLATE_PREVIEW_URL=http://buildplate.localhost -- node /ABS/PATH/buildplate/mcp/server.mjs
```

Or `~/.codex/config.toml`:

```toml
[mcp_servers.buildplate]
command = "node"
args = ["/ABS/PATH/buildplate/mcp/server.mjs"]
env = { BUILDPLATE_PREVIEW_URL = "http://buildplate.localhost" }
```

## License

Code: MIT. Model weights (TripoSR, SD-Turbo, Hunyuan3D-2mini) have their own licenses and download on first use.
