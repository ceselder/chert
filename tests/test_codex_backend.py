"""Offline tests for codex_backend: the client's frame routing, a turn's event stream into the
card and the thread, steering/queueing with read receipts, approvals as buttons, resume
fallbacks, and reconciliation after a bridge restart. No daemon, no Discord.

    .venv/bin/python -m pytest tests/test_codex_backend.py -q
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_backend as cb                                   # noqa: E402
from fakes import (FakeBridge, FakeChannel, FakeClient, FakeInteraction, FakeMessage,  # noqa: E402
                   FakeThread, FakeUser, fake_mod, view_buttons)


def run(coro):
    return asyncio.run(coro)


def setup(ent_extra=None, state=None):
    bridge = FakeBridge(owner=1)
    parent = FakeChannel(name="claudes")
    thread = FakeThread(parent)
    bridge.register(parent, thread)
    mod = fake_mod(state)
    ent = {"codex_thread": None, "cwd": "/tmp/proj", "sandbox": "danger-full-access", "effort": "xhigh",
           "model": "gpt-6-astra", "name": "test", "created": 0, "turns": 0, "ended": False, "by": "owner",
           **(ent_extra or {})}
    mod.state["_astra"] = {str(thread.id): ent}
    client = FakeClient()
    backend = cb.AstraBackend(bridge, mod, client=client)
    backend.daemon_ok = True
    return bridge, thread, mod, ent, client, backend


def ev(method, thread_id, **params):
    return {"method": method, "params": {"threadId": thread_id, **params}}


def posts(thread):
    """[(message id, content, buttons)] for everything sent or webhook-posted into a thread."""
    return [(e[1], e[2], e[3]) for e in thread.log if e[0] in ("send", "post")]


async def drain(backend):
    """Run every background task the backend spawned (receipt syncs, card finishes)."""
    for _ in range(5):
        pending = [t for t in backend.tasks if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


# ---------- the client ----------

def test_client_routes_frames():
    async def main():
        c = cb.CodexClient(socket="/nonexistent.sock")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        c.pending[7] = fut
        c.on_message({"id": 7, "result": {"ok": 1}})
        assert fut.result() == {"ok": 1}
        fut2 = loop.create_future()
        c.pending[8] = fut2
        c.on_message({"id": 8, "error": {"code": -32600, "message": "no active turn to steer"}})
        with pytest.raises(cb.RpcError, match="no active turn"):
            fut2.result()
        c.on_message({"method": "turn/started", "params": {"threadId": "T", "turn": {"id": "t1"}}})
        assert c.active_turns == {"T": "t1"}
        c.on_message({"method": "account/rateLimits/updated", "params": {}})   # not wanted: dropped
        c.on_message({"method": "item/commandExecution/requestApproval", "id": 0,
                      "params": {"threadId": "T", "command": "ls"}})
        c.on_message({"method": "turn/completed", "params": {"threadId": "T", "turn": {"id": "t1", "status": "completed"}}})
        assert c.active_turns == {} and "t1" in c.completed_turns
        kinds = []
        while not c.notifications.empty():
            kinds.append(c.notifications.get_nowait()["method"])
        assert kinds == ["turn/started", "x/request", "turn/completed"]
        assert len(c.server_requests) == 1 and list(c.server_requests)[0].endswith(":0")
    run(main())


# ---------- sending ----------

def test_first_message_starts_thread_and_turn():
    async def main():
        bridge, thread, mod, ent, client, backend = setup()
        msg = FakeMessage(thread, "reply with PLUM", FakeUser(1, "owner"))
        how = await backend.send(thread, ent, "reply with PLUM", msg.author, react_msg=msg)
        assert how == "started"
        start = client.called("thread/start")[0]
        assert start["cwd"] == "/tmp/proj" and start["sandbox"] == "danger-full-access"
        assert start["approvalPolicy"] == "on-request" and start["model"] == "gpt-6-astra"
        assert start["config"]["model_reasoning_effort"] == "xhigh"
        assert start["config"]["sandbox_workspace_write.network_access"] is True
        assert ent["codex_thread"] == "cx-new-1"
        assert client.called("thread/name/set")[0]["name"].startswith("astra · test")
        turn = client.called("turn/start")[0]
        assert turn["input"] == [{"type": "text", "text": "reply with PLUM"}]      # owner: verbatim
        assert turn["model"] == "gpt-6-astra" and turn["effort"] == "xhigh"
        assert turn["clientUserMessageId"] == f"astra:{msg.id}" and "serviceTier" not in turn
        assert ent["card"]["turn_id"] == "turn-1" and ent["active_turn"] == "turn-1"
        await drain(backend)
        assert ent["inbox"][-1]["state"] == "sent" and thread.reactions[msg.id] == ["📨"]
        # another user is attributed; fast adds the service tier
        ent["fast"] = True
        client.active_turns.clear()
        await backend.send(thread, ent, "hi", FakeUser(2, "bob"))
        turn = client.called("turn/start")[1]
        assert turn["input"][0]["text"] == "bob: hi" and turn["serviceTier"] == "priority"
    run(main())


def test_turn_stream_feeds_card_posts_reply_and_finishes():
    async def main():
        bridge, thread, mod, ent, client, backend = setup()
        msg = FakeMessage(thread, "list files", FakeUser(1))
        await backend.send(thread, ent, "list files", msg.author, react_msg=msg)
        T = ent["codex_thread"]
        await backend.handle(ev("turn/started", T, turn={"id": "turn-1", "items": [], "startedAt": 1}))
        await backend.handle(ev("item/started", T, turnId="turn-1",
                                item={"type": "userMessage", "id": "u1", "clientId": f"astra:{msg.id}",
                                      "content": [{"type": "text", "text": "list files"}]}))
        await drain(backend)
        assert ent["inbox"][-1]["state"] == "read" and thread.reactions[msg.id] == ["👀"]
        await backend.handle(ev("item/completed", T, turnId="turn-1",
                                item={"type": "agentMessage", "id": "m0", "phase": "commentary", "text": "I'll list them."}))
        await backend.handle(ev("item/started", T, turnId="turn-1",
                                item={"type": "commandExecution", "id": "e1", "command": "/bin/bash -lc 'ls -la'",
                                      "commandActions": [{"type": "unknown", "command": "ls -la"}]}))
        await backend.handle(ev("item/reasoning/summaryTextDelta", T, turnId="turn-1", itemId="r1", summaryIndex=0,
                                delta="Checking the directory"))
        await backend.render_card(str(thread.id), ent, force=True)
        card_mid = ent["card"]["msg"]
        card_body, view = thread.edits[card_mid]
        buttons = view_buttons(view)
        assert posts(thread)[-1][0] == card_mid and "Astra is working" in posts(thread)[-1][1]   # posted at send()
        assert "🛰️ **Astra is working**" in card_body and "$ ls -la" in card_body
        assert "*I'll list them.*" in card_body and "💭 Checking the directory" in card_body
        assert "🔧 Bash" in card_body and "👀 Astra **read** your message" in card_body
        assert [cid for _l, cid in buttons] == [f"x|{thread.id}|esc", f"x|{thread.id}|say", f"x|{thread.id}|fast"]
        await backend.handle(ev("item/completed", T, turnId="turn-1",
                                item={"type": "commandExecution", "id": "e1", "command": "ls -la", "exitCode": 0}))
        assert ent["card"]["open_tool"] is False
        await backend.handle(ev("item/completed", T, turnId="turn-1",
                                item={"type": "agentMessage", "id": "m1", "phase": "final_answer",
                                      "text": "Here they are:\n| a | b |\n|---|---|\n| 1 | 2 |"}))
        replies = [c for tid, name, c, _b in bridge.posted if "Here they are" in c]
        assert len(replies) == 1 and bridge.posted[-1][1] == "proj · astra · test"
        await backend.handle(ev("thread/tokenUsage/updated", T, turnId="turn-1",
                                tokenUsage={"total": {"inputTokens": 17249, "outputTokens": 2100}}))
        await backend.handle(ev("turn/completed", T, turn={"id": "turn-1", "status": "completed",
                                                           "items": [{"type": "agentMessage", "id": "m1", "phase": "final_answer",
                                                                      "text": "Here they are:"}]}))
        await drain(backend)
        final = thread.edits[card_mid]
        assert final[0].startswith("-# ✅ turn done · ") and "🔧 Bash" in final[0] and "17k in / 2k out" in final[0]
        assert final[1] is None                                   # buttons gone
        assert ent["card"] is None and ent["active_turn"] is None and ent["turns"] == 1
        assert ent["inbox"][-1]["state"] == "done" and thread.reactions[msg.id] == ["✅"]
        assert len([c for *_, c, _b in bridge.posted if "Here they are" in c]) == 1   # not re-posted from the summary
    run(main())


def test_mid_turn_message_is_steered_then_read():
    async def main():
        bridge, thread, mod, ent, client, backend = setup()
        await backend.send(thread, ent, "do a thing", FakeUser(1))
        T = ent["codex_thread"]
        msg = FakeMessage(thread, "also this", FakeUser(1))
        how = await backend.send(thread, ent, "also this", msg.author, react_msg=msg)
        assert how == "steered"
        steer = client.called("turn/steer")[0]
        assert steer["expectedTurnId"] == "turn-1" and steer["clientUserMessageId"] == f"astra:{msg.id}"
        await drain(backend)
        assert ent["inbox"][-1]["state"] == "queued" and thread.reactions[msg.id] == ["⏳"]
        assert ent["card"]["narration"].startswith("↪️ steering")
        await backend.handle(ev("item/started", T, turnId="turn-1",
                                item={"type": "userMessage", "id": "u2", "clientId": f"astra:{msg.id}",
                                      "content": [{"type": "text", "text": "also this"}]}))
        await drain(backend)
        assert ent["inbox"][-1]["state"] == "read" and thread.reactions[msg.id] == ["👀"]
        # queue=True uses the native queue instead
        msg2 = FakeMessage(thread, "later", FakeUser(1))
        assert await backend.send(thread, ent, "later", msg2.author, react_msg=msg2, queue=True) == "queued"
        assert client.called("thread/queue/add")[0]["clientUserMessageId"] == f"astra:{msg2.id}"
        await backend.handle(ev("turn/completed", T, turn={"id": "turn-1", "status": "failed",
                                                           "error": {"message": "boom"}}))
        await drain(backend)
        assert thread.reactions[msg.id] == ["❌"] and "⚠️ turn failed: boom" in thread.texts()[-1]
        assert ent["inbox"][-1]["state"] == "queued"            # the queued one is still pending
    run(main())


def test_steer_falls_back_to_a_new_turn_when_the_turn_just_ended():
    async def main():
        bridge, thread, mod, ent, client, backend = setup()
        await backend.send(thread, ent, "first", FakeUser(1))
        client.steer_error = "no active turn to steer"
        how = await backend.send(thread, ent, "second", FakeUser(1))
        assert how == "started" and len(client.called("turn/start")) == 2
        client.steer_error = "something else broke"
        assert await backend.send(thread, ent, "third", FakeUser(1)) is None
        assert "codex didn't take that message" in thread.texts()[-1]
    run(main())


def test_resume_old_thread_imports_rollout_or_starts_fresh_with_note(monkeypatch):
    async def fresh():
        bridge, thread, mod, ent, client, backend = setup({"codex_thread": "old-exec-id"})
        client.fail_resume, client.fail_resume_times = "no rollout found for thread id old-exec-id", 5
        monkeypatch.setattr(backend, "import_rollout", lambda cid: None)
        await backend.send(thread, ent, "hello again", FakeUser(1))
        assert ent["codex_thread"] == "cx-new-1" and client.called("thread/start")
        assert any("couldn't resume the earlier codex thread" in t for t in thread.texts())

    async def imported():
        bridge, thread, mod, ent, client, backend = setup({"codex_thread": "old-exec-id"})
        client.fail_resume, client.fail_resume_times = "no rollout found for thread id old-exec-id", 1
        monkeypatch.setattr(backend, "import_rollout", lambda cid: Path("/tmp/imported.jsonl"))
        await backend.send(thread, ent, "hello again", FakeUser(1))
        assert ent["codex_thread"] == "old-exec-id" and not client.called("thread/start")
        resumes = client.called("thread/resume")
        assert len(resumes) == 2 and resumes[0]["approvalPolicy"] == "on-request"
        assert not any("couldn't resume" in t for t in thread.texts())
    run(fresh())
    run(imported())


# ---------- approvals ----------

def test_approval_request_becomes_buttons_and_answers():
    async def main():
        bridge, thread, mod, ent, client, backend = setup({"codex_thread": "cx-1"})
        client.subscribed.add("cx-1")
        request = {"method": "item/commandExecution/requestApproval", "id": 0, "params": {
            "threadId": "cx-1", "turnId": "turn-1", "itemId": "e1",
            "reason": "May I write hello.txt? The sandbox is read-only.",
            "command": "/bin/bash -lc \"printf 'hi' > /tmp/hello.txt\"",
            "commandActions": [{"type": "unknown", "command": "printf 'hi' > /tmp/hello.txt"}],
            "proposedExecpolicyAmendment": ["/bin/bash", "-lc", "printf 'hi' > /tmp/hello.txt"],
            "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": []}}, "cancel"]}}
        client.server_requests["k1"] = request
        ent["card"] = backend.begin_turn(ent, "turn-1")
        await backend.handle({"method": "x/request", "requestKey": "k1", "rpc": request["method"], "params": request["params"]})
        kind, mid, body, buttons = thread.log[-1]
        assert kind == "send" and body.startswith("<@1> 🟠 **Astra asks to run**") and "printf 'hi' > /tmp/hello.txt" in body
        assert "May I write hello.txt" in body
        assert [l for l, _ in buttons] == ["✅ Allow once", "⏹ Cancel turn", "✅ Always allow this command"]
        assert [c for _, c in buttons] == ["xa|k1|0", "xa|k1|1", "xa|k1|2"]
        assert ent["card"]["status"] == "waiting"
        inter = FakeInteraction(FakeUser(1), message=thread.sends[-1], channel=thread)
        await backend.on_approval(inter, "k1", "0")
        assert client.answers == [("k1", {"decision": "accept"})]
        assert thread.edits[mid][0].startswith("-# ✅ Allow once —") and ent["card"]["status"] == "running"
        # pressing again: expired
        inter2 = FakeInteraction(FakeUser(1), message=thread.sends[-1], channel=thread)
        await backend.on_approval(inter2, "k1", "0")
        assert inter2.sent and "expired" in inter2.sent[0]
        # a stranger can't answer when an allowlist exists
        mod.SPAWN_ALLOW_USERS = {1}
        client.server_requests["k2"] = request
        await backend.handle({"method": "x/request", "requestKey": "k2", "rpc": request["method"], "params": request["params"]})
        inter3 = FakeInteraction(FakeUser(5, "stranger"), message=thread.sends[-1], channel=thread)
        await backend.on_approval(inter3, "k2", "0")
        assert "not yours" in inter3.sent[0] and len(client.answers) == 1
    run(main())


def test_decisions_and_bodies_for_other_request_kinds():
    perms = {"method": "item/permissions/requestApproval", "params": {"permissions": {"network": True}}}
    assert [l for l, _r, _s in cb.decisions_for(perms)] == ["✅ Allow for this turn", "✋ Deny"]
    assert cb.decisions_for(perms)[0][1] == {"permissions": {"network": True}, "scope": "turn"}
    q = {"method": "item/tool/requestUserInput", "params": {"questions": [{"id": "a", "question": "Which branch?"}]}}
    assert cb.decisions_for(q) == [] and "• Which branch?" in cb.request_body(q)
    el = {"method": "mcpServer/elicitation/request", "params": {"mode": "url", "url": "https://x.y/auth"}}
    assert [l for l, _r, _s in cb.decisions_for(el)][0].startswith("✅ Continue")
    fc = {"method": "item/fileChange/requestApproval", "params": {"reason": "edit outside workspace"}}
    assert [l for l, _r, _s in cb.decisions_for(fc)] == ["✅ Allow once", "✅ Allow for session", "✋ Deny", "⏹ Cancel turn"]
    assert cb.command_text({"command": "/bin/bash -lc 'ls -la'"}) == "ls -la"


# ---------- restarts and disconnects ----------

def test_reconcile_after_bridge_restart_posts_missed_reply_and_collapses_card():
    async def main():
        card = {"turn_id": "turn-9", "started": 1, "counts": {"Bash": 2}, "msg": None, "via": None, "last_edit": 0.0,
                "narration": "", "desc": "", "tool": "", "thought": "", "body": "", "dirty": True, "status": "running",
                "open_tool": False, "tool_at": 0, "tokens": None, "items": 0}
        bridge, thread, mod, ent, client, backend = setup({"codex_thread": "cx-1", "active_turn": "turn-9", "card": card})
        client.thread_status = "idle"
        client.turns_list = [{"id": "turn-9", "status": "completed", "completedAt": 5,
                              "items": [{"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "x"}]},
                                        {"type": "agentMessage", "id": "late", "phase": "final_answer", "text": "LATE REPLY"}]}]
        await backend.reconcile()
        await drain(backend)
        assert client.called("thread/resume")[0]["threadId"] == "cx-1"
        assert any("LATE REPLY" in c for *_, c, _b in bridge.posted)
        assert ent["card"] is None and ent["active_turn"] is None and ent["turns"] == 1
        assert thread.texts()[-1].startswith("-# ✅ turn done")        # no card message existed: recap said in thread

    async def still_running():
        card = {"turn_id": "turn-9", "started": 1, "counts": {}, "msg": None, "via": None, "last_edit": 0.0, "narration": "",
                "desc": "", "tool": "", "thought": "", "body": "", "dirty": True, "status": "running", "open_tool": False,
                "tool_at": 0, "tokens": None, "items": 0}
        bridge, thread, mod, ent, client, backend = setup({"codex_thread": "cx-1", "active_turn": "turn-9", "card": card})
        client.thread_status = "active"
        client.turns_list = [{"id": "turn-9", "status": "inProgress", "startedAt": 1}]
        await backend.reconcile()
        assert ent["card"]["turn_id"] == "turn-9" and ent["card"]["status"] == "running"
        assert client.active_turns["cx-1"] == "turn-9"

    async def lost():
        card = {"turn_id": "turn-9", "started": 1, "counts": {}, "msg": None, "via": None, "last_edit": 0.0, "narration": "",
                "desc": "", "tool": "", "thought": "", "body": "", "dirty": True, "status": "running", "open_tool": False,
                "tool_at": 0, "tokens": None, "items": 0}
        bridge, thread, mod, ent, client, backend = setup({"codex_thread": "cx-1", "active_turn": "turn-9", "card": card})
        client.thread_status = "idle"
        client.turns_list = [{"id": "turn-9", "status": "inProgress"}]
        await backend.reconcile()
        assert ent["card"] is None and "turn lost" in thread.texts()[-1]
    run(main())
    run(still_running())
    run(lost())


def test_disconnect_marks_cards_and_schedules_reconnect():
    async def main():
        bridge, thread, mod, ent, client, backend = setup()
        await backend.send(thread, ent, "go", FakeUser(1))
        bridge.closed = True                      # so the reconnect task exits immediately
        await backend.handle({"method": "x/disconnected", "params": {}})
        assert ent["card"]["status"] == "disconnected"
        assert any(not t.done() or t.done() for t in backend.tasks)
        await drain(backend)
    run(main())


# ---------- controls ----------

def test_buttons_interrupt_fast_and_kill():
    async def main():
        bridge, thread, mod, ent, client, backend = setup()
        await backend.send(thread, ent, "go", FakeUser(1))
        T = ent["codex_thread"]
        inter = FakeInteraction(FakeUser(1), channel=thread)
        await backend.on_button(inter, str(thread.id), "esc")
        assert client.called("turn/interrupt") and "interrupted by owner" in thread.texts()[-1]
        inter = FakeInteraction(FakeUser(1), channel=thread)
        await backend.on_button(inter, str(thread.id), "fast")
        assert ent["fast"] is True and client.called("thread/settings/update")[-1]["serviceTier"] == "priority"
        inter = FakeInteraction(FakeUser(1), channel=thread)
        await backend.on_button(inter, str(thread.id), "say")
        assert inter.modals and inter.modals[0].custom_id == f"xm|{thread.id}|say"
        # effort / model
        assert await backend.set_effort(thread, ent, "medium") is True and ent["effort"] == "medium"
        await backend.set_effort(thread, ent, "bogus")
        assert "efforts:" in thread.texts()[-1]
        await backend.set_model(thread, ent, FakeUser(2, "bob"), "gpt-5.6-sol")
        assert "owner-only" in thread.texts()[-1] and ent["model"] == "gpt-6-astra"
        await backend.set_model(thread, ent, FakeUser(1), "gpt-5.6-sol")
        assert ent["model"] == "gpt-5.6-sol"
        client.active_turns[T] = "turn-x"
        ent["card"] = backend.begin_turn(ent, "turn-x")
        await backend.kill(thread, ent)
        assert ent["ended"] is True and client.called("thread/archive")[0]["threadId"] == T
        assert client.called("turn/interrupt")[-1]["turnId"] == "turn-x" and ent["card"] is None
        assert await backend.send(thread, ent, "anything", FakeUser(1)) is None
        assert backend.help_text().startswith("🛰️ **Astra thread**")
    run(main())


def test_card_text_shapes():
    bridge, thread, mod, ent, client, backend = setup({"fast": True})
    import time
    card = {"turn_id": "t", "started": time.time() - 65, "counts": {"Bash": 3, "Edit": 1}, "tokens": (120000, 3000),
            "narration": "", "desc": "Fixing the test", "tool": "$ pytest -q", "open_tool": True, "tool_at": time.time() - 40,
            "thought": "", "status": "waiting"}
    text = backend.card_text(ent, card)
    assert text.startswith("🔔 **Astra needs you** · 1m 05s · 🧠 `gpt-6-astra` @ `xhigh` · ⚡ fast · 120k in / 3k out")
    assert "**Fixing the test**" in text and "└ `$ pytest -q` · running 40s" in text and "-# 🔧 Bash ×3, Edit" in text
    final = backend.card_text(ent, card, final="✅ turn done")
    assert final.startswith("-# ✅ turn done · 1m 05s · 🔧 Bash ×3, Edit · 120k in / 3k out · 🧠")
    assert view_buttons(backend.card_view("5", ent))[2][0] == "⚡ Fast: on → off"


def test_seed_home_links_auth_config_and_standalone(tmp_path):
    src = tmp_path / "dot-codex"
    (src / "packages/standalone/releases/0.154.0/bin").mkdir(parents=True)
    (src / "packages/standalone/releases/0.154.0/bin/codex").write_text("#!/bin/sh\n")
    (src / "packages/standalone/releases/0.154.0/codex").symlink_to("bin/codex")
    (src / "packages/standalone/current").symlink_to(src / "packages/standalone/releases/0.154.0")
    (src / "auth.json").write_text("{}")
    (src / "config.toml").write_text('model = "gpt-6-astra"\n')
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    (local_bin / "codex").symlink_to(src / "packages/standalone/current/bin/codex")
    home = tmp_path / "bridge-home"
    cb.seed_home(home, local_bin / "codex", src)
    assert (home / "auth.json").is_symlink() and (home / "auth.json").resolve() == (src / "auth.json").resolve()
    assert (home / "config.toml").read_text().startswith("model")
    current = home / "packages/standalone/current"
    assert current.is_symlink() and (current / "codex").exists() and (current / "bin/codex").exists()
    # idempotent, and a user edit to the copied config survives
    (home / "config.toml").write_text("changed\n")
    cb.seed_home(home, local_bin / "codex", src)
    assert (home / "config.toml").read_text() == "changed\n"
