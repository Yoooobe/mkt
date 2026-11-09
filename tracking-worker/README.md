# Yoobe.mail tracking — Cloudflare Worker + D1

Para o handoff completo ao responsável por Cloudflare, GA4, GTM e Google Ads, consulte
[`../HANDOFF-CLOUDFLARE-GA4-ADS.md`](../HANDOFF-CLOUDFLARE-GA4-ADS.md).

Coletor sempre-online de **aberturas** (pixel) e **cliques** (redirect) dos e-mails
da Yoobe.mail. Registra cada evento no D1 na borda — não depende do notebook, então
**nenhum evento se perde** mesmo com o dashboard offline. O dashboard puxa os eventos
por cursor (`/events?since=`).

## Deploy (uma vez)
```bash
cd tracking-worker
npx wrangler login                                   # roda no terminal da Yoobe: ! npx wrangler login
npx wrangler d1 create 4unik_mail_tracking          # copie o database_id para wrangler.toml
npx wrangler d1 execute 4unik_mail_tracking --remote --file=schema.sql
npx wrangler secret put TRACKING_SECRET               # MESMO valor de dashboard/.env
npx wrangler secret put TRACKING_SYNC_KEY             # MESMO valor de dashboard/.env
npx wrangler deploy
```
Depois aponte o domínio: no painel Cloudflare (zona Yoobe) crie a rota
`t.corporate.yoobe.co` para este Worker e descomente `routes` no `wrangler.toml`
(ou use a URL `*.workers.dev` que o deploy imprime).

`dashboard/.env` deve ter os mesmos segredos:
```
TRACKING_BASE_URL=https://t.corporate.yoobe.co
TRACKING_SECRET=<igual ao secret do worker>
TRACKING_SYNC_KEY=<igual ao sync key do worker>
```

Para espelhar cliques nos canais oficiais como metas do GA4, configure também no dashboard:
`GA4_MEASUREMENT_ID` e `GA4_MP_API_SECRET`. Os eventos usados são `contact_whatsapp`
(+55 41 98760-7512) e `contact_email` (corporate@yoobe.co); nenhuma PII do lead é enviada ao GA4.

## Rotas
| Rota | Ação |
|---|---|
| `GET /o/<token>` | grava `open`, devolve GIF 1×1 (`no-store`); marca `prefetch` p/ máquinas Apple/Google |
| `GET /c/<token>` | grava `click`, **302** para a URL original (incluindo WhatsApp/e-mail comercial); o dashboard converte esses cliques em metas GA4 |
| `GET /events?since=N` | header `X-Sync-Key`; eventos com `id > N` (cursor) |
| `GET /healthz` | ok |

## Token
`base64url(JSON{sid,email,cid,etapa,u})` + `.` + `base64url(HMAC_SHA256(TRACKING_SECRET))`.
Gerado no dashboard por `db.sign_token()` e verificado aqui — stateless, sem round-trip.

## Teste local
```bash
npx wrangler d1 execute 4unik_mail_tracking --local --file=schema.sql
npx wrangler dev
# noutro terminal: curl -v http://localhost:8787/o/<token>  e  /c/<token>
```
