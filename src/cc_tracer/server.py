#!/usr/bin/env python3
"""CC Tracer — FastAPI app: receives hook events, logs to per-session JSONL,
broadcasts via SSE, serves the web UI."""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import log_dir

# Per-session JSONL location (honors $TRACER_LOG_DIR; see config.log_dir).
LOG_DIR = log_dir()
# The web UI ships inside the package, next to this module.
STATIC_DIR = Path(__file__).resolve().parent / "static"

C = {
    "PreToolUse":       "\033[33m",
    "PostToolUse":      "\033[32m",
    "UserPromptSubmit": "\033[34m",
    "Stop":             "\033[35m",
    "SessionStart":     "\033[36m",
    "RESET":            "\033[0m",
    "DIM":              "\033[2m",
    "BOLD":             "\033[1m",
}

subscribers: set[asyncio.Queue] = set()

# The hook session_id of the most recent hook event. API requests carry no
# session key, so we attribute each ApiCall to whichever hook session is
# currently active — exact for a single session, best-effort if several run at
# once through one proxy.
active_session = None

# Anthropic API proxy (merged forwarder). Claude Code points ANTHROPIC_BASE_URL
# at this server; /v1/* is streamed to the real API and tapped for ApiCall events.
# ApiCall grouping: x-session-id header → active hook session → api-<run> fallback.
RUN_ID = time.strftime("api-%Y%m%d_%H%M%S")
UPSTREAM_URL = os.environ.get("ANTHROPIC_UPSTREAM_URL", "https://api.anthropic.com")
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

app = FastAPI()
_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))


async def record_event(event):
    """Inject a timestamp, append to the session's JSONL, print, and broadcast."""
    event["_ts"] = datetime.now().isoformat()
    session_id = event.get("session_id") or "unknown"
    line = json.dumps(event, separators=(",", ":"))
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / f"{session_id}.jsonl", "a") as f:
        f.write(line + "\n")
    _print_event(event)
    for q in subscribers:
        q.put_nowait(line)


@app.post("/event")
async def post_event(request: Request):
    event = await request.json()
    sid = event.get("session_id")
    if sid and not sid.startswith("api-"):
        global active_session
        active_session = sid          # track the live hook session for ApiCall attribution
    await record_event(event)
    return {}


@app.get("/sessions")
async def get_sessions():
    sessions = []
    for path in LOG_DIR.glob("*.jsonl"):
        events = _read_events(path)
        if not events:
            continue
        model = cwd = None
        for ev in events:
            if ev.get("hook_event_name") == "SessionStart":
                model = ev.get("model")
                cwd = ev.get("cwd")
                break
        if model is None:
            model = next((ev["model"] for ev in events if "model" in ev), None)
        if cwd is None:
            cwd = next((ev["cwd"] for ev in events if "cwd" in ev), None)
        sessions.append({
            "session_id": path.stem,
            "started_at": events[0].get("_ts"),
            "ended_at": events[-1].get("_ts"),
            "cwd": cwd,
            "model": model,
            "event_count": len(events),
        })
    sessions.sort(key=lambda s: s["started_at"] or "", reverse=True)
    return sessions


@app.get("/events/{session_id}")
async def get_events(session_id: str):
    return _read_events(LOG_DIR / f"{session_id}.jsonl")


@app.get("/stream")
async def stream():
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)

    async def gen():
        try:
            while True:
                line = await q.get()
                yield f"event: trace\ndata: {line}\n\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/export/{session_id}")
async def export(session_id: str):
    path = LOG_DIR / f"{session_id}.jsonl"
    return PlainTextResponse(path.read_text() if path.exists() else "")


def _read_events(path: Path):
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _print_event(event):
    kind = event.get("hook_event_name", "unknown")
    tool = event.get("tool_name", "")
    ts = event.get("_ts", "")[:19].replace("T", " ")
    color = C.get(kind, "")
    R, D = C["RESET"], C["DIM"]

    header = f"{D}{ts}{R}  {color}{kind}{R}"
    if tool:
        header += f"  {D}{tool}{R}"
    print(header)

    detail = _detail(kind, event)
    if detail:
        print(f"          {D}↳ {detail}{R}")


def _detail(kind, event):
    if kind == "PreToolUse":
        inp = event.get("tool_input", {})
        tool = event.get("tool_name", "")
        if tool == "Bash":
            return _clip(inp.get("command", ""))
        if tool in ("Read", "Write", "Edit"):
            return inp.get("file_path", "")
        return _clip(json.dumps(inp))
    if kind == "PostToolUse":
        resp = str(event.get("tool_response", ""))
        return _clip(resp.replace("\n", " "))
    if kind == "UserPromptSubmit":
        return _clip(event.get("prompt", ""))
    if kind == "Stop":
        return event.get("stop_reason", "")
    return ""


def _clip(s, n=120):
    return (s[:n] + "…") if len(s) > n else s


# --- Anthropic API proxy (merged forwarder) -------------------------------

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_anthropic(path: str, request: Request):
    """Stream a Claude Code API request to the real API, tapping SSE for ApiCall."""
    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in ("host", "accept-encoding", "content-length")
    }
    # Force identity so the SSE we tap is plaintext, not gzip/br. The client gets
    # an uncompressed response over the localhost hop — bandwidth is irrelevant.
    headers["Accept-Encoding"] = "identity"

    url = httpx.URL(UPSTREAM_URL).join("/v1/" + path)
    if request.url.query:
        url = url.copy_with(query=request.url.query.encode())

    upstream_req = _client.build_request(request.method, url, headers=headers, content=body)
    try:
        upstream = await _client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        print(f"[tracer] upstream error: {e}")
        return Response("Upstream connection failed", status_code=502)

    is_sse = "text/event-stream" in upstream.headers.get("content-type", "")
    tap = bytearray()

    async def body_stream():
        try:
            async for chunk in upstream.aiter_raw():
                if is_sse:
                    tap.extend(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            if request.method == "POST" and is_sse and tap:
                await _record_apicall(request, body, bytes(tap))

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in HOP_BY_HOP
        and k.lower() not in ("content-length", "content-encoding")
    }
    return StreamingResponse(body_stream(), status_code=upstream.status_code,
                             headers=resp_headers)


async def _record_apicall(request, request_body, sse_bytes):
    try:
        req = json.loads(request_body or b"{}")
    except ValueError:
        req = {}
    response, usage = _reassemble_sse(sse_bytes)
    if not response.get("content") and not usage:
        print(f"[tracer] WARNING: empty reassembly; first bytes: {sse_bytes[:80]!r}")
    await record_event({
        "session_id": request.headers.get("x-session-id") or active_session or RUN_ID,
        "hook_event_name": "ApiCall",
        "request": req,
        "response": response,
        "usage": usage,
    })


def _reassemble_sse(sse_bytes):
    """Rebuild the final assistant turn from an Anthropic SSE stream.

    Returns (response, usage) where response mirrors a Messages API result
    (role, model, stop_reason, content blocks) and usage holds token counts.
    """
    text = sse_bytes.decode("utf-8", "replace").replace("\r\n", "\n")
    response = {"role": "assistant", "content": []}
    usage = {}
    blocks = {}  # index -> {"type": ..., accumulator}

    for raw in text.split("\n\n"):
        data = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            evt = json.loads(data)
        except ValueError:
            continue

        etype = evt.get("type")
        if etype == "message_start":
            msg = evt.get("message", {})
            response["id"] = msg.get("id")
            response["model"] = msg.get("model")
            response["role"] = msg.get("role", "assistant")
            usage.update(msg.get("usage", {}))
        elif etype == "content_block_start":
            blocks[evt["index"]] = dict(evt.get("content_block", {}))
            blocks[evt["index"]].setdefault("_text", "")
            blocks[evt["index"]].setdefault("_json", "")
        elif etype == "content_block_delta":
            blk = blocks.setdefault(evt["index"], {"_text": "", "_json": ""})
            d = evt.get("delta", {})
            if d.get("type") == "text_delta":
                blk["_text"] += d.get("text", "")
            elif d.get("type") == "thinking_delta":
                blk["_text"] += d.get("thinking", "")
            elif d.get("type") == "input_json_delta":
                blk["_json"] += d.get("partial_json", "")
        elif etype == "message_delta":
            delta = evt.get("delta", {})
            if "stop_reason" in delta:
                response["stop_reason"] = delta["stop_reason"]
            if "stop_sequence" in delta:
                response["stop_sequence"] = delta["stop_sequence"]
            usage.update(evt.get("usage", {}))

    for idx in sorted(blocks):
        response["content"].append(_finalize_block(blocks[idx]))
    return response, usage


def _finalize_block(blk):
    btype = blk.get("type")
    if btype in ("text", "thinking"):
        out = {"type": btype}
        out["thinking" if btype == "thinking" else "text"] = blk["_text"]
        return out
    if btype == "tool_use":
        out = {"type": "tool_use", "id": blk.get("id"), "name": blk.get("name")}
        try:
            out["input"] = json.loads(blk["_json"]) if blk["_json"] else {}
        except ValueError:
            out["input"] = {"_raw": blk["_json"]}
        return out
    return {k: v for k, v in blk.items() if not k.startswith("_")}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
