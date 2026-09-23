# Ideas: what would make chert cooler (research, 2026-09-02)

Ranked by how much they would change day-to-day use, with effort estimates. Sources at the end.

## A. Official Claude Code mechanisms we are not using yet

1. **Event-driven bridge via hooks** (effort M, impact HIGH). Claude Code has 30+ hook events
   with `session_id`, `cwd`, `transcript_path` in every payload. A user-level hook set that POSTs to
   a local bridge endpoint would replace the 4-second registry poll + transcript diffing:
   `Notification` (`permission_prompt`, `idle_prompt`, `elicitation_dialog`) → instant 📡 with the
   prompt; `Stop` carries `last_assistant_message` → post the reply the moment the turn ends;
   `SessionStart`/`SessionEnd` (with reason) → exact arrivals/departures instead of pid liveness
   guesses; `StopFailure` with matcher `rate_limit|overloaded` → alert (and know when to swap keys);
   `SubagentStart/Stop` → "🧭 3 subagents exploring"; `PreCompact/PostCompact` → "🌀 compacting".
2. **Real remote approvals** (effort M–H, impact HIGH). Two routes, both official:
   - `PermissionRequest` hook can return `decision: approve|deny`. A hook that blocks until a
     Discord button verdict arrives (within the hook timeout) gives approvals with the *actual*
     tool input instead of screen-scraped menu rows.
   - Build chert as a **channel** (an MCP server over stdio; Node/Bun runtime, not Python).
     Declaring `claude/channel/permission` makes Claude Code relay `permission_request`
     (`request_id`, `tool_name`, `description`, `input_preview`) and accept an `allow|deny`
     verdict; `notifications/claude/channel` pushes messages natively (no tmux paste) and a
     `reply` tool lets Claude answer into the thread itself. Channels now work with Console API
     keys. During the research preview a custom channel needs
     `--dangerously-load-development-channels server:chert` at launch.
3. **Steer background sessions over the inbox socket** (effort L–M, impact MED). Every session
   binds `/run/user/<uid>/cc-socks/<pid>.sock` (documented; path is in the registry as
   `messagingSocketPath`). Posting a message there would make `--bg` agents talkable without
   `!revive`. Caveat: sessions in bypass mode *hold* messages from non-bypass senders behind a
   dialog, so set `crossSessionInbound: "accept"` in user settings first. Wire format needs a
   short probe (binary shows `{"type":"auth","token":…}` then a message frame with
   `from_session_id` + `content`).
4. **Idle notices** (effort L). `SendMessage(notify_when_idle=true)` gives one exact "finished"
   notice per session, no polling. A sentinel session, or the socket route above, could subscribe
   on every traveler and turn it into "✅ done" pings.
5. **Agent teams for Feldspar** (effort L). `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` lets the
   Feldspar lead spawn teammates (independent sessions, own contexts) that show up as travelers
   in Discord, instead of invisible subagents. `TeammateIdle` hook can keep them working.
6. **Elicitation relay** (effort L). The `Elicitation` hook can *answer* MCP servers' questions;
   route them to Discord like permission prompts.

## B. Borrowed from community bots and orchestrators

7. **Multi-machine hub** (effort M, impact HIGH for us). `claudecode-discord` runs one bot per
   machine into one server so the channel list is a fleet dashboard. We already run the same
   code on more than one machine: add a `MACHINE` label, one channel per machine, and
   `!sessions` / the dashboard showing all boxes (cluster login nodes too).
8. **Per-session message queue** (effort L–M). When a claude is exploring, queue Discord
   messages and deliver on idle instead of steering mid-turn; `!queue`, `!queue clear`.
9. **Typed answers for "Other"** (effort L). A Discord Modal opened from a button so free-text
   answers to AskUserQuestion / permission "tell Claude what to do differently" get typed in.
10. **`!usage`** (effort L). Tokens and cost per session from the transcript `usage` fields and
    `.claude.json` `lastCost`; a spend column on the dashboard and a daily digest.
11. **Worktree isolation + `!diff` / `!pr`** (effort M). Claude Squad / Conductor give each agent a
    git worktree. `/claude --worktree` for parallel claudes on one repo; `!diff` posts the diff,
    `!pr` opens the PR.
12. **Voice** (effort H, fun). `discord-vault-bot`: join a voice channel, Silero VAD + Whisper
    (Groq free tier or local) → text into the session; Edge TTS reads final replies back.
13. **Live board** (effort L). A pinned message in the main channel, edited every minute: travelers
    grouped 📡 needs you / 🔭 exploring / 🔥 campfire, like Vibe Kanban's columns.
14. **Scheduled sweeps** (effort L). Cloud Routines exist but push to `claude/` branches; a local
    `!every 24h feldspar <dir>` via systemd timers gives a nightly Feldspar sweep and a morning
    digest of all travelers.

## C. Outer Wilds flavour

15. **Hearthian names** for unnamed sessions (Esker, Gabbro, Riebeck, Hornfels, Slate, Gossan,
    Tektite, Marl, Moraine, Spinel, Tuff, Rutile, Porphy, Arkose, Galena, Tephra, Mica),
    reassigned per boot.
16. **Ship log**: `!log` builds a session's timeline of decisions from the transcript (one Haiku
    pass), rendered like the ship's log with rumor links between related sessions.
17. **Supernova timer**: a `--bg` or headless job with a hard deadline gets a countdown in its
    thread and a warning at T-2 min.

## What not to do
- Don't switch to the official Discord *channel plugin* as-is: it is DM-first and one bot per
  session (Bun required). Thread-per-session is better; building chert as its own channel server
  (item 2) keeps that and gains native delivery + permission relay.

## Sources
- Hooks reference: https://code.claude.com/docs/en/hooks
- Channels: https://code.claude.com/docs/en/channels and https://code.claude.com/docs/en/channels-reference
- Cross-session messaging (inbox socket, notify_when_idle): https://code.claude.com/docs/en/cross-session-messaging
- Agent teams: https://code.claude.com/docs/en/agent-teams · Routines: https://code.claude.com/docs/en/routines
- claudecode-discord (multi-machine hub, approval buttons, queue): https://github.com/chadingTV/claudecode-discord
- discord-vault-bot (voice pipeline): https://github.com/matzek-systems/discord-vault-bot
- Claude-Code-Remote (email/Telegram control): https://github.com/JessyTsui/Claude-Code-Remote
- Orchestrator roundups: https://www.augmentcode.com/tools/open-source-agent-orchestrators ·
  https://nimbalyst.com/blog/best-agent-management-tools-2026/
