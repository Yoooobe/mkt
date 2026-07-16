# Deploy 4unik.mail no Cloudflare (Containers + R2)

O dashboard é FastAPI + SQLite com threads de fila. **Vercel serverless não serve**;
a opção barata/rápida com a sua conta Cloudflare é **Containers** (Docker) +
**R2** via Litestream (disco do container é efêmero).

Guardrail: o container sobe com **zero campanhas ativas** e os loops de envio
só ligam após ativação explícita. Segredos **nunca** vão para o repo.

## Pré-requisitos

- Conta Cloudflare (Workers Paid / Containers habilitado)
- `wrangler` logado: `npx wrangler login`
- `rclone` ou AWS CLI (seed do banco para o R2)

## 1. Bucket R2

No dashboard Cloudflare → R2 → Create bucket: `gws4unik-db`.

Crie um API Token R2 com Object Read & Write nesse bucket. Anote:

- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_ENDPOINT` = `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

## 2. Seed inicial (uma vez)

Do Mac, com o banco local já limpo/pronto:

```bash
# Exemplo com rclone (configure remote `r2` apontando para o endpoint acima)
rclone copyto dashboard/dashboard.db r2:gws4unik-db/seed/dashboard.db
# Contas gws (nunca no git):
rclone sync gws_accounts/ r2:gws4unik-db/seed/gws_accounts/
```

No primeiro boot, se não houver réplica Litestream, o entrypoint cria DB vazio.
Para bootstrap real: faça um restore manual antes do primeiro deploy, ou
copie o seed para `/app/dashboard/dashboard.db` via volume/init (documente no
runbook da equipe). O caminho Litestream esperado é `gws4unik-db/litestream/`.

Bootstrap recomendado (primeira vez):

```bash
# Suba um restore pontual no Mac para gerar a estrutura Litestream, OU
# no primeiro deploy use o DB local já semeado e deixe o Litestream replicar.
```

## 3. Secrets do Worker / Container

Na pasta `deploy/worker`:

```bash
npm install
npx wrangler secret put API_KEY
npx wrangler secret put TRACKING_BASE_URL
npx wrangler secret put TRACKING_SECRET
npx wrangler secret put TRACKING_SYNC_KEY
npx wrangler secret put R2_ACCESS_KEY_ID
npx wrangler secret put R2_SECRET_ACCESS_KEY
npx wrangler secret put R2_ENDPOINT
# Demais vars do dashboard/.env (GA4, GTM, Z-API…) conforme necessário
```

Vars de runtime do container precisam ser injetadas no ambiente do Container
(via painel Cloudflare / `wrangler` containers env — ver docs atuais). O
`entrypoint.sh` lê `R2_*` para o Litestream.

## 4. Deploy

```bash
cd deploy/worker
npm install
npx wrangler deploy
```

A imagem é buildada a partir de `deploy/Dockerfile` (Python + gws + Litestream).

## 5. Smoke test (sem envio)

1. Abra a URL `*.workers.dev` (ou domínio custom).
2. Confirme login com a `API_KEY`.
3. CRM lista contatos; Campanhas deve estar vazia (ou só rascunhos).
4. Fila sem `campaign_send` pendente.
5. **Não** ative campanha nem dispare fila.

## 6. Multi-pessoa

Compartilhe a URL + `API_KEY` com o time. Cada operador usa o mesmo SQLite
replicado no R2. Há **uma** instância de container (`max_instances: 1`) —
SQLite não aceita multi-writer.

## Limitações conscientes

- Disco efêmero: sem Litestream/R2 o DB some no sleep do container.
- `gws` precisa das pastas `gws_accounts` no filesystem do container (seed).
- Claude CLI para geração de campanhas IA pode exigir auth adicional no
  container — geração pode continuar local até isso ser configurado.
- Custo: Containers (plano pago Cloudflare) + R2 free tier costuma bastar
  para o volume atual (~10 MB de DB).

## Rollback

```bash
litestream restore -config deploy/litestream.yml -o /tmp/dashboard.db gws4unik
# depois copie de volta para o ambiente desejado
```
