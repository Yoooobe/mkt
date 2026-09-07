#!/bin/bash
# Espelha o SQLite local → remoto (seed R2 + wipe Litestream + FORCE_SEED).
# Uso:
#   ./scripts/push-db-to-remote.sh
#
# Política: local = dono de campanhas/envio. Remoto = espelho/acompanhamento.
# Após o 1º boot com seed, o script limpa FORCE_SEED_DB e redeploya.
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="$ROOT/dashboard/dashboard.db"
SEED=/tmp/dashboard-seed.db
BUCKET=gws4unik-db
WORKER_DIR="$ROOT/deploy/worker"
ACCOUNT_ID="${CF_ACCOUNT_ID:-d0b51520d7b3935a9c3801ba7685afab}"
WRANGLER_TOML="${WRANGLER_CONFIG:-$HOME/Library/Preferences/.wrangler/config/default.toml}"

if [[ ! -f "$DB" ]]; then
  echo "ERRO: não achei $DB" >&2
  exit 1
fi

oauth_token() {
  python3 -c "import tomllib; print(tomllib.load(open('$WRANGLER_TOML','rb'))['oauth_token'])"
}

list_ltx_keys() {
  local token cursor="" url resp
  token="$(oauth_token)"
  while true; do
    url="https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/r2/buckets/${BUCKET}/objects?prefix=dashboard.db/&per_page=1000"
    if [[ -n "$cursor" ]]; then url="${url}&cursor=${cursor}"; fi
    resp="$(curl -sS "$url" -H "Authorization: Bearer ${token}")"
    python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d.get("success"), d
for o in (d.get("result") or []):
    print(o["key"] if isinstance(o, dict) else o)
info=d.get("result_info") or {}
open("/tmp/r2-cursor.txt","w").write(str(info.get("cursor") or ""))
open("/tmp/r2-count.txt","w").write(str(len(d.get("result") or [])))
' <<<"$resp"
    cursor="$(cat /tmp/r2-cursor.txt 2>/dev/null || true)"
    count="$(cat /tmp/r2-count.txt 2>/dev/null || echo 0)"
    if [[ -z "$cursor" || "$count" == "0" ]]; then break; fi
  done
}

set_force_seed() {
  FORCE_SEED_VAL="$1" WORKER_JSONC="$WORKER_DIR/wrangler.jsonc" python3 - <<'PY'
import os, pathlib, re
val = os.environ["FORCE_SEED_VAL"]
p = pathlib.Path(os.environ["WORKER_JSONC"])
text = p.read_text()
text2, n = re.subn(r'("FORCE_SEED_DB"\s*:\s*)"[^"]*"', rf'\1"{val}"', text, count=1)
if n != 1:
    raise SystemExit("não achei FORCE_SEED_DB em wrangler.jsonc")
p.write_text(text2)
print(f"FORCE_SEED_DB={val!r}")
PY
}

echo "==> 1/6 snapshot WAL-safe"
sqlite3 "$DB" ".backup $SEED"
ls -lh "$SEED"
SEED_SHA="$(shasum -a 256 "$SEED" | awk '{print $1}')"
printf '%s\n' "$SEED_SHA" > /tmp/dashboard-seed.sha
echo "    sha256=$SEED_SHA"

echo "==> 2/6 upload seed/dashboard.db + seed/version.txt"
cd "$WORKER_DIR"
npx wrangler r2 object put "${BUCKET}/seed/dashboard.db" --file "$SEED" --remote
npx wrangler r2 object put "${BUCKET}/seed/version.txt" --file /tmp/dashboard-seed.sha --remote

echo "==> 3/6 wipe réplica Litestream (prefixo dashboard.db/)"
KEYS_FILE=/tmp/r2-ltx-keys-wipe.txt
list_ltx_keys > "$KEYS_FILE"
if [[ ! -s "$KEYS_FILE" ]]; then
  echo "    (nenhum objeto sob dashboard.db/ — ok)"
else
  while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    echo "    delete $key"
    npx wrangler r2 object delete "${BUCKET}/${key}" --remote >/dev/null
  done < "$KEYS_FILE"
fi

echo "==> 4/6 FORCE_SEED_DB=1 + deploy"
set_force_seed "1"
npx wrangler deploy

echo "==> 5/6 aguardando boot (Litestream voltar a escrever LTX)…"
# Espera até aparecer pelo menos 1 objeto sob dashboard.db/ (container aplicou seed e replica).
for i in $(seq 1 36); do
  n="$(list_ltx_keys | wc -l | tr -d ' ')"
  echo "    tentativa $i: $n objeto(s) LTX"
  if [[ "$n" -gt 0 ]]; then break; fi
  sleep 5
done

echo "==> 6/6 FORCE_SEED_DB=\"\" + redeploy"
set_force_seed ""
npx wrangler deploy

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/4unik-mail"
mkdir -p "$STATE_DIR"
printf '%s\n' "$SEED_SHA" > "$STATE_DIR/last-remote-sync.sha"

rm -f "$SEED" /tmp/dashboard-seed.sha /tmp/r2-cursor.txt /tmp/r2-count.txt
echo "✔ Espelho local→remoto concluído."
echo "   Abra https://gws4unik-dashboard.genau-d0b.workers.dev (Access)"
echo "   Campanhas com origem: local ficam só acompanhamento no remoto."
echo "   sha=$SEED_SHA"
