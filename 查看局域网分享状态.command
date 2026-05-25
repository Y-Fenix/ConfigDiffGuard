#!/bin/zsh
set -e

PORT="${1:-8765}"
TOOL_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_ID="com.maxx.config-diff"
DOMAIN="gui/$(id -u)"

echo "Maxx 配置对比 - 局域网分享状态"
echo ""

PORT_LISTENING=0
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  PORT_LISTENING=1
fi

if launchctl print "$DOMAIN/$PLIST_ID" >/dev/null 2>&1; then
  echo "后台服务：运行中"
else
  if [ "$PORT_LISTENING" = "1" ]; then
    echo "后台服务：本次后台运行中"
  else
    echo "后台服务：未注册或未运行"
  fi
fi

if [ "$PORT_LISTENING" = "1" ]; then
  echo "端口监听：已开启（$PORT）"
  echo ""
  echo "本机访问：http://127.0.0.1:$PORT/"
  PRINTED_IPS=""
  for IFACE in en0 en1 en2 bridge100; do
    IP="$(ipconfig getifaddr "$IFACE" 2>/dev/null || true)"
    if [ -n "$IP" ] && [[ "$PRINTED_IPS" != *"|$IP|"* ]]; then
      echo "同事访问：http://$IP:$PORT/"
      PRINTED_IPS="$PRINTED_IPS|$IP|"
    fi
  done
  export PRINTED_IPS
  python3 - "$PORT" <<'PY'
import os
import socket
import sys

port = sys.argv[1]
printed = {item for item in os.environ.get("PRINTED_IPS", "").split("|") if item}
ips: set[str] = set()

try:
    host_name = socket.gethostname()
    for info in socket.getaddrinfo(host_name, None, socket.AF_INET):
        ip = info[4][0]
        if not ip.startswith("127."):
            ips.add(ip)
except OSError:
    pass

try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if not ip.startswith("127."):
            ips.add(ip)
except OSError:
    pass

for ip in sorted(ips - printed):
    print(f"同事访问：http://{ip}:{port}/")
PY
else
  echo "端口监听：未开启（$PORT）"
  echo ""
  echo "可运行：局域网 ON.command"
fi

echo ""
echo "日志位置：$TOOL_DIR/logs/"
