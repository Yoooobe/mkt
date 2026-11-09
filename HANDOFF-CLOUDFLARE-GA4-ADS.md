# Implantação do rastreamento de e-mail — sem Cloudflare

## Arquitetura pronta

O rastreamento usa a API Vercel e o Sanity que já atendem o formulário do site.
Não é necessário criar Worker, D1 ou alterar a conta Cloudflare.

```text
E-mail
  → https://corporate.yoobe.co/api/email/{open|click|unsubscribe}
  → proxy Nginx
  → https://leads-ingest-api.vercel.app/api/email/*
  → Sanity (eventos)
  → dashboard (sincronização autenticada)
  → GA4 (somente metas de contato, sem PII)
```

Projeto Vercel: `leads-ingest-api`

Propriedade GA4: `327916606`

Measurement ID: `G-SMJDYCENBC`

## 1. Variáveis da Vercel

No projeto `leads-ingest-api`, cadastrar em Production, Preview e Development:

```text
TRACKING_SECRET=<valor aleatório de 32+ bytes>
TRACKING_SYNC_KEY=<outro valor aleatório de 32+ bytes>
GA4_MEASUREMENT_ID=G-SMJDYCENBC
GA4_MP_API_SECRET=<secret criado no stream GA4>
```

O projeto também precisa manter as variáveis já utilizadas pelo Sanity:

```text
NEXT_PUBLIC_SANITY_PROJECT_ID
NEXT_PUBLIC_SANITY_DATASET=production
SANITY_API_WRITE_TOKEN
```

O `TRACKING_SECRET` e o `TRACKING_SYNC_KEY` devem ser entregues ao responsável
pelo dashboard por cofre de senha. Não enviar em e-mail nem fazer commit.

## 2. Variáveis do dashboard

Em `dashboard/.env`:

```text
TRACKING_BASE_URL=https://corporate.yoobe.co/api/email
TRACKING_SECRET=<exatamente o mesmo valor da Vercel>
TRACKING_SYNC_KEY=<exatamente o mesmo valor da Vercel>
```

Reiniciar o dashboard depois de alterar o arquivo.

## 3. Proxy no servidor da plataforma

Quem administra o Nginx de `corporate.yoobe.co` deve aplicar:

```nginx
location /api/email/ {
    proxy_pass https://leads-ingest-api.vercel.app/api/email/;
    proxy_ssl_server_name on;
    proxy_set_header Host leads-ingest-api.vercel.app;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_hide_header X-Powered-By;
    add_header Cache-Control "no-store" always;
}
```

O arquivo completo de exemplo está em
`landing/infra/plataforma-4unik-nginx-server.example.conf`.

Isso é configuração do servidor web, não do Cloudflare. Se o servidor da
plataforma não permitir Nginx, o dashboard pode usar temporariamente
`https://leads-ingest-api.vercel.app/api/email`, mas os links dos e-mails ficarão
com o domínio da Vercel.

## 4. Rotas públicas

| Rota | Função |
| --- | --- |
| `GET /api/email/open?token=...` | Registra abertura e devolve GIF 1×1 |
| `GET /api/email/click?token=...` | Registra clique e redireciona |
| `GET /api/email/unsubscribe?token=...` | Exibe confirmação |
| `POST /api/email/unsubscribe?token=...` | Efetiva descadastro/one-click |
| `GET /api/email/events?cursor=...` | Entrega eventos ao dashboard com `X-Sync-Key` |

Tokens inválidos nunca liberam destinos arbitrários. Clique e abertura continuam
funcionando de forma segura mesmo se Sanity ou GA4 estiverem temporariamente fora.

## 5. Sanity

O schema `emailTrackingEvent` já está registrado no Studio do site. Cada evento
guarda campanha, etapa, URL, horário, país, hash do IP e indício de prefetch.
O IP bruto não é armazenado.

O dashboard usa cursor opaco e só avança depois de persistir o lote localmente.
Descadastros aplicam a supressão no CRM e impedem novos envios.

## 6. GA4 Measurement Protocol

No GA4:

1. Admin → Data Streams → stream `G-SMJDYCENBC`.
2. Measurement Protocol API secrets → Create.
3. Nome sugerido: `4unik-email-tracking`.
4. Cadastrar o valor em `GA4_MP_API_SECRET` na Vercel.

Eventos emitidos:

- `contact_whatsapp` para `https://wa.me/5541987607512`.
- `contact_email` para `mailto:corporate@yoobe.co`.

O payload do GA4 contém somente método, campanha, etapa e UTMs. Nome, e-mail e
telefone nunca são enviados ao Analytics.

Marcar ambos como key events. `contact_whatsapp` já existe; criar
`contact_email` com acesso Editor/Administrador. Depois importar as metas no
Google Ads, inicialmente como secundárias até validar qualidade e volume.

## 7. Deploy e validação

No projeto do site:

```bash
cd landing/leads-ingest-api
npm run typecheck
npm run test:tracking
vercel --prod
```

Após aplicar o proxy, validar sem disparar campanha real:

1. token Python aceito pela API Node;
2. `/open` retorna `200` e `Content-Type: image/gif`;
3. `/click` retorna `302` para o destino assinado;
4. GET de `/unsubscribe` não descadastra sozinho;
5. POST de `/unsubscribe` cria o evento;
6. `/events` retorna `401` sem a sync key e `200` com ela;
7. evento aparece no Sanity e depois no funil do dashboard;
8. cliques de WhatsApp/e-mail aparecem no GA4 Realtime/DebugView.

## 8. Dados que ainda dependem do administrador

Entregar por canal seguro:

```text
TRACKING_SECRET
TRACKING_SYNC_KEY
GA4_MP_API_SECRET
```

E confirmar por escrito:

```text
Proxy /api/email/ aplicado no servidor: sim/não
Deploy Vercel concluído: sim/não
contact_email marcado como key event: sim/não
Metas importadas no Google Ads: sim/não
```
