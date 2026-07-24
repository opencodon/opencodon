#!/usr/bin/env bash
# Install (or refresh) the weekly upstream-triage launchd job (macOS).
# Runs scripts/upstream_triage.py every Monday 09:00 local time and logs
# to .fork/triage/launchd.log. Re-run this script after moving the repo.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.opencodon.upstream-triage"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO_ROOT/.fork/triage"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$REPO_ROOT/scripts/upstream_triage.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_ROOT</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>1</integer>
    <key>Hour</key><integer>9</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>$REPO_ROOT/.fork/triage/launchd.log</string>
  <key>StandardErrorPath</key><string>$REPO_ROOT/.fork/triage/launchd.log</string>
</dict>
</plist>
PLIST

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed: $LABEL (Mondays 09:00) -> $PLIST"
echo "reports land in .fork/triage/, log in .fork/triage/launchd.log"
