"""Live check of the Astra backend against the REAL codex app-server daemon, with a fake Discord.

    .venv/bin/python tests/live_astra.py            # ~8 tiny turns at low effort

Uses the bridge's CODEX_HOME (~/.codex-bridge, or CODEX_BRIDGE_HOME) and its ChatGPT login,
starts the daemon if needed, and drives codex_backend.AstraBackend with the fake thread/bridge
from tests/fakes.py — nothing touches the real bot, bot_state.json or Discord. What it checks:
a turn that answers PLUM; steering a running turn; interrupting one; a bridge "restart" mid-turn
(client closed, new backend reconciles and still gets the reply); resuming an existing thread id
from a fresh backend; an approval request answered through its button; model/effort/fast
settings; kill. Prints the fake thread's transcript at the end.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_bot as mod                                      # noqa: E402  (real helpers, fake state)
import codex_backend as cb                                     # noqa: E402
from fakes import FakeBridge, FakeChannel, FakeInteraction, FakeMessage, FakeThread, FakeUser  # noqa: E402

CWD = Path("/tmp/codex-probe-live")
CWD.mkdir(exist_ok=True)
mod.state = {"_meta": {}}
mod.save_state = lambda: None                                  # never write bot_state.json from here
owner = FakeUser(1, "celeste")


def ent_for(thread, **extra):
    e = {"codex_thread": None, "backend": "app-server", "cwd": str(CWD), "sandbox": "danger-full-access",
         "effort": "low", "model": "gpt-6-astra", "name": "live-test", "created": time.time(), "turns": 0,
         "ended": False, "by": "celeste", **extra}
    mod.state.setdefault("_astra", {})[str(thread.id)] = e
    return e


async def until(pred, timeout=120, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def replies(bridge, thread):
    return [c for tid, _n, c, _b in bridge.posted if tid == thread.id and "Astra" not in c[:14]]


def card_lines(thread):
    """Collapsed card recaps: edits of the card message, or a bare line when the turn was too quick for a card."""
    out = [c for mid, (c, v) in thread.edits.items() if c and c.startswith("-# ")]
    return out + [c for c in thread.texts() if c.startswith("-# ") and "turn" in c]


async def new_backend(bridge):
    backend = cb.AstraBackend(bridge, mod)
    backend.start()
    await backend.ensure_client()
    return backend


async def main():
    bridge = FakeBridge(owner=1)
    parent = FakeChannel(name="claudes")
    thread = FakeThread(parent, "🛰️ astra · live")
    bridge.register(parent, thread)
    ent = ent_for(thread)
    backend = await new_backend(bridge)
    print("daemon:", cb.daemon_status())

    # 1. a plain turn
    m1 = FakeMessage(thread, "reply with the word PLUM", owner)
    how = await backend.send(thread, ent, m1.content, owner, react_msg=m1)
    print("send #1 ->", how, "codex thread", ent["codex_thread"])
    await until(lambda: ent["turns"] == 1, 90, "PLUM turn")
    await asyncio.sleep(1)
    assert any("PLUM" in c for c in replies(bridge, thread)), replies(bridge, thread)
    assert thread.reactions[m1.id] == ["✅"], thread.reactions
    print("PLUM turn: PASS; card recap:", card_lines(thread)[-1])

    # 2. steer a running turn
    m2 = FakeMessage(thread, "Run the shell command `sleep 20` and then reply with the single word DONE. Nothing else.", owner)
    assert await backend.send(thread, ent, m2.content, owner, react_msg=m2) == "started"
    await until(lambda: ent.get("card") and "sleep 20" in (ent["card"].get("tool") or ""), 60, "the sleep command on the card")
    await backend.render_card(str(thread.id), ent, force=True)
    print("card mid-turn:\n   " + ent["card"]["body"].replace("\n", "\n   "))
    m3 = FakeMessage(thread, "After the sleep, reply with the word GRAPE instead of DONE.", owner)
    how = await backend.send(thread, ent, m3.content, owner, react_msg=m3)
    print("send mid-turn ->", how, "receipt", thread.reactions.get(m3.id))
    assert how == "steered"
    await until(lambda: ent["turns"] == 2, 120, "steered turn")
    await asyncio.sleep(1)
    last = replies(bridge, thread)[-1]
    print("steered reply:", last, "| receipts:", thread.reactions[m2.id], thread.reactions[m3.id])
    assert "GRAPE" in last and thread.reactions[m3.id] == ["✅"]

    # 3. interrupt
    m4 = FakeMessage(thread, "Run the shell command `sleep 40` then reply SLOW.", owner)
    await backend.send(thread, ent, m4.content, owner, react_msg=m4)
    await until(lambda: ent.get("card") and "sleep 40" in (ent["card"].get("tool") or ""), 60, "sleep 40 on the card")
    inter = FakeInteraction(owner, channel=thread)
    await backend.on_button(inter, str(thread.id), "esc")
    await until(lambda: ent.get("card") is None, 30, "interrupted turn to finish")
    print("interrupt:", inter.sent, "| recap:", card_lines(thread)[-1], "| receipt:", thread.reactions[m4.id])
    assert "⏹ interrupted" in card_lines(thread)[-1] and thread.reactions[m4.id] == ["❌"]

    # 4. bridge restart mid-turn: drop the client, new backend reconciles, reply still arrives once
    m5 = FakeMessage(thread, "Run the shell command `sleep 12` then reply with the word MANGO.", owner)
    await backend.send(thread, ent, m5.content, owner, react_msg=m5)
    await until(lambda: ent.get("active_turn"), 30, "turn start")
    for t in backend.tasks:
        t.cancel()
    await backend.client.close()
    print("client closed mid-turn; active_turn", ent["active_turn"], "card msg", ent["card"]["msg"])
    backend2 = await new_backend(bridge)
    await backend2.reconcile()
    print("after reconcile: card status", ent["card"] and ent["card"]["status"])
    await until(lambda: ent["turns"] == 3, 120, "MANGO after restart")
    await asyncio.sleep(1)
    mango = [c for c in replies(bridge, thread) if "MANGO" in c]
    print("MANGO replies:", len(mango), "| receipt:", thread.reactions[m5.id], "| recap:", card_lines(thread)[-1])
    assert len(mango) == 1 and thread.reactions[m5.id] == ["✅"]
    backend = backend2

    # 5. resume an existing thread id from a brand-new backend/entry (as after a state migration:
    #    the Discord thread is new, the codex thread id is the old one; the old entry is gone)
    thread_b = FakeThread(parent, "🛰️ astra · resumed")
    bridge.register(thread_b)
    main_entry = mod.state["_astra"].pop(str(thread.id))
    ent_b = ent_for(thread_b, codex_thread=ent["codex_thread"])
    for t in backend.tasks:
        t.cancel()
    await backend.client.close()
    backend = await new_backend(bridge)
    m6 = FakeMessage(thread_b, "What fruit words did I ask you to reply with so far in this conversation? List them.", owner)
    how = await backend.send(thread_b, ent_b, m6.content, owner, react_msg=m6)
    await until(lambda: ent_b["turns"] == 1, 90, "resumed turn")
    await asyncio.sleep(1)
    r = replies(bridge, thread_b)[-1]
    print("resumed ->", how, "| reply:", r.replace("\n", " ")[:200])
    assert ent_b["codex_thread"] == ent["codex_thread"] and not any("couldn't resume" in t for t in thread_b.texts())
    assert "PLUM" in r.upper() and "GRAPE" in r.upper()
    mod.state["_astra"].pop(str(thread_b.id))
    mod.state["_astra"][str(thread.id)] = main_entry
    resolved = []
    orig_on = backend.client.on_message

    def spy_on(data):
        if data.get("method") == "serverRequest/resolved":
            resolved.append(data)
        return orig_on(data)
    backend.client.on_message = spy_on

    # 6. an approval request answered via its button (read-only sandbox makes codex ask)
    thread_c = FakeThread(parent, "🛰️ astra · approvals")
    bridge.register(thread_c)
    ent_c = ent_for(thread_c, sandbox="read-only")
    target = CWD / "hello.txt"
    if target.exists():
        target.unlink()
    m7 = FakeMessage(thread_c, f"Create the file {target} containing the word hi, using a shell command. Then reply OK.", owner)
    await backend.send(thread_c, ent_c, m7.content, owner, react_msg=m7)
    await until(lambda: backend.requests, 90, "an approval request")
    key, info = next(iter(backend.requests.items()))
    body, buttons = thread_c.log[-1][2], thread_c.log[-1][3]
    print("approval request:\n   " + body.replace("\n", "\n   "))
    print("buttons:", buttons)
    assert buttons[0][1] == f"xa|{key}|0" and "Allow once" in buttons[0][0]
    await asyncio.sleep(0.5)
    await backend.render_card(str(thread_c.id), ent_c, force=True)
    assert "needs you" in ent_c["card"]["body"], ent_c["card"]["body"]
    request_msg = next(m for m in thread_c.sends if m.id == info["msg"])
    inter = FakeInteraction(owner, message=request_msg, channel=thread_c)
    await backend.on_approval(inter, key, "0")
    await until(lambda: ent_c["turns"] == 1, 90, "approved turn")
    print("after approval:", thread_c.edits.get(request_msg.id, ("?",))[0], "| file:", target.read_text() if target.exists() else None)
    assert thread_c.edits[request_msg.id][0].startswith("-# ✅ Allow once — celeste")
    print("serverRequest/resolved frames:", resolved)
    backend.client.on_message = orig_on
    assert target.exists() and target.read_text().strip() == "hi"

    # 7. settings: effort, model, fast
    assert await backend.set_effort(thread, ent, "medium") is True
    await backend.set_model(thread, ent, owner, "gpt-6-astra")
    ent["fast"] = True
    await backend.set_tier(ent)
    calls = []
    orig = backend.client.call

    async def spy(method, params, timeout=None):
        calls.append((method, params))
        return await orig(method, params, timeout)
    backend.client.call = spy
    m8 = FakeMessage(thread, "reply with the word PEAR", owner)
    await backend.send(thread, ent, m8.content, owner, react_msg=m8)
    ts = [p for m, p in calls if m == "turn/start"][0]
    print("turn/start params:", {k: v for k, v in ts.items() if k != "input"})
    assert ts["effort"] == "medium" and ts["serviceTier"] == "priority"
    await until(lambda: ent["turns"] == 4, 90, "PEAR turn")
    backend.client.call = orig

    # 8. kill
    await backend.kill(thread, ent)
    assert ent["ended"] and await backend.send(thread, ent, "x", owner) is None
    print("kill: PASS")

    print("\n===== fake Discord transcript of the main thread =====")
    for e in thread.log:
        if e[0] in ("send", "post"):
            print(f"[{e[0]}] {str(e[2])[:300]!r}" + (f" buttons={[l for l, _ in e[3]]}" if e[3] else ""))
        elif e[0] == "edit":
            print(f"[edit {e[1]}] {str(e[2])[:200]!r}")
        elif e[0] in ("react+", "react-"):
            print(f"[{e[0]} {e[1]}] {e[2]}")
    print("errors logged:", getattr(mod, "_ERR_COUNT", None))
    for t in backend.tasks:
        t.cancel()
    await backend.client.close()
    print("\nALL LIVE CHECKS PASSED")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    asyncio.run(main())
