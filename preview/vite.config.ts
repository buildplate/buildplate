import path from "node:path";
import os from "node:os";
import fs from "node:fs";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const OUT_DIR =
  process.env.BUILDPLATE_OUT_DIR?.trim() ||
  path.join(os.homedir(), "buildplate", "out");

/** Serve ~/buildplate/out at /out so the browser never needs file:// */
function serveOutDir(): Plugin {
  return {
    name: "buildplate-serve-out",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url?.startsWith("/out/")) return next();
        const rel = decodeURIComponent(req.url.slice("/out/".length).split("?")[0]);
        const file = path.normalize(path.join(OUT_DIR, rel));
        if (!file.startsWith(path.normalize(OUT_DIR + path.sep)) && file !== path.normalize(OUT_DIR)) {
          res.statusCode = 403;
          res.end("forbidden");
          return;
        }
        if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
          res.statusCode = 404;
          res.end("not found");
          return;
        }
        const ext = path.extname(file).toLowerCase();
        const type =
          ext === ".glb"
            ? "model/gltf-binary"
            : ext === ".stl"
              ? "model/stl"
              : "application/octet-stream";
        res.setHeader("Content-Type", type);
        res.setHeader("Cache-Control", "no-store");
        fs.createReadStream(file).pipe(res);
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), serveOutDir()],
  server: {
    host: "127.0.0.1",
    port: 3920,
    strictPort: true,
  },
});
