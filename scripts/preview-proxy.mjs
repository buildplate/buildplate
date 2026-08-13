#!/usr/bin/env node
/**
 * Loopback-only TCP proxy: :80 → localhost:3920
 * so http://buildplate.localhost works (browsers omit port 80).
 */
import net from "node:net";

const LISTEN_PORT = Number(process.env.BUILDPLATE_PROXY_LISTEN || 80);
const TARGET_PORT = Number(process.env.BUILDPLATE_PREVIEW_PORT || 3920);
const TARGET_HOST = process.env.BUILDPLATE_PREVIEW_HOST || "localhost";

function forward(client) {
  const up = net.connect({ port: TARGET_PORT, host: TARGET_HOST }, () => {
    client.pipe(up);
    up.pipe(client);
  });
  const fail = () => {
    try {
      client.destroy();
    } catch {
      // ignore
    }
    try {
      up.destroy();
    } catch {
      // ignore
    }
  };
  up.on("error", fail);
  client.on("error", fail);
}

function listen(host) {
  return new Promise((resolve, reject) => {
    const server = net.createServer(forward);
    server.on("error", reject);
    server.listen({ port: LISTEN_PORT, host, ipv6Only: host.includes(":") }, () => {
      console.error(
        `[buildplate-preview] :${LISTEN_PORT} on ${host} → ${TARGET_HOST}:${TARGET_PORT}`,
      );
      resolve(server);
    });
  });
}

const hosts = ["127.0.0.1", "::1"];
for (const host of hosts) {
  try {
    await listen(host);
  } catch (err) {
    if (err.code === "EACCES") {
      console.error(`[buildplate-preview] cannot bind port ${LISTEN_PORT} (${err.code})`);
      process.exit(77);
    }
    if (err.code === "EADDRINUSE") {
      continue;
    }
    throw err;
  }
}
