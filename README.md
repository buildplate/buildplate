# Buildplate

Local-first text-to-3D for agents. Clone it, install, run — **your machine** does the work (Apple Silicon Mac or NVIDIA PC). Primary interface is a **localhost MCP** your agent connects to.

## Quick start

```bash
git clone https://github.com/jordan-homan/buildplate.git
cd buildplate
npm install
npm run setup      # Python venv + PyTorch + TripoSR (one-time, downloads models on first generate)
npm start          # local worker (:8081) + preview (:3920)
```

Then point your agent at the MCP (stdio):

```bash
npm run mcp
```

Or add [`examples/cursor-mcp.json`](./examples/cursor-mcp.json) to Cursor’s MCP config. The MCP **auto-starts** the local worker if it isn’t already up — no remote GPU URL, no Tailscale Funnel.

### One-liner (target)

```bash
curl -fsSL https://raw.githubusercontent.com/jordan-homan/buildplate/main/scripts/install.sh | bash
```

## What you get

| Surface | Role |
|---------|------|
| **MCP** | `health`, `generate`, `export_stl`, `preview` |
| **Worker** | Local text/image → mesh (TripoSR pipeline) on Metal / CUDA / CPU |
| **Preview** | Read-only 3D viewer + **Export STL** at http://127.0.0.1:3920 |

## Recommended specs

| | Minimum | Comfortable | Notes |
|--|---------|-------------|--------|
| **Apple Silicon** | M1 / M2, **16 GB** unified | **M1 Pro/Max+**, **32 GB+** | Metal (MPS). This is the primary Mac path. |
| **Windows / Linux** | NVIDIA **8 GB** VRAM | **12–16 GB+** VRAM | CUDA. AMD/Intel GPU not supported yet (CPU fallback is slow). |
| **CPU-only** | 16 GB RAM | 32 GB+ | Works, but text→3D can take many minutes. |
| **Disk** | ~8 GB free | ~15 GB | PyTorch + SD-Turbo + TripoSR weights. |
| **OS** | macOS 13+ (Apple Silicon), Windows 10+, Ubuntu 22.04+ | latest | Intel Macs: CPU only (no MPS). |
| **Node** | 20+ | 22+ | MCP + preview |
| **Python** | 3.10–3.13 | 3.12 | Created by `npm run setup` |

**Assumption:** end users are on an **M1–M5 Mac** or an **NVIDIA GPU PC**. We do **not** assume a discrete GPU on Mac — unified memory + Metal is enough at 16 GB+, happier at 32 GB.

## How generation works

```
prompt  →  SD-Turbo (text→image)  →  rembg  →  TripoSR (image→mesh)  →  GLB/STL
image   →  rembg                  →  TripoSR                         →  GLB/STL
```

Device is auto-selected: **MPS → CUDA → CPU**.

## MCP tools

| Tool | What it does |
|------|----------------|
| `health` | Worker online / ready / device |
| `generate` | Prompt (+ optional image) → `~/buildplate/out/<jobId>/` |
| `export_stl` | Path to STL for a job |
| `preview` | Open localhost viewer |

## Architecture

```
Agent (Cursor / Claude / …)
        │  MCP stdio
        ▼
buildplate MCP  ──auto-spawn──►  local worker :8081
        │                              │
        └──── preview :3920 ◄──────────┘
```

See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).

## License / models

Code: MIT (`LICENSE`).

TripoSR / SD-Turbo weights have their own licenses (Stability AI / Tripo). Buildplate does not vendor weights in git — they download on first use.

## Status

Private while we harden Mac install + first-generate UX. Public when `npm run setup && npm start` feels good on M-series and an NVIDIA box.
