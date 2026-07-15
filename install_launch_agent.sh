#!/bin/bash
# Installs the power monitor as a per-user LaunchAgent so it starts
# automatically at login. Does NOT launch it immediately unless you're
# already logged in when you run this (bootstrap + RunAtLoad starts it now).
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p logs
mkdir -p ~/Library/LaunchAgents
cp com.jpothen.powermonitor.plist ~/Library/LaunchAgents/

launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.jpothen.powermonitor.plist 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.jpothen.powermonitor.plist

echo "Installed. It will now start automatically at login."
echo "Logs: $(pwd)/logs/"
echo "To stop it now: launchctl bootout gui/$(id -u)/com.jpothen.powermonitor"
echo "To remove entirely: ./uninstall_launch_agent.sh"
