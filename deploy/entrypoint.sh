#!/bin/bash
# 4unik.mail — entrypoint do Cloudflare Container.
# O disco é EFÊMERO: todo boot restaura o dashboard.db do R2 (Litestream) e a
# replicação contínua roda em volta do uvicorn (litestream replicate -exec).
#
# db.py resolve DB_PATH = <dir do db.py>/dashboard.db, sem env var de override
# (não modificar db.py) → restauramos direto em /app/gws-4unik/dashboard/dashboard.db.
set -euo pipefail

APP_ROOT=/app/gws-4unik
DB_PATH="$APP_ROOT/dashboard/dashboard.db"
BUCKET="${R2_BUCKET:-gws4unik-db}"

# --- 1. dashboard/.env a partir dos secrets do Worker ------------------------
# engine._load_env_files() lê ARQUIVOS (dashboard/.env etc.), não os.environ.
# Os secrets chegam como env vars (Worker → Container envVars) e são materializados
# aqui, no disco efêmero — nunca na imagem nem no repo.
ENV_FILE="$APP_ROOT/dashboard/.env"
if [ ! -f "$ENV_FILE" ]; then
  : > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  for var in API_KEY TRACKING_BASE_URL TRACKING_SECRET TRACKING_SYNC_KEY \
             GA4_PROPERTY_ID GA4_CREDENTIALS_JSON GA4_MEASUREMENT_ID GA4_MP_API_SECRET \
             ZAPI_URL ZAPI_CLIENT_TOKEN ZAPI_PHONE LEADS_NOTIFY_EMAIL; do
    value="${!var:-}"
    if [ -n "$value" ]; then
      printf '%s=%s\n' "$var" "$value" >> "$ENV_FILE"
    fi
  done
fi

# --- 2. gws_accounts (perfis Gmail por remetente) do R2 ----------------------
# Seed opcional: tarball em s3://<bucket>/seed/gws_accounts.tar.gz (ver deploy/README.md).
# Sem ele o dashboard sobe normalmente, apenas sem conseguir enviar e-mails.
if [ ! -d /app/gws_accounts/contato ] && [ -n "${R2_ENDPOINT:-}" ]; then
  if curl -fsS --aws-sigv4 "aws:amz:auto:s3" \
       --user "${R2_ACCESS_KEY_ID}:${R2_SECRET_ACCESS_KEY}" \
       "${R2_ENDPOINT}/${BUCKET}/seed/gws_accounts.tar.gz" \
       -o /tmp/gws_accounts.tar.gz; then
    tar -xzf /tmp/gws_accounts.tar.gz -C /app
    rm -f /tmp/gws_accounts.tar.gz
    echo "[entrypoint] gws_accounts restaurado do R2"
  else
    echo "[entrypoint] AVISO: seed/gws_accounts.tar.gz indisponível no R2 — envio de e-mail ficará inativo"
  fi
fi

# --- 3. restore do SQLite via Litestream --------------------------------------
if [ ! -f "$DB_PATH" ]; then
  echo "[entrypoint] restaurando dashboard.db do R2..."
  litestream restore -if-replica-exists -config /etc/litestream.yml "$DB_PATH"
  if [ -f "$DB_PATH" ]; then
    echo "[entrypoint] restore concluído"
  elif [ -n "${R2_ENDPOINT:-}" ] && curl -fsS --aws-sigv4 "aws:amz:auto:s3" \
         --user "${R2_ACCESS_KEY_ID}:${R2_SECRET_ACCESS_KEY}" \
         "${R2_ENDPOINT}/${BUCKET}/seed/dashboard.db" -o "$DB_PATH"; then
    # Bootstrap: snapshot cru enviado uma única vez (wrangler r2 object put).
    # A partir daqui o litestream replicate assume e mantém a réplica oficial.
    echo "[entrypoint] seed/dashboard.db aplicado (bootstrap inicial)"
  else
    rm -f "$DB_PATH"
    echo "[entrypoint] réplica e seed inexistentes — banco novo será criado pelo app (db.init)"
  fi
fi

# --- 4. replicação contínua em volta do uvicorn -------------------------------
cd "$APP_ROOT/dashboard"
exec litestream replicate -config /etc/litestream.yml \
  -exec "python -m uvicorn app:app --host 0.0.0.0 --port 8000"
