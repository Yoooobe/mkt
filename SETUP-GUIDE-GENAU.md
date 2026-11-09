# SETUP-GUIDE-GENAU.md

Guia para um agente de IA instalar e operar o `Yoobe.mail` na maquina do Genal/Genau.

## 1. Visao geral

Este projeto roda localmente um dashboard de email marketing para a Yoobe:

- Backend: FastAPI em `dashboard/app.py`.
- Banco local: SQLite em `dashboard/dashboard.db`, criado automaticamente no primeiro boot.
- Frontend: HTML/CSS/JS em `dashboard/static/`.
- Templates: `proposals/email_templates.py`.
- Contexto de IA: `dashboard/context/kb/`.
- Tracking opcional: Cloudflare Worker em `tracking-worker/`.
- Gmail: via CLI `gws`, com perfis por remetente em `gws_accounts/`.

Nunca acione envios reais antes de validar remetente, audiencia, templates, tags e opt-out.

## 2. Requisitos

- macOS ou Linux com Python 3.10+.
- Node.js 20+ se for usar Cloudflare Worker ou CLI `claude`.
- `gws` instalado e autenticavel para Gmail.
- Opcional: `wrangler` para Cloudflare Worker.
- Opcional: Z-API para notificacoes WhatsApp internas.

## 3. Instalar dependencias

Na raiz extraida:

```bash
cd gws-4unik
python3 -m venv .venv
source .venv/bin/activate
pip install -r dashboard/requirements.txt
```

Se preferir sem venv:

```bash
pip3 install --user -r dashboard/requirements.txt
```

## 4. Criar configuracoes locais

```bash
cp dashboard/.env.example dashboard/.env
cp zapi-config.env.example zapi-config.env
cp proposals/monitor_config.env.example proposals/monitor_config.env
```

Edite `dashboard/.env`:

```env
API_KEY=<gere com: openssl rand -hex 16>
TRACKING_BASE_URL=
TRACKING_SECRET=
TRACKING_SYNC_KEY=
```

Tracking pode ficar vazio no primeiro teste. O dashboard roda sem isso.

Para gerar segredos:

```bash
openssl rand -hex 16
openssl rand -hex 32
```

## 5. Autenticar Gmail com gws

O sistema usa `gws gmail ...` para ler threads e enviar emails.

**OAuth Gmail só no Mac** (`localhost:8377`). O `gws auth login` abre um callback
`http://localhost:PORTA` no processo local — isso **não funciona** no dashboard
remoto (Cloudflare Container). Na produção, use o botão desabilitado “Só no Mac”
e, após logar no local, espelhe as contas:

```bash
./scripts/sync-local-to-remote.sh --with-accounts
```

O Google pode abrir `localhost:PORTA` após o consent — isso é o servidor
temporário do `gws`, não um bug. Não clique “Conectar” de novo até ver Success.

**Bounces / Undeliverable:** NDRs de `postmaster` / `mailer-daemon` **não** são
tratados como resposta de lead (evita auto-resposta `Re: Undeliverable` para o
postmaster). Hard bounce marca o contato como `email_invalido`.

Primeiro valide que o `gws` existe:

```bash
gws --help
```

Autentique a conta principal:

```bash
gws auth login -s gmail
```

Depois suba o dashboard e abra a aba **Remetentes**. Para cada remetente adicional,
o dashboard informa o comando com `HOME=<pasta_do_perfil> gws auth login -s gmail`.
Execute exatamente o comando mostrado e autentique a conta correta no navegador.

Verificacao rapida:

```bash
gws gmail users profile get --params '{"userId":"me"}'
```

## 6. Rodar o dashboard

```bash
./start-dashboard.sh
```

Abra:

```bash
open http://localhost:8377
```

O primeiro boot cria:

- `dashboard/dashboard.db`
- remetentes seed da Yoobe
- campanha exemplo em rascunho
- templates aprovados de exemplo

## 7. Primeiro uso seguro

1. Abra **Remetentes** e confirme que a conta de Gmail aparece conectada.
2. Abra **Contexto IA** e revise os KBs da Yoobe.
3. Importe uma base pequena de teste no CRM com coluna `email`.
4. Marque contatos de teste com a tag `teste`.
5. Crie/preview templates antes de aprovar.
6. Nao ative campanha real sem revisar audiencia e conteudo.

CSV minimo:

```csv
email,nome,empresa,cargo,origem,tags
pessoa@empresa.com,Nome Pessoa,Empresa Exemplo,RH,teste,teste
```

O pacote ja inclui uma base pequena em `samples/contatos-exemplo-4unik.csv` para
testar importacao e preparar uma campanha sem tocar contatos reais.

## 8. Tracking opcional com Cloudflare Worker

Instale/valide Wrangler:

```bash
npm install -g wrangler
wrangler login
```

Crie o D1:

```bash
cd tracking-worker
npx wrangler d1 create 4unik_mail_tracking
```

Copie o `database_id` retornado para `tracking-worker/wrangler.toml`.

Crie schema:

```bash
npx wrangler d1 execute 4unik_mail_tracking --remote --file=schema.sql
```

Configure segredos:

```bash
npx wrangler secret put TRACKING_SECRET
npx wrangler secret put TRACKING_SYNC_KEY
npx wrangler deploy
```

No `dashboard/.env`, use os mesmos valores:

```env
TRACKING_BASE_URL=https://t.corporate.yoobe.co
TRACKING_SECRET=<mesmo TRACKING_SECRET>
TRACKING_SYNC_KEY=<mesmo TRACKING_SYNC_KEY>
```

Se nao houver dominio `t.corporate.yoobe.co`, use a URL `workers.dev` impressa no deploy.

## 9. Z-API opcional

Edite `zapi-config.env` apenas se quiser notificacoes internas:

```env
ZAPI_URL=
ZAPI_CLIENT_TOKEN=
ZAPI_PHONE=55DDDNUMERO
```

Sem Z-API, o dashboard continua funcionando; apenas notificacoes WhatsApp serao puladas.

## 10. Arquivos que nao devem ir para git/zip publico

Nunca incluir:

- `dashboard/.env`
- `zapi-config.env`
- `proposals/monitor_config.env`
- `tracking-worker/.dev.vars`
- `tracking-worker/.wrangler/`
- `gws_accounts/`
- `dashboard/*.db*`
- logs, CSVs reais, bases de clientes e exports de Gmail

## 11. Checklist final

- `python -m py_compile dashboard/*.py` passa.
- `./start-dashboard.sh` sobe sem erro.
- `http://localhost:8377` abre.
- Aba Remetentes mostra conta conectada.
- Campanha exemplo continua em rascunho.
- Nenhum envio real foi acionado.
