#!/usr/bin/env bash
# Push this checkout to the server and restart Linotype there (server mode).
# First-time setup (env file, systemd unit, nginx site) is described in README.md.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${1:-157.180.87.51}"
rsync -az --delete --exclude .venv --exclude out --exclude dist --exclude build \
  --exclude __pycache__ --exclude .git --exclude engine/assets --exclude .DS_Store ./ "$HOST:apps/linotype/"
ssh "$HOST" 'cd ~/apps/linotype/engine && uv sync --no-dev --python 3.12 -q && sudo systemctl restart linotype && sleep 2 && systemctl is-active linotype'
curl -s -o /dev/null -w "https://pdf.ayushsingh.dev -> %{http_code}\n" https://pdf.ayushsingh.dev/login
