import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import type { IncomingMessage, ServerResponse } from "node:http";
import type { Plugin } from "vite";

export type SlicerId = "bambu" | "orca" | "prusa" | "cura" | "creality";

type Slicer = {
  id: SlicerId;
  name: string;
  macApps: string[];
  winExes: string[];
  linuxBins: string[];
};

const SLICERS: Slicer[] = [
  {
    id: "bambu",
    name: "Bambu Studio",
    macApps: ["BambuStudio", "Bambu Studio"],
    winExes: [
      path.join(process.env.LOCALAPPDATA || "", "Programs", "BambuStudio", "bambu-studio.exe"),
      path.join(process.env.ProgramFiles || "C:\\Program Files", "Bambu Studio", "bambu-studio.exe"),
    ],
    linuxBins: ["bambu-studio", "bambu-studio-bin"],
  },
  {
    id: "orca",
    name: "OrcaSlicer",
    macApps: ["OrcaSlicer", "Orca Slicer"],
    winExes: [
      path.join(process.env.LOCALAPPDATA || "", "Programs", "OrcaSlicer", "orca-slicer.exe"),
    ],
    linuxBins: ["orca-slicer"],
  },
  {
    id: "prusa",
    name: "PrusaSlicer",
    macApps: ["PrusaSlicer", "PrusaSlicer.app"],
    winExes: [
      path.join(process.env.LOCALAPPDATA || "", "Programs", "PrusaSlicer", "prusa-slicer.exe"),
      path.join(process.env.ProgramFiles || "C:\\Program Files", "PrusaSlicer", "prusa-slicer.exe"),
    ],
    linuxBins: ["prusa-slicer"],
  },
  {
    id: "cura",
    name: "UltiMaker Cura",
    macApps: ["UltiMaker Cura", "Cura"],
    winExes: [
      path.join(process.env.ProgramFiles || "C:\\Program Files", "UltiMaker Cura 5.8", "UltiMaker-Cura.exe"),
      path.join(process.env.LOCALAPPDATA || "", "Programs", "UltiMaker Cura", "UltiMaker-Cura.exe"),
    ],
    linuxBins: ["cura", "UltiMaker-Cura"],
  },
  {
    id: "creality",
    name: "Creality Print",
    macApps: ["Creality Print", "CrealityPrint"],
    winExes: [
      path.join(process.env.ProgramFiles || "C:\\Program Files", "Creality Print", "Creality Print.exe"),
    ],
    linuxBins: ["creality-print"],
  },
];

const OUT_DIR =
  process.env.BUILDPLATE_OUT_DIR?.trim() ||
  path.join(os.homedir(), "buildplate", "out");

export function slicerApi(): Plugin {
  return {
    name: "buildplate-slicers",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url?.split("?")[0] || "";
        if (req.method === "GET" && url === "/slicers") {
          json(res, 200, { slicers: listSlicers() });
          return;
        }
        if (req.method === "POST" && url === "/slicers/open") {
          void handleOpen(req, res);
          return;
        }
        next();
      });
    },
  };
}

function listSlicers() {
  return SLICERS.map((s) => ({
    id: s.id,
    name: s.name,
    installed: Boolean(resolveSlicer(s)),
  }));
}

function resolveSlicer(slicer: Slicer): { kind: "mac" | "win" | "linux"; target: string } | null {
  if (process.platform === "darwin") {
    for (const name of slicer.macApps) {
      const app = name.endsWith(".app") ? name : `${name}.app`;
      for (const root of ["/Applications", path.join(os.homedir(), "Applications")]) {
        if (fs.existsSync(path.join(root, app))) {
          return { kind: "mac", target: name.replace(/\.app$/i, "") };
        }
      }
    }
    return null;
  }
  if (process.platform === "win32") {
    for (const exe of slicer.winExes) {
      if (exe && fs.existsSync(exe)) return { kind: "win", target: exe };
    }
    return null;
  }
  for (const bin of slicer.linuxBins) {
    const found = which(bin);
    if (found) return { kind: "linux", target: found };
  }
  return null;
}

function which(bin: string): string | null {
  const pathEnv = process.env.PATH || "";
  for (const dir of pathEnv.split(path.delimiter)) {
    const candidate = path.join(dir, bin);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

async function handleOpen(req: IncomingMessage, res: ServerResponse) {
  try {
    const u = new URL(req.url || "", "http://127.0.0.1");
    const slicerId = (u.searchParams.get("slicer") || "bambu") as SlicerId;
    const src = u.searchParams.get("src") || "";
    const slicer = SLICERS.find((s) => s.id === slicerId);
    if (!slicer) {
      json(res, 400, { error: `Unknown slicer ${slicerId}` });
      return;
    }
    const resolved = resolveSlicer(slicer);
    if (!resolved) {
      json(res, 404, { error: `${slicer.name} is not installed` });
      return;
    }
    const body = await readBody(req, 80 * 1024 * 1024);
    if (!body || body.byteLength < 84) {
      json(res, 400, { error: "STL body missing" });
      return;
    }
    const dest = stlDest(src);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, body);
    launch(resolved, dest);
    json(res, 200, { ok: true, slicer: slicer.id, path: dest });
  } catch (err) {
    json(res, 500, { error: err instanceof Error ? err.message : String(err) });
  }
}

function stlDest(src: string): string {
  try {
    const u = new URL(src, "http://buildplate.localhost");
    const parts = u.pathname.split("/").filter(Boolean);
    const outIdx = parts.indexOf("out");
    if (outIdx >= 0 && parts[outIdx + 1]) {
      const job = parts[outIdx + 1].replace(/[^a-zA-Z0-9._-]/g, "");
      if (job) return path.join(OUT_DIR, job, "preview.stl");
    }
  } catch {
    // fall through
  }
  return path.join(os.tmpdir(), "buildplate-preview.stl");
}

function launch(
  resolved: { kind: "mac" | "win" | "linux"; target: string },
  file: string,
) {
  if (resolved.kind === "mac") {
    spawn("open", ["-a", resolved.target, file], { detached: true, stdio: "ignore" }).unref();
    return;
  }
  if (resolved.kind === "win") {
    spawn(resolved.target, [file], { detached: true, stdio: "ignore", windowsHide: true }).unref();
    return;
  }
  spawn(resolved.target, [file], { detached: true, stdio: "ignore" }).unref();
}

function json(res: ServerResponse, status: number, body: unknown) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(body));
}

function readBody(req: IncomingMessage, max: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let n = 0;
    req.on("data", (c: Buffer) => {
      n += c.length;
      if (n > max) {
        reject(new Error("STL too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}
