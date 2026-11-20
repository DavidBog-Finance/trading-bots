#!/usr/bin/env bash
# Runs Portfolio 1k 24/7 on any Linux computer (Ubuntu/Debian/Fedora...) as a systemd service.
# Usage, from the repo folder:  bash portfolio1k/deploy/install-linux.sh
# Before: fill in .env (copy .env.example) and sign in to Claude Code once by running `claude`.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(pwd)"
[ -f .env ] || { echo "Create .env first: cp .env.example .env, then add your Alpaca paper keys."; exit 1; }
command -v python3 >/dev/null || { echo "Install Python 3.11+ first."; exit 1; }
command -v claude >/dev/null || echo "Note: Claude Code not found. Install it (npm i -g @anthropic-ai/claude-code) and run 'claude' once, or the agent trades on rules only."
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m pytest -q tests
mkdir -p logs ~/.config/systemd/user
cat > ~/.config/systemd/user/portfolio1k.service <<UNIT
[Unit]
Description=Portfolio 1k options agent
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$REPO
Environment=CLAUDE_BIN=$(command -v claude || echo "")
ExecStart=$REPO/.venv/bin/python -m portfolio1k run
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now portfolio1k
loginctl enable-linger "$USER" || true   # keep running after you log out and after reboots
echo
echo "Running. Logs: journalctl --user -u portfolio1k -f"
echo "Stop: systemctl --user disable --now portfolio1k"
echo "Now set the GitHub repository variable RUNNER=box so GitHub stops trading the same account."
