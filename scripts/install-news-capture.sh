#!/bin/bash
# Install or remove the daily news snapshot job (macOS launchd).
#
#   scripts/install-news-capture.sh            install, runs every day at 21:15 local time
#   scripts/install-news-capture.sh uninstall  remove
set -euo pipefail
LABEL=com.bazaar.news-snapshot
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$HOME/Library/Logs/bazaar-news-snapshot.log"

if [ "${1:-}" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL"
  exit 0
fi

UV_PATH="$(command -v uv || true)"
if [ -z "$UV_PATH" ]; then
  echo "uv is not on PATH. Install uv, then run this script again." >&2
  exit 1
fi
UV_DIR="$(dirname "$UV_PATH")"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$REPO/scripts/capture-news.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME</string>
    <key>PATH</key><string>$UV_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST_EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL, daily at 21:15. Log: $LOG"
