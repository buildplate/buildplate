# Worker

Local FastAPI brain on `:8081`. Prefer `npx buildplate setup` and `npx buildplate start` — see the [README](../README.md).

```bash
npx buildplate worker          # http://127.0.0.1:8081
# or
~/buildplate/venv/bin/python worker/server.py --lazy --verbose
```

`GET /health` · `POST /v1/generate` · `GET /v1/guide`

Venv, vendors, and cache live in `~/buildplate`, not next to this source.
