/**
 * Package root (npm / npx / git clone) vs user data (~/buildplate).
 * Mutable state never lives inside the package — npx cache is ephemeral.
 */
import { existsSync } from "node:fs";
import dns from "node:dns";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const PKG_ROOT = path.resolve(__dirname, "..");

export const HOME = process.env.BUILDPLATE_HOME?.trim()
  || path.join(os.homedir(), "buildplate");

export const OUT_DIR = process.env.BUILDPLATE_OUT_DIR?.trim() || path.join(HOME, "out");
export const REFS_DIR = process.env.BUILDPLATE_REFS_DIR?.trim() || path.join(HOME, "refs");
export const VENV = process.env.BUILDPLATE_VENV?.trim() || path.join(HOME, "venv");
export const VENDOR = process.env.BUILDPLATE_VENDOR?.trim() || path.join(HOME, "vendor");
export const CACHE = process.env.BUILDPLATE_CACHE?.trim() || path.join(HOME, "cache");
export const WORKER_SRC = path.join(PKG_ROOT, "worker");
export const PREVIEW_SRC = path.join(PKG_ROOT, "preview");
export const SETUP_OK = path.join(HOME, ".setup-ok");

export const BRANDED_HOST = "buildplate.localhost";
export const PREVIEW_PORT = Number(process.env.BUILDPLATE_PREVIEW_PORT || 3920);

let _brandedDns = undefined;

function lookupHost(host) {
  if (typeof dns.lookupSync === "function") {
    try {
      dns.lookupSync(host);
      return true;
    } catch {
      return false;
    }
  }
  const r = spawnSync(
    process.execPath,
    [
      "-e",
      `require("dns").lookup(${JSON.stringify(host)}, (e, a) => process.exit(e || !a ? 1 : 0)); setTimeout(() => process.exit(1), 1000);`,
    ],
    { timeout: 2000, stdio: "ignore" },
  );
  return r.status === 0;
}

/** macOS maps *.localhost; Windows DNS often returns NXDOMAIN unless hosts is edited. */
export function brandedHostResolves() {
  if (_brandedDns === undefined) _brandedDns = lookupHost(BRANDED_HOST);
  return _brandedDns;
}

function stripSlash(url) {
  return String(url).replace(/\/$/, "");
}

function isBrandedUrl(url) {
  try {
    return new URL(url).hostname === BRANDED_HOST;
  } catch {
    return false;
  }
}

/**
 * Public preview origin for MCP links, Vite HMR, and `open()`.
 * If buildplate.localhost does not resolve (typical Windows), use localhost:3920
 * even when BUILDPLATE_PREVIEW_URL still names the branded host.
 */
export function getPreviewUrl() {
  const fallback = `http://localhost:${PREVIEW_PORT}`;
  const env = process.env.BUILDPLATE_PREVIEW_URL?.trim();
  const dnsOk = brandedHostResolves();

  if (env) {
    const url = stripSlash(env);
    if (isBrandedUrl(url) && !dnsOk) return fallback;
    return url;
  }

  if (!dnsOk) return fallback;
  // Windows: don't assume port 80; Vite listens on PREVIEW_PORT.
  if (process.platform === "win32") return `http://${BRANDED_HOST}:${PREVIEW_PORT}`;
  return `http://${BRANDED_HOST}`;
}

export const PREVIEW_URL = getPreviewUrl();

export function venvPython() {
  return process.platform === "win32"
    ? path.join(VENV, "Scripts", "python.exe")
    : path.join(VENV, "bin", "python");
}

export function venvReady() {
  return existsSync(venvPython());
}

export function workerEnv() {
  return {
    BUILDPLATE_HOME: HOME,
    BUILDPLATE_OUT_DIR: OUT_DIR,
    BUILDPLATE_REFS_DIR: REFS_DIR,
    BUILDPLATE_VENV: VENV,
    BUILDPLATE_VENDOR: VENDOR,
    BUILDPLATE_CACHE: CACHE,
    BUILDPLATE_PREVIEW_URL: getPreviewUrl(),
  };
}
