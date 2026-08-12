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
  return "Local worker is not running. Run: npm run setup && npm run worker";
}

/**
 * @param {{ prompt: string, image?: string|null, format?: "glb"|"stl", texture?: boolean }} opts
 */
export async function generateMeshFromWorker({
  prompt,
  image = null,
  format = "glb",
  texture = true,
}) {
  const health = await ensureWorker();
  if (!health.online) {
    throw new Error(health.detail || missingWorkerError());
  }

  // Wait until models are ready (lazy load)
  if (!health.ready) {
    const deadline = Date.now() + Number(process.env.BUILDPLATE_READY_TIMEOUT_MS || 600_000);
    while (Date.now() < deadline) {
      const h = await probeHealth(5000);
      if (h.ready) break;
      if (h.detail && !h.online) throw new Error(h.detail);
      await new Promise((r) => setTimeout(r, 2000));
    }
    const final = await probeHealth(5000);
    if (!final.ready) {
      throw new Error(final.detail || "Worker models still loading — try again shortly");
    }
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
    type: format,
    texture: Boolean(texture),
  };
  if (imagePayload) body.image = imagePayload;

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
    if (res.status === 503) throw new Error("Worker not ready (models still loading)");
    throw new Error(`Worker failed: ${detail}`);
  }

  const contentType = (res.headers.get("content-type") || "").toLowerCase();
  const textured = res.headers.get("x-textured") === "1";
  const kind =
    format === "stl" || contentType.includes("stl") ? "stl" : "glb";

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
  };
}

function finalizeWorkerPrompt(prompt) {
  const base = String(prompt || "").trim();
  if (!base) {
    return "3D object, white background, centered, product photo";
  }
  return base;
}
