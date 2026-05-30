# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

A local, infra-free, raw-fidelity inspector for a *single* Claude Code session —
"DevTools Network tab for Claude Code." Deliberately not aggregate observability
(that's what native OTel / SigNoz / Grafana are for); see `README.md` for the
comparison.

## Architecture

**One process, one port (`127.0.0.1:7355`).** `tracer/server.py` is a FastAPI app
that is simultaneously the hooks sink, the Anthropic API proxy, the JSONL store, the
SSE broadcaster, and the web-UI host. There is no separate forwarder process anymore —
it was merged into the server (older docs/commits may still mention `:7356`).

Capture happens in **two tiers**, either or both can run:

- **Hook tier** — Claude Code hooks POST to `/event`. Captures *what Claude did*:
  prompts, Pre/PostToolUse, results, stop reasons. Enabled via `settings.json` hooks.
- **API-proxy tier** — Claude Code points `ANTHROPIC_BASE_URL` at the server; `/v1/*`
  is streamed through to the real API (plain HTTP in, real HTTPS out, no cert trust),
  and the SSE stream is tapped and reassembled into one `ApiCall` event per turn.
  Captures *what Claude saw and thought*: system prompt, context, reasoning, tokens.

Data flow: every event (hook payload or reassembled `ApiCall`) gets an injected `_ts`,
is appended as one compact line to `temp/logs/{session_id}.jsonl` (the server is the
single writer), printed to the server's terminal, and pushed to all `/stream` SSE
subscribers. The UI (`static/index.html`, single-file vanilla JS) renders a flat
chronological timeline and reads `/sessions`, `/events/{id}`, `/stream`.

### Key invariant: never block or break the traced session

The tracer is an observer. Hook delivery is fire-and-forget; the API proxy must stream
transparently and fail open. When changing the proxy or `/event` path, preserve this.

### Contracts before code

`docs/CONTRACT.md` is the **frozen** shared contract: the event schema, the JSONL line
format, and the HTTP/SSE API (including the `/v1/*` proxy and the exact SSE envelope).
Treat it as read-only; coordinate before changing it, because the UI and tests are
built against it. `docs/DESIGN.md` holds the full design, rationale, and phase plan.

### Why these choices (don't undo without reason)

- **JSONL, not SQL** — one file per session; listing = glob, loading = read one file,
  concurrent sessions = zero write contention. Add SQLite only if cross-session search
  ever gets slow (Phase 5).
- **Base-URL forwarder, not mitmproxy** — avoids forging a TLS cert for a domain we
  don't own and the Node `NODE_EXTRA_CA_CERTS` dance. Cost: we hand-write SSE
  reassembly (`_reassemble_sse` in `server.py`), which is schema-coupled to Anthropic's
  current event shape and needs upkeep when the API evolves.

## Commands

```bash
# One command: install deps, configure hooks, start server, run Claude through it.
./trace.sh [claude args...]          # tears the server down on exit; UI at :7355

# Run pieces by hand:
./start_tracer.sh                    # server only (hook tier + UI + proxy), backgrounded
./start_claude_traced.sh [args...]   # launch Claude routed through an already-running server
python3 tracer/server.py             # server in the foreground (what start.sh execs)

# Terminal viewer for captured JSONL (no UI):
python3 tracer/view.py list                 # list sessions in TRACER_LOG_DIR
python3 tracer/view.py show [N] [EventType]  # show session N (default latest), opt. filter

# End-to-end tests, both fully isolated (own ports/log dirs; nothing touches :7355):
./tracer_e2e_mock_test/run_e2e.sh        # MOCK upstream, no creds, no billing — default
./tracer_e2e_real_test/run_e2e_real.sh   # REAL, BILLED API; runs actual Claude headless
```

Dependencies: `fastapi`, `uvicorn`, `httpx` (`requirements.txt`). `trace.sh` installs
them on demand if missing. `claude` must be on `PATH` for `trace.sh` and the e2e test.

### Configuration

- `TRACER_LOG_DIR` — where session JSONL + server logs go (default `temp/logs/`,
  gitignored). The server and the launch scripts read the same value so they agree.
- `ANTHROPIC_UPSTREAM_URL` — proxy target (default `https://api.anthropic.com`).
- `CLAUDE_SETTINGS` — which `settings.json` `trace.sh` merges hooks into (default
  `~/.claude/settings.json`; it backs the original up to `.json.bak` once, idempotently).
- `settings_example.json` is the canonical hooks block; the proxy reads `x-session-id`
  to group API turns, falling back to a per-server-run `api-<timestamp>` id.

---

## Behavioral Guidelines

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```
