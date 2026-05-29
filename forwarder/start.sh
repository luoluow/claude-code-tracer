#!/usr/bin/env bash
set -euo pipefail

PORT="${FORWARDER_PORT:-7356}"

echo "Point Claude Code at the forwarder by running:"
echo
echo "    export ANTHROPIC_BASE_URL=http://127.0.0.1:${PORT}"
echo

exec python3 "$(dirname "$0")/proxy.py"
