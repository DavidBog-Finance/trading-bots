#!/bin/bash
# Installs the bots + agents as a 24/7 service on the Ubuntu server, then the home services.
#   bash linux/install.sh          (run as your normal user, from the repo folder)
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
[ -f .env ] || { echo "Create .env first: cp .env.example .env, then fill in your keys."; exit 1; }

echo "1/5 Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "2/5 Claude Code on your Pro plan"
if ! grep -q "^CLAUDE_BACKEND=api" .env; then
  CB="$(grep '^CLAUDE_BIN=' .env | cut -d= -f2- || true)"
  [ -n "$CB" ] || CB="$(command -v claude || true)"
  [ -n "$CB" ] || { echo "Install Claude Code (curl -fsSL https://claude.ai/install.sh | bash), run 'claude' and log in, then re-run."; exit 1; }
  grep -q '^CLAUDE_BIN=' .env && sed -i "s#^CLAUDE_BIN=.*#CLAUDE_BIN=$CB#" .env || echo "CLAUDE_BIN=$CB" >> .env
  (cd /tmp && env -u ANTHROPIC_API_KEY "$CB" -p "Reply OK" --tools "" >/dev/null) || { echo "Run 'claude' once and log in, then re-run."; exit 1; }
fi
grep -q '^LOCAL_LLM_ENABLED=' .env || echo "LOCAL_LLM_ENABLED=true" >> .env

echo "3/5 Offline tests"
.venv/bin/python -m pytest -q tests

echo "4/5 systemd service (starts at boot, no login needed, restarts on crash)"
sudo tee /etc/systemd/system/trading-bots.service >/dev/null <<UNIT
[Unit]
Description=Trading bots and Claude agents
After=network-online.target docker.service
Wants=network-online.target
[Service]
User=$USER
WorkingDirectory=$REPO
Environment=HOME=$HOME
Environment=PATH=$(dirname "$(grep '^CLAUDE_BIN=' .env | cut -d= -f2- || echo /usr/bin/claude)"):/usr/local/bin:/usr/bin:/bin
ExecStart=$REPO/.venv/bin/python -u $REPO/daemon.py
Restart=always
RestartSec=30
Nice=5
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bots.service

echo "5/5 Home services (Docker)"
cd linux
[ -f services.env ] || { cp services.env.example services.env; echo "  Fill in linux/services.env (passwords), then re-run to start the services."; exit 0; }
if command -v nvidia-smi >/dev/null; then cp docker-compose.nvidia.yml docker-compose.override.yml
else
  cp docker-compose.amd.yml docker-compose.override.yml
  for G in VIDEO RENDER; do
    GID="$(getent group "${G,,}" | cut -d: -f3)"
    [ -n "$GID" ] && { grep -q "^${G}_GID=" services.env && sed -i "s/^${G}_GID=.*/${G}_GID=$GID/" services.env || echo "${G}_GID=$GID" >> services.env; }
  done
fi
grep -q '^TS_IP=.\+' services.env || sed -i "s/^TS_IP=.*/TS_IP=$(tailscale ip -4 | head -1)/" services.env
docker compose --env-file services.env up -d
docker exec ollama ollama pull "$(grep '^LOCAL_MODEL=' ../.env | cut -d= -f2- || echo llama3.1:8b)" || docker exec ollama ollama pull llama3.1:8b

echo
echo "Done. Bots: sudo journalctl -u trading-bots -f"
echo "Pages (from your iPhone/Mac with Tailscale on): see linux/README.md"
