#!/usr/bin/env bash
# Launch Claude Code with its API traffic routed through the local tracer forwarder.
# The forwarder must already be running (./forwarder/start.sh in another terminal).
set -euo pipefail

PORT="${FORWARDER_PORT:-7356}"
URL="http://127.0.0.1:${PORT}"

# Verify the forwarder is listening (TCP only — an HTTP probe would be forwarded
# to the real API). The subshell opens and closes the connection by exiting.
if ! (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
  echo "Forwarder not listening on port ${PORT}." >&2
  echo "Start it first in another terminal:  ./forwarder/start.sh" >&2
  exit 1
fi

export ANTHROPIC_BASE_URL="$URL"
echo "Routing Claude Code through the tracer forwarder:"
echo "    ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}"
echo
exec claude "$@"
