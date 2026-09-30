#!/usr/bin/env bash
# Freeze the engine into one binary and drop it where Tauri expects its sidecar:
# desktop/src-tauri/binaries/linotype-engine-<target triple>
set -euo pipefail
cd "$(dirname "$0")"
triple="$(rustc -vV | sed -n 's/^host: //p')"
uv run --with pyinstaller pyinstaller --noconfirm --onefile --name linotype-engine \
  --collect-all pymupdf --collect-submodules fontTools app.py
mkdir -p ../desktop/src-tauri/binaries
cp dist/linotype-engine "../desktop/src-tauri/binaries/linotype-engine-$triple"
echo "sidecar: desktop/src-tauri/binaries/linotype-engine-$triple"
