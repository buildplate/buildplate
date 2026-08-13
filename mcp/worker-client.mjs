/**
 * Thin client for the local Buildplate worker.
 * Always targets localhost unless BUILDPLATE_WORKER_URL is overridden.
 */

import { WORKER_URL, probeHealth, ensureWorker, workerConfigured } from "./ensure-worker.mjs";

export { probeHealth as probeWorkerHealth, workerConfigured, ensureWorker, WORKER_URL };

function logWorker(level, msg, extra) {
  const line = `[buildplate-worker] ${msg}`;
  if (level === "error") console.error(line, extra ?? "");
  else console.error(line, extra ?? "");
}

export function missingWorkerError() {
  return "Local worker is not running. Run: npx buildplate setup && npx buildplate start";
}

/**
 * @param {{
 *   prompt?: string,
 *   image?: string|null,
 *   format?: "glb"|"stl",
 *   texture?: boolean,
 *   backend?: "auto"|"mesh"|"cad",
 *   engine?: "auto"|"openscad"|"cadquery"|"trimesh"|null,
 *   openscad?: string|null,
 *   cadquery?: string|null,
 *   trimesh_code?: string|null,
 *   quality?: "fast"|"quality"|null,
 *   vendor?: "triposr"|"hunyuan"|null,
 *   remesh?: boolean,
 *   target_faces?: number|null,
 *   waitForMesh?: boolean,
 * }} opts
 */
export async function generateMeshFromWorker({
  prompt,
  image = null,
  format = "glb",
  texture = true,
  backend = "auto",
  engine = null,
  openscad = null,
  cadquery = null,
  trimesh_code = null,
  quality = null,
  vendor = null,
  remesh = true,
  target_faces = null,
  waitForMesh = true,
}) {
  const health = await ensureWorker();
  if (!health.online) {
    throw new Error(health.detail || missingWorkerError());
  }

  const mode = (backend || "auto").toLowerCase();
  const needsMesh = mode === "mesh" || mode === "triposr" || (
    mode === "auto" && !openscad && !cadquery && !trimesh_code
  );

  // CAD can run while TripoSR is still loading
  if (needsMesh && waitForMesh && !health.mesh_ready && !health.ready) {
    const deadline = Date.now() + Number(process.env.BUILDPLATE_READY_TIMEOUT_MS || 600_000);
    while (Date.now() < deadline) {
      const h = await probeHealth(5000);
      if (h.mesh_ready || (mode === "auto" && h.ready && !needsMesh)) break;
      if (h.mesh_ready) break;
      if (h.detail && !h.online) throw new Error(h.detail);
      await new Promise((r) => setTimeout(r, 2000));
    }
    const final = await probeHealth(5000);
    if (needsMesh && !final.mesh_ready && !final.ready) {
      throw new Error(final.detail || "Worker mesh models still loading — try again shortly");
    }
  }

  if (mode === "cad" && health.cad_ready === false) {
    // Older workers may not report cad_ready — still try
  }

  const base = WORKER_URL.replace(/\/$/, "");
  const secret = process.env.BUILDPLATE_WORKER_SECRET?.trim();
  const enriched = finalizeWorkerPrompt(prompt);
  const imagePayload =
    typeof image === "string" && image.trim() ? image.trim() : null;

  const timeoutMs = Number(process.env.BUILDPLATE_WORKER_TIMEOUT_MS || 900_000);
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);

  const body = {
    prompt: enriched,
    type: mode === "cad" ? "stl" : format,
    texture: Boolean(texture),
    backend: mode,
  };
  if (engine) body.engine = engine;
  if (imagePayload) body.image = imagePayload;
  if (openscad) body.openscad = openscad;
  if (cadquery) body.cadquery = cadquery;
  if (trimesh_code) body.trimesh_code = trimesh_code;
  if (quality) body.quality = quality;
  if (vendor) body.vendor = vendor;
  if (remesh === false) body.remesh = false;
  if (target_faces) body.target_faces = target_faces;

  const headers = { "Content-Type": "application/json" };
  if (secret) headers["X-Worker-Secret"] = secret;

  let res;
  try {
    res = await fetch(`${base}/v1/generate`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error("GPU worker timed out");
    }
    throw new Error(`Worker unreachable: ${err?.message || err}`);
  } finally {
    clearTimeout(t);
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || data.error || data.text || detail;
    } catch {
      try {
        detail = (await res.text()) || detail;
      } catch {
        // ignore
      }
    }
    logWorker("error", `generate failed: ${detail}`, { status: res.status });
    if (res.status === 429) throw new Error("Worker busy — one job at a time");
    if (res.status === 503) throw new Error(detail || "Worker not ready");
    throw new Error(`Worker failed: ${detail}`);
  }

  const contentType = (res.headers.get("content-type") || "").toLowerCase();
  const textured = res.headers.get("x-textured") === "1";
  const kind =
    format === "stl" || mode === "cad" || contentType.includes("stl") ? "stl" : "glb";

  const buffer = await res.arrayBuffer();
  if (!buffer || buffer.byteLength < 84) {
    throw new Error("Worker returned an empty/invalid mesh");
  }

  return {
    buffer,
    kind,
    textured,
    taskId: res.headers.get("x-job-id") || "local",
    prompt: enriched,
    previewPath: res.headers.get("x-preview-path") || null,
    backend: res.headers.get("x-backend") || mode,
    engine: res.headers.get("x-engine") || null,
  };
}

/**
 * Retint an existing mesh job. Does not re-run Hunyuan/TripoSR.
 * @param {{ jobId?: string|null, prompt: string, color?: string|null }} opts
 */
export async function refineFromWorker({ jobId = null, prompt, color = null }) {
  const health = await ensureWorker();
  if (!health.online) {
    throw new Error(health.detail || missingWorkerError());
  }

  const base = WORKER_URL.replace(/\/$/, "");
  const secret = process.env.BUILDPLATE_WORKER_SECRET?.trim();
  const timeoutMs = Number(process.env.BUILDPLATE_WORKER_TIMEOUT_MS || 120_000);
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);

  const body = { prompt: String(prompt || "").trim(), keep_mesh: true };
  if (jobId) body.job_id = String(jobId);
  if (color) body.color = String(color);

  const headers = { "Content-Type": "application/json" };
  if (secret) headers["X-Worker-Secret"] = secret;

  let res;
  try {
    res = await fetch(`${base}/v1/refine`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error("GPU worker timed out");
    }
    throw new Error(`Worker unreachable: ${err?.message || err}`);
  } finally {
    clearTimeout(t);
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || data.error || data.text || detail;
    } catch {
      try {
        detail = (await res.text()) || detail;
      } catch {
        // ignore
      }
    }
    if (typeof detail === "object") detail = JSON.stringify(detail);
    logWorker("error", `refine failed: ${detail}`, { status: res.status });
    if (res.status === 429) throw new Error("Worker busy — one job at a time");
    throw new Error(`Worker failed: ${detail}`);
  }

  const buffer = await res.arrayBuffer();
  if (!buffer || buffer.byteLength < 84) {
    throw new Error("Worker returned an empty/invalid mesh");
  }

  return {
    buffer,
    kind: "glb",
    textured: res.headers.get("x-textured") !== "0",
    taskId: res.headers.get("x-job-id") || "local",
    prompt: String(prompt || "").trim(),
    previewPath: res.headers.get("x-preview-path") || null,
    backend: "refine",
    engine: "refine",
    parentJobId: res.headers.get("x-parent-job") || jobId || null,
  };
}

export async function fetchJobPreview(jobId) {
  const base = WORKER_URL.replace(/\/$/, "");
  try {
    const res = await fetch(`${base}/v1/jobs/${encodeURIComponent(jobId)}/preview.png`);
    if (!res.ok) return null;
    const buf = Buffer.from(await res.arrayBuffer());
    if (buf.byteLength < 32) return null;
    return buf;
  } catch {
    return null;
  }
}

function finalizeWorkerPrompt(prompt) {
  const base = String(prompt || "").trim();
  if (!base) {
    return "3D object, white background, centered, product photo";
  }
  return base;
}
