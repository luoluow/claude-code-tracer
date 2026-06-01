# cc-tracer (Local Tracer for Claude Code)

[![PyPI](https://img.shields.io/pypi/v/cc-tracer.svg)](https://pypi.org/project/cc-tracer/)
[![Python](https://img.shields.io/pypi/pyversions/cc-tracer.svg)](https://pypi.org/project/cc-tracer/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local, infra-free, raw-fidelity inspector for a **single** Claude Code session — a
*DevTools Network tab for Claude Code*. Install it, point Claude Code at it, and watch
one session's hook events and raw API turns on a live timeline like
![CC Tracer demo](https://github.com/luoluow/claude-code-tracer/blob/master/cc-tracer-demo.gif?raw=true)

## How it captures

A local FastAPI server (127.0.0.1:7355) with two capture tiers:

- **Hook tier** — Claude Code hooks POST to the tracer server. Captures *what Claude
  did*: prompts, Pre/PostToolUse, results, stop reasons.
- **API-proxy tier** — point `ANTHROPIC_BASE_URL` at the tracer; it streams `/v1/*` to
  the real API (plain HTTP in, real HTTPS out — no cert trust needed) and reassembles
  the streaming response into full API turns: system prompt, message context, reasoning
  text, token usage — *what Claude saw and thought*.

## Getting started

Prerequisites: `python3` (3.9+), `pip`, and the `claude` CLI on your `PATH`.

Install the package, then let one command do everything — configure the Claude Code
hooks, start the tracer server, and launch Claude Code routed through it:

```bash
pip install cc-tracer          # or: pip install -e .   (from a clone)
cc-tracer start
```

Then open the tracer UI at <http://127.0.0.1:7355> and use Claude Code as usual. The
tracer **keeps running after you quit Claude Code** so you can keep browsing the
capture — run `cc-tracer stop` when you're done.

What `cc-tracer start` does:

1. Adds the tracer hooks into the **project-local** `.claude/settings.json` (in the
   current directory, so they apply only to this project — not every Claude session;
   idempotent, backs the original up to `.bak` once). Use `--settings PATH` to target a
   different file, e.g. `~/.claude/settings.json` to trace globally.
2. Starts a detached tracer server on `:7355` (hook sink + UI + API proxy), or reuses
   one already running there. The server is left running when Claude exits.
3. Exports ANTHROPIC_BASE_URL=http://127.0.0.1:7355 and runs `claude` (you can pass claude args after `--`). 

Session JSONL is written to `~/.cc-tracer/logs/` (override with `TRACER_LOG_DIR` or
`--log-dir`).

For instance:
```bash
cc-tracer start \
  --log-dir ./logs/ \
  -- -c # -c to continue recent claude session
```

To run **just the server** without launching Claude — e.g. to browse past captures in the UI — use 
```bash
cc-tracer start --server-only # it will config hooks and start the server.

# To start tracing, you can run 
ANTHROPIC_BASE_URL=http://127.0.0.1:7355 claude 

```

### Stopping

```bash
cc-tracer stop    # stop the server AND remove the tracer's hooks
```

Hooks follow the server's lifecycle: `start` adds them and `stop` removes only the
tracer's own entries (your other hooks are left alone). With the server running, you
can also point a Claude session you launch yourself at it with
`export ANTHROPIC_BASE_URL=http://127.0.0.1:7355`.

## When to use this vs. OpenTelemetry

Claude Code emits OpenTelemetry natively, and tools like SigNoz, Grafana, and
LangSmith build on it. They are **aggregate observability** — dashboards, cost/latency
trends, alerting, cross-session correlation across a fleet. If that's your goal, use
them; this tracer doesn't try to.

This tracer targets the thing those tools explicitly aren't: a **local, infra-free,
raw-fidelity inspector for a single session** — think "DevTools Network tab for Claude
Code."

| | This tracer | Native OTel (SigNoz / Grafana / LangSmith) |
| --- | --- | --- |
| Raw API request/response body | ✅ | ❌ (spans/metrics; content redacted by default) |
| Reasoning / thinking text | ✅ | ❌ |
| Tool layer (Pre/Post, Edit diffs) | ✅ | ✅ |
| Backend infra required | none (JSONL + one HTML file) | collector + storage + UI |
| Data stays fully local | ✅ | configurable / often SaaS |
| Cross-session stats & alerting | ❌ | ✅ |

The closest off-the-shelf analog is a manual mitmproxy setup; the base-URL forwarder
avoids that approach's CA forging and `NODE_EXTRA_CA_CERTS` fuss.

### Caveats

- **Proxy ≠ full coverage.** The API proxy only sees HTTP traffic. Transport that
  isn't plain HTTP (e.g. the Agent SDK's IPC/WebSocket) is captured only at the hook
  tier. Native OTel emits regardless of transport.
- **SSE reassembly is schema-coupled.** Rebuilding turns depends on the current
  Anthropic event shape, so it needs upkeep when the API evolves — a maintenance cost
  the OTel-based tools don't carry.
