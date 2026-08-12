/**
 * Buildplate MCP server — stdio transport for Cursor / Claude Desktop / any MCP client.
 *
 * Tools: health, generate, export_stl, preview
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { mkdir, writeFile, readFile, access } from "node:fs/promises";
import { constants as fsConstants } from "node:fs";
import path from "node:path";
import os from "node:os";
import { createHash, randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import open from "open";
import {
  generateMeshFromWorker,
  probeWorkerHealth,
  workerConfigured,
  ensureWorker,
} from "./worker-client.mjs";

const OUT_DIR = process.env.BUILDPLATE_OUT_DIR?.trim()
  || path.join(os.homedir(), "buildplate", "out");
const PREVIEW_URL = (process.env.BUILDPLATE_PREVIEW_URL || "http://127.0.0.1:3920").replace(
  /\/$/,
  "",
);

/** Prefer /out/... URLs served by the preview Vite middleware (no file://). */
function meshHttpPath(absPath) {
  const resolved = path.resolve(absPath);
  const root = path.resolve(OUT_DIR);
  if (resolved === root || resolved.startsWith(root + path.sep)) {
    const rel = path.relative(root, resolved).split(path.sep).join("/");
    return `${PREVIEW_URL}/out/${rel}`;
  }
  return pathToFileURL(resolved).href;
}

async function ensureOutDir() {
  await mkdir(OUT_DIR, { recursive: true });
}

async function fileExists(p) {
  try {
    await access(p, fsConstants.F_OK);
    return true;
  } catch {
    return false;
  }
}

function jobDir(jobId) {
  return path.join(OUT_DIR, jobId);
}

async function resolveJobPaths(jobId) {
  const dir = jobDir(jobId);
  const glb = path.join(dir, "model.glb");
  const stl = path.join(dir, "model.stl");
  return {
    dir,
    glbPath: (await fileExists(glb)) ? glb : null,
    stlPath: (await fileExists(stl)) ? stl : null,
  };
}

function previewPageUrl(filePath) {
  const q = new URLSearchParams({
    src: meshHttpPath(filePath),
  });
  return `${PREVIEW_URL}/?${q.toString()}`;
}

const server = new McpServer({
  name: "buildplate",
  version: "0.1.0",
});

server.registerTool(
  "health",
  {
    description: "Check Buildplate + local GPU worker status (ready / busy / device).",
  },
  async () => {
    const worker = await ensureWorker();

    const payload = {
      mcp: "ok",
      outDir: OUT_DIR,
      previewUrl: PREVIEW_URL,
      workerConfigured: workerConfigured(),
      worker,
    };

    return {
      content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    };
  },
);

server.registerTool(
  "generate",
  {
    description:
      "Generate a 3D mesh from a text prompt (and optional reference image path). Writes GLB/STL under ~/buildplate/out/<jobId>/.",
    inputSchema: {
      prompt: z.string().min(1).describe("What to generate"),
      image_path: z
        .string()
        .optional()
        .describe("Optional local image path for image→3D"),
      format: z
        .enum(["glb", "stl"])
        .optional()
        .describe("Preferred mesh format (glb keeps textures when available)"),
      texture: z
        .boolean()
        .optional()
        .describe("Request textured GLB when the worker supports paint"),
      open_preview: z
        .boolean()
        .optional()
        .describe("Open the localhost preview viewer when done (default true)"),
    },
  },
  async ({ prompt, image_path, format, texture, open_preview }) => {
    // Local worker is auto-started; no remote URL / Funnel config.
    let imageB64 = null;
    if (image_path) {
      const abs = path.resolve(image_path);
      if (!(await fileExists(abs))) {
        return {
          content: [{ type: "text", text: `Image not found: ${abs}` }],
          isError: true,
        };
      }
      const buf = await readFile(abs);
      imageB64 = buf.toString("base64");
    }

    try {
      const result = await generateMeshFromWorker({
        prompt,
        image: imageB64,
        format: format || "glb",
        texture: texture !== false,
      });

      await ensureOutDir();
      const jobId =
        result.taskId && result.taskId !== "local"
          ? String(result.taskId)
          : randomUUID().slice(0, 8);
      const dir = jobDir(jobId);
      await mkdir(dir, { recursive: true });

      const ext = result.kind === "stl" ? "stl" : "glb";
      const meshPath = path.join(dir, `model.${ext}`);
      await writeFile(meshPath, Buffer.from(result.buffer));

      const meta = {
        jobId,
        prompt: result.prompt,
        kind: result.kind,
        textured: result.textured,
        meshPath,
        bytes: result.buffer.byteLength,
        sha256: createHash("sha256").update(Buffer.from(result.buffer)).digest("hex"),
        createdAt: new Date().toISOString(),
      };
      await writeFile(path.join(dir, "meta.json"), JSON.stringify(meta, null, 2));

      let previewUrl = null;
      if (open_preview !== false) {
        previewUrl = previewPageUrl(meshPath);
        try {
          await open(previewUrl);
        } catch {
          // Viewer may not be running; still return the URL.
        }
      }

      const lines = [
        `Generated ${result.kind.toUpperCase()} (${meta.bytes} bytes)`,
        `jobId: ${jobId}`,
        `path: ${meshPath}`,
        previewUrl ? `preview: ${previewUrl}` : null,
        `Call export_stl with job_id "${jobId}" for a printable STL.`,
      ].filter(Boolean);

      return { content: [{ type: "text", text: lines.join("\n") }] };
    } catch (err) {
      return {
        content: [
          {
            type: "text",
            text: err instanceof Error ? err.message : String(err),
          },
        ],
        isError: true,
      };
    }
  },
);

server.registerTool(
  "export_stl",
  {
    description:
      "Return / ensure an STL for a job. If only GLB exists, open preview Export STL or re-run generate with format=stl.",
    inputSchema: {
      job_id: z.string().min(1).describe("Job id from generate"),
    },
  },
  async ({ job_id }) => {
    const { glbPath, stlPath, dir } = await resolveJobPaths(job_id);
    if (!glbPath && !stlPath) {
      return {
        content: [
          {
            type: "text",
            text: `Unknown job_id "${job_id}". Expected files under ${dir}`,
          },
        ],
        isError: true,
      };
    }

    if (stlPath) {
      return {
        content: [
          {
            type: "text",
            text: `STL ready:\n${stlPath}\nfile: ${pathToFileURL(stlPath).href}`,
          },
        ],
      };
    }

    const url = previewPageUrl(glbPath);
    return {
      content: [
        {
          type: "text",
          text:
            `No STL on disk for ${job_id} (have GLB at ${glbPath}).\n` +
            `Open preview and click Export STL:\n${url}\n` +
            `Or re-run generate with format="stl".`,
        },
      ],
    };
  },
);

server.registerTool(
  "preview",
  {
    description: "Open the read-only localhost 3D viewer for a job or absolute mesh path.",
    inputSchema: {
      job_id: z.string().optional().describe("Job id from generate"),
      path: z.string().optional().describe("Absolute path to .glb or .stl"),
    },
  },
  async ({ job_id, path: meshPath }) => {
    let file = meshPath ? path.resolve(meshPath) : null;
    if (!file && job_id) {
      const { glbPath, stlPath } = await resolveJobPaths(job_id);
      file = glbPath || stlPath;
    }
    if (!file || !(await fileExists(file))) {
      return {
        content: [
          {
            type: "text",
            text: "Provide a valid job_id or path to a .glb / .stl file.",
          },
        ],
        isError: true,
      };
    }

    const url = previewPageUrl(file);
    try {
      await open(url);
    } catch {
      // ignore
    }

    return {
      content: [
        {
          type: "text",
          text: `Preview:\n${url}\nmesh: ${file}`,
        },
      ],
    };
  },
);

async function main() {
  await ensureOutDir();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
