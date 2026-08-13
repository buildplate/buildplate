#!/usr/bin/env node
/**
 * Bootstrap Buildplate Python worker venv + deps into ~/buildplate.
 * Picks python3.12 → 3.11 → 3.13 (skips 3.14 — torch wheels lag).
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import path from "node:path";
import os from "node:os";
import { HOME, VENV, VENDOR, CACHE, SETUP_OK, WORKER_SRC } from "./paths.mjs";

const REQ = path.join(WORKER_SRC, "requirements.txt");
const TRIPOSR = path.join(VENDOR, "TripoSR");
const HUNYUAN = path.join(VENDOR, "Hunyuan3D-2");

function findPython() {
  const candidates = [
    process.env.BUILDPLATE_PYTHON,
    "/opt/homebrew/bin/python3.12",
    "/usr/local/bin/python3.12",
    "python3.12",
    "/opt/homebrew/bin/python3.11",
    "python3.11",
    "/opt/homebrew/bin/python3.13",
    "python3.13",
    "python3",
  ].filter(Boolean);

  for (const c of candidates) {
    const r = spawnSync(c, ["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], {
      encoding: "utf8",
    });
    if (r.status !== 0) continue;
    const ver = (r.stdout || "").trim();
    const [maj, min] = ver.split(".").map(Number);
    if (maj !== 3 || min < 10 || min >= 14) {
      console.log(`  skip ${c} (${ver}) — need Python 3.10–3.13`);
      continue;
    }
    return { bin: c, ver };
  }
  return null;
}

function run(cmd, args, opts = {}) {
  console.log(`→ ${cmd} ${args.join(" ")}`);
  const r = spawnSync(cmd, args, { stdio: "inherit", ...opts });
  if (r.status !== 0) {
    process.exit(r.status ?? 1);
  }
}

function venvPython() {
  return process.platform === "win32"
    ? path.join(VENV, "Scripts", "python.exe")
    : path.join(VENV, "bin", "python");
}

function installTorch(py) {
  if (process.platform === "darwin") {
    run(py, ["-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"]);
    run(py, ["-m", "pip", "install", "torch", "torchvision", "torchaudio"]);
    return;
  }
  const nvsmi = spawnSync("nvidia-smi", [], { encoding: "utf8" });
  run(py, ["-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"]);
  if (nvsmi.status === 0) {
    console.log("→ NVIDIA detected — installing CUDA torch");
    run(py, [
      "-m",
      "pip",
      "install",
      "torch",
      "torchvision",
      "torchaudio",
      "--index-url",
      "https://download.pytorch.org/whl/cu124",
    ]);
  } else {
    console.log("→ No NVIDIA — installing CPU torch");
    run(py, [
      "-m",
      "pip",
      "install",
      "torch",
      "torchvision",
      "torchaudio",
      "--index-url",
      "https://download.pytorch.org/whl/cpu",
    ]);
  }
}

console.log("Buildplate worker setup");
console.log(`  platform: ${process.platform} ${os.arch()}`);
console.log(`  home:     ${HOME}`);
console.log(`  venv:     ${VENV}`);
console.log(`  vendor:   ${VENDOR}`);

const py = findPython();
if (!py) {
  console.error("No suitable Python found (need 3.10–3.13).");
  console.error("  macOS: brew install python@3.12");
  console.error("  Or set BUILDPLATE_PYTHON=/path/to/python3.12");
  process.exit(1);
}
console.log(`  python:   ${py.bin} (${py.ver})`);

mkdirSync(HOME, { recursive: true });
mkdirSync(CACHE, { recursive: true });

if (!existsSync(path.join(VENV, "pyvenv.cfg"))) {
  run(py.bin, ["-m", "venv", VENV]);
}

const vpy = venvPython();
installTorch(vpy);

if (!existsSync(path.join(TRIPOSR, "tsr", "system.py"))) {
  mkdirSync(VENDOR, { recursive: true });
  if (existsSync(TRIPOSR)) {
    spawnSync("rm", ["-rf", TRIPOSR], { stdio: "inherit" });
  }
  run("git", ["clone", "--depth", "1", "https://github.com/VAST-AI-Research/TripoSR.git", TRIPOSR]);
} else {
  console.log("→ TripoSR vendor present");
}

if (!existsSync(path.join(HUNYUAN, "hy3dgen", "shapegen", "pipelines.py"))) {
  mkdirSync(VENDOR, { recursive: true });
  if (existsSync(HUNYUAN)) {
    spawnSync("rm", ["-rf", HUNYUAN], { stdio: "inherit" });
  }
  run("git", ["clone", "--depth", "1", "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.git", HUNYUAN]);
} else {
  console.log("→ Hunyuan3D-2 vendor present");
}

const hyInit = path.join(HUNYUAN, "hy3dgen", "shapegen", "__init__.py");
if (existsSync(hyInit)) {
  let src = readFileSync(hyInit, "utf8");
  if (src.includes("from .postprocessors import")) {
    src = src.replace(
      /from \.postprocessors import[^\n]*\n/,
      "# postprocessors skipped — pymeshlab not required; Buildplate remeshes after shape\n",
    );
    writeFileSync(hyInit, src);
    console.log("→ patched Hunyuan shapegen to skip pymeshlab postprocessors");
  }
}

run(vpy, ["-m", "pip", "install", "-r", REQ]);

const tsrSystem = path.join(TRIPOSR, "tsr", "system.py");
if (existsSync(tsrSystem)) {
  let src = readFileSync(tsrSystem, "utf8");
  if (src.includes('torch.load(weight_path, map_location="cpu")') && !src.includes("weights_only=False")) {
    src = src.replace(
      'torch.load(weight_path, map_location="cpu")',
      'torch.load(weight_path, map_location="cpu", weights_only=False)',
    );
    writeFileSync(tsrSystem, src);
    console.log("→ patched TripoSR torch.load(weights_only=False)");
  }
}

const isoPath = path.join(TRIPOSR, "tsr", "models", "isosurface.py");
if (existsSync(isoPath)) {
  let src = readFileSync(isoPath, "utf8");
  if (src.includes("self.mc_func(level.detach(), 0.0)") && !src.includes("level.detach().cpu(), 0.0)")) {
    src = src.replace(
      "self.mc_func(level.detach(), 0.0)",
      "self.mc_func(level.detach().cpu(), 0.0)",
    );
    writeFileSync(isoPath, src);
    console.log("→ patched TripoSR marching_cubes for MPS/CPU");
  }
}

writeFileSync(
  SETUP_OK,
  JSON.stringify(
    {
      python: py.ver,
      at: new Date().toISOString(),
      platform: process.platform,
      home: HOME,
    },
    null,
    2,
  ) + "\n",
);

console.log("");
console.log("Setup complete. Models and venv live in ~/buildplate");
console.log("  Start:     npx buildplate start");
console.log("  MCP stdio: npx buildplate");
console.log("");
console.log("Mesh vendors:");
console.log("  triposr  — fast (always)");
console.log("  hunyuan  — quality shape (Hunyuan3D-2mini, lazy-loaded on first quality generate)");
console.log("");
console.log("CAD engines:");
console.log("  trimesh+manifold3d — always on (agent writes trimesh_code)");
console.log("  Optional OpenSCAD: brew install --cask openscad");
console.log("  Optional CadQuery: ~/buildplate/venv/bin/pip install cadquery");
