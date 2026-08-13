/**
 * Ensure the local Buildplate worker is up (spawn if needed).
 * End users should not configure Funnel URLs — this is always localhost.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const WORKER_DIR = path.join(ROOT, "worker");
const VENV_PY =
  process.platform === "win32"
    ? path.join(WORKER_DIR, ".venv", "Scripts", "python.exe")
    : path.join(WORKER_DIR, ".venv", "bin", "python");

const HOST = process.env.BUILDPLATE_WORKER_HOST || "127.0.0.1";
const PORT = process.env.BUILDPLATE_WORKER_PORT || "8081";
export const WORKER_URL =
  process.env.BUILDPLATE_WORKER_URL?.trim() || `http://${HOST}:${PORT}`;

let child = null;

export async function probeHealth(timeoutMs = 2000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${WORKER_URL.replace(/\/$/, "")}/health`, {
      signal: ctrl.signal,
    });
    if (!res.ok) return { online: false, ready: false, detail: `HTTP ${res.status}` };
    const data = await res.json().catch(() => ({}));
    return {
      online: true,
      ready: Boolean(data.ready),
      mesh_ready: Boolean(data.mesh_ready ?? data.ready),
      cad_ready: data.cad_ready !== undefined ? Boolean(data.cad_ready) : true,
      cad_engines: data.cad_engines ?? [],
      busy: Boolean(data.busy),
      model: data.model ?? null,
      device: data.device ?? null,
      backend: data.backend ?? null,
      detail: data.last_error ?? null,
      raw: data,
    };
  } catch (err) {
    const detail = err?.name === "AbortError" ? "timeout" : err?.message || "unreachable";
    return { online: false, ready: false, detail };
  } finally {
    clearTimeout(t);
  }
}

export function workerConfigured() {
  // Always "configured" for localhost auto-managed worker.
  return true;
}

export async function ensureWorker() {
  const existing = await probeHealth(1500);
  if (existing.online) return existing;

  if (!existsSync(VENV_PY)) {
    return {
      online: false,
      ready: false,
      detail: "Worker venv missing — run: npm run setup",
    };
  }

  if (!child || child.exitCode != null) {
    console.error(`[buildplate] starting local worker on ${HOST}:${PORT}`);
    child = spawn(
      VENV_PY,
      ["server.py", "--lazy", "--verbose"],
      {
        cwd: WORKER_DIR,
        stdio: ["ignore", "pipe", "pipe"],
        env: {
          ...process.env,
          BUILDPLATE_WORKER_HOST: HOST,
          BUILDPLATE_WORKER_PORT: String(PORT),
          // Prefer real backend; allow stub only if explicitly set
          BUILDPLATE_ALLOW_STUB: process.env.BUILDPLATE_ALLOW_STUB || "0",
        },
        detached: false,
      },
    );
    child.stdout?.on("data", (b) => console.error("[worker]", b.toString().trimEnd()));
    child.stderr?.on("data", (b) => console.error("[worker]", b.toString().trimEnd()));
    child.on("exit", (code) => {
      console.error(`[buildplate] worker exited (${code})`);
      child = null;
    });
  }

  // Wait for /health to come up (models may still be loading → ready=false ok)
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    const h = await probeHealth(2000);
    if (h.online) return h;
    await new Promise((r) => setTimeout(r, 500));
  }
  return { online: false, ready: false, detail: "worker start timeout" };
}
