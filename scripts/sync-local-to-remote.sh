#!/bin/bash
# Sync econômico local → remoto (espelho/acompanhamento). Sem LLM.
#
# Só chama push-db-to-remote.sh quando o fingerprint do SQLite mudou.
# Fingerprint = campanhas + contagens (ignora ruído de WAL/mtime).
#
# Uso:
#   ./scripts/sync-local-to-remote.sh                # skip se igual
#   ./scripts/sync-local-to-remote.sh --force        # sempre espelha DB
#   ./scripts/sync-local-to-remote.sh --with-accounts  # + sobe gws_accounts.tar.gz (OAuth Gmail)
#   ./scripts/sync-local-to-remote.sh --status
#
# Direções:
#   local → remoto: este script (campanhas/CRM; opcional contas Gmail)
#   remoto → local: NÃO sobrescreve o SQLite do Mac. Engajamento chega nos dois
#                   via TRACKING_* compartilhado. Gmail OAuth só no Mac.
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="$ROOT/dashboard/dashboard.db"
ACCOUNTS_DIR="$(cd "$ROOT/.." && pwd)/gws_accounts"
BUCKET=gws4unik-db
WORKER_DIR="$ROOT/deploy/worker"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/4unik-mail"
STATE_FILE="$STATE_DIR/last-remote-sync.fp"
LOG_DIR="$STATE_DIR/logs"
LOG_FILE="$LOG_DIR/sync-local-to-remote.log"
LOCK_FILE="$STATE_DIR/sync.lock"
FORCE=0
STATUS_ONLY=0
WITH_ACCOUNTS=0

for arg in "$@"; do
  case "$arg" in
    --force|-f) FORCE=1 ;;
    --status|-s) STATUS_ONLY=1 ;;
    --with-accounts|--accounts) WITH_ACCOUNTS=1 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

upload_gws_accounts() {
  if [[ ! -d "$ACCOUNTS_DIR" ]]; then
    log "AVISO: sem $ACCOUNTS_DIR — pulando seed de contas"
    return 0
  fi
  local tar=/tmp/gws_accounts-sync.tar.gz
  log "upload seed/gws_accounts.tar.gz"
  (cd "$(dirname "$ACCOUNTS_DIR")" && tar --exclude '.DS_Store' -czf "$tar" "$(basename "$ACCOUNTS_DIR")")
  (cd "$WORKER_DIR" && npx wrangler r2 object put "${BUCKET}/seed/gws_accounts.tar.gz" --file "$tar" --remote)
  rm -f "$tar"
  log "gws_accounts no R2 — reinicie o container remoto (próximo cold start / deploy) para aplicar"
}

mkdir -p "$STATE_DIR" "$LOG_DIR"

log() {
  local line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$line"
  echo "$line" >> "$LOG_FILE"
}

if [[ ! -f "$DB" ]]; then
  log "ERRO: não achei $DB"
  exit 1
fi

current_fp() {
  python3 - <<PY
import hashlib, sqlite3
con = sqlite3.connect("file:$DB?mode=ro", uri=True, timeout=10)
con.execute("PRAGMA busy_timeout=10000")
cur = con.cursor()
parts = []
rows = cur.execute(
    "SELECT id, ifnull(status,''), ifnull(atualizada_em,''), ifnull(versao,'') "
    "FROM campaigns ORDER BY id"
).fetchall()
parts.append("c:" + "|".join(",".join(map(str, r)) for r in rows))
for sql, key in (
    ("SELECT count(*) FROM templates_ai", "tpl"),
    ("SELECT count(*) FROM campaign_contacts", "cc"),
    ("SELECT count(*) FROM campaign_steps", "st"),
    ("SELECT count(*) FROM improvement_tasks", "imp"),
):
    try:
        n = cur.execute(sql).fetchone()[0]
    except sqlite3.Error:
        n = -1
    parts.append(f"{key}:{n}")
con.close()
print(hashlib.sha256("\n".join(parts).encode()).hexdigest())
PY
}

FP="$(current_fp)"
PREV="$(cat "$STATE_FILE" 2>/dev/null | tr -d '[:space:]' || true)"
CAMP_LINE="$(sqlite3 "$DB" "SELECT id||'|'||nome||'|'||status FROM campaigns ORDER BY id DESC LIMIT 3;" | tr '\n' '; ')"

if [[ "$STATUS_ONLY" == "1" ]]; then
  echo "fingerprint=$FP"
  echo "last_synced=${PREV:-"(nunca)"}"
  if [[ "$FP" == "$PREV" ]]; then
    echo "status=em_dia"
  else
    echo "status=pendente_push"
  fi
  echo "campanhas_recentes=$CAMP_LINE"
  echo "log=$LOG_FILE"
  exit 0
fi

NEED_DB=0
if [[ "$FORCE" == "1" || -z "$PREV" || "$FP" != "$PREV" ]]; then
  NEED_DB=1
fi
if [[ "$NEED_DB" != "1" && "$WITH_ACCOUNTS" != "1" ]]; then
  log "skip — fingerprint igual ($FP)"
  exit 0
fi

# Lock simples: evita dois pushes em paralelo (LaunchAgent + manual)
if [[ -f "$LOCK_FILE" ]]; then
  lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE" 2>/dev/null || echo 0) ))
  if [[ "$lock_age" -lt 3600 ]]; then
    log "skip — outro sync em andamento (lock ${lock_age}s)"
    exit 0
  fi
  log "lock antigo (${lock_age}s) — seguindo"
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

if [[ "$WITH_ACCOUNTS" == "1" ]]; then
  upload_gws_accounts
fi

if [[ "$NEED_DB" == "1" ]]; then
  log "push — fp=$FP (prev=${PREV:-nenhum}) · top=$CAMP_LINE"
  "$ROOT/scripts/push-db-to-remote.sh"
  printf '%s\n' "$FP" > "$STATE_FILE"
  log "ok — espelho DB atualizado · fp=$FP"
else
  log "DB em dia — contas enviadas; force cold start do container para carregar gws_accounts"
fi
echo "✔ Sync local→remoto ok. Confira em https://gws4unik-dashboard.genau-d0b.workers.dev (Access)."