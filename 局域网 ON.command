#!/bin/zsh
set -e

TOOL_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_ID="com.maxx.config-diff"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_ID.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$TOOL_DIR/logs"

python3 - "$TOOL_DIR" "$PLIST_ID" "$PLIST_DST" <<'PY'
from pathlib import Path
import plistlib
import sys

tool_dir = Path(sys.argv[1])
plist_id = sys.argv[2]
plist_dst = Path(sys.argv[3])
command = (
    f"cd {str(tool_dir)!r} && "
    "if [ -f '.codeup.env' ]; then source '.codeup.env'; fi && "
    "exec python3 -B -m config_diff_guard.server --host 0.0.0.0 --no-open"
)
payload = {
    "Label": plist_id,
    "ProgramArguments": ["/bin/zsh", "-lc", command],
    "RunAtLoad": True,
    "KeepAlive": True,
    "WorkingDirectory": str(tool_dir),
    "StandardOutPath": str(tool_dir / "logs" / "lan-share.out.log"),
    "StandardErrorPath": str(tool_dir / "logs" / "lan-share.err.log"),
}
plist_dst.write_bytes(plistlib.dumps(payload, sort_keys=False))
PY

launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
pkill -f "[c]onfig_diff_guard.server --host 0.0.0.0" 2>/dev/null || true

if launchctl bootstrap "$DOMAIN" "$PLIST_DST" 2>/dev/null; then
  launchctl enable "$DOMAIN/$PLIST_ID"
  START_MODE="开机常驻"
else
  START_MODE="本次后台"
  python3 - "$TOOL_DIR" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

tool_dir = Path(sys.argv[1])
logs_dir = tool_dir / "logs"
logs_dir.mkdir(parents=True, exist_ok=True)

command = (
    f"cd {str(tool_dir)!r} && "
    "if [ -f '.codeup.env' ]; then source '.codeup.env'; fi && "
    "exec python3 -B -m config_diff_guard.server --host 0.0.0.0 --no-open"
)

with (logs_dir / "lan-share.out.log").open("ab") as stdout, (logs_dir / "lan-share.err.log").open("ab") as stderr:
    subprocess.Popen(
        ["/bin/zsh", "-lc", command],
        cwd=str(tool_dir),
        stdout=stdout,
        stderr=stderr,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
PY
fi

sleep 1
"$TOOL_DIR/查看局域网分享状态.command"

echo ""
if [ "$START_MODE" = "开机常驻" ]; then
  echo "后台常驻已开启。以后不用保留这个终端窗口；关闭窗口不影响同事访问。"
else
  echo "已改用本次后台模式启动：关闭这个终端窗口不影响同事访问；重启电脑后需要再点一次启动脚本。"
fi
echo "如需停止，请运行：局域网 OFF.command"
