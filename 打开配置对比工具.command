#!/bin/zsh
set -e

cd "$(dirname "$0")"
if [ -f ".codeup.env" ]; then
  source ".codeup.env"
fi
python3 -B -m config_diff_guard.server
