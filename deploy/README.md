# Deploy — 4unik.mail em Cloudflare Containers (com estado no R2)

O dashboard (FastAPI + SQLite) roda num **Cloudflare Container** atrás de um
Worker mínimo (`deploy/worker/`). O disco do container é **efêmero**: o estado
vive num bucket **R2** e é gerenciado pelo **Litestream** — restore no boot +
replicação contínua enquanto o app roda.

```
request → Worker (gws4unik-dashboard) → getContainer("main") → uvicorn :8000
boot do container:
  1. materializa dashboard/.env a partir dos secrets (env vars)
  2. baixa seed/gws_accounts.tar.gz do R2 (perfis Gmail do gws)
  3. litestream restore  →  /app/gws-4unik/dashboard/dashboard.db
  4. litestream replicate -exec "uvicorn app:app --host 0.0.0.0 --port 8000"
```

## Segurança — leia antes de qualquer coisa

- **Segredos nunca vão para o repo nem para a imagem.** Entram só via
  `wrangler secret put` e são materializados em `dashboard/.env` dentro do
  container em runtime (o código lê env de arquivos, não de `os.environ`).
- **O container sobe "frio": zero campanhas ativas e fila vazia** (o banco
  restaurado é o snapshot do seu banco local — se lá não havia campanha ativa,
  nada envia). **Nenhum e-mail sai sem ação explícita** no dashboard
  (ativação de campanha exige confirmação; campanhas que tocam cliente exigem
  `QUERO_ENVIAR`).
- **`max_instances: 1` é obrigatório**: SQLite tem escritor único; duas
  instâncias replicando o mesmo caminho no R2 corrompem a réplica.

## Caminho do banco (por que /app/gws-4unik/dashboard/)

`dashboard/db.py` resolve `DB_PATH = <dir do db.py>/dashboard.db` — **não há
env var para sobrescrever** e o código não deve ser modificado. Por isso o
entrypoint restaura direto em `/app/gws-4unik/dashboard/dashboard.db` (não em
`/data`). Pelo mesmo motivo, `engine.py` resolve `gws_accounts` como **irmão do
diretório do repo** (`GWS_DIR.parent / "gws_accounts"`), então a imagem usa o
layout `/app/gws-4unik/` + `/app/gws_accounts/`.

## Pré-requisitos

- Conta Cloudflare paga (Workers Paid — Containers exige) e `wrangler` ≥ 4 logado (`npx wrangler login`).
- Docker rodando localmente (o `wrangler deploy` builda e envia a imagem).
- Litestream instalado localmente para o seed (`brew install benbjohnson/litestream/litestream`).
- `aws` CLI ou `rclone` para subir o tarball do `gws_accounts`.

## Passo a passo

### 1. Criar o bucket R2

```bash
npx wrangler r2 bucket create gws4unik-db
```

### 2. Gerar o API token do R2

Dashboard Cloudflare → **R2 → Manage R2 API Tokens → Create API Token**:

- Permissão **Object Read & Write**, escopo **apenas** no bucket `gws4unik-db`.
- Anote `Access Key ID` e `Secret Access Key`.
- Endpoint: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
  (o `ACCOUNT_ID` aparece na página do R2).

### 3. Configurar os secrets do Worker

Rode a partir de `deploy/worker/`. Os valores de `API_KEY`, `TRACKING_*` estão
no seu `dashboard/.env` local (nunca commitá-los):

```bash
cd deploy/worker
npx wrangler secret put API_KEY              # mesmo valor do dashboard/.env local
npx wrangler secret put TRACKING_BASE_URL
npx wrangler secret put TRACKING_SECRET
npx wrangler secret put TRACKING_SYNC_KEY
npx wrangler secret put R2_ACCESS_KEY_ID
npx wrangler secret put R2_SECRET_ACCESS_KEY
npx wrangler secret put R2_ENDPOINT          # https://<ACCOUNT_ID>.r2.cloudflarestorage.com
```

Opcionais (se usar GA4/Z-API na nuvem, adicione também ao `envVars` do
`src/index.ts` e à lista do `entrypoint.sh`): `GA4_PROPERTY_ID`,
`GA4_CREDENTIALS_JSON`, `ZAPI_URL`, `ZAPI_CLIENT_TOKEN`, `ZAPI_PHONE`.

### 4. Seed inicial no R2 (uma vez, a partir da máquina local)

Tudo via `wrangler r2 object put` (usa o login OAuth do wrangler — não precisa
das chaves S3 do R2, de Litestream local nem de aws/rclone).

**a) dashboard.db — snapshot cru** em `seed/dashboard.db`. O entrypoint tenta
primeiro `litestream restore` (réplica oficial); se ela ainda não existir,
baixa este snapshot e o Litestream do container assume a replicação dali em
diante:

```bash
cd /Users/genautech/mkt/gws-4unik
sqlite3 dashboard/dashboard.db ".backup /tmp/dashboard-seed.db"   # cópia consistente (WAL-safe)
npx wrangler r2 object put gws4unik-db/seed/dashboard.db \
  --file /tmp/dashboard-seed.db --remote
rm /tmp/dashboard-seed.db
```

> Pause o dashboard local (LaunchAgent) quando a nuvem virar a instância
> oficial — dois processos escrevendo/replicando o mesmo banco divergem.

### Performance remota (já aplicado no código)

- `instance_type: standard-1` (½ vCPU / 4 GiB) em vez de `basic`
- `sleepAfter = "6h"` — evita restore do R2 a cada visita após 10 min de idle
- SQLite com `synchronous=NORMAL`, cache 64 MiB, mmap 256 MiB
- Startup pesado (strategy/Calendly/backfills) em background depois da porta abrir
- Entrypoint restaura DB + gws_accounts em paralelo; sem probe HEAD no bucket

**b) gws_accounts — tarball simples** (o entrypoint baixa
`seed/gws_accounts.tar.gz` e extrai em `/app`):

```bash
cd /Users/genautech/mkt   # pai do repo — o tar precisa conter "gws_accounts/..."
tar --exclude '.DS_Store' -czf /tmp/gws_accounts.tar.gz gws_accounts
npx wrangler r2 object put gws4unik-db/seed/gws_accounts.tar.gz \
  --file /tmp/gws_accounts.tar.gz --remote
rm /tmp/gws_accounts.tar.gz   # não deixe credenciais em /tmp
```

> Atenção: use o `gws_accounts/` que o `engine.py` realmente usa —
> `/Users/genautech/mkt/gws_accounts` (irmão do repo). Só perfis com keyring de
> **arquivo** (`.config/gws/.encryption_key` + `credentials.enc`) funcionam no
> container; perfis que dependem do Keychain do macOS (ex.: `contato` e a conta
> default `comercial`) precisam de re-auth com
> `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` antes de portar.

### 5. Deploy

```bash
cd deploy/worker
npm install
npx wrangler deploy      # builda a imagem (Docker), envia ao registry e publica
```

O primeiro deploy imprime a URL (`https://gws4unik-dashboard.<subdomínio>.workers.dev`).

### 6. Verificação / smoke test

Sem enviar nada — apenas leitura:

```bash
URL=https://gws4unik-dashboard.<subdomínio>.workers.dev
API_KEY='<mesmo valor do secret>'

# 1. dashboard abre (a primeira request pode demorar ~30s: boot + restore)
curl -sS -o /dev/null -w '%{http_code}\n' "$URL/"          # → 200

# 2. nenhuma campanha ativa
curl -sS -H "X-API-Key: $API_KEY" "$URL/api/campaigns" | python3 -c \
  "import json,sys; cs=json.load(sys.stdin); print([c for c in cs if c.get('status')=='ativa'] or 'nenhuma campanha ativa ✔')"

# 3. fila vazia (sem pending/processing)
curl -sS -H "X-API-Key: $API_KEY" "$URL/api/queue/status"
```

Confira também `npx wrangler tail` para ver os logs do entrypoint
(`restore concluído`, `gws_accounts restaurado do R2`).

## Pendências para o deploy real

- **Plano Workers Paid (US$ 5/mês) na conta Cloudflare** — Containers não roda
  no free tier; o `wrangler deploy` falha com `Unauthorized` no push da imagem.
  Ativar em <https://dash.cloudflare.com/?to=/:account/workers/plans>.
- `npx wrangler login` + Docker rodando.
- Seed do `dashboard.db` e do `gws_accounts.tar.gz` (passo 4) — **feito**
  (objetos em `seed/` no bucket `gws4unik-db`).
- Perfis Gmail no container: só `genaujunior` tem keyring de arquivo completo;
  `contato`/`comercial` dependem do Keychain do macOS e precisam de re-auth
  (ver passo 4b) para enviar e-mail a partir da nuvem.
- Confirmar se o binário linux do `gws` (`@googleworkspace/cli`) baixa corretamente
  no build; se o npm mudar o nome do pacote, ajustar o `RUN npm install -g` no Dockerfile.
- IA (`ai.py`) chama o CLI `claude` local — indisponível no container; os fluxos
  de geração por IA degradam (erro tratado), o restante do dashboard funciona.
- GA4/Z-API na nuvem: adicionar os secrets opcionais (passo 3) se necessário.
- Desativar o LaunchAgent local (`com.4unik.mail`) quando a nuvem virar a
  instância oficial — nunca rodar os dois replicando o mesmo caminho do R2.
- ~~Proteger a URL pública~~ — **feito**: Cloudflare Access (Zero Trust, org
  `genau-4unik.cloudflareaccess.com`) na frente do Worker, com login One-Time
  PIN por e-mail e política de allow-list (genaujunior@gmail.com,
  comercial@4unik.com.br, marketing@4unik.com.br). O HTML embute a API key,
  então quem loga usa o dashboard direto; para adicionar/remover pessoas,
  edite a política em Zero Trust → Access → Applications → "4unik.mail
  dashboard". Acesso programático externo exige um Service Token do Access.
