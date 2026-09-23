"""Claude Check-In / signalscope — watch & talk to the claudes running on this box.

Mounted under /claudes (nginx proxies /claudes -> 127.0.0.1:8899, basic auth at nginx).

How it works:
  - ~/.claude/sessions/<pid>.json is Claude Code's live session registry
    (pid, sessionId, name, cwd, status: busy|idle|shell, updatedAt).
  - Each live pid maps to a tty (ps), each tty to a tmux pane -> we can
    `tmux capture-pane` to see the screen and `tmux paste-buffer` to type.
  - Transcripts live at ~/.claude/projects/<slug>/<sessionId>.jsonl.
"""
import html
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request

PREFIX = "/claudes"
HOME = Path(os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude")))
SESSIONS_DIR = HOME / "sessions"
PROJECTS_DIR = HOME / "projects"
TAIL_BYTES = 2 * 1024 * 1024   # initial transcript tail
MAX_SEND_LEN = 20000

app = Flask(__name__, static_url_path=f"{PREFIX}/static")

ALLOWED_KEYS = {"Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BTab", "Space",
                "PageUp", "PageDown", *[str(i) for i in range(1, 10)]}


# ---------- shell helpers ----------

def run(cmd, input_bytes=None, timeout=10):
    try:
        p = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except Exception as e:
        return 1, str(e)


def pid_ttys(pids):
    """{pid: 'pts/7'} for live pids."""
    if not pids:
        return {}
    rc, out = run(["ps", "-o", "pid=,tty=", "-p", ",".join(str(p) for p in pids)])
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] != "?":
            result[int(parts[0])] = parts[1]
    return result


def tmux_panes():
    """{'pts/7': {'pane_id': '%5', 'title': ..., 'window': '1.2'}}"""
    rc, out = run(["tmux", "list-panes", "-a", "-F",
                   "#{pane_tty}\t#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_title}"])
    panes = {}
    if rc != 0:
        return panes
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            tty = parts[0].removeprefix("/dev/")
            panes[tty] = {"pane_id": parts[1], "window": parts[2], "title": parts[3]}
    return panes


# ---------- session registry ----------

def path_slug(p):
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def transcript_path(cwd, session_id):
    return PROJECTS_DIR / path_slug(cwd) / f"{session_id}.jsonl"


def proc_start(pid):
    """Kernel start-time of a pid (clock ticks since boot), or None if gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def live_sessions():
    """All registered claude sessions whose pid is alive, enriched with tmux pane info."""
    entries = []
    for f in sorted(SESSIONS_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        pid = d.get("pid")
        if not pid or not Path(f"/proc/{pid}").exists():
            continue
        # the registry is keyed by pid and outlives an unclean exit; if the pid has
        # since been recycled, procStart won't match and the entry is a phantom
        ps = proc_start(pid)
        if d.get("procStart") and ps and str(d["procStart"]) != ps:
            continue
        entries.append(d)
    ttys = pid_ttys([d["pid"] for d in entries])
    panes = tmux_panes()
    by_id = {p["pane_id"]: p for p in panes.values()}
    sessions = []
    for d in entries:
        tty = ttys.get(d["pid"])
        pane = panes.get(tty) if tty else None
        if pane is None and d.get("tmux"):
            # newer Claude Code writes its own pane ("0:@10.%82") into the registry —
            # trust it when the tty route fails (e.g. claude nested under another shell)
            pane = by_id.get("%" + str(d["tmux"]).rsplit("%", 1)[-1])
        tp = transcript_path(d.get("cwd", ""), d["sessionId"])
        sessions.append({
            "pid": d["pid"],
            "sid": d["sessionId"],
            # a claude's sessionId changes under it on fork/branch/clear and is not
            # unique across processes; pid+procStart is its stable identity
            "key": f'{d["pid"]}:{d.get("procStart") or ""}',
            "name_since": d.get("nameSince", 0),
            "started_at": d.get("startedAt", 0),
            "name": d.get("name") or Path(d.get("cwd", "?")).name,
            "cwd": d.get("cwd", ""),
            "project": Path(d.get("cwd", "?")).name,
            "status": d.get("status", "unknown"),
            "updatedAt": d.get("updatedAt", 0),
            "pane": pane["pane_id"] if pane else None,
            "pane_title": pane["title"] if pane else None,
            "kind": d.get("kind", "interactive"),
            # inbox socket for cross-session messaging (peerProtocol >= 1): the way to
            # type into a session that has no tmux pane, e.g. a `--bg` agent
            "sock": d.get("messagingSocketPath") if d.get("peerProtocol") else None,
            "transcript": str(tp) if tp.exists() else None,
            "transcript_mtime": tp.stat().st_mtime if tp.exists() else 0,
        })
    sessions.sort(key=lambda s: -s["updatedAt"])
    return sessions


def phantom_keys():
    """pid:procStart keys whose registry file is still present but whose process is gone.

    A clean exit removes its ~/.claude/sessions/<pid>.json; a crash, OOM kill or
    `kill -9` leaves it behind. That leftover is the only way to tell "it finished" from
    "it died" after the fact, so the bridge uses it to decide whether to offer a revive.
    """
    out = set()
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        pid = d.get("pid")
        if not pid:
            continue
        ps = proc_start(pid)
        if ps is None or (d.get("procStart") and str(d["procStart"]) != ps):
            out.add(f'{pid}:{d.get("procStart") or ""}')
    return out


def disk_info():
    """Free space on the filesystem holding the transcripts (the dashboard shows it —
    a full disk is the #1 way sessions silently stop persisting)."""
    try:
        du = shutil.disk_usage(str(HOME))
        return {"free_gb": round(du.free / 1e9, 1), "used_pct": round(100 * du.used / du.total)}
    except OSError:
        return None


def find_session(sid):
    for s in live_sessions():
        if s["sid"] == sid or s["sid"].startswith(sid):
            return s
    return None


def find_transcript_anywhere(sid):
    hits = list(PROJECTS_DIR.glob(f"*/{sid}*.jsonl"))
    return hits[0] if hits else None


def recent_dead_sessions(days=4, live_sids=()):
    """Recently-touched transcripts with no live process (finished/crashed claudes)."""
    cutoff = time.time() - days * 86400
    out = []
    for f in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff or f.stem in live_sids:
            continue
        out.append({"sid": f.stem, "project": f.parent.name.split("-")[-1] or f.parent.name,
                    "slug": f.parent.name, "mtime": st.st_mtime, "size": st.st_size})
    out.sort(key=lambda s: -s["mtime"])
    return out[:20]


# ---------- ANSI -> HTML ----------

ANSI16 = ["#3b4252", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#d8dee9",
          "#5c6370", "#ff7a85", "#a9d489", "#ffd68a", "#74bfff", "#d894e8", "#66d0dc", "#ffffff"]


def color256(n):
    if n < 16:
        return ANSI16[n]
    if n < 232:
        n -= 16
        r, g, b = n // 36, (n % 36) // 6, n % 6
        conv = lambda v: 0 if v == 0 else 55 + v * 40
        return f"#{conv(r):02x}{conv(g):02x}{conv(b):02x}"
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


SGR_RE = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_ESC_RE = re.compile(r"\x1b[^m\x1b]*?(?:[a-lnzA-LN-Z\\^_@~]|$)|\x1b\][^\x07]*\x07")


def ansi_to_html(text):
    st = {"fg": None, "bg": None, "b": False, "d": False, "i": False, "u": False, "r": False}

    def style():
        fg, bg = st["fg"], st["bg"]
        if st["r"]:
            fg, bg = (bg or "#1b1e26"), (fg or "#c8ccd4")
        css = []
        if fg:
            css.append(f"color:{fg}")
        if bg:
            css.append(f"background:{bg}")
        if st["b"]:
            css.append("font-weight:600")
        if st["d"]:
            css.append("opacity:.55")
        if st["i"]:
            css.append("font-style:italic")
        if st["u"]:
            css.append("text-decoration:underline")
        return ";".join(css)

    out, pos = [], 0
    text = OTHER_ESC_RE.sub("", text.replace("\r\n", "\n"))
    for m in SGR_RE.finditer(text):
        chunk = text[pos:m.start()]
        if chunk:
            s = style()
            out.append(f'<span style="{s}">{html.escape(chunk)}</span>' if s else html.escape(chunk))
        pos = m.end()
        codes = [c or "0" for c in m.group(1).replace(":", ";").split(";")] or ["0"]
        j = 0
        while j < len(codes):
            c = int(codes[j]) if codes[j].isdigit() else 0
            if c == 0:
                st.update(fg=None, bg=None, b=False, d=False, i=False, u=False, r=False)
            elif c == 1:
                st["b"] = True
            elif c == 2:
                st["d"] = True
            elif c == 3:
                st["i"] = True
            elif c == 4:
                st["u"] = True
            elif c == 7:
                st["r"] = True
            elif c == 22:
                st["b"] = st["d"] = False
            elif c == 23:
                st["i"] = False
            elif c == 24:
                st["u"] = False
            elif c == 27:
                st["r"] = False
            elif 30 <= c <= 37:
                st["fg"] = ANSI16[c - 30]
            elif c == 39:
                st["fg"] = None
            elif 40 <= c <= 47:
                st["bg"] = ANSI16[c - 40]
            elif c == 49:
                st["bg"] = None
            elif 90 <= c <= 97:
                st["fg"] = ANSI16[c - 90 + 8]
            elif 100 <= c <= 107:
                st["bg"] = ANSI16[c - 100 + 8]
            elif c in (38, 48):
                key = "fg" if c == 38 else "bg"
                if j + 1 < len(codes) and codes[j + 1] == "5" and j + 2 < len(codes):
                    st[key] = color256(int(codes[j + 2]) if codes[j + 2].isdigit() else 0)
                    j += 2
                elif j + 1 < len(codes) and codes[j + 1] == "2" and j + 4 < len(codes):
                    try:
                        r, g, b = (int(codes[j + 2]), int(codes[j + 3]), int(codes[j + 4]))
                        st[key] = f"#{r:02x}{g:02x}{b:02x}"
                    except ValueError:
                        pass
                    j += 4
            j += 1
    chunk = text[pos:]
    if chunk:
        s = style()
        out.append(f'<span style="{s}">{html.escape(chunk)}</span>' if s else html.escape(chunk))
    return "".join(out)


# ---------- transcript parsing ----------

def tool_summary(blk):
    name = blk.get("name", "?")
    inp = blk.get("input") or {}
    for key in ("command", "description", "file_path", "pattern", "url", "prompt", "query", "skill"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return name, v.strip().replace("\n", " ")[:160]
    return name, json.dumps(inp)[:160]


MD_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.S)


def md_lite(text):
    """Tiny markdown: fences, inline code, bold. Everything else escaped as-is."""
    parts, pos, out = [], 0, []
    for m in MD_FENCE_RE.finditer(text):
        parts.append(("t", text[pos:m.start()]))
        parts.append(("c", m.group(1)))
        pos = m.end()
    parts.append(("t", text[pos:]))
    for kind, chunk in parts:
        if kind == "c":
            out.append(f"<pre class='code'>{html.escape(chunk.rstrip())}</pre>")
        else:
            e = html.escape(chunk)
            e = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", e)
            e = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", e)
            out.append(e.strip().replace("\n", "<br>"))
    return "".join(out)


def extract_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


CMD_RE = re.compile(r"<command-name>(.*?)</command-name>.*?<command-args>(.*?)</command-args>", re.S)


def parse_transcript(path, from_byte=0, tail=TAIL_BYTES, raw=False):
    """Returns (items, consumed_bytes). Items are dicts {kind, ...}.

    raw=True keeps plain text (for the Discord bridge); default renders HTML.
    Only whole lines are consumed — a partially-written trailing line is left
    for the next incremental read instead of being silently dropped.
    """
    fmt = (lambda t: t) if raw else md_lite
    esc = (lambda t: t) if raw else html.escape
    body = "text" if raw else "html"
    size = path.stat().st_size
    start = from_byte
    if from_byte == 0 and size > tail:
        start = size - tail
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(size - start)
    consumed = start + len(data)
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut == -1:
            return [], start
        consumed = start + cut + 1
        data = data[:cut + 1]
    lines = data.split(b"\n")
    if start > 0 and from_byte == 0:
        lines = lines[1:]  # drop partial first line on tail reads
    items, tool_names = [], {}
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("isSidechain"):
            continue
        t = r.get("type")
        ts = r.get("timestamp", "")
        if t == "assistant":
            # the model that produced this turn — surfaced so the bridge can show which model is
            # running and flag when the auto-mode classifier reroutes a turn to another model
            model = (r.get("message") or {}).get("model")
            for blk in (r.get("message") or {}).get("content") or []:
                if not isinstance(blk, dict):
                    continue
                bt = blk.get("type")
                if bt == "text" and blk.get("text", "").strip():
                    items.append({"kind": "assistant", body: fmt(blk["text"]), "ts": ts, "model": model})
                elif bt == "thinking" and blk.get("thinking", "").strip():
                    items.append({"kind": "thinking",
                                  body: esc(blk["thinking"][:3000]), "ts": ts, "model": model})
                elif bt == "tool_use":
                    tool_names[blk.get("id", "")] = blk.get("name", "?")
                    name, summ = tool_summary(blk)
                    # the tool's own description is what Claude Code's spinner shows as the
                    # headline ("Adding log-spaced saves…"), with the command beneath it
                    desc = (blk.get("input") or {}).get("description")
                    items.append({"kind": "tool", "name": name, body: esc(summ),
                                  "desc": esc(desc.strip()[:200]) if isinstance(desc, str) else "",
                                  "ts": ts, "model": model})
        elif t == "user":
            msg = (r.get("message") or {})
            c = msg.get("content")
            human = (r.get("origin") or {}).get("kind") == "human" or r.get("promptSource") == "typed"
            if isinstance(c, str):
                cm = CMD_RE.search(c)
                if cm:
                    items.append({"kind": "cmd", body: esc(
                        f"{cm.group(1)} {cm.group(2)}".strip()), "ts": ts})
                elif human and not r.get("isMeta") and not c.startswith("<") \
                        and not c.startswith("[Request interrupted"):
                    items.append({"kind": "user", body: fmt(c), "ts": ts})
            elif isinstance(c, list):
                for blk in c:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text" and human and not r.get("isMeta"):
                        tx = blk.get("text", "")
                        if tx.strip() and not tx.startswith("<") \
                                and not tx.startswith("[Request interrupted"):
                            items.append({"kind": "user", body: fmt(tx), "ts": ts})
                    elif blk.get("type") == "tool_result":
                        tx = extract_result_text(blk.get("content", "")).strip()
                        if tx:
                            items.append({"kind": "result",
                                          "name": tool_names.get(blk.get("tool_use_id", ""), ""),
                                          "error": bool(blk.get("is_error")),
                                          body: esc(tx[:2500]), "ts": ts})
        elif t == "system" and r.get("subtype") == "local_command":
            cm = CMD_RE.search(r.get("content", ""))
            if cm:
                items.append({"kind": "cmd", body: esc(
                    f"{cm.group(1)} {cm.group(2)}".strip()), "ts": ts})
    return items, consumed


def last_assistant_snippet(path, nbytes=300 * 1024):
    try:
        items, _ = parse_transcript(path, tail=nbytes, raw=True)
    except OSError:
        return ""
    for it in reversed(items):
        if it["kind"] == "assistant":
            return re.sub(r"\s+", " ", it["text"]).strip()[:280]
    return ""


# ---------- routes ----------

@app.route(f"{PREFIX}/")
def index():
    return render_template("index.html", prefix=PREFIX)


@app.route(f"{PREFIX}/api/sessions")
def api_sessions():
    sessions = live_sessions()
    for s in sessions:
        s["snippet"] = last_assistant_snippet(Path(s["transcript"])) if s["transcript"] else ""
    dead = recent_dead_sessions(live_sids={s["sid"] for s in sessions})
    return jsonify({"sessions": sessions, "dead": dead, "now": time.time(), "disk": disk_info()})


@app.route(f"{PREFIX}/s/<sid>")
def session_page(sid):
    s = find_session(sid)
    if s:
        return render_template("session.html", prefix=PREFIX, s=s, live=True)
    tp = find_transcript_anywhere(sid)
    if not tp:
        abort(404)
    s = {"sid": tp.stem, "name": tp.stem[:8], "project": tp.parent.name,
         "cwd": "", "status": "ended", "pane": None}
    return render_template("session.html", prefix=PREFIX, s=s, live=False)


@app.route(f"{PREFIX}/api/screen/<sid>")
def api_screen(sid):
    s = find_session(sid)
    if not s or not s["pane"]:
        return jsonify({"error": "no pane"}), 404
    rc, out = run(["tmux", "capture-pane", "-p", "-e", "-t", s["pane"]])
    if rc != 0:
        return jsonify({"error": out}), 500
    return jsonify({"html": ansi_to_html(out.rstrip("\n")), "status": s["status"],
                    "name": s["name"], "updatedAt": s["updatedAt"]})


@app.route(f"{PREFIX}/api/transcript/<sid>")
def api_transcript(sid):
    s = find_session(sid)
    tp = Path(s["transcript"]) if s and s["transcript"] else find_transcript_anywhere(sid)
    if not tp or not tp.exists():
        return jsonify({"error": "no transcript"}), 404
    after = request.args.get("after", type=int, default=0)
    size = tp.stat().st_size
    if after and size <= after:
        return jsonify({"unchanged": True, "size": size,
                        "status": s["status"] if s else "ended"})
    items, size = parse_transcript(tp, from_byte=after)
    return jsonify({"items": items[-400:], "size": size,
                    "status": s["status"] if s else "ended"})


def send_via_socket(s, text):
    """Deliver text to a session over its cross-session-messaging inbox socket.

    Frame format is the one Claude Code itself documents in its uds-messaging hint:
    an optional auth line, then `{"type":"user","message":{"role":"user","content":…}}`
    as one JSON line. The receiving session reads it between tool calls, or starts a
    new turn if idle. Sessions in bypass mode HOLD frames from unknown senders behind a
    dialog unless `crossSessionInbound` is `accept` in their settings.
    """
    import socket as _socket
    path = s.get("sock")
    if not path or not os.path.exists(path):
        return False, "session has no inbox socket"
    frame = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
    try:
        c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        c.settimeout(5)
        c.connect(path)
        c.sendall(frame.encode())
        c.close()
    except OSError as e:
        return False, f"socket: {e}"
    return True, None


def send_to_session(s, text=None, key=None, submit=True):
    """Deliver text or a control key to a live session: its tmux pane when it has one,
    otherwise its inbox socket (text only — keys need a terminal).

    Returns (ok, error). Shared by the web API and the Discord bridge.
    """
    if not s.get("pane"):
        if key:
            return False, "session has no tmux pane (keys need a terminal)"
        if s.get("sock"):
            t = (text or "").rstrip("\n")
            if not t:
                return False, "empty message"
            return send_via_socket(s, t)
        return False, "session has no tmux pane (read-only)"
    if key:
        if key not in ALLOWED_KEYS:
            return False, f"key not allowed: {key}"
        rc, out = run(["tmux", "send-keys", "-t", s["pane"], key])
        return rc == 0, (None if rc == 0 else out)
    text = (text or "").rstrip("\n")
    if not text:
        return False, "empty message"
    if len(text) > MAX_SEND_LEN:
        return False, "message too long"
    buf = f"checkin-{os.getpid()}-{time.monotonic_ns()}"
    rc, out = run(["tmux", "load-buffer", "-b", buf, "-"], input_bytes=text.encode())
    if rc != 0:
        return False, f"load-buffer: {out}"
    rc, out = run(["tmux", "paste-buffer", "-p", "-d", "-b", buf, "-t", s["pane"]])
    if rc != 0:
        return False, f"paste-buffer: {out}"
    if submit:
        time.sleep(0.35)
        rc, out = run(["tmux", "send-keys", "-t", s["pane"], "Enter"])
        if rc != 0:
            return False, f"enter: {out}"
    return True, None


@app.route(f"{PREFIX}/api/send/<sid>", methods=["POST"])
def api_send(sid):
    s = find_session(sid)
    if not s:
        return jsonify({"error": "session not found or not live"}), 404
    body = request.get_json(force=True, silent=True) or {}
    ok, err = send_to_session(s, text=body.get("text"), key=body.get("key"),
                              submit=body.get("submit", True))
    if ok:
        return jsonify({"ok": True})
    code = 400 if err in ("empty message", "message too long",
                          "session has no tmux pane (read-only)") or "not allowed" in err else 500
    return jsonify({"error": err}), code


@app.route(PREFIX)
def bare():
    return redirect(f"{PREFIX}/")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8899, debug=True)
