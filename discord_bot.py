#!/usr/bin/env python3
"""Discord bridge for the hearth harness — a self-hosted "Claude Tag for Discord",
Outer Wilds flavoured: every Claude Code session on this box is a traveler, this bot is
the signalscope that tracks them.

Every live Claude Code session gets its own thread in one channel:
  - each claude posts under its OWN name + avatar (channel webhook, per-message
    username) — threads read like a room full of different agents
  - the thread's intro message is EDITED in place with live status (edits don't
    ping — a quiet thread means it's working, same trick Claude Tag uses)
  - type in a thread -> pasted into that session's tmux pane (steer-by-reply)
  - session enters `waiting` -> you get pinged AND the prompt arrives as BUTTONS:
    the numbered options on its screen, plus esc / enter / arrows — one tap answers it
  - session exits -> thread archived; if it *crashed* the thread stays open with a
    `!revive` hint (the registry's leftover file is how we tell the two apart)
  - the box rebooted -> every session that was live is revived automatically
    (REVIVE_ON_BOOT=1), each into its own tmux window, history intact

Persistence bits that make the above hold:
  - bot_state.json is written atomically (tmp + fsync + rename) and falls back to a
    .bak on corruption — a full disk used to truncate it to 0 bytes, and the next
    start then re-threaded every session
  - ended sessions stay in state for ENDED_KEEP_DAYS so `!revive` works after a
    bridge restart, not just until one
  - a disk watchdog posts when free space gets low, names the biggest cold
    directories, and hands you `!offload <dir>` (verified copy to S3, then delete)
  - ash_twin.py (S3) backs up transcripts + config hourly via a systemd timer

Multi-agent extras:
  - #claude-chat channel is bridged two-way to the cchat.py bus
    (~/shared/claude_chat/msgs.jsonl): claude<->claude coordination shows up live
    in Discord, and anything you type there lands on the bus.
  - `!all <msg>` in the main channel broadcasts to every live session.

Thread commands: !screen  !key <esc|up|down|tab|shift-tab|space|1-9|enter>  !mute  !unmute
                 !revive [force|fork]  !kill [hard|delete]  !yolo <dur>|off  !help
Channel commands: !sessions  !all <msg>  !cleanup [delete]  !revive all
                  !disk  !offload <dir> [confirm]  !restore <dir> [confirm]  !backup  !s3

Config via env (systemd loads .env): DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID,
optional DISCORD_CHAT_CHANNEL_ID (default: find-or-create #claude-chat),
DISCORD_OWNER_ID (defaults to guild owner), POLL_SECS (default 4). See .env.example.
"""
import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
import urllib.parse
from collections import Counter, deque
from pathlib import Path

import discord
from aiohttp import web

import app as checkin

# .env first: ash_twin reads S3_BUCKET etc. at import time, so loading it afterwards would
# silently disable S3 for anyone running `python discord_bot.py` outside systemd.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

try:
    import ash_twin
except Exception as _e:  # noqa: BLE001 — S3 tooling is optional; the bridge must still run
    ash_twin = None
    print(f"ash_twin unavailable ({_e!r}) — disk/S3 commands disabled", flush=True)

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or 0)
CHAT_CHANNEL_ID = int(os.environ.get("DISCORD_CHAT_CHANNEL_ID", "0") or 0)
# #all-claudes: EVERY message typed there is delivered to every live, reachable claude — the
# `!all <msg>` broadcast without the prefix. Default: find-or-create a channel named all-claudes.
BROADCAST_CHANNEL_ID = int(os.environ.get("DISCORD_BROADCAST_CHANNEL_ID", "0") or 0)
BROADCAST_CHANNEL_NAME = os.environ.get("DISCORD_BROADCAST_CHANNEL_NAME", "all-claudes")
# A plain message in #all-claudes is a QUESTION TO EVERY CLAUDE: it is fanned out, each claude's
# next reply is collected from its transcript (up to ASK_COLLECT_SECS), the bundle is written to
# ASK_DIR/round-<stamp>.md and handed to a persistent summarizer session (ASK_HUB_NAME) whose
# replies are mirrored back into #all-claudes. It follows up with individual claudes over Claude
# Code's own cross-session SendMessage. `!all <msg>` there is the old plain broadcast.
ASK_COLLECT_SECS = int(os.environ.get("ASK_COLLECT_SECS", "240"))
ASK_HUB_NAME = os.environ.get("ASK_HUB_NAME", "all-claudes-hub")
ASK_DIR = Path(os.environ.get("ASK_DIR", str(Path.home() / "shared" / "all-claudes")))
ASK_REPLY_MAX = 2500      # chars of each claude's reply kept in the round brief
OWNER_ID = int(os.environ.get("DISCORD_OWNER_ID", "0") or 0)
POLL_SECS = float(os.environ.get("POLL_SECS", "4"))
# Where [dashboard] links point. The default is the dashboard on this machine's loopback;
# set DASHBOARD_URL to wherever you expose it (behind auth) so links work from your phone.
DASHBOARD = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8899/claudes").rstrip("/")
STATE_FILE = Path(__file__).resolve().parent / "bot_state.json"
CHAT_LOG = Path(os.environ.get(
    "CHAT_LOG", str(Path.home() / "shared" / "claude_chat" / "msgs.jsonl")))
WEBHOOK_NAME = "claude-bridge"
MAX_MSGS_PER_TICK = 10   # long replies were being cut off mid-answer at 6
CHUNK = 1900
# @mention -> spawn a brand new claude in a fresh tmux window
PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", str(Path.home())))
SPAWN_FLAGS = os.environ.get(
    "SPAWN_FLAGS", "--allow-dangerously-skip-permissions --effort xhigh")
TMUX_SESSION = os.environ.get("TMUX_SESSION", "0")
SPAWN_TIMEOUT = 75
# Ended sessions stay in state this long so `!revive` in their thread keeps working
# across bridge restarts (they used to be dropped at every startup).
ENDED_KEEP_DAYS = float(os.environ.get("ENDED_KEEP_DAYS", "14"))
# After a reboot every session that was live is resumed into tmux, history intact.
REVIVE_ON_BOOT = os.environ.get("REVIVE_ON_BOOT", "1") == "1"
# A session that vanished without a clean exit (crash/OOM/kill -9) is resumed too.
# Off by default: a crash loop would just keep resuming, and a deliberate kill -9
# would come straight back.
REVIVE_ON_CRASH = os.environ.get("REVIVE_ON_CRASH", "0") == "1"
# Watcher: announce every NEW session (and every crash / clean end) as one line in the
# main channel, with a link to its thread. Discord only shows a bare "started a thread"
# system line otherwise, so a claude launched from a terminal was easy to miss.
ANNOUNCE_NEW = os.environ.get("ANNOUNCE_NEW", "0") == "1"
# The quiet alternative (default on): ONE pinned board message in the main channel, edited in
# place whenever the fleet changes. Edits never notify, so arrivals and departures show up
# without a ping. Crashes still post a real message.
BOARD = os.environ.get("BOARD", "1") == "1"
BOARD_MIN_SECS = 20
HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"
RESUME_PAGE = 15
# ---------- Feldspar: the deep-review expedition ----------
# "let feldspar look into this" sends two independent frontier reviewers at a target: the
# latest Claude at max effort as a real tmux session (it fans out to many subagents) and
# the latest OpenAI model headless through codex. Both write into one report folder.
FELDSPAR_CLAUDE_MODEL = os.environ.get("FELDSPAR_CLAUDE_MODEL", "fable")
FELDSPAR_CLAUDE_EFFORT = os.environ.get("FELDSPAR_CLAUDE_EFFORT", "max")
FELDSPAR_OPENAI_MODEL = os.environ.get("FELDSPAR_OPENAI_MODEL", "gpt-6-astra")
# gpt-6-astra efforts: low … xhigh, max, ultra ("maximum reasoning with automatic task
# delegation" — codex's own agent fan-out, the counterpart of the Claude side's agent teams).
FELDSPAR_OPENAI_EFFORT = os.environ.get("FELDSPAR_OPENAI_EFFORT", "ultra")
FELDSPAR_TIMEOUT = int(os.environ.get("FELDSPAR_TIMEOUT", "10800"))  # codex wall clock, seconds
# codex sandbox for the OpenAI reviewer. `read-only` needs bubblewrap with rights to user
# namespaces; on Ubuntu 24.04 that means the AppArmor profile in sandbox/ (see README). If
# the sandbox dies at startup the run is retried unsandboxed (the model is still told
# read-only) unless FELDSPAR_CODEX_UNSANDBOXED_FALLBACK=0.
FELDSPAR_CODEX_SANDBOX = os.environ.get("FELDSPAR_CODEX_SANDBOX", "read-only")
FELDSPAR_CODEX_UNSANDBOXED_FALLBACK = os.environ.get("FELDSPAR_CODEX_UNSANDBOXED_FALLBACK", "1") == "1"
# ---------- Astra: a GPT-6 (codex) session as a Discord thread ----------
# `!astra [dir] <prompt>` / `/astra` opens a thread bound to a codex thread. Every message in
# it runs one headless `codex exec resume <thread> --json` turn; the events feed an activity
# card edited in place and the final answer is posted. The codex thread id is persisted in
# state["_astra"], so the conversation survives bridge restarts.
ASTRA_MODEL = os.environ.get("ASTRA_MODEL", "gpt-6-astra")
ASTRA_EFFORT = os.environ.get("ASTRA_EFFORT", "xhigh")
ASTRA_SANDBOX = os.environ.get("ASTRA_SANDBOX", "workspace-write")  # danger-full-access when bwrap can't start
ASTRA_TURN_TIMEOUT = int(os.environ.get("ASTRA_TURN_TIMEOUT", "10800"))
ASTRA_RETRIES = int(os.environ.get("ASTRA_RETRIES", "2"))  # retries on transient codex model-cache errors
ASTRA_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
ASTRA_PREFIX = "🛰️"
ASTRA_LOG_DIR = Path(os.environ.get("ASTRA_LOG_DIR", str(Path.home() / "shared" / "astra")))
# `!fork astra`: how much of a claude's conversation goes into the handoff file (chars, tail)
ASTRA_HANDOFF_CHARS = int(os.environ.get("ASTRA_HANDOFF_CHARS", "400000"))
_SANDBOX_OK = [None]


def codex_sandbox_usable():
    """Can codex's bubblewrap sandbox start here? True / False, or None when bwrap isn't on
    PATH (codex then uses its bundled copy and we can't tell in advance). The probe is the
    exact thing that fails on Ubuntu 24.04 without the AppArmor profile in sandbox/: a
    network namespace with a loopback interface. Cached for the life of the bridge."""
    if _SANDBOX_OK[0] is None:
        bw = shutil.which("bwrap")
        if not bw:
            return None
        try:
            r = subprocess.run([bw, "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-net", "true"],
                               capture_output=True, timeout=15)
            _SANDBOX_OK[0] = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _SANDBOX_OK[0] = False
        print(f"codex sandbox (bwrap) usable: {_SANDBOX_OK[0]}", flush=True)
    return _SANDBOX_OK[0]


def one_line(s, n=110):
    return " ".join(str(s or "").split())[:n]
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", str(Path.home() / "shared" / "reports")))
REPORTS_URL = os.environ.get("REPORTS_URL", "").rstrip("/")   # blank = link the file path instead


def report_link(folder):
    """Where a Feldspar report can be opened: the reports site when REPORTS_URL is set
    (serving REPORTS_DIR), otherwise the path of report.html on this box."""
    return (f"{REPORTS_URL}/{Path(folder).name}/report.html" if REPORTS_URL
            else str(Path(folder) / "report.html"))
CODEX_BIN = (os.environ.get("CODEX_BIN") or shutil.which("codex")
             or str(Path.home() / ".local" / "bin" / "codex"))
# Dedicated CODEX_HOME for everything the bridge launches. The box has TWO codex versions —
# the standalone that knows gpt-6-astra and an older /usr/local/bin one that does not — and
# they share ~/.codex/models_cache.json. When the old one writes it, the standalone can't parse
# gpt-6-astra out of it (serde 'missing field base_instructions') and the server 400s the model
# with a misleading 'requires a newer version of Codex'. A private home no other codex touches
# keeps the bridge's model cache in one schema. Auth (ChatGPT login) is shared via a symlink.
CODEX_HOME_DIR = os.environ.get("CODEX_BRIDGE_HOME", str(Path.home() / ".codex-bridge"))
_CODEX_HOME_READY = [False]


def codex_env():
    """os.environ + CODEX_HOME pointing at the bridge's private codex home (seeded once)."""
    if not _CODEX_HOME_READY[0]:
        try:
            src = Path.home() / ".codex"
            home = Path(CODEX_HOME_DIR)
            home.mkdir(parents=True, exist_ok=True)
            auth, link = src / "auth.json", home / "auth.json"
            if auth.exists() and not (link.is_symlink() and link.resolve() == auth.resolve()):
                if link.exists() or link.is_symlink():
                    link.unlink()
                link.symlink_to(auth)
            cfg = home / "config.toml"
            if not cfg.exists() and (src / "config.toml").exists():
                shutil.copy2(src / "config.toml", cfg)
            _CODEX_HOME_READY[0] = True
        except OSError as e:
            log_error("codex_env", e)
            return dict(os.environ)   # fall back to the shared home rather than fail the turn
    return dict(os.environ, CODEX_HOME=CODEX_HOME_DIR)
FELDSPAR_RE = re.compile(
    r"^(?:!feldspar\b|(?:let|have|ask|get|tell)\s+feldspar\s+(?:to\s+)?"
    r"(?:look|take\s+a\s+look|have\s+a\s+look|check|review|dig|investigate|go)"
    r"(?:\s+(?:into|at|over|through))?(?:\s+(?:this|it|that|here))?)"
    r"\s*[:,.\-\u2014\u2013]*\s*(?P<focus>.*)$", re.I | re.S)
FELDSPAR_DEFAULT_FOCUS = ("everything: bugs, flaws, methodological errors, unsupported claims, "
                          "silent failures")
# ---------- hooks: Claude Code tells us, instead of us polling ----------
# hooks/hearth_hook.py (installed into ~/.claude/settings.json by hooks/install_hooks.py)
# POSTs every SessionStart/SessionEnd/Stop/StopFailure/Notification/Subagent*/*Compact event
# here. The 4 s registry poll stays as the safety net; hooks make it instant.
HOOK_PORT = int(os.environ.get("HEARTH_HOOK_PORT", "8897"))
HOOK_SECRET_FILE = Path(os.environ.get("HEARTH_HOOK_SECRET",
                                       str(Path.home() / ".claude" / "hearth-hook.secret")))
HOOK_SPOOL = Path(os.environ.get("HEARTH_HOOK_SPOOL", str(Path.home() / ".claude" / "hearth-events")))
END_REASON_WORDS = {"prompt_input_exit": "exited from the prompt", "logout": "logged out",
                    "clear": "cleared", "resume": "resumed elsewhere", "other": "exited"}
DISK_WARN_GB = float(os.environ.get("DISK_WARN_GB", "15"))
DISK_CRIT_GB = float(os.environ.get("DISK_CRIT_GB", "5"))
DISK_CHECK_SECS = 60
# Offload the biggest cold directory automatically when the disk goes critical.
# Off by default — it deletes local data (after a verified S3 copy), so it is opt-in.
AUTO_OFFLOAD = os.environ.get("AUTO_OFFLOAD", "0") == "1"
# Optional: a channel that holds CLAUDE.md as an attachment, so the system prompt is
# editable from Discord. Inert unless PROMPT_CHANNEL_ID is set.
PROMPT_CHANNEL_ID = int(os.environ.get("PROMPT_CHANNEL_ID", "0") or 0)
PROMPT_TARGET = Path(os.environ.get(
    "PROMPT_TARGET", str(Path.home() / ".claude" / "CLAUDE.md")))
# tmux runs a new-window command in a NON-login shell, which never picks up
# ~/.local/bin — so a bare `claude` is "command not found". Resolve it absolutely.
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or str(Path.home() / ".local" / "bin" / "claude"))
CLAUDE_JSON = Path.home() / ".claude.json"
USER_SETTINGS = Path.home() / ".claude" / "settings.json"
# empty = anyone who can see the bot may summon; else comma-separated discord user ids
SPAWN_ALLOW_USERS = {int(x) for x in
                     os.environ.get("SPAWN_ALLOW_USERS", "").replace(" ", "").split(",") if x}

# ---------- Outer Wilds theme ----------
# Statuses come from Claude Code's registry (busy/shell/idle/waiting); these are how
# the signalscope reads them out.
STATUS_EMOJI = {"busy": "🔭", "shell": "🚀", "idle": "🔥", "waiting": "📡", "ended": "🌌"}
STATUS_WORD = {"busy": "exploring", "shell": "in flight — a shell is still running",
               "idle": "at the campfire", "waiting": "signal — needs you",
               "ended": "loop ended"}
LIVE_PREFIX, ENDED_PREFIX = "🚀", "🌌"
# thread titles from earlier versions of the bridge, still recognised by !cleanup
ALL_PREFIXES = (LIVE_PREFIX, ENDED_PREFIX, "🤖", "💤")

# Hearthians (minus Chert, who is the bot, and Feldspar, who reviews) for sessions that
# never got a name — the registry then just repeats the project dir, which is useless in
# a channel full of threads. Display-only: the session itself is not renamed.
HEARTHIANS = ["Esker", "Gabbro", "Riebeck", "Hornfels", "Slate", "Gossan", "Tektite", "Marl",
              "Moraine", "Spinel", "Tuff", "Rutile", "Porphy", "Arkose", "Galena", "Tephra",
              "Mica", "Hal", "Gneiss", "Tektite", "Solanum", "Poke", "Pye", "Clary"]
SUPERNOVA_DEFAULT = 22 * 60          # one Outer Wilds loop
SUPERNOVA_MILESTONES = (600, 300, 120, 60)

KEYMAP = {"esc": "Escape", "escape": "Escape", "enter": "Enter", "up": "Up",
          "down": "Down", "left": "Left", "right": "Right", "tab": "Tab",
          "shift-tab": "BTab", "space": "Space", "pgup": "PageUp", "pgdn": "PageDown",
          **{str(i): str(i) for i in range(1, 10)}}
HELP = ("🔭 **chert** — every Claude Code session on this box has a thread in #claudes. "
        "Write in a thread to type into that session; permission prompts arrive as buttons.\n"
        "\n**In a session's thread**\n"
        "`/refresh [message]` unstick it: Esc first, restart in place only if still stuck\n"
        "`/screen` show the terminal · `/key <key>` press esc, enter, arrows, tab, 1–9\n"
        "`/fork [message] [to]` copy it into a new session (`to: astra` hands it to GPT)\n"
        "`/rename` · `/effort` · `/model` (owner) · `/fast [on|off]` (owner) · `/mode` (`bypass` = classifier off)\n"
        "`/restart [force]` restart in place · `/revive [mode]` bring back an ended session\n"
        "`/log [count]` timeline · `/mute` · `/unmute` · `/supernova` countdown to wrap-up\n"
        "`/kill [how]` end it · `/feldspar [focus]` Claude + GPT code review\n"
        "\n**Anywhere**\n"
        "`/claude <prompt>` start a session · `/resume <session>` bring back any past one\n"
        "`/sessions` list live sessions · `/astra <prompt>` a GPT session as a thread\n"
        "`/globalmodel` · `/fast on everywhere` every session (owner) · `/yolo <30m|1h|off>` bypass for new sessions\n"
        "\n**Fleet** (output goes to #claudes)\n"
        "`/all <message>` send to every session · `/restartall` · `/reviveall` · `/cleanup`\n"
        "`/disk` · `/backup` · `/offload <dir>` · `/restore <dir>` · `/s3` (S3 needs S3_BUCKET)\n"
        "\n**#all-claudes**: a plain message asks every session and a summarizer answers; "
        "reply to it or use `/hub` to follow up.\n"
        "Every slash command also works as `!name` text (`!screen`, `!kill hard`, …). "
        f"[dashboard]({DASHBOARD})")

# Steering needs a tmux pane. Background sessions (registry `kind: bg`) run with no
# controlling terminal at all, so `ps -o tty=` is "?" and there is nothing to paste into.
NO_PANE_HELP = (
    "-# 👁️ **read-only right now** — this is a *background* agent (`--bg`) with no "
    "controlling terminal, so there's no tmux pane to type into. Run **`!revive`** and "
    "I'll resume this exact session inside tmux, history intact, and you can talk to it "
    "here.")

NO_PING = discord.AllowedMentions.none()
# keyed by "<pid>:<procStart>" (a claude's stable process identity) ->
# {thread, parent, status_msg, size, status, name, sid, cwd, ended, ended_at,
#  ended_reason, muted, tool_msg, tool_run, prompt_msg, spawned}; "_meta" -> {...}
state = {}
_SAVE_ERR = [None]
_ERR_COUNT = Counter()


def load_state():
    global state
    for candidate in (STATE_FILE, STATE_FILE.with_suffix(".json.bak")):
        try:
            state = json.loads(candidate.read_text())
            if candidate != STATE_FILE:
                print(f"⚠️ {STATE_FILE.name} unreadable — recovered from {candidate.name}")
            break
        except FileNotFoundError:
            continue
        except json.JSONDecodeError as e:
            print(f"⚠️ {candidate.name} corrupt ({e}); trying the next copy")
            continue
    else:
        state = {}
    state.setdefault("_meta", {})
    for k in [k for k, v in state.items()
              if not k.startswith("_") and isinstance(v, dict) and v.get("pending")]:
        print(f"clearing stale pending claim for {k}")
        state.pop(k)
    for v in state.values():
        if isinstance(v, dict):
            v.pop("restarting", None)   # a restart that the bridge died in the middle of


def save_state():
    """Atomic write. A full disk used to leave a 0-byte state file behind, and the next
    start then opened a brand-new thread for every session."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(state, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        if _SAVE_ERR[0] is not None:
            print("state saves working again")
        _SAVE_ERR[0] = None
        bak = STATE_FILE.with_suffix(".json.bak")
        try:
            if not bak.exists() or time.time() - bak.stat().st_mtime > 6 * 3600:
                shutil.copy2(STATE_FILE, bak)
        except OSError:
            pass
    except OSError as e:
        if _SAVE_ERR[0] is None:
            print(f"⚠️ state save failed ({e}) — keeping state in memory until the disk recovers")
        _SAVE_ERR[0] = e
        try:
            tmp.unlink()
        except OSError:
            pass


def log_error(where, e):
    """Print an error, but not 4s-apart forever: identical errors are logged at 1, 2, 4,
    8 … occurrences (the ENOSPC flood was thousands of identical lines)."""
    k = f"{where}:{type(e).__name__}:{str(e)[:80]}"
    _ERR_COUNT[k] += 1
    n = _ERR_COUNT[k]
    if n & (n - 1) == 0:   # power of two
        print(f"{where} error (x{n}): {e!r}", flush=True)


def sessions_state():
    return {sid: v for sid, v in state.items() if not sid.startswith("_")}


def thread_to_key():
    # .get: entries mid-summon are {"pending": True} and have no thread yet
    return {v["thread"]: k for k, v in sessions_state().items() if v.get("thread")}


def hearthian_for(key):
    """A stable Hearthian name for a nameless session: hashed from its identity, skipping
    names another live-or-recent nameless session already wears."""
    taken = state["_meta"].setdefault("hearthians", {})
    if key in taken:
        return taken[key]
    used = set(taken.values())
    start = int.from_bytes(key.encode()[-4:], "little") % len(HEARTHIANS)
    for i in range(len(HEARTHIANS)):
        cand = HEARTHIANS[(start + i) % len(HEARTHIANS)]
        if cand not in used:
            break
    else:
        cand = f"{HEARTHIANS[start]}-{key.split(':')[0][-2:]}"
    taken[key] = cand
    return cand


def live_sessions():
    """checkin.live_sessions() with Hearthian display names for nameless sessions (the
    registry repeats the project dir when no name was ever set)."""
    sessions = checkin.live_sessions()
    live_keys = {s["key"] for s in sessions}
    for s in sessions:
        if not s["name"] or s["name"] == s["project"]:
            s["name"] = hearthian_for(s["key"])
    taken = state["_meta"].get("hearthians", {})
    for k in [k for k in taken if k not in live_keys]:
        del taken[k]            # free the name once its session is gone
    return sessions


def find_live_by_key(key):
    for s in live_sessions():
        if s["key"] == key:
            return s
    return None


def ship_log(transcript, limit=25):
    """A session's timeline — prompts, what Claude decided, tool bursts — as Ship Log entries.
    Deterministic (no LLM): first sentence of each assistant message, first line of each
    prompt, tool runs collapsed."""
    if not transcript or not Path(transcript).exists():
        return []
    try:
        items, _ = checkin.parse_transcript(Path(transcript), 0, 3 * 1024 * 1024, True)
    except OSError:
        return []
    entries, tools, last_ts = [], Counter(), None

    def flush():
        if tools:
            entries.append((last_ts, "tool", ", ".join(f"{n} ×{k}" if k > 1 else n
                                                         for n, k in tools.items())))
            tools.clear()

    for it in items:
        k, ts = it["kind"], it.get("ts") or last_ts
        if k == "tool":
            tools[it.get("name", "?")] += 1
            last_ts = ts
            continue
        flush()
        text = " ".join((it.get("text") or "").split())
        if k == "user":
            text = re.sub(r"^\[discord[^\]]*\]\s*", "", text)
            entries.append((ts, "user", text[:140]))
        elif k == "assistant":
            first = re.split(r"(?<=[.!?])\s+|\n", text, maxsplit=1)[0]
            entries.append((ts, "assistant", first[:160]))
        elif k == "cmd":
            entries.append((ts, "cmd", text[:80]))
        last_ts = ts
    flush()
    entries = [e for e in entries if e[2].strip()]
    return entries[-limit:]


def iso_to_unix(ts):
    try:
        return int(time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))) - time.timezone
    except (ValueError, TypeError):
        return None


def render_ship_log(name, entries):
    icons = {"user": "🧑", "assistant": "🔭", "tool": "🔧", "cmd": "⌘"}
    lines = [f"🪐 **Ship log — {name}** · {len(entries)} entries"]
    for ts, kind, text in entries:
        when = f"<t:{iso_to_unix(ts)}:t> " if iso_to_unix(ts) else ""
        pre = "-# " if kind in ("tool", "cmd") else ""
        lines.append(f"{pre}{when}{icons[kind]} {text}")
    return "\n".join(lines)


def migrate_state(sessions):
    """Move legacy sessionId-keyed entries onto stable pid:procStart keys, and age out
    ended entries older than ENDED_KEEP_DAYS.

    State used to be keyed by sessionId, but a claude's sessionId changes under it
    on fork/branch/clear and isn't unique across processes — which made sessions
    lose their thread, get re-threaded, or vanish entirely.
    """
    by_sid = {s["sid"]: s["key"] for s in sessions}
    keys = {s["key"] for s in sessions}
    moved = dropped = 0
    now = time.time()
    for k in [k for k in state if not k.startswith("_")]:
        if k in keys:
            continue
        v = state[k]
        if k in by_sid:                     # legacy entry for a still-live session
            state.pop(k)
            v["sid"] = k
            state[by_sid[k]] = v
            moved += 1
        elif ":" not in k:
            state.pop(k)                    # legacy dead entry; key can never recur
            dropped += 1
        elif v.get("ended"):
            v.setdefault("ended_at", now)   # older entries: start their clock now
            if now - (v["ended_at"] or now) > ENDED_KEEP_DAYS * 86400:
                state.pop(k)
                dropped += 1
    if moved or dropped:
        print(f"state migrated: {moved} re-keyed to pid:procStart, {dropped} dead dropped")
        save_state()


# ---------- yolo: temporary bypassPermissions (ported from chert) ----------

YOLO_MAX = 12 * 3600
YOLO = {"task": None, "until": 0.0, "prev": None}


def settings_default_mode():
    try:
        return json.loads(USER_SETTINGS.read_text()).get("permissions", {}).get("defaultMode")
    except (OSError, json.JSONDecodeError):
        return None


PERMISSION_MODE = os.environ.get("PERMISSION_MODE") or settings_default_mode() or "auto"


def yolo_active():
    return YOLO["until"] > time.time()


def current_permission_mode():
    return "bypassPermissions" if yolo_active() else PERMISSION_MODE


def parse_duration(text):
    """'1h' / '30m' / '90s' / '45' (bare = minutes) -> seconds, or None."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smh])?", text.strip().lower())
    if not m:
        return None
    return int(float(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2) or "m"])


def set_user_setting(key, value):
    """Set one top-level key in ~/.claude/settings.json, atomically (temp file + rename, like
    set_default_mode). Returns the previous value."""
    d = json.loads(USER_SETTINGS.read_text()) if USER_SETTINGS.exists() else {}
    prev = d.get(key)
    d[key] = value
    tmp = USER_SETTINGS.with_name(USER_SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n")
    tmp.replace(USER_SETTINGS)
    return prev


def set_default_model(name):
    """The top-level "model" every new or restarted claude starts on (it overrides the
    per-user default that Claude Code's own /model saves)."""
    return set_user_setting("model", name)


FAST_RESULT_RE = re.compile(r"(Kept Fast mode (?:ON|OFF)|Fast mode (?:ON|OFF)|"
                            r"Fast mode (?:disabled|unavailable)[^\n│]*)")


def fast_result(pane):
    """What Claude Code last said about fast mode on a pane's screen, e.g. 'Fast mode ON' or
    'Fast mode disabled · usage credits not available for your plan'; '' if nothing yet."""
    hits = FAST_RESULT_RE.findall(screen_text(pane, 1600) or "")
    return " ".join(hits[-1].replace("\\xB7", "·").split()) if hits else ""


def set_default_mode(mode):
    """Flip permissions.defaultMode in user settings, preserving every other key.

    Written via a temp file + rename so a session reading settings.json mid-write
    never sees a truncated file (a malformed settings.json silently disables ALL
    settings from it).
    """
    d = json.loads(USER_SETTINGS.read_text()) if USER_SETTINGS.exists() else {}
    prev = d.setdefault("permissions", {}).get("defaultMode")
    d["permissions"]["defaultMode"] = mode
    tmp = USER_SETTINGS.with_name(USER_SETTINGS.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n")
    tmp.replace(USER_SETTINGS)
    return prev


async def start_yolo(seconds):
    """bypassPermissions for `seconds`, then restore the mode that was there before.

    Re-issuing while active extends the window without clobbering `prev` — otherwise
    a second `!yolo` would record "bypassPermissions" as the mode to restore and the
    box would never come back.
    """
    if YOLO["task"] and not YOLO["task"].done():
        YOLO["task"].cancel()
    elif not yolo_active():
        YOLO["prev"] = set_default_mode("bypassPermissions")
    YOLO["until"] = time.time() + seconds

    async def revert():
        try:
            await asyncio.sleep(max(0.0, YOLO["until"] - time.time()))
        except asyncio.CancelledError:
            return
        set_default_mode(YOLO["prev"] or PERMISSION_MODE)
        YOLO["until"] = 0.0
        YOLO["task"] = None

    YOLO["task"] = asyncio.create_task(revert(), name="yolo-revert")


def stop_yolo():
    if YOLO["task"]:
        YOLO["task"].cancel()
    if yolo_active():
        set_default_mode(YOLO["prev"] or PERMISSION_MODE)
    YOLO["until"], YOLO["task"] = 0.0, None


# ---------- avatars / identities (ported from chert) ----------

# bottts-neutral defaults to one flat green for every seed, so a row of claudes read
# as identical green blobs at 40px. Seeding the background too makes them tell apart.
AVATAR_BGS = ("b6e3f4", "c0aede", "d1d4f9", "ffd5dc", "ffdfbf",
              "d9ead3", "f9cb9c", "a2c4c9")
# Override the generated robot face with a URL Discord can fetch. A literal `{seed}` is
# replaced with the claude's stable identity so faces stay distinct.
STATIC_AVATAR = os.environ.get("AVATAR_URL", "").strip()
# Preferred over AVATAR_URL: a LOCAL image, uploaded once onto the webhook itself. Every
# claude then shares that face. Keep the file outside the repo.
AVATAR_FILE = os.environ.get("AVATAR_FILE", "").strip()
# Where thread-reply attachments get saved. ~/shared is the folder CLAUDE.md already
# tells claudes to check for uploads, so this makes that convention true from Discord.
ATTACH_DIR = Path(os.environ.get("ATTACH_DIR", str(Path.home() / "shared" / "uploads")))
_AVATAR_CACHE = {}


def avatar_bytes():
    if not AVATAR_FILE:
        return None
    if "bytes" not in _AVATAR_CACHE:
        try:
            _AVATAR_CACHE["bytes"] = Path(AVATAR_FILE).read_bytes()
        except OSError as e:
            print(f"AVATAR_FILE unreadable ({e}) — using generated faces", flush=True)
            _AVATAR_CACHE["bytes"] = b""
    return _AVATAR_CACHE["bytes"] or None


def avatar_url(seed):
    """A claude's face, keyed on its stable pid:procStart identity — name-seeding gave
    a claude a brand new face every time it was renamed."""
    if STATIC_AVATAR:
        return STATIC_AVATAR.replace("{seed}", urllib.parse.quote(seed))
    return ("https://api.dicebear.com/9.x/bottts-neutral/png?size=128"
            "&backgroundColor=" + ",".join(AVATAR_BGS) + "&seed="
            + urllib.parse.quote(seed))


WEBHOOK_NAME_MAX = 76     # discord caps webhook usernames at 80; leave headroom


def webhook_name(s, maxlen=WEBHOOK_NAME_MAX):
    """"<project> · <session>", so which project a claude is working in reads at a
    glance. The session half is a slug of its first prompt, so that is what gets
    clipped — the project prefix is the part worth keeping."""
    proj = (s.get("project") or "?").strip()
    name = (s.get("name") or "claude").strip()
    if name == proj or name.startswith(proj + "-"):
        return name[:maxlen]
    room = min(24, maxlen - len(proj) - 3)
    if room < 8:
        return name[:maxlen]
    short = (name[:room].rstrip("-") + "…") if len(name) > room else name
    return f"{proj} · {short}"


# ---------- rendering ----------

def reach(s):
    """How the bridge can talk to a session: tmux pane, inbox socket, or not at all."""
    if s.get("pane"):
        return "pane"
    if s.get("sock"):
        return "sock"
    return None


def short_model(m):
    return (m or "?").replace("claude-", "")


def model_line(st):
    """A `-# 🧠 model …` line for a session's intro/status, or '' if the model isn't known yet.
    Flags when the auto-mode classifier has rerouted turns off the session's baseline model."""
    if not st or not st.get("model"):
        return ""
    cur, base, drops = st.get("model"), st.get("model_base"), st.get("model_drops", 0)
    line = f"\n-# 🧠 model `{short_model(cur)}`"
    if base and cur != base:
        line += f" · ⤵️ classifier reroute (baseline `{short_model(base)}`)"
    if drops:
        line += f" · {drops} turn{'s' if drops != 1 else ''} rerouted so far"
    return line


def status_line(s, st=None):
    em = STATUS_EMOJI.get(s["status"], "⚪")
    word = STATUS_WORD.get(s["status"], s["status"])
    r = reach(s)
    ro = "" if r == "pane" else (" · 📨 **background** (talk via its inbox socket)" if r == "sock"
                                 else " · 👁️ **read-only**")
    return (f"**{s['name']}** · `{s['project']}`\n"
            f"{em} **{word}**{ro} · updated <t:{int(time.time())}:R>"
            f"{model_line(st)}\n"
            f"-# [dashboard]({DASHBOARD}/s/{s['sid']}) · reply to talk · `!screen` · `!help`")


def fork_name(base):
    """x → x-fork → x-fork2 → x-fork3 (instead of x-fork-fork-fork, whose 20-char tmux window
    slug collapses onto the parent's and makes a fork of a fork look like nothing happened)."""
    m = re.match(r"^(.*?)-fork(\d*)$", base)
    if not m:
        return f"{base}-fork"[:60]
    root, n = m.group(1), int(m.group(2) or 1)
    return f"{root}-fork{n + 1}"[:60]


def thread_title(name, sid, collides):
    """Thread titles carry a short sid suffix only when two live claudes share a name."""
    return (f"{LIVE_PREFIX} {name} · {sid[:4]}" if collides else f"{LIVE_PREFIX} {name}")[:100]


def ended_title(name):
    for p in ALL_PREFIXES:
        if name.startswith(p):
            name = name[len(p):].lstrip()
    return f"{ENDED_PREFIX} {name}"[:100]


def split_chunks(text, limit=CHUNK):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def render_tools(counts):
    """'Edit ×3, Bash, Read ×2' — one line for a run of tool calls."""
    parts = ", ".join(f"{n} ×{k}" if k > 1 else n for n, k in counts.items())
    return f"-# 🔧 {parts}"[:1990]


# ---------- the activity card: what a claude is doing right now, edited in place ----------
# One webhook message per turn, under the claude's own name. Mirrors the terminal's spinner
# tree (tool description as headline, `└ $ command` beneath, latest thought, tool tally,
# elapsed) and is EDITED every few seconds — edits never notify, so you can watch it think
# without being pinged. When the turn ends it collapses to a one-line recap.
CLASSIFIER_PHRASE = "denied by the Claude Code auto mode classifier"   # tool_result block text
FILE_LIMIT_BYTES = int(os.environ.get("HEARTH_FILE_LIMIT", str(25 * 1024 * 1024)))  # Discord upload cap
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")   # images a claude Reads get mirrored to its thread
# Discord shrinks an image preview to the chat width (~1000 px on a phone). A figure wider than
# this aspect ratio (e.g. a 1×5 panel strip) ends up with a few-pixel-tall text, i.e. "blurry".
WIDE_ASPECT = 2.2


TWIN_GRACE = 20   # seconds a new process waits while another live process holds its conversation


def key_process_alive(key):
    """Is the process behind a `pid:procStart` key still running (and not a recycled pid)?
    Used before declaring a session gone: dropping out of one registry read is not an exit."""
    pid, _, start = str(key).partition(":")
    if not pid.isdigit():
        return False
    if proc_state(int(pid)) in (None, "Z"):
        return False
    ps = checkin.proc_start(int(pid))
    return not (start and ps and str(ps) != start)


def png_size(path):
    """(width, height) from a PNG header, or None (not a PNG / unreadable)."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) == 24:
        return struct.unpack(">II", head[16:24])
    return None
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")   # Claude Code /effort levels
MODEL_RE = re.compile(r"^[A-Za-z0-9._\-\[\]]{2,60}$")        # alias (fable/opus/...) or full id; no spaces
MODEL_SUGGESTIONS = ("fable", "opus", "sonnet", "haiku", "default", "claude-opus-5-5",
                     "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "opus[1m]")


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
CARD_MIN_GAP = 3          # seconds between edits when content changed
CARD_HEARTBEAT = 20       # seconds between elapsed-only edits
NARRATION_MAX = 160       # assistant text longer than this is a reply, not narration


def _clean(text, n):
    t = " ".join((text or "").split()).replace("`", "'")
    return t[:n] + ("…" if len(t) > n else "")


def update_card(st, items):
    """Fold new transcript items into the session's live card. Returns True if it changed."""
    card = st.get("card")
    changed = False
    for it in items:
        k = it["kind"]
        if k not in ("tool", "thinking", "assistant"):
            continue
        if card is None:
            card = st["card"] = {"started": time.time(), "counts": {}, "msg": None, "last_edit": 0.0,
                                 "narration": "", "desc": "", "tool": "", "thought": "", "body": "",
                                 "dirty": True}
        if k == "tool":
            name = it.get("name", "?")
            card["counts"][name] = card["counts"].get(name, 0) + 1
            arg = _clean(it.get("text"), 220)
            card["tool"] = f"{'$ ' if name == 'Bash' else name + ' '}{arg}" if arg else name
            card["desc"] = _clean(it.get("desc"), 200)
        elif k == "thinking":
            card["thought"] = _clean(it["text"], 280)
        elif k == "assistant":
            t = _clean(it["text"], NARRATION_MAX + 1)
            card["narration"] = t if len(t) <= NARRATION_MAX else ""   # full replies are posted, not mirrored
            if len(t) > NARRATION_MAX:
                card["desc"] = card["tool"] = ""                         # a reply ends the current step
        changed = True
    if changed:
        card["dirty"] = True
    return changed


def card_text(s, card, final=False, model=None, base=None):
    el = int(time.time() - card["started"])
    mm, ss = divmod(el, 60)
    counts = card.get("counts") or {}
    tools = ", ".join(f"{n} ×{k}" if k > 1 else n for n, k in counts.items())
    mtag = ""
    if model:
        mtag = f" · 🧠 `{short_model(model)}`" + (" ⤵️" if base and model != base else "")
    if final:
        return (f"-# ✅ turn done · {mm}m {ss:02d}s" + (f" · 🔧 {tools}" if tools else "") + mtag)[:1990]
    lines = [f"{STATUS_EMOJI.get(s['status'], '🔭')} **{STATUS_WORD.get(s['status'], s['status']).split(' —')[0]}** · {mm}m {ss:02d}s{mtag}"]
    if card.get("narration"):
        lines.append(f"*{card['narration']}*")
    if card.get("desc"):
        lines.append(f"**{card['desc']}**")
    if card.get("tool"):
        lines.append(f"└ `{card['tool']}`")
    if card.get("thought"):
        lines.append(f"💭 {card['thought']}")
    if tools:
        lines.append(f"-# 🔧 {tools}")
    return "\n".join(lines)[:1990]


def format_new_items(items, sid, include_tools=True):
    """Compress transcript items into ('tool', counts) / ('msg', text) entries.

    Runs of tool calls collapse into a single counts dict; the caller merges
    consecutive runs by EDITING the previous tool line instead of posting again,
    so a long working stretch stays one quiet, growing message. With
    include_tools=False (the activity card is showing them) tools are left out.
    """
    out, tools = [], []

    def flush_tools():
        if tools:
            out.append(("tool", dict(Counter(tools))))
            tools.clear()

    for it in items:
        k = it["kind"]
        if k == "tool":
            if include_tools:
                tools.append(it.get("name", "?"))
        elif k == "assistant":
            flush_tools()
            chunks = split_chunks(it["text"])
            if len(chunks) > 5:
                chunks = chunks[:5] + [f"-# …truncated — [full reply]({DASHBOARD}/s/{sid})"]
            if len(chunks) > 1:
                total = len(chunks)
                chunks = [f"{c}\n-# ⤵ part {i}/{total}"
                          for i, c in enumerate(chunks, 1)]
            out.extend(("msg", c) for c in chunks)
        elif k == "user":
            flush_tools()
            # messages that came FROM a thread are already in it — don't echo them back
            if it["text"].lstrip().startswith("[discord ·"):
                continue
            out.append(("msg", f"-# 🧑 you (terminal): {it['text'][:350]}".replace("\n", " ")))
        elif k == "cmd":
            flush_tools()
            out.append(("msg", f"-# ⌘ {it['text'][:200]}"))
    flush_tools()
    return out


def resolve_project(words):
    """First word may name a project dir; returns (cwd, remaining_prompt_words)."""
    if words:
        cand = Path(words[0]).expanduser()
        for p in ((cand,) if cand.is_absolute() else (PROJECT_ROOT / words[0], cand)):
            if p.is_dir():
                return p, words[1:]
    return PROJECT_ROOT, words


def slug(text, maxlen=32):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:maxlen].rstrip("-") or "adhoc"


def transcript_cwd(sid):
    """Recover a session's working directory from its transcript (survives the process)."""
    p = checkin.find_transcript_anywhere(sid)
    if not p:
        return None
    try:
        with open(p) as f:
            for i, line in enumerate(f):
                if i > 20:
                    break
                if (cwd := json.loads(line).get("cwd")):
                    return cwd
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


# ---------- screens, prompts, first-run gates ----------

def screen_text(pane, limit=1800):
    """Visible text of a pane. Both buffers: a TUI's dialog can sit on the alternate
    screen, and `-S` scrollback is useless because TUIs redraw in place."""
    text = ""
    for args in (["tmux", "capture-pane", "-p", "-t", pane],
                 ["tmux", "capture-pane", "-p", "-a", "-t", pane]):
        rc, scr = checkin.run(args)
        cand = "\n".join(l.rstrip() for l in scr.splitlines() if l.strip())
        if len(cand) > len(text):
            text = cand
    return text[-limit:]


# "│ ❯ 1. Yes                      │" -> selected, 1, "Yes"
OPT_RE = re.compile(r"^\s*(?:[│┃|]\s*)?(❯|›|>)?\s*(\d{1,2})\.\s+(.+?)\s*(?:[│┃|])?\s*$")


def parse_options(text):
    """Numbered menu options on a screen -> ([(n, label)], selected_n|None).

    Claude Code renders permission prompts and AskUserQuestion menus as `❯ 1. Yes` /
    `  2. No…` lines inside a box; the ❯ marks the highlighted row.
    """
    opts, selected, seen = [], None, set()
    for line in text.splitlines():
        m = OPT_RE.match(line)
        if not m:
            continue
        n = int(m.group(2))
        if n in seen or n > 12:
            continue
        seen.add(n)
        label = re.sub(r"\s{2,}", " ", m.group(3)).strip()
        opts.append((n, label))
        if m.group(1):
            selected = n
    opts.sort()
    # a real menu starts at 1 and is contiguous; anything else is prose with numbers
    if not opts or opts[0][0] != 1 or [n for n, _ in opts] != list(range(1, len(opts) + 1)):
        return [], None
    return opts, selected


# First-run gates that stall a freshly spawned claude BEFORE it registers a session
# (so no `waiting` status ever appears — the pane just sits there). Each one is
# answered the way a human who just asked for this claude would answer it.
GATES = [
    ("trust", re.compile(r"trust this folder|Quick safety check", re.I), ("choose", "Yes, I trust")),
    ("bypass", re.compile(r"Bypass Permissions mode", re.I), ("choose", "Yes, I accept")),
    ("apikey", re.compile(r"Do you want to use this API key", re.I), ("choose", "Yes")),
    ("theme", re.compile(r"Choose the text style", re.I), ("enter",)),
    ("continue", re.compile(r"Press Enter to continue", re.I), ("enter",)),
]
LOGIN_RE = re.compile(r"Select login method|log ?in with|Claude account with subscription", re.I)


def detect_gate(text):
    for name, rx, action in GATES:
        if rx.search(text):
            return name, action
    return None, None


def answer_gate(pane, text, action):
    """Press the keys for a gate action. Returns a description of what was pressed."""
    if action[0] == "enter":
        checkin.run(["tmux", "send-keys", "-t", pane, "Enter"])
        return "Enter"
    opts, selected = parse_options(text)
    target = next((n for n, lab in opts if lab.lower().startswith(action[1].lower())), None)
    if target is None:
        # no parseable menu: the wanted answer is usually the default — take it
        checkin.run(["tmux", "send-keys", "-t", pane, "Enter"])
        return "Enter (no menu parsed)"
    cur = selected or 1
    keys = ["Down"] * (target - cur) if target > cur else ["Up"] * (cur - target)
    for k in keys:
        checkin.run(["tmux", "send-keys", "-t", pane, k])
        time.sleep(0.15)
    checkin.run(["tmux", "send-keys", "-t", pane, "Enter"])
    return f"{'/'.join(keys) + '/' if keys else ''}Enter → {target}. {action[1]}"


def pane_claude_alive(pane):
    """Is a claude process really on this pane? Deliberately does NOT trust
    `pane_current_command` — tmux reports the process-GROUP leader (the wrapping bash),
    not the foreground child, so it says "bash" even while claude waits on a dialog."""
    rc, tty = checkin.run(["tmux", "display", "-p", "-t", pane, "#{pane_tty}"])
    tty = tty.strip().removeprefix("/dev/")
    if not tty:
        return False
    rc, procs = checkin.run(["ps", "-t", tty, "-o", "args="])
    return any("/claude" in l and "bash -lc" not in l for l in procs.splitlines())


def ensure_trusted(cwd):
    """Pre-answer the per-directory trust dialog in ~/.claude.json for a spawn target.

    The trust map is keyed by absolute path; a directory that was never opened
    interactively stalls the new claude on "Quick safety check" before it registers.
    Read-modify-write via rename; a lost race with a claude's own write only costs us
    the flag (the gate watcher in await_registration is the fallback).
    """
    try:
        d = json.loads(CLAUDE_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    proj = d.setdefault("projects", {}).setdefault(str(cwd), {})
    if proj.get("hasTrustDialogAccepted"):
        return False
    proj["hasTrustDialogAccepted"] = True
    tmp = CLAUDE_JSON.with_name(CLAUDE_JSON.name + ".tmp-bridge")
    try:
        tmp.write_text(json.dumps(d, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, CLAUDE_JSON)
    except OSError as e:
        print(f"ensure_trusted({cwd}) failed: {e}")
        return False
    return True


def spawn_flags():
    """SPAWN_FLAGS, plus bypass while `!yolo` is active.

    An explicit --permission-mode on the command line overrides settings.json, so a
    stale one in SPAWN_FLAGS would make `!yolo` silently do nothing for new claudes:
    strip it while yolo is on and put bypassPermissions in its place.
    """
    toks = shlex.split(SPAWN_FLAGS)
    if not yolo_active():
        return " ".join(shlex.quote(t) for t in toks)
    out, skip = [], False
    for t in toks:
        if skip:
            skip = False
            continue
        if t == "--permission-mode":
            skip = True
            continue
        if t.startswith("--permission-mode="):
            continue
        out.append(t)
    if "--dangerously-skip-permissions" not in out:
        out += ["--permission-mode", "bypassPermissions"]
    return " ".join(shlex.quote(t) for t in out)


def spawn_claude(name, cwd, resume_sid=None, fork=False, extra_flags="", env=None):
    """Launch a claude in its own tmux window. Returns (pane_id, err).

    With resume_sid, resumes that conversation (`-r`) so a background agent with no
    tty comes back as a steerable, tmux-backed session with its history intact.
    If claude exits (bad flag, crash) the window drops to a shell instead of
    vanishing, so the error stays visible via !screen. `env` adds variables for this
    launch only (e.g. the agent-teams flag for Feldspar).
    """
    ensure_trusted(cwd)
    flags = spawn_flags()
    if extra_flags:
        flags += " " + extra_flags
    if resume_sid:
        flags += f" -r {shlex.quote(resume_sid)}" + (" --fork-session" if fork else "")
    envs = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items())
    inner = (f"CLAUDE_INSTANCE_NAME={shlex.quote(name)} {envs} "
             f"{shlex.quote(CLAUDE_BIN)} {flags} -n {shlex.quote(name)}; "
             f"echo '[claude exited — window kept for inspection]'; exec bash")
    rc, out = checkin.run(
        ["tmux", "new-window", "-d", "-t", f"{TMUX_SESSION}:", "-n", slug(name, 20),
         "-c", str(cwd), "-P", "-F", "#{pane_id}",
         f"bash -lc {shlex.quote(inner)}"])   # -l so PATH/env match a real terminal
    if rc != 0:
        return None, out.strip()
    return out.strip(), None


VALUE_FLAGS = {"--effort", "--model", "--permission-mode", "--add-dir", "--settings", "--agents",
               "--append-system-prompt", "--system-prompt", "--plugin-dir", "--channels",
               "--mcp-config", "--max-turns", "--output-format", "--input-format", "--fallback-model",
               "--worktree", "--environment", "--session-id", "-r", "--resume", "-n", "--name",
               "--append-system-prompt-file", "--system-prompt-file"}
DROP_FLAGS = {"-r", "--resume", "-n", "--name", "--session-id", "-c", "--continue", "--fork-session",
              "--remote-control", "--bg", "--background", "-p", "--print"}


def session_crons(transcript, limit=6):
    """CronCreate calls made during a session (spec, prompt head, durable?). Session-only
    crons live in the claude process and die with it, so a restarted claude is handed this
    list to re-arm what should still be running."""
    out = []
    if not transcript or not Path(transcript).exists():
        return out
    try:
        with open(transcript, "rb") as f:
            for line in f:
                if b"CronCreate" not in line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for blk in ((r.get("message") or {}).get("content") or []):
                    if isinstance(blk, dict) and blk.get("type") == "tool_use" \
                            and blk.get("name") == "CronCreate":
                        inp = blk.get("input") or {}
                        out.append({"cron": inp.get("cron"), "durable": bool(inp.get("durable")),
                                    "recurring": inp.get("recurring", True),
                                    "prompt": " ".join(str(inp.get("prompt", "")).split())[:300]})
    except OSError:
        pass
    return [c for c in out if not c["durable"]][-limit:]


def proc_state(pid):
    """Kernel state letter for a pid: R/S running-ish, T stopped (ctrl-z), Z zombie; None if gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2:].split()[0]
    except (OSError, ValueError, IndexError):
        return None


def claude_argv(pid):
    try:
        return [a.decode("utf-8", "replace") for a in
                Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return []


def restart_flags(argv):
    """The flags to relaunch a session with `claude -r <sid>`: keep permission / effort /
    model / add-dir etc., drop resume/fork/name/continue (re-added by the caller) and any
    positional prompt (it would be replayed as a new first message)."""
    out, i = [], 1
    while i < len(argv):
        t = argv[i]
        base, _, inline = t.partition("=")
        if base in DROP_FLAGS:
            i += 2 if (base in VALUE_FLAGS and not inline) else 1
            continue
        if not t.startswith("-"):
            i += 1                                   # positional prompt: never replay
            continue
        out.append(t)
        if base in VALUE_FLAGS and not inline and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 1
        i += 1
    return " ".join(shlex.quote(t) for t in out)


def bus_append(frm, text):
    """Append a message to the cchat bus (same schema cchat.py writes)."""
    CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CHAT_LOG, "a") as f:
        f.write(json.dumps({"ts": time.strftime("%H:%M:%S"), "from": frm,
                            "text": text, "via": "discord"}) + "\n")


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def attribute(author_id, display_name, text, owner_id):
    """What actually gets typed into a session for a Discord message.

    This used to be "[discord · name] text". The bracketed channel tag kept tripping
    Claude Code's safety monitor (it reads like injected content from an outside
    channel), so the owner's words now arrive verbatim. Other participants still get a
    plain "name: " so a claude knows who is talking to it.
    """
    return text if author_id == owner_id else f"{display_name}: {text}"


# ---------- every session this box has ever had ----------

def transcript_head(path, max_lines=60):
    """(cwd, custom_title, agent_name, first_user_prompt) from the top of a transcript."""
    cwd = title = agent = first = None
    try:
        with open(path) as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = r.get("type")
                cwd = cwd or r.get("cwd")
                if t == "custom-title":
                    title = r.get("customTitle") or title
                elif t == "agent-name":
                    agent = r.get("agentName") or agent
                elif t == "user" and first is None and not r.get("isMeta"):
                    c = (r.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(b.get("text", "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text")
                    if isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
                        first = c.strip()
    except OSError:
        pass
    return cwd, title, agent, first


def history_index():
    """sid -> {first, cwd, ts} from ~/.claude/history.jsonl: every prompt ever typed, kept
    even for sessions whose transcript Claude Code has since deleted."""
    out = {}
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = r.get("sessionId")
                if not sid:
                    continue
                e = out.setdefault(sid, {"first": None, "cwd": None, "ts": 0.0})
                disp = (r.get("display") or "").strip()
                if e["first"] is None and disp and disp not in ("exit", "/exit"):
                    e["first"] = disp
                e["ts"] = max(e["ts"], (r.get("timestamp") or 0) / 1000)
                e["cwd"] = e["cwd"] or r.get("project")
    except OSError:
        pass
    return out


_INDEX = {"at": 0.0, "rows": []}


def session_index(max_age=120):
    """Every session: local transcripts, then transcripts that only survive in the S3
    backup (Claude Code deletes local ones after cleanupPeriodDays). Cached briefly —
    it reads the head of every transcript."""
    now = time.time()
    if now - _INDEX["at"] < max_age:
        return _INDEX["rows"]
    hist = history_index()
    names = {v["sid"]: v["name"] for v in sessions_state().values()
             if v.get("sid") and v.get("name")}
    live = {s["sid"]: s for s in live_sessions()}
    for s in live.values():
        names[s["sid"]] = s["name"]
    rows, seen = [], set()
    for p in checkin.PROJECTS_DIR.glob("*/*.jsonl"):
        sid = p.stem
        try:
            st = p.stat()
        except OSError:
            continue
        cwd, title, agent, first = transcript_head(p)
        h = hist.get(sid, {})
        rows.append({"sid": sid, "cwd": cwd or h.get("cwd") or "", "mtime": st.st_mtime,
                     "size": st.st_size, "name": names.get(sid) or title or agent or "",
                     "first": first or h.get("first") or "", "where": "disk", "live": sid in live})
        seen.add(sid)
    if ash_twin:
        for sid, b in ash_twin.backup_transcripts().items():
            if sid in seen:
                continue
            h = hist.get(sid, {})
            rows.append({"sid": sid, "cwd": h.get("cwd") or "", "mtime": h.get("ts") or b["modified"],
                         "size": b["size"], "name": names.get(sid, ""), "first": h.get("first") or "",
                         "where": "s3", "live": False})
    for r in rows:
        r["project"] = Path(r["cwd"]).name if r["cwd"] else "?"
        first = re.sub(r"^\[discord[^\]]*\]\s*", "", r["first"] or "")
        r["label"] = " ".join((r["name"] or first or r["sid"][:8]).split())[:60]
    rows.sort(key=lambda r: -r["mtime"])
    _INDEX.update(at=now, rows=rows)
    return rows


def search_sessions(query, rows=None):
    rows = session_index() if rows is None else rows
    toks = [t for t in query.lower().split() if t]
    if not toks:
        return rows
    return [r for r in rows
            if all(t in f"{r['name']} {r['first']} {r['project']} {r['cwd']} {r['sid']}".lower()
                   for t in toks)]


def age_str(ts):
    d = max(0.0, time.time() - (ts or 0))
    return f"{int(d / 60)}m" if d < 3600 else f"{d / 3600:.0f}h" if d < 86400 else f"{d / 86400:.0f}d"


def resume_text(query, page, rows):
    n = len(rows)
    pages = max(1, (n + RESUME_PAGE - 1) // RESUME_PAGE)
    head = (f"🔭 **sessions** — {n} {'match' + ('es' if n != 1 else '') if query else 'total'}"
            + (f" for `{query}`" if query else "") + f" · page {page + 1}/{pages}")
    lines = [head]
    for r in rows[page * RESUME_PAGE:(page + 1) * RESUME_PAGE]:
        mark = "🟢" if r["live"] else ("🪐" if r["where"] == "s3" else "🌌")
        lines.append(f"-# {mark} **{r['label']}** · `{r['project']}` · {age_str(r['mtime'])} ago · `{r['sid'][:8]}`")
    lines.append("-# 🟢 live · 🌌 on disk · 🪐 only in the S3 backup (restored when picked) · "
                 "`!resume <words>` filters by name, first prompt, project or id")
    return "\n".join(lines)[:1990]


def recent_conversation(transcript, nchars=6000):
    """Plain-text tail of a transcript (who said what) for a reviewer's brief."""
    if not transcript or not Path(transcript).exists():
        return ""
    try:
        items, _ = checkin.parse_transcript(Path(transcript), 0, 400 * 1024, True)
    except OSError:
        return ""
    out = []
    for it in items:
        if it["kind"] == "assistant":
            out.append("CLAUDE: " + it["text"])
        elif it["kind"] == "user":
            out.append("USER: " + it["text"])
        elif it["kind"] == "tool":
            out.append(f"[tool {it.get('name')}: {it.get('text', '')[:120]}]")
    return "\n".join(out)[-nchars:]


def render_handoff(name, sid, cwd, transcript, max_chars=ASTRA_HANDOFF_CHARS):
    """A Claude Code session's conversation as a markdown brief for another agent: header,
    then who said what — user, Claude, tool calls as one-liners, results — oldest first,
    cut to the last `max_chars`. Written to a file the new agent reads first."""
    try:
        items, _ = checkin.parse_transcript(Path(transcript), 0, 12 * 1024 * 1024, True)
    except OSError:
        items = []
    lines = []
    for it in items:
        k, t = it["kind"], it.get("text", "")
        if k == "user":
            lines.append(f"\n**USER:** {t}")
        elif k == "assistant":
            lines.append(f"\n**CLAUDE:** {t}")
        elif k == "tool":
            lines.append(f"- tool {it.get('name')}: {one_line(t, 200)}")
        elif k == "cmd":
            lines.append(f"- $ {one_line(t, 200)}")
        elif k == "result":
            lines.append(f"  ↳ {one_line(t, 300)}")
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = "…(earlier conversation cut — the JSONL transcript has all of it)…\n" + body[-max_chars:]
    head = (f"# Handoff from Claude Code session {name}\n\n- session id: {sid}\n- project: {cwd}\n"
            f"- transcript (JSONL, exact tool outputs): {transcript}\n"
            f"- rendered: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n\n"
            "The human forked this Claude Code session into a GPT-6 Astra (codex) session. Below is "
            "the conversation so far, oldest first; tool calls are one-liners.\n\n## Conversation\n")
    return head + body + "\n"


def feldspar_brief(target, focus, requester):
    lines = ["# Feldspar review brief", "",
             f"- requested by: {requester}",
             f"- when: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
             f"- target project: {target['cwd']}",
             f"- focus: {focus}"]
    if target.get("sid"):
        lines += [f"- session under review: {target.get('name') or '?'} ({target['sid']})",
                  f"- transcript: {target.get('transcript') or 'n/a'}"]
    conv = recent_conversation(target.get("transcript"))
    if conv:
        lines += ["", "## Recent conversation (tail)", "", "```", conv, "```"]
    return "\n".join(lines) + "\n"


def feldspar_claude_prompt(folder, target, focus):
    about = (f" Session under review: {target.get('name') or '?'} — transcript {target['transcript']}; "
             f"read its tail to see what was done and what was claimed." if target.get("transcript") else "")
    return (
        f"You are Feldspar's expedition: an adversarial review team, not a helper. Read the brief at "
        f"{folder}/brief.md first. Target project: {target['cwd']}.{about} Focus: {focus}.\n"
        "Method: fan out hard. Agent teams are enabled for you (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1): "
        "spawn a teammate per big angle — they are full sessions with their own context and show up as "
        "travelers in Discord — and use subagents (Agent tool, model \"fable\") for smaller checks. "
        "Angles: correctness bugs; data leakage / train-test contamination; statistics, baselines and "
        "significance; silent failure modes and swallowed errors; config/hparam mismatches versus what "
        "the writeup claims; reproducibility; edge cases and off-by-ones; unsupported conclusions. "
        "Every candidate finding must be verified against the actual code/data/logs before it is "
        "reported; drop anything unconfirmed. Rank by severity.\n"
        f"Deliverable: write {folder}/report.md (ranked findings: file:line, why it matters, a concrete "
        f"failure scenario, a fix) and {folder}/report.html (follow ~/shared/reports/style-guide/, link "
        "/reports/static/claude.css, tldr box first, findings table color-coded by severity). "
        f"A second reviewer, OpenAI {FELDSPAR_OPENAI_MODEL} via codex at effort {FELDSPAR_OPENAI_EFFORT}, "
        f"is working independently and writes {folder}/openai-review.md. Before you finalise, check "
        "whether it exists: if so, verify each of its findings against the code and fold them in under "
        "its attribution, marked confirmed / refuted / new, and call out where you disagree. If it isn't "
        "there when you are done, publish without it and do NOT wait: the bridge will message you in "
        "this session the moment it lands, and you then fold it in the same way, update report.md and "
        "report.html in place and re-post the link. Finally reply here with the top findings and the "
        f"link {report_link(folder)}. Review only: do not modify the target project.")


def feldspar_codex_prompt(folder, target, focus):
    about = (f" The session under review left its transcript at {target['transcript']} (JSONL; read the "
             "tail to see what was done and claimed)." if target.get("transcript") else "")
    return (
        f"You are an adversarial reviewer. Read the brief at {folder}/brief.md first. Target project: "
        f"{target['cwd']}.{about} Focus: {focus}.\n"
        "Hunt for bugs, flaws and methodological errors: correctness, data leakage, statistics and "
        "baselines, silent failures, config versus claims, reproducibility, edge cases, unsupported "
        "conclusions. You may read the whole filesystem (the transcript, logs, wandb exports and "
        "~/shared/reports included); search with rg. Verify each finding against the actual code before "
        "reporting it; drop anything unconfirmed. Your FINAL message must be the complete report in "
        "markdown: a one-paragraph verdict, then findings ranked by severity, each headed "
        "`### [HIGH|MEDIUM|LOW] title` with file:line, why it matters, a concrete failure scenario and "
        "a fix. If you could not read the files, say so in the very first line. Read-only: do not "
        "modify anything.")


def codex_sandbox_broken(log, report):
    """True when codex's bubblewrap sandbox died before any command ran, so the 'report' is
    really an apology about not being able to read files. Signature on Ubuntu 24.04
    (kernel.apparmor_restrict_unprivileged_userns=1, no AppArmor profile for bwrap):
    'bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted'."""
    try:
        text = log.read_text(errors="replace") if log.exists() else ""
    except OSError:
        text = ""
    blob = text + "\n" + (report or "")
    if "bwrap:" in blob and "Operation not permitted" in blob:
        return True
    return ("needs access to create user namespaces" in text
            and "sandbox" in (report or "").lower() and len(report or "") < 4000)


class _SlashMsg:
    """Just enough of a discord.Message for the text-command handlers, so every slash command
    runs exactly the same code as typing its `!` form in that channel. Reactions are no-ops:
    the interaction's own (ephemeral) reply is the acknowledgement."""
    def __init__(self, interaction, channel, content):
        self.content = content
        self.channel = channel
        self.author = interaction.user
        self.guild = interaction.guild
        self.id = interaction.id
        self.attachments, self.mentions = [], []
        self.reference = self.webhook_id = None

    async def add_reaction(self, emoji):
        return None

    async def reply(self, content=None, **kw):
        return await self.channel.send(content, **kw)


class _Owner:
    """Stand-in author for messages the bridge itself types into a session (they arrive
    verbatim, like the owner's, instead of prefixed with a name)."""
    def __init__(self, owner_id):
        self.id, self.display_name = owner_id, "chert"


def resume_view(query, page, rows):
    """A Select of the sessions on this page + paging buttons. custom_ids carry the
    query and page so the list keeps working after a bridge restart."""
    view = discord.ui.View(timeout=None)
    q = query.replace("|", " ")[:50]
    chunk = rows[page * RESUME_PAGE:(page + 1) * RESUME_PAGE]
    if chunk:
        sel = discord.ui.Select(custom_id=f"rs|{q}|{page}", min_values=1, max_values=1,
                                placeholder=f"pick a session to bring up ({len(rows)} listed)")
        for r in chunk:
            mark = "🟢 " if r["live"] else ("🪐 " if r["where"] == "s3" else "")
            size = ash_twin.human(r["size"]) if ash_twin else str(r["size"])
            sel.add_option(label=(mark + r["label"])[:100], value=r["sid"],
                           description=f"{r['project']} · {age_str(r['mtime'])} ago · {size} · {r['sid'][:8]}"[:100])
        view.add_item(sel)
    if page > 0:
        view.add_item(discord.ui.Button(label="◀ newer", custom_id=f"rp|{q}|{page - 1}",
                                        style=discord.ButtonStyle.secondary, row=1))
    if (page + 1) * RESUME_PAGE < len(rows):
        view.add_item(discord.ui.Button(label="older ▶", custom_id=f"rp|{q}|{page + 1}",
                                        style=discord.ButtonStyle.secondary, row=1))
    return view


# ---------- prompt buttons ----------

CONTROL_KEYS = [("esc", "Escape", discord.ButtonStyle.danger),
                ("⏎", "Enter", discord.ButtonStyle.success),
                ("↑", "Up", discord.ButtonStyle.secondary),
                ("↓", "Down", discord.ButtonStyle.secondary),
                ("tab", "Tab", discord.ButtonStyle.secondary),
                ("space", "Space", discord.ButtonStyle.secondary),
                ("🖥 refresh", "__refresh", discord.ButtonStyle.secondary)]


def prompt_view(key, opts):
    """Buttons for a waiting prompt: one per numbered option, then the control keys.
    custom_id carries everything needed (`o|<key>|<n>` / `k|<key>|<tmux key>`), so a
    press is handled even after a bridge restart — no in-memory View required."""
    view = discord.ui.View(timeout=None)
    slot = 0                        # discord: 5 rows × 5 buttons; row = slot // 5
    for n, label in opts[:10]:
        negative = re.match(r"(no\b|don't|do not|cancel|exit|reject|deny)", label, re.I)
        style = discord.ButtonStyle.secondary if negative else discord.ButtonStyle.primary
        view.add_item(discord.ui.Button(label=f"{n}. {label}"[:80], style=style,
                                        custom_id=f"o|{key}|{n}", row=slot // 5))
        slot += 1
    if slot % 5:
        slot += 5 - slot % 5        # controls start on their own row
    for label, tk, style in CONTROL_KEYS:
        if slot >= 25:
            break
        view.add_item(discord.ui.Button(label=label, style=style,
                                        custom_id=f"k|{key}|{tk}", row=slot // 5))
        slot += 1
    return view


def prompt_body(text, note=""):
    body = text[-1500:] if text else "(blank screen)"
    return f"{note}```\n{body}\n```"


class Bridge(discord.Client):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.webhooks = {}  # channel_id -> discord.Webhook
        self._avatared = set()
        self.chat_channel = None
        self.broadcast_channel = None   # #all-claudes: every message -> every live claude
        self.main_channel = None
        self.owner = 0
        self._last_disk = 0.0
        self._offload_lock = asyncio.Lock()
        self._rebooted = False
        self._sent = {}          # session key -> deque of (ts, text) typed in via Discord
        self._astra_busy = set()   # Astra thread ids with a codex turn in flight
        self._astra_queue = {}     # Astra thread id -> messages that arrived mid-turn
        self._astra_procs = {}     # Astra thread id -> running codex process
        self.slash_error = None  # why slash commands didn't register, if they didn't
        self._tick_lock = asyncio.Lock()   # hook fast-paths and the poller share one tick
        self._hook_backlog = []            # events that arrived before the bridge was ready
        self._subs_pending = set()         # session keys with a subagent line render scheduled
        self._rate_limits = {}             # session key -> deque of StopFailure timestamps
        self._hook_secret = None
        self._twin_wait = {}               # new key -> first seen, while its sid is still live elsewhere
        self.tree = discord.app_commands.CommandTree(self)
        self.register_commands()

    async def setup_hook(self):
        self.poller = asyncio.create_task(self.poll_loop())
        try:
            await self.start_hook_server()
        except OSError as e:
            print(f"hook listener NOT started on 127.0.0.1:{HOOK_PORT} ({e}) — polling only", flush=True)

    # ---------- hook listener ----------

    def hook_secret(self):
        if self._hook_secret is None:
            try:
                self._hook_secret = HOOK_SECRET_FILE.read_text().strip()
            except OSError:
                self._hook_secret = ""
        return self._hook_secret

    async def start_hook_server(self):
        app = web.Application(client_max_size=1_000_000)
        app.router.add_post("/hook", self.hook_http)
        app.router.add_post("/admin/restart-all", self.admin_restart_all)
        app.router.add_post("/session-file", self.session_file_http)
        app.router.add_get("/health", lambda r: web.Response(text="ok\n"))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", HOOK_PORT)
        await site.start()
        self._hook_runner = runner
        print(f"hook listener on 127.0.0.1:{HOOK_PORT}", flush=True)

    async def hook_http(self, request):
        secret = self.hook_secret()
        if secret and request.headers.get("X-Hearth-Secret", "") != secret:
            return web.Response(status=403, text="bad secret\n")
        try:
            ev = await request.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400, text="bad json\n")
        if isinstance(ev, dict):
            asyncio.create_task(self.on_hook(ev))
        return web.Response(status=204)

    async def session_file_http(self, request):
        """Local endpoint a claude uses (via `hearth-send`) to post a file/image inline into its
        own Discord thread. Body: {path, caption?, pane?, sid?, cwd?}. The file stays on the box —
        only its bytes are uploaded to the mapped thread."""
        secret = self.hook_secret()
        if secret and request.headers.get("X-Hearth-Secret", "") != secret:
            return web.Response(status=403, text="bad secret\n")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400, text="bad json\n")
        path = body.get("path")
        if not path or not os.path.isfile(path):
            return web.Response(status=404, text="file not found\n")
        if self.main_channel is None:
            return web.Response(status=503, text="bridge not ready\n")
        asyncio.create_task(self.deliver_session_file(
            path, (body.get("caption") or "").strip(), body.get("pane"), body.get("sid"), body.get("cwd")))
        return web.Response(status=202, text="queued\n")

    async def deliver_session_file(self, path, caption, pane, sid, cwd):
        """Upload a file into the thread of the session it came from (matched by tmux pane, then
        sid, then cwd). Falls back to the main channel, named, if the thread can't be found."""
        sess = await asyncio.to_thread(live_sessions)
        match = None
        if pane:
            match = next((s for s in sess if s.get("pane") == pane), None)
        if not match and sid:
            match = next((s for s in sess if s.get("sid") == sid), None)
        if not match and cwd:
            match = next((s for s in sess if s.get("cwd") == cwd), None)
        st = state.get(match["key"]) if match else None
        thread = await self.get_thread(st["thread"]) if st and st.get("thread") else None
        target = thread or self.main_channel
        name = (match or {}).get("name")
        size = os.path.getsize(path)
        head = caption
        if not thread and name:                       # posting outside its own thread — name it
            head = f"**{name}**" + (f": {caption}" if caption else "")
        if size > FILE_LIMIT_BYTES:
            return await self.say(target, f"⚠️ **{name or 'a session'}** tried to send "
                                          f"`{os.path.basename(path)}` but it is {human_size(size)} "
                                          f"(Discord limit ~{FILE_LIMIT_BYTES // (1024*1024)}MB). "
                                          f"Path on the box: `{path}`")
        try:
            f = await asyncio.to_thread(discord.File, path)
            await target.send(content=(head or None), file=f, allowed_mentions=NO_PING,
                              suppress_embeds=False)
        except discord.HTTPException as e:
            if thread and e.code == 50083 and await self.unarchive(st["thread"]):
                try:
                    await target.send(content=(head or None),
                                      file=await asyncio.to_thread(discord.File, path),
                                      allowed_mentions=NO_PING)
                    return
                except discord.HTTPException:
                    pass
            await self.say(target, f"⚠️ couldn't post `{os.path.basename(path)}`: {e}")

    async def admin_restart_all(self, request):
        """Local-only trigger for a rolling restart (same as `!restart all`), e.g. from a
        claude on this box after a settings/key change. Body: {"force": bool, "skip": [pid]}."""
        secret = self.hook_secret()
        if not secret or request.headers.get("X-Hearth-Secret", "") != secret:
            return web.Response(status=403, text="bad secret\n")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if self.main_channel is None:
            return web.Response(status=503, text="bridge not ready\n")
        asyncio.create_task(self.restart_all(self.main_channel, force=bool(body.get("force")),
                                             skip_pids=set(int(p) for p in body.get("skip", []))))
        return web.Response(status=202, text="rolling restart started\n")

    async def drain_spool(self):
        """Events the hook wrote to disk while the bridge was down."""
        if not HOOK_SPOOL.exists():
            return
        for f in sorted(HOOK_SPOOL.glob("*.json"))[:200]:
            try:
                ev = json.loads(f.read_text())
                f.unlink()
            except (OSError, json.JSONDecodeError):
                continue
            age = time.time() - ev.get("_received", 0)
            if age > 900 and ev.get("hook_event_name") not in ("SessionEnd", "StopFailure"):
                continue
            await self.on_hook(ev)

    async def resolve_hook_session(self, ev):
        """(s, key, st) for a hook payload: by claude pid first (exact, works before the
        registry file exists), then by session id + transcript path."""
        sid, pid, tp = ev.get("session_id"), ev.get("_pid"), ev.get("transcript_path")
        sessions = await asyncio.to_thread(live_sessions)
        s = next((x for x in sessions if pid and x["pid"] == pid), None)
        if s is None and sid:
            cands = [x for x in sessions if x["sid"] == sid]
            s = next((x for x in cands if tp and x["transcript"] == tp), cands[0] if cands else None)
        if s is None:
            return None, None, None
        st = state.get(s["key"])
        return s, s["key"], (None if st is None or st.get("pending") else st)

    async def run_tick(self):
        async with self._tick_lock:
            await self.tick(self.main_channel)

    async def run_tick_for(self, key):
        async with self._tick_lock:
            s = await asyncio.to_thread(find_live_by_key, key)
            st = state.get(key)
            if s and st and not st.get("pending") and not st.get("ended"):
                await self.tick_session(self.main_channel, key, s, st)

    async def on_hook(self, ev):
        name = ev.get("hook_event_name", "?")
        if self.main_channel is None:
            if len(self._hook_backlog) < 200:
                self._hook_backlog.append(ev)
            return
        try:
            s, key, st = await self.resolve_hook_session(ev)
            thread = await self.get_thread(st.get("thread")) if st else None
            if name not in ("SubagentStart", "SubagentStop"):
                print(f"hook {name} sid={str(ev.get('session_id', ''))[:8]} pid={ev.get('_pid')} "
                      f"-> {key or 'unknown session'}"
                      + (f" ({ev.get('source') or ev.get('reason') or ev.get('notification_type') or ev.get('error_type') or ''})"),
                      flush=True)
            if name == "SessionStart":
                src = ev.get("source") or ev.get("matcher") or ""
                if st and src in ("clear", "fork"):
                    st["sid_note"] = src
                await asyncio.sleep(0.4)          # let the registry file land
                await self.run_tick()             # opens the thread within a second
            elif name == "SessionEnd":
                if st:
                    st["end_reason_hint"] = ev.get("reason") or ""
                await asyncio.sleep(0.5)
                await self.run_tick()             # pid liveness decides; the hint words it
            elif name == "Stop":
                if key:
                    if st and st.get("subs"):
                        st["subs"]["closed"] = True   # next subagent run starts a new line
                    await asyncio.sleep(0.3)      # transcript flush
                    await self.run_tick_for(key)
            elif name == "StopFailure":
                await self.hook_stop_failure(ev, s, key, st, thread)
            elif name == "Notification":
                ntype = ev.get("notification_type") or ev.get("matcher") or ""
                if s and st and thread and ntype != "idle_prompt":
                    if not (st.get("prompt_msg") and time.time() - st.get("prompt_at", 0) < 30):
                        await asyncio.sleep(0.6)  # the dialog is still painting
                        s = await asyncio.to_thread(find_live_by_key, key) or s
                        await self.open_prompt(s, st, thread, note=ev.get("message") or "")
                        save_state()
            elif name in ("SubagentStart", "SubagentStop"):
                if st and thread:
                    self.hook_subagent(name, ev, st)
                    self.schedule_subs_render(key, s, st)
            elif name == "PreCompact":
                if thread:
                    await self.say(thread, f"-# 🌀 compacting context ({ev.get('trigger') or 'auto'})…")
            elif name == "PostCompact":
                if thread:
                    await self.say(thread, "-# 🌀 compacted — context is smaller now, memory intact")
        except Exception as e:  # noqa: BLE001
            log_error(f"hook {name}", e)

    async def hook_stop_failure(self, ev, s, key, st, thread):
        etype = ev.get("error_type") or ev.get("matcher") or "error"
        detail = str(ev.get("error") or ev.get("message") or "")[:300]
        if not thread:
            return
        if etype == "rate_limit" or etype == "overloaded":
            q = self._rate_limits.setdefault(key, deque(maxlen=20))
            q.append(time.time())
            recent = sum(1 for t in q if time.time() - t < 600)
            await self.say(thread, f"-# ⏳ API {etype.replace('_', ' ')} — Claude Code retries by itself"
                                   + (f" · {recent}× in 10 min" if recent > 1 else ""))
            if recent == 3:
                await self.say(self.main_channel,
                               f"⏳ **{st.get('name', '?')}** hit `{etype}` 3× in 10 min · <#{thread.id}> — "
                               "if this run matters, this is when the fallback key earns its keep")
        elif etype in ("authentication_failed", "billing_error", "invalid_api_key"):
            await self.say(thread, f"🔑 **turn failed: {etype}** {detail}", ping_owner=True)
            await self.say(self.main_channel,
                           f"🔑 **{st.get('name', '?')}**: `{etype}` — check the API key · <#{thread.id}>")
        else:
            await self.say(thread, f"-# ⚠️ turn failed: `{etype}` {detail}")

    def hook_subagent(self, name, ev, st):
        subs = st.get("subs")
        if not subs or subs.get("closed"):
            subs = st["subs"] = {"live": {}, "done": {}, "msg": None, "closed": False, "started": time.time()}
        aid = str(ev.get("agent_id") or ev.get("tool_use_id") or time.time())
        atype = str(ev.get("agent_type") or "agent")
        if name == "SubagentStart":
            subs["live"][aid] = atype
        else:
            atype = subs["live"].pop(aid, atype)
            subs["done"][atype] = subs["done"].get(atype, 0) + 1

    def schedule_subs_render(self, key, s, st):
        if key in self._subs_pending:
            return
        self._subs_pending.add(key)

        async def later():
            try:
                await asyncio.sleep(2.0)          # coalesce a burst of starts/stops
                self._subs_pending.discard(key)
                await self.render_subs(key, s, st)
            except Exception as e:  # noqa: BLE001
                self._subs_pending.discard(key)
                log_error("subs render", e)
        asyncio.create_task(later())

    async def render_subs(self, key, s, st):
        subs = st.get("subs")
        if not subs:
            return
        live_c = Counter(subs["live"].values())
        fmt = lambda c: ", ".join(f"{t} ×{n}" if n > 1 else t for t, n in c.items())  # noqa: E731
        done_n = sum(subs["done"].values())
        if subs["live"]:
            line = f"-# 🧭 {len(subs['live'])} subagent{'s' if len(subs['live']) != 1 else ''} exploring: {fmt(live_c)}"
            if done_n:
                line += f" · {done_n} done"
        else:
            line = f"-# 🧭 {done_n} subagent{'s' if done_n != 1 else ''} done: {fmt(Counter(subs['done']))}"
        line = line[:1990]
        parent = await self.get_thread(st.get("parent", CHANNEL_ID)) or self.main_channel
        if subs.get("msg"):
            try:
                await self.edit_as(parent, subs["msg"], line, st["thread"])
                return
            except discord.HTTPException:
                pass
        m = await self.post_as(parent, webhook_name(s), line, st["thread"], seed=s["key"])
        subs["msg"] = m.id if m else None
        save_state()

    def deliver(self, s, author, text):
        """Type `text` into a session on behalf of `author` (a Discord user). Remembers what
        was typed so the terminal's echo of the same words is not re-posted into the
        thread (it is already there — the user wrote it)."""
        payload = attribute(author.id, author.display_name, text, self.owner)
        ok, err = checkin.send_to_session(s, payload)
        if ok:
            self._sent.setdefault(s["key"], deque(maxlen=30)).append(
                (time.time(), " ".join(payload.split())[:200]))
        return ok, err

    def is_echo(self, key, text):
        norm = " ".join((text or "").split())[:200]
        if not norm:
            return False
        now = time.time()
        return any(now - ts < 1800 and (norm == t or norm.startswith(t[:120]) or t.startswith(norm[:120]))
                   for ts, t in self._sent.get(key, ()))

    # ---------- plumbing ----------

    async def get_thread(self, tid):
        if not tid:
            return None
        ch = self.get_channel(tid)
        if ch:
            return ch
        try:
            return await self.fetch_channel(tid)
        except (discord.NotFound, discord.Forbidden):
            return None

    async def unarchive(self, thread_id):
        """Discord auto-archives threads after 7 quiet days and then refuses posts
        (error 50083). Reopen and let the caller retry."""
        thread = await self.get_thread(thread_id)
        if thread is None or not getattr(thread, "archived", False):
            return False
        try:
            await thread.edit(archived=False)
            return True
        except discord.HTTPException:
            return False

    async def webhook_for(self, channel):
        wh = self.webhooks.get(channel.id)
        if wh:
            return wh
        for existing in await channel.webhooks():
            if existing.name == WEBHOOK_NAME and existing.token:
                wh = existing
                break
        else:
            wh = await channel.create_webhook(name=WEBHOOK_NAME)
        self.webhooks[channel.id] = wh
        await self.apply_avatar(wh)
        return wh

    async def apply_avatar(self, wh):
        img = avatar_bytes()
        if img is None or wh.id in self._avatared:
            return
        self._avatared.add(wh.id)
        try:
            await wh.edit(avatar=img)
        except discord.HTTPException as e:
            print(f"webhook {wh.id}: avatar edit failed ({e})", flush=True)

    async def post_as(self, channel, name, content, thread_id=None, seed=None):
        """Post under a per-agent identity via the channel webhook. Returns the message.

        `seed` fixes the avatar to something stable (a session's pid:procStart).
        """
        wh = await self.webhook_for(channel)
        kwargs = dict(content=content[:2000], username=name[:WEBHOOK_NAME_MAX],
                      allowed_mentions=NO_PING, suppress_embeds=True, wait=True)
        if avatar_bytes() is None:
            kwargs["avatar_url"] = avatar_url(seed or name)
        if thread_id:
            kwargs["thread"] = discord.Object(id=thread_id)
        try:
            return await wh.send(**kwargs)
        except discord.NotFound:  # webhook deleted -> recreate once
            self.webhooks.pop(channel.id, None)
            wh = await self.webhook_for(channel)
            return await wh.send(**kwargs)
        except discord.HTTPException as e:
            if thread_id and e.code == 50083 and await self.unarchive(thread_id):
                return await wh.send(**kwargs)
            raise

    async def edit_as(self, channel, message_id, content, thread_id=None):
        """Edit a previously webhook-posted message (used to grow the tool-call line)."""
        wh = await self.webhook_for(channel)
        kwargs = {"content": content[:2000]}
        if thread_id:
            kwargs["thread"] = discord.Object(id=thread_id)
        await wh.edit_message(message_id, **kwargs)

    async def post_entry(self, parent, s, st, kind, payload):
        """Post one streamed entry, merging consecutive tool runs into one edited line."""
        if kind == "tool":
            merged = dict(st.get("tool_run") or {})
            for name, k in payload.items():
                merged[name] = merged.get(name, 0) + k
            line = render_tools(merged)
            if st.get("tool_msg") and len(line) < 1900:
                try:
                    await self.edit_as(parent, st["tool_msg"], line, st["thread"])
                    st["tool_run"] = merged
                    return
                except discord.HTTPException:
                    pass  # message gone / too old -> fall through and post fresh
            m = await self.post_as(parent, webhook_name(s), render_tools(payload),
                                   st["thread"], seed=s["key"])
            st["tool_msg"] = m.id if m else None
            st["tool_run"] = dict(payload)
        else:
            await self.post_as(parent, webhook_name(s), payload, st["thread"], seed=s["key"])
            st["tool_msg"] = None
            st["tool_run"] = None

    async def edit_status(self, st, s):
        thread = await self.get_thread(st.get("thread"))
        if not thread or not st.get("status_msg"):
            return thread
        try:
            m = await thread.fetch_message(st["status_msg"])
            await m.edit(content=status_line(s, st), suppress=True, allowed_mentions=NO_PING)
        except discord.HTTPException:
            pass
        return thread

    async def send_help(self, channel):
        """HELP in ≤2000-char chunks (one oversized message used to fail silently)."""
        for chunk in split_chunks(HELP):
            await channel.send(chunk, suppress_embeds=True, allowed_mentions=NO_PING)

    async def say(self, channel, text, ping_owner=False):
        """Bot-voice message to a channel/thread, chunked. Returns the first message."""
        if channel is None:
            return None
        ping = f"<@{self.owner}> " if ping_owner and self.owner else ""
        first = None
        for i, chunk in enumerate(split_chunks(text)):
            mentions = discord.AllowedMentions(users=True) if (ping and i == 0) else NO_PING
            try:
                m = await channel.send((ping if i == 0 else "") + chunk, suppress_embeds=True,
                                       allowed_mentions=mentions)
                first = first or m
            except discord.HTTPException as e:
                log_error("say", e)
        return first

    # ---------- main loop ----------

    async def poll_loop(self):
        await self.wait_until_ready()
        channel = None
        while channel is None and not self.is_closed():
            try:
                channel = await self.fetch_channel(CHANNEL_ID)
            except discord.HTTPException:
                print(f"can't access channel {CHANNEL_ID} yet — has the bot been "
                      "invited to the server? retrying in 30s")
                await asyncio.sleep(30)
        self.main_channel = channel
        guild = channel.guild
        self.owner = OWNER_ID or (guild.owner_id if guild else 0)
        if guild:
            # guild-scoped sync is instant (global takes up to an hour). Fails with
            # "Missing Access" when the bot was invited without the applications.commands
            # scope — then !help shows the re-invite link and the !commands keep working.
            try:
                self.tree.copy_global_to(guild=guild)
                cmds = await self.tree.sync(guild=guild)
                print(f"slash commands synced: {[c.name for c in cmds]}")
            except discord.HTTPException as e:
                self.slash_error = str(e)
                print(f"slash command sync failed: {e} — re-invite with the applications.commands "
                      f"scope: {self.invite_url()}")
        if CHAT_CHANNEL_ID:
            self.chat_channel = await self.fetch_channel(CHAT_CHANNEL_ID)
        elif guild:
            self.chat_channel = discord.utils.get(guild.text_channels, name="claude-chat") \
                or await guild.create_text_channel(
                    "claude-chat", topic="two-way bridge to the claude↔claude bus "
                    "(cchat.py) — type here to talk to all listening claudes")
        try:
            if BROADCAST_CHANNEL_ID:
                self.broadcast_channel = await self.fetch_channel(BROADCAST_CHANNEL_ID)
            elif guild:
                self.broadcast_channel = discord.utils.get(guild.text_channels, name=BROADCAST_CHANNEL_NAME) \
                    or await guild.create_text_channel(
                        BROADCAST_CHANNEL_NAME, topic="📣 everything typed here is delivered to EVERY "
                        "live claude (no !all needed) — attachments are saved and their paths passed along")
        except discord.HTTPException as e:
            log_error("broadcast channel", e)
        meta = state["_meta"]
        if "chat_offset" not in meta:
            meta["chat_offset"] = CHAT_LOG.stat().st_size if CHAT_LOG.exists() else 0
            save_state()
        migrate_state(await asyncio.to_thread(live_sessions))
        # reboot detection: the kernel's boot_id changes exactly once per boot
        bid = boot_id()
        if bid and meta.get("boot_id") and meta["boot_id"] != bid:
            self._rebooted = True
            print("boot_id changed since last run — the box rebooted")
        if bid:
            meta["boot_id"] = bid
            save_state()
        last_presence = None
        print(f"bridge up in #{channel}; chat bridge: #{self.chat_channel}; "
              f"broadcast: #{self.broadcast_channel}; owner {self.owner}; "
              f"revive_on_boot={REVIVE_ON_BOOT} disk warn/crit={DISK_WARN_GB}/{DISK_CRIT_GB} GB")
        backlog, self._hook_backlog = self._hook_backlog, []
        for ev in backlog:
            asyncio.create_task(self.on_hook(ev))
        while not self.is_closed():
            try:
                await self.run_tick()
            except Exception as e:  # noqa: BLE001
                log_error("tick", e)
            for fn in (self.chat_tick, self.prompt_tick, self.disk_tick, self.drain_spool,
                       self.board_tick, self.supernova_tick):
                try:
                    await fn()
                except Exception as e:  # noqa: BLE001
                    log_error(fn.__name__, e)
            try:
                n = len([1 for v in sessions_state().values() if not v.get("ended")])
                free = f" · 💾 {ash_twin.disk_free()[0]:.0f} GB free" if ash_twin else ""
                pres = f"🔭 {n} traveler{'s' if n != 1 else ''}{free}"
                if pres != last_presence:
                    last_presence = pres
                    await self.change_presence(activity=discord.CustomActivity(pres))
            except Exception as e:  # noqa: BLE001
                log_error("presence", e)
            await asyncio.sleep(POLL_SECS)

    async def tick(self, channel):
        # keyed by pid:procStart, never sessionId: two live claudes can share a
        # sessionId right after a fork/resume, and a dict keyed on sid would silently
        # drop one of them (which then looked like "my session vanished").
        sessions = {s["key"]: s for s in await asyncio.to_thread(live_sessions)}
        created = 0
        for key, s in sessions.items():
            st = state.get(key)
            if st is not None and (st.get("pending") or st.get("restarting")):
                continue  # a summon/restart is mid-flight and owns this session's thread
            if st is not None and st.get("ended") and st.get("thread"):
                # This exact process is live again although we recorded it as ended: it only
                # dropped out of one registry read. Keep its thread; never open a twin over it.
                if await self.reopen_thread(key, s, st):
                    continue
            if st is None or st.get("ended"):
                # A NEW process carrying a conversation we already have a thread for — `claude -r`
                # in the pane, a /refresh or /restart, or a fork/restart whose entry we lost.
                # Continue in that thread instead of opening a twin.
                if st is None and s.get("sid"):
                    same = [(k, v) for k, v in sessions_state().items()
                            if v.get("sid") == s["sid"] and v.get("thread") and k != key]
                    old_key, old = next(((k, v) for k, v in same if k not in sessions), (None, None))
                    if not old and any(k in sessions for k, _ in same):
                        # the old process is still shutting down (restarts overlap by a few
                        # seconds): wait for it rather than treat this as a second live copy.
                        # Two genuinely live copies of one conversation get their own thread
                        # after TWIN_GRACE seconds.
                        first = self._twin_wait.setdefault(key, time.time())
                        if time.time() - first < TWIN_GRACE:
                            continue
                    if old:
                        self._twin_wait.pop(key, None)
                        state[key] = {**old, "ended": False, "ended_at": None, "ended_reason": None,
                                      "status": s["status"], "name": s["name"], "cwd": s["cwd"] or old.get("cwd"),
                                      "size": os.path.getsize(s["transcript"]) if s["transcript"] else 0,
                                      "tool_msg": None, "tool_run": None, "prompt_msg": None, "card": None,
                                      "thread_missing": 0, "thread_name": None}
                        state[key].pop("restarting", None)
                        state.pop(old_key, None)
                        save_state()
                        thread = await self.get_thread(old["thread"])
                        if thread:
                            try:
                                if getattr(thread, "archived", False):
                                    await thread.edit(archived=False)
                                await thread.send("-# ♻️ resumed as a new process — same conversation, "
                                                  "continuing here", allowed_mentions=NO_PING)
                            except discord.HTTPException as e:
                                log_error("resume-thread", e)
                        print(f"resume: {s['name']} ({s['sid'][:8]}) {old_key} -> {key}, kept thread {old['thread']}",
                              flush=True)
                        continue
                if created >= 6:
                    continue  # spread thread creation across ticks
                # a pane-less session younger than 20 s is almost always a one-shot
                # `claude -p`; real background agents outlive this and get their thread
                if not s.get("pane") and time.time() * 1000 - (s.get("started_at") or 0) < 20000:
                    continue
                created += 1
                self._twin_wait.pop(key, None)
                try:
                    await self.open_thread(channel, s, key)
                except Exception as e:  # noqa: BLE001
                    log_error(f"open_thread {key}", e)
                continue
            try:
                await self.tick_session(channel, key, s, st)
            except Exception as e:  # noqa: BLE001 — one broken session must not stall the rest
                log_error(f"session {key}", e)
        # sessions that went away — but only if the process really is gone: a session that
        # merely dropped out of one registry read must not be ended (and its thread archived)
        gone = [(k, st) for k, st in sessions_state().items()
                if k not in sessions and not st.get("ended") and not st.get("pending")
                and not st.get("restarting") and not key_process_alive(k)]
        if gone:
            phantoms = await asyncio.to_thread(checkin.phantom_keys)
            for key, st in gone:
                try:
                    await self.session_gone(key, st, phantoms)
                except Exception as e:  # noqa: BLE001
                    log_error(f"gone {key}", e)
        if self._rebooted:
            self._rebooted = False
            await self.after_reboot()

    async def tick_session(self, channel, key, s, st):
        # fork / branch / clear: same process, brand new sessionId + transcript
        if st.get("sid") and s["sid"] != st["sid"]:
            st["sid"] = s["sid"]
            # start at the END of the new transcript: a fork's file already
            # contains the whole inherited history, which must not be replayed
            st["size"] = os.path.getsize(s["transcript"]) if s["transcript"] else 0
            st["tool_msg"] = None
            thread = await self.get_thread(st["thread"])
            if thread:
                what = st.pop("sid_note", None)
                await thread.send(f"-# ↪ {what + 'ed' if what else 'forked / branched / cleared'} to a new "
                                  "session — still tracking it here", allowed_mentions=NO_PING)
            save_state()
        # rename (/rename or -n): sessionId is unchanged, so only the name moves.
        # Compare against the title we last SET, so a title can never drift.
        collides = any(v.get("name") == s["name"] and k != key and not v.get("ended")
                       for k, v in sessions_state().items())
        title = thread_title(s["name"], s["sid"], collides)
        if title != st.get("thread_name"):
            backfill = st.get("thread_name") is None
            st["thread_name"] = title
            st["name"] = s["name"]
            await self.edit_status(st, s)
            thread = await self.get_thread(st["thread"])
            if thread:
                try:
                    await thread.edit(name=title)
                except discord.HTTPException:
                    pass
                if not backfill:
                    await thread.send(f"-# ✏️ renamed to **{s['name']}**", allowed_mentions=NO_PING)
            save_state()
        st["cwd"] = s["cwd"] or st.get("cwd")
        # a /model or /fast that was queued because the claude was busy or showing a prompt
        # (/fast is an "immediate" command in Claude Code, so it only waits out prompts)
        for pk, cmd, blocked in (("pending_model", "/model", ("busy", "waiting")),
                                 ("pending_fast", "/fast", ("waiting",))):
            if st.get(pk) and s.get("pane") and s["status"] not in blocked:
                val = st.pop(pk)
                ok, _ = await self.type_slash(s, f"{cmd} {val}")
                save_state()
                thread = await self.get_thread(st.get("thread"))
                if thread:
                    await self.say(thread, f"-# applied the queued `{cmd} {val}`" if ok
                                   else f"-# ⚠️ couldn't apply the queued `{cmd} {val}`")
        if st.get("muted"):
            st["status"] = s["status"]
            return
        # stream new transcript content under the claude's own identity
        if s["transcript"]:
            tsize = os.path.getsize(s["transcript"])
            if tsize < st["size"]:
                st["size"] = 0  # transcript replaced; resync
            if tsize > st["size"]:
                items, consumed = await asyncio.to_thread(
                    checkin.parse_transcript, Path(s["transcript"]),
                    st["size"], checkin.TAIL_BYTES, True)
                items = [it for it in items
                         if not (it["kind"] == "user" and self.is_echo(key, it["text"]))]
                update_card(st, items)
                await self.track_models(s, st, items)
                await self.mirror_images(s, st, items)
                entries = format_new_items(items, s["sid"], include_tools=False)
                if entries:
                    if await self.get_thread(st["thread"]) is None:
                        # Don't drop a live session's entry on ONE failed lookup: a Discord hiccup
                        # once looked like "thread deleted", the entry was popped, and the session
                        # ended up orphaned from the thread the user was typing in. Three misses.
                        st["thread_missing"] = st.get("thread_missing", 0) + 1
                        if st["thread_missing"] >= 3:
                            print(f"thread {st['thread']} for {key} ({st.get('name')}) missing on 3 checks — "
                                  "recreating", flush=True)
                            state.pop(key, None)
                        save_state()
                        return
                    if st.get("thread_missing"):
                        st["thread_missing"] = 0
                    # summoned claudes live in whichever channel summoned them
                    parent = await self.get_thread(st.get("parent", CHANNEL_ID)) or channel
                    extra = len(entries) - MAX_MSGS_PER_TICK
                    for kind, payload in entries[:MAX_MSGS_PER_TICK]:
                        await self.post_entry(parent, s, st, kind, payload)
                        # the summarizer hub's replies also land in #all-claudes
                        if st.get("hub") and kind == "msg" and not payload.startswith("-#") \
                                and self.broadcast_channel:
                            try:
                                await self.post_as(self.broadcast_channel, webhook_name(s), payload,
                                                   None, seed=s["key"])
                            except discord.HTTPException as e:
                                log_error("hub mirror", e)
                    if extra > 0:
                        await self.post_as(
                            parent, webhook_name(s),
                            f"-# …{extra} more — [dashboard]({DASHBOARD}/s/{s['sid']})",
                            st["thread"], seed=s["key"])
                        st["tool_msg"] = None
                st["size"] = consumed
                save_state()
        if st.get("card"):
            parent = await self.get_thread(st.get("parent", CHANNEL_ID)) or channel
            await self.render_card(parent, s, st)
        # status changes: edit the intro in place; ping only on `waiting`
        if s["status"] != st["status"]:
            prev = st["status"]
            st["status"] = s["status"]
            st["name"] = s["name"]
            thread = await self.edit_status(st, s)
            if prev == "waiting" and st.get("prompt_msg"):
                await self.close_prompt(st, thread)
            # the Notification hook usually gets here first; don't post a second prompt
            if s["status"] == "waiting" and thread and not (
                    st.get("prompt_msg") and time.time() - st.get("prompt_at", 0) < 30):
                await self.open_prompt(s, st, thread)
            save_state()

    async def mirror_images(self, s, st, items):
        """Automatic inline plots: when a claude opens an image with the Read tool (CLAUDE.md
        tells it to do that for every plot it makes), post that image into its thread. No
        command for the claude to remember; `hearth-send` remains for non-image files. Each
        (path, mtime) is posted once — re-reading the same unchanged image doesn't repost."""
        paths = []
        for it in items:
            if it.get("kind") == "tool" and it.get("name") == "Read":
                p = (it.get("text") or "").strip().strip("'\"`")
                if p.lower().endswith(IMAGE_EXTS) and os.path.isfile(p) and p not in paths:
                    paths.append(p)
        if not paths:
            return
        thread = await self.get_thread(st.get("thread"))
        if not thread:
            return
        seen = st.setdefault("mirrored", [])
        for p in paths[:4]:
            try:
                key = f"{p}:{int(os.path.getmtime(p))}"
                size = os.path.getsize(p)
            except OSError:
                continue
            if key in seen:
                continue
            if size > FILE_LIMIT_BYTES:
                await self.say(thread, f"-# 🖼️ `{os.path.basename(p)}` is {human_size(size)} — too big to "
                                       f"post inline; it's at `{p}`")
                seen.append(key)
                continue
            try:
                note = ""
                wh = png_size(p)
                if wh and wh[1] and wh[0] / wh[1] >= WIDE_ASPECT:
                    note = (f" · wide figure ({wh[0]}×{wh[1]}): Discord shrinks it to the chat width, "
                            "so tap it and open the original to read it")
                await thread.send(content=f"-# 🖼️ `{os.path.basename(p)}`{note}",
                                  file=await asyncio.to_thread(discord.File, p),
                                  allowed_mentions=NO_PING)
                seen.append(key)
            except discord.HTTPException as e:
                log_error("mirror image", e)
        del seen[:-40]
        save_state()

    async def track_models(self, s, st, items):
        """Surface which model a session is running and when the auto-mode classifier reroutes a
        turn to another model or blocks an action. `items` is the new transcript batch this tick."""
        models = [it.get("model") for it in items if it.get("model")]
        blocks = sum(1 for it in items if it.get("kind") == "result" and it.get("error")
                     and CLASSIFIER_PHRASE in (it.get("text") or ""))
        if not models and not blocks:
            return
        thread = None
        if models:
            base = st.get("model_base") or models[0]
            st["model_base"] = base
            drops_now = sum(1 for m in models if m != base)
            if drops_now:
                st["model_drops"] = st.get("model_drops", 0) + drops_now
            st["model"] = models[-1]
            if st["model"] != base and not st.get("model_drop_alerted"):
                st["model_drop_alerted"] = True
                thread = await self.get_thread(st["thread"])
                if thread:
                    await self.say(thread,
                                   f"⤵️ **the auto-mode classifier rerouted a turn to `{short_model(st['model'])}`** "
                                   f"— this session's baseline model is `{short_model(base)}`. The model in use "
                                   "now shows on the activity card and the status line.")
        if blocks:
            st["classifier_blocks"] = st.get("classifier_blocks", 0) + blocks
            thread = thread or await self.get_thread(st["thread"])
            if thread:
                await self.say(thread, f"🚧 auto-mode classifier blocked {blocks} "
                                       f"action{'s' if blocks != 1 else ''} this turn "
                                       f"({st['classifier_blocks']} total in this session).")
        save_state()

    async def render_card(self, parent, s, st):
        """Post or edit the session's activity card; collapse it once the turn is over."""
        card = st.get("card")
        if not card:
            return
        now = time.time()
        final = s["status"] != "busy"
        if not final:
            since = now - card.get("last_edit", 0)
            if card.get("dirty"):
                if since < CARD_MIN_GAP:
                    return                          # coalesce a burst; next tick renders it
            elif since < CARD_HEARTBEAT:
                return
        text = card_text(s, card, final, model=st.get("model"), base=st.get("model_base"))
        if text == card.get("body") and not final:
            return
        try:
            if card.get("msg"):
                try:
                    await self.edit_as(parent, card["msg"], text, st["thread"])
                except discord.HTTPException:
                    card["msg"] = None                # message gone; post a fresh one
            if not card.get("msg"):
                m = await self.post_as(parent, webhook_name(s), text, st["thread"], seed=s["key"])
                card["msg"] = m.id if m else None
        except discord.HTTPException as e:
            log_error("card", e)
        card["last_edit"], card["body"], card["dirty"] = now, text, False
        if final:
            st["card"] = None
            st["tool_msg"] = None
        save_state()

    async def open_prompt(self, s, st, thread, note=""):
        """A claude is blocked on you: ping, show its screen, and turn the menu it is
        showing into buttons so one tap from a phone answers it."""
        st["prompt_at"] = time.time()
        if not s.get("pane"):
            await self.say(thread, f"📡 **signal — needs your input** {NO_PANE_HELP}", ping_owner=True)
            return
        text = await asyncio.to_thread(screen_text, s["pane"])
        opts, _ = parse_options(text)
        hint = "tap an option" if opts else "reply here to type, or use the keys"
        ping = f"<@{self.owner}> " if self.owner else ""
        head = f"{ping}📡 **signal — needs your input** · {hint}\n"
        if note:
            head += f"-# {note[:300]}\n"
        try:
            m = await thread.send(
                prompt_body(text, head),
                view=prompt_view(s["key"], opts),
                allowed_mentions=discord.AllowedMentions(users=True))
            st["prompt_msg"] = m.id
        except discord.HTTPException as e:
            log_error("open_prompt", e)
            await self.say(thread, "📡 **signal — needs your input** — `!screen` to see it",
                           ping_owner=True)

    async def close_prompt(self, st, thread):
        mid = st.pop("prompt_msg", None)
        if not mid or thread is None:
            return
        try:
            m = await thread.fetch_message(mid)
            await m.edit(content="-# ✅ answered", view=None)
        except discord.HTTPException:
            pass

    async def on_interaction(self, interaction):
        """Button presses. custom_id: `o|<key>|<n>` (menu option) or `k|<key>|<tmuxkey>`."""
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        parts = cid.split("|")
        if len(parts) == 3 and parts[0] == "rp":              # !resume list: paging
            q, page = parts[1], int(parts[2] or 0)
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            rows = await asyncio.to_thread(search_sessions, q)
            try:
                await interaction.message.edit(content=resume_text(q, page, rows),
                                               view=resume_view(q, page, rows))
            except discord.HTTPException as e:
                log_error("resume page", e)
            return
        if parts and parts[0] == "rs":                        # !resume list: a pick
            sid = ((interaction.data or {}).get("values") or [None])[0]
            try:
                await interaction.response.defer(thinking=True)
            except discord.HTTPException:
                pass

            async def respond(text):
                try:
                    await interaction.followup.send(text, allowed_mentions=NO_PING,
                                                    suppress_embeds=True)
                except discord.HTTPException as e:
                    log_error("resume followup", e)
            if sid:
                await self.bring_up(sid, interaction.user, respond)
            return
        if len(parts) != 3 or parts[0] not in ("o", "k"):
            return
        kind, key, arg = parts
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        s = await asyncio.to_thread(find_live_by_key, key)
        if not s or not s.get("pane"):
            try:
                await interaction.followup.send("👻 that claude is gone (or has no pane)",
                                                ephemeral=True)
            except discord.HTTPException:
                pass
            return
        who = interaction.user.display_name if interaction.user else "someone"
        before = await asyncio.to_thread(screen_text, s["pane"])
        if kind == "k" and arg == "__refresh":
            pressed = None
        elif kind == "k":
            if arg not in checkin.ALLOWED_KEYS:
                return
            await asyncio.to_thread(checkin.send_to_session, s, None, arg)
            pressed = arg
        else:
            await asyncio.to_thread(checkin.send_to_session, s, None, arg)
            pressed = arg
            # number keys select-and-confirm in permission prompts, but only select in
            # some menus: if the very same menu is still up after a beat, confirm it
            await asyncio.sleep(1.2)
            after = await asyncio.to_thread(screen_text, s["pane"])
            if parse_options(after)[0] and parse_options(after)[0] == parse_options(before)[0]:
                await asyncio.to_thread(checkin.send_to_session, s, None, "Enter")
                pressed += " ⏎"
        await asyncio.sleep(1.0)
        text = await asyncio.to_thread(screen_text, s["pane"])
        live = await asyncio.to_thread(find_live_by_key, key)
        still = bool(live and live["status"] == "waiting")
        opts = parse_options(text)[0] if still else []
        note = (f"-# {who} pressed `{pressed}`\n" if pressed else "") + \
               ("📡 still waiting · " if still else "✅ ") + \
               ("tap an option\n" if opts else ("\n" if still else "handled\n"))
        try:
            await interaction.message.edit(content=prompt_body(text, note),
                                           view=prompt_view(key, opts) if still else None)
        except discord.HTTPException as e:
            log_error("prompt edit", e)
        st = state.get(key)
        if st is not None and not still:
            st.pop("prompt_msg", None)
            save_state()

    async def session_gone(self, key, st, phantoms):
        """A tracked session is no longer live. Clean exits archive the thread; a crash
        (registry file left behind = the process never cleaned up) keeps the thread open
        with a revive hint; a reboot revives it outright (REVIVE_ON_BOOT)."""
        st["ended"] = True
        st["status"] = "ended"
        st["ended_at"] = time.time()
        reason = "reboot" if self._rebooted else ("crash" if key in phantoms else "exit")
        st["ended_reason"] = reason
        save_state()
        thread = await self.get_thread(st.get("thread"))
        if not thread:
            return
        if st.get("prompt_msg"):
            await self.close_prompt(st, thread)
        self.log_event(f"{'💥' if reason == 'crash' else '🌌'} {st.get('name', '?')} "
                       f"{'died' if reason == 'crash' else 'ended'}")
        if reason == "exit":
            hint = END_REASON_WORDS.get(st.pop("end_reason_hint", "") or "", "session exited")
            await self.say(thread, f"-# 🌌 loop ended — {hint}")
            try:
                await thread.edit(name=ended_title(st.get("name", "")), archived=True)
            except discord.HTTPException:
                pass
            if ANNOUNCE_NEW:
                await self.say(self.main_channel,
                               f"-# 🌌 **{st.get('name', '?')}** ended · <#{thread.id}>")
        elif reason == "crash":
            await self.say(thread, "💥 **supernova** — this session vanished without a clean exit "
                                   "(crash, OOM, or a hard kill). `!revive` brings it back with its "
                                   "history intact.", ping_owner=True)
            await self.say(self.main_channel,
                           f"💥 **{st.get('name', '?')}** died without a clean exit · "
                           f"`!revive` in <#{thread.id}>")
            if REVIVE_ON_CRASH and st.get("sid"):
                await self.revive(key, st, thread, reporter=lambda t: self.say(thread, t))
        # reboot: after_reboot() handles the whole batch

    async def after_reboot(self):
        dead = [(k, v) for k, v in sessions_state().items()
                if v.get("ended_reason") == "reboot" and v.get("sid")]
        if not dead:
            return
        names = ", ".join(f"**{v.get('name', '?')}**" for _, v in dead[:15])
        if REVIVE_ON_BOOT:
            await self.say(self.main_channel,
                           f"🌅 **the loop reset** — the box rebooted. {len(dead)} travelers were live "
                           f"({names}). Reviving each into tmux, history intact…")
            ok = 0
            for k, v in dead:
                thread = await self.get_thread(v.get("thread"))
                res = await self.revive(k, v, thread,
                                        reporter=lambda t, th=thread: self.say(th, t))
                ok += bool(res)
                await asyncio.sleep(3)   # let each claude finish loading before the next
            await self.say(self.main_channel, f"🌅 revived **{ok}/{len(dead)}** — the rest have "
                                              "details in their threads (`!revive` to retry).")
        else:
            await self.say(self.main_channel,
                           f"🌅 **the loop reset** — the box rebooted. {len(dead)} travelers were live "
                           f"({names}). `!revive all` here brings them all back, or `!revive` in a thread.",
                           ping_owner=True)

    async def reopen_thread(self, key, s, st):
        """A tracked session recorded as ended is live again under the same key: it dropped out
        of one registry read, or its entry was ended by mistake. Un-end it and keep its thread
        (unarchive, retitle on the next tick). Returns False if the thread is gone."""
        thread = await self.get_thread(st.get("thread"))
        if not thread:
            return False
        st.update({"ended": False, "ended_at": None, "ended_reason": None, "status": s["status"],
                   "name": s["name"], "sid": s["sid"], "cwd": s["cwd"] or st.get("cwd"),
                   "thread_name": None, "card": None, "prompt_msg": None,
                   "tool_msg": None, "tool_run": None, "thread_missing": 0})
        twin = st.pop("retire_thread", None)
        save_state()
        try:
            if getattr(thread, "archived", False):
                await thread.edit(archived=False)
            await thread.send("-# ♻️ reconnected — this session is live, continuing here",
                              allowed_mentions=NO_PING)
        except discord.HTTPException as e:
            log_error("reopen_thread", e)
        if twin and twin != thread.id:
            tw = await self.get_thread(twin)
            if tw:
                try:
                    await tw.send(f"-# this was a duplicate thread — the session continues in <#{thread.id}>",
                                  allowed_mentions=NO_PING)
                    await tw.edit(name=ended_title(tw.name), archived=True)
                except discord.HTTPException as e:
                    log_error("retire twin", e)
        print(f"reopen: {s['name']} ({key}) back in thread {thread.id}"
              + (f", retired twin {twin}" if twin else ""), flush=True)
        return True

    async def open_thread(self, channel, s, key):
        name = s["name"]
        cur = state.get(key)
        if cur and cur.get("thread") and not cur.get("ended"):
            return                        # something else already gave this session a thread
        collides = any(v.get("name") == name and not v.get("ended")
                       for v in sessions_state().values())
        title = thread_title(name, s["sid"], collides)
        thread = await channel.create_thread(
            name=title, type=discord.ChannelType.public_thread,
            auto_archive_duration=10080)
        cur = state.get(key)
        if cur and cur.get("thread") and cur["thread"] != thread.id and not cur.get("ended"):
            # a restart/revive claimed this session while the thread was being created: keep
            # its (original) thread and drop the duplicate we just made
            try:
                await thread.delete()
            except discord.HTTPException:
                pass
            return
        size = os.path.getsize(s["transcript"]) if s["transcript"] else 0
        # record the thread FIRST: if the intro fails, the next tick must not open a twin
        state[key] = {"thread": thread.id, "parent": channel.id, "status_msg": None,
                      "size": size, "status": s["status"], "name": s["name"],
                      "sid": s["sid"], "cwd": s["cwd"], "thread_name": title,
                      "ended": False, "muted": False}
        save_state()
        intro = await thread.send(status_line(s), suppress_embeds=True, allowed_mentions=NO_PING)
        state[key]["status_msg"] = intro.id
        snippet = ""
        if s["transcript"]:
            snippet = await asyncio.to_thread(
                checkin.last_assistant_snippet, Path(s["transcript"]))
        if snippet:
            await self.post_as(channel, webhook_name(s), f"-# last said: {snippet[:300]}",
                               thread.id, seed=s["key"])
        save_state()
        self.log_event(f"🚀 {name} arrived")
        if ANNOUNCE_NEW and self.main_channel:
            how = ("background agent · 📨 inbox socket" if reach(s) == "sock"
                   else f"tmux pane `{s['pane']}`" if s.get("pane") else "no tmux pane · 👁️ read-only")
            await self.say(self.main_channel,
                           f"🚀 **new traveler:** **{name}** · `{s['project']}` · {how} · "
                           f"{STATUS_EMOJI.get(s['status'], '⚪')} {STATUS_WORD.get(s['status'], s['status'])} "
                           f"· <#{thread.id}>")

    # ---------- system prompt, editable from Discord ----------

    async def prompt_tick(self):
        """Apply the newest CLAUDE.md attachment from the prompt channel to disk.

        Upload a file named exactly CLAUDE.md to that channel and it becomes the
        system prompt; the channel's history is the version history. The previous
        file is always backed up first, so a bad upload is recoverable.
        """
        if not PROMPT_CHANNEL_ID:
            return
        ch = self.get_channel(PROMPT_CHANNEL_ID)
        if ch is None:
            try:
                ch = await self.fetch_channel(PROMPT_CHANNEL_ID)
            except discord.HTTPException:
                return
        meta = state["_meta"]
        async for msg in ch.history(limit=20):          # newest first
            att = next((a for a in msg.attachments if a.filename == "CLAUDE.md"), None)
            if not att:
                continue
            if meta.get("prompt_msg") == msg.id:
                return                                   # newest is already applied
            try:
                text = (await att.read()).decode("utf-8", "replace")
            except discord.HTTPException as e:
                print(f"prompt fetch failed: {e}")
                return
            if not text.strip():
                await ch.send("-# ⚠️ that CLAUDE.md is empty — ignoring", allowed_mentions=NO_PING)
                meta["prompt_msg"] = msg.id
                save_state()
                return

            def write():
                if PROMPT_TARGET.exists():
                    bak = PROMPT_TARGET.with_name(f"{PROMPT_TARGET.name}.bak-{msg.id}")
                    bak.write_text(PROMPT_TARGET.read_text())
                PROMPT_TARGET.parent.mkdir(parents=True, exist_ok=True)
                tmp = PROMPT_TARGET.with_name(PROMPT_TARGET.name + ".tmp")
                tmp.write_text(text)
                tmp.replace(PROMPT_TARGET)

            await asyncio.to_thread(write)
            meta["prompt_msg"] = msg.id
            save_state()
            print(f"prompt synced from message {msg.id}: {len(text)} bytes")
            await ch.send(
                f"-# ✅ applied to `{PROMPT_TARGET}` — {len(text)} bytes, "
                f"{len(text.splitlines())} lines. Previous version saved as "
                f"`{PROMPT_TARGET.name}.bak-{msg.id}`. New sessions pick it up; "
                f"running ones keep the prompt they started with.",
                allowed_mentions=NO_PING)
            return

    # ---------- claude<->claude bus bridge ----------

    async def chat_tick(self):
        if not self.chat_channel or not CHAT_LOG.exists():
            return
        meta = state["_meta"]
        size = CHAT_LOG.stat().st_size
        if size < meta["chat_offset"]:
            meta["chat_offset"] = 0  # log truncated/rotated
        if size == meta["chat_offset"]:
            return
        with open(CHAT_LOG, "rb") as f:
            f.seek(meta["chat_offset"])
            data = f.read(size - meta["chat_offset"])
        consumed = meta["chat_offset"] + len(data)
        if data and not data.endswith(b"\n"):
            cut = data.rfind(b"\n")
            if cut == -1:
                return
            consumed = meta["chat_offset"] + cut + 1
            data = data[:cut + 1]
        for line in data.split(b"\n"):
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("via") == "discord":
                continue  # our own relays — don't echo back
            frm, text = str(m.get("from", "?")), str(m.get("text", ""))
            for chunk in split_chunks(text)[:3]:
                await self.post_as(self.chat_channel, frm, chunk)
        meta["chat_offset"] = consumed
        save_state()

    # ---------- supernova: a countdown for a session ----------

    async def handle_supernova(self, msg, key, st, arg):
        low = arg.lower()
        if low in ("off", "stop", "cancel") and st.get("supernova"):
            sn = st.pop("supernova")
            save_state()
            await self.edit_countdown(msg.channel, sn, "☀️ supernova cancelled — the sun holds")
            return await msg.add_reaction("🌤️")
        words = low.split()
        action = next((w for w in words if w in ("warn", "stop", "kill")), "warn")
        dur = next((parse_duration(w) for w in words if parse_duration(w)), None) or SUPERNOVA_DEFAULT
        if dur > 12 * 3600:
            return await self.say(msg.channel, "max 12h — `!supernova 22m`, `!supernova 2h stop`, `!supernova off`")
        s = await asyncio.to_thread(find_live_by_key, key)
        if not s:
            return await msg.add_reaction("👻")
        until = int(time.time() + dur)
        m = await self.say(msg.channel, self.countdown_text(until, action))
        st["supernova"] = {"until": until, "action": action, "msg": m.id if m else None,
                           "warned": [], "set_by": msg.author.display_name}
        save_state()
        await msg.add_reaction("☀️")

    def countdown_text(self, until, action, final=None):
        if final:
            return final
        left = max(0, until - int(time.time()))
        what = {"warn": "the claude is told to wrap up and report",
                "stop": "its turn is interrupted, then it is told to wrap up",
                "kill": "the session is ended"}[action]
        sun = "☀️" if left > 600 else "🔴" if left > 120 else "💥"
        return (f"{sun} **supernova in {left // 60}m {left % 60:02d}s** · <t:{until}:t> (<t:{until}:R>) · "
                f"at zero {what}")

    async def edit_countdown(self, thread, sn, text):
        if not sn.get("msg") or thread is None:
            return
        try:
            m = await thread.fetch_message(sn["msg"])
            await m.edit(content=text, suppress=True, allowed_mentions=NO_PING)
        except discord.HTTPException:
            pass

    async def supernova_tick(self):
        now = int(time.time())
        for key, st in list(sessions_state().items()):
            sn = st.get("supernova")
            if not sn:
                continue
            thread = await self.get_thread(st.get("thread"))
            left = sn["until"] - now
            if left > 0:
                if now - sn.get("last_edit", 0) >= 55:
                    sn["last_edit"] = now
                    await self.edit_countdown(thread, sn, self.countdown_text(sn["until"], sn["action"]))
                for ms in SUPERNOVA_MILESTONES:
                    if left <= ms and ms not in sn["warned"]:
                        sn["warned"].append(ms)
                        if ms == 120 and thread:
                            await self.say(thread, f"🔴 **two minutes to the supernova** — <t:{sn['until']}:R>",
                                           ping_owner=True)
                        break
                save_state()
                continue
            # zero
            st.pop("supernova", None)
            save_state()
            s = await asyncio.to_thread(find_live_by_key, key)
            await self.edit_countdown(thread, sn, "💥 **SUPERNOVA** — the loop ended at <t:%d:t>" % sn["until"])
            if not s or not thread:
                continue
            note = ("Supernova: the time budget for this task is up. Stop what you are doing, write down "
                    "exactly where you are and what is left, and report it here.")
            if sn["action"] == "kill":
                try:
                    await asyncio.to_thread(os.kill, s["pid"], signal.SIGTERM)
                    await self.say(thread, "💥 supernova — session ended (SIGTERM)", ping_owner=True)
                except OSError as e:
                    await self.say(thread, f"💥 supernova — couldn't end the session: {e}")
                continue
            if sn["action"] == "stop" and s.get("pane"):
                await asyncio.to_thread(checkin.send_to_session, s, None, "Escape")
                await asyncio.sleep(1.0)
            ok, err = await asyncio.to_thread(checkin.send_to_session, s, note)
            await self.say(thread, "💥 **supernova** — told the claude to wrap up and report"
                           + ("" if ok else f" (⚠️ not delivered: {err})"), ping_owner=True)

    # ---------- the board: one pinned message, edited in place ----------

    def log_event(self, text):
        ev = state["_meta"].setdefault("events", [])
        ev.append({"ts": int(time.time()), "text": text[:120]})
        del ev[:-8]

    async def board_text(self):
        sessions = await asyncio.to_thread(live_sessions)
        free = f" · 💾 {ash_twin.disk_free()[0]:.1f} GB free" if ash_twin else ""
        lines = [f"🔭 **signalscope board** · {len(sessions)} traveler{'s' if len(sessions) != 1 else ''}"
                 f"{free} · [dashboard]({DASHBOARD})"]
        order = {"waiting": 0, "busy": 1, "shell": 2, "idle": 3}
        for s in sorted(sessions, key=lambda x: (order.get(x["status"], 9), -x["updatedAt"])):
            st = state.get(s["key"], {})
            where = f"<#{st['thread']}>" if st.get("thread") else "*(thread opening)*"
            tag = {"pane": "", "sock": " 📨", None: " 👁️"}[reach(s)]
            lines.append(f"{STATUS_EMOJI.get(s['status'], '⚪')} **{s['name'][:40]}** · `{s['project']}` · "
                         f"{STATUS_WORD.get(s['status'], s['status']).split(' —')[0]}{tag} · {where}")
        events = state["_meta"].get("events", [])
        if events:
            lines.append("-# recent: " + " · ".join(f"{e['text']} <t:{e['ts']}:R>" for e in events[-5:]))
        body = "\n".join(lines)[:1900]
        return body, body + f"\n-# updated <t:{int(time.time())}:R> · edits, never pings"

    async def board_tick(self):
        if not BOARD or self.main_channel is None:
            return
        meta = state["_meta"]
        now = time.time()
        if now - meta.get("board_at", 0) < BOARD_MIN_SECS:
            return
        body, full = await self.board_text()
        if body == meta.get("board_body") and now - meta.get("board_at", 0) < 600:
            return
        meta["board_at"] = now
        msg = None
        if meta.get("board_msg"):
            try:
                msg = await self.main_channel.fetch_message(meta["board_msg"])
                await msg.edit(content=full, suppress=True, allowed_mentions=NO_PING)
            except discord.HTTPException:
                msg = None
        if msg is None:
            try:
                msg = await self.main_channel.send(full, suppress_embeds=True, allowed_mentions=NO_PING)
                meta["board_msg"] = msg.id
                try:
                    await msg.pin(reason="signalscope board")
                except discord.HTTPException:
                    pass
            except discord.HTTPException as e:
                log_error("board", e)
                return
        meta["board_body"] = body
        save_state()

    # ---------- disk watchdog (Dark Bramble is hungry) ----------

    async def disk_tick(self):
        if ash_twin is None or self.main_channel is None:
            return
        now = time.time()
        if now - self._last_disk < DISK_CHECK_SECS:
            return
        self._last_disk = now
        free_gb, pct = ash_twin.disk_free()
        level = "crit" if free_gb < DISK_CRIT_GB else "low" if free_gb < DISK_WARN_GB else "ok"
        meta = state["_meta"]
        prev = meta.get("disk_level", "ok")
        if level == prev:
            return
        meta["disk_level"] = level
        save_state()
        if level == "ok":
            await self.say(self.main_channel,
                           f"🌤️ disk recovered — **{free_gb:.1f} GB free** ({pct:.0f}% used)")
            return
        rep = await asyncio.to_thread(ash_twin.disk_report)
        cands = ash_twin.candidates(rep=rep)
        head = ("🕳️ **Dark Bramble is eating the disk** — " if level == "crit"
                else "🌋 **disk running low** — ")
        head += (f"**{free_gb:.1f} GB free**. When it hits 0, transcripts stop being written "
                 "and the bridge can't save state.\n")
        await self.say(self.main_channel, head + ash_twin.format_report(rep, cands),
                       ping_owner=(level == "crit"))
        if level == "crit" and AUTO_OFFLOAD and cands:
            target = cands[0]["path"]
            await self.say(self.main_channel, f"🪐 AUTO_OFFLOAD=1 → offloading the biggest cold "
                                              f"directory: `{target}`")
            await self.run_offload(self.main_channel, target)

    async def run_offload(self, channel, path):
        if ash_twin is None:
            return await self.say(channel, "❌ S3 tooling unavailable (ash_twin import failed)")
        if self._offload_lock.locked():
            return await self.say(channel, "⏳ an offload/restore is already running — one at a time")
        async with self._offload_lock:
            loop = asyncio.get_running_loop()
            last = [0.0]

            def progress(text):
                if (time.time() - last[0] > 20 or "done" in text or "FAILED" in text
                        or text.startswith("offload:")):
                    last[0] = time.time()
                    asyncio.run_coroutine_threadsafe(self.say(channel, f"-# 🪐 {text}"), loop)

            try:
                res = await asyncio.to_thread(ash_twin.offload, path, progress)
                free_gb, _ = ash_twin.disk_free()
                await self.say(channel, f"✅ **offloaded** `{res['path']}` — "
                                        f"{ash_twin.human(res['bytes'])} in {res['uploaded']} files now "
                                        f"at `s3://{ash_twin.BUCKET}/{res['prefix']}` · marker "
                                        f"`{res['marker']}` · **{free_gb:.1f} GB free** now")
            except Exception as e:  # noqa: BLE001
                await self.say(channel, f"❌ offload failed, nothing deleted: `{e}`")

    async def run_restore(self, channel, path):
        if ash_twin is None:
            return await self.say(channel, "❌ S3 tooling unavailable")
        if self._offload_lock.locked():
            return await self.say(channel, "⏳ an offload/restore is already running — one at a time")
        async with self._offload_lock:
            try:
                res = await asyncio.to_thread(ash_twin.restore, path, lambda t: None)
                await self.say(channel, f"✅ **restored** `{res['path']}` — "
                                        f"{ash_twin.human(res['bytes'])} in {res['files']} files")
            except Exception as e:  # noqa: BLE001
                await self.say(channel, f"❌ restore failed: `{e}`")

    # ---------- summon a new claude ----------

    async def await_registration(self, pane):
        """Wait for a freshly spawned pane to register a session, answering first-run
        gates (trust dialog, bypass acceptance, API-key confirmation, theme picker) as
        they appear. Returns (session|None, notes)."""
        notes, answered = [], Counter()
        deadline = time.time() + SPAWN_TIMEOUT
        n = 0
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            n += 1
            for s in await asyncio.to_thread(live_sessions):
                if s["pane"] == pane:
                    return s, notes
            if n % 2:
                continue
            text = await asyncio.to_thread(screen_text, pane, 1200)
            gate, action = detect_gate(text)
            if gate and answered[gate] < 3:
                answered[gate] += 1
                pressed = await asyncio.to_thread(answer_gate, pane, text, action)
                notes.append(f"answered the **{gate}** gate ({pressed})")
                await asyncio.sleep(1.0)
            elif LOGIN_RE.search(text):
                notes.append("a **login** screen is up — that needs a human (`tmux select-window`)")
                break
        return None, notes

    async def spawn_failure_text(self, pane, cwd, notes):
        alive = await asyncio.to_thread(pane_claude_alive, pane)
        tail = await asyncio.to_thread(screen_text, pane, 800)
        state_line = ("🟡 **claude is still alive in that pane, waiting on something** — "
                      "answer it and the session completes" if alive else
                      "🔴 **no claude process on that pane** — it exited or never started")
        extra = ("\n".join(f"-# {n}" for n in notes) + "\n") if notes else ""
        return (f"⚠️ launched in `{cwd}` (pane `{pane}`) but it never registered a session.\n"
                f"{state_line}\n{extra}```\n{tail or '(blank screen)'}\n```\n"
                f"-# usual causes: a login screen, a missing/invalid API key, or `claude` not on "
                f"PATH. Attach with `tmux select-window -t {pane}`")

    async def summon(self, msg, prompt_text):
        """@mention -> spawn a fresh claude and open its thread on the summoning message."""
        if SPAWN_ALLOW_USERS and msg.author.id not in SPAWN_ALLOW_USERS:
            await msg.reply("⛔ you're not on the spawn allowlist (SPAWN_ALLOW_USERS)",
                            allowed_mentions=NO_PING)
            return
        cwd, words = resolve_project(prompt_text.split())
        prompt = " ".join(words).strip()
        attached = await self.save_attachments(msg)
        if not prompt and not attached:
            await msg.reply(
                f"tag me with what you want done, e.g.\n"
                f"> @{self.user.display_name} oracle-lens why is the step-20 eval slow?\n"
                f"-# first word may name a project dir under `{PROJECT_ROOT}` "
                f"(otherwise I start in `{PROJECT_ROOT}`)", allowed_mentions=NO_PING)
            return
        await msg.add_reaction("🚀")
        pane, err = await asyncio.to_thread(spawn_claude, slug(prompt or "upload", 32), cwd)
        if not pane:
            await msg.reply(f"❌ couldn't launch: `{err}`", allowed_mentions=NO_PING)
            return
        session, notes = await self.await_registration(pane)
        if not session:
            await msg.reply(await self.spawn_failure_text(pane, cwd, notes),
                            allowed_mentions=NO_PING)
            return
        key = session["key"]
        state[key] = {"pending": True}  # claim it before the poller opens a duplicate
        save_state()
        try:
            title = thread_title(session["name"], session["sid"], False)
            thread = await msg.create_thread(name=title, auto_archive_duration=10080)
            intro = await thread.send(status_line(session), suppress_embeds=True,
                                      allowed_mentions=NO_PING)
        except discord.HTTPException as e:
            state.pop(key, None)
            save_state()
            await msg.reply(f"⚠️ launched but couldn't open a thread: `{e}`",
                            allowed_mentions=NO_PING)
            return
        size = os.path.getsize(session["transcript"]) if session["transcript"] else 0
        state[key] = {"thread": thread.id, "parent": msg.channel.id,
                      "status_msg": intro.id, "size": size, "status": session["status"],
                      "name": session["name"], "sid": session["sid"],
                      "cwd": session["cwd"], "thread_name": title,
                      "ended": False, "muted": False, "spawned": True}
        save_state()
        if notes:
            await self.say(thread, "\n".join(f"-# 🛠 {n}" for n in notes))
        payload = f"{prompt}\n{attached}".strip() if attached else prompt
        ok, serr = await asyncio.to_thread(self.deliver, session, msg.author, payload)
        print(f"summon by {msg.author} ({msg.author.id}) in #{msg.channel} -> "
              f"{cwd} pane {pane} key {key} ok={ok} gates={notes}")
        if ok:
            await msg.add_reaction("📡")
        else:
            await thread.send(f"⚠️ prompt not delivered: {serr}", allowed_mentions=NO_PING)

    async def save_attachments(self, msg):
        """Save a message's attachments to disk and describe them for the claude.

        Saved rather than inlined: input reaches the session through a tmux paste
        buffer, so pasting a multi-megabyte file would be slow at best and would corrupt
        the prompt at worst. A path costs one Read and works for images and binaries too.
        """
        if not msg.attachments:
            return ""
        lines = []
        for a in msg.attachments:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", a.filename)[:80] or "upload"
            dest = ATTACH_DIR / f"{msg.id}-{safe}"
            try:
                ATTACH_DIR.mkdir(parents=True, exist_ok=True)
                await a.save(dest)
            except (discord.HTTPException, OSError) as e:
                lines.append(f"[attachment {a.filename!r} FAILED to save: {e}]")
                continue
            lines.append(f"[attachment saved to {dest} · {a.size} bytes]")
        return "\n".join(lines)

    # ---------- retiring threads / ending / reviving sessions ----------

    async def retire_thread(self, thread, note=None, delete=False):
        """Archive (reversible, keeps history) or delete (irreversible) a thread."""
        try:
            if delete:
                await thread.delete()
                return True
            if note:
                await thread.send(note, allowed_mentions=NO_PING)
            await thread.edit(name=ended_title(thread.name), archived=True)
            return True
        except discord.HTTPException as e:
            print(f"retire_thread {thread.id} failed: {e}")
            return False

    async def revive(self, key, st, thread, force=False, fork=False, reporter=None):
        """Resume a pane-less (background), ended, or crashed session as a steerable
        tmux claude. The thread is moved onto the revived process, so the conversation
        continues in the same place. Returns the new session dict, or None."""
        async def report(text):
            if reporter:
                await reporter(text)
        sid = st.get("sid")
        if not sid:
            await report("-# can't revive — no session id recorded for this thread (it predates "
                         "the identity fix). @mention me to start a fresh claude instead.")
            return None
        live = await asyncio.to_thread(find_live_by_key, key)
        if live and live["pane"]:
            await report(f"-# already live and steerable (pane `{live['pane']}`) — just type here")
            return live
        cwd = ((live or {}).get("cwd") or st.get("cwd")
               or await asyncio.to_thread(transcript_cwd, sid))
        if not cwd or not Path(cwd).is_dir():
            await report(f"-# can't revive — its working directory is unknown or gone (`{cwd}`)")
            return None
        name = (live or {}).get("name") or st.get("name") or "revived"
        if live and not fork and not force and live["status"] in ("busy", "shell"):
            await report(f"-# ⚠️ that background agent is **{live['status']}** right now.\n"
                         f"-# `!revive force` — stop it, resume the same session in tmux\n"
                         f"-# `!revive fork` — leave it running, open a steerable copy")
            return None
        if live and not fork:
            try:                                  # clean exit, flushes its transcript
                await asyncio.to_thread(os.kill, live["pid"], signal.SIGTERM)
            except OSError:
                pass
            for _ in range(14):   # never let two processes share one session id
                await asyncio.sleep(1)
                if not await asyncio.to_thread(find_live_by_key, key):
                    break
        pane, err = await asyncio.to_thread(spawn_claude, name, cwd, sid, fork)
        if not pane:
            await report(f"-# ❌ couldn't open a tmux window: `{err}` — is the tmux server "
                         f"(session `{TMUX_SESSION}`) running?")
            return None
        session, notes = await self.await_registration(pane)
        if not session:
            await report(await self.spawn_failure_text(pane, cwd, notes))
            return None
        old = state.pop(key, None) or st              # move the thread onto the new process
        title = thread_title(session["name"], session["sid"], False)
        state[session["key"]] = {
            **old, "sid": session["sid"], "name": session["name"], "cwd": session["cwd"],
            "status": session["status"], "thread_name": title, "ended": False,
            "ended_at": None, "ended_reason": None,
            "size": os.path.getsize(session["transcript"]) if session["transcript"] else 0,
            "tool_msg": None, "tool_run": None, "prompt_msg": None}
        save_state()
        await self.edit_status(state[session["key"]], session)
        if thread:
            try:
                await thread.edit(name=title, archived=False)
            except discord.HTTPException:
                pass
        await report(f"-# ♻️ revived in tmux pane `{pane}`"
                     + (" as a fork (new session id, original left running)" if fork else "")
                     + " — history intact, talk to it here"
                     + ("".join(f"\n-# 🛠 {n}" for n in notes) if notes else ""))
        return session

    async def fork_session(self, channel, user, key, st, first_prompt, respond):
        """Clone this session into a NEW claude — own tmux window, own thread, history
        shared up to now — and leave the original running.

        Claude Code's own `/fork` spawns a *background* agent: no tty, so no pane, so
        read-only in Discord until someone runs `!revive` in its thread. This does the
        steerable version directly: `claude -r <sid> --fork-session` in tmux.
        `respond` posts progress where the request came from (thread or slash reply).
        """
        live = await asyncio.to_thread(find_live_by_key, key)
        sid = (live or {}).get("sid") or st.get("sid")
        if not sid:
            return await respond("-# can't fork — no session id recorded for this thread")
        cwd = ((live or {}).get("cwd") or st.get("cwd")
               or await asyncio.to_thread(transcript_cwd, sid))
        if not cwd or not Path(cwd).is_dir():
            return await respond(f"-# can't fork — working directory unknown or gone (`{cwd}`)")
        base = (live or {}).get("name") or st.get("name") or "claude"
        pane, err = await asyncio.to_thread(spawn_claude, fork_name(base), cwd, sid, True)
        if not pane:
            return await respond(f"-# ❌ couldn't open a tmux window: `{err}`")
        session, notes = await self.await_registration(pane)
        if not session:
            return await respond(await self.spawn_failure_text(pane, cwd, notes))
        parent = await self.get_thread(st.get("parent", CHANNEL_ID)) or self.main_channel
        thread = await self.adopt_session(session, parent, {"spawned": True, "forked_from": key})
        if thread is None:
            return await respond(f"-# ⚠️ forked into pane `{pane}` but couldn't open its thread — "
                                 "the poller will pick it up")
        await self.say(thread, f"🌱 **forked from** <#{channel.id}> — same history up to now, "
                               f"new session id `{session['sid'][:8]}`, tmux pane `{pane}`. Talk to it here."
                               + ("".join(f"\n-# 🛠 {n}" for n in notes) if notes else ""))
        await respond(f"🌱 forked → <#{thread.id}> · the original keeps running here")
        if first_prompt:
            ok, serr = await asyncio.to_thread(self.deliver, session, user, first_prompt)
            if not ok:
                await self.say(thread, f"-# ⚠️ first message not delivered: {serr}")
        print(f"fork by {user} ({getattr(user, 'id', '?')}): {key} -> {session['key']} pane {pane}")

    async def broadcast(self, msg):
        """#all-claudes. `!all <msg>` → plain broadcast to every live claude (receipt line, 📣).
        Anything else → an ask-round: fan the question out, collect every claude's reply, hand
        the bundle to the summarizer hub, whose synthesis is mirrored back here."""
        content = (msg.content or "").strip()
        if content.lower() == "!help":
            return await self.send_help(msg.channel)
        attached = await self.save_attachments(msg)
        low = content.lower()
        plain = low.startswith("!all ")
        to_hub = low.startswith("!hub ") or msg.reference is not None   # reply-to = talk to the hub
        body = content[5:].strip() if (plain or low.startswith("!hub ")) else content
        text = f"{body}\n{attached}".strip() if attached else body
        if not text:
            return
        if plain:
            return await self.broadcast_plain(msg, text)
        if to_hub:
            hub = await self.ensure_hub(msg.channel)
            if not hub:
                return
            ok, err = await asyncio.to_thread(self.deliver, hub, msg.author,
                                              f"[all-claudes · follow-up from {getattr(msg.author, 'display_name', msg.author)}] {text}")
            try:
                await msg.add_reaction("🧠" if ok else "❌")
            except discord.HTTPException:
                pass
            if not ok:
                await msg.channel.send(f"-# ⚠️ not delivered to the summarizer: {err}", allowed_mentions=NO_PING)
            return
        await self.ask_round(msg, text)

    async def broadcast_plain(self, msg, text):
        sessions = await asyncio.to_thread(live_sessions)
        got, lines = 0, []
        for s in sorted(sessions, key=lambda x: x["name"]):
            if not reach(s):
                lines.append(f"⏭️ {s['name']}")
                continue
            ok, _ = await asyncio.to_thread(self.deliver, s, msg.author, text)
            got += bool(ok)
            lines.append(f"{'✅' if ok else '❌'} {s['name']}" + ("" if s.get("pane") else " 📨"))
        try:
            await msg.add_reaction("📣" if got else "❌")
        except discord.HTTPException:
            pass
        await msg.channel.send((f"-# 📣 → {got}/{len(sessions)} claudes · " + " · ".join(lines))[:1900]
                               if sessions else "-# no live claudes to broadcast to",
                               allowed_mentions=NO_PING)

    # ---------- #all-claudes ask-rounds: fan out, fan in, summarize ----------

    async def ensure_hub(self, channel):
        """The persistent summarizer session. Reuse it if live (it accumulates context across
        rounds and can follow up with individual claudes); otherwise spawn it in tmux with its
        own thread under the main channel. Its replies are mirrored into #all-claudes."""
        for s in await asyncio.to_thread(live_sessions):
            if s["name"] == ASK_HUB_NAME and reach(s):
                st = state.get(s["key"])
                if st is not None and not st.get("hub"):
                    st["hub"] = True
                    save_state()
                return s
        ASK_DIR.mkdir(parents=True, exist_ok=True)
        pane, err = await asyncio.to_thread(spawn_claude, ASK_HUB_NAME, ASK_DIR)
        if not pane:
            await self.say(channel, f"❌ couldn't launch the summarizer: `{err}`")
            return None
        session, notes = await self.await_registration(pane)
        if not session:
            await self.say(channel, await self.spawn_failure_text(pane, str(ASK_DIR), notes))
            return None
        thread = await self.adopt_session(session, self.main_channel, {"spawned": True, "hub": True})
        await self.say(channel, f"🧠 summarizer **{ASK_HUB_NAME}** is up"
                                + (f" → <#{thread.id}>" if thread else "") + " — it persists across rounds")
        return session

    async def ask_round(self, msg, question):
        """One round: ask every live claude, collect replies, hand the bundle to the hub."""
        who = getattr(msg.author, "display_name", str(msg.author))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rid = stamp[-6:]
        sessions = [s for s in await asyncio.to_thread(live_sessions)
                    if reach(s) and s["name"] != ASK_HUB_NAME]
        if not sessions:
            return await msg.channel.send("-# no live claudes to ask", allowed_mentions=NO_PING)
        try:
            await msg.add_reaction("🧠")
        except discord.HTTPException:
            pass
        status = await msg.channel.send(
            f"🧠 **round {rid}** — asking {len(sessions)} claudes, collecting replies "
            f"(up to {ASK_COLLECT_SECS // 60} min)…", allowed_mentions=NO_PING)
        framed = (f"[all-claudes · round {rid}] {who} asks every claude at once: {question}\n"
                  "Reply briefly (a few sentences; concrete numbers, paths, states where relevant). "
                  "Your reply is collected with the other claudes' and handed to a summarizer claude "
                  "for the user — so answer directly, don't ask what they meant. If it doesn't apply "
                  "to your work, reply exactly: n/a")
        pending, undelivered = {}, []
        for s in sessions:
            tp = s.get("transcript")
            off = os.path.getsize(tp) if tp and os.path.exists(tp) else 0
            ok, _ = await asyncio.to_thread(self.deliver, s, msg.author, framed)
            if ok:
                pending[s["key"]] = {"s": s, "off": off, "text": "", "done": False}
            else:
                undelivered.append(s["name"])
        t0, last_edit = time.time(), 0.0

        def n_done():
            return sum(1 for p in pending.values() if p["done"])

        while pending and n_done() < len(pending) and time.time() - t0 < ASK_COLLECT_SECS:
            await asyncio.sleep(5)
            live = {x["key"]: x for x in await asyncio.to_thread(live_sessions)}
            for key, p in pending.items():
                if p["done"]:
                    continue
                tp = p["s"].get("transcript")
                if tp and os.path.exists(tp):
                    try:
                        items, _ = await asyncio.to_thread(
                            checkin.parse_transcript, Path(tp), p["off"], checkin.TAIL_BYTES, True)
                    except OSError:
                        items = []
                    texts = [it["text"] for it in items
                             if it["kind"] == "assistant" and it.get("text", "").strip()]
                    if texts:
                        p["text"] = "\n".join(texts)
                cur = live.get(key)
                if p["text"] and (cur is None or cur["status"] != "busy"):
                    p["done"] = True
            if time.time() - last_edit > 10:
                last_edit = time.time()
                try:
                    await status.edit(content=f"🧠 **round {rid}** — {n_done()}/{len(pending)} replied "
                                              f"({int(time.time() - t0)}s)…", allowed_mentions=NO_PING)
                except discord.HTTPException:
                    pass
        # the round brief
        ASK_DIR.mkdir(parents=True, exist_ok=True)
        brief = ASK_DIR / f"round-{stamp}.md"
        lines = [f"# all-claudes round {rid}", "", f"- asked by: {who}",
                 f"- when: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
                 f"- asked: {len(sessions)} claudes · answered: {n_done()} · "
                 f"collection window: {ASK_COLLECT_SECS}s", "", "## Question", "", question, "",
                 "## Replies (one per claude; follow up with any of them by name via SendMessage)", ""]
        live = {x["key"]: x for x in await asyncio.to_thread(live_sessions)}
        for key, p in pending.items():
            s = p["s"]
            cur = live.get(key) or s
            lines += [f"### {s['name']}  ·  `{s.get('project') or s.get('cwd')}`  ·  status now: {cur.get('status')}",
                      f"transcript: {s.get('transcript')}", ""]
            if p["text"]:
                t = p["text"].strip()
                lines.append(t[:ASK_REPLY_MAX] + (" …(truncated)" if len(t) > ASK_REPLY_MAX else ""))
            else:
                lines.append(f"*(no reply within {ASK_COLLECT_SECS}s — it was {cur.get('status')}; "
                             "its answer may land in its own thread later)*")
            lines.append("")
        for name in undelivered:
            lines += [f"### {name}", "*(message could not be delivered)*", ""]
        brief.write_text("\n".join(lines))
        # hand it to the summarizer
        hub = await self.ensure_hub(msg.channel)
        if not hub:
            await status.edit(content=f"⚠️ **round {rid}** — {n_done()}/{len(pending)} replied but the "
                                      f"summarizer couldn't start. Raw replies: `{brief}`",
                              allowed_mentions=NO_PING)
            return
        hub_st = state.get(hub["key"]) or {}
        hub_msg = (
            f"[all-claudes · round {rid}] {who} asked every claude: {question!r}\n"
            f"The replies are in {brief} — {n_done()}/{len(pending)} answered; each entry has the "
            "claude's name, project, current status, transcript path and reply. Read the whole file.\n"
            "Then write ONE synthesized answer for the user: lead with the conclusion, attribute "
            "specifics to the claude that said them, flag contradictions and who didn't answer, keep "
            "it tight. Your replies in this session are mirrored into #all-claudes automatically, so "
            "just reply here.\n"
            "If something needs checking with a specific claude, follow up: `ListAgents` shows the "
            "other local sessions and `SendMessage` reaches one by name — they reply back to you. "
            "Use that for gaps and contradictions. The user's follow-ups reach you when they reply to "
            "one of your messages in #all-claudes or prefix with `!hub`; a plain new message there "
            "starts a fresh round to everyone. You are the synthesizer: don't redo their work.")
        ok, err = await asyncio.to_thread(self.deliver, hub, _Owner(self.owner), hub_msg)
        await status.edit(
            content=(f"🧠 **round {rid}** — {n_done()}/{len(pending)} replied"
                     + (f", {len(undelivered)} unreachable" if undelivered else "")
                     + f" · handed to **{ASK_HUB_NAME}**"
                     + (f" <#{hub_st['thread']}>" if hub_st.get("thread") else "")
                     + (" — synthesis lands here" if ok else f" (⚠️ brief not delivered: {err})")
                     + f"\n-# raw replies: `{brief}`"),
            allowed_mentions=NO_PING)
        self.log_event(f"🧠 all-claudes round {rid}: {n_done()}/{len(pending)} replied")

    MODE_LABELS = {"bypass": "bypass permissions", "auto": "auto mode", "plan": "plan mode",
                   "accept": "accept edits", "default": "default mode"}

    async def cycle_mode(self, key, st, respond, want):
        """`!bypass` / `!auto` / `!mode <x>`: switch a live session's permission mode by pressing
        shift+tab (the same cycle as in the terminal) until the wanted mode's label is on the
        bottom status line. `!bypass` is how you turn the auto-mode classifier OFF for a
        session that is being blocked; `!auto` turns it back on. Bypass only appears in the
        cycle when the claude was launched with --allow-dangerously-skip-permissions (bridge-
        spawned ones are), so a session that never shows it says so instead of looping."""
        want = (want or "").strip().lower()
        label = self.MODE_LABELS.get(want)
        if not label:
            return await respond(f"modes: {', '.join(self.MODE_LABELS)}")
        s = await asyncio.to_thread(find_live_by_key, key)
        if not s or not s.get("pane"):
            return await respond("-# needs a live session with a tmux pane")

        def bottom():
            lines = [l.strip() for l in screen_text(s["pane"], 700).splitlines() if l.strip()]
            return " ".join(lines[-3:]).lower()

        seen = []
        for attempt in range(7):
            b = await asyncio.to_thread(bottom)
            seen.append(b[-60:])
            if label in b:
                on = "classifier **off**" if want == "bypass" else ("classifier **on**" if want == "auto" else "")
                return await respond(f"✅ **{s['name']}** is now in **{want}** mode"
                                     + (f" — {on}" if on else "")
                                     + (f" (after {attempt} shift+tab)" if attempt else " (already was)"))
            await asyncio.to_thread(checkin.send_to_session, s, None, "BTab")
            await asyncio.sleep(0.8)
        hint = (" — bypass isn't in this session's cycle (launched without "
                "--allow-dangerously-skip-permissions); `!restart` it and try again" if want == "bypass" else "")
        await respond(f"⚠️ pressed shift+tab 7× without seeing **{label}**{hint}. Bottom line now: "
                      f"`{seen[-1][:120]}`")

    def privileged(self, user):
        """Owner-only commands (/model, /globalmodel): they change which model — and so what
        spend and behaviour — sessions run on, and Claude Code's /model also rewrites the
        default for new sessions."""
        return bool(self.owner) and getattr(user, "id", None) == self.owner

    async def type_slash(self, s, text):
        """Type a Claude Code slash command into a session's pane as keystrokes (send-keys -l,
        not a paste, so it is recognised as a command) and submit it."""
        rc, out = await asyncio.to_thread(checkin.run, ["tmux", "send-keys", "-t", s["pane"], "-l", text])
        if rc == 0:
            await asyncio.sleep(0.6)
            rc, out = await asyncio.to_thread(checkin.run, ["tmux", "send-keys", "-t", s["pane"], "Enter"])
        return rc == 0, (out or "").strip()

    async def set_model(self, key, st, user, respond, name):
        """`!model <name>` / `/model` (owner only): switch this claude's model via Claude Code's
        own `/model`. A busy claude or one showing a prompt gets it queued and applied the
        moment it is idle (typing into a running turn or a dialog would go astray)."""
        if not self.privileged(user):
            return await respond("⛔ `/model` is owner-only")
        name = (name or "").strip()
        if not MODEL_RE.match(name):
            return await respond("`!model <name>` — fable, opus, sonnet, haiku, or a full id like "
                                 "`claude-opus-5-5`")
        s = await asyncio.to_thread(find_live_by_key, key)
        if not s or not s.get("pane"):
            return await respond("-# needs a live session with a tmux pane (`!revive` first)")
        if s["status"] in ("busy", "waiting"):
            st["pending_model"] = name
            save_state()
            return await respond(f"⏳ **{s['name']}** is {s['status']} — `/model {name}` is queued and "
                                 "goes in the moment it's idle")
        ok, out = await self.type_slash(s, f"/model {name}")
        await respond(f"🧠 **{s['name']}** → model **{name}**" if ok
                      else f"⚠️ couldn't type into pane `{s['pane']}`: {out[:100]}")

    async def global_model(self, user, respond, name):
        """`/globalmodel <name>` / `!globalmodel` (owner only): switch EVERY live claude to a model
        and make it the default for new and restarted ones (settings.json "model"). Idle ones
        switch now; busy or prompting ones get it queued; one keystroke burst per pane."""
        if not self.privileged(user):
            return await respond("⛔ `/globalmodel` is owner-only")
        name = (name or "").strip()
        if not MODEL_RE.match(name):
            return await respond("`/globalmodel <name>` — fable, opus, sonnet, haiku, or a full id")
        now, queued, skipped, panes = [], [], [], set()
        for s in sorted(await asyncio.to_thread(live_sessions), key=lambda x: x["name"]):
            if not s.get("pane"):
                skipped.append(f"{s['name']} (no pane)")
                continue
            if proc_state(s["pid"]) == "T" or s["pane"] in panes:
                continue                      # ctrl-z'd, or a second claude sharing that pane
            panes.add(s["pane"])
            st = state.get(s["key"])
            if s["status"] in ("busy", "waiting"):
                if st is not None:
                    st["pending_model"] = name
                    queued.append(s["name"])
                else:
                    skipped.append(f"{s['name']} (untracked)")
                continue
            ok, _ = await self.type_slash(s, f"/model {name}")
            (now if ok else skipped).append(s["name"] if ok else f"{s['name']} (tmux error)")
            await asyncio.sleep(0.3)
        save_state()
        try:
            prev = await asyncio.to_thread(set_default_model, name)
            dflt = f"default for new/restarted claudes: `{prev}` → `{name}` (settings.json)"
        except (OSError, ValueError) as e:
            dflt = f"⚠️ couldn't update settings.json: {e}"
        lines = [f"🧠 **global model → {name}**", f"-# {dflt}"]
        if now:
            lines.append(f"✅ switched now ({len(now)}): " + ", ".join(now))
        if queued:
            lines.append(f"⏳ queued until idle ({len(queued)}): " + ", ".join(queued))
        if skipped:
            lines.append(f"⏭️ skipped ({len(skipped)}): " + ", ".join(skipped))
        await respond("\n".join(lines)[:1900])
        self.log_event(f"🧠 global model → {name}: {len(now)} now, {len(queued)} queued")

    async def set_fast(self, key, st, user, respond, mode):
        """`/fast [on|off]` / `!fast` (owner only — fast mode draws from usage credits): type
        Claude Code's own `/fast on|off` into this session, then report what Claude Code
        answered on screen. /fast is an immediate command, so a busy session gets it right
        away; one showing a prompt gets it once the prompt is answered."""
        if not self.privileged(user):
            return await respond("⛔ `/fast` is owner-only (fast mode draws from usage credits)")
        mode = (mode or "on").strip().lower()
        if mode not in ("on", "off"):
            return await respond("`/fast on` or `/fast off`")
        s = await asyncio.to_thread(find_live_by_key, key)
        if not s or not s.get("pane"):
            return await respond("-# needs a live session with a tmux pane (`/revive` first)")
        if s["status"] == "waiting":
            st["pending_fast"] = mode
            save_state()
            return await respond(f"⏳ **{s['name']}** is showing a prompt — `/fast {mode}` goes in once "
                                 "it's answered")
        ok, out = await self.type_slash(s, f"/fast {mode}")
        if not ok:
            return await respond(f"⚠️ couldn't type into pane `{s['pane']}`: {out[:100]}")
        await asyncio.sleep(1.8)
        said = await asyncio.to_thread(fast_result, s["pane"])
        await respond(f"⚡ **{s['name']}**: {said or f'sent `/fast {mode}` (no answer on screen yet — `/screen` to check)'}")

    async def global_fast(self, user, respond, mode):
        """`/fast <on|off> everywhere` (owner only): every live claude, plus "fastMode" in
        settings.json so new and restarted sessions start that way. Reports what each session's
        Claude Code answered, grouped (e.g. '5× Fast mode ON · 2× Fast mode disabled · …')."""
        if not self.privileged(user):
            return await respond("⛔ `/fast` is owner-only (fast mode draws from usage credits)")
        mode = (mode or "on").strip().lower()
        if mode not in ("on", "off"):
            return await respond("`/fast on everywhere` or `/fast off everywhere`")
        typed, queued, skipped, panes = [], [], [], set()
        for s in sorted(await asyncio.to_thread(live_sessions), key=lambda x: x["name"]):
            if not s.get("pane"):
                skipped.append(f"{s['name']} (no pane)")
                continue
            if proc_state(s["pid"]) == "T" or s["pane"] in panes:
                continue
            panes.add(s["pane"])
            st = state.get(s["key"])
            if s["status"] == "waiting":
                if st is not None:
                    st["pending_fast"] = mode
                    queued.append(s["name"])
                continue
            ok, _ = await self.type_slash(s, f"/fast {mode}")
            if ok:
                typed.append(s)
            else:
                skipped.append(f"{s['name']} (tmux error)")
            await asyncio.sleep(0.3)
        save_state()
        await asyncio.sleep(1.8)
        results = Counter()
        for s in typed:
            results[await asyncio.to_thread(fast_result, s["pane"]) or "no answer on screen yet"] += 1
        try:
            prev = await asyncio.to_thread(set_user_setting, "fastMode", mode == "on")
            dflt = f"default for new/restarted sessions: fastMode `{prev}` → `{mode == 'on'}` (settings.json)"
        except (OSError, ValueError) as e:
            dflt = f"⚠️ couldn't update settings.json: {e}"
        lines = [f"⚡ **fast mode {mode} everywhere** — {len(typed)} session(s)", f"-# {dflt}"]
        if results:
            lines.append(" · ".join(f"{n}× {r}" for r, n in results.most_common()))
        if queued:
            lines.append(f"⏳ queued until their prompt is answered: " + ", ".join(queued))
        if skipped:
            lines.append(f"⏭️ skipped: " + ", ".join(skipped))
        await respond("\n".join(lines)[:1900])
        self.log_event(f"⚡ fast mode {mode} everywhere: {len(typed)} sessions")

    async def set_effort(self, key, st, respond, level):
        """`!effort <level>` / `/effort`: change a live claude's reasoning effort by typing Claude
        Code's own `/effort <level>` into its pane (typed, not pasted, so the slash command is
        recognised). Levels: low, medium, high, xhigh, max."""
        level = (level or "").strip().lower()
        if level not in EFFORT_LEVELS:
            return await respond(f"effort levels: {', '.join(EFFORT_LEVELS)}")
        s = await asyncio.to_thread(find_live_by_key, key)
        if not s or not s.get("pane"):
            return await respond("-# needs a live session with a tmux pane (`!revive` first)")
        rc, out = await asyncio.to_thread(checkin.run, ["tmux", "send-keys", "-t", s["pane"], "-l",
                                                        f"/effort {level}"])
        if rc == 0:
            await asyncio.sleep(0.6)
            rc, out = await asyncio.to_thread(checkin.run, ["tmux", "send-keys", "-t", s["pane"], "Enter"])
        if rc == 0:
            return await respond(f"🧑‍🔬 **{s['name']}** effort → **{level}**")
        await respond(f"⚠️ couldn't type into pane `{s['pane']}`: {out.strip()[:100]}")

    async def rename_session(self, key, st, respond, new_name):
        """`!rename <name>` / `/rename`: rename a session. Live with a pane → type Claude Code's
        own `/rename <name>` into it (typed keystrokes, not a paste, so the slash command is
        recognised); the registry name changes and the poller retitles the thread and posts the
        ✏️ line as it does for any rename. Otherwise → rename the thread and the bridge's
        display name only."""
        new = " ".join((new_name or "").split())[:60]
        if not new:
            return await respond("`!rename <new name>`")
        s = await asyncio.to_thread(find_live_by_key, key)
        if s and s.get("pane"):
            rc, out = await asyncio.to_thread(checkin.run, ["tmux", "send-keys", "-t", s["pane"], "-l",
                                                            f"/rename {new}"])
            if rc == 0:
                await asyncio.sleep(0.6)
                rc, out = await asyncio.to_thread(checkin.run, ["tmux", "send-keys", "-t", s["pane"], "Enter"])
            if rc == 0:
                return await respond(f"✏️ asked **{s['name']}** to rename itself to **{new}** — the "
                                     "thread title follows in a few seconds")
            await respond(f"⚠️ couldn't type into pane `{s['pane']}` ({out.strip()[:80]}) — renaming "
                          "the thread only")
        st["name"] = new
        st["thread_name"] = thread_title(new, st.get("sid") or "", False)
        save_state()
        thread = await self.get_thread(st.get("thread"))
        if thread:
            try:
                await thread.edit(name=st["thread_name"])
            except discord.HTTPException as e:
                return await respond(f"⚠️ couldn't rename the thread: {e}")
        await respond(f"✏️ thread renamed to **{new}**"
                      + ("" if (s and s.get("pane")) else " (session not live in tmux — its own name is unchanged)"))

    async def refresh_session(self, key, st, channel, respond, message="", force=False):
        """`!refresh [force] [message]` / `/refresh`: unstick a claude with the gentlest thing
        that works, escalating only as needed.
          1. not live (ended/crashed/background) → revive it into tmux.
          2. live and busy/waiting → send Esc (twice; the first may just close a dialog) and
             see if it comes back idle. Usually enough for a hung turn or a stuck prompt.
          3. still frozen → in-place restart with force (clean exit + `claude -r`, history kept).
          A claude that is idle and wrote to its transcript recently is not stuck: say so and
          only deliver `message`, unless `force`. After any successful unstick, deliver `message`."""
        s = await asyncio.to_thread(find_live_by_key, key)
        name = (s or {}).get("name") or st.get("name") or "?"
        owner = _Owner(self.owner)

        async def send(sess):
            if not message:
                return
            ok, err = await asyncio.to_thread(self.deliver, sess, owner, message)
            await respond("📨 your message was delivered" if ok else f"⚠️ message not delivered: {err}")

        if not s:
            await respond(f"🔄 **{name}** isn't live — reviving it instead")
            sess = await self.revive(key, st, channel, force=True, reporter=respond)
            if sess:
                await send(sess)
            return
        if not s.get("pane"):
            await respond(f"🔄 **{name}** has no tmux pane (background) — reviving it into tmux")
            sess = await self.revive(key, st, channel, force=True, reporter=respond)
            if sess:
                await send(sess)
            return
        tp = s.get("transcript")
        last = os.path.getmtime(tp) if tp and os.path.exists(tp) else None
        since = f", transcript last written {age_str(last)} ago" if last else ""
        await respond(f"🔄 refreshing **{name}** — status **{s['status']}**{since}")
        if s["status"] == "idle" and not force and last and time.time() - last < 300:
            await respond("-# it looks idle and was active in the last 5 min — not stuck. "
                          + ("Delivering your message." if message else
                             "`!refresh force` restarts it anyway."))
            await send(s)
            return
        if s["status"] in ("busy", "waiting", "shell"):
            for attempt in (1, 2):
                await asyncio.to_thread(checkin.send_to_session, s, None, "Escape")
                await asyncio.sleep(4)
                s2 = await asyncio.to_thread(find_live_by_key, key)
                if s2 and s2["status"] not in ("busy", "shell"):
                    await respond(f"✅ Esc{'' if attempt == 1 else ' ×2'} did it — **{name}** is "
                                  f"**{s2['status']}** again, history intact")
                    await send(s2)
                    return
        await respond(f"⚠️ still stuck after Esc — restarting **{name}** in place "
                      "(clean exit + `claude -r`, history kept)"
                      + (" — this also kills the shell job it was watching" if s["status"] == "shell" else ""))
        sess = await self.restart_session(key, st, channel, respond, force=True)
        if sess:
            await send(sess)

    async def restart_session(self, key, st, thread, respond, force=False):
        """Restart a live tmux session IN PLACE: clean SIGTERM (flushes the transcript),
        then `claude -r <sid>` with its original flags typed into the same pane, thread
        moved onto the new process. Used to pick up new settings, keys or hooks — a
        running claude keeps the auth and env it started with.

        Refuses busy/shell sessions unless forced: a `shell` session has a child process
        (background job, tunnel) that dies with it. Also refuses a pane that hosts more
        than one claude, since killing one would just foreground the other.
        Returns the new session dict or None."""
        s = await asyncio.to_thread(find_live_by_key, key)
        if not s:
            await respond("-# 👻 not live — `!revive` instead")
            return None
        if not s.get("pane"):
            await respond("-# no tmux pane — `!revive` instead")
            return None
        if proc_state(s["pid"]) == "T":
            await respond(f"-# ⏭️ **{s['name']}** is suspended (ctrl-z) in its pane — `fg` it first, "
                          "or `!kill` it")
            return None
        if s["status"] in ("busy", "shell") and not force:
            await respond(f"-# ⏭️ **{s['name']}** is **{s['status']}** — a restart would interrupt it"
                          + (" and kill the shell job it is watching" if s["status"] == "shell" else "")
                          + ". `!restart force` if you really want to.")
            return None
        # a suspended (ctrl-z) claude in the same pane just sits in the shell's job table
        siblings = [x for x in await asyncio.to_thread(live_sessions)
                    if x["pane"] == s["pane"] and x["key"] != key and proc_state(x["pid"]) != "T"]
        if siblings:
            await respond(f"-# ⏭️ pane `{s['pane']}` hosts {len(siblings) + 1} claudes "
                          f"({', '.join(x['name'] for x in siblings)}) — restart that one by hand")
            return None
        argv = await asyncio.to_thread(claude_argv, s["pid"])
        flags = restart_flags(argv) if argv else spawn_flags()
        sid, name, pane = s["sid"], s["name"], s["pane"]
        crons = await asyncio.to_thread(session_crons, s.get("transcript"))
        st["restarting"] = True
        save_state()
        try:
            try:
                await asyncio.to_thread(os.kill, s["pid"], signal.SIGTERM)
            except OSError as e:
                await respond(f"-# ❌ couldn't signal pid {s['pid']}: {e}")
                return None
            for _ in range(30):
                await asyncio.sleep(1)
                if not await asyncio.to_thread(find_live_by_key, key) and \
                        not await asyncio.to_thread(pane_claude_alive, pane):
                    break
            else:
                await respond(f"-# ⚠️ **{name}** didn't exit within 30 s of SIGTERM — left alone "
                              f"(`!kill hard` kills its pane if it's stuck)")
                return None
            await asyncio.sleep(0.8)                   # let the shell prompt come back
            cmd = f"{shlex.quote(CLAUDE_BIN)} {flags} -r {shlex.quote(sid)} -n {shlex.quote(name)}"
            rc, out = await asyncio.to_thread(checkin.run, ["tmux", "send-keys", "-t", pane, "-l", cmd])
            if rc == 0:
                rc, out = await asyncio.to_thread(checkin.run, ["tmux", "send-keys", "-t", pane, "Enter"])
            if rc != 0:
                await respond(f"-# ❌ couldn't type into pane `{pane}`: {out.strip()[:120]}")
                return None
            session, notes = await self.await_registration(pane)
            if not session:
                await respond(await self.spawn_failure_text(pane, s["cwd"], notes))
                return None
        finally:
            st.pop("restarting", None)
        old = state.pop(key, None) or st
        title = thread_title(session["name"], session["sid"], False)
        state[session["key"]] = {
            **old, "sid": session["sid"], "name": session["name"], "cwd": session["cwd"],
            "status": session["status"], "thread_name": title, "ended": False,
            "size": os.path.getsize(session["transcript"]) if session["transcript"] else 0,
            "tool_msg": None, "tool_run": None, "prompt_msg": None, "subs": None}
        save_state()
        await self.edit_status(state[session["key"]], session)
        await respond(f"-# ♻️ **{session['name']}** restarted in place — pane `{pane}`, history intact"
                      + ("".join(f"\n-# 🛠 {n}" for n in notes) if notes else ""))
        if crons:
            listing = "\n".join(f"- `{c['cron']}`{'' if c['recurring'] else ' (one-shot)'}: {c['prompt']}"
                                for c in crons)
            note = ("[chert] You were just restarted in place (clean exit + `claude -r`) so you pick up "
                    "new auth/settings; your conversation history is intact. Session-only cron jobs do "
                    "not survive a restart. These CronCreate calls were made during this session:\n"
                    f"{listing}\nRe-create with CronCreate whichever of them should still be running "
                    "(skip ones you had already cancelled or that are finished), then carry on.")
            ok, _ = await asyncio.to_thread(checkin.send_to_session, session, note)
            await respond(f"-# ⏰ {len(crons)} session-only cron(s) were in this session — the claude "
                          f"has been asked to re-arm the ones that should still run"
                          + ("" if ok else " (⚠️ note not delivered)"))
        self.log_event(f"♻️ {session['name']} restarted")
        return session

    async def restart_all(self, channel, force=False, skip_pids=()):
        """Rolling in-place restart of every live tmux session (idle ones unless forced),
        longest-idle first, one at a time."""
        sessions = await asyncio.to_thread(live_sessions)
        todo = [s for s in sessions if s["pane"] and s["pid"] not in skip_pids]
        todo.sort(key=lambda s: s["updatedAt"])
        await self.say(channel, f"♻️ **rolling restart** — {len(todo)} session(s) with a pane"
                                + ("" if force else "; busy/shell ones are skipped"))
        done, skipped, failed = [], [], []
        for s in todo:
            st = state.get(s["key"])
            if not st or st.get("pending") or st.get("ended"):
                skipped.append(f"{s['name']} (no thread yet)")
                continue
            if proc_state(s["pid"]) == "T":
                skipped.append(f"{s['name']} (suspended with ctrl-z)")
                continue
            if s["status"] in ("busy", "shell") and not force:
                skipped.append(f"{s['name']} ({s['status']})")
                continue
            thread = await self.get_thread(st.get("thread"))
            res = await self.restart_session(s["key"], st, thread, lambda t, th=thread: self.say(th, t),
                                             force=force)
            (done if res else failed).append(s["name"])
            await asyncio.sleep(2)
        lines = [f"♻️ restarted **{len(done)}**: " + (", ".join(done) or "—")]
        if skipped:
            lines.append(f"⏭️ skipped **{len(skipped)}** (busy/shell/untracked): " + ", ".join(skipped)
                         + " — `!restart force` in their threads when they are done")
        if failed:
            lines.append(f"❌ failed **{len(failed)}**: " + ", ".join(failed) + " — details in their threads")
        await self.say(channel, "\n".join(lines))
        return done, skipped, failed

    async def revive_all(self, msg):
        """Bring back everything that died in the last reboot/crash (last 48h)."""
        cutoff = time.time() - 48 * 3600
        targets = [(k, v) for k, v in sessions_state().items()
                   if v.get("ended") and v.get("ended_reason") in ("reboot", "crash")
                   and (v.get("ended_at") or 0) > cutoff and v.get("sid")]
        if not targets:
            return await self.say(msg.channel,
                                  "nothing to revive — no reboot/crash casualties in the last 48h")
        await self.say(msg.channel, f"♻️ reviving {len(targets)} session(s)…")
        ok = 0
        for k, v in targets:
            thread = await self.get_thread(v.get("thread"))
            res = await self.revive(k, v, thread, reporter=lambda t, th=thread: self.say(th, t))
            ok += bool(res)
            await asyncio.sleep(3)
        await self.say(msg.channel, f"♻️ revived **{ok}/{len(targets)}**")

    async def kill_session(self, msg, key, st, hard=False, delete=False):
        """End the claude behind this thread, then retire the thread."""
        s = await asyncio.to_thread(find_live_by_key, key)
        what = "session was already gone"
        if s:
            if hard and s["pane"]:
                rc, out = await asyncio.to_thread(
                    checkin.run, ["tmux", "kill-pane", "-t", s["pane"]])
                what = (f"killed tmux pane {s['pane']}" if rc == 0
                        else f"kill-pane failed: {out.strip()[:120]}")
            else:
                try:
                    await asyncio.to_thread(os.kill, s["pid"], signal.SIGTERM)
                    what = f"SIGTERM → pid {s['pid']} (exits cleanly; `!kill hard` kills the pane)"
                except OSError as e:
                    what = f"couldn't signal pid {s['pid']}: {e}"
        st["ended"] = True
        st["status"] = "ended"
        st["ended_at"] = time.time()
        st["ended_reason"] = "killed"
        save_state()
        await self.retire_thread(msg.channel, f"-# 🌌 {what} — archiving", delete=delete)

    # ---------- inbound ----------

    async def sessions_text(self):
        """Every live claude as a clickable thread link.

        Discord's sidebar quietly hides threads once a channel has a dozen-plus, so
        without this a live session is effectively invisible even though it's fine.
        """
        sessions = await asyncio.to_thread(live_sessions)
        if not sessions:
            return "🔭 no signals — no live claudes"
        free = f" · 💾 {ash_twin.disk_free()[0]:.1f} GB free" if ash_twin else ""
        lines = [f"🔭 **signalscope: {len(sessions)} traveler(s)**{free} · [dashboard]({DASHBOARD})"]
        for s in sessions:
            st = state.get(s["key"], {})
            where = f"<#{st['thread']}>" if st.get("thread") else "*(no thread yet)*"
            tag = "" if s["pane"] else " · 👁️ read-only → `!revive`"
            ago = f" · <t:{int(s['updatedAt'] / 1000)}:R>" if s.get("updatedAt") else ""
            lines.append(f"{STATUS_EMOJI.get(s['status'], '⚪')} {where} · "
                         f"`{s['project']}` · {STATUS_WORD.get(s['status'], s['status'])}{tag}{ago}")
        return "\n".join(lines)

    async def post_sessions(self, msg):
        for chunk in split_chunks(await self.sessions_text()):
            await msg.channel.send(chunk, suppress_embeds=True, allowed_mentions=NO_PING)

    def invite_url(self):
        return (f"https://discord.com/oauth2/authorize?client_id={self.application_id}"
                f"&scope=bot%20applications.commands&permissions=0")

    def register_commands(self):
        """Real Discord slash commands (/resume with live autocomplete, /fork, /sessions).
        They need the bot invited with the applications.commands scope; the ! commands
        work either way."""
        tree = self.tree
        bridge = self

        def followup(interaction):
            async def respond(text):
                try:
                    await interaction.followup.send(text, allowed_mentions=NO_PING,
                                                    suppress_embeds=True)
                except discord.HTTPException as e:
                    log_error("slash followup", e)
            return respond

        @tree.command(name="resume", description="Search every session this box has ever had "
                                                 "and bring one up in a thread")
        @discord.app_commands.describe(session="start typing: name, first prompt, project or id")
        async def resume_cmd(interaction: discord.Interaction, session: str):
            await interaction.response.defer(thinking=True)
            respond = followup(interaction)
            rows = await asyncio.to_thread(session_index)
            sid = session.strip()
            if sid not in {r["sid"] for r in rows}:
                hits = search_sessions(sid, rows)
                if not hits:
                    return await respond(f"🔭 no sessions match `{sid}`")
                if len(hits) > 1:
                    return await interaction.followup.send(
                        resume_text(sid, 0, hits), view=resume_view(sid, 0, hits),
                        allowed_mentions=NO_PING, suppress_embeds=True)
                sid = hits[0]["sid"]
            await bridge.bring_up(sid, interaction.user, respond)

        @resume_cmd.autocomplete("session")
        async def resume_autocomplete(interaction: discord.Interaction, current: str):
            rows = await asyncio.to_thread(search_sessions, current)
            return [discord.app_commands.Choice(
                name=(f"{'🟢 ' if r['live'] else '🪐 ' if r['where'] == 's3' else ''}"
                      f"{r['label']} · {r['project']} · {age_str(r['mtime'])}")[:100],
                value=r["sid"]) for r in rows[:25]]

        @tree.command(name="fork", description="Clone this thread's session into a new claude "
                                               "with its own thread")
        @discord.app_commands.describe(message="optional first message for the copy",
                                       to="claude (default): same history in a new claude · "
                                          "astra: hand the conversation to a GPT-6 Astra session")
        @discord.app_commands.choices(to=[
            discord.app_commands.Choice(name="claude — same history, new claude", value="claude"),
            discord.app_commands.Choice(name="astra — hand the conversation to GPT-6 (codex)", value="astra")])
        async def fork_cmd(interaction: discord.Interaction, message: str = "", to: str = "claude"):
            key = thread_to_key().get(interaction.channel_id)
            if not key:
                return await interaction.response.send_message(
                    "run this inside a session's thread", ephemeral=True)
            await interaction.response.defer(thinking=True)
            if to == "astra":
                return await bridge.fork_to_astra(interaction.channel, interaction.user, key,
                                                  state[key], message, followup(interaction))
            await bridge.fork_session(interaction.channel, interaction.user, key, state[key],
                                      message, followup(interaction))

        @tree.command(name="mode", description="Switch this thread's claude's permission mode "
                                               "(bypass = classifier off, auto = on)")
        @discord.app_commands.describe(mode="bypass turns the auto-mode classifier off; auto turns it back on")
        @discord.app_commands.choices(mode=[
            discord.app_commands.Choice(name="bypass — classifier OFF", value="bypass"),
            discord.app_commands.Choice(name="auto — classifier ON", value="auto"),
            discord.app_commands.Choice(name="plan", value="plan"),
            discord.app_commands.Choice(name="default", value="default")])
        async def mode_cmd(interaction: discord.Interaction, mode: str):
            key = thread_to_key().get(interaction.channel_id)
            if not key:
                return await interaction.response.send_message(
                    "run this inside a session's thread", ephemeral=True)
            await interaction.response.defer(thinking=True)
            await bridge.cycle_mode(key, state[key], followup(interaction), mode)
        # ---- slash versions of the text commands: each runs the same handler as its `!` form ----
        Choice = discord.app_commands.Choice

        async def run_as_text(interaction, text, where):
            """where = "thread" (a session or Astra thread only), "main" (#claudes),
            "broadcast" (#all-claudes), or "here" (this session thread, else #claudes).
            A thread-only command never runs anywhere else, so it can't fall through and be
            typed into a session as plain text."""
            in_session = interaction.channel_id in thread_to_key()
            in_astra = str(interaction.channel_id) in state.get("_astra", {})
            if where == "thread":
                if not (in_session or in_astra):
                    return await interaction.response.send_message(
                        "run this inside a session's thread", ephemeral=True)
                target = interaction.channel
            elif where == "main":
                target = bridge.main_channel
            elif where == "broadcast":
                target = bridge.broadcast_channel
            else:
                target = interaction.channel if in_session else bridge.main_channel
            if target is None:
                return await interaction.response.send_message("that channel isn't set up", ephemeral=True)
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await bridge.on_message(_SlashMsg(interaction, target, text))
            except Exception as e:  # noqa: BLE001
                log_error(f"slash {text.split()[0]}", e)
                return await interaction.followup.send(f"⚠️ `{text}` failed: {e}", ephemeral=True)
            there = "" if target.id == interaction.channel_id else f" → <#{target.id}>"
            await interaction.followup.send(f"-# ran `{text}`{there}", ephemeral=True)

        # in a session's thread
        @tree.command(name="screen", description="Show this session's terminal screen")
        async def screen_cmd(interaction: discord.Interaction):
            await run_as_text(interaction, "!screen", "thread")

        @tree.command(name="key", description="Press a key in this session's terminal")
        @discord.app_commands.describe(key="the key to press")
        @discord.app_commands.choices(key=[Choice(name=k, value=k) for k in (
            "esc", "enter", "up", "down", "left", "right", "tab", "shift-tab", "space", "pgup", "pgdn",
            "1", "2", "3", "4", "5", "6", "7", "8", "9")])
        async def key_cmd(interaction: discord.Interaction, key: str):
            await run_as_text(interaction, f"!key {key}", "thread")

        @tree.command(name="restart", description="Restart this session in place, history kept (e.g. to pick up new settings)")
        @discord.app_commands.describe(force="also restart it if it's busy or watching a shell job")
        async def restart_cmd(interaction: discord.Interaction, force: bool = False):
            await run_as_text(interaction, "!restart force" if force else "!restart", "thread")

        @tree.command(name="revive", description="Bring back this thread's ended, crashed or background session")
        @discord.app_commands.describe(mode="normal · force: also stop a busy copy · fork: keep the original running")
        @discord.app_commands.choices(mode=[Choice(name="normal", value="normal"),
                                            Choice(name="force", value="force"),
                                            Choice(name="fork", value="fork")])
        async def revive_cmd(interaction: discord.Interaction, mode: str = "normal"):
            await run_as_text(interaction, "!revive" if mode == "normal" else f"!revive {mode}", "thread")

        @tree.command(name="log", description="Timeline of this session's prompts, replies and tool runs")
        @discord.app_commands.describe(count="how many entries (default 25)")
        async def log_cmd(interaction: discord.Interaction,
                          count: discord.app_commands.Range[int, 1, 200] = 25):
            await run_as_text(interaction, f"!log {count}", "thread")

        @tree.command(name="mute", description="Stop posting this session's updates in this thread")
        async def mute_cmd(interaction: discord.Interaction):
            await run_as_text(interaction, "!mute", "thread")

        @tree.command(name="unmute", description="Resume posting this session's updates in this thread")
        async def unmute_cmd(interaction: discord.Interaction):
            await run_as_text(interaction, "!unmute", "thread")

        @tree.command(name="kill", description="End this session and archive its thread")
        @discord.app_commands.describe(how="end · hard: also kill its tmux pane · delete: also delete the thread")
        @discord.app_commands.choices(how=[Choice(name="end", value="end"), Choice(name="hard", value="hard"),
                                           Choice(name="delete", value="delete")])
        async def kill_cmd(interaction: discord.Interaction, how: str = "end"):
            await run_as_text(interaction, "!kill" if how == "end" else f"!kill {how}", "thread")

        @tree.command(name="supernova", description="Countdown for this session; at zero it's told to wrap up and report")
        @discord.app_commands.describe(minutes="length of the countdown (default 22)", then="what happens at zero",
                                       cancel="cancel the running countdown instead")
        @discord.app_commands.choices(then=[Choice(name="tell it to wrap up", value="wrap"),
                                            Choice(name="interrupt its turn first", value="stop"),
                                            Choice(name="end the session", value="kill")])
        async def supernova_cmd(interaction: discord.Interaction,
                                minutes: discord.app_commands.Range[int, 1, 720] = 22,
                                then: str = "wrap", cancel: bool = False):
            text = "!supernova off" if cancel else f"!supernova {minutes}m" + ("" if then == "wrap" else f" {then}")
            await run_as_text(interaction, text, "thread")

        # anywhere
        @tree.command(name="yolo", description="bypassPermissions for sessions started in the next while, or off")
        @discord.app_commands.default_permissions(administrator=True)
        @discord.app_commands.describe(duration="e.g. 30m, 1h, 90s (max 12h), or off")
        async def yolo_cmd(interaction: discord.Interaction, duration: str):
            await run_as_text(interaction, f"!yolo {duration.strip()}", "here")

        @tree.command(name="help", description="Everything chert can do")
        async def help_cmd(interaction: discord.Interaction):
            chunks = split_chunks(HELP)
            await interaction.response.send_message(chunks[0], ephemeral=True, suppress_embeds=True)
            for c in chunks[1:]:
                await interaction.followup.send(c, ephemeral=True, suppress_embeds=True)

        # fleet commands: always run in #claudes
        @tree.command(name="all", description="Send a message to every live session")
        @discord.app_commands.describe(message="what to send")
        async def all_cmd(interaction: discord.Interaction, message: str):
            await run_as_text(interaction, f"!all {message}", "main")

        @tree.command(name="restartall", description="Restart every idle session in place, one at a time")
        @discord.app_commands.default_permissions(administrator=True)
        @discord.app_commands.describe(force="also restart busy sessions")
        async def restartall_cmd(interaction: discord.Interaction, force: bool = False):
            await run_as_text(interaction, "!restart all force" if force else "!restart all", "main")

        @tree.command(name="reviveall", description="Bring back every session that died in a reboot or crash")
        async def reviveall_cmd(interaction: discord.Interaction):
            await run_as_text(interaction, "!revive all", "main")

        @tree.command(name="cleanup", description="Archive the threads of ended sessions")
        @discord.app_commands.default_permissions(administrator=True)
        @discord.app_commands.describe(delete="delete them instead of archiving")
        async def cleanup_cmd(interaction: discord.Interaction, delete: bool = False):
            await run_as_text(interaction, "!cleanup delete" if delete else "!cleanup", "main")

        @tree.command(name="disk", description="Free disk space and the largest directories nobody has touched lately")
        async def disk_cmd(interaction: discord.Interaction):
            await run_as_text(interaction, "!disk", "main")

        @tree.command(name="backup", description="Back up transcripts and config to S3 now")
        async def backup_cmd(interaction: discord.Interaction):
            await run_as_text(interaction, "!backup", "main")

        @tree.command(name="s3", description="What's in the S3 backup bucket")
        async def s3_cmd(interaction: discord.Interaction):
            await run_as_text(interaction, "!s3", "main")

        @tree.command(name="offload", description="Move a directory to S3 and free the disk (shows the plan first)")
        @discord.app_commands.default_permissions(administrator=True)
        @discord.app_commands.describe(directory="directory to move (under your home)",
                                       confirm="actually do it (without this you only get the plan)")
        async def offload_cmd(interaction: discord.Interaction, directory: str, confirm: bool = False):
            await run_as_text(interaction, f"!offload {directory.strip()}" + (" confirm" if confirm else ""), "main")

        @tree.command(name="restore", description="Bring an offloaded directory back from S3")
        @discord.app_commands.default_permissions(administrator=True)
        @discord.app_commands.describe(directory="directory to restore", confirm="actually do it")
        async def restore_cmd(interaction: discord.Interaction, directory: str, confirm: bool = False):
            await run_as_text(interaction, f"!restore {directory.strip()}" + (" confirm" if confirm else ""), "main")

        # #all-claudes
        @tree.command(name="hub", description="Talk to the #all-claudes summarizer directly")
        @discord.app_commands.describe(message="your follow-up")
        async def hub_cmd(interaction: discord.Interaction, message: str):
            await run_as_text(interaction, f"!hub {message}", "broadcast")

        async def model_autocomplete(interaction: discord.Interaction, current: str):
            cur = (current or "").lower()
            return [discord.app_commands.Choice(name=m, value=m)
                    for m in MODEL_SUGGESTIONS if cur in m][:25]

        @tree.command(name="model", description="Switch this thread's claude to another model (owner only)")
        @discord.app_commands.default_permissions(administrator=True)
        @discord.app_commands.describe(name="fable, opus, sonnet, haiku, or a full id like claude-opus-5-5")
        async def model_cmd(interaction: discord.Interaction, name: str):
            if not bridge.privileged(interaction.user):
                return await interaction.response.send_message("⛔ `/model` is owner-only", ephemeral=True)
            key = thread_to_key().get(interaction.channel_id)
            if not key:
                return await interaction.response.send_message(
                    "run this inside a session's thread (or `/globalmodel` for every claude)", ephemeral=True)
            await interaction.response.defer(thinking=True)
            await bridge.set_model(key, state[key], interaction.user, followup(interaction), name)
        model_cmd.autocomplete("name")(model_autocomplete)

        @tree.command(name="globalmodel", description="Switch EVERY live claude — and the default for new "
                                                      "ones — to a model (owner only)")
        @discord.app_commands.default_permissions(administrator=True)
        @discord.app_commands.describe(name="fable, opus, sonnet, haiku, or a full id like claude-opus-5-5")
        async def globalmodel_cmd(interaction: discord.Interaction, name: str):
            if not bridge.privileged(interaction.user):
                return await interaction.response.send_message("⛔ `/globalmodel` is owner-only",
                                                               ephemeral=True)
            await interaction.response.defer(thinking=True)
            await bridge.global_model(interaction.user, followup(interaction), name)
        globalmodel_cmd.autocomplete("name")(model_autocomplete)

        @tree.command(name="fast", description="Fast mode on or off for this session, or everywhere (owner only)")
        @discord.app_commands.default_permissions(administrator=True)
        @discord.app_commands.describe(mode="on or off",
                                       everywhere="every live session, and the default for new ones")
        @discord.app_commands.choices(mode=[Choice(name="on", value="on"), Choice(name="off", value="off")])
        async def fast_cmd(interaction: discord.Interaction, mode: str = "on", everywhere: bool = False):
            if not bridge.privileged(interaction.user):
                return await interaction.response.send_message(
                    "⛔ `/fast` is owner-only (fast mode draws from usage credits)", ephemeral=True)
            key = thread_to_key().get(interaction.channel_id)
            await interaction.response.defer(thinking=True)
            if everywhere or not key:
                await bridge.global_fast(interaction.user, followup(interaction), mode)
            else:
                await bridge.set_fast(key, state[key], interaction.user, followup(interaction), mode)

        @tree.command(name="effort", description="Set this claude's reasoning effort "
                                                 "(low/medium/high/xhigh/max)")
        @discord.app_commands.describe(level="reasoning effort for this session")
        @discord.app_commands.choices(level=[discord.app_commands.Choice(name=x, value=x)
                                             for x in ("low", "medium", "high", "xhigh", "max")])
        async def effort_cmd(interaction: discord.Interaction, level: str):
            key = thread_to_key().get(interaction.channel_id)
            if not key:
                return await interaction.response.send_message(
                    "run this inside a session's thread", ephemeral=True)
            await interaction.response.defer(thinking=True)
            await bridge.set_effort(key, state[key], followup(interaction), level)
        @tree.command(name="rename", description="Rename this thread's session (drives Claude Code's "
                                                 "/rename; the thread title follows)")
        @discord.app_commands.describe(name="the new session name")
        async def rename_cmd(interaction: discord.Interaction, name: str):
            key = thread_to_key().get(interaction.channel_id)
            if not key:
                return await interaction.response.send_message(
                    "run this inside a session's thread", ephemeral=True)
            await interaction.response.defer(thinking=True)
            await bridge.rename_session(key, state[key], followup(interaction), name)
        @tree.command(name="refresh", description="Unstick this thread's claude: Esc first, in-place "
                                                  "restart (history kept) only if still frozen")
        @discord.app_commands.describe(message="optional message to send once it's unstuck",
                                       force="restart even if it looks idle")
        async def refresh_cmd(interaction: discord.Interaction, message: str = "", force: bool = False):
            key = thread_to_key().get(interaction.channel_id)
            if not key:
                return await interaction.response.send_message(
                    "run this inside a session's thread", ephemeral=True)
            await interaction.response.defer(thinking=True)
            await bridge.refresh_session(key, state[key], interaction.channel, followup(interaction),
                                         message=message, force=force)
        @tree.command(name="astra", description="Launch a GPT-6 Astra (codex) session here and open "
                                                "its thread")
        @discord.app_commands.describe(prompt="what it should do (first word may name a project dir)",
                                       project="project dir under PROJECT_ROOT (optional)")
        async def astra_cmd(interaction: discord.Interaction, prompt: str, project: str = ""):
            if SPAWN_ALLOW_USERS and interaction.user.id not in SPAWN_ALLOW_USERS:
                return await interaction.response.send_message(
                    "⛔ you're not on the spawn allowlist (SPAWN_ALLOW_USERS)", ephemeral=True)
            await interaction.response.defer(thinking=True)
            words = (f"{project} {prompt}" if project else prompt).split()
            cwd, rest = resolve_project(words)
            ch = interaction.channel
            parent = ch.parent if isinstance(ch, discord.Thread) else ch
            await bridge.astra_start(parent or bridge.main_channel, interaction.user, cwd,
                                     " ".join(rest).strip() or prompt, followup(interaction))
        @tree.command(name="claude", description="Launch a new claude here and open its thread")
        @discord.app_commands.describe(prompt="what it should do (first word may name a project dir)",
                                       project="project dir under PROJECT_ROOT (optional)")
        async def claude_cmd(interaction: discord.Interaction, prompt: str, project: str = ""):
            if SPAWN_ALLOW_USERS and interaction.user.id not in SPAWN_ALLOW_USERS:
                return await interaction.response.send_message(
                    "⛔ you're not on the spawn allowlist (SPAWN_ALLOW_USERS)", ephemeral=True)
            await interaction.response.defer(thinking=True)
            respond = followup(interaction)
            words = (f"{project} {prompt}" if project else prompt).split()
            cwd, rest = resolve_project(words)
            text = " ".join(rest).strip() or prompt
            pane, err = await asyncio.to_thread(spawn_claude, slug(text, 32), cwd)
            if not pane:
                return await respond(f"❌ couldn't launch: `{err}`")
            session, notes = await self.await_registration(pane)
            if not session:
                return await respond(await bridge.spawn_failure_text(pane, cwd, notes))
            parent = (interaction.channel.parent if isinstance(interaction.channel, discord.Thread)
                      else interaction.channel) or bridge.main_channel
            thread = await bridge.adopt_session(session, parent, {"spawned": True})
            ok, serr = await asyncio.to_thread(bridge.deliver, session, interaction.user, text)
            print(f"/claude by {interaction.user} ({interaction.user.id}) -> {cwd} pane {pane} "
                  f"key {session['key']} ok={ok} gates={notes}")
            await respond(f"🚀 launched **{session['name']}** in `{cwd}`"
                          + (f" → <#{thread.id}>" if thread else " (thread opens shortly)")
                          + ("" if ok else f" · ⚠️ prompt not delivered: {serr}")
                          + ("".join(f"\n-# 🛠 {n}" for n in notes) if notes else ""))

        @tree.command(name="feldspar", description="Let Feldspar look into this: two frontier "
                                                   "reviewers hunt for bugs, flaws and methodological errors")
        @discord.app_commands.describe(
            focus="what to look at (in a session thread: optional; elsewhere: project dir first)")
        async def feldspar_cmd(interaction: discord.Interaction, focus: str = ""):
            target, focus2, err = await bridge.feldspar_target(interaction.channel, focus)
            if err:
                return await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            await interaction.response.defer(thinking=True)
            await bridge.feldspar(interaction.channel, interaction.user, target, focus2,
                                  followup(interaction))

        @tree.command(name="sessions", description="Every live claude as a clickable thread link")
        async def sessions_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            respond = followup(interaction)
            for chunk in split_chunks(await bridge.sessions_text()):
                await respond(chunk)

    async def feldspar_target(self, channel, focus):
        """Where 'this' points: the session behind a thread, or a named project dir.
        Returns (target|None, remaining_focus, error|None)."""
        key = thread_to_key().get(channel.id) if isinstance(channel, discord.Thread) else None
        if key:
            st = state[key]
            s = await asyncio.to_thread(find_live_by_key, key)
            sid = (s or {}).get("sid") or st.get("sid")
            tp = (s or {}).get("transcript")
            if not tp and sid:
                hit = await asyncio.to_thread(checkin.find_transcript_anywhere, sid)
                tp = str(hit) if hit else None
            cwd = (s or {}).get("cwd") or st.get("cwd")
            if not cwd and sid:
                cwd = await asyncio.to_thread(transcript_cwd, sid)
            if not cwd or not Path(cwd).is_dir():
                return None, focus, f"this session's working directory is unknown or gone (`{cwd}`)"
            return ({"name": (s or {}).get("name") or st.get("name") or "", "cwd": cwd,
                     "sid": sid, "transcript": tp}, focus, None)
        words = focus.split()
        cwd, rest = resolve_project(words)
        if not words or (cwd == PROJECT_ROOT and rest == words):
            return (None, focus, "outside a session thread, name the project first: "
                                 f"`!feldspar <dir> [focus]` (dirs under `{PROJECT_ROOT}`)")
        return {"name": cwd.name, "cwd": str(cwd), "sid": None, "transcript": None}, " ".join(rest), None

    async def feldspar(self, channel, user, target, focus, respond):
        """'let feldspar look into this': two independent reviewers on one target — the
        latest Claude at max effort as a real session/thread (it fans out to many
        subagents) and the latest OpenAI model headless via codex. Both write into one
        report folder; the Claude reviewer merges and posts the link."""
        focus = focus.strip() or FELDSPAR_DEFAULT_FOCUS
        stamp = time.strftime("%Y%m%d-%H%M")
        folder = REPORTS_DIR / f"feldspar-{slug(target.get('name') or Path(target['cwd']).name, 24)}-{stamp}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            brief = await asyncio.to_thread(feldspar_brief, target, focus,
                                            getattr(user, "display_name", str(user)))
            (folder / "brief.md").write_text(brief)
        except OSError as e:
            return await respond(f"❌ couldn't create the report folder: `{e}`")
        await respond(f"🧭 **Feldspar is on it** — target `{target['cwd']}`"
                      + (f" (session **{target['name']}**)" if target.get("sid") else "")
                      + f" · focus: {focus}\n"
                      f"-# Claude `{FELDSPAR_CLAUDE_MODEL}` @ `{FELDSPAR_CLAUDE_EFFORT}` in its own thread "
                      f"(many subagents) · OpenAI `{FELDSPAR_OPENAI_MODEL}` @ `{FELDSPAR_OPENAI_EFFORT}` "
                      f"headless via codex · report folder `{folder}`")
        asyncio.create_task(self.run_codex_review(folder, target, focus, channel))
        name = f"feldspar-{slug(target.get('name') or Path(target['cwd']).name, 20)}"
        extra = f"--model {shlex.quote(FELDSPAR_CLAUDE_MODEL)} --effort {shlex.quote(FELDSPAR_CLAUDE_EFFORT)}"
        pane, err = await asyncio.to_thread(spawn_claude, name, target["cwd"], None, False, extra,
                                            {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"})
        if not pane:
            return await respond(f"❌ couldn't launch the Claude reviewer: `{err}`")
        session, notes = await self.await_registration(pane)
        if not session:
            return await respond(await self.spawn_failure_text(pane, target["cwd"], notes))
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        thread = await self.adopt_session(session, parent or self.main_channel,
                                          {"spawned": True, "feldspar": str(folder)})
        ok, serr = await asyncio.to_thread(self.deliver, session, user,
                                           feldspar_claude_prompt(folder, target, focus))
        await respond(f"🔭 Claude reviewer is exploring" + (f" → <#{thread.id}>" if thread else "")
                      + ("" if ok else f" (⚠️ brief not delivered: {serr})"))
        print(f"feldspar by {user}: {target['cwd']} focus={focus[:60]!r} folder={folder} "
              f"claude={session['key']}")

    @staticmethod
    def codex_cmd(folder, target, focus, sandbox, out):
        return [CODEX_BIN, "exec", "-C", target["cwd"], "--skip-git-repo-check", "-s", sandbox,
                "-m", FELDSPAR_OPENAI_MODEL, "-c", f'model_reasoning_effort="{FELDSPAR_OPENAI_EFFORT}"',
                "-o", str(out), feldspar_codex_prompt(folder, target, focus)]

    @staticmethod
    async def codex_run(cmd, log):
        """One `codex exec` to completion, stdout+stderr into `log`. Returns (rc, minutes);
        rc -9 when FELDSPAR_TIMEOUT ran out."""
        t0 = time.time()
        with open(log, "wb") as lf:
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=codex_env(), stdout=lf, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL)
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=FELDSPAR_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                rc = -9
        return rc, (time.time() - t0) / 60

    async def run_codex_review(self, folder, target, focus, channel):
        """The OpenAI half of an expedition: codex headless in a read-only sandbox. If the
        sandbox itself cannot start (the bubblewrap/userns problem), retry unsandboxed so a
        review actually happens; when the report lands, hand it to the Claude reviewer."""
        out, log = folder / "openai-review.md", folder / "openai-review.log"
        sandbox = FELDSPAR_CODEX_SANDBOX
        if sandbox != "danger-full-access" and FELDSPAR_CODEX_UNSANDBOXED_FALLBACK \
                and await asyncio.to_thread(codex_sandbox_usable) is False:
            # known in advance: don't burn 10 minutes on a doomed sandboxed attempt
            await self.say(channel, "⚠️ codex's sandbox can't start on this box (bubblewrap has no "
                                    "rights to user namespaces — README → Feldspar sandbox); running "
                                    "the OpenAI reviewer **unsandboxed** (it is still told read-only).")
            sandbox = "danger-full-access"
        try:
            rc, mins = await self.codex_run(self.codex_cmd(folder, target, focus, sandbox, out), log)
            text = out.read_text() if out.exists() else ""
            if sandbox != "danger-full-access" and codex_sandbox_broken(log, text):
                note = (f"⚠️ codex's `{sandbox}` sandbox can't start on this box (bubblewrap has no "
                        "rights to user namespaces — see README → Feldspar sandbox). ")
                if not FELDSPAR_CODEX_UNSANDBOXED_FALLBACK:
                    return await self.say(channel, note + "The OpenAI review did not happen. Fix the "
                                          "sandbox, or set `FELDSPAR_CODEX_UNSANDBOXED_FALLBACK=1`.")
                await self.say(channel, note + f"Re-running the OpenAI reviewer **unsandboxed** "
                                               f"(after {mins:.0f} min; it is still told read-only).")
                sandbox, log = "danger-full-access", folder / "openai-review.unsandboxed.log"
                out.unlink(missing_ok=True)
                rc, mins2 = await self.codex_run(self.codex_cmd(folder, target, focus, sandbox, out), log)
                mins += mins2
                text = out.read_text() if out.exists() else ""
        except OSError as e:
            return await self.say(channel, f"❌ codex couldn't start: `{e}`")
        label = f"`{FELDSPAR_OPENAI_MODEL}` @ `{FELDSPAR_OPENAI_EFFORT}`"
        if rc != 0 or not text.strip():
            tail = log.read_text(errors="replace")[-600:] if log.exists() else ""
            return await self.say(channel, f"⚠️ OpenAI reviewer ({label}) ended with rc={rc} after "
                                           f"{mins:.0f} min and no report. Log tail:\n```\n{tail}\n```")
        handed = await self.hand_to_claude_reviewer(folder, out)
        head = text.strip()[:1500]
        await self.say(channel, f"🛰️ **OpenAI reviewer done** ({label}, {mins:.0f} min"
                                + (", unsandboxed" if sandbox == "danger-full-access" else "")
                                + f") — full report `{out}`; {handed}\n{head}"
                                + ("\n-# …truncated" if len(text) > 1500 else ""))

    async def hand_to_claude_reviewer(self, folder, out):
        """Tell this expedition's Claude reviewer that the OpenAI report has landed, so it folds
        the findings in (and republishes if it had already finished). Returns a short status
        for the Discord message."""
        key = next((k for k, st in state.items() if st.get("feldspar") == str(folder)), None)
        s = await asyncio.to_thread(find_live_by_key, key) if key else None
        if not s:
            return "the Claude reviewer is gone, so this is **not merged** — read it next to `report.md`."
        msg = (f"The OpenAI reviewer ({FELDSPAR_OPENAI_MODEL} via codex) has finished: read {out}. "
               "Verify each of its findings against the code, then fold them into "
               f"{folder}/report.md and {folder}/report.html under its attribution, marked confirmed / "
               "refuted / new, and say where you disagree. If you already published, update both files "
               "in place. Then re-post the report link here.")
        ok, err = await asyncio.to_thread(self.deliver, s, _Owner(self.owner), msg)
        return ("handed to the Claude reviewer to fold in." if ok
                else f"couldn't hand it to the Claude reviewer ({err}) — **not merged**.")

    # ---------- Astra: GPT-6 sessions as threads ----------

    @staticmethod
    def astra_entries():
        return state.setdefault("_astra", {})

    async def astra_start(self, parent, user, cwd, prompt, respond, title=None, show=None, extra=None):
        """`!astra [dir] <prompt>` / `/astra`: open a thread bound to a brand-new codex thread
        (GPT-6 Astra) and run the first turn in it. `title`/`show` override the thread name
        and the quoted first line (handoffs quote the source, not the long briefing)."""
        prompt = (prompt or "").strip()
        if not prompt:
            return await respond("`!astra [dir] <prompt>` — what should Astra do?")
        if parent is None:
            return await respond("❌ no channel to open the thread in")
        sandbox = ASTRA_SANDBOX
        degraded = False
        if sandbox != "danger-full-access" and await asyncio.to_thread(codex_sandbox_usable) is False:
            sandbox, degraded = "danger-full-access", True
        title = (title or f"{ASTRA_PREFIX} astra · {slug(prompt, 40)}")[:100]
        try:
            thread = await parent.create_thread(name=title, type=discord.ChannelType.public_thread,
                                                auto_archive_duration=10080)
        except discord.HTTPException as e:
            return await respond(f"❌ couldn't open a thread: `{e}`")
        who = getattr(user, "display_name", str(user))
        self.astra_entries()[str(thread.id)] = {
            "codex_thread": None, "cwd": str(cwd), "sandbox": sandbox, "effort": ASTRA_EFFORT,
            "name": slug(prompt, 40), "created": time.time(), "turns": 0, "ended": False, "by": who,
            **(extra or {})}
        save_state()
        await self.say(thread, f"🛰️ **Astra** — `{ASTRA_MODEL}` @ `{ASTRA_EFFORT}` in `{cwd}` · sandbox "
                               f"`{sandbox}`"
                               + (" (bubblewrap can't start here — see README → Feldspar sandbox)"
                                  if degraded else "")
                               + f"\n-# reply here to talk to it · `!effort {'|'.join(ASTRA_EFFORTS)}` · "
                               f"`!kill` ends it · started by {who}\n"
                               + (show if show else f"> {one_line(prompt, 300)}"))
        await respond(f"🛰️ Astra is on it → <#{thread.id}>")
        asyncio.create_task(self.astra_turn(thread, prompt, user))

    async def fork_to_astra(self, channel, user, key, st, prompt, respond):
        """`!fork astra [message]` in a claude's thread: hand the conversation to a new GPT-6
        Astra session. Codex can't load a Claude transcript, so it is rendered to a handoff
        file that Astra reads first; the original claude keeps running."""
        live = await asyncio.to_thread(find_live_by_key, key)
        sid = (live or {}).get("sid") or st.get("sid")
        tp = (live or {}).get("transcript")
        if not tp and sid:
            hit = await asyncio.to_thread(checkin.find_transcript_anywhere, sid)
            tp = str(hit) if hit else None
        if not tp or not Path(tp).exists():
            return await respond("-# can't hand off — no transcript found for this session")
        cwd = ((live or {}).get("cwd") or st.get("cwd")
               or (await asyncio.to_thread(transcript_cwd, sid) if sid else None))
        if not cwd or not Path(cwd).is_dir():
            return await respond(f"-# can't hand off — working directory unknown or gone (`{cwd}`)")
        name = (live or {}).get("name") or st.get("name") or "claude"
        ASTRA_LOG_DIR.mkdir(parents=True, exist_ok=True)
        handoff = ASTRA_LOG_DIR / f"handoff-{slug(name, 24)}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        text = await asyncio.to_thread(render_handoff, name, sid or "?", cwd, tp)
        try:
            handoff.write_text(text)
        except OSError as e:
            return await respond(f"❌ couldn't write the handoff file: `{e}`")
        first = (f"You are taking over from a Claude Code session named {name}, working in {cwd}; the "
                 f"human forked it to you and the original claude keeps running. Read {handoff} in full "
                 f"first — it is the whole conversation so far ({len(text) // 1000}k chars, oldest first): "
                 "what was asked, tried, found and claimed. Don't redo finished work; verify anything "
                 "you rely on against the actual files. "
                 + (f"Then: {prompt}" if prompt else
                    "Then say in a few lines where things stand and what you would do next, and wait "
                    "for the human."))
        parent = (channel.parent if isinstance(channel, discord.Thread) else channel) or self.main_channel
        await self.astra_start(parent, user, Path(cwd), first, respond,
                               title=f"{ASTRA_PREFIX} astra ⑂ {name}",
                               show=(f"⑂ forked from <#{channel.id}> (**{name}**) · handoff `{handoff}` "
                                     f"({len(text) // 1000}k chars)"
                                     + (f"\n> {one_line(prompt, 300)}" if prompt else "")),
                               extra={"forked_from": key, "handoff": str(handoff)})

    async def handle_astra_message(self, msg, ent):
        content = (msg.content or "").strip()
        low = content.lower()
        if low == "!help":
            return await self.send_help(msg.channel)
        if low.startswith("!kill"):
            return await self.astra_kill(msg.channel, ent, delete=low.endswith("delete"))
        if low.startswith("!effort"):
            eff = content[7:].strip().lower()
            if eff not in ASTRA_EFFORTS:
                return await self.say(msg.channel, f"-# efforts: {', '.join(ASTRA_EFFORTS)} "
                                                   f"(now `{ent.get('effort', ASTRA_EFFORT)}`)")
            ent["effort"] = eff
            save_state()
            return await msg.add_reaction("✅")
        if low.startswith("!"):
            return await self.say(msg.channel, "-# Astra threads take plain messages, `!effort <level>`, "
                                               "`!kill`, `!help`")
        if ent.get("ended"):
            return await self.say(msg.channel, "-# 🌌 this Astra session was ended")
        attached = await self.save_attachments(msg)
        payload = f"{content}\n{attached}".strip() if attached else content
        if payload:
            await self.astra_turn(msg.channel, payload, msg.author, react_msg=msg)

    async def astra_turn(self, thread, text, user, react_msg=None):
        """One turn, serialised per thread: messages that land mid-turn are queued and sent
        together when it ends."""
        tid = str(thread.id)
        ent = self.astra_entries().get(tid)
        if not ent or ent.get("ended"):
            return
        if tid in self._astra_busy:
            self._astra_queue.setdefault(tid, []).append(text)
            if react_msg:
                await react_msg.add_reaction("⏳")
            return
        self._astra_busy.add(tid)
        try:
            await self._astra_run(thread, ent, text, user, react_msg)
            while (queued := self._astra_queue.pop(tid, None)) and not ent.get("ended"):
                await self._astra_run(thread, ent, "\n\n".join(queued), user, None)
        finally:
            self._astra_busy.discard(tid)

    ASTRA_TRANSIENT = ("requires a newer version", "failed to refresh available models",
                       "timeout waiting for child process")

    async def _astra_run(self, thread, ent, text, user, react_msg):
        """One turn. Codex's background model-catalog refresh is flaky (it times out and can
        briefly corrupt ~/.codex/models_cache.json); when that races a turn, the server
        rejects gpt-6-astra with a misleading 'requires a newer version of Codex'. That is
        transient — a fresh launch re-reads a good cache — so retry a couple of times."""
        tid = str(thread.id)
        who = getattr(user, "display_name", None)
        prompt = text if (user is None or getattr(user, "id", None) == self.owner or not who) \
            else f"{who}: {text}"
        ASTRA_LOG_DIR.mkdir(parents=True, exist_ok=True)
        errlog, evlog = ASTRA_LOG_DIR / f"{tid}.stderr.log", ASTRA_LOG_DIR / f"{tid}.events.jsonl"
        card = await self.say(thread, "\U0001f6f0\ufe0f **Astra is working** \u00b7 \u23f1 0 min")
        t0 = time.time()
        had_thread = bool(ent.get("codex_thread"))   # first turn creates the codex thread

        async def attempt():
            cmd = [CODEX_BIN, "exec"]
            cmd += ["resume", ent["codex_thread"]] if ent.get("codex_thread") else ["-C", ent["cwd"]]
            cmd += ["--json", "--skip-git-repo-check", "-m", ASTRA_MODEL,
                    "-c", f'model_reasoning_effort="{ent.get("effort", ASTRA_EFFORT)}"',
                    "-c", f'sandbox_mode="{ent["sandbox"]}"',
                    "-c", "sandbox_workspace_write.network_access=true", prompt]
            acts, seen, errors = deque(maxlen=5), set(), []
            final, usage, last_edit = None, None, 0.0

            async def show(done=False, rc=None):
                if card is None:
                    return
                mins = (time.time() - t0) / 60
                head = ("\U0001f6f0\ufe0f **Astra is working**" if not done else
                        "\U0001f6f0\ufe0f **Astra turn done**" if rc == 0 else f"\u26a0\ufe0f **Astra turn ended (rc={rc})**")
                tok = (f" \u00b7 {usage.get('input_tokens', 0) // 1000}k in / {usage.get('output_tokens', 0) // 1000}k out"
                       if usage else "")
                body = "\n".join([f"{head} \u00b7 \u23f1 {mins:.0f} min{tok}"] + [f"-# {a}" for a in acts])[:1900]
                try:
                    await card.edit(content=body, suppress=True, allowed_mentions=NO_PING)
                except discord.HTTPException as e:
                    log_error("astra card", e)

            def note(ev):
                nonlocal final, usage
                typ = ev.get("type", "")
                if typ == "thread.started" and ev.get("thread_id"):
                    if ent.get("codex_thread") != ev["thread_id"]:
                        ent["codex_thread"] = ev["thread_id"]
                        save_state()
                elif typ in ("item.started", "item.completed", "item.updated"):
                    it = ev.get("item") or {}
                    kind, iid = it.get("type"), it.get("id")
                    if kind == "agent_message":
                        if typ == "item.completed" and it.get("text"):
                            final = it["text"]
                            acts.append("\U0001f4ac " + one_line(final))
                    elif kind == "command_execution":
                        if iid not in seen:
                            seen.add(iid)
                            acts.append("$ " + one_line(it.get("command")))
                        if typ == "item.completed" and it.get("exit_code") not in (None, 0):
                            acts.append(f"\u21b3 exit {it.get('exit_code')}: " + one_line(it.get("aggregated_output"), 90))
                    elif kind == "reasoning" and typ == "item.completed":
                        acts.append("\U0001f9e0 " + one_line(it.get("text") or it.get("summary")))
                    elif kind == "file_change" and typ == "item.completed":
                        names = ", ".join(Path(str(c.get("path", "?"))).name for c in (it.get("changes") or [])[:4])
                        acts.append(f"\u270f\ufe0f {names or 'files changed'}")
                    elif kind == "web_search":
                        if iid not in seen:
                            seen.add(iid)
                            acts.append("\U0001f50e " + one_line(it.get("query"), 100))
                    elif kind == "error":
                        errors.append(one_line(it.get("message"), 300))
                elif typ == "turn.completed":
                    usage = ev.get("usage")
                elif typ in ("turn.failed", "error"):
                    errors.append(one_line(json.dumps(ev.get("error") or ev), 300))

            try:
                with open(errlog, "ab") as ef:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd, cwd=ent["cwd"], env=codex_env(), stdout=asyncio.subprocess.PIPE,
                        stderr=ef, stdin=asyncio.subprocess.DEVNULL)
            except OSError as e:
                return None, None, [f"codex couldn't start: {e}"], None
            self._astra_procs[tid] = proc

            async def pump():
                nonlocal last_edit
                with open(evlog, "ab") as lf:
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        lf.write(line)
                        try:
                            note(json.loads(line))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if time.time() - last_edit > 4:
                            last_edit = time.time()
                            await show()

            try:
                await asyncio.wait_for(pump(), timeout=ASTRA_TURN_TIMEOUT)
                rc = await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                rc = -9
                errors.append(f"turn hit the {ASTRA_TURN_TIMEOUT // 60} min wall clock")
            finally:
                self._astra_procs.pop(tid, None)
            await show(done=True, rc=rc)
            return rc, final, errors, usage

        if react_msg:
            await react_msg.add_reaction("\U0001f6f0\ufe0f")
        rc, final, errors, usage = None, None, [], None
        for att in range(ASTRA_RETRIES + 1):
            rc, final, errors, usage = await attempt()
            transient = (rc != 0 and not final
                         and any(any(p in e for p in self.ASTRA_TRANSIENT) for e in errors))
            if not transient or att == ASTRA_RETRIES:
                break
            if not had_thread:                 # discard the empty thread the failed attempt made
                ent["codex_thread"] = None
                save_state()
            await self.say(thread, f"-# \u21bb transient codex model-cache hiccup, retrying "
                                   f"({att + 1}/{ASTRA_RETRIES})\u2026")
            await asyncio.sleep(4)

        ent["turns"] = ent.get("turns", 0) + 1
        save_state()
        if final:
            await self.say(thread, final)
        if errors:
            await self.say(thread, "\u26a0\ufe0f " + "\n\u26a0\ufe0f ".join(errors[-3:]))
        if rc != 0 and not final:
            tail = errlog.read_text(errors="replace")[-700:] if errlog.exists() else ""
            await self.say(thread, f"\u26a0\ufe0f codex ended with rc={rc} and no answer. stderr tail:\n```\n{tail}\n```")
        if react_msg:
            await react_msg.add_reaction("\u2705" if rc == 0 and final else "\u274c")

    async def astra_kill(self, thread, ent, delete=False):
        proc = self._astra_procs.get(str(thread.id))
        if proc and proc.returncode is None:
            proc.kill()
        ent["ended"] = True
        self._astra_queue.pop(str(thread.id), None)
        save_state()
        try:
            if delete:
                await thread.delete()
            else:
                await thread.send("-# 🌌 Astra session ended", allowed_mentions=NO_PING)
                name = thread.name[len(ASTRA_PREFIX):].strip() if thread.name.startswith(ASTRA_PREFIX) else thread.name
                await thread.edit(name=f"{ENDED_PREFIX} {name}"[:100], archived=True)
        except discord.HTTPException as e:
            log_error("astra_kill", e)

    async def adopt_session(self, session, parent, extra=None):
        """Open (or find) the thread for a session the bridge itself just launched, in
        `parent`. Claims the key first so the poller doesn't open a twin; if the poller
        won the race, its thread is reused."""
        nkey = session["key"]
        existing = state.get(nkey)
        if existing and existing.get("thread") and not existing.get("pending"):
            existing.update(extra or {})
            save_state()
            return await self.get_thread(existing["thread"])
        state[nkey] = {"pending": True}
        save_state()
        title = thread_title(session["name"], session["sid"], False)
        try:
            thread = await parent.create_thread(
                name=title, type=discord.ChannelType.public_thread, auto_archive_duration=10080)
            intro = await thread.send(status_line(session), suppress_embeds=True,
                                      allowed_mentions=NO_PING)
        except discord.HTTPException as e:
            log_error("adopt_session", e)
            state.pop(nkey, None)
            save_state()
            return None
        state[nkey] = {"thread": thread.id, "parent": parent.id, "status_msg": intro.id,
                       "size": os.path.getsize(session["transcript"]) if session["transcript"] else 0,
                       "status": session["status"], "name": session["name"], "sid": session["sid"],
                       "cwd": session["cwd"], "thread_name": title, "ended": False, "muted": False,
                       **(extra or {})}
        save_state()
        if ANNOUNCE_NEW and self.main_channel:
            await self.say(self.main_channel,
                           f"🚀 **new traveler:** **{session['name']}** · `{session['project']}` · "
                           f"tmux pane `{session['pane']}` · <#{thread.id}>")
        return thread

    async def bring_up(self, sid, user, respond):
        """A `!resume` / `/resume` pick. Already live → link its thread. Ended but still
        tracked → revive into its old thread. Otherwise resume the transcript (pulling it
        from the S3 backup first if that's the only copy) into a brand-new thread."""
        live = next((s for s in await asyncio.to_thread(live_sessions) if s["sid"] == sid), None)
        if live:
            st = state.get(live["key"])
            if st and st.get("thread"):
                ro = "" if live["pane"] else " (read-only — `!revive` there makes it steerable)"
                return await respond(f"🟢 **{live['name']}** is already live · <#{st['thread']}>{ro}")
            return await respond(f"🟢 **{live['name']}** is live; its thread opens within a few seconds")
        tracked = next(((k, v) for k, v in sessions_state().items()
                        if v.get("sid") == sid and v.get("thread")), None)
        if tracked:
            k, v = tracked
            thread = await self.get_thread(v["thread"])
            if thread:
                await respond(f"♻️ it already has a thread — reviving into <#{thread.id}>")
                res = await self.revive(k, v, thread, reporter=lambda t, th=thread: self.say(th, t))
                return await respond(f"✅ back up · <#{thread.id}>" if res
                                     else f"❌ revive failed — details in <#{thread.id}>")
        rows = {r["sid"]: r for r in await asyncio.to_thread(session_index)}
        r = rows.get(sid)
        if not r:
            return await respond(f"❌ unknown session `{sid[:8]}`")
        if r["where"] == "s3":
            size = ash_twin.human(r["size"]) if ash_twin else ""
            await respond(f"🪐 that transcript only exists in the S3 backup — restoring it first ({size})")
            try:
                await asyncio.to_thread(ash_twin.restore_transcript, sid)
            except Exception as e:  # noqa: BLE001
                return await respond(f"❌ restore failed: `{e}`")
        cwd = r["cwd"] or await asyncio.to_thread(transcript_cwd, sid)
        if not cwd or not Path(cwd).is_dir():
            return await respond(f"❌ its working directory is unknown or gone (`{cwd}`)")
        name = r["name"] or slug(r["first"] or r["label"], 32)
        await respond(f"⏳ resuming **{name}** (`{sid[:8]}`) in `{cwd}`…")
        pane, err = await asyncio.to_thread(spawn_claude, name, cwd, sid)
        if not pane:
            return await respond(f"❌ couldn't open a tmux window: `{err}`")
        session, notes = await self.await_registration(pane)
        if not session:
            return await respond(await self.spawn_failure_text(pane, cwd, notes))
        thread = await self.adopt_session(session, self.main_channel,
                                          {"spawned": True, "resumed_by": getattr(user, "id", None)})
        if not thread:
            return await respond(f"⚠️ resumed into pane `{pane}` but couldn't open a thread — "
                                 "the poller will pick it up")
        if notes:
            await self.say(thread, "\n".join(f"-# 🛠 {n}" for n in notes))
        await respond(f"✅ **{session['name']}** is back · <#{thread.id}>")

    async def handle_yolo(self, msg, content):
        arg = content[len("!yolo"):].strip() or "1h"
        if arg in ("off", "stop", "end"):
            stop_yolo()
            await self.say(msg.channel, f"-# 🔒 yolo off — back to `{current_permission_mode()}`")
            return
        secs = parse_duration(arg)
        if not secs or secs > YOLO_MAX:
            await self.say(msg.channel,
                           "`!yolo 1h` · `!yolo 30m` · `!yolo 90s` · `!yolo off` — max 12h")
            return
        await start_yolo(secs)
        await self.say(msg.channel,
                       f"-# 🎲 **bypassPermissions** until <t:{int(YOLO['until'])}:t> "
                       f"(<t:{int(YOLO['until'])}:R>). Applies to claudes started from now on; "
                       f"already-running sessions keep the mode they started with.")

    async def handle_main_command(self, msg, content):
        low = content.lower()
        if low.startswith("!all "):
            text = content[5:].strip()
            lines = []
            for s in await asyncio.to_thread(live_sessions):
                if reach(s):
                    ok, _ = await asyncio.to_thread(self.deliver, s, msg.author, text)
                    lines.append(f"{'✅' if ok else '❌'} {s['name']}" + ("" if s["pane"] else " (via socket)"))
                else:
                    lines.append(f"⏭️ {s['name']} (read-only)")
            await msg.channel.send("\n".join(lines) or "no live claudes", allowed_mentions=NO_PING)
        elif low.startswith("!globalmodel"):
            await msg.add_reaction("🧠")
            await self.global_model(msg.author, lambda t: self.say(msg.channel, t), content[12:].strip())
        elif low == "!fast" or low.startswith("!fast "):
            mode = next((w for w in low.split()[1:] if w in ("on", "off")), "on")
            await msg.add_reaction("⚡")
            await self.global_fast(msg.author, lambda t: self.say(msg.channel, t), mode)
        elif low.startswith("!astra"):
            cwd, rest = resolve_project(content[6:].split())
            await self.astra_start(msg.channel, msg.author, cwd, " ".join(rest),
                                   lambda t: self.say(msg.channel, t))
        elif low.startswith("!cleanup"):
            delete = "delete" in low
            sessions = await asyncio.to_thread(live_sessions)
            keep = {state[s["key"]]["thread"] for s in sessions
                    if s["key"] in state and state[s["key"]].get("thread")}
            pool = [t for t in await msg.guild.active_threads()
                    if t.parent_id == msg.channel.id]
            if delete:  # archived ones are out of the way already; only purge on delete
                async for t in msg.channel.archived_threads(limit=100):
                    pool.append(t)
            stale = [t for t in pool if t.id not in keep and t.name.startswith(ALL_PREFIXES)]
            done = [t for t in stale[:40] if await self.retire_thread(t, None, delete=delete)]
            gone = {t.id for t in done}
            if delete:
                for k, v in list(sessions_state().items()):
                    if v.get("thread") in gone:
                        state.pop(k, None)
                save_state()
            extra = len(stale) - len(stale[:40])
            await msg.channel.send(
                f"{'🗑️ deleted' if delete else '📦 archived'} **{len(done)}** stale "
                f"thread(s) · kept **{len(keep)}** live one(s)"
                + (f" · **{extra}** more left, run again" if extra else "")
                + ("" if delete else " · `!cleanup delete` removes them for good"),
                allowed_mentions=NO_PING)
        elif low == "!revive all":
            await self.revive_all(msg)
        elif low.startswith("!restart all"):
            await msg.add_reaction("♻️")
            asyncio.create_task(self.restart_all(msg.channel, force="force" in low))
        elif low.startswith("!yolo"):
            await self.handle_yolo(msg, content)
        elif low.startswith("!resume"):
            query = content[7:].strip()
            await msg.add_reaction("🔭")
            rows = await asyncio.to_thread(search_sessions, query)
            if not rows:
                return await self.say(msg.channel, f"🔭 no sessions match `{query}`")
            await msg.channel.send(resume_text(query, 0, rows), view=resume_view(query, 0, rows),
                                   suppress_embeds=True, allowed_mentions=NO_PING)
        elif low == "!disk":
            if ash_twin is None:
                return await self.say(msg.channel, "❌ S3/disk tooling unavailable")
            await msg.add_reaction("🔍")
            rep = await asyncio.to_thread(ash_twin.disk_report)
            await self.say(msg.channel, ash_twin.format_report(rep))
        elif low.startswith("!offload"):
            if ash_twin is None:
                return await self.say(msg.channel, "❌ S3 tooling unavailable")
            words = content.split()
            if len(words) < 2:
                return await self.say(msg.channel,
                                      "`!offload <dir>` shows the plan · `!offload <dir> confirm` runs it")
            path, confirm = words[1], len(words) > 2 and words[2].lower() == "confirm"
            try:
                plan = await asyncio.to_thread(ash_twin.plan_offload, path)
            except Exception as e:  # noqa: BLE001
                return await self.say(msg.channel, f"❌ {e}")
            dirty = (f"\n⚠️ it is a git repo with **{plan['git_dirty']} uncommitted paths**"
                     if plan.get("git_dirty") else "")
            if not confirm:
                return await self.say(
                    msg.channel,
                    f"🪐 **plan:** copy `{plan['path']}` ({ash_twin.human(plan['bytes'])}, "
                    f"{plan['files']} files) → `s3://{ash_twin.BUCKET}/{plan['prefix']}`, verify every "
                    f"object, then delete it locally and leave `{plan['path']}{ash_twin.MARKER_SUFFIX}`."
                    f"{dirty}\nrun it: `!offload {plan['path']} confirm`")
            await msg.add_reaction("🪐")
            asyncio.create_task(self.run_offload(msg.channel, plan["path"]))
        elif low.startswith("!restore"):
            words = content.split()
            if len(words) < 3 or words[2].lower() != "confirm":
                return await self.say(msg.channel, "`!restore <dir> confirm` — downloads it back from S3")
            await msg.add_reaction("🪐")
            asyncio.create_task(self.run_restore(msg.channel, words[1]))
        elif low == "!backup":
            if ash_twin is None:
                return await self.say(msg.channel, "❌ S3 tooling unavailable")
            await msg.add_reaction("🪐")
            try:
                res = await asyncio.to_thread(ash_twin.backup, lambda t: None)
                await self.say(msg.channel, f"🪐 **Ash Twin backup** — {res['uploaded']} files uploaded "
                                            f"({ash_twin.human(res['bytes'])}), {res['failed']} failed, "
                                            f"{res['tracked']} tracked → "
                                            f"`s3://{ash_twin.BUCKET}/backup/{ash_twin.HOST}/`")
            except Exception as e:  # noqa: BLE001
                await self.say(msg.channel, f"❌ backup failed: `{e}`")
        elif low == "!s3":
            if ash_twin is None:
                return await self.say(msg.channel, "❌ S3 tooling unavailable")
            try:
                st = await asyncio.to_thread(ash_twin.status)
                await self.say(msg.channel, ash_twin.format_status(st))
            except Exception as e:  # noqa: BLE001
                await self.say(msg.channel, f"❌ `{e}`")
        elif low == "!help":
            await self.send_help(msg.channel)

    async def on_message(self, msg):
        # Never process our OWN posts: everything the bridge streams out goes through a
        # webhook, so feeding those back into panes would be an infinite loop. But DO
        # accept other bot accounts — they're real participants (e.g. ArkBot/Raziel),
        # and blanket-ignoring `author.bot` made them invisible to every claude.
        if msg.author.id == self.user.id or msg.webhook_id:
            return
        content = (msg.content or "").strip()
        # @mention in ANY non-thread channel -> summon a brand new claude there
        if self.user in msg.mentions and not isinstance(msg.channel, discord.Thread):
            await self.summon(msg, re.sub(rf"<@!?{self.user.id}>", "", content).strip())
            return
        # works in EVERY channel and inside threads — it's the way to find a thread
        if content.lower() in ("!sessions", "!status", "!threads", "!ls"):
            await self.post_sessions(msg)
            return
        if self.broadcast_channel and msg.channel.id == self.broadcast_channel.id:
            await self.broadcast(msg)
            return
        if self.chat_channel and msg.channel.id == self.chat_channel.id:
            if content:
                await asyncio.to_thread(bus_append, msg.author.display_name, content)
                await msg.add_reaction("📨")
            return
        if msg.channel.id == CHANNEL_ID:
            fm = FELDSPAR_RE.match(content)
            if fm:
                target, focus, err = await self.feldspar_target(msg.channel, fm.group("focus"))
                if err:
                    return await self.say(msg.channel, f"❌ {err}")
                await msg.add_reaction("🧭")
                await self.feldspar(msg.channel, msg.author, target, focus,
                                    lambda t: self.say(msg.channel, t))
                return
            if content.startswith("!"):
                await self.handle_main_command(msg, content)
            return
        if isinstance(msg.channel, discord.Thread):
            astra = state.get("_astra", {}).get(str(msg.channel.id))
            if astra is not None:
                await self.handle_astra_message(msg, astra)
                return
        key = thread_to_key().get(msg.channel.id)
        low = content.lower()
        if not key:
            # a thread the bot no longer tracks (session long gone, state pruned) —
            # still let you clear it out, otherwise these are unkillable
            if isinstance(msg.channel, discord.Thread) and low.startswith("!kill"):
                await self.retire_thread(msg.channel, "-# 🌌 untracked thread — archiving",
                                         delete=low.endswith("delete"))
            elif isinstance(msg.channel, discord.Thread) and low == "!help":
                await self.send_help(msg.channel)
            return
        st = state[key]
        if low == "!help":
            await self.send_help(msg.channel)
            return
        if low in ("!mute", "!unmute"):
            st["muted"] = low == "!mute"
            save_state()
            await msg.add_reaction("🔇" if st["muted"] else "🔊")
            return
        if low.startswith("!kill"):
            await self.kill_session(msg, key, st, hard=low.endswith("hard"),
                                    delete=low.endswith("delete"))
            return
        fm = FELDSPAR_RE.match(content)
        if fm:
            target, focus, err = await self.feldspar_target(msg.channel, fm.group("focus"))
            if err:
                return await self.say(msg.channel, f"❌ {err}")
            await msg.add_reaction("🧭")
            await self.feldspar(msg.channel, msg.author, target, focus,
                                lambda t: self.say(msg.channel, t))
            return
        if low.startswith("!fork"):
            arg = content[5:].strip()
            if re.match(r"(?:to\s+)?astra\b", arg, re.I):        # `!fork astra [message]`
                await msg.add_reaction("🛰️")
                await self.fork_to_astra(msg.channel, msg.author, key, st,
                                         re.sub(r"^(?:to\s+)?astra\b[:,\s]*", "", arg, flags=re.I),
                                         lambda t: self.say(msg.channel, t))
                return
            await msg.add_reaction("🌱")
            await self.fork_session(msg.channel, msg.author, key, st, arg,
                                    lambda t: self.say(msg.channel, t))
            return
        if low.startswith("!astra"):
            arg = content[6:].strip()
            if re.match(r"fork\b", arg, re.I):                    # `!astra fork [message]`
                await msg.add_reaction("🛰️")
                await self.fork_to_astra(msg.channel, msg.author, key, st,
                                         re.sub(r"^fork\b[:,\s]*", "", arg, flags=re.I),
                                         lambda t: self.say(msg.channel, t))
                return
            # inside a claude's thread: Astra gets that session's project, own thread alongside
            cwd = st.get("cwd") or str(PROJECT_ROOT)
            parent = msg.channel.parent if isinstance(msg.channel, discord.Thread) else msg.channel
            await self.astra_start(parent or self.main_channel, msg.author, Path(cwd), arg,
                                   lambda t: self.say(msg.channel, t))
            return
        if low.startswith("!revive"):
            await msg.add_reaction("⏳")
            await self.revive(key, st, msg.channel, force="force" in low, fork="fork" in low,
                              reporter=lambda t: self.say(msg.channel, t))
            return
        if low.startswith("!model"):
            await msg.add_reaction("🧠")
            await self.set_model(key, st, msg.author, lambda t: self.say(msg.channel, t), content[6:].strip())
            return
        if low.startswith("!globalmodel"):
            await msg.add_reaction("🧠")
            await self.global_model(msg.author, lambda t: self.say(msg.channel, t), content[12:].strip())
            return
        if low == "!fast" or low.startswith("!fast "):
            words = low.split()[1:]
            mode = next((w for w in words if w in ("on", "off")), "on")
            await msg.add_reaction("⚡")
            if "all" in words or "everywhere" in words:
                await self.global_fast(msg.author, lambda t: self.say(msg.channel, t), mode)
            else:
                await self.set_fast(key, st, msg.author, lambda t: self.say(msg.channel, t), mode)
            return
        if low in ("!bypass", "!auto", "!mode") or low.startswith("!mode "):
            want = low[1:] if low in ("!bypass", "!auto") else content[5:].strip()
            await msg.add_reaction("🔐")
            await self.cycle_mode(key, st, lambda t: self.say(msg.channel, t), want)
            return
        if low.startswith("!rename"):
            await msg.add_reaction("✏️")
            await self.rename_session(key, st, lambda t: self.say(msg.channel, t), content[7:].strip())
            return
        if low.startswith("!effort"):
            await msg.add_reaction("🧑‍🔬")
            await self.set_effort(key, st, lambda t: self.say(msg.channel, t), content[7:].strip())
            return
        if low.startswith("!refresh") or low.startswith("!unstick"):
            await msg.add_reaction("🔄")
            rest = content.split(None, 1)[1].strip() if len(content.split(None, 1)) > 1 else ""
            force = rest.lower().startswith("force")
            if force:
                rest = rest[5:].strip()
            await self.refresh_session(key, st, msg.channel, lambda t: self.say(msg.channel, t),
                                       message=rest, force=force)
            return
        if low.startswith("!restart"):
            await msg.add_reaction("♻️")
            await self.restart_session(key, st, msg.channel, lambda t: self.say(msg.channel, t),
                                       force="force" in low)
            return
        if low.startswith("!yolo"):
            await self.handle_yolo(msg, content)
            return
        if low.startswith("!log"):
            arg = content[4:].strip()
            limit = int(arg) if arg.isdigit() else (200 if arg == "all" else 25)
            live0 = await asyncio.to_thread(find_live_by_key, key)
            tp = (live0 or {}).get("transcript")
            if not tp and st.get("sid"):
                hit = await asyncio.to_thread(checkin.find_transcript_anywhere, st["sid"])
                tp = str(hit) if hit else None
            entries = await asyncio.to_thread(ship_log, tp, min(limit, 200))
            if not entries:
                return await self.say(msg.channel, "🪐 the ship log is empty — no transcript found")
            for chunk in split_chunks(render_ship_log((live0 or st).get("name", "?"), entries))[:4]:
                await msg.channel.send(chunk, suppress_embeds=True, allowed_mentions=NO_PING)
            return
        if low.startswith("!supernova"):
            await self.handle_supernova(msg, key, st, content[10:].strip())
            return
        s = await asyncio.to_thread(find_live_by_key, key)
        if not s:
            await msg.add_reaction("👻")
            if st.get("ended"):
                await self.say(msg.channel,
                               f"-# 🌌 this session ended ({st.get('ended_reason', 'exit')}) — "
                               "`!revive` resumes it with its history")
            return
        if low == "!screen":
            if not s["pane"]:
                await msg.channel.send(NO_PANE_HELP, allowed_mentions=NO_PING)
                return
            text = await asyncio.to_thread(screen_text, s["pane"])
            opts = parse_options(text)[0] if s["status"] == "waiting" else []
            await msg.channel.send(prompt_body(text),
                                   view=prompt_view(key, opts) if s["status"] == "waiting" else None)
            return
        if low.startswith("!key"):
            tmux_key = KEYMAP.get(content[4:].strip().lower())  # never shadow `key`
            if not tmux_key:
                await msg.channel.send(f"keys: {', '.join(sorted(set(KEYMAP)))}")
                return
            ok, err = await asyncio.to_thread(checkin.send_to_session, s, None, tmux_key)
            await msg.add_reaction("✅" if ok else "❌")
            return
        attached = await self.save_attachments(msg)
        if not content and not attached:
            return
        if not reach(s):
            await msg.add_reaction("👁️")
            await msg.channel.send(NO_PANE_HELP, allowed_mentions=NO_PING)
            return
        # attribute it: several humans and bots share these threads now, and a bare
        # paste gave the claude no way to tell who it was talking to
        payload = f"{content}\n{attached}".strip() if attached else content
        ok, err = await asyncio.to_thread(self.deliver, s, msg.author, payload)
        # 🤔 = delivered, claude is chewing on it (📨 = delivered over the inbox socket of a
        # background session). A reaction instead of a reply, so acknowledging your own
        # message never notifies you.
        await msg.add_reaction(("🤔" if s["pane"] else "📨") if ok else "❌")
        if err:
            await msg.channel.send(f"-# ⚠️ {err}", allowed_mentions=NO_PING)


def main():
    if not TOKEN or not CHANNEL_ID:
        sys.exit("DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set (see .env)")
    load_state()
    intents = discord.Intents.default()
    intents.message_content = True
    Bridge(intents=intents).run(TOKEN)


if __name__ == "__main__":
    main()
