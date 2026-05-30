#!/usr/bin/env bash
# Claude Code Tracer — one-command launcher.
#
# Configures Claude Code hooks, starts the tracer server (UI + hooks + API
# proxy on one port), then runs Claude Code with its API traffic routed through
# it. The server is torn down automatically when Claude Code exits.
#
#   ./trace.sh [claude args...]
set -euo pipefail

cd "$(dirname "$0")"

TRACER_PORT=7355
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
DATE=$(date +"%Y%m%d%H%M")

# Where session JSONL and the server stdout logs are written. The tracer server
# reads the same TRACER_LOG_DIR, so they always agree.
export TRACER_LOG_DIR="${TRACER_LOG_DIR:-$PWD/temp/logs}"
mkdir -p "$TRACER_LOG_DIR"

# 1. Dependencies ----------------------------------------------------------
if ! python3 -c 'import fastapi, uvicorn, httpx' 2>/dev/null; then
  echo "Installing Python dependencies..."
  python3 -m pip install -q -r requirements.txt
fi

# 2. Configure Claude Code hooks (idempotent; backs up the original once) --
SETTINGS="$SETTINGS" python3 - settings_example.json <<'PY'
import json, os, shutil
from pathlib import Path

settings_path = Path(os.environ["SETTINGS"])
example = json.loads(Path("settings_example.json").read_text())["hooks"]

current = {}
if settings_path.exists():
    current = json.loads(settings_path.read_text() or "{}")
    bak = settings_path.with_suffix(".json.bak")
    if not bak.exists():
        shutil.copy(settings_path, bak)
        print(f"Backed up existing settings to {bak}")

settings_path.parent.mkdir(parents=True, exist_ok=True)
hooks = current.setdefault("hooks", {})
added = False
for event, entries in example.items():
    bucket = hooks.setdefault(event, [])
    serialized = json.dumps(bucket)
    for entry in entries:
        if json.dumps(entry) not in serialized:
            bucket.append(entry)
            added = True
settings_path.write_text(json.dumps(current, indent=2) + "\n")
print(f"Tracer hooks {'added to' if added else 'already present in'} {settings_path}")
PY

# 3. Start the tracer server (UI + hooks + API proxy), tear down on exit ---
./tracer/start.sh > "$TRACER_LOG_DIR/tracer_$DATE.log" 2>&1 &
TRACER_PID=$!
trap 'echo; echo "Stopping tracer..."; kill "$TRACER_PID" 2>/dev/null || true' EXIT

# Wait for the server to accept connections (TCP probe only).
wait_port() {
  for _ in $(seq 1 50); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
    sleep 0.1
  done
  return 1
}
wait_port "$TRACER_PORT" || { echo "Tracer failed to start — see $TRACER_LOG_DIR/tracer_$DATE.log" >&2; exit 1; }

# 4. Run Claude Code routed through the in-process API proxy ---------------
export ANTHROPIC_BASE_URL="http://127.0.0.1:${TRACER_PORT}"
echo "Tracer UI + API proxy:  http://127.0.0.1:${TRACER_PORT}"
echo "Logs:                   $TRACER_LOG_DIR/"
echo
claude "$@"
