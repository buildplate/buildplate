# Architecture

See the root [README](../README.md) for install and agent usage.

```
npx -y github:buildplate/buildplate          →  cli.mjs  →  MCP stdio (mcp/server.mjs)
npx -y github:buildplate/buildplate setup    →  ~/buildplate/venv + vendor
npx -y github:buildplate/buildplate start    →  worker :8081 + preview http://buildplate.localhost
```

```
Agent  --MCP stdio-->  npx -y github:buildplate/buildplate  --auto-spawn-->  worker :8081
                                              |
                                              +--> preview http://buildplate.localhost
```

| Path | Role |
|------|------|
| `cli.mjs` | `npx -y github:buildplate/buildplate` — setup / start / MCP |
| `mcp/` | Stdio MCP: `health`, `save_reference`, `generate`, `refine`, `export_stl`, `preview` |
| `worker/` | FastAPI sources in the npm package. Mesh: Hunyuan (quality) / TripoSR (fast) + remesh + view-projected PBR. CAD: trimesh / OpenSCAD / CadQuery |
| `preview/` | Vite viewer + Export STL + open in Bambu / Orca / Prusa / Cura / Creality Print |

User data (`~/buildplate`): `out/`, `refs/`, `venv/`, `vendor/` (TripoSR, Hunyuan), `cache/` (weights + jobs).
