#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
    echo "[ERROR] Backend/.env is missing. Copy .env.example to .env and configure it."
    exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
    if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 13))'; then
        echo "[ERROR] Python 3.13+ is required to create Backend/.venv."
        exit 1
    fi
    echo "[INFO] Creating Backend/.venv..."
    python3 -m venv .venv
fi

if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 13))'; then
    echo "[ERROR] Backend/.venv is invalid or uses Python older than 3.13. Recreate it with Python 3.13+."
    exit 1
fi

echo "[INFO] Checking backend dependencies..."
.venv/bin/python -m pip install -r requirements.txt
echo "[INFO] Starting backend [$1]..."
exec .venv/bin/python bot.py
