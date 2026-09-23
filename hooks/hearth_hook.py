#!/usr/bin/env python3
"""Claude Code hook → chert bridge. Installed for every event chert cares about (see
install_hooks.py); Claude Code runs it with the event JSON on stdin.

It never blocks Claude: configured `async`, exits 0, prints nothing. It POSTs the event to
the bridge on localhost; if the bridge is down it spools the event to disk and the bridge
drains the spool when it comes back, so SessionEnd / StopFailure during a bridge restart
are not lost.

Adds three fields the bridge can't get otherwise:
  _pid      the claude process that ran this hook (walked up from our parent), which maps
            straight onto the registry's pid:procStart identity even before the registry
            file exists
  _received wall-clock arrival time
  _tty      the controlling tty, when there is one
Stdlib only, so it starts in ~30 ms.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("HEARTH_HOOK_PORT", "8897"))
SECRET_FILE = Path(os.environ.get("HEARTH_HOOK_SECRET", str(Path.home() / ".claude" / "hearth-hook.secret")))
SPOOL = Path(os.environ.get("HEARTH_HOOK_SPOOL", str(Path.home() / ".claude" / "hearth-events")))


def claude_pid():
    """Walk up from our parent until we hit the claude binary (hooks run via a shell)."""
    pid = os.getppid()
    for _ in range(6):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            return None
        if comm in ("claude", "node", "bun") or comm.startswith("claude"):
            return pid
        if ppid <= 1:
            return None
        pid = ppid
    return None


def main():
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return
    if not isinstance(ev, dict):
        return
    ev["_received"] = time.time()
    ev["_pid"] = claude_pid()
    try:
        ev["_tty"] = os.ttyname(2) if os.isatty(2) else None
    except OSError:
        ev["_tty"] = None
    body = json.dumps(ev).encode()
    try:
        secret = SECRET_FILE.read_text().strip()
    except OSError:
        secret = ""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/hook", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Hearth-Secret": secret})
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read(64)
        return
    except Exception:  # noqa: BLE001 — bridge down: spool, never fail the hook
        pass
    try:
        SPOOL.mkdir(parents=True, exist_ok=True)
        name = f"{time.time():.6f}-{ev.get('hook_event_name', 'event')}-{os.getpid()}.json"
        tmp = SPOOL / (name + ".tmp")
        tmp.write_bytes(body)
        tmp.replace(SPOOL / name)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
