#!/bin/zsh
set -e

cd "$(dirname "$0")"

if [ ! -f "provider_accounts.json" ]; then
  cp "provider_accounts.example.json" "provider_accounts.json"
fi

if [ ! -f ".codeup.env" ]; then
  cp ".codeup.env.example" ".codeup.env"
fi

open -e "provider_accounts.json" ".codeup.env"
