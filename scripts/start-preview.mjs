#!/usr/bin/env node
/**
 * Vite on :3920 plus a loopback :80 proxy so the public URL is
 * http://buildplate.localhost (no port). Binding 80 needs one macOS
 * admin prompt; after that the proxy stays up on localhost only.
 */
import { spawn, spawnSync } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const PROXY = path.join(ROOT, "scripts/preview-proxy.mjs");
const VITE = path.join(ROOT, "preview", "node_modules", ".bin", "vite");
const PREVIEW_PORT = Number(process.env.BUILDPLATE_PREVIEW_PORT || 3920);

function portOpen(port, host) {
  return new Promise((resolve) => {
    const socket = net.connect({ port, host }, () => {
      socket.end();
      resolve(true);
    });
    socket.setTimeout(400, () => {
      socket.destroy();
      resolve(false);
    });
    socket.on("error", () => resolve(false));
  });
}

async function waitPort(port, host, ms = 20_000) {
  const start = Date.now();
  while (Date.now() - start < ms) {
    if (await portOpen(port, host)) return true;
    await new Promise((r) => setTimeout(r, 150));
  }
  return false;
}

async function port80Up() {
  return (await portOpen(80, "127.0.0.1")) || (await portOpen(80, "::1"));
}

function appleQuote(value) {
  return `"${String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

async function ensurePort80() {
  if (process.env.BUILDPLATE_PREVIEW_BIND80 === "0") return;
  if (await port80Up()) {
    console.error("[buildplate-preview] http://buildplate.localhost");
    return;
  }

  const child = spawn(process.execPath, [PROXY], {
    stdio: ["ignore", "inherit", "inherit"],
  });
  const outcome = await Promise.race([
    new Promise((resolve) => child.once("exit", (code) => resolve(code ?? 1))),
    new Promise((resolve) => setTimeout(() => resolve("running"), 500)),
  ]);
  if (outcome === "running" || outcome === 0) {
    if (await waitPort(80, "127.0.0.1", 3000) || await waitPort(80, "::1", 1000)) {
      console.error("[buildplate-preview] http://buildplate.localhost");
      return;
    }
  }
  if (outcome !== 77 && outcome !== "running" && outcome !== 0) {
    console.error(`[buildplate-preview] port-80 proxy exited ${outcome}`);
  }

  if (process.platform === "darwin") {
    const inner = `${JSON.stringify(process.execPath)} ${JSON.stringify(PROXY)} --daemon`;
    console.error(
      "Buildplate needs loopback port 80 so preview is http://buildplate.localhost. " +
        "macOS will ask for your password — binds localhost only, not the LAN.",
    );
    const r = spawnSync(
      "osascript",
      ["-e", `do shell script ${appleQuote(inner)} with administrator privileges`],
      { stdio: "inherit" },
    );
    if (r.status !== 0) {
      console.error(
        "Port 80 not granted. Preview is still at http://buildplate.localhost:3920 — " +
          "set BUILDPLATE_PREVIEW_URL to that, or re-run and allow the prompt.",
      );
      return;
    }
  } else if (process.platform === "linux") {
    console.error("Retrying port 80 with sudo (localhost only)…");
    const r = spawnSync("sudo", ["-n", process.execPath, PROXY], {
      stdio: "inherit",
      detached: true,
    });
    if (r.status !== 0) {
      console.error(
        "Could not bind port 80. Allow sudo for the preview proxy, or use " +
          "http://buildplate.localhost:3920",
      );
      return;
    }
  } else {
    console.error(
      "Port 80 needs elevation on this OS. Using http://buildplate.localhost:3920",
    );
    return;
  }

  if (await waitPort(80, "127.0.0.1", 8000) || await waitPort(80, "::1", 2000)) {
    console.error("[buildplate-preview] http://buildplate.localhost");
    return;
  }
  console.error("[buildplate-preview] port 80 did not come up; use :3920 as fallback");
}

const vite = spawn(
  VITE,
  ["--host", "localhost", "--port", String(PREVIEW_PORT)],
  {
    cwd: path.join(ROOT, "preview"),
    stdio: "inherit",
    env: process.env,
  },
);
vite.on("exit", (code) => process.exit(code ?? 1));

if (!(await waitPort(PREVIEW_PORT, "localhost"))) {
  console.error(`Vite did not listen on ${PREVIEW_PORT}`);
  process.exit(1);
}
await ensurePort80();
