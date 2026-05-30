#!/usr/bin/env bash
# Launch Claude Code with its API traffic routed through the local tracer.
# The tracer server must already be running (./start_tracer.sh).
set -euo pipefail

PORT="${TRACER_PORT:-7355}"
URL="http://127.0.0.1:${PORT}"

# Verify the tracer is listening (TCP only — an HTTP probe to /v1 would be relayed
# to the real API). The subshell opens and closes the connection by exiting.
if ! (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
  echo "Tracer not listening on port ${PORT}." >&2
  echo "Start it first:  ./start_tracer.sh" >&2
  exit 1
fi

export ANTHROPIC_BASE_URL="$URL"
echo "Routing Claude Code through the tracer:"
echo "    ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}"
echo
exec claude "$@"
