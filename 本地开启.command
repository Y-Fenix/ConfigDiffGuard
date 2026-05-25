#!/bin/zsh
set -e

cd "$(dirname "$0")"
if [ -f ".codeup.env" ]; then
  source ".codeup.env"
fi
pkill -f "[c]onfig_diff_guard.server" 2>/dev/null || true
python3 -B -m config_diff_guard.server
