#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
echo "Starting ForgeTrace at http://127.0.0.1:8765"
echo "Repository files are stored in: $(pwd)/workspace"
python3 server.py --port 8765
