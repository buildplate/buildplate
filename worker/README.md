# Buildplate worker

Local mesh brain. Runs **on the same machine** as the MCP — Apple Silicon (Metal), NVIDIA CUDA, or CPU.

## Pipeline

1. **Text→image** — `stabilityai/sd-turbo` (diffusers)
2. **Background remove** — `rembg`
3. **Image→mesh** — `stabilityai/TripoSR`
4. Export **GLB** or **STL**

## Setup

From repo root (preferred):

```bash
npm run setup
npm run worker          # http://127.0.0.1:8081
```

Or:

```bash
worker/.venv/bin/python worker/server.py --lazy --verbose
```

`--lazy` serves `/health` immediately and loads models in the background (or on first `generate`).

## API

```http
GET  /health
POST /v1/generate
     Body: { "prompt": "a small red mug", "type": "glb" }
     → binary mesh
```

Optional `image` (base64). Auth secret is **optional** on localhost.

## Device selection

| Priority | Backend |
|----------|---------|
| 1 | Apple **MPS** (M1–M5) |
| 2 | NVIDIA **CUDA** |
| 3 | **CPU** (slow) |

## Env

| var | default | meaning |
|-----|---------|---------|
| `BUILDPLATE_WORKER_HOST` | `127.0.0.1` | bind address |
| `BUILDPLATE_WORKER_PORT` | `8081` | port |
| `BUILDPLATE_CACHE` | `worker/cache` | logs + job scratch |
| `BUILDPLATE_BACKEND` | `triposr` | or `stub` |
| `BUILDPLATE_ALLOW_STUB` | `0` | fall back to stub mesh if model load fails |
| `BUILDPLATE_WORKER_SECRET` | unset | optional `X-Worker-Secret` |

## Smoke test

```bash
curl -s http://127.0.0.1:8081/health | jq
curl -X POST http://127.0.0.1:8081/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"cute fox figurine","type":"stl"}' \
  --output /tmp/fox.stl
```
