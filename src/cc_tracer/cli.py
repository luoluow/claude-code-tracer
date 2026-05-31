"""cc-tracer command-line interface.

  start   install hooks (if needed), start a detached server, run Claude through it
  stop    stop the server and remove its hooks

(`_serve` is an internal, hidden command: the bare server process that `start`
spawns detached. Use `start` instead.)
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import config, hooks as hooks_mod

# Project-local by default so hooks only apply to Claude Code run in this project
# (not every session on the machine). Override with --settings ~/.claude/settings.json.
DEFAULT_SETTINGS = ".claude/settings.json"


def _wait_port(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.1)
    return False


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _hook_url(port):
    return f"http://127.0.0.1:{port}/event"


def cmd_serve(a):
    """Internal `_serve`: the bare server process (writes a pidfile so `stop`
    can find it). Spawned detached by `start`; not meant to be run directly."""
    if a.log_dir:
        os.environ["TRACER_LOG_DIR"] = str(a.log_dir)
    import uvicorn
    from .server import app  # imported after env is set so LOG_DIR picks it up
    pid_file = config.pid_file(a.port)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    try:
        uvicorn.run(app, host=a.host, port=a.port)
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass
    return 0


def cmd_start(a):
    base = f"http://127.0.0.1:{a.port}"
    settings = Path(a.settings).expanduser()
    log_dir = Path(a.log_dir) if a.log_dir else config.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    # 1. Ensure the tracer hooks are installed (idempotent).
    added = hooks_mod.install(settings, _hook_url(a.port))
    print(f"Hooks {'installed in' if added else 'already in'} {settings}")

    # 2. Start a detached server (or reuse one already on the port). Detached +
    #    own session so quitting Claude leaves the tracer running.
    server = None
    if _port_open(a.port):
        print(f"Reusing tracer already running at {base}")
    else:
        env = os.environ.copy()
        env["TRACER_LOG_DIR"] = str(log_dir)
        server_log = open(log_dir / "server.log", "a")
        server = subprocess.Popen(
            [sys.executable, "-m", "cc_tracer", "_serve", "--host", "127.0.0.1", "--port", str(a.port)],
            env=env, stdout=server_log, stderr=server_log, start_new_session=True,
        )
        if not _wait_port(a.port):
            print(f"cc-tracer server failed to start; see {log_dir / 'server.log'}", file=sys.stderr)
            server.terminate()
            return 1
        print(f"Started tracer at {base}  (log: {log_dir / 'server.log'})")

    print(f"Tracer UI: {base}")
    stop = "cc-tracer stop" + ("" if a.port == config.DEFAULT_PORT else f" --port {a.port}")

    # 3. With --server-only, leave the detached server running and exit.
    if a.server_only:
        print(f"  point Claude at it:  export ANTHROPIC_BASE_URL={base}")
        print(f"  stop it (and remove hooks) with:  {stop}")
        return 0

    # Otherwise run Claude Code routed through the tracer.
    run_env = os.environ.copy()
    run_env["ANTHROPIC_BASE_URL"] = base
    claude_args = [x for x in a.claude_args if x != "--"]
    try:
        rc = subprocess.call(["claude", *claude_args], env=run_env)
    except FileNotFoundError:
        print("claude not found on PATH. The tracer is running; start Claude yourself "
              f"with: export ANTHROPIC_BASE_URL={base}", file=sys.stderr)
        rc = 127
    except KeyboardInterrupt:
        rc = 130

    # Leave the server running after Claude exits; `stop` tears it down.
    print(f"\nTracer still running at {base}")
    print(f"  stop it (and remove hooks) with:  {stop}")
    return rc


def cmd_stop(a):
    if a.log_dir:
        os.environ["TRACER_LOG_DIR"] = str(a.log_dir)

    # 1. Stop the server (via its pidfile), if one is running.
    pid_file = config.pid_file(a.port)
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except ValueError:
            print(f"Corrupt pidfile; removing {pid_file}")
            pid_file.unlink()
            pid = None
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):     # wait for shutdown to free the port
                    if not _port_open(a.port):
                        break
                    time.sleep(0.1)
                print(f"Stopped cc-tracer (pid {pid}) on port {a.port}.")
            except ProcessLookupError:
                print(f"Tracer (pid {pid}) wasn't running.")
            try:
                pid_file.unlink()
            except OSError:
                pass
    else:
        msg = f"No running tracer for port {a.port}."
        if _port_open(a.port):
            msg += f" (Something else is listening on {a.port}.)"
        print(msg)

    # 2. Remove the tracer hooks (only the entries pointing at our URL).
    settings = Path(a.settings).expanduser()
    url = _hook_url(a.port)
    removed = hooks_mod.uninstall(settings, url)
    print(f"{'Removed' if removed else 'No'} tracer hooks ({url}) in {settings}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="cc-tracer", description="A local inspector for a single Claude Code session.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="install hooks, start a detached server, run Claude through it")
    s.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    s.add_argument("--settings", default=DEFAULT_SETTINGS, help="settings.json for hooks (default: project-local .claude/settings.json)")
    s.add_argument("--log-dir", type=Path, help="override $TRACER_LOG_DIR")
    s.add_argument("--server-only", action="store_true",
                   help="just start the server (don't launch Claude)")
    s.add_argument("claude_args", nargs=argparse.REMAINDER, help="args passed through to `claude`")
    s.set_defaults(func=cmd_start)

    st = sub.add_parser("stop", help="stop the tracer server and remove its hooks")
    st.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    st.add_argument("--settings", default=DEFAULT_SETTINGS, help="settings.json to remove hooks from (default: project-local)")
    st.add_argument("--log-dir", type=Path, help="where start recorded its pidfile")
    st.set_defaults(func=cmd_stop)

    # Internal: the bare server process that `start` spawns detached.
    sv = sub.add_parser("_serve", help=argparse.SUPPRESS)
    sv.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--log-dir", type=Path)
    sv.set_defaults(func=cmd_serve)

    a = p.parse_args(argv)
    return a.func(a) or 0
