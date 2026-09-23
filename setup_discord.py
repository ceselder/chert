#!/usr/bin/env python3
"""Build the Discord side of chert from nothing but the bot token.

  ./setup_discord.py            # interactive: invite link, create channels, write ids to .env
  ./setup_discord.py --check    # just log in and show what the bot can see
  ./setup_discord.py --guild "My Server"   # pick a server when the bot is in several

What it does
  1. Reads DISCORD_BOT_TOKEN from .env (or prompts for it, hidden) and decodes the bot's
     application id from the token itself, so it can print the exact invite URL — with the
     permissions chert needs and the `bot` + `applications.commands` scopes (slash commands
     silently fail without the second one).
  2. Logs in. If the bot is in no server yet, it prints the invite URL and waits for you to
     click it. If it's in several, it asks which one (or use --guild).
  3. Finds or creates a `chert` category with three text channels —
       #claudes      one thread per live claude (the main channel)
       #claude-chat  two-way bridge to the claude↔claude bus
       #all-claudes  ask every claude at once; a summarizer claude answers
     — sets their topics, and writes DISCORD_CHANNEL_ID / DISCORD_CHAT_CHANNEL_ID /
     DISCORD_BROADCAST_CHANNEL_ID (and DISCORD_OWNER_ID if blank) into .env.
Idempotent: run it again and it reuses what exists.

Needs: `pip install -r requirements.txt` (discord.py, python-dotenv). The bot must have the
Message Content intent enabled in the Developer Portal → Bot → Privileged Gateway Intents.
"""
import argparse
import asyncio
import base64
import getpass
import os
import re
import sys
from pathlib import Path

try:
    import discord
except ImportError:
    sys.exit("discord.py missing — run:  .venv/bin/pip install -r requirements.txt  (or ./setup.sh)")

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
CHANNELS = [
    ("claudes", "DISCORD_CHANNEL_ID",
     "🔭 chert's signalscope — one thread per live claude on the box. Reply in a thread to talk to that "
     "claude; /claude <prompt> or @chert to launch one; !help for everything."),
    ("claude-chat", "DISCORD_CHAT_CHANNEL_ID",
     "two-way bridge to the claude↔claude bus (cchat.py) — type here to talk to all listening claudes"),
    ("all-claudes", "DISCORD_BROADCAST_CHANNEL_ID",
     "🧠 ask EVERY claude at once: a plain message is fanned out, replies are collected and a summarizer "
     "claude answers here. Reply to it / !hub for follow-ups. !all <msg> = plain broadcast."),
]


def read_env():
    vals = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            m = re.match(r"^\s*([A-Z_0-9]+)\s*=\s*(.*?)\s*(#.*)?$", line)
            if m and "=" in line and not line.lstrip().startswith("#"):
                v = m.group(2).strip().strip('"').strip("'")
                vals[m.group(1)] = v
    return vals


def write_env(updates):
    """Set KEY=value lines in .env in place (keeps comments/order; appends missing keys)."""
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    done = set()
    out = []
    for line in lines:
        m = re.match(r"^\s*([A-Z_0-9]+)\s*=\s*(.*?)\s*(#.*)?$", line)
        if m and m.group(1) in updates and not line.lstrip().startswith("#"):
            comment = f"   {m.group(3)}" if m.group(3) else ""
            out.append(f"{m.group(1)}={updates[m.group(1)]}{comment}")
            done.add(m.group(1))
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in done:
            out.append(f"{k}={v}")
    ENV.write_text("\n".join(out) + "\n")
    os.chmod(ENV, 0o600)


def app_id_from_token(token):
    """Bot tokens are `base64(application_id).timestamp.hmac`; the first part is public."""
    try:
        first = token.split(".")[0]
        first += "=" * (-len(first) % 4)
        return int(base64.urlsafe_b64decode(first).decode())
    except Exception:  # noqa: BLE001
        return None


def needed_permissions():
    return discord.Permissions(
        view_channel=True, send_messages=True, send_messages_in_threads=True,
        create_public_threads=True, manage_threads=True, manage_messages=True,   # pin the board
        manage_channels=True,          # create the three channels / rename threads
        manage_webhooks=True,          # one webhook per channel: each claude posts under its own name
        embed_links=True, attach_files=True, add_reactions=True, read_message_history=True,
        use_application_commands=True, mention_everyone=False,
    )


def invite_url(app_id):
    return discord.utils.oauth_url(app_id, permissions=needed_permissions(),
                                   scopes=("bot", "applications.commands"))


async def build(token, want_guild, check_only):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    client = discord.Client(intents=intents)
    result = {}

    @client.event
    async def on_ready():
        try:
            app_id = client.user.id
            guilds = list(client.guilds)
            print(f"logged in as {client.user} (application id {app_id})")
            if not guilds:
                print("\nThe bot is not in any server yet. Invite it with this link (it has exactly the\n"
                      "permissions chert needs + the applications.commands scope for slash commands):\n\n"
                      f"  {invite_url(app_id)}\n\nwaiting up to 10 minutes for the bot to join a server…")
                for _ in range(120):
                    await asyncio.sleep(5)
                    guilds = list(client.guilds)
                    if guilds:
                        break
                if not guilds:
                    print("still not in a server — run me again after inviting it.")
                    return
            if want_guild:
                g = next((x for x in guilds if x.name == want_guild or str(x.id) == want_guild), None)
                if not g:
                    print(f"no server named/id {want_guild!r}; the bot is in: " + ", ".join(f"{x.name} ({x.id})" for x in guilds))
                    return
            elif len(guilds) == 1:
                g = guilds[0]
            else:
                print("the bot is in several servers:")
                for i, x in enumerate(guilds, 1):
                    print(f"  {i}. {x.name}  ({x.id})")
                pick = input("which one? [number] ").strip()
                g = guilds[int(pick) - 1]
            print(f"server: {g.name} ({g.id}), owner {g.owner_id}")
            if check_only:
                for name, key, _ in CHANNELS:
                    ch = discord.utils.get(g.text_channels, name=name)
                    print(f"  #{name:<12} {'exists ' + str(ch.id) if ch else 'missing'}")
                me = g.me
                missing = [p for p, v in needed_permissions() if v and not getattr(me.guild_permissions, p)]
                print("  permissions:", "ok" if not missing else "MISSING " + ", ".join(missing))
                return
            cat = discord.utils.get(g.categories, name="chert") or await g.create_category("chert")
            updates = {}
            for name, key, topic in CHANNELS:
                ch = discord.utils.get(g.text_channels, name=name)
                if ch is None:
                    ch = await g.create_text_channel(name, category=cat, topic=topic)
                    print(f"  created #{name} ({ch.id})")
                else:
                    print(f"  found   #{name} ({ch.id})")
                    if not ch.topic:
                        try:
                            await ch.edit(topic=topic)
                        except discord.HTTPException:
                            pass
                updates[key] = str(ch.id)
            env = read_env()
            if not env.get("DISCORD_OWNER_ID"):
                updates["DISCORD_OWNER_ID"] = str(g.owner_id)
            write_env(updates)
            result.update(updates)
            print("\nwrote to .env: " + ", ".join(f"{k}={v}" for k, v in updates.items()))
            print(f"\nstart the bridge and type  /claude hello  in #{CHANNELS[0][0]}  🔭")
        except discord.Forbidden as e:
            print(f"\nthe bot lacks a permission: {e}. Re-invite it with:\n  {invite_url(client.user.id)}")
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.PrivilegedIntentsRequired:
        print("\n❌ Message Content intent is OFF. Developer Portal → your app → Bot → Privileged Gateway\n"
              "   Intents → enable MESSAGE CONTENT INTENT, save, then run me again.")
    except discord.LoginFailure:
        print("\n❌ Discord rejected the token. Copy it again from Developer Portal → Bot → Reset Token.")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only log in and report; change nothing")
    ap.add_argument("--guild", help="server name or id when the bot is in several")
    ap.add_argument("--token", help="bot token (else .env / hidden prompt)")
    a = ap.parse_args()
    env = read_env()
    token = a.token or env.get("DISCORD_BOT_TOKEN") or ""
    if not token:
        token = getpass.getpass("Discord bot token (hidden; Developer Portal → Bot → Reset Token): ").strip()
        if not token:
            sys.exit("no token")
        write_env({"DISCORD_BOT_TOKEN": token})
        print("saved the token to .env (chmod 600)")
    app_id = app_id_from_token(token)
    if app_id:
        print(f"invite link (also shown if the bot turns out not to be in a server yet):\n  {invite_url(app_id)}\n")
    asyncio.run(build(token, a.guild, a.check))


if __name__ == "__main__":
    main()
