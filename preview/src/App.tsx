import { useCallback, useEffect, useMemo, useState } from "react";
import type { Object3D } from "three";
import { MeshViewer } from "./MeshViewer";
import { exportObjectToStl, downloadBlob } from "./exportStl";
import { fetchSlicers, openInSlicer, type SlicerInfo } from "./slicers";

type GeneratedKind = "cad" | "mesh";

function srcFromQuery(): string | null {
  const q = new URLSearchParams(window.location.search);
  return q.get("src");
}

function kindFromQuery(): GeneratedKind | null {
  const k = new URLSearchParams(window.location.search).get("kind");
  if (k === "cad" || k === "mesh") return k;
  return null;
}

export function App() {
  const src = useMemo(() => srcFromQuery(), []);
  const queryKind = useMemo(() => kindFromQuery(), []);
  const [root, setRoot] = useState<Object3D | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [slicers, setSlicers] = useState<SlicerInfo[]>([]);
  const [kind, setKind] = useState<GeneratedKind | null>(queryKind);

  useEffect(() => {
    void fetchSlicers().then(setSlicers);
  }, []);

  useEffect(() => {
    if (queryKind) {
      setKind(queryKind);
      return;
    }
    let cancelled = false;
    void detectKind(src).then((next) => {
      if (!cancelled) setKind(next);
    });
    return () => {
      cancelled = true;
    };
  }, [src, queryKind]);

  const onReady = useCallback((obj: Object3D | null, err?: string) => {
    setRoot(obj);
    setError(err ?? null);
  }, []);

  const stlBlob = useCallback(() => {
    if (!root) throw new Error("No mesh loaded");
    return exportObjectToStl(root);
  }, [root]);

  const onExport = useCallback(() => {
    if (!root) return;
    setBusy(true);
    try {
      const blob = stlBlob();
      const name = guessName(src) || "buildplate-model.stl";
      downloadBlob(blob, name);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setBusy(false);
    }
  }, [root, src, stlBlob]);

  const onOpenSlicer = useCallback(
    async (id: string) => {
      if (!root || !src) return;
      setBusy(true);
      setError(null);
      try {
        await openInSlicer(id, src, stlBlob());
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not open slicer");
      } finally {
        setBusy(false);
      }
    },
    [root, src, stlBlob],
  );

  const bambu = slicers.find((s) => s.id === "bambu");
  const others = slicers.filter((s) => s.id !== "bambu");
  const installedOthers = others.filter((s) => s.installed);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-title">
            <strong>Buildplate</strong>
            {kind && (
              <span
                className={`kind-tag ${kind}`}
                title={
                  kind === "cad"
                    ? "Compiled CAD — exact solids you (or the agent) authored"
                    : "Neural mesh — reconstructed from a photo or text still"
                }
              >
                {kind === "cad" ? "CAD" : "MESH"}
              </span>
            )}
          </div>
          <span className="brand-path">{src ? shortSrc(src) : "no mesh loaded"}</span>
        </div>
        <div className="actions">
          <button type="button" disabled={!root || busy} onClick={onExport}>
            Export STL
          </button>
          <button
            type="button"
            className="primary"
            disabled={!root || busy || !bambu?.installed}
            title={bambu?.installed ? "Open this mesh in Bambu Studio" : "Bambu Studio is not installed"}
            onClick={() => void onOpenSlicer("bambu")}
          >
            {busy ? "Opening…" : "Open in Bambu"}
          </button>
          {installedOthers.length > 0 && (
            <select
              className="slicer-select"
              disabled={!root || busy}
              defaultValue=""
              onChange={(e) => {
                const id = e.target.value;
                e.target.value = "";
                if (id) void onOpenSlicer(id);
              }}
            >
              <option value="" disabled>
                More slicers
              </option>
              {installedOthers.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          )}
        </div>
      </header>
      <main className="stage">
        {!src && (
          <div className="empty">
            <div>Pass a mesh via query string</div>
            <code>?src=file:///path/to/model.glb</code>
          </div>
        )}
        {src && error && <div className="error">{error}</div>}
        {src && <MeshViewer src={src} onReady={onReady} />}
      </main>
    </div>
  );
}

function siblingUrl(src: string, name: string): string | null {
  try {
    const u = new URL(src, window.location.href);
    const parts = u.pathname.split("/");
    parts[parts.length - 1] = name;
    u.pathname = parts.join("/");
    u.search = "";
    u.hash = "";
    return u.toString();
  } catch {
    return null;
  }
}

async function probe(url: string): Promise<boolean> {
  try {
    const res = await fetch(url, { method: "HEAD", cache: "no-store" });
    return res.ok;
  } catch {
    return false;
  }
}

function kindFromMeta(data: unknown): GeneratedKind | null {
  if (!data || typeof data !== "object") return null;
  const m = data as Record<string, unknown>;
  const generated = String(m.generated ?? "").toLowerCase();
  if (generated === "cad" || generated === "mesh") return generated;
  const mode = String(m.mode ?? "").toLowerCase();
  const backend = String(m.backend ?? "").toLowerCase();
  if (mode === "cad" || backend === "cad") return "cad";
  if (
    mode === "image_to_3d" ||
    mode === "text_to_3d" ||
    mode === "refine" ||
    backend === "hunyuan" ||
    backend === "triposr" ||
    backend === "mesh" ||
    backend === "refine"
  ) {
    return "mesh";
  }
  return null;
}

async function detectKind(src: string | null): Promise<GeneratedKind | null> {
  if (!src) return null;
  const metaUrl = siblingUrl(src, "meta.json");
  if (metaUrl) {
    try {
      const res = await fetch(metaUrl, { cache: "no-store" });
      if (res.ok) {
        const fromMeta = kindFromMeta(await res.json());
        if (fromMeta) return fromMeta;
      }
    } catch {
      // fall through to sibling probes
    }
  }
  for (const name of ["model_trimesh.py", "model.scad", "model_cq.py"]) {
    const url = siblingUrl(src, name);
    if (url && (await probe(url))) return "cad";
  }
  try {
    const path = new URL(src, window.location.href).pathname.toLowerCase();
    if (path.endsWith(".glb") || path.endsWith(".gltf")) return "mesh";
  } catch {
    // ignore
  }
  for (const name of ["albedo.png", "composited.png"]) {
    const url = siblingUrl(src, name);
    if (url && (await probe(url))) return "mesh";
  }
  return null;
}

function shortSrc(src: string): string {
  try {
    const u = new URL(src);
    const parts = u.pathname.split("/").filter(Boolean);
    return parts.slice(-2).join("/") || src;
  } catch {
    return src.slice(-48);
  }
}

function guessName(src: string | null): string {
  if (!src) return "buildplate-model.stl";
  try {
    const u = new URL(src);
    const base = u.pathname.split("/").pop() || "model";
    return base.replace(/\.(glb|gltf|stl)$/i, "") + ".stl";
  } catch {
    return "buildplate-model.stl";
  }
}
