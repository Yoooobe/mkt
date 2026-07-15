# 4unik.mail Project Rules

## Escopo
Este projeto é o `4unik.mail`: dashboard local de email marketing com IA, CRM leve,
campanhas, templates, fila persistente, autenticação Gmail via `gws` e tracking opcional
por Cloudflare Worker + D1.

## Guardrail absoluto de envio
- Este ambiente pode conter contatos reais depois da importação do Genal.
- Nunca execute envio, ciclo de fila, ativação de campanha ou alteração que destrave envio
  sem confirmação explícita do operador.
- Campanhas devem nascer em `rascunho`; templates de IA nascem `pendente`.
- Antes de qualquer envio real, validar remetente, audiência, tags, opt-out, limites e
  conteúdo aprovado.
- Para teste, usar apenas contatos marcados com a tag `teste`.

## Cliente e prospecção
- Tags protegidas de cliente: `cliente_ativo_4unik`, `cliente_inativo_4unik`,
  `possivel_cliente_4unik`.
- Prospecção não deve mirar clientes atuais sem confirmação explícita.
- Relacionamento/newsletter para clientes deve ser tratado como campanha separada.

## Marca
- Produto: 4Unik / 4unik.mail.
- Posicionamento: infraestrutura de recompensas corporativas.
- Assuntos centrais: gamificação, carteiras/pontos, loja corporativa, API/webhooks,
  fulfillment, rastreio, ROI, eNPS, budget, governança e LGPD.
- Não usar narrativas antigas de outros produtos em novas campanhas.

## Operação
- Gmail via `gws`: leitura livre para análise; envio somente quando explicitamente autorizado.
- Google Calendar: não criar/editar/deletar eventos sem ordem explícita.
- WhatsApp/Z-API: usar apenas para notificações internas ao número configurado.
- Segredos devem ficar em `.env` locais e nunca no zip/repositório.
