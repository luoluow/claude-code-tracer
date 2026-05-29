# Claude Code Tracer

This project is to build a simple local tracer for claude code for learning, debugging and trouble-shooting purpose.

## How it captures

Two tiers, so you can run either or both:

- **Hook tier** — Claude Code hooks POST to the tracer server. Captures *what Claude
  did*: prompts, Pre/PostToolUse, results, stop reasons.
- **Forwarder tier** — point `ANTHROPIC_BASE_URL` at the local proxy (plain HTTP in,
  real HTTPS out — no cert trust needed). Taps and reassembles the streaming response
  into full API turns: system prompt, message context, reasoning text, token usage —
  *what Claude saw and thought*.

## Getting started

Prerequisites: `python3`, `pip`, and the `claude` CLI on your `PATH`.

One command does everything — installs deps, configures the Claude Code hooks,
starts the tracer server + forwarder, and launches Claude Code routed through them:

```bash
./trace.sh
```

Then open the tracer UI at <http://127.0.0.1:7355> and use Claude Code as usual. When
you quit Claude Code, the tracer and forwarder shut down automatically.

What `trace.sh` does:

1. Installs `requirements.txt` if FastAPI/uvicorn are missing.
2. Merges the tracer hooks into `~/.claude/settings.json` (idempotent; backs the
   original up to `settings.json.bak` once). Override the target with
   `CLAUDE_SETTINGS=/path/to/settings.json`.
3. Starts the tracer server (`:7355`) and forwarder (`:7356`), logging to `temp/`.
4. Exports `ANTHROPIC_BASE_URL` at the forwarder and runs `claude`.

### Running the pieces separately

The hooks stay configured after `trace.sh` exits, so the tracer keeps capturing the
tool tier whenever its server is running. To run parts by hand:

```bash
./start_tracer.sh           # tracer server only (hook tier + UI)
./start_forwarder.sh        # API forwarder only
./start_claude_traced.sh    # launch Claude Code through an already-running forwarder
```

To stop tracing entirely, restore `~/.claude/settings.json` from the `.bak` file.

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

- **Forwarder ≠ full coverage.** The forwarder only sees HTTP traffic. Transport that
  isn't plain HTTP (e.g. the Agent SDK's IPC/WebSocket) is captured only at the hook
  tier. Native OTel emits regardless of transport.
- **SSE reassembly is schema-coupled.** Rebuilding turns depends on the current
  Anthropic event shape, so it needs upkeep when the API evolves — a maintenance cost
  the OTel-based tools don't carry.
