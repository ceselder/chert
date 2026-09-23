#!/usr/bin/env bash
# chert — one-shot installer.  ./setup.sh [--skip-discord] [--no-systemd] [--dry-run]
#
# Does, in order (each step is idempotent, re-run freely):
#   1. checks prerequisites (python3 ≥ 3.11, tmux, claude; codex/modal/aws optional)
#   2. creates .venv and installs requirements.txt
#   3. creates .env from .env.example if missing, asks for the Discord bot token (hidden)
#   4. builds the Discord side: invite link, #claudes / #claude-chat / #all-claudes, ids → .env
#      (setup_discord.py; skip with --skip-discord if you already filled DISCORD_CHANNEL_ID)
#   5. installs the CLI helpers into ~/.local/bin (hearth-send, model-check)
#   6. installs Claude Code hooks into ~/.claude/settings.json (event-driven updates)
#   7. renders systemd units for YOUR user/paths from systemd/templates and enables them:
#      chert-tmux (the tmux server claudes live in), chert-discord-bridge, chert-checkin (dashboard)
#      — needs sudo; with --no-systemd (or no sudo) it writes them to systemd/rendered/ and tells
#      you what to copy.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
APP="$(pwd)"; ME="$(id -un)"; HOME_DIR="$HOME"; VENV="$APP/.venv"
SKIP_DISCORD=0; NO_SYSTEMD=0; DRY=0
for a in "$@"; do case "$a" in --skip-discord) SKIP_DISCORD=1;; --no-systemd) NO_SYSTEMD=1;; --dry-run) DRY=1;; -h|--help) sed -n '2,17p' "$0"; exit 0;; esac; done
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  ✓ %s\n' "$*"; }
warn(){ printf '  ⚠ %s\n' "$*"; }
run() { if (( DRY )); then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

say "1/7 prerequisites"
PY="$(command -v python3.12 || command -v python3 || true)"
[[ -n "$PY" ]] || { echo "python3 not found"; exit 1; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || { echo "need python ≥ 3.11 (found $("$PY" -V))"; exit 1; }
ok "python: $("$PY" -V 2>&1) at $PY"
command -v tmux >/dev/null && ok "tmux: $(tmux -V)" || { echo "  ✗ tmux missing — install it (apt install tmux) and re-run"; exit 1; }
if command -v claude >/dev/null; then ok "claude: $(claude --version 2>/dev/null | head -1)"; else warn "claude not on PATH — install Claude Code first (npm i -g @anthropic-ai/claude-code); the bridge can still start and will watch for sessions"; fi
command -v codex >/dev/null && ok "codex: $(codex --version 2>/dev/null | head -1)  (Feldspar's second reviewer + Astra sessions)" || warn "codex not found — optional (Feldspar/Astra need it): npm i -g @openai/codex"
command -v modal >/dev/null && ok "modal cli found (optional)" || true
command -v aws  >/dev/null && ok "aws cli found (optional, Ash Twin S3 backups)" || true

say "2/7 python venv"
[[ -d "$VENV" ]] || run "$PY" -m venv "$VENV"
run "$VENV/bin/pip" install -q --upgrade pip
run "$VENV/bin/pip" install -q -r requirements.txt
ok "venv ready at $VENV"

say "3/7 .env"
if [[ ! -f .env ]]; then run cp .env.example .env; run chmod 600 .env; ok "created .env from .env.example"; else ok ".env exists (left as is)"; fi
if (( ! DRY )) && ! grep -qE '^DISCORD_BOT_TOKEN=.+' .env; then
  echo "  Create a bot: https://discord.com/developers/applications → New Application → Bot →"
  echo "  Reset Token (copy it) → Privileged Gateway Intents → enable MESSAGE CONTENT INTENT → Save."
  read -rsp "  paste the bot token (hidden): " TOKEN; echo
  [[ -n "$TOKEN" ]] || { echo "no token; re-run when you have one"; exit 1; }
  "$PY" - "$TOKEN" <<'PY'
import re,sys,os
p=".env"; t=sys.argv[1]; s=open(p).read()
s=re.sub(r"^DISCORD_BOT_TOKEN=.*$", f"DISCORD_BOT_TOKEN={t}", s, flags=re.M) if re.search(r"^DISCORD_BOT_TOKEN=", s, re.M) else s+f"\nDISCORD_BOT_TOKEN={t}\n"
open(p,"w").write(s); os.chmod(p,0o600)
PY
  ok "token saved (chmod 600)"
fi
grep -q '^PYTHONUNBUFFERED=1' .env || echo 'PYTHONUNBUFFERED=1' >> .env

say "4/7 Discord: channels + ids"
if (( SKIP_DISCORD )); then ok "skipped (--skip-discord)"
elif (( DRY )); then echo "  [dry-run] would run: $VENV/bin/python setup_discord.py"
elif grep -qE '^DISCORD_CHANNEL_ID=[0-9]+' .env; then ok "DISCORD_CHANNEL_ID already set — run ./setup_discord.py --check to verify, or ./setup_discord.py to (re)create channels"
else "$VENV/bin/python" setup_discord.py; fi

say "5/7 CLI helpers → ~/.local/bin"
mkdir -p "$HOME_DIR/.local/bin"
for t in bin/*; do run install -m 755 "$t" "$HOME_DIR/.local/bin/$(basename "$t")"; ok "$(basename "$t")"; done
case ":$PATH:" in *":$HOME_DIR/.local/bin:"*) ;; *) warn "add ~/.local/bin to your PATH (e.g. in ~/.bashrc): export PATH=\"\$HOME/.local/bin:\$PATH\"";; esac

say "6/7 Claude Code hooks (event-driven bridge)"
if (( DRY )); then echo "  [dry-run] would run hooks/install_hooks.py"; else "$VENV/bin/python" hooks/install_hooks.py && ok "hooks installed into ~/.claude/settings.json (new sessions pick them up)"; fi

say "7/7 systemd units"
mkdir -p systemd/rendered
for tpl in systemd/templates/*.service.in; do
  unit="$(basename "${tpl%.in}")"
  sed -e "s|@USER@|$ME|g" -e "s|@HOME@|$HOME_DIR|g" -e "s|@APP@|$APP|g" -e "s|@VENV@|$VENV|g" "$tpl" > "systemd/rendered/$unit"
done
ok "rendered $(ls systemd/rendered | wc -l) units for user $ME at $APP → systemd/rendered/"
if (( NO_SYSTEMD || DRY )) || ! command -v systemctl >/dev/null; then
  echo "  install them yourself:"; echo "    sudo cp systemd/rendered/*.service /etc/systemd/system/ && sudo systemctl daemon-reload"
  echo "    sudo systemctl enable --now chert-tmux chert-discord-bridge chert-checkin"
elif sudo -n true 2>/dev/null || [[ -t 0 ]]; then
  sudo cp systemd/rendered/*.service /etc/systemd/system/ && sudo systemctl daemon-reload
  sudo systemctl enable --now chert-tmux chert-discord-bridge chert-checkin
  sleep 3
  for u in chert-tmux chert-discord-bridge chert-checkin; do printf '  %-22s %s\n' "$u" "$(systemctl is-active "$u")"; done
  echo "  logs:  journalctl -u chert-discord-bridge -f"
else
  warn "no sudo — units are in systemd/rendered/; copy them as shown above"
fi

say "done 🔭"
echo "  • in Discord, #claudes:  /claude hello   (or @mention the bot) → a claude appears as a thread"
echo "  • !help in any thread lists every command; README.md has the full tour"
echo "  • dashboard (loopback): http://127.0.0.1:8899/claudes  — put it behind auth before exposing it"
