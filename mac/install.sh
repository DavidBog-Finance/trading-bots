#!/bin/bash
# Installs the always-on runner on your Mac. Run from the repo folder:
#   bash mac/install.sh
# Safe to re-run (it reloads the service). Remove with: bash mac/uninstall.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
LABEL="com.tradingagents.runner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f .env ] || { echo "Create .env first: cp .env.example .env, then fill in your keys."; exit 1; }

echo "1/4 Python environment"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v $c >/dev/null && $c -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then PY=$c; break; fi
done
[ -n "$PY" ] || { echo "Needs Python 3.10+. Install it with: brew install python@3.12"; exit 1; }
[ -d .venv ] || $PY -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if ! grep -q "^CLAUDE_BACKEND=api" .env; then
  CB="$(grep '^CLAUDE_BIN=' .env | cut -d= -f2-)"
  [ -n "$CB" ] || CB="$(command -v claude || true)"
  [ -n "$CB" ] || { echo "Claude Code not found. Install it (curl -fsSL https://claude.ai/install.sh | bash), run 'claude' once and log in with your Pro account."; exit 1; }
  grep -q '^CLAUDE_BIN=' .env || echo "CLAUDE_BIN=$CB" >> .env   # launchd doesn't see your shell PATH
  echo "  Checking Claude Code is signed in with your plan..."
  (cd /tmp && env -u ANTHROPIC_API_KEY "$CB" -p "Reply OK" --tools "" >/dev/null) || { echo "Run 'claude' once and log in with your Claude account, then re-run this."; exit 1; }
fi

echo "2/4 Offline tests"
.venv/bin/python -m pytest -q tests

echo "3/4 launchd service (starts at login, restarts if it crashes)"
mkdir -p logs "$HOME/Library/LaunchAgents"
sed -e "s#__REPO__#$REPO#g" -e "s#__LABEL__#$LABEL#g" mac/runner.plist.template > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "4/4 Keep the Mac awake on the charger (asks for your password)"
# Never sleep while plugged in; display can still turn off. Restart after a power cut.
sudo pmset -c sleep 0 disksleep 0 powernap 0 || echo "  (skipped: set System Settings > Battery > Options > Prevent automatic sleeping on power adapter)"
sudo pmset -a autorestart 1 2>/dev/null || true

echo
echo "Done. The runner is live. Logs: tail -f $REPO/logs/runner.log"
echo "Your phone gets a 'Runner online' push in a few seconds if NTFY_TOPIC is set."
echo "IMPORTANT: turn off the GitHub schedule so trades don't run twice (see the guide)."
