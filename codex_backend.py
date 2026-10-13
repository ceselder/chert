"""Astra on Codex's native app-server: GPT sessions as Discord threads, one daemon, live turns.

Every Astra thread (`/astra`, `!astra`, `/fork to: astra`) is bound to a thread in Codex's
*app-server daemon* (`codex app-server daemon start`, a background `codex app-server` listening on
a Unix socket under the bridge's private CODEX_HOME). The bridge speaks the app-server JSON-RPC
protocol over a WebSocket on that socket: `thread/start|resume`, `turn/start|steer|interrupt`,
`thread/queue/add`, and streams `item/*` / `turn/*` notifications into an activity card. Codex's
approval requests become Discord buttons. Because the daemon — not the bridge — runs the turn,
a bridge restart mid-turn loses nothing: on startup the bridge reconnects, re-attaches the
threads that had a turn in flight and reconciles what it missed from the thread history.

Ported from recursiveforte/chertu (MIT) — chert/backends/codex/{client,events,reactions,
controls}.py and chert/discord/prompts.py — reshaped into one module with a narrow seam into
discord_bot.py (Bridge.astra). The multi-host/SSH, project-store and ContextVar machinery of
that fork is deliberately not here.

Daemon: `python codex_backend.py ensure-daemon` seeds the bridge CODEX_HOME (auth symlink,
config copy, the `packages/standalone/current` link the daemon manager insists on) and starts the
daemon if it is not running; systemd/templates/chert-codex-daemon.service.in runs the same thing.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiohttp
import discord

CODEX_HOME = Path(os.environ.get("CODEX_BRIDGE_HOME") or str(Path.home() / ".codex-bridge"))
CODEX_BIN = (os.environ.get("CODEX_BIN") or shutil.which("codex")
             or str(Path.home() / ".local" / "bin" / "codex"))
SOCKET = Path(os.environ.get("CODEX_APP_SERVER_SOCKET")
              or str(CODEX_HOME / "app-server-control" / "app-server-control.sock"))
# approvals: on-request = codex asks only when it wants to step outside the sandbox / do
# something it judges risky (those become buttons); never = run everything
APPROVAL_POLICY = os.environ.get("ASTRA_APPROVAL_POLICY", "on-request")
FAST_TIER = "priority"           # model/list → serviceTiers[{id: priority, name: Fast}]
CARD_MIN_GAP = 3                 # seconds between card edits when its content changed
CARD_HEARTBEAT = 20              # seconds between elapsed-only edits
NARRATION_MAX = 160              # agent text longer than this is a reply (posted), not narration
RPC_TIMEOUT = 60
RECEIPT = {"sent": "📨", "queued": "⏳", "read": "👀", "done": "✅", "failed": "❌"}
TOOL_NAMES = {"commandExecution": "Bash", "fileChange": "Edit", "mcpToolCall": "MCP",
              "webSearch": "WebSearch", "collabAgentToolCall": "Agent", "imageView": "Read"}
APPROVAL_METHODS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                    "item/permissions/requestApproval", "item/tool/requestUserInput",
                    "tool/requestUserInput", "mcpServer/elicitation/request"}
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
NO_PING = discord.AllowedMentions.none()


class RpcError(RuntimeError):
    pass


# ---------- the daemon: seed the private CODEX_HOME, start `codex app-server daemon` ----------

def seed_home(home=None, codex_bin=None, src=None):
    """Make `home` a complete private CODEX_HOME: the ChatGPT login shared by symlink, the
    user's config copied once, and `packages/standalone/current` linked to the real standalone
    install — `codex app-server daemon start` refuses to run without that link ("managed
    standalone Codex install not found"), because the daemon launches app-server from there."""
    home = Path(home or CODEX_HOME)
    src = Path(src or (Path.home() / ".codex"))
    home.mkdir(parents=True, exist_ok=True)
    auth, link = src / "auth.json", home / "auth.json"
    if auth.exists() and not (link.is_symlink() and link.resolve() == auth.resolve()):
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(auth)
    cfg = home / "config.toml"
    if not cfg.exists() and (src / "config.toml").exists():
        shutil.copy2(src / "config.toml", cfg)
    current = home / "packages" / "standalone" / "current"
    if not (current / "codex").exists():
        release = None
        real = Path(codex_bin or CODEX_BIN)
        try:
            real = real.resolve()
        except OSError:
            pass
        # ~/.local/bin/codex -> <home>/.codex/packages/standalone/releases/<ver>/bin/codex
        if real.parent.name == "bin" and (real.parent.parent / "codex").exists():
            release = real.parent.parent
        elif (src / "packages" / "standalone" / "current" / "codex").exists():
            release = src / "packages" / "standalone" / "current"
        if release is not None:
            current.parent.mkdir(parents=True, exist_ok=True)
            if current.is_symlink() or current.exists():
                current.unlink()
            current.symlink_to(release)
    return home


def daemon_env(home=None):
    return dict(os.environ, CODEX_HOME=str(home or CODEX_HOME))


def daemon_status(home=None, codex_bin=None, timeout=20):
    """`codex app-server daemon version` → its JSON (status running/stale/…) or None."""
    try:
        r = subprocess.run([codex_bin or CODEX_BIN, "app-server", "daemon", "version"],
                           capture_output=True, text=True, timeout=timeout, env=daemon_env(home))
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return None


def socket_alive(sock=None, timeout=3):
    import socket as _s
    s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock or SOCKET))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start_daemon(home=None, codex_bin=None, sock=None, timeout=60):
    """Idempotent: seed the home, `codex app-server daemon start` (a no-op when it already
    runs). If the managed-daemon path is unavailable (not a standalone install), fall back to
    launching `codex app-server --listen unix://<sock>` detached. Returns a status dict."""
    home, codex_bin, sock = Path(home or CODEX_HOME), codex_bin or CODEX_BIN, Path(sock or SOCKET)
    seed_home(home, codex_bin)
    if socket_alive(sock):
        return {"status": "running", "how": "already", "socketPath": str(sock)}
    env = daemon_env(home)
    out = ""
    try:
        r = subprocess.run([codex_bin, "app-server", "daemon", "start"], capture_output=True,
                           text=True, timeout=timeout, env=env)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            deadline = time.time() + 20
            while time.time() < deadline and not socket_alive(sock):
                time.sleep(0.3)
            if socket_alive(sock):
                return {"status": "running", "how": "daemon start", "socketPath": str(sock),
                        "detail": out.strip()[-400:]}
    except (OSError, subprocess.TimeoutExpired) as e:
        out += f"\n{e}"
    # fallback: run app-server ourselves, detached, logging into the home
    logdir = home / "app-server-daemon"
    logdir.mkdir(parents=True, exist_ok=True)
    sock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = open(logdir / "bridge-fallback.log", "ab")
    try:
        subprocess.Popen([codex_bin, "app-server", "--listen", f"unix://{sock}"], env=env,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         cwd=str(home))
    except OSError as e:
        return {"status": "failed", "error": f"{e}; daemon start said: {out.strip()[-400:]}"}
    finally:
        log.close()
    deadline = time.time() + 20
    while time.time() < deadline and not socket_alive(sock):
        time.sleep(0.3)
    if socket_alive(sock):
        return {"status": "running", "how": "fallback app-server", "socketPath": str(sock),
                "detail": out.strip()[-400:]}
    return {"status": "failed", "error": f"socket never came up; daemon start said: {out.strip()[-400:]}"}


# ---------- the client: app-server JSON-RPC over a WebSocket on the daemon's unix socket ----------

class CodexClient:
    """One connection to the daemon. Responses resolve `call()` futures; notifications and
    server→client requests (approvals) land in `self.notifications` for the backend to drain, so
    a slow Discord never blocks an RPC reply."""

    WANTED = {"item/started", "item/completed", "turn/started", "turn/completed",
              "thread/status/changed", "thread/name/updated", "item/agentMessage/delta",
              "item/reasoning/summaryTextDelta", "turn/plan/updated", "error",
              "thread/tokenUsage/updated", "serverRequest/resolved"}

    def __init__(self, socket=None, timeout=RPC_TIMEOUT, client_name="chert"):
        self.socket, self.timeout, self.client_name = Path(socket or SOCKET), timeout, client_name
        self.http = self.ws = self.reader = None
        self.pending = {}
        self.sequence = 0
        self.notifications = asyncio.Queue(maxsize=4096)
        self.subscribed = set()
        self.active_turns = {}         # codex thread id -> in-flight turn id (from turn/started)
        self.completed_turns = set()
        self.server_requests = {}      # key -> the raw request (approvals waiting on a human)
        self.generation = 0
        self.instance = uuid.uuid4().hex[:8]
        self.connect_lock = asyncio.Lock()
        self.attach_lock = asyncio.Lock()

    @property
    def connected(self):
        return (self.ws is not None and not self.ws.closed
                and self.reader is not None and not self.reader.done())

    async def connect(self):
        async with self.connect_lock:
            if self.connected:
                return
            await self.close()
            self.http = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(self.socket)))
            self.generation += 1
            try:
                self.ws = await self.http.ws_connect("http://localhost", max_msg_size=16 * 1024 * 1024)
                self.reader = asyncio.create_task(self._read())
                await self.call("initialize", {
                    "clientInfo": {"name": self.client_name, "title": "chert Discord bridge (Astra)",
                                   "version": "1"},
                    "capabilities": {"experimentalApi": True}})
                await self.ws.send_json({"method": "initialized", "params": {}})
            except BaseException:
                await self.close()
                raise

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.ws is not None:
            await self.ws.close()
        if self.http is not None:
            await self.http.close()
        self.reader = self.ws = self.http = None
        self.subscribed.clear()
        self.active_turns.clear()
        self.server_requests.clear()
        self._fail_pending()

    def _fail_pending(self):
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("codex app-server disconnected"))

    def _queue(self, event):
        try:
            self.notifications.put_nowait(event)
        except asyncio.QueueFull:
            pass   # the consumer is far behind; reconciliation picks up what it missed

    def on_message(self, data):
        """One decoded frame from the server (split out of the read loop for tests)."""
        method = data.get("method")
        if method in ("turn/started", "turn/completed"):
            params = data.get("params") or {}
            tid, turn_id = params.get("threadId"), (params.get("turn") or {}).get("id")
            if tid and turn_id:
                if method == "turn/started":
                    self.active_turns[tid] = turn_id
                else:
                    if self.active_turns.get(tid) == turn_id:
                        self.active_turns.pop(tid, None)
                    if len(self.completed_turns) > 2048:
                        self.completed_turns.clear()
                    self.completed_turns.add(turn_id)
        if method is not None:
            if "id" in data:                                   # server -> client request
                key = f"{self.instance}:{self.generation}:{data['id']}"
                self.server_requests[key] = data
                self._queue({"method": "x/request", "requestKey": key, "rpc": method,
                             "params": data.get("params") or {}})
            elif method in self.WANTED:
                self._queue(data)
            return
        fut = self.pending.get(data.get("id"))
        if fut is not None and not fut.done():
            if "error" in data:
                err = data["error"]
                fut.set_exception(RpcError(err.get("message", str(err)) if isinstance(err, dict) else str(err)))
            else:
                fut.set_result(data.get("result", {}))

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    self.on_message(message.json())
                except (ValueError, TypeError):
                    continue
        finally:
            self._fail_pending()
            self._queue({"method": "x/disconnected", "params": {}})

    async def call(self, method, params, timeout=None):
        if self.ws is None or self.ws.closed:
            raise ConnectionError("codex app-server is not connected")
        self.sequence += 1
        rid = self.sequence
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self.ws.send_json({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout or self.timeout)
        finally:
            self.pending.pop(rid, None)

    async def answer(self, key, result):
        """Reply to a server→client request (an approval) by its key."""
        request = self.server_requests.get(key)
        if request is None or not self.connected:
            raise RpcError("that request expired (the turn moved on or the connection dropped)")
        await self.ws.send_json({"id": request["id"], "result": result})
        self.server_requests.pop(key, None)

    async def attach(self, thread_id, **overrides):
        """Load a thread into this connection (so its events reach us). Idempotent per connection."""
        async with self.attach_lock:
            if thread_id in self.subscribed:
                return None
            r = await self.call("thread/resume", {"threadId": thread_id, "excludeTurns": True, **overrides})
            self.subscribed.add(thread_id)
            return r

    async def active_turn(self, thread_id):
        if thread_id in self.active_turns:
            return self.active_turns[thread_id]
        info = await self.call("thread/read", {"threadId": thread_id, "includeTurns": False})
        if (info["thread"].get("status") or {}).get("type") != "active":
            return None
        page = await self.call("thread/turns/list", {"threadId": thread_id, "limit": 1,
                                                      "sortDirection": "desc", "itemsView": "notLoaded"})
        return next((t["id"] for t in page.get("data", []) if t.get("status") == "inProgress"), None)

    async def interrupt(self, thread_id):
        turn_id = await self.active_turn(thread_id)
        if turn_id:
            await self.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return turn_id


# ---------- helpers ----------

def _clean(text, n):
    t = " ".join(str(text or "").split()).replace("`", "'")
    return t[:n] + ("…" if len(t) > n else "")


def _ago(secs):
    secs = max(0, int(secs))
    return f"{secs}s" if secs < 90 else f"{secs // 60}m" if secs < 5400 else f"{secs // 3600}h"


def _same_text(a, b):
    a, b = " ".join((a or "").split())[:200], " ".join((b or "").split())[:200]
    return bool(a and b) and (a == b or a.startswith(b[:120]) or b.startswith(a[:120]))


def _modal_values(data):
    """Every text value in a modal submission's component tree, in order."""
    out = []
    if isinstance(data, dict):
        if isinstance(data.get("value"), str):
            out.append(data["value"])
        for v in (data.get("components"), data.get("component")):
            out.extend(_modal_values(v))
    elif isinstance(data, list):
        for v in data:
            out.extend(_modal_values(v))
    return out


def command_text(item):
    """What to show for a commandExecution item: the commands themselves, not the bash -lc wrapper."""
    actions = [a.get("command") for a in (item.get("commandActions") or []) if a.get("command")]
    if actions:
        return " && ".join(actions)
    cmd = item.get("command") or ""
    m = re.match(r"^/bin/(?:ba|z)?sh -lc ['\"](.*)['\"]$", cmd, re.S)
    return m.group(1) if m else cmd


def item_summary(item):
    kind = item.get("type")
    if kind == "commandExecution":
        return command_text(item)
    if kind == "fileChange":
        names = [Path(str(c.get("path", "?"))).name for c in (item.get("changes") or [])[:4]]
        return ", ".join(names) or "files"
    if kind == "mcpToolCall":
        return f"{item.get('server', '')}/{item.get('tool', '')}".strip("/")
    if kind == "webSearch":
        return item.get("query", "")
    if kind == "collabAgentToolCall":
        return item.get("tool") or "subagent"
    if kind == "imageView":
        return Path(str(item.get("path", ""))).name
    return item.get("text") or item.get("tool") or kind or ""


def reasoning_text(item):
    summary = item.get("summary")
    if isinstance(summary, str):
        return summary
    if isinstance(summary, list):
        return "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in summary)
    return item.get("text") or ""


def decisions_for(request):
    """(label, result, style) buttons for a server→client request. Approval buttons follow the
    request's own `availableDecisions` when it lists them, so a button never offers an answer
    the server would reject."""
    method, params = request.get("method", ""), request.get("params") or {}
    S = discord.ButtonStyle
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        catalogue = [("accept", "✅ Allow once", {"decision": "accept"}, S.success),
                     ("acceptForSession", "✅ Allow for session", {"decision": "acceptForSession"}, S.primary),
                     ("decline", "✋ Deny", {"decision": "decline"}, S.secondary),
                     ("cancel", "⏹ Cancel turn", {"decision": "cancel"}, S.danger)]
        avail = params.get("availableDecisions")
        names = {d if isinstance(d, str) else next(iter(d), "") for d in avail} if avail else None
        out = [(label, res, style) for name, label, res, style in catalogue
               if names is None or name in names]
        amendment = params.get("proposedExecpolicyAmendment")
        if amendment and (names is None or "acceptWithExecpolicyAmendment" in names) \
                and len(json.dumps(amendment)) < 300:
            out.append(("✅ Always allow this command",
                        {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": amendment}}},
                        S.secondary))
        return out
    if method == "item/permissions/requestApproval":
        perms = params.get("permissions") or {}
        return [("✅ Allow for this turn", {"permissions": perms, "scope": "turn"}, S.success),
                ("✋ Deny", {"permissions": {}, "scope": "turn"}, S.danger)]
    if method == "mcpServer/elicitation/request":
        out = []
        if params.get("mode") == "url" and str(params.get("url", "")).startswith(("https://", "http://")):
            out.append(("✅ Continue (done in browser)", {"action": "accept"}, S.success))
        out += [("✋ Decline", {"action": "decline"}, S.secondary), ("⏹ Cancel", {"action": "cancel"}, S.danger)]
        return out
    return []


def request_body(request):
    method, params = request.get("method", ""), request.get("params") or {}
    if method in ("item/tool/requestUserInput", "tool/requestUserInput"):
        qs = params.get("questions") or []
        body = "🟠 **Astra needs your input**\n" + "\n".join(f"• {q.get('question', '')}" for q in qs[:5])
        return body[:1900]
    if method == "item/permissions/requestApproval":
        return ("🟠 **Astra asks for permissions**\n```\n"
                + json.dumps(params.get("permissions") or {})[:1200] + "\n```")[:1900]
    if method == "mcpServer/elicitation/request":
        detail = params.get("message") or params.get("url") or method
        return f"🟠 **A tool Astra is using needs input**\n{str(detail)[:1400]}"[:1900]
    detail = command_text(params) if params.get("command") else (params.get("reason") or method)
    body = f"🟠 **Astra asks to run**\n```\n{str(detail)[:1200]}\n```"
    if params.get("reason") and params.get("command"):
        body += f"-# {_clean(params['reason'], 300)}\n"
    if params.get("cwd"):
        body += f"-# in `{params['cwd']}`"
    return body[:1900]


# ---------- the backend: Astra threads ↔ app-server threads ----------

class AstraBackend:
    """Owns the client, the per-thread card/receipt state in state["_astra"], the notification
    loop and the Discord controls. `bridge` is the discord Bridge, `mod` the discord_bot module
    (state, save_state, log_error, split_chunks, discord_tables, webhook_name, ASTRA_* …) — passed
    in, not imported, so this module stays importable (and testable) on its own."""

    def __init__(self, bridge, mod, client=None, socket=None):
        self.bridge, self.mod = bridge, mod
        self.client = client or CodexClient(socket)
        self.locks = {}                 # discord thread id -> lock serialising its submits/finalisation
        self.requests = {}              # request key -> {thread, msg, decisions, request}
        self.tasks = set()
        self.started = False
        self.daemon_ok = False
        self.reconnect_backoff = 2

    # ----- state -----

    def entries(self):
        return self.mod.state.setdefault("_astra", {})

    def by_codex(self, codex_id):
        for tid, ent in self.entries().items():
            if ent.get("codex_thread") == codex_id:
                return tid, ent
        return None, None

    def save(self):
        self.mod.save_state()

    def lock(self, tid):
        return self.locks.setdefault(str(tid), asyncio.Lock())

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def allowed(self, user):
        """Who may steer Astra (interrupt, approve, fast): the owner, anyone on the spawn
        allowlist when there is one, otherwise anyone in the thread — the same gate as /astra."""
        uid = getattr(user, "id", None)
        allow = getattr(self.mod, "SPAWN_ALLOW_USERS", set())
        return uid == self.bridge.owner or not allow or uid in allow

    # ----- lifecycle -----

    def start(self):
        if self.started:
            return
        self.started = True
        self.spawn(self.run_events())
        self.spawn(self.card_loop())
        self.spawn(self.boot())

    async def boot(self):
        """Reconnect after a bridge restart and pick up turns that were in flight."""
        try:
            await self.bridge.wait_until_ready()
        except Exception:   # noqa: BLE001  (fakes)
            pass
        pending = [e for e in self.entries().values() if not e.get("ended") and e.get("active_turn")]
        if not pending:
            return
        try:
            await self.ensure_client()
            await self.reconcile()
        except Exception as e:   # noqa: BLE001
            self.mod.log_error("astra boot", e)

    async def ensure_client(self):
        """Daemon up (started on demand, idempotently) and the socket connected."""
        if self.client.connected:
            return
        if not self.daemon_ok:
            status = await asyncio.to_thread(start_daemon, CODEX_HOME, CODEX_BIN, self.client.socket)
            if status.get("status") != "running":
                raise RpcError(f"codex app-server daemon isn't running: {status.get('error', status)}")
            self.daemon_ok = True
            print(f"codex app-server daemon: {status.get('how')} at {status.get('socketPath')}", flush=True)
        try:
            await self.client.connect()
        except (OSError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.daemon_ok = False       # the socket died under us; next attempt re-checks the daemon
            raise RpcError(f"couldn't connect to the codex app-server: {e}")

    # ----- threads -----

    def thread_params(self, ent):
        cfg = {"model_reasoning_effort": ent.get("effort") or self.mod.ASTRA_EFFORT,
               "sandbox_workspace_write.network_access": True}
        return {"approvalPolicy": ent.get("approval") or APPROVAL_POLICY,
                "sandbox": ent.get("sandbox") or self.mod.ASTRA_SANDBOX,
                "model": ent.get("model") or self.mod.ASTRA_MODEL, "config": cfg}

    def import_rollout(self, codex_id):
        """An old `codex exec` thread whose rollout lives in another CODEX_HOME (the shared
        ~/.codex, before the bridge got its private home): copy the file in so the app-server
        can resume it. Returns the destination or None."""
        home = CODEX_HOME
        for src_home in (Path.home() / ".codex",):
            for folder in ("sessions", "archived_sessions"):
                base = src_home / folder
                if not base.is_dir():
                    continue
                for p in base.rglob(f"rollout-*-{codex_id}.jsonl"):
                    dest = home / folder / p.relative_to(base)
                    if dest.exists():
                        return dest
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest)
                    return dest
        return None

    async def ensure_thread(self, thread, ent):
        """The codex thread behind an Astra thread: resume it (importing an old exec rollout if
        needed), or start a fresh one — with a note in the thread when the old one is gone."""
        await self.ensure_client()
        codex_id = ent.get("codex_thread")
        params = self.thread_params(ent)
        if codex_id:
            if codex_id in self.client.subscribed:
                return codex_id
            for attempt in range(2):
                try:
                    await self.client.attach(codex_id, **params)
                    return codex_id
                except RpcError as e:
                    msg = str(e)
                    if attempt == 0 and "no rollout" in msg \
                            and await asyncio.to_thread(self.import_rollout, codex_id):
                        continue
                    if attempt == 0 and "archived" in msg.lower():
                        try:
                            await self.client.call("thread/unarchive", {"threadId": codex_id})
                            continue
                        except RpcError:
                            pass
                    await self.bridge.say(thread, f"-# ⚠️ couldn't resume the earlier codex thread "
                                                  f"`{codex_id[:8]}…` ({_clean(msg, 160)}) — starting a "
                                                  "fresh one; the old conversation is not in its context")
                    break
        r = await self.client.call("thread/start", {"cwd": ent["cwd"], **params})
        codex_id = r["thread"]["id"]
        ent["codex_thread"], ent["backend"] = codex_id, "app-server"
        ent.setdefault("seen_items", [])
        self.client.subscribed.add(codex_id)
        self.save()
        try:
            await self.client.call("thread/name/set", {"threadId": codex_id,
                                                        "name": f"astra · {ent.get('name', '')}"[:90]})
        except RpcError:
            pass
        return codex_id

    # ----- a message arrives -----

    async def send(self, thread, ent, text, user, react_msg=None, queue=False):
        """One Discord message → the running turn (steered), the native queue, or a new turn.
        Returns "steered" | "queued" | "started" | None."""
        tid = str(thread.id)
        who = getattr(user, "display_name", None)
        uid = getattr(user, "id", None)
        prompt = text if (user is None or uid == self.bridge.owner or not who) else f"{who}: {text}"
        async with self.lock(tid):
            if ent.get("ended"):
                return None
            entry = self.track(ent, react_msg, prompt) if react_msg is not None else None
            try:
                codex_id = await self.ensure_thread(thread, ent)
            except (RpcError, ConnectionError, OSError, asyncio.TimeoutError) as e:
                self.mod.log_error("astra thread", e)
                if entry:
                    entry["state"] = "failed"
                    self.spawn(self.sync_receipt(entry))
                await self.bridge.say(thread, f"⚠️ couldn't reach codex: `{_clean(e, 300)}`")
                return None
            client_id = entry["client_id"] if entry else f"astra:{uuid.uuid4().hex[:12]}"
            inputs = [{"type": "text", "text": prompt}]
            how = None
            try:
                turn_id = await self.client.active_turn(codex_id)
                if turn_id and not queue:
                    try:
                        await self.client.call("turn/steer", {"threadId": codex_id, "expectedTurnId": turn_id,
                                                              "input": inputs, "clientUserMessageId": client_id})
                        how = "steered"
                    except RpcError as e:
                        if "no active turn" not in str(e):
                            raise
                        turn_id = None
                if how is None and turn_id and queue:
                    await self.client.call("thread/queue/add", {"threadId": codex_id, "input": inputs,
                                                                "clientUserMessageId": client_id})
                    how = "queued"
                if how is None:
                    params = {"threadId": codex_id, "input": inputs, "clientUserMessageId": client_id,
                              "model": ent.get("model") or self.mod.ASTRA_MODEL,
                              "effort": ent.get("effort") or self.mod.ASTRA_EFFORT}
                    if ent.get("fast"):
                        params["serviceTier"] = FAST_TIER
                    r = await self.client.call("turn/start", params)
                    turn = r.get("turn") or {}
                    how = "started"
                    if turn.get("id"):
                        self.client.active_turns[codex_id] = turn["id"]
                        self.begin_turn(ent, turn["id"], turn.get("startedAt") or time.time())
            except (RpcError, ConnectionError, OSError, asyncio.TimeoutError) as e:
                self.mod.log_error("astra send", e)
                if entry:
                    entry["state"] = "failed"
                    self.spawn(self.sync_receipt(entry))
                await self.bridge.say(thread, f"⚠️ codex didn't take that message: `{_clean(e, 300)}`")
                return None
            if entry:
                entry["state"] = "queued" if how in ("steered", "queued") else "sent"
                entry["how"] = how
                self.spawn(self.sync_receipt(entry))
            card = ent.get("card")
            if card and how == "steered":
                card["narration"], card["dirty"] = f"↪️ steering: {_clean(text, 100)}", True
            elif card and how == "started" and not card.get("msg"):
                self.spawn(self.render_card(tid, ent, force=True))     # show "working" right away
            self.save()
            return how

    # ----- receipts (one bot reaction per message, swapped in place) -----

    def track(self, ent, msg, prompt):
        e = {"mid": msg.id, "ch": msg.channel.id, "norm": " ".join(prompt.split())[:200],
             "client_id": f"astra:{msg.id}", "state": "sent", "shown": None, "at": time.time(),
             "turn": None}
        ib = ent.setdefault("inbox", [])
        ib.append(e)
        del ib[:-8]
        return e

    async def sync_receipt(self, e):
        want = RECEIPT.get(e.get("state"))
        if not want or e.get("shown") == want:
            return
        ch = self.bridge.get_channel(e["ch"]) or await self.bridge.get_thread(e["ch"])
        if ch is None:
            return
        pm = ch.get_partial_message(e["mid"])
        try:
            if e.get("shown"):
                await pm.remove_reaction(e["shown"], self.bridge.user)
            await pm.add_reaction(want)
            e["shown"] = want
        except discord.NotFound:
            e["state"] = "gone"
        except discord.HTTPException as ex:
            self.mod.log_error("astra receipt", ex)

    def receipt_seen(self, ent, item, turn_id):
        """A userMessage item: mark the Discord message it came from as read."""
        text = "\n".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
        cid = item.get("clientId")
        for e in ent.get("inbox") or []:
            if e["state"] in ("done", "failed", "gone"):
                continue
            if (cid and e["client_id"] == cid) or (not cid and _same_text(e["norm"], text)):
                e["state"], e["turn"], e["t_read"] = "read", turn_id, time.time()
                self.spawn(self.sync_receipt(e))
                if cid:
                    break

    def receipts_finish(self, ent, turn_id, ok):
        for e in ent.get("inbox") or []:
            if e["state"] in ("done", "failed", "gone"):
                continue
            if e.get("turn") == turn_id or (e["state"] == "read" and e.get("turn") is None) \
                    or (e["state"] in ("sent",) and e.get("how") == "started"):
                e["state"] = "done" if ok else "failed"
                self.spawn(self.sync_receipt(e))
        ent["inbox"] = [e for e in ent.get("inbox") or [] if time.time() - e["at"] < 86400]

    # ----- the activity card -----

    def begin_turn(self, ent, turn_id, started=None):
        card = ent.get("card")
        if card and card.get("turn_id") in (None, turn_id):
            card["turn_id"] = turn_id
            card.setdefault("started", started or time.time())
        else:
            if card and card.get("msg"):
                self.spawn(self.finish_card(ent, card, "✅ turn done"))   # a card without its completion event
            ent["card"] = card = {"turn_id": turn_id, "started": started or time.time(), "counts": {},
                                  "msg": None, "via": None, "last_edit": 0.0, "narration": "", "desc": "",
                                  "tool": "", "thought": "", "body": "", "dirty": True, "status": "running",
                                  "open_tool": False, "tool_at": 0, "tokens": None, "items": 0}
        card["status"] = "running"
        card["dirty"] = True
        ent["active_turn"] = turn_id
        self.save()
        return card

    def card_text(self, ent, card, final=None):
        el = int(time.time() - card.get("started", time.time()))
        mm, ss = divmod(el, 60)
        tools = ", ".join(f"{n} ×{k}" if k > 1 else n for n, k in (card.get("counts") or {}).items())
        tok = card.get("tokens")
        toks = f" · {tok[0] // 1000}k in / {tok[1] // 1000}k out" if tok else ""
        badges = [f"🧠 `{ent.get('model') or self.mod.ASTRA_MODEL}` @ `{ent.get('effort') or self.mod.ASTRA_EFFORT}`"]
        if ent.get("fast"):
            badges.append("⚡ fast")
        btxt = " · " + " · ".join(badges)
        if final is not None:
            return (f"-# {final} · {mm}m {ss:02d}s" + (f" · 🔧 {tools}" if tools else "") + toks + btxt)[:1990]
        status = card.get("status", "running")
        head = {"running": "🛰️ **Astra is working**", "waiting": "🔔 **Astra needs you**",
                "disconnected": "⚪ **Astra — live updates lost, reconnecting**"}.get(status, "🛰️ **Astra**")
        lines = [f"{head} · {mm}m {ss:02d}s{btxt}{toks}"]
        if card.get("narration"):
            lines.append(f"*{card['narration']}*")
        if card.get("desc"):
            lines.append(f"**{card['desc']}**")
        if card.get("tool"):
            run = ""
            if card.get("open_tool") and card.get("tool_at") and time.time() - card["tool_at"] > 30:
                run = f" · running {_ago(time.time() - card['tool_at'])}"
            lines.append(f"└ `{card['tool']}`{run}")
        if card.get("thought"):
            lines.append(f"💭 {card['thought']}")
        inbox = (ent.get("inbox") or [None])[-1]
        if inbox and inbox["state"] in ("queued", "read") and time.time() - inbox["at"] < 1800:
            age = _ago(time.time() - inbox["at"])
            lines.append("↪️ your message is **queued** into this turn" + f" ({age})" if inbox["state"] == "queued"
                         else f"👀 Astra **read** your message ({_ago(time.time() - inbox.get('t_read', inbox['at']))} ago)")
        if tools:
            lines.append(f"-# 🔧 {tools}")
        return "\n".join(lines)[:1990]

    def card_view(self, tid, ent):
        """Buttons under a running turn. custom_id `x|<thread_id>|<action>`, handled in
        Bridge.on_interaction → AstraBackend.on_button, so they survive bridge restarts."""
        view = discord.ui.View(timeout=None)
        B, S = discord.ui.Button, discord.ButtonStyle
        view.add_item(B(label="⏹ Interrupt", style=S.danger, custom_id=f"x|{tid}|esc", row=0))
        view.add_item(B(label="✋ Interrupt & say…", style=S.primary, custom_id=f"x|{tid}|say", row=0))
        fast = ent.get("fast")
        view.add_item(B(label="⚡ Fast: on → off" if fast else "⚡ Fast: off → on", style=S.secondary,
                        custom_id=f"x|{tid}|fast", row=1))
        return view

    async def parent_of(self, thread):
        parent = getattr(thread, "parent", None)
        if parent is None and getattr(thread, "parent_id", None):
            parent = self.bridge.get_channel(thread.parent_id)
            if parent is None:
                try:
                    parent = await self.bridge.fetch_channel(thread.parent_id)
                except discord.HTTPException:
                    parent = None
        return parent

    def speaker(self, ent):
        return self.mod.webhook_name({"project": Path(ent.get("cwd") or "/").name or "astra",
                                      "name": f"astra · {ent.get('name') or 'gpt'}"})

    async def post(self, thread, ent, text):
        """An agent message: chunked, tables rendered, under Astra's own webhook identity (bot
        voice when the webhook isn't available). Returns the first message."""
        text = self.mod.discord_tables(str(text))
        chunks = self.mod.split_chunks(text)
        first = None
        parent = await self.parent_of(thread)
        for i, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                chunk += f"\n-# ⤵ part {i}/{len(chunks)}"
            m = None
            if parent is not None:
                try:
                    m = await self.bridge.post_as(parent, self.speaker(ent), chunk, thread.id,
                                                  seed=f"astra:{thread.id}")
                except discord.HTTPException as e:
                    self.mod.log_error("astra post_as", e)
            if m is None:
                m = await self.bridge.say(thread, chunk)
            first = first or m
        return first

    async def render_card(self, tid, ent, force=False):
        card = ent.get("card")
        if not card:
            return
        now = time.time()
        if not force:
            since = now - card.get("last_edit", 0)
            if card.get("dirty"):
                if since < CARD_MIN_GAP:
                    return
            elif since < CARD_HEARTBEAT:
                return
        text = self.card_text(ent, card)
        if text == card.get("body") and card.get("msg") and not force:
            card["last_edit"], card["dirty"] = now, False
            return
        thread = await self.bridge.get_thread(int(tid))
        if thread is None:
            return
        view = self.card_view(tid, ent)
        try:
            if card.get("msg"):
                try:
                    await self.edit_card(thread, card, text, view)
                except discord.HTTPException:
                    card["msg"] = None
            if not card.get("msg"):
                await self.post_card(thread, ent, card, text, view)
        except discord.HTTPException as e:
            self.mod.log_error("astra card", e)
        card["last_edit"], card["body"], card["dirty"] = now, text, False
        self.save()

    async def post_card(self, thread, ent, card, text, view):
        parent = await self.parent_of(thread)
        m = None
        if parent is not None:
            try:
                m = await self.bridge.post_as(parent, self.speaker(ent), text, thread.id,
                                              seed=f"astra:{thread.id}", view=view)
                card["via"] = "webhook"
            except discord.HTTPException as e:
                self.mod.log_error("astra card post_as", e)
        if m is None:
            m = await thread.send(text, view=view, allowed_mentions=NO_PING, suppress_embeds=True)
            card["via"] = "bot"
        card["msg"] = m.id if m else None

    async def edit_card(self, thread, card, text, view):
        if card.get("via") == "webhook":
            parent = await self.parent_of(thread)
            await self.bridge.edit_as(parent, card["msg"], text, thread.id, view=view)
        else:
            await thread.get_partial_message(card["msg"]).edit(content=text, view=view,
                                                               allowed_mentions=NO_PING)

    async def finish_card(self, ent, card, label):
        """Collapse the card to its one-line recap and drop the buttons."""
        tid = next((t for t, e in self.entries().items() if e is ent), None)
        text = self.card_text(ent, card, final=label)
        if tid is None:
            return
        thread = await self.bridge.get_thread(int(tid))
        if thread is None:
            return
        try:
            if card.get("msg"):
                await self.edit_card(thread, card, text, None)
            else:
                await self.bridge.say(thread, text)
        except discord.HTTPException as e:
            self.mod.log_error("astra card finish", e)

    async def card_loop(self):
        while not self.bridge.is_closed():
            try:
                for tid, ent in list(self.entries().items()):
                    card = ent.get("card")
                    if not card or ent.get("ended"):
                        continue
                    if card.get("status") == "running" and self.mod.ASTRA_TURN_TIMEOUT \
                            and time.time() - card.get("started", time.time()) > self.mod.ASTRA_TURN_TIMEOUT \
                            and not card.get("timed_out"):
                        card["timed_out"] = True
                        self.spawn(self.timeout_turn(tid, ent))
                    await self.render_card(tid, ent)
            except Exception as e:   # noqa: BLE001
                self.mod.log_error("astra card loop", e)
            await asyncio.sleep(CARD_MIN_GAP)

    async def timeout_turn(self, tid, ent):
        thread = await self.bridge.get_thread(int(tid))
        try:
            await self.ensure_client()
            await self.client.interrupt(ent["codex_thread"])
            if thread:
                await self.bridge.say(thread, f"-# ⏱ turn hit the {self.mod.ASTRA_TURN_TIMEOUT // 60} min wall "
                                              "clock — interrupted")
        except (RpcError, ConnectionError, OSError, asyncio.TimeoutError) as e:
            self.mod.log_error("astra timeout", e)

    # ----- notifications from the daemon -----

    async def run_events(self):
        while not self.bridge.is_closed():
            try:
                event = await asyncio.wait_for(self.client.notifications.get(), timeout=5)
            except asyncio.TimeoutError:
                continue
            try:
                await self.handle(event)
            except Exception as e:   # noqa: BLE001
                self.mod.log_error("astra event", e)
            finally:
                self.client.notifications.task_done()

    async def handle(self, event):
        method = event.get("method")
        params = event.get("params") or {}
        if method == "x/disconnected":
            return await self.on_disconnected()
        if method == "x/request":
            tid, ent = self.by_codex(params.get("threadId"))
            if ent is not None:
                await self.show_request(tid, ent, event["requestKey"], self.client.server_requests.get(event["requestKey"]))
            return
        if method == "serverRequest/resolved":
            return await self.on_request_resolved(params)
        tid, ent = self.by_codex(params.get("threadId"))
        if ent is None or ent.get("ended"):
            return
        async with self.lock(tid):
            await self._handle(tid, ent, method, params)

    async def _handle(self, tid, ent, method, params):
        card = ent.get("card")
        turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
        if method == "turn/started":
            turn = params.get("turn") or {}
            if turn.get("id") in (ent.get("completed_turns") or []):
                return
            self.begin_turn(ent, turn["id"], turn.get("startedAt"))
            for it in turn.get("items") or []:
                if it.get("type") == "userMessage":
                    self.receipt_seen(ent, it, turn["id"])
            return
        if method == "turn/completed":
            return await self.complete_turn(tid, ent, params.get("turn") or {})
        if method == "thread/status/changed":
            st = params.get("status") or {}
            if card and st.get("type") == "active":
                flags = set(st.get("activeFlags") or [])
                card["status"] = "waiting" if flags & {"waitingOnApproval", "waitingOnUserInput"} else "running"
                card["dirty"] = True
            return
        if method == "error":
            err = (params.get("error") or {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            msg = msg or "codex reported an error"
            if params.get("willRetry"):
                if card:
                    card["desc"], card["dirty"] = f"⏳ retrying: {_clean(msg, 160)}", True
                return
            key = f"err:{_clean(msg, 80)}"
            if key not in (ent.get("seen_items") or []):
                ent.setdefault("seen_items", []).append(key)
                thread = await self.bridge.get_thread(int(tid))
                if thread:
                    await self.bridge.say(thread, f"⚠️ codex: {_clean(msg, 600)}")
            return
        if method == "thread/tokenUsage/updated":
            tu = (params.get("tokenUsage") or {}).get("total") or {}
            if card and tu:
                card["tokens"] = (int(tu.get("inputTokens") or 0), int(tu.get("outputTokens") or 0))
            return
        if method == "thread/name/updated":
            return
        # everything below is an item inside a turn: make sure a card exists for it
        if turn_id and (card is None or card.get("turn_id") != turn_id) \
                and turn_id not in (ent.get("completed_turns") or []):
            card = self.begin_turn(ent, turn_id)
        if card is None:
            return
        if method == "item/agentMessage/delta":
            key = f"{params.get('itemId')}:msg"
            if card.get("delta_key") != key:
                card["delta_key"], card["delta_text"] = key, ""
            card["delta_text"] = (card["delta_text"] + (params.get("delta") or ""))[-4000:]
            t = _clean(card["delta_text"], NARRATION_MAX + 1)
            card["narration"] = t if len(t) <= NARRATION_MAX else f"✍️ writing a reply… ({len(card['delta_text'])}+ chars)"
            card["dirty"] = True
            return
        if method == "item/reasoning/summaryTextDelta":
            key = f"{params.get('itemId')}:{params.get('summaryIndex', 0)}:think"
            if card.get("delta_key") != key:
                card["delta_key"], card["delta_text"] = key, ""
            card["delta_text"] = (card["delta_text"] + (params.get("delta") or ""))[-4000:]
            card["thought"], card["dirty"] = _clean(card["delta_text"], 280), True
            return
        if method == "turn/plan/updated":
            steps = params.get("plan") or []
            current = next((s.get("step", "") for s in steps if s.get("status") == "inProgress"), "")
            card["desc"], card["dirty"] = _clean(current or params.get("explanation", ""), 200), True
            return
        if method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            kind = item.get("type")
            if kind == "userMessage":
                self.receipt_seen(ent, item, turn_id)
                return
            if kind == "contextCompaction":
                if method == "item/started":
                    card["desc"], card["dirty"] = "🌀 compacting context…", True
                else:
                    card["desc"], card["dirty"] = "", True
                return
            if kind in TOOL_NAMES:
                key = f"{turn_id}:{item.get('id')}"
                seen = card.setdefault("seen_tools", [])
                if key not in seen:
                    seen.append(key)
                    del seen[:-200]
                    name = TOOL_NAMES[kind]
                    card["counts"][name] = card["counts"].get(name, 0) + 1
                    arg = _clean(item_summary(item), 220)
                    card["tool"] = f"{'$ ' if name == 'Bash' else name + ' '}{arg}" if arg else name
                    card["desc"] = ""
                    card["open_tool"], card["tool_at"] = True, time.time()
                if method == "item/completed":
                    card["open_tool"] = False
                    if kind == "commandExecution" and item.get("exitCode") not in (None, 0):
                        card["desc"] = f"↳ exit {item.get('exitCode')}: {_clean(item.get('aggregatedOutput'), 120)}"
                card["dirty"] = True
                return
            if kind == "reasoning" and method == "item/completed":
                t = reasoning_text(item)
                if t:
                    card["thought"], card["dirty"] = _clean(t, 280), True
                return
            if kind == "agentMessage" and method == "item/completed":
                await self.agent_message(tid, ent, card, turn_id, item)
                return

    async def agent_message(self, tid, ent, card, turn_id, item):
        key = f"{turn_id}:{item.get('id')}"
        seen = ent.setdefault("seen_items", [])
        text = item.get("text") or ""
        if key in seen or not text.strip():
            return
        seen.append(key)
        del seen[:-300]
        phase = item.get("phase") or "final_answer"
        if phase == "commentary" and len(" ".join(text.split())) <= NARRATION_MAX:
            if card is not None:
                card["narration"], card["dirty"] = _clean(text, NARRATION_MAX), True
            self.save()
            return
        if card is not None:
            card["narration"] = card["tool"] = card["desc"] = ""
            card["dirty"] = True
        thread = await self.bridge.get_thread(int(tid))
        if thread is not None:
            await self.post(thread, ent, text)
        self.save()

    async def complete_turn(self, tid, ent, turn):
        turn_id = turn.get("id")
        done = ent.setdefault("completed_turns", [])
        if turn_id in done:
            return
        done.append(turn_id)
        del done[:-50]
        card = ent.get("card")
        status = turn.get("status") or "completed"
        # anything the stream didn't deliver (bridge was down mid-turn): the turn summary has it
        for it in turn.get("items") or []:
            if it.get("type") == "agentMessage":
                await self.agent_message(tid, ent, card, turn_id, it)
        label = {"completed": "✅ turn done", "failed": "❌ turn failed",
                 "interrupted": "⏹ interrupted"}.get(status, f"turn {status}")
        if status == "completed":
            ent["turns"] = int(ent.get("turns") or 0) + 1
        self.receipts_finish(ent, turn_id, status == "completed")
        if card and card.get("turn_id") in (None, turn_id):
            await self.finish_card(ent, card, label)
            ent["card"] = None
        if ent.get("active_turn") == turn_id or card is None:
            ent["active_turn"] = None
        if turn.get("error"):
            thread = await self.bridge.get_thread(int(tid))
            err = turn["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if thread:
                await self.bridge.say(thread, f"⚠️ turn failed: {_clean(msg, 600)}")
        self.save()

    async def on_disconnected(self):
        for tid, ent in self.entries().items():
            card = ent.get("card")
            if card and not ent.get("ended"):
                card["status"], card["dirty"] = "disconnected", True
        self.requests.clear()
        self.spawn(self.reconnect())

    async def reconnect(self):
        delay = self.reconnect_backoff
        while not self.bridge.is_closed():
            await asyncio.sleep(delay)
            try:
                await self.ensure_client()
                self.reconnect_backoff = 2
                await self.reconcile()
                return
            except Exception as e:   # noqa: BLE001
                self.mod.log_error("astra reconnect", e)
                delay = min(delay * 2, 120)

    async def reconcile(self):
        """After (re)connecting: re-attach every thread that had a turn in flight and settle its
        card from the daemon's view — still running (keep streaming), finished while we were
        away (post what we missed, collapse the card), or lost with the daemon."""
        for tid, ent in list(self.entries().items()):
            if ent.get("ended") or not ent.get("codex_thread") or not (ent.get("active_turn") or ent.get("card")):
                continue
            async with self.lock(tid):
                try:
                    await self.client.attach(ent["codex_thread"], **self.thread_params(ent))
                    info = (await self.client.call("thread/read", {"threadId": ent["codex_thread"],
                                                                    "includeTurns": False}))["thread"]
                    page = await self.client.call("thread/turns/list", {"threadId": ent["codex_thread"], "limit": 1,
                                                                         "sortDirection": "desc", "itemsView": "summary"})
                except (RpcError, ConnectionError, OSError, asyncio.TimeoutError) as e:
                    self.mod.log_error("astra reconcile", e)
                    card = ent.get("card")
                    if card:
                        await self.finish_card(ent, card, f"⚠️ lost track of this turn ({_clean(e, 80)})")
                    ent["card"], ent["active_turn"] = None, None
                    self.save()
                    continue
                last = next(iter(page.get("data") or []), None)
                active = (info.get("status") or {}).get("type") == "active"
                card = ent.get("card")
                if last and last.get("status") == "inProgress" and active:
                    self.client.active_turns[ent["codex_thread"]] = last["id"]
                    c = self.begin_turn(ent, last["id"], last.get("startedAt") or (card or {}).get("started"))
                    c["status"] = "waiting" if set((info.get("status") or {}).get("activeFlags") or []) \
                        & {"waitingOnApproval", "waitingOnUserInput"} else "running"
                    continue
                if last and last.get("status") != "inProgress":
                    if card and card.get("turn_id") not in (None, last["id"]):
                        card["turn_id"] = last["id"]     # the card was for this turn by construction
                    await self.complete_turn(tid, ent, last)
                    continue
                # the daemon is idle but its last turn never completed: the daemon restarted mid-turn
                if card:
                    await self.finish_card(ent, card, "⚠️ turn lost (codex restarted) — say it again")
                ent["card"], ent["active_turn"] = None, None
                self.save()

    # ----- approvals and other server→client requests -----

    async def show_request(self, tid, ent, key, request):
        if request is None:
            return
        thread = await self.bridge.get_thread(int(tid))
        if thread is None:
            return
        card = ent.get("card")
        if card:
            card["status"], card["dirty"] = "waiting", True
        decisions = decisions_for(request)
        view = discord.ui.View(timeout=None)
        for n, (label, _result, style) in enumerate(decisions[:5]):
            view.add_item(discord.ui.Button(label=label[:80], style=style, custom_id=f"xa|{key}|{n}"))
        method = request.get("method", "")
        if method in ("item/tool/requestUserInput", "tool/requestUserInput"):
            view.add_item(discord.ui.Button(label="✍️ Answer", style=discord.ButtonStyle.primary,
                                            custom_id=f"xa|{key}|q"))
        elif method == "mcpServer/elicitation/request" and request.get("params", {}).get("mode") == "url":
            url = request["params"].get("url", "")
            if url.startswith(("https://", "http://")):
                view.add_item(discord.ui.Button(label="Open page", url=url))
        body = request_body(request)
        if not decisions and method not in ("item/tool/requestUserInput", "tool/requestUserInput"):
            body += "\n-# this request can't be answered from Discord"
            view = None
        ping = f"<@{self.bridge.owner}> " if self.bridge.owner else ""
        try:
            m = await thread.send(ping + body, view=view,
                                  allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except discord.HTTPException as e:
            self.mod.log_error("astra request", e)
            return
        self.requests[key] = {"thread": tid, "msg": m.id if m else None, "decisions": decisions, "request": request}

    async def on_request_resolved(self, params):
        """Someone (another client, or a cancelled turn) answered a request we showed: retire its buttons."""
        rid = params.get("requestId", params.get("id"))
        for key, info in list(self.requests.items()):
            if info["request"].get("id") == rid or key not in self.client.server_requests:
                await self.retire_request(key, "-# ↩️ resolved elsewhere")

    async def retire_request(self, key, note):
        info = self.requests.pop(key, None)
        if not info or not info.get("msg"):
            return
        thread = await self.bridge.get_thread(int(info["thread"]))
        if thread is None:
            return
        try:
            await thread.get_partial_message(info["msg"]).edit(content=note, view=None)
        except discord.HTTPException:
            pass

    async def on_approval(self, interaction, key, which):
        """`xa|<key>|<n>` button: answer the request with decision n; `q` opens the questions form."""
        if not self.allowed(interaction.user):
            return await interaction.response.send_message("⛔ not yours to answer", ephemeral=True)
        info = self.requests.get(key)
        if info is None or key not in self.client.server_requests:
            try:
                await interaction.response.send_message("that request expired — Astra moved on", ephemeral=True)
                if interaction.message:
                    await interaction.message.edit(content="-# ⌛ expired", view=None)
            except discord.HTTPException:
                pass
            return
        if which == "q":
            qs = (info["request"].get("params") or {}).get("questions") or []
            return await interaction.response.send_modal(QuestionsModal(key, qs))
        try:
            label, result, _ = info["decisions"][int(which)]
        except (ValueError, IndexError):
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        who = getattr(interaction.user, "display_name", "someone")
        try:
            await self.client.answer(key, result)
        except RpcError as e:
            return await self.ephemeral(interaction, f"⚠️ {e}")
        self.requests.pop(key, None)
        tid, ent = self.by_codex((info["request"].get("params") or {}).get("threadId"))
        if ent and ent.get("card"):
            ent["card"]["status"], ent["card"]["dirty"] = "running", True
        try:
            await interaction.message.edit(content=f"-# {label} — {who}", view=None)
        except discord.HTTPException:
            pass

    async def on_questions_modal(self, interaction, key):
        info = self.requests.get(key)
        if info is None or key not in self.client.server_requests:
            return await self.ephemeral(interaction, "that request expired — Astra moved on")
        qs = (info["request"].get("params") or {}).get("questions") or []
        values = _modal_values(interaction.data or {})
        answers = {q.get("id", str(i)): {"answers": [v]} for i, (q, v) in enumerate(zip(qs, values))}
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        try:
            await self.client.answer(key, {"answers": answers})
        except RpcError as e:
            return await self.ephemeral(interaction, f"⚠️ {e}")
        self.requests.pop(key, None)
        try:
            await self.bridge.get_thread(int(info["thread"]))
            await interaction.message.edit(content=f"-# ✅ answered by {getattr(interaction.user, 'display_name', 'someone')}", view=None)
        except (discord.HTTPException, AttributeError):
            pass

    # ----- card buttons, modal, text commands -----

    async def ephemeral(self, interaction, text, **kw):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text[:2000], ephemeral=True, allowed_mentions=NO_PING, **kw)
            else:
                await interaction.response.send_message(text[:2000], ephemeral=True, allowed_mentions=NO_PING, **kw)
        except discord.HTTPException as e:
            self.mod.log_error("astra ephemeral", e)

    async def on_button(self, interaction, tid, action):
        ent = self.entries().get(str(tid))
        if ent is None or ent.get("ended"):
            return await self.ephemeral(interaction, "🌌 this Astra session is over")
        if not self.allowed(interaction.user):
            return await self.ephemeral(interaction, "⛔ not on the allowlist")
        if action == "say":                       # a modal must be the FIRST response — no defer
            return await interaction.response.send_modal(SayModal(tid))
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except discord.HTTPException:
            pass
        thread = await self.bridge.get_thread(int(tid))
        who = getattr(interaction.user, "display_name", "someone")
        if action == "esc":
            try:
                turn = await self.interrupt(ent)
            except (RpcError, ConnectionError, OSError, asyncio.TimeoutError) as e:
                return await self.ephemeral(interaction, f"⚠️ couldn't interrupt: {_clean(e, 200)}")
            if turn and thread:
                await self.bridge.say(thread, f"-# ⏹ interrupted by {who}")
            await self.ephemeral(interaction, "⏹ interrupted" if turn else "nothing is running")
        elif action == "fast":
            ent["fast"] = not ent.get("fast")
            self.save()
            await self.set_tier(ent)
            if ent.get("card"):
                ent["card"]["dirty"] = True
            await self.ephemeral(interaction, f"⚡ fast {'on' if ent['fast'] else 'off'} (applies from the next request)")

    async def on_say_modal(self, interaction, tid):
        """✋ Interrupt & say…: stop the running turn, then send the text as a new turn."""
        text = " ".join(_modal_values(interaction.data or {})[:1]).strip()
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except discord.HTTPException:
            pass
        ent = self.entries().get(str(tid))
        thread = await self.bridge.get_thread(int(tid))
        if not text or ent is None or ent.get("ended") or thread is None:
            return
        try:
            await self.interrupt(ent, wait=15)
        except (RpcError, ConnectionError, OSError, asyncio.TimeoutError) as e:
            await self.bridge.say(thread, f"-# ⚠️ couldn't interrupt first ({_clean(e, 120)}) — sending anyway")
        who = getattr(interaction.user, "display_name", "someone")
        echo = await self.bridge.say(thread, f"✋ **{who}** interrupted: {text}")
        await self.send(thread, ent, text, interaction.user, react_msg=echo)

    async def interrupt(self, ent, wait=0):
        await self.ensure_client()
        if not ent.get("codex_thread"):
            return None
        turn = await self.client.interrupt(ent["codex_thread"])
        if turn and wait:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and await self.client.active_turn(ent["codex_thread"]):
                await asyncio.sleep(0.3)
        return turn

    async def set_tier(self, ent):
        if not ent.get("codex_thread") or not self.client.connected:
            return
        try:
            await self.client.call("thread/settings/update", {"threadId": ent["codex_thread"],
                                                              "serviceTier": FAST_TIER if ent.get("fast") else "default"})
        except RpcError as e:
            self.mod.log_error("astra tier", e)

    async def set_model(self, thread, ent, user, value):
        if not self.bridge.privileged(user):
            return await self.bridge.say(thread, "⛔ `!model` is owner-only")
        value = (value or "").strip()
        if not value or value == "?":
            names = []
            try:
                await self.ensure_client()
                names = [m["model"] for m in (await self.client.call("model/list", {})).get("data", [])]
            except (RpcError, ConnectionError, OSError, asyncio.TimeoutError):
                pass
            return await self.bridge.say(thread, f"-# model: `{ent.get('model') or self.mod.ASTRA_MODEL}`"
                                                 + (f" · available: {', '.join(f'`{n}`' for n in names)}" if names else "")
                                                 + " · `!model <name>` or `!model default`")
        if value == "default":
            value = self.mod.ASTRA_MODEL
        if not MODEL_RE.match(value):
            return await self.bridge.say(thread, "-# that doesn't look like a model id")
        ent["model"] = value
        self.save()
        if ent.get("active_turn") and ent.get("codex_thread") and self.client.connected:
            try:
                await self.client.call("thread/settings/update", {"threadId": ent["codex_thread"], "model": value})
            except RpcError as e:
                self.mod.log_error("astra model", e)
        await self.bridge.say(thread, f"🧠 model → `{value}` from the next turn")

    async def set_effort(self, thread, ent, value):
        value = (value or "").strip().lower()
        if value not in self.mod.ASTRA_EFFORTS:
            return await self.bridge.say(thread, f"-# efforts: {', '.join(self.mod.ASTRA_EFFORTS)} "
                                                 f"(now `{ent.get('effort') or self.mod.ASTRA_EFFORT}`)")
        ent["effort"] = value
        self.save()
        if ent.get("active_turn") and ent.get("codex_thread") and self.client.connected:
            try:
                await self.client.call("thread/settings/update", {"threadId": ent["codex_thread"], "effort": value})
            except RpcError as e:
                self.mod.log_error("astra effort", e)
        return True

    async def kill(self, thread, ent, delete=False):
        ent["ended"] = True
        self.save()
        try:
            if ent.get("codex_thread"):
                await self.ensure_client()
                try:
                    await asyncio.wait_for(self.client.interrupt(ent["codex_thread"]), 20)
                except (RpcError, asyncio.TimeoutError):
                    pass
                try:
                    await self.client.call("thread/archive", {"threadId": ent["codex_thread"]})
                except RpcError:
                    pass
                self.client.subscribed.discard(ent["codex_thread"])
        except (RpcError, ConnectionError, OSError, asyncio.TimeoutError) as e:
            self.mod.log_error("astra kill", e)
        card = ent.get("card")
        if card:
            await self.finish_card(ent, card, "🌌 session ended")
            ent["card"], ent["active_turn"] = None, None
        self.save()

    def help_text(self):
        return ("🛰️ **Astra thread** — plain messages are turns for GPT (codex). A message that lands while it "
                "is working is **steered into the running turn** (`!queue <msg>` queues it for after instead).\n"
                "`!stop` interrupt · `!effort <level>` · `!model <name>` (owner) · `!fast [on|off]` · "
                "`!kill [delete]` end it · `!help`\n"
                "Reactions on your message: 📨 sent · ⏳ queued · 👀 read · ✅ answered · ❌ failed. Approval "
                "requests arrive as buttons; the card's ⏹ / ✋ buttons interrupt the turn.")


class SayModal(discord.ui.Modal):
    """The form behind ✋ Interrupt & say… on an Astra card (handled by custom_id in
    Bridge.on_interaction → AstraBackend.on_say_modal, so it survives bridge restarts)."""

    def __init__(self, tid):
        super().__init__(title="Interrupt Astra and say…", custom_id=f"xm|{tid}|say", timeout=None)
        self.add_item(discord.ui.TextInput(label="Message (sent once the turn has stopped)", custom_id="text",
                                           style=discord.TextStyle.paragraph, max_length=1900))


class QuestionsModal(discord.ui.Modal):
    """Codex's requestUserInput questions as a form (≤5 fields, Discord's limit)."""

    def __init__(self, key, questions):
        super().__init__(title="Astra has questions", custom_id=f"xq|{key}", timeout=None)
        for q in questions[:5]:
            options = ", ".join(o.get("label", "") for o in (q.get("options") or []) if isinstance(o, dict))
            self.add_item(discord.ui.TextInput(label=(q.get("header") or q.get("question") or "answer")[:45],
                                               placeholder=(options or q.get("question") or "")[:100],
                                               style=discord.TextStyle.paragraph, required=False, max_length=1900))


# ---------- CLI: `python codex_backend.py ensure-daemon` (used by the systemd unit) ----------

def _cli(argv):
    cmd = argv[1] if len(argv) > 1 else "ensure-daemon"
    if cmd == "ensure-daemon":
        status = start_daemon()
        print(json.dumps(status))
        return 0 if status.get("status") == "running" else 1
    if cmd == "status":
        print(json.dumps({"socket": str(SOCKET), "alive": socket_alive(), "daemon": daemon_status()}))
        return 0
    if cmd == "seed":
        print(seed_home())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
