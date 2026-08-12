#!/usr/bin/env node
/** Start the local Buildplate worker (venv python). */
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const WORKER = path.join(ROOT, "worker");
const VENV_PY =
  process.platform === "win32"
    ? path.join(WORKER, ".venv", "Scripts", "python.exe")
    : path.join(WORKER, ".venv", "bin", "python");

if (!existsSync(VENV_PY)) {
  console.error("Worker venv missing. Run: npm run setup");
  process.exit(1);
}

const extra = process.argv.slice(2);
const args = ["server.py", "--lazy", "--verbose", ...extra];
const child = spawn(VENV_PY, args, {
  cwd: WORKER,
  stdio: "inherit",
  env: {
    ...process.env,
    BUILDPLATE_WORKER_HOST: process.env.BUILDPLATE_WORKER_HOST || "127.0.0.1",
    BUILDPLATE_WORKER_PORT: process.env.BUILDPLATE_WORKER_PORT || "8081",
  },
});

child.on("exit", (code) => process.exit(code ?? 1));
