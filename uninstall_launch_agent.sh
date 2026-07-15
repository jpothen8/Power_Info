#!/bin/bash
# Removes the power monitor LaunchAgent so it no longer starts at login.
set -euo pipefail

LABEL="com.jpothen.powermonitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"

echo "Uninstalled. It will no longer start at login."
