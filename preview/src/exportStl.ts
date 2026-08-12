import * as THREE from "three";
import { STLExporter } from "three/examples/jsm/exporters/STLExporter.js";

export function exportObjectToStl(root: THREE.Object3D): Blob {
  const clone = root.clone(true);
  clone.updateMatrixWorld(true);

  // Bake world transforms into geometry so the STL matches the preview.
  const group = new THREE.Group();
  clone.traverse((obj) => {
    const mesh = obj as THREE.Mesh;
    if (!mesh.isMesh || !mesh.geometry) return;
    const geom = mesh.geometry.clone();
    geom.applyMatrix4(mesh.matrixWorld);
    const m = new THREE.Mesh(geom);
    group.add(m);
  });

  const exporter = new STLExporter();
  const result = exporter.parse(group, { binary: true }) as unknown as DataView;
  return new Blob([result as unknown as BlobPart], { type: "model/stl" });
}

export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
