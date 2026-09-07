#!/bin/bash
# 4unik.mail — sobe o dashboard local em http://localhost:8377
# Usado também pelo LaunchAgent com.4unik.mail (KeepAlive + RunAtLoad).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/dashboard"
mkdir -p logs

# Local = dono da criação/edição/envio de campanhas. Remoto (container) é espelho.
export DASHBOARD_ENV="${DASHBOARD_ENV:-local}"

# `--reload` alinha o backend com mudanças em *.py. Inclui só Python para não
# reiniciar a cada edição de static/, escrita em .db ou logs (isso matava o
# loop contínuo de estratégia/SEO-GEO no meio do ciclo).
exec "$ROOT/.venv/bin/python" -m uvicorn app:app --host 127.0.0.1 --port 8377 --log-level warning \
  --reload --reload-dir "$ROOT/dashboard" --reload-include '*.py' \
  --reload-exclude '**/__pycache__/**' --reload-exclude 'logs/**' --reload-exclude 'static/**' \
  >> logs/server.log 2>&1
