# 4unik.mail — dashboard local de email marketing com IA

MVP local em `http://localhost:8377` usando FastAPI, SQLite e JavaScript simples.
O sistema gerencia contatos, tags, campanhas, templates, fila de envio, respostas,
bounces e tracking opcional de abertura/clique.

## Subir
```bash
pip3 install --user -r dashboard/requirements.txt
cp dashboard/.env.example dashboard/.env
./start-dashboard.sh
open http://localhost:8377
```

## Telas
Início, Campanhas, CRM, Qualidade, Respostas, Remetentes, Templates, Métricas e
Contexto IA. Os KBs iniciais ficam em `dashboard/context/kb/` e orientam a IA para
reward infrastructure, gamificação, carteiras, loja corporativa, API, fulfillment e ROI.

## Guardrails
- Campanhas nascem em rascunho e só enviam depois de ativação explícita.
- Templates de IA nascem pendentes e precisam aprovação.
- Respostas pausam a sequência do contato.
- Opt-out, hard bounce e soft bounce reincidente suprimem contatos.
- Tracking de abertura/clique é opcional via os endpoints públicos
  `https://plataforma.4unik.com.br/api/email/*`, persistidos no Sanity.
- No boot e a cada ciclo de 30 min, o **Ciclo de estratégia** sincroniza dados,
  corrige tags, atualiza a qualidade do CRM e verifica o Claude. Ele não cria,
  aprova, ativa nem envia campanhas automaticamente.
- Telefone, e-mail e observações individuais ficam no CRM. A IA recebe somente
  contagens e perfis agregados (histórico, etapas, telefone preenchido), para
  melhorar a estratégia sem expor dados pessoais na geração de copy.
