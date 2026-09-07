# lead-quality Worker (Firecrawl + Queue)

Scrape público off critical path para validação de leads do 4unik.mail.

## Fluxo

1. Dashboard `POST /api/leads/validar` → Vertex `scrape_planner` → só enfileira quem precisa
2. Este Worker `POST /enqueue` (auth `X-Sync-Key`) → Queue `lead-scrape`
3. Consumer: **1 scrape** da homepage (`domain_only`); `search_one` só se pedido e scrape falhou
4. `POST {DASHBOARD_URL}/api/leads/evidence` com `credits_spent`
5. Dashboard grava cache (email + domínio) e roda `lead_validator`

Orçamento tipicamente **1 crédito/lead**. Cap no dashboard: `FIRECRAWL_CREDIT_BUDGET=35`.

## Setup

```bash
cd lead-quality-worker
npm install
npx wrangler queues create lead-scrape
npx wrangler secret put FIRECRAWL_API_KEY
npx wrangler secret put SYNC_KEY              # mesmo LEAD_QUALITY_SYNC_KEY / TRACKING_SYNC_KEY
npx wrangler secret put DASHBOARD_URL         # URL do Container
npx wrangler secret put DASHBOARD_API_KEY     # mesmo API_KEY do dashboard
npx wrangler types
npx wrangler deploy
```

No dashboard (`.env` ou secrets do Container):

```
LEAD_QUALITY_WORKER_URL=https://4unik-lead-quality.<subdomínio>.workers.dev
LEAD_QUALITY_SYNC_KEY=<mesmo SYNC_KEY>
FIRECRAWL_API_KEY=<para fallback sync local>
```

## Health

`GET /healthz` → `ok`
