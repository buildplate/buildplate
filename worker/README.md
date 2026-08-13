# Worker

Local FastAPI brain on `:8081`. Prefer `npx -y github:buildplate/buildplate setup` and `npx -y github:buildplate/buildplate start` — see the [README](../README.md).

```bash
npx -y github:buildplate/buildplate worker          # http://127.0.0.1:8081
# or
~/buildplate/venv/bin/python worker/server.py --lazy --verbose
```

`GET /health` · `POST /v1/generate` · `GET /v1/guide`

Venv, vendors, and cache live in `~/buildplate`, not next to this source.

Orientation regression bank (figurine photo-up, hat brim-down, CAD Y-up):

```bash
~/buildplate/venv/bin/python -m unittest tests.test_upright -v
```

See `tests/README.md` to add a fixture from an existing job.
