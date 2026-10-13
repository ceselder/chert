"""Fake Discord objects, a fake bridge and a fake discord_bot module for codex_backend tests.

Everything records what it would have shown so a test (or the live script) can read the
"transcript" of a thread: sends, edits, reactions, button rows.
"""

import asyncio
import itertools
import types

import discord

_ids = itertools.count(1000)


def view_buttons(view):
    """[(label, custom_id)] of a discord.ui.View (or [] for None)."""
    if view is None:
        return []
    return [(getattr(c, "label", None), getattr(c, "custom_id", None)) for c in view.children]


class FakeUser:
    def __init__(self, uid=1, name="owner"):
        self.id, self.display_name = uid, name


class FakePartial:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    async def add_reaction(self, emoji):
        self.channel.log.append(("react+", self.id, emoji))
        self.channel.reactions.setdefault(self.id, []).append(emoji)

    async def remove_reaction(self, emoji, user=None):
        self.channel.log.append(("react-", self.id, emoji))
        if emoji in self.channel.reactions.get(self.id, []):
            self.channel.reactions[self.id].remove(emoji)

    async def edit(self, content=None, view=None, **kw):
        self.channel.log.append(("edit", self.id, content, view_buttons(view)))
        self.channel.edits[self.id] = (content, view)


class FakeMessage:
    def __init__(self, channel, content="", author=None, mid=None):
        self.channel, self.content, self.author = channel, content, author or FakeUser()
        self.id = mid or next(_ids)
        self.attachments = []
        self.webhook_id = None

    async def add_reaction(self, emoji):
        await FakePartial(self.channel, self.id).add_reaction(emoji)

    async def remove_reaction(self, emoji, user=None):
        await FakePartial(self.channel, self.id).remove_reaction(emoji, user)

    async def edit(self, **kw):
        await FakePartial(self.channel, self.id).edit(**kw)


class FakeChannel:
    """A text channel; also the base for threads."""

    def __init__(self, cid=None, name="claudes"):
        self.id, self.name = cid or next(_ids), name
        self.log = []          # every Discord-visible action, in order
        self.sends = []        # FakeMessage per send/post
        self.edits = {}        # message id -> (content, view)
        self.reactions = {}    # message id -> [emoji]
        self.archived = False
        self.threads = []

    async def send(self, content=None, view=None, **kw):
        m = FakeMessage(self, content or "")
        self.sends.append(m)
        self.log.append(("send", m.id, content, view_buttons(view)))
        return m

    def get_partial_message(self, mid):
        return FakePartial(self, mid)

    async def edit(self, **kw):
        self.log.append(("thread-edit", kw))
        if "archived" in kw:
            self.archived = kw["archived"]
        if "name" in kw:
            self.name = kw["name"]

    async def delete(self):
        self.log.append(("delete",))

    async def create_thread(self, name, **kw):
        t = FakeThread(self, name)
        self.threads.append(t)
        return t

    def texts(self):
        return [c for kind, _mid, c, *_ in self.log if kind == "send" and c]


class FakeThread(FakeChannel):
    def __init__(self, parent, name="🛰️ astra · test"):
        super().__init__(name=name)
        self.parent, self.parent_id = parent, parent.id


class FakeBridge:
    """The slice of discord_bot.Bridge that codex_backend touches."""

    def __init__(self, owner=1):
        self.owner = owner
        self.user = FakeUser(999, "chert")
        self.channels = {}
        self.closed = False
        self.posted = []       # (thread_id, name, content, buttons) from post_as

    def register(self, *channels):
        for c in channels:
            self.channels[c.id] = c

    def is_closed(self):
        return self.closed

    async def wait_until_ready(self):
        return None

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        if cid in self.channels:
            return self.channels[cid]
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="nf"), "unknown channel")

    async def get_thread(self, tid):
        return self.channels.get(tid)

    def privileged(self, user):
        return getattr(user, "id", None) == self.owner

    async def say(self, channel, text, ping_owner=False):
        if channel is None:
            return None
        return await channel.send(text)

    async def post_as(self, channel, name, content, thread_id=None, seed=None, view=None):
        thread = self.channels[thread_id]
        m = FakeMessage(thread, content)
        thread.sends.append(m)
        thread.log.append(("post", m.id, content, view_buttons(view)))
        self.posted.append((thread_id, name, content, view_buttons(view)))
        return m

    async def edit_as(self, channel, message_id, content, thread_id=None, view="missing"):
        thread = self.channels[thread_id]
        thread.log.append(("edit", message_id, content, view_buttons(view) if view != "missing" else "keep"))
        thread.edits[message_id] = (content, view)


def fake_mod(state=None):
    """A stand-in for the discord_bot module: the names codex_backend reads from it."""
    import re

    def split_chunks(text, limit=1900):
        return [text[i:i + limit] for i in range(0, len(text), limit)] or [text]

    reserved = re.compile(r"discord|clyde", re.I)

    def webhook_name(s, maxlen=76):
        return f"{reserved.sub('chat', s.get('project') or '?')} · {reserved.sub('chat', s.get('name') or '')}"[:maxlen]

    errors = []
    return types.SimpleNamespace(
        state=state if state is not None else {}, save_state=lambda: None,
        log_error=lambda where, e: errors.append((where, repr(e))), errors=errors,
        split_chunks=split_chunks, discord_tables=lambda t: t, webhook_name=webhook_name,
        ASTRA_MODEL="gpt-6-astra", ASTRA_EFFORT="xhigh", ASTRA_SANDBOX="danger-full-access",
        ASTRA_EFFORTS=("low", "medium", "high", "xhigh", "max", "ultra"), ASTRA_TURN_TIMEOUT=10800,
        SPAWN_ALLOW_USERS=set())


class FakeClient:
    """A scripted app-server: records every call, answers from a tiny model of thread state."""

    def __init__(self):
        self.connected = True
        self.socket = "fake.sock"
        self.notifications = asyncio.Queue()
        self.server_requests = {}
        self.active_turns = {}
        self.subscribed = set()
        self.completed_turns = set()
        self.calls = []
        self.answers = []
        self.turn_seq = itertools.count(1)
        self.fail_resume = None          # RpcError message to raise on thread/resume (first N times)
        self.fail_resume_times = 0
        self.turns_list = []             # scripted thread/turns/list data
        self.thread_status = "idle"
        self.steer_error = None
        self.new_thread_id = "cx-new-1"

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False

    def _turn(self):
        return f"turn-{next(self.turn_seq)}"

    async def call(self, method, params, timeout=None):
        from codex_backend import RpcError
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": self.new_thread_id, "cwd": params.get("cwd")}, "model": params.get("model")}
        if method == "thread/resume":
            if self.fail_resume and self.fail_resume_times > 0:
                self.fail_resume_times -= 1
                raise RpcError(self.fail_resume)
            return {"thread": {"id": params["threadId"], "status": {"type": self.thread_status}}}
        if method == "thread/read":
            active = params["threadId"] in self.active_turns or self.thread_status == "active"
            return {"thread": {"id": params["threadId"], "status": {"type": "active" if active else "idle"}}}
        if method == "thread/turns/list":
            return {"data": list(self.turns_list), "nextCursor": None}
        if method == "turn/start":
            tid = self._turn()
            self.active_turns[params["threadId"]] = tid
            return {"turn": {"id": tid, "status": "inProgress", "startedAt": None}}
        if method == "turn/steer":
            if self.steer_error:
                raise RpcError(self.steer_error)
            if params["threadId"] not in self.active_turns:
                raise RpcError("no active turn to steer")
            return {"turnId": params["expectedTurnId"]}
        if method == "thread/queue/add":
            return {"queuedSubmission": {"id": "q-1", "clientUserMessageId": params.get("clientUserMessageId")}}
        if method == "turn/interrupt":
            self.active_turns.pop(params["threadId"], None)
            return {}
        if method == "model/list":
            return {"data": [{"model": "gpt-6-astra"}, {"model": "gpt-5.6-sol"}]}
        if method in ("thread/settings/update", "thread/name/set", "thread/archive", "thread/unarchive"):
            return {}
        raise RpcError(f"unknown method {method}")

    async def attach(self, thread_id, **overrides):
        if thread_id in self.subscribed:
            return None
        r = await self.call("thread/resume", {"threadId": thread_id, "excludeTurns": True, **overrides})
        self.subscribed.add(thread_id)
        return r

    async def active_turn(self, thread_id):
        return self.active_turns.get(thread_id)

    async def interrupt(self, thread_id):
        turn = self.active_turns.get(thread_id)
        if turn:
            await self.call("turn/interrupt", {"threadId": thread_id, "turnId": turn})
        return turn

    async def answer(self, key, result):
        from codex_backend import RpcError
        if key not in self.server_requests:
            raise RpcError("expired")
        self.answers.append((key, result))
        self.server_requests.pop(key)

    def called(self, method):
        return [p for m, p in self.calls if m == method]


class FakeInteraction:
    """Enough of discord.Interaction for buttons/modals: response.defer/send_message/send_modal,
    followup.send, message.edit, data (modal values)."""

    def __init__(self, user, message=None, channel=None, data=None):
        self.user, self.message, self.channel = user, message, channel
        self.data = data or {}
        self.sent = []
        self.modals = []
        self._done = False
        inter = self

        class Response:
            def is_done(self_):
                return inter._done

            async def defer(self_, **kw):
                inter._done = True

            async def send_message(self_, content=None, **kw):
                inter._done = True
                inter.sent.append(content)

            async def send_modal(self_, modal):
                inter._done = True
                inter.modals.append(modal)

        class Followup:
            async def send(self_, content=None, **kw):
                inter.sent.append(content)

        self.response, self.followup = Response(), Followup()
