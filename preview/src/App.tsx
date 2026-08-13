import { useCallback, useEffect, useMemo, useState } from "react";
import type { Object3D } from "three";
import { MeshViewer } from "./MeshViewer";
import { exportObjectToStl, downloadBlob } from "./exportStl";
import { fetchSlicers, openInSlicer, type SlicerInfo } from "./slicers";

function srcFromQuery(): string | null {
  const q = new URLSearchParams(window.location.search);
  return q.get("src");
}

export function App() {
  const src = useMemo(() => srcFromQuery(), []);
  const [root, setRoot] = useState<Object3D | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [slicers, setSlicers] = useState<SlicerInfo[]>([]);

  useEffect(() => {
    void fetchSlicers().then(setSlicers);
  }, []);

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
          <strong>Buildplate</strong>
          <span>{src ? shortSrc(src) : "no mesh loaded"}</span>
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
