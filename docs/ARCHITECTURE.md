# Buildplate architecture

## Product pivot (from Shapeful)

| Shapeful | Buildplate |
|----------|------------|
| Cloud SPA + Clerk + AWS | Local process on your machine |
| Agent talks to our API | Agent talks to **localhost MCP** |
| Funnel to home GPU | Worker on same host (or LAN URL) |
| Full browser editor | Read-only preview + STL export |
| We host LLM keys | You bring agent + optional local keys later |

## Processes

1. **MCP server** (`mcp/server.mjs`) — stdio MCP for Cursor / Claude Desktop.
2. **Worker** (`worker/buildplate_worker.py`) — FastAPI `:8081`, Hunyuan inference.
3. **Preview** (`preview/`) — Vite app `:3920`, orbit camera, Export STL.

Outputs land in `~/buildplate/out/<jobId>/` (override with `BUILDPLATE_OUT_DIR`).

## Backend adapter (planned)

```
generate() → Backend.generate(prompt, image?) → { glbPath, stlPath? }
```

| Backend | Platform | Status |
|---------|----------|--------|
| `hunyuan-cuda` | Windows / Linux + NVIDIA | Ported from Shapeful |
| `hunyuan-mps` / MLX | Apple Silicon | Not started |
| `stub` | Any | Dev without GPU (returns fixture mesh) |

MCP should not care which backend is hot — only `/health` + `/v1/generate`.

## Chat-window preview

Ideal UX: agent calls `generate` → user sees the mesh **in the chat UI** with Export STL.

Constraints today:
- Cursor canvases cannot import Three.js / load arbitrary npm.
- MCP can return images (screenshot of the mesh) and file paths/URIs.

v0: `preview` opens localhost viewer. Next: worker or MCP captures a PNG turntable still and returns it as an MCP image content block alongside the STL path.

## Cross-platform install

| Piece | Mac | Windows | Linux |
|-------|-----|---------|-------|
| MCP (Node) | ✓ | ✓ | ✓ |
| Preview (browser) | ✓ | ✓ | ✓ |
| CUDA worker | — | ✓ first | ✓ (manual) |
| Apple inference | TBD | — | — |

One-liner (`scripts/install.sh`) installs Node side everywhere; GPU bootstrap remains OS-specific scripts under `worker/`.
