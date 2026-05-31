# Claude Code Tracer — Design

A simple local tracer for Claude Code, for learning, debugging, and troubleshooting
Claude Code behavior. This document captures the agreed design.

## Goals

Observe, replay, and understand Claude Code behavior: what tools it calls, in what
order, with what inputs, and why.

## Architecture Overview

```
Claude Code CLI
    │
    ├─── HTTP hooks ──────────────────→ Tracer Server (localhost:7355)
    │                                         │
    └─── API calls (ANTHROPIC_BASE_URL) ─────→│ /v1/* proxy ──→ api.anthropic.com
         no cert trust needed                 │   (taps SSE → ApiCall, in-process)
                                              │
                                         ┌────┴────┐
                                       JSONL    SSE broadcast
                                       (disk)      │
                                         └────┬────┘
                                       Web UI (served by the same server)
```

One process, one port: hooks sink, API proxy, JSONL storage, SSE, and the web UI
all live in `tracer/server.py`.

## Decisions

| Dimension    | Decision                                                              |
| ------------ | --------------------------------------------------------------------- |
| Backend      | FastAPI + uvicorn, local                                              |
| Storage      | Per-session JSONL in `temp/logs/` (`$TRACER_LOG_DIR`, no SQL)          |
| Frontend     | Single-file vanilla JS + SSE, served by backend; flat-stream timeline |
| API capture  | In-process `/v1/*` proxy on the same server (no mitmproxy, no cert trust) |
| Live updates | In-memory broadcast → SSE                                             |

### Why JSONL, not SQL

For the core job — capture a session, view/replay it — one JSONL file per session
maps perfectly: listing = glob a directory, loading = read one file, concurrent
sessions = separate files with zero write contention. SQLite would only earn its
place for Phase 5 cross-session search/stats, and at the scale of a local debugging
tool (dozens to hundreds of sessions) scanning files is instant. Add SQLite later
*only* if cross-session queries ever feel slow.

### Why a base-URL forwarder, not mitmproxy

mitmproxy must forge a TLS cert for `api.anthropic.com` (a domain we don't own) and
have Claude Code trust mitmproxy's CA — and because Claude Code is a Node app, that
often also requires `NODE_EXTRA_CA_CERTS`, not just the system trust store. A
forwarder pointed at via `ANTHROPIC_BASE_URL` avoids all of it:

```
Claude Code ──plain HTTP──▶ localhost tracer ──normal HTTPS──▶ api.anthropic.com
            (no cert)                            (validates real cert normally)
```

The client→server hop is plain HTTP (no cert at all); the server→API hop is a normal
outbound HTTPS request. Tradeoff: we write a small streaming reverse proxy (handling
SSE passthrough) instead of getting mitmproxy's machinery for free. This proxy lives
**in-process** in the tracer server (httpx async streaming) so there is one process
and one port; the cost is that the live API path shares fate with the UI server.

## Tech Stack

| Layer    | Choice                            | Reason                                           |
| -------- | --------------------------------- | ------------------------------------------------ |
| Backend  | Python + FastAPI + uvicorn        | WebSocket/SSE support, clean routing; few deps   |
| Storage  | Per-session JSONL (stdlib)        | Simple, greppable, portable, no schema           |
| Frontend | Single HTML file, vanilla JS + SSE| Zero build step, no npm, served by backend        |
| API proxy| httpx async streaming, in the FastAPI app | Plain-HTTP in, HTTPS out; no cert trust; one port |

## Storage

Per-session JSONL files in `temp/logs/` (override with `TRACER_LOG_DIR`), keyed by
`session_id`. Each line is
one event (the hook payload plus an injected timestamp). The tracer server is the
single writer process; it routes each incoming event to the right per-session file by
`session_id`.

> Note: Phase 1 currently writes all events to one file per server-run. The proper
> version routes each event to a per-`session_id` file. Small change for Phase 2.

## Data Tiers

The UI shows two tiers of richness depending on what is running:

- **Hook-only (Phases 2–3):** what Claude *did* — the tool layer. Prompts, tool
  calls, results, stop reasons. Great for troubleshooting behavior.
- **API proxy on (Phase 4):** additionally what Claude *saw and thought* — system
  prompt, full context, reasoning text, token counts. Great for learning *why*.

## UI Design

Mental model: **DevTools' Network tab, but for Claude Code** — a chronological stream
of "what happened" that you click into for full detail.

### Layout — three panes

```
┌──────────────┬───────────────────────────────────────┬─────────────────────────┐
│ SESSIONS     │ TIMELINE  (session 17:42, opus-4.8)    │ INSPECTOR               │
│              │  ● live                  [filter ▾][🔍] │                         │
│ ● 17:42 live │                                         │ PreToolUse · Bash       │
│   forked/cct │ 17:42:01 │ UserPrompt  "fix the failing…│ 17:42:09  (took 1.3s)   │
│   42 events  │ 17:42:03 │ PreTool Bash  npm test       │                         │
│              │ 17:42:04 │ PostTool Bash  ✗ exit 1  …    │ command:                │
│   17:31      │ 17:42:09 │ PreTool Read  src/auth.ts    │   npm test              │
│   forked/cct │ 17:42:09 │ PostTool Read  240 lines     │                         │
│   18 events  │ 17:42:14 │ PreTool Edit  src/auth.ts ◆  │ ── result ──            │
│              │ 17:42:14 │ PostTool Edit  ✓             │  exit 1                 │
│   17:09      │ 17:42:30 │ PreTool Bash  npm test       │  FAIL auth.test.ts      │
│   other-proj │ 17:42:33 │ PostTool Bash  ✓ exit 0      │  ● expected 200, got401 │
│   31 events  │ 17:42:35 │ Stop  end_turn               │  [show raw JSON ▾]      │
└──────────────┴───────────────────────────────────────┴─────────────────────────┘
```

- **Left — Sessions:** pick one. Timestamp, project (cwd), model, event count; live
  sessions show a pulse indicator.
- **Center — Timeline:** the heart. One row per event, color-coded by type, with a
  one-line preview. Errors flagged (`✗`), edits marked (`◆`).
- **Right — Inspector:** click any row for the full payload, rendered per type.

### Timeline organizing metaphor: flat chronological stream

Every event is one row in time order (DevTools-Network style). Simplest to build, and
neutral for both learning and troubleshooting.

```
FLAT STREAM

17:42:01  UserPrompt  "fix failing test"
17:42:03  PreTool  Bash   npm test
17:42:04  PostTool Bash   ✗ exit 1
17:42:09  PreTool  Read   src/auth.ts
17:42:09  PostTool Read   240 lines
17:42:14  PreTool  Edit   src/auth.ts ◆
17:42:14  PostTool Edit   ✓
17:42:30  PreTool  Bash   npm test
17:42:33  PostTool Bash   ✓ exit 0
17:42:35  Stop  end_turn
```

Two alternative metaphors (turn-grouped narrative; paired request/response cards)
become **additive view toggles later** — the flat stream is the substrate they fold or
merge on top of, so choosing flat now does not close those doors.

### What you can SEE — per-event inspector rendering

The value is in rendering each event type usefully, not dumping JSON:

| Event             | Rendering                                                       |
| ----------------- | --------------------------------------------------------------- |
| UserPromptSubmit  | the full prompt text                                            |
| PreTool Bash      | the command in a code block                                     |
| PostTool Bash     | stdout/stderr + **exit code**, errors highlighted red           |
| PreTool Edit/Write| a **diff** (old → new) — most useful view for "what changed"    |
| PreTool Read      | file path + line range                                          |
| PreTool Task      | subagent prompt + agent type (later: nest the subagent's events)|
| Stop              | stop reason — where and why the turn ended                      |
| *(ApiCall)*       | system prompt, full message context, token counts, reasoning   |

### What you can DO — tagged by purpose

- **Pair Pre/Post** into one expandable unit → read it as "ran X → got Y." `[both]`
- **Filter** by event type / tool (e.g. only Bash, only errors). `[troubleshoot]`
- **Search within session** for a command, file, or error string. `[troubleshoot]`
- **Jump to next error** — skip straight to failed tool calls. `[troubleshoot]`
- **Timing annotations** — duration per tool call + gaps between (thinking time). `[learn]`
- **Live follow** with pause/resume, auto-scroll. `[both]`
- **Turn grouping** — collapse each `UserPrompt → … → Stop` cycle into a narrative unit. `[learn]`
- **Session diff** (Phase 5) — two runs side by side, to compare working vs broken. `[troubleshoot]`

### MVP vs later

**Phase 3 MVP:** sessions list, chronological timeline, click-to-inspect with
per-type rendering (incl. Edit diffs), Pre/Post pairing, live follow, basic type
filter. That alone is a genuinely useful tracer.

**Later:** turn grouping, timing, error-jump, search, session diff, subagent nesting,
and the whole API-proxy tier. All additive — none require rework of the MVP.

## Phases

### Phase 1 — Foundation (done)

- [x] HTTP hook receiver (`tracer/server.py`)
- [x] JSONL logging + terminal output
- [x] Terminal viewer (`tracer/view.py`)
- [x] `settings_example.json`
- [x] `tracer/start.sh`

### Phase 2 — Backend (db-less)

- Migrate `tracer/server.py` → FastAPI app
- Per-session JSONL routing by `session_id`
- Endpoints: `POST /event`, `GET /sessions`, `GET /events/{session_id}`,
  `GET /stream` (SSE), `GET /export/{session_id}`
- In-memory broadcast to SSE subscribers

### Phase 3 — Web UI

- Single-file `static/index.html` served by the backend
- Flat timeline, sessions list, inspector, Pre/Post pairing, live follow, type filter

### Phase 4 — Anthropic API proxy (optional, deep visibility)

- In-process `/v1/*` proxy in the tracer server; point `ANTHROPIC_BASE_URL` at
  `http://127.0.0.1:7355`. Forwards over HTTPS to api.anthropic.com.
- Forces `Accept-Encoding: identity` so the tapped SSE is plaintext (gzip/br would
  otherwise be unparseable).
- Reassembles streaming SSE into complete turns; records `ApiCall` events in-process
  (no loopback hop), attributed to the **active hook session** so API turns merge into
  the live session's timeline (x-session-id header wins; `api-<timestamp>` fallback when
  no hooks run). Exact for one session; best-effort temporal with several at once.
- Rendered inline in the timeline/inspector (system prompt, context, response,
  tokens) — not a separate tab.

> Originally built as a standalone forwarder on `:7356`, later merged into the tracer
> server (single process, single port) — see the architecture overview above.

### Phase 5 — Analysis (optional)

- Token + latency stats per session
- Tool-usage frequency across sessions
- Session diff (side by side)
- Full-text search across prompts and tool inputs (add SQLite only if file scans slow)

## Multi-Agent Execution Plan

The work parallelizes into a contract-first keystone plus a few independent tracks —
not a naive per-phase split.

### Dependency graph

```
                    ┌─────────────────────────┐
                    │  KEYSTONE (sequential)  │
                    │  contracts: event schema│
                    │  + JSONL line format    │
                    │  + HTTP/SSE API spec    │
                    └────────────┬────────────┘
                                 │ (frozen, read-only for all)
              ┌──────────────────┼──────────────────┐
              ▼                   ▼                  ▼
       ┌────────────┐     ┌────────────┐     ┌────────────┐
       │ TRACK A    │     │ TRACK B    │     │ TRACK C    │
       │ backend    │     │ web UI     │     │ forwarder  │
       │ server     │     │ (vs mock)  │     │ (independent)
       └─────┬──────┘     └─────┬──────┘     └─────┬──────┘
             └────────────┬─────┘                  │
                          ▼                         │
                 ┌────────────────┐                │
                 │  INTEGRATION   │◄───────────────┘
                 │  + Phase 5     │
                 └────────────────┘
```

### Keystone (must come first)

Freeze the shared contracts before any parallel work:

1. **Event schema** — the JSON hooks POST.
2. **JSONL line format** — basically the event schema plus injected timestamp.
3. **HTTP/SSE API spec** — `/event`, `/stream`, `/sessions`, `/events/{id}`,
   `/export`, and the exact SSE message envelope (with a concrete example payload).

Output: a short contract doc + a committed example payload. Everything downstream
treats these as read-only.

### Tracks (disjoint file ownership, independent verification)

| Track          | Owns                                   | Depends on        | Verifies in isolation by                                  |
| -------------- | -------------------------------------- | ----------------- | --------------------------------------------------------- |
| A — Backend    | `tracer/server.py`, `requirements.txt` | API spec          | `curl POST /event` → line appears; `/stream` emits event  |
| B — Web UI     | `static/index.html`                    | API spec only     | Loads against committed fixture JSON + mock SSE stream    |
| C — Forwarder  | `forwarder/` (proxy + start script)    | JSONL line format | Replays a captured request fixture → `ApiCall` line appears|

`settings_example.json` and `tracer/view.py` already exist and need no agent.
`requirements.txt` is created in the keystone (listing all deps) to avoid collisions.

### Risks / honest notes

- **Don't split the backend** into separate writer/server agents — it co-evolves and
  is small; one agent owns Track A.
- **#1 integration risk is API/SSE drift** between Track A and Track B. Mitigation:
  freeze the SSE envelope with a concrete example payload; Track B builds against that
  exact committed fixture.
- **Track C is the highest-uncertainty piece** (streaming SSE reassembly). Isolate and
  time-box it; it must never block the core backend+UI.
- **Simplicity check:** JSONL is justified only because we are building the UI/replay.
  Don't carry storage cost without the benefit.

### Recommended execution sequence

1. **Keystone** (direct) — contract doc, example payload, `requirements.txt`.
2. **Parallel:** Track A + Track B + (optionally) Track C — 3 agents max.
3. **Integration** (direct) — wire UI to the real backend; run a live session through
   actual Claude Code hooks.
4. **Phase 5** last — needs real captured data.
