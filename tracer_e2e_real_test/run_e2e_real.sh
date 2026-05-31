#!/usr/bin/env bash
# REAL end-to-end test: launches actual Claude Code routed through the tracer and
# verifies the tracer captured both the hook events and the real API calls.
#
#   ./tracer_e2e_real_test/run_e2e_real.sh
#
# Scripted interaction (headless):
#   - ask "How is the agent memory managed in Claude Code?"  (one real turn)
#   - claude -p exits when the turn completes
#
# ┌─ WARNING ───────────────────────────────────────────────────────────────┐
# │ This makes REAL, BILLED Anthropic API calls and uses your Claude Code     │
# │ auth. It is NOT free and NOT deterministic. Run it deliberately.          │
# └───────────────────────────────────────────────────────────────────────────┘
#
# Everything lives under this test dir's run/ — deterministic and easy to find.
# Claude runs in run/project/ (its own `git init` scopes it to that dir so it
# does not roam the parent repo). Nothing touches your real tracer (:7355).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PORT="${E2E_REAL_PORT:-7411}"
BASE="http://127.0.0.1:${PORT}"
WORK="$HERE/run"
LOGS="$WORK/sessions"          # tracer JSONL output
PROJECT="$WORK/project"        # where claude runs (under the test dir)

PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="python3"

pass=0; fail=0
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }

# The tracer is intentionally LEFT RUNNING after the test so you can browse the
# web UI; the script prints the command to stop it. (No EXIT-trap kill.)

# --- preconditions -------------------------------------------------------
command -v claude >/dev/null || { echo "claude not on PATH" >&2; exit 1; }
"$PY" -c 'import cc_tracer.server, uvicorn' 2>/dev/null \
  || { echo "cc_tracer not importable. Run: pip install -e . (from the repo root)" >&2; exit 1; }

rm -rf "$WORK"; mkdir -p "$PROJECT/.claude" "$LOGS"
git init -q "$PROJECT"   # scope claude's workspace to run/project, not the parent repo

# --- 1. project with tracer hooks ----------------------------------------
cat > "$PROJECT/.claude/settings.json" <<JSON
{
  "hooks": {
    "SessionStart":       [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "SessionEnd":         [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "UserPromptSubmit":   [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "PreToolUse":         [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "PostToolUse":        [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "PostToolUseFailure": [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "SubagentStart":      [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "SubagentStop":       [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "PreCompact":         [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "PostCompact":        [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }],
    "Stop":               [{ "hooks": [{ "type": "http", "url": "${BASE}/event" }] }]
  }
}
JSON

# --- 2. start an isolated tracer (real upstream), wait for it ------------
TRACER_LOG_DIR="$LOGS" \
  "$PY" -c "import cc_tracer.server as server, uvicorn; uvicorn.run(server.app, host='127.0.0.1', port=${PORT})" \
  > "$WORK/tracer.log" 2>&1 & SRV=$!
started=""
for _ in $(seq 1 50); do (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null && { started=1; break; }; sleep 0.1; done
[ -n "$started" ] || { echo "Tracer failed to start — see $WORK/tracer.log" >&2; kill "$SRV" 2>/dev/null; exit 1; }
disown "$SRV" 2>/dev/null || true   # detach so it survives this script exiting

echo "Real E2E: tracer :$PORT   project $PROJECT"
echo "Routing Claude Code through the tracer (real API calls)..."
echo

# --- 3. drive real Claude Code, headless ---------------------------------
export ANTHROPIC_BASE_URL="$BASE"
SID="$("$PY" -c 'import uuid; print(uuid.uuid4())')"

echo "[turn] asking about agent memory"
( cd "$PROJECT" && claude -p --session-id "$SID" --permission-mode bypassPermissions \
    "How is the agent memory managed in Claude Code?" ) \
  > "$WORK/turn.log" 2>&1 || echo "  (claude returned non-zero — see run/turn.log)"

sleep 1   # let the last ApiCall/Stop hook land

# --- 4. assertions -------------------------------------------------------
echo

# A. hook events captured for the scripted session
if curl -s "$BASE/events/$SID" | "$PY" -c "
import sys, json
evs = json.load(sys.stdin)
kinds = {e.get('hook_event_name') for e in evs}
assert len(evs) >= 3, f'only {len(evs)} events'
assert 'UserPromptSubmit' in kinds, kinds
" 2>/dev/null; then ok "hook events captured for session"; else bad "hook events captured for session"; fi

# B. real API calls captured, reassembled, AND merged into the same session
if curl -s "$BASE/events/$SID" | "$PY" -c "
import sys, json
evs = json.load(sys.stdin)
calls = [e for e in evs if e.get('hook_event_name') == 'ApiCall']
assert calls, 'no ApiCall merged into the hook session'
populated = [e for e in calls if e.get('response', {}).get('content')]
assert populated, f'{len(calls)} ApiCalls but none populated'
print(f'   ({len(populated)}/{len(calls)} ApiCalls merged into session, model={populated[0][\"response\"].get(\"model\")})')
" 2>/dev/null; then ok "API calls merged into the session & reassembled"; else bad "API calls merged into the session"; fi

# --- report --------------------------------------------------------------
echo
echo "Real E2E result: ${pass} passed, ${fail} failed"
echo "Everything is under: $WORK/"
echo "  - captured sessions : $LOGS/        (<uuid>.jsonl hooks, api-*.jsonl API calls)"
echo "  - claude's project  : $PROJECT/     (.claude/settings.json, any files it wrote)"
echo "  - claude's output   : $WORK/turn.log"
echo
echo "──────────────────────────────────────────────────────────────────────"
echo "  The tracer is STILL RUNNING (pid $SRV) so you can inspect the capture."
echo
echo "      Open the web UI:   http://127.0.0.1:${PORT}"
echo
echo "  When you're done looking, stop the server with:"
echo
echo "      kill $SRV          # or:  fuser -k ${PORT}/tcp"
echo "──────────────────────────────────────────────────────────────────────"

# Exit code reflects the assertions; the tracer is left running on purpose.
exit $([ "$fail" -eq 0 ] && echo 0 || echo 1)
