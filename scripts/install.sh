#!/usr/bin/env bash
# Optional curl|bash wrapper. Prefer: npx -y buildplate setup
set -euo pipefail

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js 20+ is required. Install from https://nodejs.org and re-run."
  exit 1
fi

NODE_MAJOR="$(node -p "process.versions.node.split('.')[0]")"
if [ "$NODE_MAJOR" -lt 20 ]; then
  echo "Node.js 20+ required (found $(node -v))."
  exit 1
fi

echo "→ npx -y buildplate setup"
npx -y buildplate setup

echo
echo "Setup complete. Next:"
echo "  npx buildplate start"
echo
echo "MCP (Cursor / Claude / Codex):"
cat <<EOF
{
  "mcpServers": {
    "buildplate": {
      "command": "npx",
      "args": ["-y", "buildplate"],
      "env": { "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost" }
    }
  }
}
EOF
