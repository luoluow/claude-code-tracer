#!/usr/bin/env python3
"""Claude Code Tracer — receives hook events via HTTP, logs to JSONL, prints to terminal."""

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = 7355
LOG_DIR = Path.home() / ".claude-tracer"

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


class Handler(BaseHTTPRequestHandler):
    log_file = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        event["_ts"] = datetime.now().isoformat()

        if self.log_file:
            self.log_file.write(json.dumps(event) + "\n")
            self.log_file.flush()

        _print_event(event)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_):
        pass  # suppress default request logs


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


def main():
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"session_{stamp}.jsonl"

    R, B, D = C["RESET"], C["BOLD"], C["DIM"]
    print(f"{B}Claude Code Tracer{R}")
    print(f"Session : {stamp}")
    print(f"Log     : {log_path}")
    print(f"Port    : {PORT}")
    print()

    with open(log_path, "w") as f:
        Handler.log_file = f
        server = HTTPServer(("127.0.0.1", PORT), Handler)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print(f"\nTracer stopped — log saved to {log_path}")


if __name__ == "__main__":
    main()
