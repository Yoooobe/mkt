#!/bin/bash
# 4unik.mail — sobe o dashboard local em http://localhost:8377
# Usado também pelo LaunchAgent com.4unik.mail (KeepAlive + RunAtLoad).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/dashboard"
mkdir -p logs

# `--reload` keeps the local dashboard aligned with changes in app.py while it
# is open, avoiding a stale backend accepting the UI but missing new routes.
exec "$ROOT/.venv/bin/python" -m uvicorn app:app --host 127.0.0.1 --port 8377 --log-level warning --reload --reload-dir "$ROOT/dashboard" \
  >> logs/server.log 2>&1
