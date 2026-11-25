#!/usr/bin/env bash
# MAC BACKUP STARTER for the cloud options agent (it does NOT trade on the Mac).
# Every 15 minutes on weekdays, 9:00 am-3:30 pm New York, it asks GitHub whether an options-agent run is
# going; if none is, it starts one. Needs the GitHub CLI signed in (brew install gh && gh auth login).
# Install (Terminal):   curl -fsSL https://raw.githubusercontent.com/DavidBog-Finance/trading-bots/main/portfolio1k/deploy/mac-backup-starter.sh | bash
# Remove:               bash ~/.portfolio1k/kick.sh uninstall
# Log:                  ~/.portfolio1k/kick.log
set -euo pipefail
REPO="${P1K_REPO:-DavidBog-Finance/trading-bots}"
GH="$(command -v gh || true)"
[ -n "$GH" ] || { echo "GitHub CLI missing. Run: brew install gh   then: gh auth login   then run this again."; exit 1; }
"$GH" auth status >/dev/null 2>&1 || { echo "GitHub CLI not signed in. Run: gh auth login   then run this again."; exit 1; }
"$GH" run list -R "$REPO" -w options-agent.yml -L 1 >/dev/null || { echo "Can't see $REPO with this GitHub login."; exit 1; }

DIR="$HOME/.portfolio1k"
LABEL=com.portfolio1k.kick
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$DIR" "$HOME/Library/LaunchAgents"

cat > "$DIR/kick.sh" <<K
#!/bin/bash
if [ "\${1:-}" = "uninstall" ]; then
  launchctl bootout gui/\$(id -u) "$PLIST" 2>/dev/null; rm -f "$PLIST"; echo "Backup starter removed."; exit 0
fi
[ "\$(TZ=America/New_York date +%u)" -le 5 ] || exit 0                    # Mon-Fri
hm=\$(TZ=America/New_York date +%H%M)
[ "\$hm" -ge 900 ] && [ "\$hm" -le 1530 ] || exit 0                        # 9:00 am-3:30 pm New York
# Moved to your own always-on computer (RUNNER=box)? Then GitHub must not trade too.
[ "\$("$GH" variable get RUNNER -R "$REPO" 2>/dev/null)" = "box" ] && exit 0
busy=\$("$GH" run list -R "$REPO" -w options-agent.yml -L 20 --json status -q '[.[] | select(.status != "completed")] | length') || exit 0
if [ "\$busy" = "0" ]; then
  "$GH" workflow run options-agent.yml -R "$REPO" --ref main -f action=run-now && echo "\$(date) no run going, started one"
fi
K
chmod +x "$DIR/kick.sh"

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DIR/kick.sh</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/kick.log</string>
  <key>StandardErrorPath</key><string>$DIR/kick.log</string>
</dict></plist>
PL
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "Backup starter installed. It checks every 15 min on weekdays 9:00 am-3:30 pm New York."
echo "Log: $DIR/kick.log   Remove: bash $DIR/kick.sh uninstall"
