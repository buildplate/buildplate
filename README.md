# Buildplate

Local-first text-to-3D for agents. You bring the machine (and ideally a GPU); Buildplate exposes a **localhost MCP** your agent connects to. Prompt → mesh → preview → STL.

Shapeful was the cloud-editor prototype. Buildplate keeps the mesh brain and drops the SaaS editor.

## What you get

| Surface | Role |
|---------|------|
| **MCP (primary)** | Tools your agent calls: `generate`, `export_stl`, `preview`, `health` |
| **Local GPU worker** | Hunyuan3D inference on your box (CUDA first; Mac path TBD) |
| **Preview** | Read-only 3D viewer at `http://127.0.0.1:3920` + **Export STL** — no browser editing |

Your agent (Cursor, Claude Desktop, etc.) talks to Buildplate over MCP. Buildplate talks to the local worker. Models never need a Shapeful cloud account.

## Install (one-liner — target)

```bash
curl -fsSL https://raw.githubusercontent.com/jordan-homan/buildplate/main/scripts/install.sh | bash
```

Today the install script clones/updates the repo, installs Node deps, and prints Cursor MCP config. GPU weights are a separate step on machines that will run inference.

### Manual

```bash
git clone https://github.com/jordan-homan/buildplate.git
cd buildplate
npm install
npm run mcp          # MCP over stdio (for Cursor / Claude Desktop)
npm run preview      # optional browser viewer on :3920
```

### Cursor MCP config

Add to `~/.cursor/mcp.json` (or project `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "buildplate": {
      "command": "node",
      "args": ["/absolute/path/to/buildplate/mcp/server.mjs"],
      "env": {
        "BUILDPLATE_WORKER_URL": "http://127.0.0.1:8081",
        "BUILDPLATE_WORKER_SECRET": "same-as-worker/.worker_secret"
      }
    }
  }
}
```

Then start the GPU worker on the same machine (or point `BUILDPLATE_WORKER_URL` at a LAN box).

## MCP tools

| Tool | What it does |
|------|----------------|
| `health` | Worker ready / busy / device |
| `generate` | `prompt` (+ optional image path) → GLB/STL under `~/buildplate/out/` |
| `export_stl` | Ensure an STL exists for a job (converts from GLB if needed) |
| `preview` | Open the local viewer for a mesh (or return its `file://` / HTTP URL) |

**Chat preview:** the long-term goal is an in-chat 3D / image preview with an Export STL action. Cursor canvases cannot host Three.js today, so v0 uses the localhost viewer (and MCP can return a still preview when we add screenshot capture). Agents should still prefer `preview` + `export_stl` over pasting mesh bytes into chat.

## GPU worker

Adapted from Shapeful’s Hunyuan FastAPI sidecar. Same HTTP contract:

```http
GET  /health
POST /v1/generate
     Header: X-Worker-Secret: <secret>
     Body: { "prompt": "a small robot", "type": "glb" }
```

**Windows (CUDA)** — supported first (WinPortable Hunyuan layout). See [`worker/README.md`](./worker/README.md).

**Linux (CUDA)** — same Python worker; install Hunyuan/deps yourself and set `BUILDPLATE_HY3D_ROOT`.

**macOS** — MCP + preview run fine; **inference is not Metal/MPS yet**. Options we’re considering: port Hunyuan to MPS, a smaller MLX/Core ML backend, or “GPU box on the LAN.” Powerful Apple Silicon may eventually run a lighter backend without a discrete NVIDIA GPU.

## Architecture

```
Agent (Cursor / Claude / …)
        │  MCP (stdio)
        ▼
buildplate MCP  ──►  local worker :8081  (Hunyuan / future backends)
        │                    │
        └──── preview :3920 ─┘   out/ → .glb + .stl
```

**Dropped from Shapeful:** Clerk, Stripe, AWS CDK/Lambda, Dynamo/S3, Tailscale Funnel as default, Tinkercad-style browser editing, parametric shapes mode as the product core.

**Kept:** worker generate contract, single-job GPU lock, GLB/STL export path, read-only orbit preview.

## License / models

Code in this repo: MIT (see `LICENSE`).

Hunyuan3D weights are under Tencent’s community / non-commercial terms — fine for personal and dogfood use; check before commercial redistribution. Buildplate does not ship weights in git.

## Status

Private while we harden install + Mac story. Public when the one-liner and MCP loop feel good on at least Windows CUDA and a no-GPU Mac (MCP + preview only).
