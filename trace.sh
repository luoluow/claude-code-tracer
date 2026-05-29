#!/usr/bin/env bash
# Claude Code Tracer — one-command launcher.
#
# Configures Claude Code hooks, starts the tracer server + API forwarder, then
# runs Claude Code with its API traffic routed through the forwarder. The
# servers are torn down automatically when Claude Code exits.
#
#   ./trace.sh [claude args...]
set -euo pipefail

cd "$(dirname "$0")"

TRACER_PORT=7355
FORWARDER_PORT="${FORWARDER_PORT:-7356}"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
DATE=$(date +"%Y%m%d%H%M")

mkdir -p temp

# 1. Dependencies ----------------------------------------------------------
if ! python3 -c 'import fastapi, uvicorn' 2>/dev/null; then
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

# 3. Start tracer server + forwarder, tear down on exit --------------------
./tracer/start.sh    > "temp/tracer_$DATE.log"    2>&1 &
TRACER_PID=$!
./forwarder/start.sh > "temp/forwarder_$DATE.log" 2>&1 &
FWD_PID=$!
trap 'echo; echo "Stopping tracer + forwarder..."; kill "$TRACER_PID" "$FWD_PID" 2>/dev/null || true' EXIT

# Wait for both to accept connections (TCP probe only — an HTTP request to the
# forwarder would be relayed to the real API).
wait_port() {
  for _ in $(seq 1 50); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
    sleep 0.1
  done
  return 1
}
wait_port "$TRACER_PORT"    || { echo "Tracer failed to start — see temp/tracer_$DATE.log" >&2; exit 1; }
wait_port "$FORWARDER_PORT" || { echo "Forwarder failed to start — see temp/forwarder_$DATE.log" >&2; exit 1; }

# 4. Run Claude Code through the forwarder ---------------------------------
export ANTHROPIC_BASE_URL="http://127.0.0.1:${FORWARDER_PORT}"
echo "Tracer UI:  http://127.0.0.1:${TRACER_PORT}"
echo "Forwarder:  ${ANTHROPIC_BASE_URL}"
echo "Logs:       temp/tracer_$DATE.log, temp/forwarder_$DATE.log"
echo
claude "$@"
