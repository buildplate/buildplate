# Worker

Local FastAPI brain on `:8081`. Prefer `npm run setup` and `npm start` from the repo root — see the [README](../README.md).

```bash
npm run worker          # http://127.0.0.1:8081
# or
worker/.venv/bin/python worker/server.py --lazy --verbose
```

`GET /health` · `POST /v1/generate` · `GET /v1/guide`
