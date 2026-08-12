# Buildplate architecture

## Product

Local-first text-to-3D. The user’s Mac or PC runs inference. Agents talk to a **localhost MCP**. No cloud GPU relay, no Tailscale Funnel, no hosted editor.

## Processes

1. **MCP** (`mcp/server.mjs`) — stdio; auto-spawns the worker if needed.
2. **Worker** (`worker/server.py`) — FastAPI `:8081`, TripoSR pipeline.
3. **Preview** (`preview/`) — Vite `:3920`, orbit + Export STL.

Outputs: `~/buildplate/out/<jobId>/`.

## Backend

```
generate(prompt|image)
  → SD-Turbo (if text)
  → rembg
  → TripoSR
  → GLB/STL
```

| Device | When |
|--------|------|
| `mps` | Apple Silicon Metal available |
| `cuda` | NVIDIA GPU available |
| `cpu` | fallback (slow) |
| `stub` | `BUILDPLATE_BACKEND=stub` or `BUILDPLATE_ALLOW_STUB=1` on load failure |

## Install surface

```bash
npm install
npm run setup    # venv + torch + deps
npm start        # worker + preview
npm run mcp      # agent entry (also starts worker on demand)
```

## Specs

See README recommended specs table (M1–M5 16 GB+ / NVIDIA 8 GB+ VRAM).
