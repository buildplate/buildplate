#!/usr/bin/env node
/**
 * Buildplate CLI — what `npx buildplate` runs.
 *
 *   (no args) / mcp   MCP stdio for Cursor, Claude, Codex, …
 *   setup             Python venv + PyTorch + TripoSR + Hunyuan → ~/buildplate
 *   start             worker :8081 + preview http://buildplate.localhost
 *   worker | preview  one process
 */
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PKG_ROOT = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const cmd = args[0];

const HELP = `Buildplate — local 3D MCP (CAD + mesh) on your machine.

Usage:
  npx buildplate setup      once — Python venv, PyTorch, TripoSR, Hunyuan
  npx buildplate start      worker + preview (http://buildplate.localhost)
  npx buildplate            MCP stdio (Cursor / Claude / Codex run this)

Need Node 20+ and Python 3.10–3.13 (3.12 best). Data: ~/buildplate
`;

function runScript(rel, extra = [], opts = {}) {
  const child = spawn(process.execPath, [path.join(PKG_ROOT, rel), ...extra], {
    stdio: "inherit",
    env: process.env,
    ...opts,
  });
  return child;
}

function exitWith(child) {
  child.on("exit", (code) => process.exit(code ?? 1));
}

if (!cmd || cmd === "mcp") {
  await import("./mcp/server.mjs");
} else if (cmd === "setup") {
  exitWith(runScript("scripts/setup-worker.mjs", args.slice(1)));
} else if (cmd === "worker") {
  exitWith(runScript("scripts/start-worker.mjs", args.slice(1)));
} else if (cmd === "preview") {
  exitWith(runScript("scripts/start-preview.mjs", args.slice(1)));
} else if (cmd === "start") {
  const worker = runScript("scripts/start-worker.mjs");
  const preview = runScript("scripts/start-preview.mjs");
  const stop = () => {
    worker.kill("SIGTERM");
    preview.kill("SIGTERM");
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  let exiting = false;
  const onExit = (code) => {
    if (exiting) return;
    exiting = true;
    stop();
    process.exit(code ?? 1);
  };
  worker.on("exit", onExit);
  preview.on("exit", onExit);
} else if (cmd === "help" || cmd === "-h" || cmd === "--help") {
  process.stdout.write(HELP);
} else {
  process.stderr.write(`Unknown command: ${cmd}\n\n${HELP}`);
  process.exit(1);
}
