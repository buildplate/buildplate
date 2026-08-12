import { useCallback, useMemo, useState } from "react";
import type { Object3D } from "three";
import { MeshViewer } from "./MeshViewer";
import { exportObjectToStl, downloadBlob } from "./exportStl";

function srcFromQuery(): string | null {
  const q = new URLSearchParams(window.location.search);
  return q.get("src");
}

export function App() {
  const src = useMemo(() => srcFromQuery(), []);
  const [root, setRoot] = useState<Object3D | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onReady = useCallback((obj: Object3D | null, err?: string) => {
    setRoot(obj);
    setError(err ?? null);
  }, []);

  const onExport = useCallback(() => {
    if (!root) return;
    setBusy(true);
    try {
      const blob = exportObjectToStl(root);
      const name = guessName(src) || "buildplate-model.stl";
      downloadBlob(blob, name);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setBusy(false);
    }
  }, [root, src]);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <strong>Buildplate</strong>
          <span>{src ? shortSrc(src) : "no mesh loaded"}</span>
        </div>
        <div className="actions">
          <button
            type="button"
            className="primary"
            disabled={!root || busy}
            onClick={onExport}
          >
            Export STL
          </button>
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
