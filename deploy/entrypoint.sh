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

# Remoto = espelho/acompanhamento. Criação e mutação de campanhas ficam no Mac.
export DASHBOARD_ENV="${DASHBOARD_ENV:-remoto}"

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
             GA4_HOSTNAME_PLATAFORMA GA4_HOSTNAME_LOJA \
             GOOGLE_GA4_ACCOUNT_ID GOOGLE_GA4_PROPERTY_ID GOOGLE_GA4_MEASUREMENT_ID \
             GOOGLE_GA4_SITE_MEASUREMENT_ID GOOGLE_TAG_ID GOOGLE_ADS_TAG_ID \
             GOOGLE_GTM_ACCOUNT_ID GOOGLE_GTM_CONTAINER_ID \
             GOOGLE_ADS_CUSTOMER_ID GOOGLE_ADS_LOGIN_CUSTOMER_ID GOOGLE_ADS_DEVELOPER_TOKEN \
             GMAIL_OAUTH_CLIENT_ID GMAIL_OAUTH_CLIENT_SECRET GMAIL_OAUTH_REDIRECT_URI GMAIL_OAUTH_ENCRYPTION_KEY \
             ZAPI_URL ZAPI_CLIENT_TOKEN ZAPI_PHONE LEADS_NOTIFY_EMAIL \
             FIRECRAWL_API_KEY FIRECRAWL_CREDIT_BUDGET FIRECRAWL_MAX_LEADS_PER_JOB \
             LEAD_EVIDENCE_TTL_DAYS LEAD_QUALITY_WORKER_URL LEAD_QUALITY_SYNC_KEY \
             VERTEX_AGENT_NAME_ANALYZER VERTEX_AGENT_LEAD_VALIDATOR VERTEX_AGENT_SCRAPE_PLANNER \
             VERTEX_AGENT_LOCATION \
             VERTEX_PROJECT_ID VERTEX_CREDENTIALS_JSON VERTEX_LOCATION \
             FOURUNIK_MAIL_PROVIDER VERTEX_MODEL_PRO VERTEX_MODEL_FLASH; do
    value="${!var:-}"
    if [ -n "$value" ]; then
      # JSON inline de SA → arquivo (ga4.py / libs que esperam path).
      if { [ "$var" = "GA4_CREDENTIALS_JSON" ] || [ "$var" = "VERTEX_CREDENTIALS_JSON" ]; } \
         && [ "${value#\{}" != "$value" ]; then
        cred_path="/tmp/${var}.json"
        printf '%s\n' "$value" > "$cred_path"
        chmod 600 "$cred_path"
        value="$cred_path"
      fi
      printf '%s=%s\n' "$var" "$value" >> "$ENV_FILE"
    fi
  done
fi

# --- 2+3. gws_accounts e SQLite em PARALELO (ambos são downloads do R2) -----
# Antes era serial: tar → restore → probe → uvicorn. No cold start isso somava
# dezenas de segundos antes da porta abrir.
restore_db() {
  SEED_MARKER="$APP_ROOT/dashboard/.seed_sha"
  # seed/version.txt no R2 = sha256 do snapshot local (sync econômico).
  # Se mudou, reaplica seed mesmo com disco ainda quente (sleepAfter).
  REMOTE_SEED_SHA=""
  if [ -n "${R2_ENDPOINT:-}" ]; then
    REMOTE_SEED_SHA="$(curl -fsS --aws-sigv4 "aws:amz:auto:s3" \
      --user "${R2_ACCESS_KEY_ID}:${R2_SECRET_ACCESS_KEY}" \
      "${R2_ENDPOINT}/${BUCKET}/seed/version.txt" 2>/dev/null | tr -d '[:space:]' || true)"
  fi
  LOCAL_SEED_SHA="$(cat "$SEED_MARKER" 2>/dev/null | tr -d '[:space:]' || true)"
  NEED_SEED=0
  if [ "${FORCE_SEED_DB:-}" = "1" ]; then
    NEED_SEED=1
    echo "[entrypoint] FORCE_SEED_DB=1"
  elif [ -n "$REMOTE_SEED_SHA" ] && [ -n "$LOCAL_SEED_SHA" ] && [ "$REMOTE_SEED_SHA" != "$LOCAL_SEED_SHA" ]; then
    NEED_SEED=1
    echo "[entrypoint] seed/version.txt novo ($REMOTE_SEED_SHA ≠ ${LOCAL_SEED_SHA:-vazio}) — reseed"
  elif [ -n "$REMOTE_SEED_SHA" ] && [ -z "$LOCAL_SEED_SHA" ]; then
    echo "[entrypoint] sem marcador local — priorizando restore Litestream sobre o seed"
  fi

  if [ "$NEED_SEED" = "1" ] && [ -n "${R2_ENDPOINT:-}" ]; then
    rm -f "$DB_PATH" "${DB_PATH}-wal" "${DB_PATH}-shm" "$SEED_MARKER"
    echo "[entrypoint] baixando seed/dashboard.db"
    if curl -fsS --aws-sigv4 "aws:amz:auto:s3" \
         --user "${R2_ACCESS_KEY_ID}:${R2_SECRET_ACCESS_KEY}" \
         "${R2_ENDPOINT}/${BUCKET}/seed/dashboard.db" -o "$DB_PATH"; then
      if [ -n "$REMOTE_SEED_SHA" ]; then
        printf '%s\n' "$REMOTE_SEED_SHA" > "$SEED_MARKER"
      fi
      echo "[entrypoint] seed/dashboard.db aplicado"
      return 0
    fi
    echo "[entrypoint] seed falhou — tentando restore Litestream"
    rm -f "$DB_PATH"
  fi

  if [ -f "$DB_PATH" ]; then
    echo "[entrypoint] DB local já presente — mantendo"
    return 0
  fi

  echo "[entrypoint] restaurando dashboard.db do R2..."
  litestream restore -if-replica-exists -config /etc/litestream.yml "$DB_PATH" \
    || echo "[entrypoint] litestream restore falhou — tentando seed cru"
  if [ -f "$DB_PATH" ]; then
    echo "[entrypoint] restore concluído"
    if [ -n "$REMOTE_SEED_SHA" ]; then
      printf '%s\n' "$REMOTE_SEED_SHA" > "$SEED_MARKER"
    fi
  elif [ -n "${R2_ENDPOINT:-}" ] && curl -fsS --aws-sigv4 "aws:amz:auto:s3" \
         --user "${R2_ACCESS_KEY_ID}:${R2_SECRET_ACCESS_KEY}" \
         "${R2_ENDPOINT}/${BUCKET}/seed/dashboard.db" -o "$DB_PATH"; then
    echo "[entrypoint] seed/dashboard.db aplicado (bootstrap inicial)"
    if [ -n "$REMOTE_SEED_SHA" ]; then
      printf '%s\n' "$REMOTE_SEED_SHA" > "$SEED_MARKER"
    fi
  else
    rm -f "$DB_PATH"
    echo "[entrypoint] réplica e seed inexistentes — banco novo será criado pelo app (db.init)"
  fi
}

restore_gws() {
  if [ -d /app/gws_accounts/contato ] || [ -z "${R2_ENDPOINT:-}" ]; then
    return 0
  fi
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
}

restore_db &
pid_db=$!
restore_gws &
pid_gws=$!
wait "$pid_db"
wait "$pid_gws"

# --- 4. replicação contínua em volta do uvicorn -------------------------------
cd "$APP_ROOT/dashboard"

# Se as três vars R2 estão presentes, sobe com Litestream direto — o probe HEAD
# no bucket adicionava um round-trip em todo boot e o Litestream já falha
# sozinho (e loga) se as credenciais estiverem erradas.
if [ -n "${R2_ENDPOINT:-}" ] && [ -n "${R2_ACCESS_KEY_ID:-}" ] && [ -n "${R2_SECRET_ACCESS_KEY:-}" ]; then
  echo "[entrypoint] R2 configurado — replicação Litestream ativa"
  exec litestream replicate -config /etc/litestream.yml \
    -exec "python -m uvicorn app:app --host 0.0.0.0 --port 8000"
else
  echo "[entrypoint] AVISO: credenciais R2 ausentes — subindo SEM replicação (dados deste boot não persistem!)"
  exec python -m uvicorn app:app --host 0.0.0.0 --port 8000
fi
