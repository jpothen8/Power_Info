#!/bin/bash
# Removes the power monitor LaunchAgent so it no longer starts at login.
set -euo pipefail

launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jpothen.powermonitor.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.jpothen.powermonitor.plist

echo "Uninstalled. It will no longer start at login."
