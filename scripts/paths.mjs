/**
 * Package root (npm / npx / git clone) vs user data (~/buildplate).
 * Mutable state never lives inside the package — npx cache is ephemeral.
 */
import { existsSync } from "node:fs";
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

export const PREVIEW_URL = (process.env.BUILDPLATE_PREVIEW_URL || "http://buildplate.localhost").replace(
  /\/$/,
  "",
);

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
    BUILDPLATE_PREVIEW_URL: PREVIEW_URL,
  };
}
