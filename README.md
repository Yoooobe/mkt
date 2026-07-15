# 4unik.mail / gws-4unik

Repositório do dashboard local de CRM e email marketing da 4unik, com tracking
de abertura/clique, geração de campanhas, templates e worker de coleta.

## O que foi publicado

- `dashboard/` com a aplicação FastAPI e frontend simples
- `tracking-worker/` com o coletor de eventos
- `proposals/` com templates e rascunhos de campanha
- `scripts/` com utilitários de ingestao
- `start-dashboard.sh`
- documentos de operacao e handoff
- arquivos `.example` de configuracao

## O que fica privado

- `.env` reais
- segredos OAuth e chaves de integracao
- bancos SQLite locais
- logs de execucao
- arquivos de cache
- dados brutos de clientes

## Arquivos de exemplo que podem ser mantidos no repo

Os arquivos abaixo servem como referencia de formato e podem continuar versionados
se estiverem sanitizados:

- `dashboard/.env.example`
- `zapi-config.env.example`
- `proposals/monitor_config.env.example`
- `tracking-worker/.dev.vars.example`

Se algum arquivo de exemplo passar a carregar dados reais, mova para fora do
repositório e preserve apenas a versao `.example`.

## Arquivos que nao devem ser publicados

Estes permanecem locais e sao ignorados pelo Git:

- `dashboard/.env`
- `zapi-config.env`
- `proposals/monitor_config.env`
- `dashboard/*.db`
- `dashboard/logs/`
- `client_secret_*.json`
- `gws_accounts/`
- `*.csv` e `*.xlsx` operacionais

## Como rodar

```bash
pip3 install --user -r dashboard/requirements.txt
cp dashboard/.env.example dashboard/.env
./start-dashboard.sh
```

Abra depois `http://localhost:8377`.

## Estrutura

- `dashboard/`: UI, API, IA, banco e sincronizacao
- `tracking-worker/`: coletor de eventos de email
- `proposals/`: rascunhos e templates de campanha
- `scripts/`: utilitarios de importacao
- `samples/`: exemplos locais nao versionados por padrao

## Observacao

O repositório deve manter apenas dados seguros para compartilhamento. Informacao
de cliente, planilhas de operacao e credenciais ficam fora do Git.
