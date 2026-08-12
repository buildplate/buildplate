/**
 * Thin client for the local Buildplate GPU worker (Hunyuan FastAPI).
 * Same contract as Shapeful: GET /health, POST /v1/generate → binary mesh.
 */

function logWorker(level, msg, extra) {
  // MCP stdio: never write to stdout. stderr is fine for diagnostics.
  const line = `[buildplate-worker] ${msg}`;
  if (level === "error") console.error(line, extra ?? "");
  else console.error(line, extra ?? "");
}

export function workerConfigured() {
  return Boolean(
    process.env.BUILDPLATE_WORKER_URL?.trim() &&
      process.env.BUILDPLATE_WORKER_SECRET?.trim(),
  );
}

export function missingWorkerError() {
  return (
    "Mesh generation needs BUILDPLATE_WORKER_URL and BUILDPLATE_WORKER_SECRET " +
    "(local GPU worker). See worker/README.md."
  );
}

export async function probeWorkerHealth() {
  const base = process.env.BUILDPLATE_WORKER_URL?.trim()?.replace(/\/$/, "");
  if (!base) {
    return { online: false, ready: false, detail: "BUILDPLATE_WORKER_URL unset" };
  }
  const timeoutMs = Number(process.env.BUILDPLATE_WORKER_HEALTH_TIMEOUT_MS || 4000);
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${base}/health`, { signal: ctrl.signal });
    if (!res.ok) {
      return { online: false, ready: false, detail: `HTTP ${res.status}` };
    }
    const data = await res.json().catch(() => ({}));
    return {
      online: true,
      ready: Boolean(data.ready),
      busy: Boolean(data.busy),
      model: data.model ?? null,
      device: data.device ?? null,
      texgen: Boolean(data.texgen),
      detail: data.last_error ?? null,
    };
  } catch (err) {
    const detail = err?.name === "AbortError" ? "timeout" : err?.message || "unreachable";
    return { online: false, ready: false, detail };
  } finally {
    clearTimeout(t);
  }
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
  const base = process.env.BUILDPLATE_WORKER_URL?.trim()?.replace(/\/$/, "");
  const secret = process.env.BUILDPLATE_WORKER_SECRET?.trim();
  if (!base || !secret) throw new Error(missingWorkerError());

  const enriched = finalizeWorkerPrompt(prompt);
  const imagePayload =
    typeof image === "string" && image.trim() ? image.trim() : null;

  const timeoutMs = Number(process.env.BUILDPLATE_WORKER_TIMEOUT_MS || 600_000);
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);

  const body = {
    prompt: enriched,
    type: format,
    texture: Boolean(texture),
    octree_resolution: Number(process.env.BUILDPLATE_WORKER_OCTREE || 128),
    num_inference_steps: Number(process.env.BUILDPLATE_WORKER_STEPS || 5),
  };
  if (imagePayload) body.image = imagePayload;

  let res;
  try {
    res = await fetch(`${base}/v1/generate`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Worker-Secret": secret,
      },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error("GPU worker timed out — is the worker running?");
    }
    throw new Error(`GPU worker unreachable: ${err?.message || err}`);
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
    if (res.status === 429) throw new Error("GPU worker busy — one job at a time");
    if (res.status === 503) throw new Error("GPU worker not ready (models still loading)");
    throw new Error(`GPU worker failed: ${detail}`);
  }

  const contentType = (res.headers.get("content-type") || "").toLowerCase();
  const textured = res.headers.get("x-textured") === "1";
  const kind =
    format === "stl" || contentType.includes("stl") ? "stl" : "glb";

  const buffer = await res.arrayBuffer();
  if (!buffer || buffer.byteLength < 84) {
    throw new Error("GPU worker returned an empty/invalid mesh");
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
    return "3D object, white background, centered, multi-color textured 3D asset";
  }
  const lower = base.toLowerCase();
  if (lower.includes("white background") || lower.includes("textured")) {
    return base;
  }
  return `${base}, white background, centered, multi-color textured 3D asset`;
}
