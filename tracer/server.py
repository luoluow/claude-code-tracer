#!/usr/bin/env python3
"""Claude Code Tracer — FastAPI app: receives hook events, logs to per-session JSONL,
broadcasts via SSE, serves the web UI."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

PORT = 7355
LOG_DIR = Path.home() / ".claude-tracer"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

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

app = FastAPI()


@app.post("/event")
async def post_event(request: Request):
    event = await request.json()
    event["_ts"] = datetime.now().isoformat()

    session_id = event.get("session_id") or "unknown"
    line = json.dumps(event, separators=(",", ":"))
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / f"{session_id}.jsonl", "a") as f:
        f.write(line + "\n")

    _print_event(event)

    for q in subscribers:
        q.put_nowait(line)

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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    R, B = C["RESET"], C["BOLD"]
    print(f"{B}Claude Code Tracer{R}  —  http://127.0.0.1:{PORT}\n")
    uvicorn.run(app, host="127.0.0.1", port=PORT)
