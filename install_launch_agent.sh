#!/bin/bash
# Installs the power monitor as a per-user LaunchAgent so it starts
# automatically at login. Does NOT launch it immediately unless you're
# already logged in when you run this (bootstrap + RunAtLoad starts it now).
set -euo pipefail
cd "$(dirname "$0")"
REPO_DIR="$(pwd)"
LABEL="com.jpothen.powermonitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p logs
mkdir -p ~/Library/LaunchAgents

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$REPO_DIR/.venv/bin/power_monitor</string>
		<string>$REPO_DIR/power_monitor.py</string>
	</array>
	<key>WorkingDirectory</key>
	<string>$REPO_DIR</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<false/>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>StandardOutPath</key>
	<string>$REPO_DIR/logs/power_monitor.out.log</string>
	<key>StandardErrorPath</key>
	<string>$REPO_DIR/logs/power_monitor.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed. It will now start automatically at login."
echo "Logs: $REPO_DIR/logs/"
echo "To stop it now: launchctl bootout gui/$(id -u)/$LABEL"
echo "To remove entirely: ./uninstall_launch_agent.sh"
