#!/usr/bin/env bash
# Runs Portfolio 1k 24/7 on a Mac (launchd). Keep the Mac plugged in with sleep disabled
# (System Settings > Battery/Energy > prevent automatic sleeping).
# Usage, from the repo folder:  bash portfolio1k/deploy/install-mac.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(pwd)"
[ -f .env ] || { echo "Create .env first: cp .env.example .env, then add your Alpaca paper keys."; exit 1; }
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m pytest -q tests
mkdir -p logs
PLIST=~/Library/LaunchAgents/com.portfolio1k.agent.plist
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.portfolio1k.agent</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key><array><string>$REPO/.venv/bin/python</string><string>-m</string><string>portfolio1k</string><string>run</string></array>
  <key>EnvironmentVariables</key><dict><key>CLAUDE_BIN</key><string>$(command -v claude || echo "")</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$REPO/logs/portfolio1k.log</string>
  <key>StandardErrorPath</key><string>$REPO/logs/portfolio1k.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Running. Logs: tail -f logs/portfolio1k.log"
echo "Stop: launchctl unload $PLIST"
echo "Now set the GitHub repository variable RUNNER=box so GitHub stops trading the same account."
