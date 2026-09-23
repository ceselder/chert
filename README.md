<p align="center">
  <img src="static/chert.svg" width="240" alt="Chibi Chert: a round little astronaut with a big gold helmet and four glowing eyes, drumming at dusk">
</p>

<h1 align="center">chert 🔭</h1>

<p align="center"><i>Run your claudes from Discord. "Claude tag at home"</i></p>

chert is a Discord bot that runs on the machine where your Claude Code sessions live. Each
session gets its own thread: you can read what it's doing and type to it from your phone. You can
also start new sessions, fork them, restart them and change their model without opening a
terminal.

chert doesn't modify Claude Code. It reads the session files and transcripts Claude Code already
writes, receives Claude Code hook events, and types into sessions through tmux.

## What it does

- Every Claude Code session on the machine gets a thread in `#claudes`. The session's replies are
  posted there, and anything you write in the thread is typed into the session.
- While a session is working, one message per turn is edited in place with the current step, the
  command it's running, its latest thought, tool counts, elapsed time and the model.
- Permission prompts and menus appear as buttons.
- Slash commands start, fork, rename and unstick sessions, and change their model, effort or
  permission mode.
- A message in `#all-claudes` goes to every live session. A summarizer session collects the
  replies and posts one answer.
- Images a session opens are posted to its thread. `hearth-send <file>` posts any other file.
- After a reboot, sessions that were running come back in their old threads.
- Optional: code review by a Claude and a GPT reviewer (Feldspar), GPT sessions as threads
  (Astra), S3 backups, and a web dashboard.

## Requirements

- Linux with systemd (tested on Ubuntu 24.04)
- Claude Code, installed and logged in
- tmux and Python 3.11 or newer
- A Discord server you can add a bot to
- Optional: the [Codex CLI](https://github.com/openai/codex) for Feldspar and Astra, and an S3
  bucket for backups

## Install

1. Create the bot at <https://discord.com/developers/applications>: **New Application**, then on
   the **Bot** page click **Reset Token** and copy the token, enable **Message Content Intent**, and
   save.
2. On the machine:

   ```bash
   git clone https://github.com/ceselder/chert.git
   cd chert
   ./setup.sh
   ```

   `setup.sh` asks for the token and prints an invite link. Once you've added the bot to your
   server, it creates a `chert` category with `#claudes`, `#all-claudes` and `#claude-chat`,
   writes the channel ids to `.env`, installs the Claude Code hooks and two command-line tools,
   and starts three systemd services. You can run it again safely. `--dry-run` shows what it would
   do; `--no-systemd` skips the services.
3. In `#claudes`, run `/claude hello`.

## Slash commands

In a session's thread:

| Command | What it does |
|---|---|
| `/refresh [message] [force]` | Unstick the session. Sends Esc first and restarts the session in place (history kept) only if it stays stuck. `message` is sent once it responds. If the session is idle and was active in the last five minutes, it isn't treated as stuck: only `message` is sent, unless `force` is set. |
| `/screen` | Show the terminal screen. |
| `/key <key>` | Press a key: `esc`, `enter`, arrow keys, `tab`, `shift-tab`, `space`, `pgup`, `pgdn`, `1`–`9`. |
| `/fork [message] [to]` | Copy the session and its history into a new session with its own thread. With `to: astra`, the conversation goes to a GPT session instead. |
| `/rename <name>` | Rename the session. The thread title follows. |
| `/effort <level>` | Set reasoning effort: `low`, `medium`, `high`, `xhigh` or `max`. |
| `/model <name>` | Switch the model: `fable`, `opus`, `sonnet`, `haiku` or a full model id. If the session is busy, the switch waits until it's idle. Owner only. |
| `/fast [mode] [everywhere]` | Turn fast mode `on` or `off` for this session and show what Claude Code answered (fast mode draws from usage credits, and Claude Code says so if they aren't available). `everywhere` applies it to every live session and makes it the default for new ones. Owner only. |
| `/mode <mode>` | Switch permission mode: `auto`, `bypass`, `plan` or `default`. In `auto` mode, a classifier can block actions; `bypass` turns it off for this session. |
| `/restart [force]` | Restart the session in place (history kept), for example to pick up new settings. `force` also restarts it while it's busy. |
| `/revive [mode]` | Bring back an ended, crashed or background session. `force` also stops a busy copy; `fork` keeps the original running. |
| `/log [count]` | Show a timeline of prompts, replies and tool runs. |
| `/mute`, `/unmute` | Stop or resume updates in this thread. |
| `/supernova [minutes] [then] [cancel]` | Start a countdown (22 minutes by default). At zero the session is told to wrap up and report; `then` can interrupt its turn first or end the session. |
| `/kill [how]` | End the session and archive the thread. `hard` also kills its tmux pane; `delete` also deletes the thread. |
| `/feldspar [focus]` | Have two reviewers check this session's project. See [Feldspar](#feldspar). |

Anywhere:

| Command | What it does |
|---|---|
| `/claude <prompt> [project]` | Start a new session and open its thread. `project` is a directory under `PROJECT_ROOT`. |
| `/resume <session>` | Search every session the machine has had (with autocomplete) and bring one back into a thread. |
| `/sessions` | List live sessions with links to their threads. |
| `/astra <prompt> [project]` | Start a GPT session in its own thread. See [Astra](#astra). |
| `/globalmodel <name>` | Switch every live session to a model, and make it the default for new ones. Owner only. |
| `/yolo <duration>` | Start new sessions with permission checks off for a while (`30m`, `1h`, at most `12h`), or `off`. |
| `/help` | List all commands. |

For all sessions at once (the output is posted in `#claudes`):

| Command | What it does |
|---|---|
| `/all <message>` | Send a message to every live session. |
| `/restartall [force]` | Restart every idle session in place, one at a time. |
| `/reviveall` | Bring back every session that died in a reboot or crash. |
| `/cleanup [delete]` | Archive (or delete) the threads of ended sessions. |
| `/disk` | Show free disk space and the largest directories that haven't been touched in a while. |
| `/backup`, `/s3` | Back up transcripts and config to S3 now, or show what's in the bucket. Needs `S3_BUCKET`. |
| `/offload <directory> [confirm]` | Copy a directory to S3, check every file, then delete the local copy. Without `confirm` it only shows the plan. Needs `S3_BUCKET`. |
| `/restore <directory> [confirm]` | Bring an offloaded directory back from S3. |

In `#all-claudes`, `/hub <message>` talks to the summarizer directly (see [Channels](#channels)).

`/model`, `/globalmodel` and `/fast` only work for the owner (`DISCORD_OWNER_ID`, or the server owner).
Discord shows `/model`, `/globalmodel`, `/fast`, `/yolo`, `/restartall`, `/cleanup`, `/offload` and
`/restore` only to admins unless you change that in the server's integration settings.

Every command also works as text in the same place: `!screen`, `!kill hard`, `!restart all`,
and so on.

## Channels

**`#claudes`** has one thread per session and a pinned message listing all sessions and their
status.

**`#all-claudes`** is for asking every session the same thing. When you post a message, every
live session receives it. chert collects their replies for up to four minutes, saves them to a
file, and passes the file to a summarizer session called `all-claudes-hub`, whose answer appears
in the channel. The summarizer stays running between questions and can follow up with individual
sessions. To talk to the summarizer directly, reply to one of its messages or use `/hub`. To send a message to
every session without a summary, start it with `!all`.

**`#claude-chat`** mirrors a shared message file (`CHAT_LOG`) that sessions can append to if you
want them to talk to each other.

## Feldspar

`/feldspar [focus]` in a session's thread (or writing "let feldspar look into this") starts two
independent reviews of that session's project:

- a Claude session at maximum effort, in its own thread;
- GPT via `codex exec` in a read-only sandbox.

Both write to `REPORTS_DIR/feldspar-<name>-<time>/`. When the GPT review is done, the Claude
reviewer checks its findings and merges them into its own `report.md`. This needs the Codex CLI,
logged in with `codex login`.

## Astra

`/astra <prompt>` opens a thread connected to a Codex session (`gpt-6-astra` by default). Each
message you write in the thread is one turn. `!effort <level>` sets its reasoning effort (up to
`ultra`) and `/kill` ends it. In a Claude session's thread, `/fork to: astra` hands that conversation to a new
Astra session.

## How it works

- For each running session, Claude Code writes a file to `~/.claude/sessions/` (process id, tmux
  pane, name, status) and a transcript to `~/.claude/projects/`. chert checks these every few
  seconds. The Claude Code hooks installed by `setup.sh` report events immediately.
- New transcript lines become messages in the session's thread and update its progress message.
- Your messages are pasted into the session's tmux pane. Sessions without a tmux pane are reached
  through their messaging socket.
- Sessions chert starts run in a tmux session named `0`, which the `chert-tmux` service keeps
  running.
- The mapping between sessions and threads is saved in `bot_state.json`.

## Configuration

Settings are environment variables in `.env`. `.env.example` lists all of them with their
defaults; `setup.sh` fills in the Discord ones. Settings you may want to change:

| Variable | Default | What it's for |
|---|---|---|
| `DASHBOARD_URL` | `http://127.0.0.1:8899/claudes` | Where you serve the dashboard, so its links work from Discord. |
| `PROJECT_ROOT` | your home directory | Where `/claude … project` looks for directories. |
| `SPAWN_FLAGS` | `--allow-dangerously-skip-permissions --effort xhigh` | Flags for sessions chert starts. |
| `SPAWN_ALLOW_USERS` | anyone in the server | Comma-separated Discord user ids allowed to start sessions. |
| `DISCORD_OWNER_ID` | the server owner | Who gets pinged and who can use `/model` and `/globalmodel`. |
| `REPORTS_URL` | none | A web server that serves `REPORTS_DIR`. Without it, reports are linked by file path. |
| `S3_BUCKET` | none | Turns on backups and offloading. |

## Running it

- `setup.sh` installs three services: `chert-tmux`, `chert-discord-bridge`, and `chert-checkin`
  (the dashboard, on `127.0.0.1:8899`). To follow the bot's log: `journalctl -u chert-discord-bridge -f`.
- To update: `git pull && sudo systemctl restart chert-discord-bridge`. Running sessions are not
  affected, but an Astra turn or the GPT half of a Feldspar review that is in progress is stopped.
- The dashboard has no login and can type into sessions. Keep it on localhost, or put it behind
  nginx with basic auth or behind `dashboard_proxy.py`.

## Security

- Anyone who can post in a session's thread can type into that session. Keep the server private
  and set `SPAWN_ALLOW_USERS`.
- Sessions chert starts get `--allow-dangerously-skip-permissions` so that `/mode bypass` works.
  Remove it from `SPAWN_FLAGS` if you don't want that to be possible.
- `.env` contains the bot token. `setup.sh` creates it with mode 600, and git ignores it.
- Don't put API keys in `CLAUDE.md`: its contents end up in every session's transcript.

## Troubleshooting

- **The bot is online but doesn't respond.** Message Content Intent is turned off.
- **Slash commands don't appear.** The bot was invited without the `applications.commands`
  scope. Run `./setup_discord.py` to get a correct invite link.
- **`/claude` says the session never registered.** Either the tmux session `0` isn't running
  (`systemctl status chert-tmux`), `claude` isn't on the service's `PATH` (set `CLAUDE_BIN`), or
  Claude Code is showing a login screen (`tmux attach`).
- **Codex reviews fail with `bwrap: loopback: Failed RTM_NEWADDR`.** Ubuntu 24.04 restricts user
  namespaces, which Codex's sandbox needs. `sandbox/bwrap-userns-restrict` explains the fix. Until
  then chert runs Codex without its sandbox, unless you set `FELDSPAR_CODEX_UNSANDBOXED_FALLBACK=0`.
- **A thread stopped updating.** Try `/refresh`. A session that was restarted outside chert is
  picked up again in its old thread.

## Files

| File | What it is |
|---|---|
| `discord_bot.py` | The bot. |
| `app.py` | The dashboard, and the transcript parser the bot uses. |
| `ash_twin.py` | S3 backup and offloading. |
| `setup.sh`, `setup_discord.py` | The installer. |
| `hooks/` | The Claude Code hooks. |
| `bin/hearth-send` | Posts a file into the thread of the session you run it from. |
| `bin/model-check` | Shows which model each session actually used, turn by turn. |
| `systemd/templates/` | Service files that `setup.sh` fills in. |
| `dashboard_proxy.py` | A basic-auth proxy for the dashboard. |

## License

MIT, see [`LICENSE`](LICENSE). Chert is a character from
[Outer Wilds](https://www.mobiusdigitalgames.com/outer-wilds.html) by Mobius Digital. The drawing
in `static/` is fan art made for this project. chert is not affiliated with Mobius Digital or
Anthropic.
