#!/usr/bin/env python3
"""View and filter Claude Code tracer sessions.

Usage:
  view.py list                     list all recorded sessions
  view.py show [N] [EventType]     show session N (default: latest), optionally filtered
"""

import json
import os
import sys
from pathlib import Path

# Mirror server.py: override with TRACER_LOG_DIR, else temp/logs under the project root.
LOG_DIR = Path(
    os.environ.get("TRACER_LOG_DIR")
    or Path(__file__).resolve().parent.parent / "temp" / "logs"
)

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


def list_sessions():
    files = sorted(LOG_DIR.glob("session_*.jsonl"), reverse=True)
    if not files:
        print(f"No sessions in {LOG_DIR}")
        return
    R, B, D = C["RESET"], C["BOLD"], C["DIM"]
    print(f"{B}Sessions in {LOG_DIR}{R}")
    for i, f in enumerate(files):
        count = sum(1 for _ in f.open())
        size = f.stat().st_size
        print(f"  [{i}]  {f.stem}  {D}{count} events  {size} bytes{R}")


def show_session(path, filter_type=None):
    R, D = C["RESET"], C["DIM"]
    for line in path.open():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("hook_event_name", "unknown")
        if filter_type and kind != filter_type:
            continue

        color = C.get(kind, "")
        ts = event.get("_ts", "")[:19].replace("T", " ")
        tool = event.get("tool_name", "")

        print(f"{D}{ts}{R}  {color}{kind}{R}{'  ' + D + tool + R if tool else ''}")

        # Best-effort single-line detail per event type
        detail = None
        if kind == "PreToolUse":
            inp = event.get("tool_input", {})
            detail = inp.get("command") or inp.get("file_path") or json.dumps(inp)
        elif kind == "PostToolUse":
            detail = str(event.get("tool_response", "")).replace("\n", " ")
        elif kind == "UserPromptSubmit":
            detail = event.get("prompt", "")

        if detail:
            preview = detail[:120] + ("…" if len(detail) > 120 else "")
            print(f"          {D}↳ {preview}{R}")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "list"

    if cmd == "list":
        list_sessions()
        return

    if cmd == "show":
        files = sorted(LOG_DIR.glob("session_*.jsonl"), reverse=True)
        if not files:
            print("No sessions found.")
            return
        idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        filter_type = args[2] if len(args) > 2 else (args[1] if len(args) > 1 and not args[1].isdigit() else None)
        show_session(files[idx], filter_type)
        return

    print(__doc__)


if __name__ == "__main__":
    main()
