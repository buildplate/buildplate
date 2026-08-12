# Buildplate GPU worker

Local mesh brain (Hunyuan3D). Lives in-repo; multi‑GB runtime + weights stay on disk outside git.

Default exposure is **localhost only** — the MCP on the same machine calls `http://127.0.0.1:8081`. No Tailscale Funnel required.

## Layout

| In git (`worker/`) | On disk (default) |
|--------------------|-------------------|
| `buildplate_worker.py` | `C:\buildplate-worker\Hunyuan3D2_WinPortable\...` |
| `start-worker.bat` | HF weights under that tree’s `HuggingFaceHub\` |
| `download-models.bat` | |
| `.worker_secret` (local, gitignored) | |

Override runtime path with `BUILDPLATE_HY3D_ROOT`.

## Windows (CUDA) — first-class

1. Install Hunyuan3D2 WinPortable under `C:\buildplate-worker\` (or set `BUILDPLATE_HY3D_ROOT`).
2. Run `download-models.bat`.
3. Copy `.worker_secret.example` → `.worker_secret` and set a secret.
4. Run `start-worker.bat`.
5. Point MCP env at this box:

```text
BUILDPLATE_WORKER_URL=http://127.0.0.1:8081
BUILDPLATE_WORKER_SECRET=<same as .worker_secret>
```

## Linux (CUDA)

Same FastAPI entrypoint. Install Hunyuan + CUDA torch yourself, set `BUILDPLATE_HY3D_ROOT` to the tree that contains `Hunyuan3D-2`, then:

```bash
export BUILDPLATE_CACHE="$PWD/cache"
export WORKER_SECRET="$(cat .worker_secret)"
python buildplate_worker.py --host 127.0.0.1 --port 8081 --enable_t23d --enable_texgen
```

## macOS

MCP + preview work without a worker. **Inference on Metal/MPS is not shipped yet.** Use a Windows/Linux CUDA box on the LAN (`BUILDPLATE_WORKER_URL=http://<lan-ip>:8081`) or wait for an Apple backend.

## API

```http
GET  /health
POST /v1/generate
     Header: X-Worker-Secret: <secret>
     Body: { "prompt": "a small robot figurine", "type": "glb" }
     → binary GLB or STL
```

Also `{ "image": "<base64>" }` for image→3D. One job at a time.

## Smoke test

```powershell
curl http://127.0.0.1:8081/health
$secret = Get-Content .\worker\.worker_secret -Raw
curl.exe -X POST http://127.0.0.1:8081/v1/generate `
  -H "Content-Type: application/json" `
  -H "X-Worker-Secret: $secret" `
  -d "{\"prompt\":\"cute fox figurine\",\"type\":\"stl\"}" `
  --output test.stl
```

## Notes

- Hunyuan weights: Tencent community / non-commercial — OK for personal use; revisit before commercial redistribution.
- Admin update/restart endpoints remain for remote ops; Funnel is optional, not the product default.
