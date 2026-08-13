# Architecture

See the root [README](../README.md) for install and agent usage.

```
Agent  --MCP stdio-->  mcp/server.mjs  --auto-spawn-->  worker :8081
                                              |
                                              +--> preview :3920
```

| Path | Role |
|------|------|
| `mcp/` | Stdio MCP: `health`, `save_reference`, `generate`, `refine`, `export_stl`, `preview` |
| `worker/` | FastAPI. Mesh: Hunyuan (quality) / TripoSR (fast) + remesh + view-projected PBR. CAD: trimesh / OpenSCAD / CadQuery |
| `preview/` | Vite viewer + Export STL |

Outputs: `~/buildplate/out/<jobId>/`. Refs: `~/buildplate/refs/`.
