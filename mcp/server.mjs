/**
 * Buildplate MCP server — stdio transport for Cursor / Claude Desktop / any MCP client.
 *
 * Design: the *agent* (model provider) does the heavy lifting —
 *   - pick mesh vs CAD (rules live in generate tool docs + worker /v1/guide)
 *   - gather reference images OR author OpenSCAD / CadQuery / trimesh CSG
 * then Buildplate compiles / reconstructs locally.
 *
 * Tools: health, save_reference, generate, refine, export_stl, preview
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { mkdir, writeFile, readFile, access, copyFile, readdir, stat } from "node:fs/promises";
import { constants as fsConstants } from "node:fs";
import path from "node:path";
import os from "node:os";
import { createHash, randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import open from "open";
import {
  generateMeshFromWorker,
  refineFromWorker,
  probeWorkerHealth,
  workerConfigured,
  ensureWorker,
  fetchJobPreview,
} from "./worker-client.mjs";

const OUT_DIR = process.env.BUILDPLATE_OUT_DIR?.trim()
  || path.join(os.homedir(), "buildplate", "out");
const REFS_DIR = process.env.BUILDPLATE_REFS_DIR?.trim()
  || path.join(os.homedir(), "buildplate", "refs");
const PREVIEW_URL = (process.env.BUILDPLATE_PREVIEW_URL || "http://127.0.0.1:3920").replace(
  /\/$/,
  "",
);

const AGENT_PLAYBOOK = `Buildplate — YOU (the agent) do the thinking; the worker only compiles/reconstructs.

## Choose backend yourself (baked into generate — no separate tool)

| backend | Use when | You must supply |
|---------|----------|-----------------|
| cad | Mechanical: brackets, boxes, enclosures, plates, mounts, adapters, gears, mm/holes/flats | openscad OR cadquery OR trimesh_code (you author it) |
| mesh | Organic: character, toy, figurine, creature — or anything that should look like a photo | image_path via save_reference (user attach or web photo) |

Neural mesh (TripoSR) is NOT CAD. Hard-edged products from photos become soft blobs → use cad and approximate with solids.

## CAD call
generate({ backend: "cad", prompt, trimesh_code|openscad|cadquery, format: "stl" })
Prefer trimesh_code (always on). Example:
  import trimesh
  box = trimesh.creation.box(extents=[40, 30, 12])
  hole = trimesh.creation.cylinder(radius=3, height=20)
  hole.apply_translation([10, 0, 0])
  result = box.difference(hole)

## Mesh call
save_reference → generate({ backend: "mesh", prompt, image_path, quality: "quality" })
quality=quality uses Hunyuan3D-2mini shape + remesh + view-projected PBR albedo (slower, better).
quality=fast uses TripoSR + remesh + the same albedo bake.
Text-only mesh only with allow_text_only=true (weak).

## After
Preview. Color/material follow-ups: refine({ job_id, prompt }) — keeps the mesh, retints albedo.
  Example: refine({ job_id, prompt: "I want it to be green instead of yellow", color: "green" })
Shape follow-ups (longer ears, extra parts): new generate with a new photo, or edit CAD source and re-generate.
`;

const GENERATE_TOOL_DESCRIPTION = `Local 3D generate. YOU choose mesh vs cad and enrich the request — the worker does not invent good CAD from a short prompt alone.

WHEN TO USE backend=cad (hard edges / printable mechanical):
  brackets, boxes, enclosures, plates, mounts, adapters, gears, lids, trays, anything with mm / holes / screws.
  REQUIRED: author geometry yourself via ONE of openscad | cadquery | trimesh_code.
  Prefer trimesh_code (always available). Example trimesh_code:
    import trimesh
    box = trimesh.creation.box(extents=[40, 30, 12])
    hole = trimesh.creation.cylinder(radius=3, height=20)
    hole.apply_translation([10, 0, 0])
    result = box.difference(hole)

WHEN TO USE backend=mesh (organic / look-alike photo):
  characters, toys, figurines, creatures, sculptures.
  REQUIRED: image_path from save_reference (user attachment or a clean web product photo).
  quality="quality" (default): Hunyuan3D-2mini shape + remesh + PBR albedo from the photo.
  quality="fast": TripoSR + remesh + the same albedo bake. Seconds, softer.
  Text-only mesh is weak — only with allow_text_only=true.

DO NOT use mesh for hard-edged products (e.g. Bambu Lab P1S) expecting CAD-clean results.
If you call generate without enough fields, the response includes a recommendation + example args — follow it and retry.
Set backend explicitly when possible (cad or mesh). format=stl for CAD; mesh may be glb or stl.

FOLLOW-UPS: do not re-generate for color/material tweaks. Call refine({ job_id, prompt, color }) to retint the existing mesh. Shape changes need a new generate.`;

const TRIMESH_HINT = `trimesh_code must assign \`result\` to a trimesh.Trimesh or Scene.
Available: trimesh, np/numpy, math. Use trimesh.creation + .difference/.union/.intersection.`;

function meshHttpPath(absPath) {
  const resolved = path.resolve(absPath);
  const root = path.resolve(OUT_DIR);
  if (resolved === root || resolved.startsWith(root + path.sep)) {
    const rel = path.relative(root, resolved).split(path.sep).join("/");
    return `${PREVIEW_URL}/out/${rel}`;
  }
  return pathToFileURL(resolved).href;
}

async function ensureDirs() {
  await mkdir(OUT_DIR, { recursive: true });
  await mkdir(REFS_DIR, { recursive: true });
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

function extFromMimeOrName(name, mime) {
  const lower = (name || "").toLowerCase();
  if (lower.endsWith(".png") || mime === "image/png") return "png";
  if (lower.endsWith(".webp") || mime === "image/webp") return "webp";
  if (lower.endsWith(".gif") || mime === "image/gif") return "gif";
  return "jpg";
}

async function writeReference({ source_path, image_base64, filename, mime_type }) {
  await ensureDirs();
  const id = randomUUID().slice(0, 8);
  if (source_path) {
    const abs = path.resolve(source_path);
    if (!(await fileExists(abs))) {
      throw new Error(`Image not found: ${abs}`);
    }
    const ext = extFromMimeOrName(abs, mime_type);
    const dest = path.join(REFS_DIR, `${id}.${ext}`);
    await copyFile(abs, dest);
    return dest;
  }
  if (image_base64) {
    let raw = image_base64.trim();
    let mime = mime_type || null;
    if (raw.startsWith("data:") && raw.includes(",")) {
      const [header, data] = raw.split(",", 2);
      const m = /data:([^;]+)/.exec(header);
      if (m) mime = m[1];
      raw = data;
    }
    const ext = extFromMimeOrName(filename, mime);
    const dest = path.join(REFS_DIR, `${id}.${ext}`);
    await writeFile(dest, Buffer.from(raw, "base64"));
    return dest;
  }
  throw new Error("Provide source_path or image_base64");
}

function recommendBackend({ intent, has_image, wants_precise_mm }) {
  const text = String(intent || "").toLowerCase();
  const cadHints = [
    "bracket", "enclosure", "case", "plate", "mount", "adapter", "hinge", "gear",
    "box", "mm", "hole", "screw", "m3", "m4", "flange", "spacer", "washer",
    "rail", "slot", "parametric", "openscad", "cad", "lid", "tray", "stand",
  ];
  const meshHints = [
    "character", "figurine", "toy", "creature", "animal", "person", "organic",
    "statue", "sculpture", "pokemon", "pikachu", "bust", "mascot",
  ];
  const cadScore = cadHints.filter((h) => text.includes(h)).length + (wants_precise_mm ? 3 : 0);
  const meshScore = meshHints.filter((h) => text.includes(h)).length + (has_image ? 1 : 0);

  let backend = "cad";
  let reason = "Defaulting to CAD for hard-edged printable geometry; agent authors the solids.";
  if (meshScore > cadScore) {
    backend = "mesh";
    reason = "Looks organic/character-like — use image→TripoSR mesh.";
  } else if (cadScore > 0) {
    backend = "cad";
    reason = "Mechanical / dimensional cues — agent should write OpenSCAD or trimesh CSG.";
  } else if (has_image) {
    backend = "mesh";
    reason = "Reference image present without strong CAD cues — mesh reconstruction.";
  }

  const example_generate_args =
    backend === "cad"
      ? {
          backend: "cad",
          prompt: intent || "part",
          trimesh_code:
            "import trimesh\n" +
            "box = trimesh.creation.box(extents=[40, 30, 12])\n" +
            "hole = trimesh.creation.cylinder(radius=3, height=20)\n" +
            "hole.apply_translation([10, 0, 0])\n" +
            "result = box.difference(hole)\n",
          format: "stl",
        }
      : {
          backend: "mesh",
          prompt: intent || "subject",
          image_path: "/absolute/path/from/save_reference.png",
        };

  const next =
    backend === "cad"
      ? [
          "Author openscad OR trimesh_code (prefer trimesh_code if OpenSCAD is not installed).",
          "Re-call generate with the example_generate_args shape below (fill in real dimensions).",
          "Preview; edit your source and re-run if needed.",
        ]
      : [
          has_image
            ? "save_reference if needed, then generate({ backend: \"mesh\", image_path, prompt })."
            : "Fetch or ask for a clean reference photo → save_reference → generate(backend=\"mesh\", image_path).",
          "Do not expect CAD-clean edges from mesh mode.",
        ];

  return { backend, reason, next, example_generate_args, trimesh_hint: TRIMESH_HINT };
}

async function finishGenerateJob({ result, prompt, imagePath, open_preview, backend, cadMeta }) {
  await ensureDirs();
  const jobId =
    result.taskId && result.taskId !== "local"
      ? String(result.taskId)
      : randomUUID().slice(0, 8);
  const dir = jobDir(jobId);
  await mkdir(dir, { recursive: true });

  const ext = result.kind === "stl" ? "stl" : "glb";
  const meshPath = path.join(dir, `model.${ext}`);
  await writeFile(meshPath, Buffer.from(result.buffer));

  if (cadMeta?.openscad) {
    await writeFile(path.join(dir, "model.scad"), cadMeta.openscad);
  }
  if (cadMeta?.cadquery) {
    await writeFile(path.join(dir, "model_cq.py"), cadMeta.cadquery);
  }
  if (cadMeta?.trimesh_code) {
    await writeFile(path.join(dir, "model_trimesh.py"), cadMeta.trimesh_code);
  }

  const meta = {
    jobId,
    prompt: result.prompt || prompt,
    kind: result.kind,
    textured: result.textured,
    meshPath,
    imagePath: imagePath || null,
    backend: result.backend || backend,
    engine: result.engine || null,
    parentJobId: result.parentJobId || null,
    mode:
      backend === "cad"
        ? "cad"
        : backend === "refine"
          ? "refine"
          : imagePath
            ? "image_to_3d"
            : "text_to_3d",
    bytes: result.buffer.byteLength,
    sha256: createHash("sha256").update(Buffer.from(result.buffer)).digest("hex"),
    createdAt: new Date().toISOString(),
  };
  await writeFile(path.join(dir, "meta.json"), JSON.stringify(meta, null, 2));

  let previewPng = null;
  const remotePreview = await fetchJobPreview(jobId);
  if (remotePreview) {
    await writeFile(path.join(dir, "preview.png"), remotePreview);
    previewPng = remotePreview;
  }

  let previewUrl = null;
  if (open_preview !== false) {
    previewUrl = previewPageUrl(meshPath);
    try {
      await open(previewUrl);
    } catch {
      // ignore
    }
  }

  const lines = [
    backend === "refine"
      ? `Refined ${result.kind.toUpperCase()} (${meta.bytes} bytes) — appearance only, geometry unchanged`
      : `Generated ${result.kind.toUpperCase()} (${meta.bytes} bytes)`,
    `backend: ${meta.backend}${meta.engine ? ` / engine=${meta.engine}` : ""}`,
    `mode: ${meta.mode}`,
    `jobId: ${jobId}`,
    meta.parentJobId ? `parent: ${meta.parentJobId}` : null,
    `path: ${meshPath}`,
    imagePath ? `reference: ${imagePath}` : null,
    previewUrl ? `preview: ${previewUrl}` : null,
  ].filter(Boolean);

  const content = [{ type: "text", text: lines.join("\n") }];
  if (previewPng) {
    content.push({
      type: "image",
      data: previewPng.toString("base64"),
      mimeType: "image/png",
    });
  }
  return { content };
}

async function latestOutJobId() {
  let names;
  try {
    names = await readdir(OUT_DIR);
  } catch {
    return null;
  }
  let best = null;
  let bestM = 0;
  for (const name of names) {
    const dir = path.join(OUT_DIR, name);
    const glb = path.join(dir, "model.glb");
    const stl = path.join(dir, "model.stl");
    if (!(await fileExists(glb)) && !(await fileExists(stl))) continue;
    try {
      const s = await stat(dir);
      if (s.mtimeMs > bestM) {
        bestM = s.mtimeMs;
        best = name;
      }
    } catch {
      // skip
    }
  }
  return best;
}

const server = new McpServer({
  name: "buildplate",
  version: "0.5.0",
});

server.registerTool(
  "health",
  {
    description:
      "Check Buildplate worker: mesh_ready (TripoSR), cad_ready / cad_engines, device. Also returns the mesh-vs-cad playbook.",
  },
  async () => {
    const worker = await ensureWorker();
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(
            {
              mcp: "ok",
              outDir: OUT_DIR,
              refsDir: REFS_DIR,
              previewUrl: PREVIEW_URL,
              workerConfigured: workerConfigured(),
              worker,
              tip: "generate for a new object. refine({job_id, prompt}) for color/material follow-ups on the last mesh. Incomplete generate returns a retry recipe.",
              playbook: AGENT_PLAYBOOK,
            },
            null,
            2,
          ),
        },
      ],
    };
  },
);

server.registerTool(
  "save_reference",
  {
    description:
      "Save a reference photo into ~/buildplate/refs/ for mesh generate(image_path=...). Use for user attachments or web downloads before backend=mesh.",
    inputSchema: {
      source_path: z
        .string()
        .optional()
        .describe("Absolute path to an existing image on disk"),
      image_base64: z
        .string()
        .optional()
        .describe("Raw base64 or data-URL if you have bytes instead of a path"),
      filename: z.string().optional().describe("Optional original filename hint"),
      mime_type: z.string().optional().describe("Optional MIME type, e.g. image/png"),
      label: z.string().optional().describe("Short label for logs"),
    },
  },
  async ({ source_path, image_base64, filename, mime_type, label }) => {
    try {
      if (!source_path && !image_base64) {
        return {
          content: [{ type: "text", text: "Provide source_path (preferred) or image_base64." }],
          isError: true,
        };
      }
      const dest = await writeReference({
        source_path,
        image_base64,
        filename: filename || (label ? `${label}.png` : undefined),
        mime_type,
      });
      return {
        content: [
          {
            type: "text",
            text:
              `Reference saved.\n` +
              `path: ${dest}\n` +
              `Next: generate({ backend: "mesh", image_path: "${dest}", prompt: "..." }).`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: err instanceof Error ? err.message : String(err) }],
        isError: true,
      };
    }
  },
);

server.registerTool(
  "generate",
  {
    description: GENERATE_TOOL_DESCRIPTION,
    inputSchema: {
      prompt: z.string().min(1).describe("Short label / intent for the job"),
      backend: z
        .enum(["auto", "mesh", "cad"])
        .optional()
        .describe("mesh=TripoSR, cad=compile agent source, auto=infer (prefer setting explicitly)"),
      engine: z
        .enum(["auto", "openscad", "cadquery", "trimesh"])
        .optional()
        .describe("CAD engine override (default auto)"),
      openscad: z.string().optional().describe("Full OpenSCAD source (agent-authored)"),
      cadquery: z
        .string()
        .optional()
        .describe("CadQuery Python that assigns result= (agent-authored)"),
      trimesh_code: z
        .string()
        .optional()
        .describe("trimesh CSG Python that assigns result=Trimesh (agent-authored; always-on)"),
      image_path: z.string().optional().describe("Absolute path to reference image for mesh"),
      image_base64: z.string().optional().describe("Inline image for mesh if no path"),
      allow_text_only: z
        .boolean()
        .optional()
        .describe("Allow mesh without image (weak). Ignored for cad."),
      format: z.enum(["glb", "stl"]).optional().describe("Mesh output format (CAD always STL)"),
      texture: z
        .boolean()
        .optional()
        .describe("Bake reference photo as PBR albedo (default true). STL has no color — textured jobs return GLB."),
      quality: z
        .enum(["fast", "quality"])
        .optional()
        .describe("fast=TripoSR, quality=Hunyuan shape+remesh (default quality when Hunyuan is installed)"),
      vendor: z
        .enum(["triposr", "hunyuan"])
        .optional()
        .describe("Explicit mesh vendor; overrides quality"),
      remesh: z.boolean().optional().describe("CPU remesh/decimate after shape (default true)"),
      target_faces: z.number().optional().describe("Remesh target face count (default 40000)"),
      open_preview: z.boolean().optional().describe("Open localhost preview (default true)"),
    },
  },
  async ({
    prompt,
    backend,
    engine,
    openscad,
    cadquery,
    trimesh_code,
    image_path,
    image_base64,
    allow_text_only,
    format,
    texture,
    quality,
    vendor,
    remesh,
    target_faces,
    open_preview,
  }) => {
    let resolvedImagePath = image_path ? path.resolve(image_path) : null;
    const hasCad =
      Boolean((openscad || "").trim()) ||
      Boolean((cadquery || "").trim()) ||
      Boolean((trimesh_code || "").trim());

    let mode = backend || "auto";
    if (mode === "auto") {
      if (hasCad) mode = "cad";
      else if (resolvedImagePath || image_base64) mode = "mesh";
      else {
        const rec = recommendBackend({ intent: prompt, has_image: false });
        // Incomplete auto call — return guidance instead of guessing badly
        return {
          content: [
            {
              type: "text",
              text:
                "Incomplete generate call — enrich and retry.\n\n" +
                JSON.stringify(rec, null, 2) +
                "\n\n" +
                AGENT_PLAYBOOK,
            },
          ],
          isError: true,
        };
      }
    }

    try {
      if (mode === "cad") {
        if (!hasCad) {
          const rec = recommendBackend({
            intent: prompt,
            has_image: Boolean(resolvedImagePath || image_base64),
            wants_precise_mm: true,
          });
          return {
            content: [
              {
                type: "text",
                text:
                  "backend=cad needs agent-authored source. Enrich and retry.\n\n" +
                  JSON.stringify(rec, null, 2) +
                  "\n\n" +
                  TRIMESH_HINT,
              },
            ],
            isError: true,
          };
        }

        const result = await generateMeshFromWorker({
          prompt,
          backend: "cad",
          engine: engine || "auto",
          openscad: openscad || null,
          cadquery: cadquery || null,
          trimesh_code: trimesh_code || null,
          format: "stl",
          texture: false,
          waitForMesh: false,
        });

        return await finishGenerateJob({
          result,
          prompt,
          imagePath: null,
          open_preview,
          backend: "cad",
          cadMeta: { openscad, cadquery, trimesh_code },
        });
      }

      // mesh
      if (image_base64 && !resolvedImagePath) {
        resolvedImagePath = await writeReference({
          image_base64,
          filename: "inline-ref.png",
        });
      }

      if (resolvedImagePath && !(await fileExists(resolvedImagePath))) {
        return {
          content: [{ type: "text", text: `Image not found: ${resolvedImagePath}` }],
          isError: true,
        };
      }

      if (!resolvedImagePath && allow_text_only !== true) {
        const rec = recommendBackend({ intent: prompt, has_image: false });
        return {
          content: [
            {
              type: "text",
              text:
                "Refusing text-only mesh. Enrich and retry.\n\n" +
                JSON.stringify(rec, null, 2) +
                "\n\n" +
                AGENT_PLAYBOOK +
                "\nFor mechanical parts: backend=cad + trimesh_code/openscad.\n" +
                "For organic: save_reference + image_path.\n" +
                "To force weak text→mesh: allow_text_only=true.",
            },
          ],
          isError: true,
        };
      }

      let imageB64 = null;
      if (resolvedImagePath) {
        imageB64 = (await readFile(resolvedImagePath)).toString("base64");
      }

      const result = await generateMeshFromWorker({
        prompt,
        image: imageB64,
        format: format || "glb",
        texture: texture !== false,
        backend: "mesh",
        quality: quality || "quality",
        vendor: vendor || null,
        remesh: remesh !== false,
        target_faces: target_faces || null,
        waitForMesh: true,
      });

      return await finishGenerateJob({
        result,
        prompt,
        imagePath: resolvedImagePath,
        open_preview,
        backend: "mesh",
      });
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
  "refine",
  {
    description:
      "Update an existing mesh job with a follow-up prompt. Keeps geometry; retints albedo (e.g. 'make it green instead of yellow'). Pass the job_id from generate. For shape changes (longer ears, extra parts) call generate instead. CAD: edit trimesh_code/openscad and re-generate.",
    inputSchema: {
      prompt: z
        .string()
        .min(1)
        .describe("Follow-up, e.g. 'I want it to be green instead of yellow'"),
      job_id: z
        .string()
        .optional()
        .describe("Job id from generate. Omit to use the most recent job."),
      color: z
        .string()
        .optional()
        .describe("Target color name or #hex if you extracted it (green, #22aa44)"),
      keep_mesh: z
        .boolean()
        .optional()
        .describe("Must stay true. Shape edits are a new generate."),
      open_preview: z.boolean().optional().describe("Open localhost preview (default true)"),
    },
  },
  async ({ prompt, job_id, color, keep_mesh, open_preview }) => {
    if (keep_mesh === false) {
      return {
        content: [
          {
            type: "text",
            text:
              "refine keeps the mesh. For shape changes, call generate with a new photo (mesh) or edited CAD source.",
          },
        ],
        isError: true,
      };
    }
    try {
      const jobId = job_id || (await latestOutJobId());
      if (!jobId) {
        return {
          content: [
            {
              type: "text",
              text: "No previous job to refine. generate something first, then refine({ job_id, prompt }).",
            },
          ],
          isError: true,
        };
      }
      const result = await refineFromWorker({
        jobId,
        prompt,
        color: color || null,
      });
      return await finishGenerateJob({
        result,
        prompt,
        imagePath: null,
        open_preview,
        backend: "refine",
      });
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
    description: "Return the STL path for a job (or tell you how to export from a GLB).",
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
            text: "Provide a valid job_id or path to a .glb or .stl file.",
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
  await ensureDirs();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
