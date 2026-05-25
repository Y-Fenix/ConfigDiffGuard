#!/bin/zsh
set -e

TOOL_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_ID="com.maxx.config-diff"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_ID.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
launchctl disable "$DOMAIN/$PLIST_ID" 2>/dev/null || true
pkill -f "[c]onfig_diff_guard.server --host 0.0.0.0" 2>/dev/null || true

echo "后台局域网分享已停止。"
echo "如需重新开启，请运行：局域网 ON.command"
