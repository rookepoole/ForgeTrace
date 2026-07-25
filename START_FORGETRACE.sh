#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' \
    "ForgeTrace could not find python3." \
    "Install Python 3.10 or newer, then run this launcher again." >&2
  exit 1
fi

printf '%s\n' \
  "ForgeTrace - Local Repository Workspace" \
  "This package opens only after its own server binds successfully." \
  "If port 8765 is already used by an older ForgeTrace package, close it first." \
  "Owner workspace: http://127.0.0.1:8765" \
  "Press Ctrl+C to stop ForgeTrace and all sharing."

exec python3 server.py --port 8765 --open-browser
