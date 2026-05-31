"""Shared configuration: where session JSONL lives, default port."""

import os
from pathlib import Path

DEFAULT_PORT = 7355


def log_dir() -> Path:
    """Directory holding per-session JSONL. Override with $TRACER_LOG_DIR;
    defaults to ~/.cc-tracer/logs so sessions land in one predictable place."""
    return Path(os.environ.get("TRACER_LOG_DIR") or Path.home() / ".cc-tracer" / "logs")


def pid_file(port: int) -> Path:
    """Where `serve` records its PID so `stop` can find it (per port, in log_dir)."""
    return log_dir() / f"cc-tracer-{port}.pid"
