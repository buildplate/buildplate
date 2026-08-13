#!/usr/bin/env bash
# Buildplate install — Node + Python worker setup.
set -euo pipefail

REPO_URL="${BUILDPLATE_REPO_URL:-https://github.com/buildplate/buildplate.git}"
INSTALL_DIR="${BUILDPLATE_HOME:-$HOME/buildplate/src}"

echo "→ Buildplate install"
echo "  dir: $INSTALL_DIR"

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js 20+ is required. Install from https://nodejs.org and re-run."
  exit 1
fi

NODE_MAJOR="$(node -p "process.versions.node.split('.')[0]")"
if [ "$NODE_MAJOR" -lt 20 ]; then
  echo "Node.js 20+ required (found $(node -v))."
  exit 1
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo "→ Updating existing clone"
  git -C "$INSTALL_DIR" pull --ff-only
else
  echo "→ Cloning"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "→ npm install"
(cd "$INSTALL_DIR" && npm install)

echo "→ npm run setup (Python worker)"
(cd "$INSTALL_DIR" && npm run setup)

MCP_ENTRY="$INSTALL_DIR/mcp/server.mjs"
echo
echo "Installed. Add this to ~/.cursor/mcp.json (or project .cursor/mcp.json):"
echo
cat <<EOF
{
  "mcpServers": {
    "buildplate": {
      "command": "node",
      "args": ["$MCP_ENTRY"],
      "env": {
        "BUILDPLATE_PREVIEW_URL": "http://buildplate.localhost:3920"
      }
    }
  }
}
EOF
echo
echo "Run preview + worker:  cd $INSTALL_DIR && npm start"
echo "MCP alone:             cd $INSTALL_DIR && npm run mcp"
echo "Done."
