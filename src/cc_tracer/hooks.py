"""Install / remove the tracer's HTTP hooks in a Claude Code settings.json."""

import json
import shutil
from pathlib import Path

# The Claude Code events we capture. The server records any event it receives,
# so this list is purely which hooks we register.
EVENTS = [
    "SessionStart", "SessionEnd", "UserPromptSubmit",
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "SubagentStart", "SubagentStop", "PreCompact", "PostCompact", "Stop",
]


def tracer_hooks(url):
    """The hooks block that POSTs every captured event to `url`."""
    return {ev: [{"hooks": [{"type": "http", "url": url}]}] for ev in EVENTS}


def install(settings_path, url):
    """Merge the tracer hooks into settings.json (idempotent). Backs the original
    up to <name>.json.bak once. Returns True if anything was added."""
    settings_path = Path(settings_path)
    current = {}
    if settings_path.exists():
        current = json.loads(settings_path.read_text() or "{}")
        bak = settings_path.with_suffix(".json.bak")
        if not bak.exists():
            shutil.copy(settings_path, bak)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    hooks = current.setdefault("hooks", {})
    added = False
    for event, entries in tracer_hooks(url).items():
        bucket = hooks.setdefault(event, [])
        serialized = json.dumps(bucket)
        for entry in entries:
            if json.dumps(entry) not in serialized:
                bucket.append(entry)
                added = True
    settings_path.write_text(json.dumps(current, indent=2) + "\n")
    return added


def uninstall(settings_path, url):
    """Remove only the HTTP hook entries pointing at `url`. Returns True if any
    were removed. Leaves the user's other hooks untouched."""
    settings_path = Path(settings_path)
    if not settings_path.exists():
        return False
    current = json.loads(settings_path.read_text() or "{}")
    hooks = current.get("hooks", {})
    removed = False
    for event in list(hooks):
        kept_groups = []
        for group in hooks[event]:
            inner = [h for h in group.get("hooks", [])
                     if not (h.get("type") == "http" and h.get("url") == url)]
            if len(inner) != len(group.get("hooks", [])):
                removed = True
            if inner:
                kept_groups.append({**group, "hooks": inner})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    current["hooks"] = hooks
    settings_path.write_text(json.dumps(current, indent=2) + "\n")
    return removed
