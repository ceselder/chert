#!/usr/bin/env python3
"""Install (or refresh) chert's hook set in ~/.claude/settings.json and create the shared
secret the hook uses to talk to the bridge. Idempotent: existing hooks for other purposes
are kept; chert's entries are recognised by the script path and replaced.

New sessions pick the hooks up at startup; sessions already running keep the hook set they
started with (Claude Code snapshots hooks at launch).
"""
import json
import os
import secrets
import sys
from pathlib import Path

HOOK = str(Path(__file__).resolve().parent / "hearth_hook.py")
SETTINGS = Path(os.environ.get("CLAUDE_SETTINGS", str(Path.home() / ".claude" / "settings.json")))
SECRET_FILE = Path(os.environ.get("HEARTH_HOOK_SECRET", str(Path.home() / ".claude" / "hearth-hook.secret")))
# event -> matcher (None = always fires / no matcher supported)
EVENTS = {
    "SessionStart": None,
    "SessionEnd": None,
    "Stop": None,
    "StopFailure": None,
    "Notification": "permission_prompt|elicitation_dialog|elicitation_url_dialog|idle_prompt",
    "SubagentStart": None,
    "SubagentStop": None,
    "PreCompact": None,
    "PostCompact": None,
}


def entry(matcher):
    e = {"hooks": [{"type": "command", "command": HOOK, "async": True, "timeout": 10}]}
    if matcher:
        e["matcher"] = matcher
    return e


def main():
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(24) + "\n")
        os.chmod(SECRET_FILE, 0o600)
        print(f"created {SECRET_FILE}")
    d = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    hooks = d.setdefault("hooks", {})
    changed = 0
    for event, matcher in EVENTS.items():
        lst = [e for e in hooks.get(event, [])
               if not any(h.get("command") == HOOK for h in e.get("hooks", []))]
        lst.append(entry(matcher))
        if hooks.get(event) != lst:
            hooks[event] = lst
            changed += 1
    tmp = SETTINGS.with_name(SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n")
    tmp.replace(SETTINGS)
    print(f"{SETTINGS}: {changed} hook event(s) written, {len(EVENTS)} total for chert")
    if "--uninstall" in sys.argv:
        for event in list(hooks):
            hooks[event] = [e for e in hooks[event]
                            if not any(h.get("command") == HOOK for h in e.get("hooks", []))]
            if not hooks[event]:
                del hooks[event]
        tmp.write_text(json.dumps(d, indent=2) + "\n")
        tmp.replace(SETTINGS)
        print("chert hooks removed")


if __name__ == "__main__":
    main()
