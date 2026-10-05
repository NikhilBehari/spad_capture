#!/usr/bin/env bash
# Double-click to open the dashboard, setting up the environment first if needed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

URL=http://127.0.0.1:8888
INSTALL=startup/install/install.sh

SPAD="$("$INSTALL" --prefix)/bin/spad"
if [ ! -x "$SPAD" ]; then
  "$INSTALL" --yes
  SPAD="$("$INSTALL" --prefix)/bin/spad"
fi

( for _ in $(seq 60); do
    curl -sf -o /dev/null "$URL" && { open "$URL"; exit; }
    sleep 0.5
  done ) &

exec "$SPAD" tmf capture --mode manual --viz
