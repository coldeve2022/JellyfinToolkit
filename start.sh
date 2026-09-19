#!/usr/bin/env sh
# Jellyfin Toolkit - source checkout launcher (macOS / Linux)
# Creates a local .venv on first run, then starts the app.
set -eu

cd "$(dirname "$0")"

PY=""
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "[ERROR] Python 3.9+ not found on PATH." >&2
    echo "        Install it first (apt/dnf/brew install python3)." >&2
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "[setup] Creating virtual environment .venv ..."
    "$PY" -m venv .venv
    PY=".venv/bin/python"
    echo "[setup] Installing dependencies ..."
    "$PY" -m pip install -q --upgrade pip
    "$PY" -m pip install -q -r requirements.txt
fi

exec "$PY" main.py "$@"
