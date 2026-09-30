#!/usr/bin/env bash
# Build dist/Linotype.app (and dist/Linotype.dmg) with PyInstaller.
set -euo pipefail
cd "$(dirname "$0")"

# the pages' JavaScript lives in Python strings; a syntax error there only shows up as a dead page
tmp="$(mktemp -d)"
uv run python - "$tmp" <<'PY'
import re, sys
import app, editor
out = sys.argv[1]
open(f"{out}/editor.js", "w").write(editor.JS)
for name in ("HOME", "SHELL"):
    open(f"{out}/{name}.js", "w").write(re.findall(r"<script>(.*?)</script>", getattr(app, name), re.S)[0])
PY
for f in "$tmp"/*.js; do node --check "$f"; done
rm -rf "$tmp"

uv run --with pyinstaller pyinstaller --noconfirm --clean linotype.spec
if [[ "$(uname)" == Darwin ]]; then
  hdiutil create -volname Linotype -srcfolder dist/Linotype.app -ov -format UDZO dist/Linotype.dmg >/dev/null
  echo "built: engine/dist/Linotype.app, engine/dist/Linotype.dmg"
else
  echo "built: engine/dist/Linotype"
fi
