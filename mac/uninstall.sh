#!/bin/bash
# Stops the always-on runner and removes it from launchd. Positions are NOT sold.
LABEL="com.tradingagents.runner"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "Runner stopped." || echo "Runner was not running."
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
