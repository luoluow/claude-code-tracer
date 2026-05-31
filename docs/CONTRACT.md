# Contract (frozen)

This file is the shared, **read-only** contract for all tracer tracks. Agents build
against this; do not change it without coordinating, because other tracks assume it.

## 1. Event schema

An event is the raw Claude Code hook payload with one injected field, `_ts`
(ISO-8601 local timestamp, added by the server on receipt).

Common fields (present on most events):

```json
{
  "session_id": "abc123",
  "hook_event_name": "PreToolUse",
  "cwd": "/path/to/project",
  "permission_mode": "default",
  "_ts": "2026-05-28T17:42:03"
}
```

Event-specific fields:

| hook_event_name    | Adds                                  |
| ------------------ | ------------------------------------- |
| `SessionStart`     | `source`, `model`                     |
| `UserPromptSubmit` | `prompt`                              |
| `PreToolUse`       | `tool_name`, `tool_input` (object)    |
| `PostToolUse`      | `tool_name`, `tool_response`          |
| `Stop`             | `stop_reason`                         |
| `ApiCall` (Phase 4)| `request`, `response`, `usage` (token counts) |

`tool_input` shapes the UI cares about:
- Bash → `{ "command": "..." }`
- Read → `{ "file_path": "...", "offset": N, "limit": N }`
- Edit → `{ "file_path": "...", "old_string": "...", "new_string": "..." }`
- Write → `{ "file_path": "...", "content": "..." }`
- Task → `{ "subagent_type": "...", "prompt": "..." }`

`tool_response` is free-form (string or object); the UI renders it best-effort and
highlights errors (e.g. non-zero exit, `FAIL`, `error`).

## 2. JSONL storage format

- One directory: `temp/logs/` under the project root (override with `TRACER_LOG_DIR`)
- One file per session: `{TRACER_LOG_DIR}/{session_id}.jsonl`
- One event per line, exactly the JSON object from §1 (compact, no pretty-print).
- The tracer server is the **single writer**; it routes each incoming event to the
  file named by its `session_id`. If `session_id` is missing, use `"unknown"`.

## 3. HTTP / SSE API

Base: `http://127.0.0.1:7355`

### `POST /event`
Receives a hook payload (JSON body). Server injects `_ts`, appends to the session's
JSONL file, and broadcasts to SSE subscribers. Responds `200` with body `{}`.

### `GET /sessions`
Returns session metadata derived from the JSONL files:

```json
[
  {
    "session_id": "abc123",
    "started_at": "2026-05-28T17:42:01",
    "ended_at":   "2026-05-28T17:42:35",
    "cwd":        "/path/to/project",
    "model":      "claude-opus-4-8",
    "event_count": 11
  }
]
```

`started_at`/`ended_at` = `_ts` of first/last event. `model`/`cwd` from the
`SessionStart` event if present, else first event that has them, else `null`.
Sorted most-recent first.

### `GET /events/{session_id}`
Returns all events for the session as a JSON array, in file order.

### `GET /stream`
Server-Sent Events. Emits every new event received (all sessions). The client filters
by `session_id`. Each message:

```
event: trace
data: {"session_id":"abc123","hook_event_name":"PreToolUse",...,"_ts":"..."}

```

(One `data:` line containing the compact event JSON, terminated by a blank line.)

### `GET /export/{session_id}`
Returns the raw JSONL file as `text/plain` (for download).

### `GET /`
Serves `static/index.html`. Static assets served from `static/`.

### `ANY /v1/{path}` — Anthropic API proxy (merged forwarder)
Claude Code points `ANTHROPIC_BASE_URL` at this server. Any `/v1/*` request is
streamed to `ANTHROPIC_UPSTREAM_URL` (default `https://api.anthropic.com`) and the
response is streamed straight back. `Accept-Encoding` is forced to `identity` so the
tapped SSE is plaintext. On a streaming POST, the server reassembles the SSE into a
final turn and records one `ApiCall` event (§1) **in-process**. Session attribution,
in order: an `x-session-id` request header if present → the **active hook session**
(the `session_id` of the most recent hook event, so API turns merge into the live
session) → an `api-<timestamp>` fallback when no hooks are running. The client→server
hop is plain HTTP (no cert); the server→API hop is normal HTTPS.

> The active-session attribution is exact for a single session; with several
> sessions sharing one proxy it is best-effort (temporal) and may misattribute.

## 4. SSE envelope — concrete example

A subscriber to `GET /stream` receives, for the Bash PreToolUse below:

```
event: trace
data: {"session_id":"abc123","hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"npm test"},"cwd":"/p","_ts":"2026-05-28T17:42:03"}

```

The UI parses `data` as JSON and appends it to the timeline iff its `session_id`
matches the currently selected session (or always, in "all" mode).
