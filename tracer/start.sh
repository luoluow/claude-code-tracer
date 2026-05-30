#!/usr/bin/env bash
set -euo pipefail

PORT="${TRACER_PORT:-7355}"

# Free the port so a restart binds cleanly (kills any prior tracer server).
fuser -k "${PORT}/tcp" 2>/dev/null && sleep 0.3 || true

exec python3 "$(dirname "$0")/server.py"
