#!/usr/bin/env node
/**
 * Bootstrap Buildplate Python worker venv + deps into ~/buildplate.
 * Picks python3.12 → 3.11 → 3.13 (skips 3.14 — torch wheels lag).
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
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
    "python",
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

function torchCudaCmake(py) {
  const r = spawnSync(
    py,
    [
      "-c",
      "import torch, pathlib; print(pathlib.Path(torch.__file__).resolve().parent / 'share' / 'cmake' / 'Caffe2' / 'public' / 'cuda.cmake')",
    ],
    { encoding: "utf8" },
  );
  if (r.status !== 0) return null;
  const p = (r.stdout || "").trim();
  return p && existsSync(p) ? p : null;
}

/**
 * CUDA 12.9 removed CUDA::nvToolsExt (header-only nvtx3). PyTorch wheels still
 * link torch::nvtoolsext → CUDA::nvToolsExt, so torchmcubes' CUDA build dies.
 * Mac Metal never hits this file's NVTX else-branch. Idempotent.
 */
function patchTorchNvtxForCuda129(py) {
  const cmake = torchCudaCmake(py);
  if (!cmake) return false;
  let src = readFileSync(cmake, "utf8");
  if (src.includes("BUILDPLATE_NVTX3_STUB")) return true;
  const re =
    /else\(\)\s*\r?\n\s*message\(WARNING "Cannot find NVTX3, find old NVTX instead"\)\s*\r?\n\s*add_library\(torch::nvtoolsext INTERFACE IMPORTED\)\s*\r?\n\s*set_property\(TARGET torch::nvtoolsext PROPERTY INTERFACE_LINK_LIBRARIES CUDA::nvToolsExt\)\s*\r?\n\s*endif\(\)/;
  if (!re.test(src)) return false;
  const stub = `else()
  # BUILDPLATE_NVTX3_STUB — CUDA 12.9+ removed CUDA::nvToolsExt
  find_path(nvtx3_dir NAMES nvtx3 PATHS "\${CUDAToolkit_INCLUDE_DIRS}" "\${CUDA_INCLUDE_DIRS}"
            "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.9/include"
            "/usr/local/cuda/include")
  if(nvtx3_dir)
    message(STATUS "Using system NVTX3 headers at \${nvtx3_dir}")
    add_library(torch::nvtx3 INTERFACE IMPORTED)
    target_include_directories(torch::nvtx3 INTERFACE "\${nvtx3_dir}")
    target_compile_definitions(torch::nvtx3 INTERFACE TORCH_CUDA_USE_NVTX3)
  elseif(TARGET CUDA::nvtx3)
    add_library(CUDA::nvToolsExt INTERFACE IMPORTED)
    target_compile_definitions(CUDA::nvToolsExt INTERFACE TORCH_CUDA_USE_NVTX3)
    target_link_libraries(CUDA::nvToolsExt INTERFACE CUDA::nvtx3)
    add_library(torch::nvtoolsext INTERFACE IMPORTED)
    set_property(TARGET torch::nvtoolsext PROPERTY INTERFACE_LINK_LIBRARIES CUDA::nvToolsExt)
  else()
    message(WARNING "Cannot find NVTX3, creating empty nvtoolsext stub")
    add_library(torch::nvtoolsext INTERFACE IMPORTED)
  endif()
endif()`;
  src = src.replace(re, stub);
  writeFileSync(cmake, src);
  console.log("→ patched Torch cuda.cmake for CUDA 12.9 nvtx3");
  return true;
}

function installTorchmcubes(py) {
  const spec = "git+https://github.com/tatsy/torchmcubes.git";
  console.log("→ torchmcubes (TripoSR marching cubes)");
  if (process.platform === "darwin") {
    run(py, ["-m", "pip", "install", spec]);
    return;
  }
  const withNvtx = [spec, "--config-settings=cmake.define.USE_SYSTEM_NVTX=ON"];
  console.log(`→ ${py} -m pip install ${withNvtx.join(" ")}`);
  let r = spawnSync(py, ["-m", "pip", "install", ...withNvtx], { stdio: "inherit" });
  if (r.status === 0) return;
  console.log("→ torchmcubes: CUDA 12.9 nvToolsExt missing — patch Torch cmake and retry");
  patchTorchNvtxForCuda129(py);
  r = spawnSync(py, ["-m", "pip", "install", ...withNvtx], { stdio: "inherit" });
  if (r.status === 0) return;
  console.log("→ torchmcubes CUDA build failed — CPU marching cubes (same path Mac uses)");
  r = spawnSync(
    py,
    [
      "-m",
      "pip",
      "install",
      spec,
      "--config-settings=cmake.define.CMAKE_CUDA_COMPILER=CMAKE_CUDA_COMPILER-NOTFOUND",
    ],
    {
      stdio: "inherit",
      env: { ...process.env, CMAKE_CUDA_COMPILER: "CMAKE_CUDA_COMPILER-NOTFOUND" },
    },
  );
  if (r.status !== 0) process.exit(r.status ?? 1);
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
  console.error("  Windows: install Python 3.12 and set BUILDPLATE_PYTHON to python.exe");
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
  rmSync(TRIPOSR, { recursive: true, force: true });
  run("git", ["clone", "--depth", "1", "https://github.com/VAST-AI-Research/TripoSR.git", TRIPOSR]);
} else {
  console.log("→ TripoSR vendor present");
}

if (!existsSync(path.join(HUNYUAN, "hy3dgen", "shapegen", "pipelines.py"))) {
  mkdirSync(VENDOR, { recursive: true });
  rmSync(HUNYUAN, { recursive: true, force: true });
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
installTorchmcubes(vpy);

// TripoSR + newer torch: weights_only=False for ckpt load
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
console.log("  Start:     npx -y github:buildplate/buildplate start");
console.log("  MCP stdio: npx -y github:buildplate/buildplate");
console.log("");
console.log("Mesh vendors:");
console.log("  triposr  — fast (always)");
console.log("  hunyuan  — quality shape (Hunyuan3D-2mini, lazy-loaded on first quality generate)");
console.log("");
console.log("CAD engines:");
console.log("  trimesh+manifold3d — always on (agent writes trimesh_code)");
console.log("  Optional OpenSCAD: brew install --cask openscad");
console.log(
  `  Optional CadQuery: ${path.join(VENV, process.platform === "win32" ? path.join("Scripts", "pip.exe") : path.join("bin", "pip"))} install cadquery`,
);
