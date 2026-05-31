#!/usr/bin/env bash
# End-to-end test for the Claude Code Tracer — fully isolated, no creds, no real
# API calls. Stands up the merged tracer (UI + hooks + /v1 proxy) against a MOCK
# Anthropic upstream, drives it the way Claude Code would (hook POSTs + a streaming
# /v1 request), and asserts the whole data flow. Exits non-zero on any failure.
#
#   ./tracer_e2e_mock_test/run_e2e.sh
#
# Everything lives under this dir; nothing touches your real tracer or temp/logs.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

TRACER_PORT="${E2E_TRACER_PORT:-7400}"
MOCK_PORT="${E2E_MOCK_PORT:-9400}"
BASE="http://127.0.0.1:${TRACER_PORT}"
WORK="$HERE/run"

PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="python3"

pass=0; fail=0
ok()  { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }

cleanup() { kill "${SRV:-}" "${MOCK:-}" 2>/dev/null; }
trap cleanup EXIT

# --- deps ----------------------------------------------------------------
if ! "$PY" -c 'import cc_tracer.server, uvicorn' 2>/dev/null; then
  echo "cc_tracer not importable. Run: pip install -e . (from the repo root)" >&2
  exit 1
fi

rm -rf "$WORK"; mkdir -p "$WORK/sessions"

# --- 1. mock Anthropic upstream (canned SSE; honors Accept-Encoding) ------
cat > "$WORK/mock.py" <<'PY'
import sys, gzip
from http.server import BaseHTTPRequestHandler, HTTPServer
PORT = int(sys.argv[1])
SSE = b"""event: message_start
data: {"type":"message_start","message":{"id":"msg_e2e","model":"claude-opus-4-8","role":"assistant","usage":{"input_tokens":1234,"cache_read_input_tokens":1000}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"running the tests now"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_e2e","name":"Bash","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":\\"pytest\\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":42}}

event: message_stop
data: {"type":"message_stop"}

"""
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0)); self.rfile.read(n)
        ae = (self.headers.get('Accept-Encoding') or '').lower()
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
        if 'gzip' in ae and 'identity' not in ae:
            body = gzip.compress(SSE); self.send_header('Content-Encoding', 'gzip')
        else:
            body = SSE
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
HTTPServer(('127.0.0.1', PORT), H).serve_forever()
PY
"$PY" "$WORK/mock.py" "$MOCK_PORT" > "$WORK/mock.log" 2>&1 & MOCK=$!

# --- 2. start the merged tracer (isolated logs, pointed at the mock) ------
TRACER_LOG_DIR="$WORK/sessions" ANTHROPIC_UPSTREAM_URL="http://127.0.0.1:${MOCK_PORT}" \
  "$PY" -c "import cc_tracer.server as server, uvicorn; uvicorn.run(server.app, host='127.0.0.1', port=${TRACER_PORT})" \
  > "$WORK/tracer.log" 2>&1 & SRV=$!

wait_port() {
  for _ in $(seq 1 50); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
    sleep 0.1
  done
  return 1
}
wait_port "$MOCK_PORT"   || { echo "mock failed to start; see $WORK/mock.log"   >&2; exit 1; }
wait_port "$TRACER_PORT" || { echo "tracer failed to start; see $WORK/tracer.log" >&2; exit 1; }

echo "E2E: tracer :$TRACER_PORT  mock-upstream :$MOCK_PORT"
echo

post_hook() { curl -s -o /dev/null -X POST "$BASE/event" -H 'Content-Type: application/json' -d "$1"; }

# --- 3. assertions -------------------------------------------------------

# A. UI served at /
code=$(curl -s -o "$WORK/ui.html" -w '%{http_code}' "$BASE/")
if [ "$code" = 200 ] && grep -q "Claude Code Tracer" "$WORK/ui.html"; then
  ok "UI served at /"
else
  bad "UI served at / (http $code)"
fi

# B. hook flow: 5 events for one session
post_hook '{"session_id":"e2e-hooks","hook_event_name":"SessionStart","source":"startup","model":"claude-opus-4-8","cwd":"/tmp/proj"}'
post_hook '{"session_id":"e2e-hooks","hook_event_name":"UserPromptSubmit","prompt":"run tests"}'
post_hook '{"session_id":"e2e-hooks","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"pytest"}}'
post_hook '{"session_id":"e2e-hooks","hook_event_name":"PostToolUse","tool_name":"Bash","tool_response":"2 passed"}'
post_hook '{"session_id":"e2e-hooks","hook_event_name":"Stop","stop_reason":"end_turn"}'
n=$(curl -s "$BASE/events/e2e-hooks" | "$PY" -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null)
[ "$n" = 5 ] && ok "hook events stored (5)" || bad "hook events stored (got '$n')"

# C. /sessions metadata derived correctly
if curl -s "$BASE/sessions" | "$PY" -c "
import sys, json
ss = {s['session_id']: s for s in json.load(sys.stdin)}
s = ss.get('e2e-hooks'); assert s, 'e2e-hooks missing'
assert s['event_count'] == 5, s['event_count']
assert s['model'] == 'claude-opus-4-8', s['model']
" 2>/dev/null; then ok "/sessions metadata (count + model)"; else bad "/sessions metadata"; fi

# D. proxy streams plaintext SSE back (client asked gzip; identity forced)
curl -s -X POST "$BASE/v1/messages" -H 'Accept-Encoding: gzip, br' -H 'x-session-id: e2e-api' \
  -d '{"model":"claude-opus-4-8","messages":[{"role":"user","content":"hi"}],"stream":true}' > "$WORK/stream.out"
grep -q "event: message_start" "$WORK/stream.out" \
  && ok "proxy streamed plaintext SSE (identity override)" \
  || bad "proxy streaming"
sleep 0.4

# E. ApiCall reassembled in-process
if curl -s "$BASE/events/e2e-api" | "$PY" -c "
import sys, json
evs = json.load(sys.stdin); assert evs, 'no api events'
e = evs[-1]; assert e['hook_event_name'] == 'ApiCall'
r = e['response']; assert r.get('model') == 'claude-opus-4-8', r.get('model')
types = [b['type'] for b in r.get('content', [])]
assert 'text' in types and 'tool_use' in types, types
tu = next(b for b in r['content'] if b['type'] == 'tool_use')
assert tu['input'] == {'command': 'pytest'}, tu['input']
assert e['usage'].get('output_tokens') == 42, e['usage']
assert e['usage'].get('cache_read_input_tokens') == 1000, e['usage']
" 2>/dev/null; then ok "ApiCall reassembled (text+tool_use, tokens, cache)"; else bad "ApiCall reassembly"; fi

# F. SSE live broadcast reaches a subscriber
( timeout 3 curl -sN "$BASE/stream" > "$WORK/sse.out" 2>/dev/null ) & sub=$!
sleep 0.6
post_hook '{"session_id":"e2e-sse","hook_event_name":"Stop","stop_reason":"e2e-marker-xyz"}'
wait "$sub" 2>/dev/null
grep -q "e2e-marker-xyz" "$WORK/sse.out" \
  && ok "SSE live broadcast received" \
  || bad "SSE live broadcast"

# --- report --------------------------------------------------------------
echo
echo "E2E result: ${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]
