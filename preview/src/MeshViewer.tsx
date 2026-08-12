import { useEffect, useRef, type MutableRefObject } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { Grid, OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import type { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import { useState } from "react";

type Props = {
  src: string;
  onReady: (root: THREE.Object3D | null, error?: string) => void;
};

export function MeshViewer({ src, onReady }: Props) {
  const [root, setRoot] = useState<THREE.Object3D | null>(null);
  const orbitRef = useRef<OrbitControlsImpl>(null);

  useEffect(() => {
    let cancelled = false;
    setRoot(null);
    onReady(null);

    loadMesh(src)
      .then((obj) => {
        if (cancelled) return;
        setRoot(obj);
        onReady(obj);
      })
      .catch((err) => {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : "Failed to load mesh";
        setRoot(null);
        onReady(null, msg);
      });

    return () => {
      cancelled = true;
    };
  }, [src, onReady]);

  return (
    <Canvas
      shadows
      gl={{ preserveDrawingBuffer: true, antialias: true }}
      camera={{ position: [90, 80, 120], fov: 45, near: 0.1, far: 5000 }}
      style={{ width: "100%", height: "100%" }}
    >
      <color attach="background" args={["#12171c"]} />
      <hemisphereLight args={["#ffffff", "#1a222b", 0.85]} />
      <directionalLight position={[80, 140, 60]} intensity={1.35} castShadow />

      <Grid
        args={[400, 400]}
        cellSize={10}
        cellThickness={0.5}
        cellColor="#2a343f"
        sectionSize={50}
        sectionThickness={1.1}
        sectionColor="#3d4c5c"
        infiniteGrid
        fadeDistance={900}
        fadeStrength={1}
      />

      {root && <primitive object={root} />}

      <OrbitControls
        ref={orbitRef}
        makeDefault
        enablePan
        enableDamping
        dampingFactor={0.08}
        maxPolarAngle={Math.PI * 0.49}
        minDistance={5}
        maxDistance={2000}
      />
      {root && <FitOnce root={root} orbitRef={orbitRef} />}
    </Canvas>
  );
}

function FitOnce({
  root,
  orbitRef,
}: {
  root: THREE.Object3D;
  orbitRef: MutableRefObject<OrbitControlsImpl | null>;
}) {
  const { camera, size } = useThree();
  const done = useRef(false);

  useEffect(() => {
    done.current = false;
  }, [root]);

  useEffect(() => {
    if (done.current) return;
    const box = new THREE.Box3().setFromObject(root);
    if (box.isEmpty()) return;
    done.current = true;

    const center = box.getCenter(new THREE.Vector3());
    const boxSize = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(boxSize.x, boxSize.y, boxSize.z, 1);

    const persp = camera as THREE.PerspectiveCamera;
    const fov = THREE.MathUtils.degToRad(persp.fov);
    const aspect =
      size.width > 0 && size.height > 0 ? size.width / size.height : persp.aspect || 1;
    const fitHeight = maxDim / (2 * Math.tan(fov / 2));
    const fitWidth = fitHeight / aspect;
    const distance = Math.max(fitHeight, fitWidth) * 1.45;

    const dir = new THREE.Vector3(1, 0.85, 1.25).normalize();
    persp.position.copy(center).addScaledVector(dir, distance);
    persp.near = Math.max(0.1, distance / 200);
    persp.far = Math.max(5000, distance * 20);
    persp.updateProjectionMatrix();

    const apply = () => {
      const orbit = orbitRef.current;
      if (!orbit) return;
      orbit.target.copy(center);
      orbit.update();
    };
    apply();
    const raf = requestAnimationFrame(apply);
    return () => cancelAnimationFrame(raf);
  }, [root, camera, size.width, size.height, orbitRef]);

  return null;
}

async function loadMesh(src: string): Promise<THREE.Object3D> {
  const lower = src.toLowerCase();
  const buffer = await fetchBuffer(src);

  if (lower.includes(".stl")) {
    return loadStl(buffer);
  }
  return loadGlb(buffer);
}

async function fetchBuffer(src: string): Promise<ArrayBuffer> {
  // file:// and http(s) both work when the preview is opened locally;
  // browsers may block file:// from http origins — prefer serving via MCP path later.
  const res = await fetch(src);
  if (!res.ok) throw new Error(`Could not fetch mesh (${res.status})`);
  return res.arrayBuffer();
}

function loadGlb(buffer: ArrayBuffer): Promise<THREE.Object3D> {
  return new Promise((resolve, reject) => {
    const loader = new GLTFLoader();
    loader.parse(
      buffer,
      "",
      (gltf) => {
        const root = gltf.scene;
        normalize(root);
        resolve(root);
      },
      (err) => reject(err instanceof Error ? err : new Error("Failed to parse GLB")),
    );
  });
}

function loadStl(buffer: ArrayBuffer): THREE.Object3D {
  const loader = new STLLoader();
  const geometry = loader.parse(buffer);
  geometry.computeVertexNormals();
  const material = new THREE.MeshStandardMaterial({
    color: "#c5d0db",
    metalness: 0.05,
    roughness: 0.55,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  const group = new THREE.Group();
  group.add(mesh);
  normalize(group);
  return group;
}

/** Center + scale longest axis to ~80 units (≈ mm in print-ish space). */
function normalize(root: THREE.Object3D) {
  root.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z, 1e-8);
  const scale = 80 / maxDim;
  root.position.x -= center.x;
  root.position.y -= center.y;
  root.position.z -= center.z;
  root.scale.multiplyScalar(scale);
  root.updateMatrixWorld(true);
  const after = new THREE.Box3().setFromObject(root);
  if (!after.isEmpty()) {
    root.position.y -= after.min.y;
  }
}
