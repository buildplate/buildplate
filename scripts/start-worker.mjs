#!/usr/bin/env node
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { venvPython, venvReady, WORKER_SRC, workerEnv } from "./paths.mjs";

const py = venvPython();
if (!venvReady() || !existsSync(py)) {
  console.error("Worker venv missing. Run: npx -y github:buildplate/buildplate setup");
  process.exit(1);
}

const extra = process.argv.slice(2);
const child = spawn(
  py,
  ["server.py", "--lazy", "--verbose", ...extra],
  {
    cwd: WORKER_SRC,
    stdio: "inherit",
    env: {
      ...process.env,
      ...workerEnv(),
    },
  },
);
child.on("exit", (code) => process.exit(code ?? 1));
